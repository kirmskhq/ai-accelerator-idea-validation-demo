from langflow.custom import Component
from langflow.io import Output
from langflow.schema import Data


class VerdictComponent(Component):
    """Deterministic verdict: rules R1-R5 applied to the interview state.

    Takes no arguments. The scores, entry stage and not-tech flag come from the
    Interview Controller's state, and the number of similar products comes from
    the record Yandex Web Search leaves in the same state session. The agent
    therefore has nothing to pass in and nothing to get wrong.

    Fails loudly: if the state cannot be read, is missing or is malformed, the
    tool raises an error that tells the agent not to improvise a verdict. An
    earlier version substituted zeros instead, and produced a confident,
    well-argued and completely wrong "improve" verdict.

    Self-contained on purpose (one file per Langflow node), so the state
    helpers are repeated from interview_controller.py.
    """

    display_name = "Verdict R1-R5"
    description = (
        "Deterministic pass / improve / not-tech decision. Takes no arguments: "
        "reads the interview scores and the market search result from session "
        "state. Call once, at the very end, after search_market."
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

    STATE_SESSION_SUFFIX = "::interview-state"
    STATE_SENDER_NAME = "__controller_state__"
    MARKET_SENDER_NAME = "__market_search__"

    AGENT_ON_ERROR = (
        " Вердикт не выноси и не придумывай: скажи человеку, что произошёл "
        "технический сбой и вердикт посчитать не удалось."
    )

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

    inputs = []

    outputs = [
        Output(display_name="Verdict", name="verdict", method="compute_verdict"),
    ]

    def _flow_uuid(self):
        # Inside a tool call Langflow hands components a placeholder graph whose
        # flow_id is a string; the message store needs a UUID.
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

    def _state_session_id(self) -> str:
        return str(self.graph.session_id) + self.STATE_SESSION_SUFFIX

    def _latest_record(self, sender_name: str):
        """Return the latest JSON record by sender_name, or None if there is none."""
        import json
        from langflow.memory import get_messages

        try:
            msgs = get_messages(
                session_id=self._state_session_id(),
                sender_name=sender_name,
                order_by="timestamp",
                order="DESC",
                limit=1,
                flow_id=self._flow_uuid(),
            )
        except Exception as exc:
            raise ValueError("Не удалось прочитать состояние интервью: " + str(exc) + "." + self.AGENT_ON_ERROR) from exc
        if not msgs:
            return None
        try:
            return json.loads(msgs[0].text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Состояние интервью повреждено: " + str(exc) + "." + self.AGENT_ON_ERROR) from exc

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
            return "Поиск не нашёл ни одного сайта с похожим продуктом. Обычно это значит одно из двух: либо рынка нет, либо запрос был сформулирован неудачно."
        word = self._plural(count, "сайт", "сайта", "сайтов")
        found = "Поиск нашёл " + str(count) + " " + word + " с похожими продуктами"
        if count >= self.CROWDED_MARKET:
            return found + " — ниша плотная. Нужен внятный ответ, почему клиент уйдёт от них к тебе."
        return found + " — это нормальный признак живого рынка."

    def compute_verdict(self) -> Data:
        state = self._latest_record(self.STATE_SENDER_NAME)
        if not state:
            raise ValueError("Состояние интервью не найдено." + self.AGENT_ON_ERROR)

        topics = state.get("topics")
        if not isinstance(topics, dict) or any(d not in topics for d in self.DIM_ORDER):
            raise ValueError("В состоянии интервью нет оценок по всем четырём темам." + self.AGENT_ON_ERROR)

        filled = {}
        for d in self.DIM_ORDER:
            score = (topics.get(d) or {}).get("score")
            if score not in (0, 1, 2):
                raise ValueError("Недопустимая оценка по теме " + d + ": " + repr(score) + "." + self.AGENT_ON_ERROR)
            filled[d] = int(score)

        entry = str(state.get("entry") or "idea").strip().lower()
        not_tech = bool(state.get("not_tech", False))
        not_tech_reason = str(state.get("not_tech_reason") or "").strip()

        market = self._latest_record(self.MARKET_SENDER_NAME) or {}
        try:
            count = int(market.get("count", -1))
        except (TypeError, ValueError):
            count = -1

        total = sum(filled.values())
        weak = [d for d in self.DIM_ORDER if filled[d] != 2]
        note = self._market_note(count)

        if not_tech:
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
