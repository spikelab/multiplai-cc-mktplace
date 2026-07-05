# Job Stories — Canonical Format

## Origin

Job stories were popularized by Paul Adams and the Intercom team as a replacement for traditional user stories ("As a [persona], I want to [action], so that [benefit]"). User stories anchor on personas, which fossilize assumptions. Job stories anchor on *situation*, which is more resilient to changing user populations.

## Canonical Structure

```
When [situation],
I want to [motivation],
So I can [expected outcome].
```

That's the whole structure. Three clauses. No persona. No features. No solutions.

### When [situation]

The trigger context. Concrete, observable, specific enough to recognize when it happens. Time markers, location markers, state markers.

**Good:** "When I'm in a standup and I'm asked for a status update on a project I haven't touched in three days..."

**Bad:** "When I need to know what's going on..." (too vague — when *exactly* does that happen?)

### I want to [motivation]

The progress the customer wants to make. Stated in their language, in their frame. Outcome-oriented, not solution-oriented.

**Good:** "I want to give an accurate two-sentence update without sounding like I'm guessing..."

**Bad:** "I want a dashboard..." (that's a solution, not a motivation)

### So I can [outcome]

The reason the motivation matters. The deeper value or higher-order job behind it. Often emotional or social.

**Good:** "...so I can keep my team's trust in my project oversight."

**Bad:** "...so I can have visibility." (visibility is just a re-statement of the motivation — what does visibility *get* them?)

## Output Format (for pm-jtbd-synthesis file 2)

```markdown
# Job Stories — [topic / customer segment]

**Date:** YYYY-MM-DD
**Derived from:** `INBOX/jtbd-synthesis-YYYY-MM-DD.md`

## Primary Jobs (2+ transcripts)

### Job 1 — [Short label]

When **[situation]**,
I want to **[motivation]**,
So I can **[outcome]**.

- **Evidence:** N transcripts
- **Confidence:** STRONG / SUPPORTED / WEAK
- **Anchor quote:**
  > "..." — `transcript.txt L#-#`

(Repeat per primary job; cap at 5.)

## Secondary Jobs (single transcript)

Same structure, but mark as `single-source`. Cap at 3.

### Job — [Short label]

When ...
I want to ...
So I can ...

`single-source` — `transcript.txt`

## Anti-Jobs (jobs the customer explicitly does NOT have)

This section is optional but high-value when present. Customers often say what they're *not* trying to do, which is gold for scope discipline.

- **[Anti-job]** — "[verbatim quote]" — `transcript.txt L#`
```

## Heuristics

### Test 1 — Solution-Free

Re-read each job story. Could it be answered by 3+ different products/solutions? If only one solution would satisfy it, the motivation clause is too solution-shaped. Rewrite.

### Test 2 — Outcome-Anchored

Re-read the "so I can" clause. Does it state a *consequence* that matters to the customer? Or is it just the motivation restated? If restated, you haven't reached the real value. Probe deeper using `interviewer`'s TEDW or "5 whys."

### Test 3 — Situation-Concrete

Read the "when" clause aloud. Could a teammate identify the moment when it's happening? If not, the situation is too abstract. Add markers.

### Test 4 — Customer's Frame

Read the whole story. Does it sound like the customer wrote it, or like a PM wrote it about the customer? PM language ("user," "experience," "delight," "friction") is a tell. Rewrite in plain customer language.

## Common Failure Modes

1. **Feature-disguised-as-job.** "I want a notification when..." is a solution. The job is "I want to know when something needs my attention without checking constantly."

2. **Persona-leakage.** "As a sales manager, when..." reintroduces personas through the back door. Job stories are persona-agnostic on purpose.

3. **Story sprawl.** 20 job stories is not synthesis. If you can't compress to ≤5 primary + ≤3 secondary, the clustering in Pass 2 of the parent skill wasn't aggressive enough.

4. **Outcome stuck on the motivation.** "...so I can do [the motivation] better" is a smell. The outcome should be a step removed from the motivation — what the motivation buys them.
