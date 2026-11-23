from langflow.custom import Component
from langflow.io import MessageTextInput, SecretStrInput, StrInput, SliderInput, Output
from langflow.schema.message import Message


class InterviewControllerComponent(Component):
    """Deterministic interview orchestrator, wired into the graph as a node.

    An earlier version exposed this logic as a tool the agent had to call on
    every turn. In testing the agent called it on one turn in five and
    improvised the rest, so the scoring never ran. As a graph node
    (Chat Input -> here -> Agent's system prompt) it runs on every turn and the
    agent has no way to skip it.

    The model is used for one thing only: scoring the open topics 0/1/2
    against the rubric. Everything around that judgement is code: monotonic
    merging (a 2 never falls back), attempt counters, the question budget,
    the choice of the next topic and the instruction the agent receives.

    State lives in a companion session ("<session_id>::interview-state"), not
    in the chat session itself, so the agent's chat memory never sees the
    scores. Storage failures raise instead of silently starting over.

    This file is self-contained on purpose: Langflow custom components are
    pasted one file per node, so a few helpers are repeated in
    verdict_r1_r5.py and yandex_web_search.py.
    """

    display_name = "Interview Controller"
    description = (
        "Reads the recent conversation, scores the open topics, and builds "
        "the agent's system prompt for this turn."
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
    MAX_SCORE_ERRORS_IN_ROW = 2
    TRANSCRIPT_LINES = 10

    STATE_SESSION_SUFFIX = "::interview-state"
    STATE_SENDER_NAME = "__controller_state__"
    YANDEX_BASE = "https://ai.api.cloud.yandex.net/v1"

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
- Вопрос не длиннее 25 слов. На финальный пересказ вердикта это не распространяется.
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
        SecretStrInput(name="api_key", display_name="API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=False,
                 info="Yandex AI Studio only."),
        StrInput(
            name="model",
            display_name="Scoring Model",
            value="gpt-oss-120b",
            info="Model for scoring. Must support structured output (JSON schema).",
        ),
        SliderInput(
            name="temperature",
            display_name="Scoring Temperature",
            value=0.0,
            range_spec={"min": 0.0, "max": 1.0, "step": 0.01},
        ),
        StrInput(
            name="api_base",
            display_name="API Base URL",
            value=YANDEX_BASE,
            advanced=True,
            info="OpenAI-compatible endpoint with the Responses API, e.g. https://api.neuraldeep.ru/v1",
        ),
    ]

    outputs = [
        Output(display_name="System Prompt", name="system_prompt", method="run_controller"),
    ]

    # ---------------------------------------------------------------- helpers

    def _is_yandex(self) -> bool:
        return "api.cloud.yandex.net" in (self.api_base or self.YANDEX_BASE)

    def _model_uri(self) -> str:
        # Yandex AI Studio expects gpt://<folder>/<model>/latest; other
        # OpenAI-compatible providers take the model name as is.
        model = self.model or "gpt-oss-120b"
        if not self._is_yandex() or model.startswith(("gpt://", "ds://")):
            return model
        if not self.folder_id:
            raise ValueError("Yandex Folder ID is required for Yandex AI Studio models.")
        if "/" not in model:
            model = model + "/latest"
        return "gpt://" + self.folder_id + "/" + model

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

    def _fresh_state(self) -> dict:
        return {
            "current_topic": self.DIM_ORDER[0],
            "topics": {
                d: {"score": 0, "attempts": 0, "missing": ""}
                for d in self.DIM_ORDER
            },
            "questions_asked": 0,
            "idea": "",
            "entry": "idea",
            "not_tech": False,
            "not_tech_reason": "",
            "last_score_error": "",
            "score_errors_in_row": 0,
        }

    def _session_messages(self) -> list:
        from langflow.memory import get_messages

        try:
            return get_messages(
                session_id=self.graph.session_id,
                order_by="timestamp",
                order="ASC",
                flow_id=self._flow_uuid(),
            ) or []
        except Exception as exc:
            msg = "Interview Controller could not read the chat history: " + str(exc)
            raise RuntimeError(msg) from exc

    def _load_state(self, has_bot_turn: bool) -> dict:
        import json
        from langflow.memory import get_messages

        try:
            msgs = get_messages(
                session_id=self._state_session_id(),
                sender_name=self.STATE_SENDER_NAME,
                order_by="timestamp",
                order="DESC",
                limit=1,
                flow_id=self._flow_uuid(),
            )
        except Exception as exc:
            msg = "Interview Controller could not read its state: " + str(exc)
            raise RuntimeError(msg) from exc

        if not msgs:
            if has_bot_turn:
                # The bot has already asked questions in this chat, so state must
                # exist. Starting from zero would silently re-run the interview
                # and could produce a verdict on scores that were never given.
                msg = (
                    "Interview state for this chat was not found although the "
                    "interview is already in progress. Refusing to start over."
                )
                raise RuntimeError(msg)
            return self._fresh_state()

        try:
            loaded = json.loads(msgs[0].text)
        except (json.JSONDecodeError, TypeError) as exc:
            msg = "Interview state for this chat is corrupted: " + str(exc)
            raise RuntimeError(msg) from exc

        state = self._fresh_state()
        state.update(loaded)
        for d in self.DIM_ORDER:
            topic = self._fresh_state()["topics"][d]
            topic.update((state.get("topics") or {}).get(d) or {})
            state["topics"][d] = topic
        return state

    def _save_state(self, state: dict) -> None:
        import json
        from langflow.memory import store_message

        msg = Message(
            text=json.dumps(state, ensure_ascii=False),
            sender="Machine",
            sender_name=self.STATE_SENDER_NAME,
            session_id=self._state_session_id(),
        )
        try:
            store_message(msg, flow_id=self._flow_uuid())
        except Exception as exc:
            err = "Interview Controller could not save its state: " + str(exc)
            raise RuntimeError(err) from exc

    def _transcript(self, msgs: list, latest_answer: str) -> str:
        lines = []
        for m in msgs:
            if str(m.sender_name or "").startswith("__"):
                continue
            text = (m.text or "").strip()
            if not text:
                continue
            if m.sender == "Machine":
                lines.append("Бот: " + text)
            elif m.sender == "User":
                lines.append("Человек: " + text)

        # Chat Input normally stores the message before this node runs. Add the
        # latest answer only if it is not already the last line. (A substring
        # check here used to drop short answers such as «да» or a single number.)
        latest_line = "Человек: " + (latest_answer or "").strip()
        if latest_answer and (not lines or lines[-1] != latest_line):
            lines.append(latest_line)

        return "\n".join(lines[-self.TRANSCRIPT_LINES:])

    def _score_open(self, transcript: str, dims: list) -> dict:
        import json
        import openai

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=(self.api_base or self.YANDEX_BASE).rstrip("/"),
            project=self.folder_id if self._is_yandex() else None,
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
                                "missing": {"type": "string"},
                            },
                            "required": ["score", "missing"],
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
            "missing — чего не хватает до 2, не длиннее 10 слов; при 2 — пустая строка."
        )

        model_uri = self._model_uri()  # a configuration error should fail loudly, not count as an outage
        last_err = ""
        for attempt in range(2):
            text = prompt if not attempt else (
                prompt + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАСПАРСЕН (" + last_err
                + "). Верни только валидный JSON."
            )
            try:
                resp = client.responses.create(
                    model=model_uri,
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
                parsed = json.loads(resp.output_text or "")
                if not isinstance(parsed.get("topics"), dict):
                    raise ValueError("no topics in scorer output")
                return parsed
            except Exception as exc:
                last_err = str(exc)[:300]

        return {"_error": last_err or "unknown scoring error"}

    # ------------------------------------------------------------ prompt text

    def _header(self, topics: dict) -> str:
        asked = sum(topics[d]["attempts"] for d in self.DIM_ORDER)
        closed = [self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if topics[d]["score"] == 2]
        return (
            "\nСЛУЖЕБНО: засчитано ответов " + str(asked) + " из "
            + str(self.MAX_QUESTIONS) + ". Уже закрыто: " + (", ".join(closed) or "ничего") + ".\n"
        )

    def _ask_block(self, topics: dict, dim: str, note: str = "") -> str:
        return (
            self._header(topics) + note
            + "СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — " + self.DIM_TITLE_RU[dim] + ".\n"
            "Что нужно вытянуть: " + self.DIG[dim] + ".\n"
            "Задай один вопрос по этой теме."
        )

    def _retry_block(self, topics: dict, dim: str) -> str:
        missing = topics[dim]["missing"]
        return (
            self._header(topics)
            + "СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — " + self.DIM_TITLE_RU[dim] + ".\n"
            "Тема ещё не закрыта" + (". Не хватает: " + missing if missing else "") + ".\n"
            "Задай второй вопрос по ТОЙ ЖЕ теме с другой стороны: про конкретный "
            "случай, про источник знания, про числа. Не повторяй свой прошлый "
            "вопрос дословно."
        )

    # The two end states get a prompt of their own, without the persona's
    # question rules: with those rules in place, a weaker model kept asking
    # questions instead of calling the tools.

    def _final_prompt(self, state: dict) -> str:
        return (
            "Ты — первый фильтр стартап-акселератора. Интервью завершено, "
            "вопросов больше не задавай.\n\n"
            "Сейчас сделай по порядку:\n"
            "1) Вызови инструмент search_market с запросом по идее и аудитории: "
            + (state["idea"] or "идея не названа") + ".\n"
            "2) Вызови инструмент compute_verdict. Аргументы не нужны: оценки и "
            "результат поиска он берёт из состояния сам.\n"
            "3) Передай человеку текст из поля message результата compute_verdict. "
            "Можно сгладить формулировки, но ничего не убирай и не меняй суть: "
            "решение, объяснение, заметка о рынке и домашнее задание остаются. "
            "Баллы не называй.\n\n"
            "Если compute_verdict вернул ошибку, вердикт не выноси: скажи, что "
            "произошёл технический сбой и вердикт посчитать не удалось."
        )

    def _failure_prompt(self) -> str:
        return (
            "Ты — первый фильтр стартап-акселератора. Оценка ответов сейчас "
            "недоступна из-за технического сбоя.\n"
            "Вопросов больше не задавай, вердикт не выноси, инструменты не вызывай.\n"
            "Скажи человеку одной-двумя фразами по-русски, на «ты», что произошёл "
            "технический сбой и интервью нужно продолжить чуть позже."
        )

    def _progress(self, topics: dict) -> tuple:
        """Return (complete, next_topic) for the current scores and attempts."""
        nxt = None
        for d in self.DIM_ORDER:
            if topics[d]["score"] < 2 and topics[d]["attempts"] < self.MAX_ATTEMPTS:
                nxt = d
                break
        total_attempts = sum(topics[d]["attempts"] for d in self.DIM_ORDER)
        complete = (
            all(topics[d]["score"] == 2 for d in self.DIM_ORDER)
            or total_attempts >= self.MAX_QUESTIONS
            or nxt is None
        )
        return complete, nxt

    # ---------------------------------------------------------------- main

    def run_controller(self) -> Message:
        raw = self.answer
        answer = raw.text if hasattr(raw, "text") else str(raw or "")
        answer = (answer or "").strip()

        msgs = self._session_messages()
        has_bot_turn = any(
            m.sender == "Machine" and not str(m.sender_name or "").startswith("__")
            for m in msgs
        )
        state = self._load_state(has_bot_turn)
        topics = state["topics"]
        topic = state["current_topic"] or self.DIM_ORDER[0]
        already_complete, _ = self._progress(topics)

        # Once the interview is over, further messages change nothing: no
        # scoring, no attempts. The agent keeps getting the final instruction.
        if answer and not already_complete:
            # The founder's opening message is the idea itself, not an answer
            # to a question, so it is scored but does not use up an attempt.
            counts_as_answer = has_bot_turn
            open_dims = [d for d in self.DIM_ORDER if topics[d]["score"] < 2]
            fresh = self._score_open(self._transcript(msgs, answer), open_dims) if open_dims else {}

            if fresh.get("_error"):
                # Do not charge the founder for an outage: the answer stays in
                # the transcript and is scored on the next successful turn.
                state["last_score_error"] = fresh["_error"]
                state["score_errors_in_row"] = int(state.get("score_errors_in_row") or 0) + 1
            else:
                state["last_score_error"] = ""
                state["score_errors_in_row"] = 0
                if counts_as_answer:
                    state["questions_asked"] += 1
                    topics[topic]["attempts"] += 1

                fresh_topics = fresh.get("topics") or {}
                for d in open_dims:
                    item = fresh_topics.get(d) or {}
                    new_score = item.get("score")
                    if new_score not in (0, 1, 2):
                        continue
                    new_score = int(new_score)
                    # Monotonic merge: keep the best score, and keep the
                    # "missing" note that belongs to that score.
                    if new_score >= int(topics[d]["score"]):
                        topics[d]["score"] = new_score
                        topics[d]["missing"] = "" if new_score == 2 else str(item.get("missing", ""))

                if fresh.get("idea"):
                    state["idea"] = str(fresh["idea"])
                if fresh.get("entry") in ("nothing", "idea", "built"):
                    state["entry"] = fresh["entry"]
                if "not_tech" in fresh:
                    state["not_tech"] = bool(fresh["not_tech"])
                if fresh.get("not_tech_reason"):
                    state["not_tech_reason"] = str(fresh["not_tech_reason"])

        complete, nxt = self._progress(topics)
        failed = int(state.get("score_errors_in_row") or 0) >= self.MAX_SCORE_ERRORS_IN_ROW

        block = ""
        if failed or complete:
            pass
        elif nxt == topic:
            if topics[topic]["attempts"] > 0:
                block = self._retry_block(topics, topic)
            else:
                block = self._ask_block(topics, topic)
        elif topics[topic]["score"] < 2 and topics[topic]["attempts"] >= self.MAX_ATTEMPTS:
            note = (
                "Тема «" + self.DIM_TITLE_RU[topic] + "» не закрыта за две попытки. "
                "Долбить бессмысленно.\n"
            )
            block = self._ask_block(topics, nxt, note)
        else:
            block = self._ask_block(topics, nxt)

        if not complete:
            state["current_topic"] = nxt

        self._save_state(state)
        self.status = {
            "complete": complete,
            "topic": state["current_topic"],
            "scores": {d: topics[d]["score"] for d in self.DIM_ORDER},
            "last_score_error": state["last_score_error"],
        }
        if failed:
            return Message(text=self._failure_prompt())
        if complete:
            return Message(text=self._final_prompt(state))
        return Message(text=self.STATIC_PERSONA + block)
