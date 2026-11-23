# Design decisions

The decisions that shaped the screener, each with the evidence behind it.
D1–D7 came out of building and debugging the flow. D8–D12 came from reviewing
the components against Langflow 1.11.1's source code. Measurements are in
[model-evaluation.md](model-evaluation.md).

## D1. One agent instead of a hand-wired graph

The first port of the interview logic into Langflow had 16 nodes. About ten
of them only moved state around: load, save and merge state, build the
transcript, build the prompt, route the topic. The canvas was unreadable and
left no room for real agentic parts. A Langflow Agent gets conversation memory
for free, so most of that plumbing went away.

A reply filter that trimmed the agent's output to one question was also
dropped. It cut everything after the first «?», which would have mangled the
final verdict message. "One question per reply" became a prompt rule instead.

## D2. The verdict is code

Rules R1–R5 decide pass, improve or not-a-tech-business. They are plain Python
in `verdict_r1_r5.py`, so the same scores always give the same verdict and
any verdict can be traced back to a rule.

| Rule | Condition | Outcome |
|---|---|---|
| R1 | Not a technology business | not_tech |
| R2 | Any topic scored 0 | improve, with homework for the weak topics |
| R3 | Any topic scored 1 | improve, with homework for the weak topics |
| R4 | All topics at 2, but the founder arrived without an idea of their own (entry stage `nothing`) | pass, with homework on talking to real users |
| R5 | All topics at 2 | pass |

The scores that feed the rules still come from a model (see the README's
known limitations).

## D3. Counting cannot live in a prompt

The rule "at most two questions per topic, and ask a second one if the first
answer was weak" did not hold as a prompt instruction. Given a weak audience
answer, the agent judged it good enough and moved on, so interviews ran four
questions instead of six to eight. The verdict then marked the founder down
for the vagueness the agent never probed.

Two rewordings were tried and both failed. One of them spelled out the count
explicitly with examples. A stronger interviewer model held the rule (see
[model-evaluation.md](model-evaluation.md)), but only because it was strong
enough, not because anything enforced it. Counting moved into code.

## D4. The controller is a graph node, not a tool

The first version of the code controller was a tool, `submit_answer`, that the
agent was told to call after every answer. In a test interview it called the
tool on 1 turn out of 5 and improvised the rest, with no scoring and no
question budget.

The controller now sits between Chat Input and the agent's system prompt, so
Langflow runs it on every turn and the agent receives the next topic as an
instruction. In the same test it ran on 5 turns out of 5. The tool no longer
exists.

## D5. One model per role

The interviewer and the scorer have different jobs. The interviewer's wording
is read by a person and must not hint at answers; the scorer fills in a JSON
schema nobody sees. Splitting them let each role use the model that suited it:
`deepseek-v4-flash` to interview and `gpt-oss-120b` to score, three times
faster than DeepSeek at scoring.

## D6. Short outputs for speed

Latency was dominated by output length, not by the model or JSON mode. A
25-word limit on questions, a shorter scorer schema and scoring only the
still-open topics cut a question turn from 15–38 s to 6–20 s.

## D7. The verdict fails loudly and takes no arguments

In one test the assistant gave a confident, well-argued and completely wrong
verdict: all four topics were at 2, yet the founder was told to improve, with
homework on every topic. The verdict component could not read the interview
state, quietly substituted zeros, and rule R2 fired on the zeros. From the chat
it looked like normal behaviour.

The verdict now raises an error instead of guessing. The error text tells the
agent not to give a verdict and to tell the founder that a technical failure
occurred. A tool error in Langflow reaches the agent as the tool's result, so
the agent sees that instruction.

Separately, the agent used to pass the scores into the verdict tool. It sent
booleans and numbers into text fields, Langflow rejected the call, and the
retry cost an extra model round-trip (26,780 tokens and 69 s in one run). The
verdict now reads everything from the interview state and takes no arguments.

## D8. Interview state is kept out of the agent's memory

The controller stored its state, including the scores and what each answer was
missing, as a chat message in the conversation's own session. In Langflow
1.11.1 the Agent loads its chat history by session and flow only, with no
filter on who sent a message. So every saved state reached the interviewer
model as if it were one of its own earlier replies. This undercut D2–D4: the
model that words the questions could see the scores.

State now lives in a companion session, `<session id>::interview-state`. The
controller, the verdict and the web search write to it and read from it; the
agent never loads it. The trade-off is one extra entry per conversation in
the Playground's session list.

Related: inside a flow, Langflow 1.11.1 returns an empty list, not an error,
when a message read is out of scope. An empty read is therefore treated as an
error whenever the state should exist, for example when the bot has already
asked a question in this chat.

## D9. The opening message does not use up an attempt

The founder's first message, which describes the idea, was counted as the
first answer on the audience topic. The bot's very first question was then
already the topic's "second attempt", and a weak answer to it closed the topic.
The opening message is still scored, since it often answers some topics
outright, but attempts are only counted for answers to the bot's questions.

## D10. Outages are not charged to the founder

If the scoring call failed, the turn still used up one of the founder's
attempts with no score recorded. A provider outage could therefore turn into
an "improve" verdict. Now:

- A failed scoring call does not count as an attempt. The answer stays in the
  transcript and is scored on the next turn.
- The error is shown in the controller's status and saved in the state as
  `last_score_error`.
- After two failures in a row the agent stops asking questions and tells the
  founder that the interview has to continue later.
- Failures to read or save the state stop the turn with an error, instead of
  silently starting the interview over.

## D11. Short answers always reach the scorer

The controller added the latest answer to the transcript only if no earlier
line already contained it as a substring. Short answers such as «да» or a
single number matched words in earlier questions and were dropped whenever
Chat Input had not stored the message first. It now compares whole lines.

In the same merge step, the "what is missing" note could be overwritten by a
note for a lower score than the one kept, so the state could contradict
itself. The note now changes only together with the score.

## D12. The market count comes from the search, not the agent

The agent used to read the number of results from the search tool and type it
into the verdict tool, so a model relayed a number that code then relied on.
The search now writes its result to the interview state and the verdict reads
it from there.

The number itself was the count of cited URLs, and two pages from one site
counted twice. It is now the number of distinct websites among the URLs the
search actually cited. The founder-facing text describes it that way: sites
with similar products, not verified competitors.
