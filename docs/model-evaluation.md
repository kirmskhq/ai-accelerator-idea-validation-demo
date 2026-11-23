# Model evaluation

How the models for each role were chosen, with the measurements behind the
numbers in the README. All runs used synthetic interviews in August 2026 on
Yandex AI Studio. These are small engineering checks, not a benchmark: each
comparison ran on one prompt or a handful of interviews.

## Interviewer (the agent that words the questions)

**Test.** The interviewer's system prompt plus a deliberately weak audience
answer, «строительные компании и их поставщики» ("construction companies and
their suppliers"). That names a whole category, a clear 1 on the rubric. The
correct behaviour is a *second* question on the same topic that does not hint
at how to narrow the audience. Tool-calling support was checked separately,
because the agent has to call the search and verdict tools.

| Model | Calls tools | Stayed on the topic | Hinted at the answer |
|---|---|---|---|
| `yandexgpt/latest` | unreliable¹ | no, skipped to the next topic | — |
| `yandexgpt-5.1` | yes | yes | yes: suggested narrowing «по масштабу проектов» |
| `yandexgpt-5-pro` | **no** | yes | no |
| **`deepseek-v4-flash`** | yes | yes | no: asked where the founder's knowledge comes from |
| `qwen3-235b-a22b-fp8` | yes | yes | mildly: «в каком регионе» |
| `qwen3.6-35b-a3b` | yes | yes | no |
| `gpt-oss-120b` | yes | yes | yes: listed example answers |

¹ Returned tool calls in an isolated test but not inside the flow.

**Result.** Two of the seven models passed all three checks: `deepseek-v4-flash`
and `qwen3.6-35b-a3b`. DeepSeek is the interviewer; Qwen 3.6 is the fallback
for this role.

**Follow-up.** In a full end-to-end interview DeepSeek still asked once «Может,
какие-то программы, каталоги, другие сервисы?», which offers example answers.
The persona now forbids listing options inside a question.

**What the comparison does not show.** Holding the rule here depends on the
model being strong enough, not on the rule being enforced. That is why topic
choice and attempt counting later moved into code (see
[DECISIONS.md](DECISIONS.md), D3).

## Scorer (structured JSON, never shown to the founder)

**Test.** The scoring prompt on the same transcript, strict JSON schema,
temperature 0.

| Model | Time | Output tokens |
|---|---|---|
| **`gpt-oss-120b`** | **6.9 s** | 674 |
| `deepseek-v4-flash` | 19.6 s | 732 |
| `qwen3.6-35b-a3b` | 14.4 s | 2,245 |

**Result.** `gpt-oss-120b` is the scorer. It hints at answers when it *talks*
to founders, which is why it is not the interviewer, but here it only fills in
a JSON schema.

- `qwen3.6-35b-a3b` was tried and rolled back. It was fast on its own, but
  inside the flow a turn hung for over two minutes. The likely cause is its
  three times larger output overrunning the 90 s timeout and triggering a
  retry. This was not confirmed.
- On the first turn `gpt-oss-120b` was slightly more generous than DeepSeek
  (it gave the audience a 1 where DeepSeek gave a 0). This did not change any
  final verdict in the test runs. Switching back is a one-field change.
- A hypothesis that turned out wrong: strict JSON mode was not the cause of
  slow scoring. Plain text on DeepSeek took 17.5 s, strict mode 16.8 s. The
  time went on output length.

## Web search

`yandexgpt/latest` with Yandex AI Studio's built-in `web_search` tool. It was
left unchanged because it worked. The tool returns the URLs it actually cited,
and the component counts the distinct websites among them.

## Whole interview

Measured on synthetic interviews with the models above:

| Metric | Value |
|---|---|
| Latency per question turn | 6–20 s |
| Final turn (search + verdict) | about 26 s |
| Interview length | 4–5 questions against a budget of 10 |
| Tokens per interview | about 34,000 in, 2,200 out |
| Cost per application | about ₽12 (≈ $0.15); ceiling about ₽17 at the 10-question budget |

The cost breaks down as ₽10.2 for input tokens, ₽1.1 for output tokens and
₽0.9 for the web search call, at Yandex AI Studio's prices at the time. The
ceiling is set by the question budget, not by the price list.

Two things cut the question-turn latency from 15–38 s down to the range above:

1. **A 25-word limit on questions** made the biggest difference. In a clean
   test on DeepSeek, one turn took 24.4 s and 1,019 tokens without the limit,
   and 2.0 s and 60 tokens with it. The limit does not apply to the final
   explanation of the verdict.
2. **Smaller scorer output**: no free-text justification field, `missing`
   capped at 10 words, only still-open topics scored, and only the last 10
   messages sent (scores never go down, so closed topics need no re-reading).

These measurements predate fixes D8–D15 in [DECISIONS.md](DECISIONS.md).
Those fixes leave the models and the scoring rubric unchanged. One has a
visible effect: the founder's opening message no longer uses up an attempt on the
first topic, so an interview can run one question longer than measured here.

## Live run on NeuralDeep (November 2026)

The current version was run end to end on NeuralDeep's free tier, with all
three roles on `qwen3.6-35b-a3b-noreason`. The run used Langflow 1.11.1's
own Agent, message store and tool-permission checks, and loaded the flow from
its JSON the way the Langflow server does. The founder was simulated: each
turn it sent a prepared answer for whichever topic the controller had just
assigned.

| | Strong founder | Weak founder |
|---|---|---|
| Opening | Burnout app for students, MVP, 3,000 users | "A platform for choosing concrete slabs" |
| Questions asked | 3 | 7 |
| Verdict | pass (R5), with homework | improve (R3): behaviour and validation still vague |
| Question turns | 5–15 s | 2–5 s |
| Final turn (search + verdict) | 16 s | 9 s |
| Scores seen by the agent | none | none |

Both interviews together used 27 chat requests. The behaviour matched the
design: one question per reply, a second question on a weak answer, a move to
the next topic after two attempts, and the verdict text relayed in full.

What the run surfaced:

- **The final step needed its own prompt (D13).** In the first live run the
  agent ignored the instruction to call the tools and kept asking questions
  for 11 more turns. In a direct test, Qwen called `search_market` 0 times out
  of 1 with the question rules in the prompt, and 3 times out of 3 with a
  separate final-stage prompt.
- **A weaker model shortens the verdict (D14).** Qwen retold a pass verdict as
  one line, dropping the explanation and the homework. The verdict now writes
  the founder-facing text itself.
- **Scoring on Qwen 3.6 is lenient.** It gave 2 to «строительные компании и
  их поставщики», which the rubric's own examples treat as a whole category
  (a 1). The reasoning variant gave the same score and took 29 s instead of
  3 s. The free tier is good for testing the flow, not for judging scoring
  quality.
- **NeuralDeep's web search returns weak matches** for these queries: app-store
  front pages, catalogues and articles. The model correctly rejected them, so
  both runs reported no similar products. The verdict does not depend on that
  count.
- The model praised the founder once («Отлично, 3000 пользователей — это уже
  что-то»), which the persona forbids.
