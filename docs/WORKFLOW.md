# Langflow interview-bot workflow — state as of 2026-08-03

Porting the Python project at `C:\Users\Home\ai-accel` (`core/*.py`) into a Langflow
flow called **"New Flow"** in the **Starter Project**, running against a local
Langflow instance at `http://localhost:7860` (login user: `langflow`).

Every custom component's docstring says which original function it mirrors —
that's intentional, it's how you check this port against `ai-accel/core/*.py`
if behavior ever looks wrong.

## NEW (2026-08-04): agentic rewrite — flow "Interview Bot (agentic)"

Flow id `45fd9442-d8a7-4cb4-97c4-3922e153e015`, same Starter Project. The original
16-node flow is untouched (now named "New Flow (1)"); this is a separate flow.

Reason for the rewrite: the hand-rolled flow had ~10 "plumbing" nodes that existed
only to shuffle state around (Load/Save/Merge State, Transcript Builder, State
Prompt Builder, Build Focus Prompt, Topic Router, Record Attempt, Build Search
Query). They made the canvas unreadable and left no room to add real agentic
nodes. A Langflow `Agent` gets conversation memory for free (`n_messages`), so
almost all of that plumbing is unnecessary.

**6 nodes, 5 edges:**
```
Chat Input ──────────────► Agent ──► Chat Output
Yandex Chat Model ───────►  │  (model)
Verdict R1-R5 (tool) ────►  │  (tools)
Yandex Web Search (tool) ─►  │  (tools)
```

- **Yandex Chat Model** (custom) — returns a LangChain `ChatOpenAI` pointed at
  Yandex's OpenAI-compatible endpoint, typed as `LanguageModel` so the Agent can
  consume it. Verified: Yandex **does** support OpenAI-style tool calling, both
  raw and via `ChatOpenAI.bind_tools()` — that's what makes this design possible.
  Langflow's native LanguageModel component has no generic base-URL override
  (only watsonx/ollama), which is why this one custom component is unavoidable.
- **Agent** (native) — the whole interviewer lives in its Agent Instructions:
  persona, the four topics, the scoring rubric, the NEVER_SUGGEST prohibitions,
  the one-question-per-reply and 2-attempts-per-topic/10-questions budget rules,
  and the end-of-interview orchestration. `n_messages=100` gives it the transcript.
- **Verdict R1-R5** (custom, Tool Mode) — kept as deterministic code on purpose.
  The R1–R5 rules are the actual pass/fail decision and must stay auditable and
  reproducible (see the golden-dataset/evals tasks in Asana). The agent supplies
  the scores; the rules themselves are not up to the model. Exposed as tool
  `compute_verdict`.
- **Yandex Web Search** (custom, Tool Mode) — exposed as tool `search_market`.
  Gating is now free: the agent simply calls it when the interview is over, so
  the old "run this node only at stage=market" problem disappears.

Dropped deliberately: Reply Filter. Its job (one question, no invented dialogue,
no placeholders) is now prompt-level. Keeping it would have corrupted the final
verdict message, since it truncates everything after the first "?".

### Verified working (2026-08-04)
Ran the graph server-side against the real DB payload: Agent replied
`Кто именно ищет нянь?` — one question, correct first topic, no preamble.

### RESOLVED (2026-08-04): it was the model, not the prompt — use `deepseek-v4-flash`

The 2-attempts rule failure below turned out to be a model capability problem.
Benchmarked every chat model the account exposes on the exact failing turn
(system prompt + «строительные компании и их поставщики» → does it ask a SECOND
Аудитория question or skip to Поведение?), and on tool-calling support:

| model | tools | stayed on topic | NEVER_SUGGEST |
|---|---|---|---|
| `yandexgpt/latest` (was in use) | no* | NO — skipped | — |
| `yandexgpt-5.1` | yes | yes | violates 5а (suggests «по масштабу проектов») |
| `yandexgpt-5-pro` | **no** | yes | ok — but unusable, no tool calling |
| **`deepseek-v4-flash`** | yes | yes | clean — asks source-of-knowledge exactly per 5а |
| `qwen3-235b-a22b-fp8` | yes | yes | mild 5а («в каком регионе») |
| `qwen3.6-35b-a3b` | yes | yes | clean |
| `gpt-oss-120b` | yes | yes | violates rules 2/3 (lists examples) |

\* `yandexgpt/latest` returned tool_calls in an isolated test but not in this one —
flaky tool support, a second reason to avoid it.

Switched `Yandex Chat Model.model` to `deepseek-v4-flash/latest`. Confirmed in the
Playground: on the same weak answer it now replies «Откуда ты про это знаешь — сам
работал в стройке или уже общался с кем-то из этих компаний?» — second attempt on
the same topic, no invented признак, no example list.

Fallback if deepseek is unavailable: `qwen3.6-35b-a3b`. Do NOT use `yandexgpt-5-pro`
(no tool calling) or `gpt-oss-120b` (hands the applicant example answers).

#### Full end-to-end run PASSED on deepseek-v4-flash (2026-08-04)
Six-turn interview, "ИИ-платформа для подбора бетонных плит":
segment needed 2 attempts and closed; behavior/alternatives/test each closed in 1.
Then BOTH tools fired — verified in the message `content_blocks`, not just on screen:
- `search_market` → real Yandex results with live URLs (calculator.fixplans.ru …), 5 competitors
- `compute_verdict` → `{"outcome":"pass","rule":"R5",...}`

The agent relayed the R5 explanation and HOMEWORK_BEGINNER verbatim from the
component's output rather than inventing them — the code/model split holds.

Remaining drift on deepseek: on the Альтернативы question it asked «Может,
какие-то программы, каталоги, другие сервисы?», which offers example answers
(prohibitions 2/3). Milder than yandexgpt's failures but same class. Cheap fix if
it matters: add "не перечисляй категории решений внутри самого вопроса" to rule 2.

Caveat: prompt-level enforcement is still not a guarantee — it now holds because
the model is strong enough, not because the rule is enforced. If it drifts again,
option B below (controller as a tool) is still the durable fix.

### Original diagnosis: the 2-attempts-per-topic rule does NOT hold in the prompt
Observed 2026-08-04. Given a deliberately weak segment answer («строительные
компании и их поставщики» — a whole category, a clear 1 on the rubric), the agent
scores it as good enough and moves straight to the next topic. A full interview
runs 4 questions instead of 6–8, and the verdict then dings the applicant for the
very vagueness it never probed.

Two escalating prompt revisions were tried and BOTH failed (verified: the new
prompt was confirmed present in the DB during the test run, so this is not a
stale-prompt artifact):
1. «На одну тему максимум 2 вопроса» → reads as a ceiling, one question satisfies it.
2. An explicit "ПРАВИЛО ДВУХ ПОПЫТОК" block spelling out the count, plus examples
   of answers that never score 2 → still skipped the second question.

Conclusion: counting/state rules ("how many questions have I asked about topic X")
are exactly what an LLM is unreliable at and what the original Python enforced in
code via `session.attempts[dim] += 1` + `_current_dim()`. This is the real cost of
the agentic rewrite, and it is not fixable by wording alone on yandexgpt.

Options, none applied yet:
- **A. Accept it.** The agent judges when a topic is done. Simplest, but the
  2-attempt guarantee and the question budget are gone.
- **B. Put the controller back in code as a tool.** One extra node exposing e.g.
  `next_step(topic, score)` that owns the attempts counter and returns a literal
  instruction ("ask a second question about segment" / "move on to behavior").
  Counting becomes deterministic again; still depends on the agent calling the
  tool every turn, but tool-calling compliance has been reliable here where
  counting has not. Needs somewhere to persist attempts per session.
- **C. Stronger interviewer model.** yandexgpt is weak at multi-constraint
  instruction following; a stronger model may hold the rule. Constrained by
  whatever made Yandex the choice in the first place.

### Build notes (things that silently break)
- Flows created through `POST /api/v1/flows/` have **`SecretStrInput` values
  stripped** — everything else (folder_id, system_prompt, n_messages) persists.
  The Yandex API key must be typed into the two nodes by hand in the UI.
- An edge's `targetHandle.type` **must equal the target template field's `type`**
  (`str` for `input_value`, `model` for the Agent's model, `other` for `tools`).
  Get it wrong and the frontend silently drops that edge on the next autosave —
  no error, the flow just does nothing. This is what ate two edges on first
  creation, and the same class of bug as the earlier import losing an edge.
- Tool-mode nodes are built by POSTing to `/api/v1/custom_component/update` with
  `field: "tool_mode", field_value: true`; that returns the correct
  `component_as_tool` output plus generated `tools_metadata`. Don't hand-write it.

## Why this file exists

The Claude Code app crashed mid-session on 2026-08-03 and asked for a reinstall.
This file is a crash-resistant snapshot of the flow so the next session doesn't
have to re-mine the old chat transcript. Update it whenever the flow structure
changes.

## Current node graph — FULLY WIRED as of 2026-08-03 (confirmed via flow JSON export)

```
Chat Input ──► Yandex Ask ──► Reply Filter ──► Chat Output
                  ▲
                  └── instructions ← Build Focus Prompt.focus_prompt

Load State ──┐
             ├─► Merge State ──► Build Focus Prompt (state) ──┐
Yandex Grade ┘        │                                        ├─► Yandex Ask.instructions
                       ├─► Save State (state)                  │
                       ├─► Topic Router (state) ──► Build Focus Prompt (route)
                       ├─► Verdict R1-R5 (state)
                       └─► Build Search Query (state) ──► Yandex Web Search (input_value)

Transcript Builder ──► State Prompt Builder ──► Yandex Grade (input_value)
```

Every one of the 15 edges was verified by exporting the flow to JSON
(`New Flow.json`) and diffing node IDs against the edges array — not just by
eyeballing the canvas. All match the intended design.

### Confirmed working in Playground
Load State → Merge State → Topic Router → Build Focus Prompt — verified producing
correct output: `Задано реплик: 1 из 10`, `Уже закрыто: Аудитория`,
`СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — Поведение` with the right `dig` text.

### Components rewritten to take Merge State's Data directly (2026-08-03)
`Save State`, `Topic Router`, and `Verdict R1-R5` originally took separate
`MultilineInput`/`StrInput`/`BoolInput` fields (scores_json, entry, not_tech, etc.)
— but those field types only accept `Message` connections (or, for Str/Bool/Int,
no connection at all), while Merge State only outputs one combined `Data` blob.
Confirmed against the actual Langflow source in the container:
```
MultilineInput -> input_types = ['Message']
StrInput / BoolInput / IntInput -> input_types = None (no connector, ever)
```
So all three were rewritten to take a single `DataInput(name="state", ...)` instead
(the same pattern Build Focus Prompt already used successfully), with the
sub-fields extracted inside `run_*()` via `state.get(...)`. Full current code for
all three is below, superseding the versions further down this doc that still show
the old separate-field style — those are being left as-is (see historical section)
but the versions immediately below are what's actually running.

Also added a new component, **Build Search Query**, to bridge Merge State → Yandex
Web Search (which needs actual query *text*, a `Message`, not `Data`). It formats
`idea_summary` + the `segment` reason from merged state into the original
`SEARCH_PROMPT` shape from `ai-accel/core/prompts.py`.

### Known remaining gap — not yet solved
Yandex Web Search now runs on **every** turn once wired, not just when Topic
Router's route says `stage: "market"` — Langflow has no built-in way to gate node
execution on a field value without an explicit conditional/router component. That
component doesn't exist yet. This is the next real piece of work, not a wiring task.

## Playground test — PASSED (2026-08-03 19:07 UTC)

Sent `Привет! Хочу сделать сервис для поиска нянь.` in Default Session. Got back
`Ты сам сталкивался с поиском няни или общался с кем-то, кто искал?` with zero
errors in the message log (verified via `/api/v1/monitor/messages` — only the
user message + AI reply, no `category: "error"` entries). Confirms the full loop
works end-to-end: Transcript Builder → State Prompt Builder → Yandex Grade
(READER) → Merge State → Topic Router → Build Focus Prompt → Yandex Ask → Reply
Filter → Chat Output.

### Bug found and fixed: `self.session_id` AttributeError
Load State, Save State, and Transcript Builder all used `self.session_id_override
or self.session_id`. Langflow only populates the bare `self.session_id` attribute
if the component's own template declares a field literally named `session_id`
(checked in `lfx/graph/vertex/base.py`: `self.has_session_id = "session_id" in
template_dicts`). Ours are named `session_id_override`, so `self.session_id` was
never set — worked fine whenever the override field had a manually-typed test
value (short-circuited before touching the broken attribute), but crashed with
`AttributeError: Attribute session_id not found in ...Component` the moment the
override was left blank (the real production case). Fixed by replacing
`self.session_id` with `self.graph.session_id` in all three (confirmed safe:
`self.graph` always resolves, and `graph.session_id` is a real property in
`lfx/graph/graph/base.py`). All three components' code above already reflects
this fix.

## Next steps

1. **Gate the market-search branch** so Yandex Web Search / Verdict only actually
   run once Topic Router reports `stage: "market"` (needs a conditional/router
   component — Langflow's built-in "If-Else" component may work, or a small custom
   one that mirrors `core/interview.py:handle()`'s branch logic).
2. Test the full loop in the Playground end-to-end with a real conversation and
   confirm state actually persists turn-to-turn via Save State → Load State.
3. Cosmetic: Yandex Ask's `instructions` field still shows a stale leftover value
   (`"Answer in one short sentence in English."`) in the exported JSON even though
   it's correctly wired from Build Focus Prompt (the edge takes priority at
   runtime) — harmless, but can be cleared for cleanliness.

## Rewritten components (current, superseding the old versions further down)

### Save State (current)
```python
from langflow.custom import Component
from langflow.io import DataInput, StrInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class SaveStateComponent(Component):
    display_name = "Save State"
    description = "Writes session state as a hidden __state__ message so the next turn can load it."
    icon = "upload"
    name = "SaveState"

    inputs = [
        DataInput(name="state", display_name="Merged State (from Merge State)", required=True),
        StrInput(name="session_id_override", display_name="Session ID Override (blank = use graph session)", value=""),
    ]

    outputs = [
        Output(display_name="Saved", name="saved", method="run_save"),
    ]

    def run_save(self) -> Data:
        import json

        from langflow.memory import store_message

        sid = self.session_id_override or self.session_id
        payload = self.state.data if isinstance(self.state, Data) else (self.state or {})

        msg = Message(
            text=json.dumps(payload, ensure_ascii=False),
            sender="Machine",
            sender_name="__state__",
            session_id=sid,
        )
        try:
            store_message(msg, flow_id=self.graph.flow_id)
        except Exception as exc:
            self.status = f"save error: {exc}"
            return Data(data={"_error": str(exc)})

        self.status = "saved"
        return Data(data=payload)
```

### Topic Router (current)
```python
from langflow.custom import Component
from langflow.io import DataInput, IntInput, Output
from langflow.schema import Data


class TopicRouterComponent(Component):
    display_name = "Topic Router"
    description = "Picks the next open topic to ask about, or routes to market search (mirrors core/interview.py:_current_dim()+handle())."
    icon = "split"
    name = "TopicRouter"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]

    inputs = [
        DataInput(name="state", display_name="Merged State (from Merge State)", required=True),
        IntInput(name="max_questions", display_name="Max Questions", value=10),
        IntInput(name="max_attempts_per_topic", display_name="Max Attempts Per Topic", value=2),
    ]

    outputs = [
        Output(display_name="Route", name="route", method="run_route"),
    ]

    def run_route(self) -> Data:
        state = self.state.data if isinstance(self.state, Data) else (self.state or {})
        scores = state.get("scores", {}) or {}
        attempts = state.get("attempts", {}) or {}
        questions_asked = state.get("questions_asked", 0)

        next_dim = None
        for dim in self.DIM_ORDER:
            if int(scores.get(dim, 0)) == 2:
                continue
            if int(attempts.get(dim, 0)) >= self.max_attempts_per_topic:
                continue
            next_dim = dim
            break

        if next_dim is None or questions_asked >= self.max_questions:
            result = {"next_dim": None, "stage": "market"}
        else:
            result = {"next_dim": next_dim, "stage": "talk"}

        self.status = result
        return Data(data=result)
```

### Verdict R1-R5 (current)
```python
from langflow.custom import Component
from langflow.io import DataInput, IntInput, BoolInput, Output
from langflow.schema import Data


class VerdictComponent(Component):
    display_name = "Verdict R1-R5"
    description = "Deterministic pass/improve/not-tech decision from the 4 topic scores (mirrors core/verdict.py:decide())."
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
        DataInput(name="state", display_name="Merged State (from Merge State)", required=True),
        IntInput(name="market_count", display_name="Market: Competitor Count (-1 = not run)", value=-1),
        BoolInput(name="market_ok", display_name="Market Search Succeeded?", value=True),
    ]

    outputs = [
        Output(display_name="Verdict", name="verdict", method="run_verdict"),
    ]

    def _plural(self, n, one, few, many):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return few
        return many

    def _market_note(self):
        if self.market_count < 0 or not self.market_ok:
            return "Проверка рынка не выполнена — оцени конкурентов вручную."
        n = self.market_count
        if n == 0:
            return "Поиск не нашёл ни одного работающего аналога. Обычно это значит одно из двух: либо рынка нет, либо запрос был сформулирован неудачно. Проверь руками, прежде чем радоваться."
        word = self._plural(n, "работающий аналог", "работающих аналога", "работающих аналогов")
        if n >= self.CROWDED_MARKET:
            return f"Найдено {n} {word} — ниша плотная. Само по себе это не стоп, но нужен внятный ответ, почему клиент уйдёт от них к тебе."
        return f"Найдено {n} {word} — это нормальный признак живого рынка."

    def run_verdict(self) -> Data:
        state = self.state.data if isinstance(self.state, Data) else (self.state or {})
        scores = state.get("scores", {}) or {}
        entry = state.get("entry") or "idea"
        not_tech = bool(state.get("not_tech", False))
        not_tech_reason = state.get("not_tech_reason", "")

        filled = {d: int(scores.get(d, 0)) for d in self.DIM_ORDER}
        total = sum(filled.values())
        weak = [d for d in self.DIM_ORDER if filled[d] != 2]
        note = self._market_note()

        if not_tech:
            result = {
                "outcome": "not_tech", "rule": "R1",
                "explanation": "Задача акселератора — технологические компании, которые могут расти без пропорционального роста затрат. " + (not_tech_reason or "Здесь этого признака нет."),
                "weak_dims": weak, "homework": [], "market_note": note, "total": total,
            }
        elif any(filled[d] == 0 for d in self.DIM_ORDER):
            empty = [d for d in self.DIM_ORDER if filled[d] == 0]
            names = ", ".join(self.DIM_TITLE_RU[d] for d in empty)
            result = {
                "outcome": "improve", "rule": "R2",
                "explanation": f"Нет ответа по темам: {names}. Без них оценивать нечего.",
                "weak_dims": weak, "homework": [self.DIM_HOMEWORK[d] for d in weak], "market_note": note, "total": total,
            }
        elif any(filled[d] == 1 for d in self.DIM_ORDER):
            vague = [d for d in self.DIM_ORDER if filled[d] == 1]
            names = ", ".join(self.DIM_TITLE_RU[d] for d in vague)
            result = {
                "outcome": "improve", "rule": "R3",
                "explanation": f"Ответы по темам «{names}» остались общими. Проходит только конкретика: живые люди, наблюдаемые действия, проверяемые сроки.",
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
```

### Build Search Query (new)
```python
from langflow.custom import Component
from langflow.io import DataInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class BuildSearchQueryComponent(Component):
    display_name = "Build Search Query"
    description = "Turns merged state into the market-search query (mirrors core/prompts.py:SEARCH_PROMPT)."
    icon = "search"
    name = "BuildSearchQuery"

    SEARCH_PROMPT = """
Найди работающие продукты и сервисы, которые решают эту задачу:
{idea}
Аудитория: {segment}

Ищи короткими запросами из 3–7 слов: категория продукта плюс слово
«сервис», «платформа» или «приложение». Длинные запросы возвращают статьи,
а не продукты.

Перечисли найденное со ссылками. Статьи, обзоры и подборки «10 лучших»
не нужны — нужны сами продукты.
"""

    inputs = [
        DataInput(name="state", display_name="Merged State (from Merge State)", required=True),
    ]

    outputs = [
        Output(display_name="Search Query", name="query", method="run_build"),
    ]

    def run_build(self) -> Message:
        state = self.state.data if isinstance(self.state, Data) else (self.state or {})
        idea = state.get("idea_summary", "") or "(идея не названа)"
        segment = (state.get("reasons", {}) or {}).get("segment", "") or "(аудитория не названа)"

        result = self.SEARCH_PROMPT.format(idea=idea, segment=segment)
        self.status = result
        return Message(text=result)
```

Yandex Web Search's `System Instructions` field is set manually (typed, no wiring
needed — it's a static prompt) to:
```
Ты ищешь в вебе работающие продукты и сервисы. Всегда приводи ссылки на найденное. Не выдумывай названий и адресов: только то, что реально нашёл.
```

## Historical next-steps section (superseded — kept for context only)

The section below was written mid-session, before Save State/Topic Router/Verdict
were rewritten to take Merge State's Data directly. It's kept only so the history
of *why* the rewrite happened is traceable; don't follow it — follow "Next steps"
above instead.

1. ~~Turn Yandex Grade into the real READER~~ — done.
2. ~~Wire Merge State → Save State~~ — done (via rewrite to DataInput).
3. ~~Wire Topic Router from Merge State~~ — done (via rewrite to DataInput).
4. ~~Wire Reply Filter between Yandex Ask and Chat Output~~ — done.
5. Wire the market-search branch — partially done (Build Search Query → Yandex Web
   Search built and wired), but execution isn't gated on `stage: "market"` yet.
6. ~~Wire Verdict R1-R5 from Merge State~~ — done (via rewrite to DataInput).

## Source-of-truth prompts/schema (from `ai-accel/core/prompts.py`)

```python
READER_SYSTEM = """
Ты читаешь расшифровку интервью в стартап-акселераторе и возвращаешь состояние
строгим JSON. С пользователем ты не разговариваешь, вопросов не задаёшь.

Оценивай строго по рубрике. «Звучит разумно» — это не 2. Двойка ставится,
только если сказанное можно проверить. Если ответ можно без изменений
приклеить к любой другой идее — это максимум 1.

Засчитывай сказанное в ЛЮБОМ месте разговора, даже если оно прозвучало в
ответе на другой вопрос. Человек не обязан повторяться.
{NEVER_SUGGEST — see full text in Build Focus Prompt component's NEVER_SUGGEST class attr}
"""
```

`STATE_SCHEMA` as JSON text (paste literally into Yandex Grade's "JSON Schema (as text)"):

```json
{
  "type": "object",
  "properties": {
    "idea": {"type": "string"},
    "entry": {"type": "string", "enum": ["nothing", "idea", "built"]},
    "not_tech": {"type": "boolean"},
    "not_tech_reason": {"type": "string"},
    "topics": {
      "type": "object",
      "properties": {
        "segment": {
          "type": "object",
          "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "reason": {"type": "string"},
            "missing": {"type": "string"}
          },
          "required": ["score", "reason", "missing"],
          "additionalProperties": false
        },
        "behavior": {
          "type": "object",
          "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "reason": {"type": "string"},
            "missing": {"type": "string"}
          },
          "required": ["score", "reason", "missing"],
          "additionalProperties": false
        },
        "alternatives": {
          "type": "object",
          "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "reason": {"type": "string"},
            "missing": {"type": "string"}
          },
          "required": ["score", "reason", "missing"],
          "additionalProperties": false
        },
        "test": {
          "type": "object",
          "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "reason": {"type": "string"},
            "missing": {"type": "string"}
          },
          "required": ["score", "reason", "missing"],
          "additionalProperties": false
        }
      },
      "required": ["segment", "behavior", "alternatives", "test"],
      "additionalProperties": false
    }
  },
  "required": ["idea", "entry", "not_tech", "not_tech_reason", "topics"],
  "additionalProperties": false
}
```

Confirmed against `ai-accel/core/llm.py:grade()` — the schema is passed to Yandex's
Responses API raw (no extra wrapper), matching how Yandex Grade's field is already
used for the placeholder sentiment schema.

## Full code for every custom component

### Yandex Ask
```python
from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MultilineInput, MessageInput, SliderInput, Output
from langflow.schema.message import Message


class YandexAskComponent(Component):
    display_name = "Yandex Ask"
    description = "One conversational turn via Yandex AI Studio Responses API (mirrors core/llm.py:ask())."
    icon = "message-circle"
    name = "YandexAsk"

    inputs = [
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        MultilineInput(name="instructions", display_name="System Instructions", required=True),
        MessageInput(name="input_value", display_name="Input", required=True),
        SliderInput(name="temperature", display_name="Temperature", value=0.4, range_spec={"min": 0.0, "max": 1.0, "step": 0.01}),
    ]

    outputs = [
        Output(display_name="Reply", name="reply", method="run_ask"),
    ]

    def _model_uri(self) -> str:
        model = self.model
        if model.startswith(("gpt://", "ds://")):
            return model
        if "/" not in model:
            model = f"{model}/latest"
        return f"gpt://{self.folder_id}/{model}"

    def run_ask(self) -> Message:
        import openai

        client = openai.OpenAI(api_key=self.api_key, base_url="https://ai.api.cloud.yandex.net/v1", project=self.folder_id, timeout=90)
        input_text = self.input_value.text if isinstance(self.input_value, Message) else str(self.input_value)
        try:
            resp = client.responses.create(model=self._model_uri(), instructions=self.instructions, input=input_text, temperature=self.temperature)
        except Exception as exc:
            self.status = f"Yandex error: {exc}"
            return Message(text="")
        text = (resp.output_text or "").strip()
        self.status = text
        return Message(text=text)
```

### Yandex Grade
```python
from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MultilineInput, MessageInput, IntInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class YandexGradeComponent(Component):
    display_name = "Yandex Grade"
    description = "Strict JSON-schema scoring call via Yandex AI Studio Responses API (mirrors core/llm.py:grade())."
    icon = "list-checks"
    name = "YandexGrade"

    inputs = [
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        MultilineInput(name="instructions", display_name="System Instructions", required=True),
        MessageInput(name="input_value", display_name="Input", required=True),
        MultilineInput(name="schema_json", display_name="JSON Schema (as text)", required=True),
        IntInput(name="retries", display_name="Retries", value=1),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_grade"),
    ]

    def _model_uri(self) -> str:
        model = self.model
        if model.startswith(("gpt://", "ds://")):
            return model
        if "/" not in model:
            model = f"{model}/latest"
        return f"gpt://{self.folder_id}/{model}"

    @staticmethod
    def _extract_json(raw: str) -> dict:
        import json
        import re

        if not raw or not raw.strip():
            raise ValueError("empty response")
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fenced:
            text = fenced.group(1).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("no JSON object in response") from None
            parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("expected object, got " + type(parsed).__name__)
        return parsed

    def run_grade(self) -> Data:
        import json

        import openai

        try:
            schema = json.loads(self.schema_json)
        except json.JSONDecodeError as exc:
            self.status = f"Bad schema JSON: {exc}"
            return Data(data={"_error": str(exc)})

        client = openai.OpenAI(api_key=self.api_key, base_url="https://ai.api.cloud.yandex.net/v1", project=self.folder_id, timeout=90)
        input_text = self.input_value.text if isinstance(self.input_value, Message) else str(self.input_value)

        last_err = ""
        for attempt in range(self.retries + 1):
            text = input_text if not attempt else f"{input_text}\n\nPREVIOUS ANSWER DID NOT PARSE ({last_err}). Return only valid JSON per the schema."
            try:
                resp = client.responses.create(
                    model=self._model_uri(),
                    instructions=self.instructions,
                    input=text,
                    temperature=0.0,
                    text={"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}},
                )
            except Exception as exc:
                self.status = f"Yandex error: {exc}"
                return Data(data={"_error": str(exc)})
            try:
                parsed = self._extract_json(resp.output_text or "")
                self.status = parsed
                return Data(data=parsed)
            except ValueError as exc:
                last_err = str(exc)

        self.status = f"Model never returned valid JSON: {last_err}"
        return Data(data={"_error": last_err})
```

### Yandex Web Search
```python
from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MultilineInput, MessageInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class YandexWebSearchComponent(Component):
    display_name = "Yandex Web Search"
    description = "Web search via Yandex AI Studio's built-in tool (mirrors core/llm.py:web_search())."
    icon = "search"
    name = "YandexWebSearch"

    inputs = [
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        MultilineInput(name="instructions", display_name="System Instructions", required=True),
        MessageInput(name="input_value", display_name="Input", required=True),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_search"),
    ]

    def _model_uri(self) -> str:
        model = self.model
        if model.startswith(("gpt://", "ds://")):
            return model
        if "/" not in model:
            model = f"{model}/latest"
        return f"gpt://{self.folder_id}/{model}"

    def run_search(self) -> Data:
        import openai

        client = openai.OpenAI(api_key=self.api_key, base_url="https://ai.api.cloud.yandex.net/v1", project=self.folder_id, timeout=90)
        input_text = self.input_value.text if isinstance(self.input_value, Message) else str(self.input_value)

        try:
            resp = client.responses.create(
                model=self._model_uri(),
                instructions=self.instructions,
                input=input_text,
                tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "medium"}],
            )
        except Exception as exc:
            self.status = f"Yandex error: {exc}"
            return Data(data={"_error": str(exc), "text": "", "urls": []})

        urls = []
        for item in getattr(resp, "output", None) or []:
            for chunk in getattr(item, "content", None) or []:
                for ann in getattr(chunk, "annotations", None) or []:
                    url = getattr(ann, "url", None)
                    if url and url not in urls:
                        urls.append(url)

        text = (resp.output_text or "").strip()
        self.status = f"{len(urls)} urls found"
        return Data(data={"text": text, "urls": urls})
```

### Topic Router
```python
from langflow.custom import Component
from langflow.io import MultilineInput, IntInput, Output
from langflow.schema import Data


class TopicRouterComponent(Component):
    display_name = "Topic Router"
    description = "Picks the next open topic to ask about, or routes to market search (mirrors core/interview.py:_current_dim()+handle())."
    icon = "split"
    name = "TopicRouter"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]

    inputs = [
        MultilineInput(name="scores_json", display_name="Scores (JSON: {dim: 0|1|2})", required=True, value="{}"),
        MultilineInput(name="attempts_json", display_name="Attempts (JSON: {dim: count})", required=True, value="{}"),
        IntInput(name="questions_asked", display_name="Questions Asked So Far", value=0),
        IntInput(name="max_questions", display_name="Max Questions", value=10),
        IntInput(name="max_attempts_per_topic", display_name="Max Attempts Per Topic", value=2),
    ]

    outputs = [
        Output(display_name="Route", name="route", method="run_route"),
    ]

    def run_route(self) -> Data:
        import json

        try:
            scores = json.loads(self.scores_json or "{}")
        except json.JSONDecodeError:
            scores = {}
        try:
            attempts = json.loads(self.attempts_json or "{}")
        except json.JSONDecodeError:
            attempts = {}

        next_dim = None
        for dim in self.DIM_ORDER:
            if int(scores.get(dim, 0)) == 2:
                continue
            if int(attempts.get(dim, 0)) >= self.max_attempts_per_topic:
                continue
            next_dim = dim
            break

        if next_dim is None or self.questions_asked >= self.max_questions:
            result = {"next_dim": None, "stage": "market"}
        else:
            result = {"next_dim": next_dim, "stage": "talk"}

        self.status = result
        return Data(data=result)
```

### Verdict R1-R5
```python
from langflow.custom import Component
from langflow.io import MultilineInput, StrInput, BoolInput, IntInput, Output
from langflow.schema import Data


class VerdictComponent(Component):
    display_name = "Verdict R1-R5"
    description = "Deterministic pass/improve/not-tech decision from the 4 topic scores (mirrors core/verdict.py:decide())."
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

    HOMEWORK_NO_IDEA = [
        "Выбери среду, где ты бываешь каждую неделю: работа, учёба, секция, подработка, любое место, куда у тебя есть свой доступ.",
        "Найди там пять человек и поговори с каждым по 20–30 минут. Спрашивай только про прошлое: что они делали в последний раз, когда что-то шло долго, дорого или бесило.",
        "Ничего не предлагай и не придумывай за них решений. Как только предложишь, человек начнёт вежливо соглашаться, и разговор можно выбрасывать.",
        "Запиши дословные цитаты, а не пересказ. Возвращайся с ними — идея вырастет из них, а не из размышлений за столом.",
    ]
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
        MultilineInput(name="scores_json", display_name="Scores (JSON: {dim: 0|1|2})", required=True, value="{}"),
        StrInput(name="entry", display_name="Entry (nothing|idea|built)", value="idea"),
        BoolInput(name="not_tech", display_name="Not Tech?", value=False),
        StrInput(name="not_tech_reason", display_name="Not Tech Reason", value=""),
        IntInput(name="market_count", display_name="Market: Competitor Count (-1 = not run)", value=-1),
        BoolInput(name="market_ok", display_name="Market Search Succeeded?", value=True),
    ]

    outputs = [
        Output(display_name="Verdict", name="verdict", method="run_verdict"),
    ]

    def _plural(self, n, one, few, many):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return few
        return many

    def _market_note(self):
        if self.market_count < 0 or not self.market_ok:
            return "Проверка рынка не выполнена — оцени конкурентов вручную."
        n = self.market_count
        if n == 0:
            return "Поиск не нашёл ни одного работающего аналога. Обычно это значит одно из двух: либо рынка нет, либо запрос был сформулирован неудачно. Проверь руками, прежде чем радоваться."
        word = self._plural(n, "работающий аналог", "работающих аналога", "работающих аналогов")
        if n >= self.CROWDED_MARKET:
            return f"Найдено {n} {word} — ниша плотная. Само по себе это не стоп, но нужен внятный ответ, почему клиент уйдёт от них к тебе."
        return f"Найдено {n} {word} — это нормальный признак живого рынка."

    def run_verdict(self) -> Data:
        import json

        try:
            scores = json.loads(self.scores_json or "{}")
        except json.JSONDecodeError:
            scores = {}

        filled = {d: int(scores.get(d, 0)) for d in self.DIM_ORDER}
        total = sum(filled.values())
        weak = [d for d in self.DIM_ORDER if filled[d] != 2]
        note = self._market_note()

        if self.not_tech:
            result = {
                "outcome": "not_tech", "rule": "R1",
                "explanation": "Задача акселератора — технологические компании, которые могут расти без пропорционального роста затрат. " + (self.not_tech_reason or "Здесь этого признака нет."),
                "weak_dims": weak, "homework": [], "market_note": note, "total": total,
            }
        elif any(filled[d] == 0 for d in self.DIM_ORDER):
            empty = [d for d in self.DIM_ORDER if filled[d] == 0]
            names = ", ".join(self.DIM_TITLE_RU[d] for d in empty)
            result = {
                "outcome": "improve", "rule": "R2",
                "explanation": f"Нет ответа по темам: {names}. Без них оценивать нечего.",
                "weak_dims": weak, "homework": [self.DIM_HOMEWORK[d] for d in weak], "market_note": note, "total": total,
            }
        elif any(filled[d] == 1 for d in self.DIM_ORDER):
            vague = [d for d in self.DIM_ORDER if filled[d] == 1]
            names = ", ".join(self.DIM_TITLE_RU[d] for d in vague)
            result = {
                "outcome": "improve", "rule": "R3",
                "explanation": f"Ответы по темам «{names}» остались общими. Проходит только конкретика: живые люди, наблюдаемые действия, проверяемые сроки.",
                "weak_dims": weak, "homework": [self.DIM_HOMEWORK[d] for d in weak], "market_note": note, "total": total,
            }
        elif self.entry == "nothing":
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
```

### Reply Filter
```python
from langflow.custom import Component
from langflow.io import MessageInput, StrInput, Output
from langflow.schema.message import Message


class ReplyFilterComponent(Component):
    display_name = "Reply Filter"
    description = "Strips invented dialogue, cuts after the first '?', rejects placeholders (mirrors core/interview.py:_clean()+_last_resort())."
    icon = "scissors"
    name = "ReplyFilter"

    inputs = [
        MessageInput(name="raw_text", display_name="Raw Model Reply", required=True),
        StrInput(name="fallback_text", display_name="Fallback Question (used if cleaning empties the reply)", required=True),
    ]

    outputs = [
        Output(display_name="Clean Reply", name="clean_reply", method="run_clean"),
    ]

    def run_clean(self) -> Message:
        import re

        text = self.raw_text.text if isinstance(self.raw_text, Message) else str(self.raw_text)

        fake_turn = re.compile(r"\b(Пользовател[ья]|Человек|Ассистент|Ответ|Бот)\s*:", re.I)
        placeholder = re.compile(r"\[[^\]]{2,}\]|\{[^}]{2,}\}|<[^>]{2,}>")

        cleaned = " ".join((text or "").replace("**", "").split())
        for prefix in ("Вопрос:", "Реплика:", "Бот:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()

        fake = fake_turn.search(cleaned)
        if fake:
            cleaned = cleaned[: fake.start()].strip()

        mark = cleaned.find("?")
        if mark != -1:
            cleaned = cleaned[: mark + 1].strip()

        if placeholder.search(cleaned):
            cleaned = ""

        cleaned = cleaned[:500]
        result = cleaned or self.fallback_text

        self.status = result
        return Message(text=result)
```

### Load State
```python
from langflow.custom import Component
from langflow.io import StrInput, Output
from langflow.schema import Data


class LoadStateComponent(Component):
    display_name = "Load State"
    description = "Reads the latest hidden __state__ message for this session (JSON session state)."
    icon = "download"
    name = "LoadState"

    DEFAULT_STATE = {
        "scores": {}, "attempts": {}, "questions_asked": 0,
        "entry": None, "not_tech": False, "not_tech_reason": "",
        "idea_summary": "",
    }

    inputs = [
        StrInput(name="session_id_override", display_name="Session ID Override (blank = use graph session)", value=""),
    ]

    outputs = [
        Output(display_name="State", name="state", method="run_load"),
    ]

    def run_load(self) -> Data:
        import json

        from langflow.memory import get_messages

        sid = self.session_id_override or self.session_id
        try:
            msgs = get_messages(
                session_id=sid,
                sender_name="__state__",
                order_by="timestamp",
                order="DESC",
                limit=1,
                flow_id=self.graph.flow_id,
            )
        except Exception as exc:
            self.status = f"load error: {exc}"
            return Data(data=dict(self.DEFAULT_STATE))

        if not msgs:
            self.status = "no prior state — fresh session"
            return Data(data=dict(self.DEFAULT_STATE))

        try:
            state = json.loads(msgs[0].text)
        except (json.JSONDecodeError, AttributeError):
            state = dict(self.DEFAULT_STATE)

        self.status = state
        return Data(data=state)
```

### Save State
```python
from langflow.custom import Component
from langflow.io import MultilineInput, StrInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class SaveStateComponent(Component):
    display_name = "Save State"
    description = "Writes session state as a hidden __state__ message so the next turn can load it."
    icon = "upload"
    name = "SaveState"

    inputs = [
        MultilineInput(name="state_json", display_name="State (JSON)", required=True),
        StrInput(name="session_id_override", display_name="Session ID Override (blank = use graph session)", value=""),
    ]

    outputs = [
        Output(display_name="Saved", name="saved", method="run_save"),
    ]

    def run_save(self) -> Data:
        import json

        from langflow.memory import store_message

        sid = self.session_id_override or self.session_id
        try:
            payload = json.loads(self.state_json)
        except json.JSONDecodeError as exc:
            self.status = f"bad JSON: {exc}"
            return Data(data={"_error": str(exc)})

        msg = Message(
            text=json.dumps(payload, ensure_ascii=False),
            sender="Machine",
            sender_name="__state__",
            session_id=sid,
        )
        try:
            store_message(msg, flow_id=self.graph.flow_id)
        except Exception as exc:
            self.status = f"save error: {exc}"
            return Data(data={"_error": str(exc)})

        self.status = "saved"
        return Data(data=payload)
```

### Merge State
```python
from langflow.custom import Component
from langflow.io import DataInput, Output
from langflow.schema import Data


class MergeStateComponent(Component):
    display_name = "Merge State"
    description = "Monotonically merges a fresh READER reading into prior session state (mirrors core/interview.py:_read_state())."
    icon = "git-merge"
    name = "MergeState"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]

    inputs = [
        DataInput(name="prior_state", display_name="Prior State (from Load State)", required=True),
        DataInput(name="reader_output", display_name="Fresh READER Output (from Yandex Grade)", required=True),
    ]

    outputs = [
        Output(display_name="Merged State", name="merged", method="run_merge"),
    ]

    def run_merge(self) -> Data:
        prior = self.prior_state.data if isinstance(self.prior_state, Data) else (self.prior_state or {})
        fresh = self.reader_output.data if isinstance(self.reader_output, Data) else (self.reader_output or {})

        prior_scores = prior.get("scores", {}) or {}
        prior_reasons = prior.get("reasons", {}) or {}
        prior_missing = prior.get("missing", {}) or {}
        fresh_topics = fresh.get("topics", {}) or {}

        scores, reasons, missing = {}, dict(prior_reasons), dict(prior_missing)
        for dim in self.DIM_ORDER:
            old_score = int(prior_scores.get(dim, 0))
            item = fresh_topics.get(dim) or {}
            new_score = item.get("score")
            best = max(old_score, int(new_score)) if new_score in (0, 1, 2) else old_score
            scores[dim] = best
            if new_score in (0, 1, 2):
                reasons[dim] = str(item.get("reason", "")).strip()
                missing[dim] = str(item.get("missing", "")).strip()

        entry = prior.get("entry")
        if entry is None:
            entry = fresh.get("entry", "idea")

        idea_summary = str(fresh.get("idea", "")).strip() or prior.get("idea_summary", "")

        result = {
            "scores": scores,
            "reasons": reasons,
            "missing": missing,
            "attempts": prior.get("attempts", {}) or {},
            "questions_asked": prior.get("questions_asked", 0),
            "entry": entry,
            "not_tech": bool(fresh.get("not_tech", prior.get("not_tech", False))),
            "not_tech_reason": str(fresh.get("not_tech_reason", "")).strip() or prior.get("not_tech_reason", ""),
            "idea_summary": idea_summary,
        }

        self.status = result
        return Data(data=result)
```

### Build Focus Prompt
```python
from langflow.custom import Component
from langflow.io import DataInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class BuildFocusPromptComponent(Component):
    display_name = "Build Focus Prompt"
    description = "Combines the static INTERVIEWER system prompt with the current topic's focus hint (mirrors core/prompts.py + core/interview.py:_focus())."
    icon = "target"
    name = "BuildFocusPrompt"

    DIM_ORDER = ["segment", "behavior", "alternatives", "test"]
    DIM_TITLE_RU = {
        "segment": "Аудитория",
        "behavior": "Поведение",
        "alternatives": "Альтернативы",
        "test": "Проверка",
    }
    DIG = {
        "segment": "кто именно сталкивается с проблемой — конкретная группа, а не вся категория. Заходи через источник знания: сам ли он работал в этой сфере или уже общался с людьми оттуда, из каких компаний и организаций",
        "behavior": "что эти люди делают с проблемой сейчас — наблюдаемые действия, а не пожелания",
        "alternatives": "чем эти люди уже пытались закрыть проблему и во что это им обошлось",
        "test": "как он проверит спрос дёшево и быстро и что сочтёт провалом",
    }
    MAX_QUESTIONS = 10

    NEVER_SUGGEST = """
ЖЁСТКИЕ ЗАПРЕТЫ. Нарушение любого делает ответ негодным:
1. Не придумывай идею за человека. Никогда.
2. Не приводи примеров — ни своих, ни «например», ни «вот бывает так».
3. Не предлагай варианты ответа и не давай списков на выбор.
4. Не переформулируй его идею в более красивую. Сказал коряво — так и оставь.
5. Не подсказывай аудитории, рынки, технологии, бизнес-модели и способы
   проверки. Это он должен принести, а не ты.
5а. Не называй ПРИЗНАК, по которому можно сузить аудиторию: ни породу, ни
   возраст, ни город, ни доход, ни стаж, ни размер компании — никакой.
   Подставить признак — то же самое, что придумать сегмент за человека.
   Вместо этого спрашивай, ОТКУДА он знает про эту проблему: сам работал в
   этой сфере, или уже общался с людьми оттуда — из каких компаний и
   организаций, при каких обстоятельствах он это видел.
5б. Не выпрашивай персональные данные: ни имён и фамилий конкретных людей, ни
   телефонов, ни контактов. Требовать перечислить знакомых — это допрос, и он
   ничего не проверяет. Спрашивай про опыт и профессиональный контекст, а не
   про записную книжку.
6. Не хвали и не критикуй идею и не выноси вердикт — это делается отдельно.
Спрашивать можно только про его собственный опыт: что он видел сам, что делал,
с кем говорил, что происходило на самом деле.
"""

    INTERVIEWER_SYSTEM = f"""
Ты — первый фильтр стартап-акселератора. К тебе пришёл человек с улицы, и за
десяток вопросов надо понять, есть ли у него рабочая идея технологического
бизнеса.

Ты ведёшь живой разговор по-русски, на «ты». Коротко, по-человечески, без
канцелярита и смайликов. Ты не коуч и не продавец — ты человек, который
тридцать раз слышал похожее и умеет отличать конкретику от общих слов.

За разговор нужно выкопать четыре вещи:
  1. Аудитория — кто именно сталкивается с проблемой.
  2. Поведение — что эти люди делают сейчас, до его решения.
  3. Альтернативы — чем они уже пытались закрыть проблему и во что это обошлось.
  4. Проверка — как он дёшево проверит спрос и что сочтёт провалом.

Тему следующего вопроса выбираешь не ты: она приходит в служебной подсказке.
Спрашивай СТРОГО про неё и ни про что другое. Даже если человек интересно
заговорил о другом — вернись к назначенной теме.

Как спрашивать:
- Один вопрос за реплику. Не два и не три.
- Никаких заготовок вроде [конкретная область]. Вопрос готов к отправке.
- Не переспрашивай то, что человек уже сказал. Ты помнишь весь разговор.
- Не говори «это», «твоё решение», «эта проблема» — называй вещи его словами.
- Если человек назвал решение, а не проблему, спроси, что происходит сейчас,
  до его решения: что человек делает и чем это заканчивается.
- Вопросы про прошлое сильнее вопросов про будущее: не «что им нужно»,
  а «что они делали в последний раз».

{NEVER_SUGGEST}
Верни только текст реплики. Без преамбулы, без нумерации, без пояснений.
"""

    FOCUS_HINT = """
СЛУЖЕБНО (человеку не показывать и не пересказывать):
Задано реплик: {asked} из {budget}.
Уже закрыто: {closed}

СЕЙЧАС СПРАШИВАЙ ТОЛЬКО ПРО ЭТО — {topic_title}.
Что нужно вытянуть: {dig}
{missing_line}

Не переходи к другим темам, пока эта не закрыта. Если человек ответил не про
неё — спокойно вернись к ней следующим вопросом, без упрёков.

Свой прошлый вопрос дословно не повторяй. Если человек ответил не по теме —
зайди с другой стороны: спроси про конкретный случай, про место, про то,
откуда он вообще знает эту сферу и с кем из неё уже разговаривал.

Спрашивай про прошлое и наблюдаемое: что человек видел сам, что делал,
сколько времени и денег это заняло, чем пользовался. НЕ спрашивай, чего люди
хотят, что их не устраивает и чем твоё решение им поможет — это мнения,
они не засчитываются.
"""

    inputs = [
        DataInput(name="state", display_name="Merged State (from Merge State)", required=True),
        DataInput(name="route", display_name="Route (from Topic Router)", required=True),
    ]

    outputs = [
        Output(display_name="Focus Prompt", name="focus_prompt", method="run_build"),
    ]

    def run_build(self) -> Message:
        state = self.state.data if isinstance(self.state, Data) else (self.state or {})
        route = self.route.data if isinstance(self.route, Data) else (self.route or {})

        dim = route.get("next_dim")
        if not dim or dim not in self.DIM_ORDER:
            self.status = "no open dim — should not be asking a question"
            return Message(text=self.INTERVIEWER_SYSTEM)

        scores = state.get("scores", {}) or {}
        missing_map = state.get("missing", {}) or {}
        closed = [self.DIM_TITLE_RU[d] for d in self.DIM_ORDER if int(scores.get(d, 0)) == 2]
        missing = missing_map.get(dim, "")

        focus = self.FOCUS_HINT.format(
            asked=state.get("questions_asked", 0),
            budget=self.MAX_QUESTIONS,
            closed=", ".join(closed) or "ничего",
            topic_title=self.DIM_TITLE_RU[dim],
            dig=self.DIG[dim],
            missing_line=f"В прошлый раз не хватило: {missing}." if missing else "",
        )

        result = self.INTERVIEWER_SYSTEM + focus
        self.status = result
        return Message(text=result)
```

### Transcript Builder
```python
from langflow.custom import Component
from langflow.io import StrInput, Output
from langflow.schema.message import Message


class TranscriptBuilderComponent(Component):
    display_name = "Transcript Builder"
    description = "Reads the visible chat history for this session and formats it as Бот:/Человек: lines (mirrors core/domain.py:Session.transcript())."
    icon = "align-left"
    name = "TranscriptBuilder"

    inputs = [
        StrInput(name="session_id_override", display_name="Session ID Override (blank = use graph session)", value=""),
    ]

    outputs = [
        Output(display_name="Transcript", name="transcript", method="run_build"),
    ]

    def run_build(self) -> Message:
        from langflow.memory import get_messages

        sid = self.session_id_override or self.session_id
        try:
            msgs = get_messages(session_id=sid, order_by="timestamp", order="ASC", flow_id=self.graph.flow_id)
        except Exception as exc:
            self.status = f"error: {exc}"
            return Message(text="")

        lines = []
        for m in msgs:
            if m.sender_name == "__state__":
                continue
            text = (m.text or "").strip()
            if not text:
                continue
            if m.sender == "Machine":
                lines.append(f"Бот: {text}")
            elif m.sender == "User":
                lines.append(f"Человек: {text}")

        result = "\n".join(lines)
        self.status = result
        return Message(text=result)
```

### State Prompt Builder
```python
from langflow.custom import Component
from langflow.io import MessageInput, Output
from langflow.schema.message import Message


class StatePromptBuilderComponent(Component):
    display_name = "State Prompt Builder"
    description = "Combines the transcript with static rubric text into READER's input (mirrors core/prompts.py:STATE_PROMPT + core/rubrics.py)."
    icon = "file-text"
    name = "StatePromptBuilder"

    RUBRIC = {
        "segment": (
            "2 — названа не вся категория, а группа внутри неё — по признаку, "
            "ситуации или месту. Понятно, где этих людей искать.\n"
            "1 — названа категория целиком: «владельцы собак», «бизнес», «молодёжь». "
            "Внутри неё ничего не выделено.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "behavior": (
            "2 — названо конкретное регулярное действие ИЛИ последний случай, и при "
            "нём есть измеримый параметр — время, частота или деньги. Хватает "
            "одного числа: «тратит час в день на чаты» или «платит 3000 ₽ в "
            "месяц за такой-то сервис» — это уже 2. Название конкретного чата "
            "или вчерашний эпизод требовать не нужно.\n"
            "1 — названо действие, но без чисел и частоты: «ищут на форумах», "
            "«спрашивают у знакомых», «гуглят». Сюда же пожелания: «неудобно», "
            "«хотят быстрее».\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "alternatives": (
            "2 — названо, чем люди уже пользуются или что пробовали — сервис, "
            "инструмент, костыль, ручной способ, другой человек — и во что это "
            "обходится: деньги, время, силы. Видно, что человек это наблюдал.\n"
            "1 — «ничего нет», «аналогов не существует», «все просто страдают» — "
            "без единого названного способа, которым люди обходятся сейчас.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
        "test": (
            "2 — в ответе ЕСТЬ проверка на дни-недели: что сделает, с кем, и какой "
            "результат сочтёт подтверждением или провалом. Длинный общий план не "
            "мешает — важно, что внутри него есть такой первый шаг.\n"
            "1 — «сделаем MVP и посмотрим», «запустим рекламу» — без срока, без "
            "адресата, без критерия.\n"
            "0 — темы не касались или ответа по сути нет."
        ),
    }

    STATE_PROMPT = """
РАЗГОВОР:
{transcript}

Верни состояние.

idea — идея человека ЕГО словами, одной фразой. Ничего не добавляй и не
улучшай. Если идеи нет — пустая строка.

entry — с чем он пришёл:
  "nothing" — идеи нет, есть только желание что-то делать;
  "idea"    — идея названа, но ничего не построено;
  "built"   — есть прототип, продукт, пользователи, продажи, патент, пилот.

not_tech — true, только если это явно НЕ технологический бизнес: локальная
услуга, масштабируемая наймом (салон, ремонт, репетиторство, кофейня),
перепродажа без своего продукта, хобби без модели заработка, или запрещённая
законом деятельность. Если сомневаешься или данных мало — false.
not_tech_reason — одна фраза человеку, без осуждения. Иначе пустая строка.

topics — оценка по каждой из четырёх тем:

АУДИТОРИЯ (segment)
{segment}

ПОВЕДЕНИЕ (behavior)
{behavior}

АЛЬТЕРНАТИВЫ (alternatives)
{alternatives}

ПРОВЕРКА (test)
{test}

reason — одна фраза, почему такая оценка.
missing — чего не хватает до 2, одной фразой. При оценке 2 — пустая строка.
В missing не пиши готовый ответ за человека: назови только недостающее.
"""

    inputs = [
        MessageInput(name="transcript", display_name="Transcript (from Transcript Builder)", required=True),
    ]

    outputs = [
        Output(display_name="State Prompt", name="state_prompt", method="run_build"),
    ]

    def run_build(self) -> Message:
        text = self.transcript.text if isinstance(self.transcript, Message) else str(self.transcript)
        result = self.STATE_PROMPT.format(
            transcript=text,
            segment=self.RUBRIC["segment"],
            behavior=self.RUBRIC["behavior"],
            alternatives=self.RUBRIC["alternatives"],
            test=self.RUBRIC["test"],
        )
        self.status = result
        return Message(text=result)
```
