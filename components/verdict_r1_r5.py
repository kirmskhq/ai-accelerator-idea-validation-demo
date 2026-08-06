from langflow.custom import Component
from langflow.io import MessageTextInput, Output
from langflow.schema import Data


class VerdictComponent(Component):
    """Deterministic verdict.

    Scores, entry and not_tech are read from the controller state in session
    memory instead of being passed in by the agent.  The agent kept sending
    booleans and ints into string inputs, which Langflow rejected and then
    retried — costing a whole extra model round-trip per interview.
    """

    display_name = "Verdict R1-R5"
    description = (
        "Deterministic pass / improve / not-tech decision. Reads the interview "
        "scores from session state itself. Pass only how many working "
        "competitors search_market found. Call once, at the very end."
    )
    icon = "gavel"
    name = "Verdict"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]
    DIM_TITLE_RU = {
        "segment": "Аудитория",
        "behavior": "Поведение",
        "alternatives": "Альтернативы",
        "test": "Проверка",
    }
    CROWDED_MARKET = 10

    HOMEWORK_BEGINNER = [
        "Найди пять живых людей из своего сегмента — не друзей и не коллег по учёбе.",
        "Поговори с каждым по 20–30 минут. Спрашивай только про прошлое: что они делали в последний раз, когда столкнулись с проблемой, сколько это стоило им времени и денег.",
        "Ни разу не рассказывай про своё решение. Как только ты его назовёшь, человек начнёт вежливо соглашаться, и интервью можно выбрасывать.",
        "Запиши дословные цитаты, а не пересказ. Возвращайся с ними.",
    ]
    DIM_HOMEWORK = {
        "segment": "Сегмент: сузь категорию до группы внутри неё — по признаку, ситуации или месту, — и скажи, где этих людей искать.",
        "behavior": "Реальное поведение: опиши, что эти люди делают сейчас — какими инструментами, за какие деньги, сколько времени тратят.",
        "alternatives": "Альтернативы: выясни, чем эти люди уже закрывают проблему и во что им это обходится в деньгах и времени.",
        "test": "Проверка: придумай эксперимент на неделю и до 10 000 ₽, у которого заранее назван результат, считающийся провалом.",
    }

    inputs = [
        MessageTextInput(
            name="market_count",
            display_name="Competitor Count",
            value="-1",
            tool_mode=True,
            info=(
                "How many working competitors search_market found, as digits. "
                "Use -1 if the market search was not run."
            ),
        ),
    ]

    outputs = [
        Output(display_name="Verdict", name="verdict", method="compute_verdict"),
    ]

    def _flow_uuid(self):
        from uuid import UUID

        fid = getattr(self.graph, "flow_id", None)
        if isinstance(fid, UUID):
            return fid
        if isinstance(fid, str):
            try:
                return UUID(fid)
            except ValueError:
                return None
        return None

    def _load_state(self) -> dict:
        import json
        from langflow.memory import get_messages

        try:
            msgs = get_messages(
                session_id=self.graph.session_id,
                sender_name="__controller_state__",
                order_by="timestamp",
                order="DESC",
                limit=1,
                flow_id=self._flow_uuid(),
            )
        except Exception:
            return {}
        if msgs:
            try:
                return json.loads(msgs[0].text)
            except (json.JSONDecodeError, AttributeError):
                return {}
        return {}

    @staticmethod
    def _as_int(value, default):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _plural(self, n, one, few, many):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return few
        return many

    def _market_note(self, count):
        if count < 0:
            return "Проверка рынка не выполнена — оцени конкурентов вручную."
        if count == 0:
            return "Поиск не нашёл ни одного работающего аналога. Обычно это значит одно из двух: либо рынка нет, либо запрос был сформулирован неудачно."
        word = self._plural(count, "работающий аналог", "работающих аналога", "работающих аналогов")
        if count >= self.CROWDED_MARKET:
            return "Найдено " + str(count) + " " + word + " — ниша плотная. Нужен внятный ответ, почему клиент уйдёт от них к тебе."
        return "Найдено " + str(count) + " " + word + " — это нормальный признак живого рынка."

    def compute_verdict(self) -> Data:
        state = self._load_state()
        topics = state.get("topics") or {}
        entry = str(state.get("entry") or "idea").strip().lower()
        not_tech = bool(state.get("not_tech", False))
        not_tech_reason = str(state.get("not_tech_reason") or "").strip()

        raw = self.market_count
        raw = raw.text if hasattr(raw, "text") else raw
        count = self._as_int(raw, -1)

        filled = {
            d: self._as_int((topics.get(d) or {}).get("score", 0), 0)
            for d in self.DIM_ORDER
        }
        total = sum(filled.values())
        weak = [d for d in self.DIM_ORDER if filled[d] != 2]
        note = self._market_note(count)

        if not state:
            result = {
                "outcome": "improve", "rule": "R0",
                "explanation": "Состояние интервью не найдено — оценки недоступны. Проверь, вызывался ли submit_answer.",
                "weak_dims": self.DIM_ORDER, "homework": [], "market_note": note, "total": 0,
            }
        elif not_tech:
            result = {
                "outcome": "not_tech", "rule": "R1",
                "explanation": "Задача акселератора — технологические компании, которые могут расти без пропорционального роста затрат. " + (not_tech_reason or "Здесь этого признака нет."),
                "weak_dims": weak, "homework": [], "market_note": note, "total": total,
            }
        elif any(filled[d] == 0 for d in self.DIM_ORDER):
            names = ", ".join(self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if filled[d] == 0)
            result = {
                "outcome": "improve", "rule": "R2",
                "explanation": "Нет ответа по темам: " + names + ". Без них оценивать нечего.",
                "weak_dims": weak, "homework": [self.DIM_HOMEWORK[d] for d in weak], "market_note": note, "total": total,
            }
        elif any(filled[d] == 1 for d in self.DIM_ORDER):
            names = ", ".join(self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if filled[d] == 1)
            result = {
                "outcome": "improve", "rule": "R3",
                "explanation": "Ответы по темам «" + names + "» остались общими. Проходит только конкретика: живые люди, наблюдаемые действия, проверяемые сроки.",
                "weak_dims": weak, "homework": [self.DIM_HOMEWORK[d] for d in weak], "market_note": note, "total": total,
            }
        elif entry == "nothing":
            result = {
                "outcome": "pass_homework", "rule": "R4",
                "explanation": "Все четыре темы закрыты конкретно. Но пока это конкретика на словах: за ней не стоят разговоры с живыми людьми.",
                "weak_dims": [], "homework": self.HOMEWORK_BEGINNER, "market_note": note, "total": total,
            }
        else:
            result = {
                "outcome": "pass", "rule": "R5",
                "explanation": "Все четыре темы закрыты конкретно: понятна аудитория, видно реальное поведение людей, известно чем они обходятся сегодня и есть дешёвый способ проверить гипотезу.",
                "weak_dims": [], "homework": self.HOMEWORK_BEGINNER, "market_note": note, "total": total,
            }

        result["scores"] = filled
        self.status = result
        return Data(data=result)
