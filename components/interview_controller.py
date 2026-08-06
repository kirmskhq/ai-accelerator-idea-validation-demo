from langflow.custom import Component
from langflow.io import MessageTextInput, SecretStrInput, StrInput, SliderInput, Output
from langflow.schema.message import Message
from langflow.schema import Data


class InterviewControllerComponent(Component):
    """Deterministic interview orchestrator (v3.2 — graph node, not a tool).

    v3 and v3.1 exposed this as a tool (``submit_answer``) that the agent had
    to remember to call every turn. In testing the agent skipped it on most
    turns and just improvised the conversation instead — the scoring never
    ran. This version is wired directly into the graph (Chat Input -> here ->
    Agent's system_prompt), so Langflow executes it on every turn regardless
    of what the agent decides. The agent can no longer skip it because it
    never had a choice to call it in the first place.

    Output is one Message: static persona + rules, followed by either the
    next-topic instruction or, once all four topics are closed, the
    instruction to call search_market then compute_verdict.
    """

    display_name = "Interview Controller"
    description = (
        "Reads the whole conversation, scores the open topics, and builds "
        "the agent's system prompt for this turn (mirrors "
        "core/interview.py:_read_state()+_focus())."
    )
    icon = "route"
    name = "InterviewController"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]
    DIM_TITLE_RU = {
        "segment": "Аудитория",
        "behavior": "Поведение",
        "alternatives": "Альтернативы",
        "test": "Проверка",
    }
    DIG = {
        "segment": "кто именно сталкивается с проблемой — конкретная группа, а не вся категория",
        "behavior": "что эти люди делают с проблемой сейчас — наблюдаемые действия, а не пожелания",
        "alternatives": "чем люди уже пытались закрыть проблему и во что это обошлось",
        "test": "как он проверит спрос дёшево и что сочтёт провалом",
    }
    MAX_QUESTIONS = 10
    MAX_ATTEMPTS = 2
    TRANSCRIPT_LINES = 10

    RUBRIC = {
        "segment": (
            "2 — названа не вся категория, а группа внутри неё — по признаку, "
            "ситуации или месту. Понятно, где этих людей искать.\n"
            "1 — названа категория целиком: «владельцы собак», «бизнес», "
            "«родители». Внутри неё ничего не выделено.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "behavior": (
            "2 — названо конкретное регулярное действие ИЛИ последний случай, "
            "и при нём есть измеримый параметр — время, частота или деньги. "
            "Хватает одного числа.\n"
            "1 — названо действие, но без чисел и частоты.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "alternatives": (
            "2 — названо, чем люди уже пользуются или что пробовали — сервис, "
            "инструмент, костыль, ручной способ — и во что это обходится.\n"
            "1 — «ничего нет», «аналогов не существует», «искал и не нашёл» — "
            "без единого названного способа, которым люди обходятся сейчас.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "test": (
            "2 — есть проверка на дни-недели: что сделает, с кем, и какой "
            "результат сочтёт провалом.\n"
            "1 — «сделаем MVP и посмотрим» — без срока, без критерия.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
    }

    SCORER_SYSTEM = (
        "Ты читаешь расшифровку интервью в стартап-акселераторе и возвращаешь "
        "состояние строгим JSON. С пользователем ты не разговариваешь.\n\n"
        "Оценивай строго по рубрике. «Звучит разумно» — это не 2. Двойка "
        "ставится, только если сказанное можно проверить. Если ответ можно "
        "без изменений приклеить к любой другой идее — это максимум 1.\n\n"
        "ГЛАВНОЕ: засчитывай сказанное в ЛЮБОМ месте разговора, даже если это "
        "прозвучало в ответе на другой вопрос. Человек не обязан повторяться. "
        "Если тема уже раскрыта раньше — ставь двойку сразу, не жди "
        "отдельного вопроса про неё."
    )

    STATIC_PERSONA = """Ты — первый фильтр стартап-акселератора. К тебе пришёл человек с улицы, и за
несколько вопросов надо понять, есть ли у него рабочая идея технологического
бизнеса.

Ты ведёшь живой разговор по-русски, на «ты». Коротко, по-человечески, без
канцелярита и смайликов. Ты не коуч и не продавец — ты человек, который
тридцать раз слышал похожее и умеет отличать конкретику от общих слов.

Правила вопросов:
- Один вопрос за реплику. Не два и не три.
- Не перечисляй варианты в вопросе: не «может, какие-то программы, каталоги, сервисы?».
- Не подсказывай признаки для сужения аудитории: ни возраст, ни город, ни размер
  компании. Вместо этого спрашивай, откуда он знает про проблему.
- Не придумывай идею за человека. Не приводи примеров.
- Не хвали, не критикуй и не выноси вердикт по ходу разговора.
- Не переспрашивай то, что человек уже сказал. Ты помнишь весь разговор.
- Вопросы про прошлое сильнее вопросов про будущее: не «что им нужно», а «что
  они делали в последний раз».

Формат ответа:
- Начинай сразу с текста реплики. Никаких пустых строк в начале.
- Не показывай оценки и служебные пометки человеку — это только для тебя.
"""

    inputs = [
        MessageTextInput(
            name="answer",
            display_name="User Message (from Chat Input)",
            required=True,
            info="Wire this from Chat Input. Runs on every turn automatically.",
        ),
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(
            name="model",
            display_name="Scoring Model",
            value="deepseek-v4-flash",
            info="Model for scoring. Must support structured output (JSON schema).",
        ),
        SliderInput(
            name="temperature",
            display_name="Scoring Temperature",
            value=0.0,
            range_spec={"min": 0.0, "max": 1.0, "step": 0.01},
        ),
    ]

    outputs = [
        Output(display_name="System Prompt", name="system_prompt", method="run_controller"),
    ]

    def _model_uri(self) -> str:
        model = self.model or "deepseek-v4-flash"
        if not model.startswith(("gpt://", "ds://")):
            if "/" not in model:
                model = model + "/latest"
            model = "gpt://" + self.folder_id + "/" + model
        return model

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

    def _fresh_state(self) -> dict:
        return {
            "current_topic": self.DIM_ORDER[0],
            "topics": {
                d: {"score": 0, "attempts": 0, "evidence": "", "missing": ""}
                for d in self.DIM_ORDER
            },
            "questions_asked": 0,
            "idea": "",
            "entry": "idea",
            "not_tech": False,
            "not_tech_reason": "",
        }

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
            msgs = []

        if msgs:
            try:
                loaded = json.loads(msgs[0].text)
                base = self._fresh_state()
                base.update(loaded)
                return base
            except (json.JSONDecodeError, AttributeError):
                pass
        return self._fresh_state()

    def _save_state(self, state: dict):
        import json
        from langflow.memory import store_message
        from langflow.schema.message import Message as Msg

        msg = Msg(
            text=json.dumps(state, ensure_ascii=False),
            sender="Machine",
            sender_name="__controller_state__",
            session_id=self.graph.session_id,
        )
        try:
            store_message(msg, flow_id=self._flow_uuid())
        except Exception:
            pass

    def _transcript(self, latest_answer: str) -> str:
        from langflow.memory import get_messages

        lines = []
        try:
            msgs = get_messages(
                session_id=self.graph.session_id,
                order_by="timestamp",
                order="ASC",
                flow_id=self._flow_uuid(),
            )
        except Exception:
            msgs = []

        for m in msgs:
            if m.sender_name == "__controller_state__":
                continue
            text = (m.text or "").strip()
            if not text:
                continue
            if m.sender == "Machine":
                lines.append("Бот: " + text)
            elif m.sender == "User":
                lines.append("Человек: " + text)

        answer = (latest_answer or "").strip()
        if answer and not any(answer in ln for ln in lines):
            lines.append("Человек: " + answer)

        return "\n".join(lines[-self.TRANSCRIPT_LINES:])

    def _score_open(self, transcript: str, dims: list) -> dict:
        import json
        import openai

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=self.folder_id,
            timeout=90,
        )

        schema = {
            "type": "object",
            "properties": {
                "idea": {"type": "string"},
                "entry": {"type": "string", "enum": ["nothing", "idea", "built"]},
                "not_tech": {"type": "boolean"},
                "not_tech_reason": {"type": "string"},
                "topics": {
                    "type": "object",
                    "properties": {
                        d: {
                            "type": "object",
                            "properties": {
                                "score": {"type": "integer", "enum": [0, 1, 2]},
                                "evidence": {"type": "string"},
                                "missing": {"type": "string"},
                            },
                            "required": ["score", "evidence", "missing"],
                            "additionalProperties": False,
                        }
                        for d in dims
                    },
                    "required": list(dims),
                    "additionalProperties": False,
                },
            },
            "required": ["idea", "entry", "not_tech", "not_tech_reason", "topics"],
            "additionalProperties": False,
        }

        rubric_block = "\n\n".join(
            self.DIM_TITLE_RU[d] + " (" + d + ")\n" + self.RUBRIC[d] for d in dims
        )
        prompt = (
            "РАЗГОВОР:\n" + transcript + "\n\n"
            "Верни состояние по каждой из тем ниже.\n\n" + rubric_block + "\n\n"
            "idea — идея человека ЕГО словами, одной фразой.\n"
            "entry — с чем пришёл: nothing (идеи нет), idea (названа, ничего не "
            "построено), built (есть прототип, продукт, пользователи, продажи).\n"
            "not_tech — true, только если это явно НЕ технологический бизнес.\n"
            "evidence — одна фраза, почему такая оценка.\n"
            "missing — чего не хватает до 2, одной фразой; при 2 — пустая строка."
        )

        last_err = ""
        for attempt in range(2):
            text = prompt if not attempt else (
                prompt + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАСПАРСЕН (" + last_err
                + "). Верни только валидный JSON."
            )
            try:
                resp = client.responses.create(
                    model=self._model_uri(),
                    instructions=self.SCORER_SYSTEM,
                    input=text,
                    temperature=self.temperature,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "state_result",
                            "schema": schema,
                            "strict": True,
                        }
                    },
                )
                return json.loads(resp.output_text or "{}")
            except Exception as exc:
                last_err = str(exc)

        return {"_error": last_err, "topics": {}}

    def run_controller(self) -> Message:
        state = self._load_state()

        raw = self.answer
        answer = raw.text if hasattr(raw, "text") else str(raw)
        answer = (answer or "").strip()

        topics = state["topics"]
        topic = state["current_topic"] or self.DIM_ORDER[0]

        if answer:
            state["questions_asked"] += 1
            topics[topic]["attempts"] += 1

            open_dims = [d for d in self.DIM_ORDER if topics[d]["score"] < 2]
            if open_dims:
                transcript = self._transcript(answer)
                fresh = self._score_open(transcript, open_dims)
                fresh_topics = fresh.get("topics") or {}

                for d in open_dims:
                    item = fresh_topics.get(d) or {}
                    new_score = item.get("score")
                    if new_score in (0, 1, 2):
                        if int(new_score) > int(topics[d]["score"]):
                            topics[d]["score"] = int(new_score)
                        topics[d]["evidence"] = str(item.get("evidence", ""))
                        topics[d]["missing"] = str(item.get("missing", ""))

                if fresh.get("idea"):
                    state["idea"] = str(fresh["idea"])
                if fresh.get("entry") in ("nothing", "idea", "built"):
                    state["entry"] = fresh["entry"]
                state["not_tech"] = bool(fresh.get("not_tech", state.get("not_tech", False)))
                if fresh.get("not_tech_reason"):
                    state["not_tech_reason"] = str(fresh["not_tech_reason"])

        def _next_topic():
            for d in self.DIM_ORDER:
                if topics[d]["score"] < 2 and topics[d]["attempts"] < self.MAX_ATTEMPTS:
                    return d
            return None

        nxt_for_check = _next_topic()
        total_attempts = sum(topics[d]["attempts"] for d in self.DIM_ORDER)
        complete = (
            all(topics[d]["score"] == 2 for d in self.DIM_ORDER)
            or total_attempts >= self.MAX_QUESTIONS
            or nxt_for_check is None
        )

        if complete:
            block = (
                "\nИнтервью завершено, вопросов больше не задавай.\n"
                "1) Вызови search_market с запросом по идее и аудитории: "
                + (state["idea"] or "идея не названа") + ".\n"
                "2) Вызови compute_verdict и передай ТОЛЬКО market_count — "
                "число аналогов из search_market, цифрами. Оценки и остальное "
                "он возьмёт из состояния сам.\n"
                "3) Перескажи человеку результат compute_verdict, не меняя сути."
            )
            state["current_topic"] = topic
        elif topics[topic]["score"] == 2:
            nxt = _next_topic()
            state["current_topic"] = nxt
            closed = [self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if topics[d]["score"] == 2]
            block = (
                "\nСЛУЖЕБНО: задано попыток " + str(total_attempts) + " из "
                + str(self.MAX_QUESTIONS) + ". Уже закрыто: " + (", ".join(closed) or "ничего") + ".\n"
                "СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — " + self.DIM_TITLE_RU[nxt] + ".\n"
                "Что нужно вытянуть: " + self.DIG[nxt] + ".\n"
                "Задай один вопрос по этой теме."
            )
        elif topics[topic]["attempts"] < self.MAX_ATTEMPTS:
            closed = [self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if topics[d]["score"] == 2]
            missing = topics[topic]["missing"]
            block = (
                "\nСЛУЖЕБНО: задано попыток " + str(total_attempts) + " из "
                + str(self.MAX_QUESTIONS) + ". Уже закрыто: " + (", ".join(closed) or "ничего") + ".\n"
                "СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — " + self.DIM_TITLE_RU[topic] + ".\n"
                "Тема ещё не закрыта" + (". Не хватает: " + missing if missing else "") + ".\n"
                "Задай второй вопрос по ТОЙ ЖЕ теме с другой стороны: про конкретный "
                "случай, про источник знания, про числа. Не повторяй свой прошлый "
                "вопрос дословно."
            )
        else:
            nxt = _next_topic()
            state["current_topic"] = nxt
            closed = [self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if topics[d]["score"] == 2]
            block = (
                "\nСЛУЖЕБНО: задано попыток " + str(total_attempts) + " из "
                + str(self.MAX_QUESTIONS) + ". Уже закрыто: " + (", ".join(closed) or "ничего") + ".\n"
                "Тема «" + self.DIM_TITLE_RU[topic] + "» не закрыта за две попытки. "
                "Долбить бессмысленно.\n"
                "СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — " + self.DIM_TITLE_RU[nxt] + ".\n"
                "Что нужно вытянуть: " + self.DIG[nxt] + ".\n"
                "Задай один вопрос по этой теме."
            )

        self._save_state(state)
        result_text = self.STATIC_PERSONA + block
        self.status = {"complete": complete, "topic": state["current_topic"],
                       "scores": {d: topics[d]["score"] for d in self.DIM_ORDER}}
        return Message(text=result_text)
