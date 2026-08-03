from langflow.custom import Component
from langflow.io import MessageTextInput, Output
from langflow.schema import Data


class VerdictComponent(Component):
    display_name = "Verdict R1-R5"
    description = "Deterministic pass / improve / not-tech decision computed from the four topic scores (mirrors core/verdict.py:decide()). Call once, at the very end of the interview."
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
        "behavior": "Реальное поведение: опиши, что эти люди делают сейчас — какими инструментами, за какие деньги, сколько времени тратят. Не то, что они хотели бы, а то, что уже происходит.",
        "alternatives": "Альтернативы: выясни, чем эти люди уже закрывают проблему — каким сервисом, костылём или ручным способом, — и во что им это обходится в деньгах и времени. «Аналогов нет» почти всегда значит, что плохо искали.",
        "test": "Проверка: придумай эксперимент на неделю и до 10 000 ₽, у которого заранее назван результат, считающийся провалом.",
    }

    inputs = [
        MessageTextInput(name="scores_json", display_name="Scores JSON", required=True, tool_mode=True,
                         info='Score 0-2 per topic as JSON, e.g. {"segment": 2, "behavior": 1, "alternatives": 0, "test": 0}'),
        MessageTextInput(name="entry", display_name="Entry", value="idea", tool_mode=True,
                         info="What the person arrived with: nothing, idea, or built."),
        MessageTextInput(name="not_tech", display_name="Not Tech", value="false", tool_mode=True,
                         info="true only if this is clearly not a technology business, otherwise false."),
        MessageTextInput(name="not_tech_reason", display_name="Not Tech Reason", value="", tool_mode=True,
                         info="One short phrase in Russian saying why it is not a tech business. Empty when not_tech is false."),
        MessageTextInput(name="market_count", display_name="Competitor Count", value="-1", tool_mode=True,
                         info="How many working competitors search_market found. Use -1 if the market search was not run."),
    ]

    outputs = [
        Output(display_name="Verdict", name="verdict", method="compute_verdict"),
    ]

    @staticmethod
    def _as_bool(value):
        return str(value).strip().lower() in ("true", "1", "yes", "да")

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
            return "Поиск не нашёл ни одного работающего аналога. Обычно это значит одно из двух: либо рынка нет, либо запрос был сформулирован неудачно. Проверь руками, прежде чем радоваться."
        word = self._plural(count, "работающий аналог", "работающих аналога", "работающих аналогов")
        if count >= self.CROWDED_MARKET:
            return "Найдено " + str(count) + " " + word + " — ниша плотная. Само по себе это не стоп, но нужен внятный ответ, почему клиент уйдёт от них к тебе."
        return "Найдено " + str(count) + " " + word + " — это нормальный признак живого рынка."

    def compute_verdict(self) -> Data:
        import json

        raw = self.scores_json
        raw = raw.text if hasattr(raw, "text") else str(raw)
        try:
            scores = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            scores = {}
        if not isinstance(scores, dict):
            scores = {}

        entry = str(self.entry or "idea").strip().lower()
        not_tech = self._as_bool(self.not_tech)
        not_tech_reason = str(self.not_tech_reason or "").strip()
        count = self._as_int(self.market_count, -1)

        filled = {d: self._as_int(scores.get(d, 0), 0) for d in self.DIM_ORDER}
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
                "explanation": "Все четыре темы закрыты конкретно. Но пока это конкретика на словах: за ней не стоят разговоры с живыми людьми. Следующий шаг не вердикт, а поле.",
                "weak_dims": [], "homework": self.HOMEWORK_BEGINNER, "market_note": note, "total": total,
            }
        else:
            result = {
                "outcome": "pass", "rule": "R5",
                "explanation": "Все четыре темы закрыты конкретно: понятна аудитория, видно реальное поведение людей, известно чем они обходятся сегодня и есть дешёвый способ проверить гипотезу.",
                "weak_dims": [], "homework": self.HOMEWORK_BEGINNER, "market_note": note, "total": total,
            }

        self.status = result
        return Data(data=result)
