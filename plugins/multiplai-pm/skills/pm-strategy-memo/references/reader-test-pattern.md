# Reader Test — Fresh-Claude Pattern

## What This Tests

A reader test catches a different failure mode than the stress-test does. The stress-test asks "would a skeptical reader push back on this?" The reader test asks "would a *fresh* reader *understand* this?"

These are different defects:
- **Stress-test failures** — the memo has weak evidence, missing alternatives, unowned objections.
- **Reader-test failures** — the memo is structured around assumed context the reader doesn't have, the governing thought is buried, the ask is implicit.

A memo can pass stress-test and fail reader-test (rigorous but inscrutable) or pass reader-test and fail stress-test (clear but unrigorous). It needs both.

## The Method

Spawn a sub-agent with no context. Give it ONLY the memo. Ask three questions. Compare its answers to your intent.

### Why fresh-Claude specifically

A fresh Claude instance is the closest available stand-in for "a reader who has the role but not the context." Real readers always carry context — meetings they've been in, prior memos they've read, the political weather. Fresh Claude carries none of that. If fresh Claude understands the memo, a real reader with context will too.

### Spawning the sub-agent

Use the Agent tool with `subagent_type: general-purpose`. Pass the memo content in the prompt. Do NOT give the sub-agent any other context about the project, the user, or the intent — the test only works if the sub-agent starts cold.

### The three questions

Always exactly these three. Don't customize unless absolutely necessary; the consistency is what makes the test diagnostic.

```
1. What is this memo asking the reader to do?
   (Tests: is the ask clear?)

2. Summarize the argument in three sentences.
   (Tests: is the governing thought + supporting arguments intelligible?)

3. What is the strongest objection a reader would have to this memo,
   and does the memo address it?
   (Tests: is the memo robust to its own weaknesses?)
```

## Sub-Agent Prompt Template

```
You are a reader of a strategy memo. You have no prior context — you have not
been in any meetings about this topic, you have not read any related documents,
you do not know the team or the company.

Read the memo below. Then answer these three questions, in order:

1. What is this memo asking the reader to do? Answer in one sentence.
2. Summarize the argument of the memo in exactly three sentences.
3. What is the strongest objection a reader would have to this memo, and does
   the memo address it? If it addresses the objection, quote the passage. If
   it does not, say so.

Do not editorialize. Do not offer suggestions. Just answer the three questions
based on what is written in the memo.

---
[FULL MEMO CONTENT]
---
```

## Reading the Sub-Agent's Answers

### Question 1 — The Ask

| Sub-agent's answer | Diagnosis |
|--------------------|-----------|
| Matches your intended ask | PASS |
| Names the topic but not the ask ("the memo is about positioning") | FAIL — TL;DR doesn't surface the ask |
| Names a different ask than intended | FAIL — TL;DR is misleading |
| Says "it's not clear" | HARD FAIL — rewrite the TL;DR |

### Question 2 — The Argument

Compare the sub-agent's three-sentence summary to your intended governing thought + supporting arguments.

| Sub-agent's summary | Diagnosis |
|--------------------|-----------|
| Matches the governing thought + 1-2 main supports | PASS |
| Captures the topic but inverts or distorts the claim | FAIL — argument is unclear or evidence is misleading |
| Lists 3 different topics with no clear logical chain | FAIL — pyramid structure broke down in prose |
| Restates a side argument as the main argument | FAIL — emphasis is wrong, supporting argument was given too much weight |

### Question 3 — The Objection

This question is the trickiest because the sub-agent will name *some* objection. The diagnostic value is in **whether the objection it names is one your memo addresses**.

| Sub-agent's objection | Diagnosis |
|----------------------|-----------|
| Names an objection from your Objections section + correctly says the memo addresses it | PASS — Objections section is doing its job |
| Names an objection NOT in your Objections section | FAIL — you missed a real objection. Add it. |
| Names an objection from your section but says the memo doesn't address it | FAIL — your addressing of that objection is too weak. Strengthen. |
| Names a strawman objection | NEUTRAL — sub-agent may be reaching; sanity-check whether a real reader would have the same |

## Revision Loop

After reader-test:

1. **List the defects** (ask unclear, argument distorted, missing objection, etc.)
2. **Revise the memo** to address each.
3. **Re-run reader-test** with a fresh sub-agent (don't reuse the previous one — it's not fresh anymore).
4. **If 2nd reader-test still fails on the same defect**, stop revising blindly and escalate to the user. Sometimes the failure is in the underlying argument, not the prose.

Cap at 3 reader-test cycles. After 3, additional cycles produce diminishing returns and the user should intervene.

## When to Skip Reader-Test

Almost never. Skip only when:
- The user explicitly requests `--skip-reader-test true` (acknowledged risk).
- The memo is &lt; 300 words and the ask is one sentence (test overhead exceeds value).
- A real human reader is going to read it within the same session anyway (rare).

## Storing the Reader-Test Output

Save the full sub-agent prompt + sub-agent response + your diagnosis as part of the `-method.md` companion file (see SKILL.md output section). This makes the memo auditable later: someone reading the memo in 6 months can see what passed reader-test and what didn't.

```markdown
## Reader Test — YYYY-MM-DD HH:MM

**Sub-agent prompt:** [omitted for brevity, or full text]

**Sub-agent response:**

1. ...
2. ...
3. ...

**Diagnosis:**
- Q1: PASS / FAIL — [why]
- Q2: PASS / FAIL — [why]
- Q3: PASS / FAIL — [why]

**Revisions made before/after this test:**
- ...

**Cycle:** 1 of N
```

## Common Failure Modes

### Test the writer, not the memo

Symptom: you re-read your own memo and "feel like it's clear."
Fix: that's not the test. Spawn the sub-agent. Trust its output.

### Cherry-pick the sub-agent

Symptom: you spawn 5 sub-agents until one gives a friendly answer.
Fix: take the first answer. If it fails, revise the memo, then test again. Don't shop for outcomes.

### Treat sub-agent disagreement as the sub-agent being wrong

Symptom: "the sub-agent didn't get it because it doesn't have context."
Fix: that's the point. If the memo only makes sense with context the sub-agent doesn't have, real readers without that context will fail too. The memo is the defect, not the test.
