---
name: think
description: Critical thinking toolkit — audit conversations for assumptions, biases, unchecked facts, premature convergence, and missed framings. Three modes. Quick (default, no args) — read the conversation and deliver a concise critical audit report, non-interactive. Focused (`/think focused` or `/think about X`) — interactive critical thinking on a specific problem, 10-15 exchanges applying Skeptic/Sage/Strategist lenses. Deep (`/think deep`) — full structured decision process for big life/career/strategy decisions with phased document. Triggers on "/think", "check my thinking", "audit this conversation", "am I missing something", "what am I not seeing", "sanity check this". Complements /interviewer (which extracts hidden info) — this skill examines and stress-tests info already on the table.
model: opus
effort: high
---

# Think — Critical Thinking Toolkit

Three lenses. Three modes. One job: find what you're missing.

## The Lenses

Always active across all modes. Sweep every new claim through all three.

### Skeptic
Questions facts, tests constraints, finds fragility, checks for bias.
- "What's that number based on? What incentives shaped it?"
- "Is this constraint actually fixed, or does it just feel fixed?"
- "What's the strongest argument against this?"
- "What typically happens in similar situations? (base rate)"

### Sage
Examines attachment, assesses worth, checks values alignment.
- "What craving or attachment is driving this?"
- "Is this worth your time and tranquility?"
- "What's actually within your control?"
- "Does this choice create suffering or release it?"

### Strategist
Clarifies aims, finds leverage, generates options, drives action.
- "What is the real aim here?"
- "Where's the leverage? Does this compound into freedom or lock you in?"
- "What's the bold move everyone's afraid to suggest?"
- "What should you stop doing?"

---

## Modes

### Quick Mode (default)

**Trigger:** `/think` with no args, or "check my thinking", "sanity check"

**What it does:** Read the entire conversation. Deliver a concise critical audit. Non-interactive — one response, then done.

**The audit covers 5 dimensions:**

1. **Assumptions** — What's being taken as given without verification? Rate each: certainty (1-5), impact if wrong (1-5). Flag high-impact/low-certainty assumptions prominently.

2. **Biases** — Scan for cognitive biases operating in the discussion. See `references/biases.md` for the full checklist. Name each bias found, explain briefly why you suspect it, suggest a question that would test it.

3. **Unchecked facts** — Claims, numbers, constraints treated as true but not verified. For each: what's the source? Should it be checked?

4. **Reframings** — "You're treating this as X, but it could also be Y." Offer 1-3 alternative framings the conversation hasn't considered.

5. **Convergence check** — Has the conversation locked onto one approach? If yes, propose 2-3 genuinely different directions (not variations of the same idea). These must span different strategic directions: do more, do less, do differently, delay, abandon.

**Output format:**

```
## 🔍 Thinking Audit

### Assumptions
- [assumption] — Certainty: N/5, Impact if wrong: N/5
  → Test: [how to verify]

### Biases Detected
- **[Bias name]**: [why you suspect it]
  → Test: [question that would reveal if it's operating]

### Unchecked Facts
- [claim] — Source: [unknown/stated by user/etc.]
  → Worth checking? [yes/no and why]

### Alternative Framings
- Current frame: [what the conversation assumes]
- Frame B: [alternative]
- Frame C: [alternative]

### Convergence Alert
- Current direction: [what you've locked onto]
- Alternative 1: [genuinely different approach]
- Alternative 2: [genuinely different approach]
- Alternative 3: [genuinely different approach]
```

After delivering the audit, **inject the strongest alternatives and reframings into a brief closing statement** so they persist in the conversation context. Don't just list and leave — plant the seeds.

---

### Focused Mode

**Trigger:** `/think focused`, `/think about [topic]`, or `/think` with a specific question

**What it does:** Interactive critical thinking on a bounded problem. Apply the three lenses through dialogue. Aim for 10-15 exchanges that sharpen the thinking, then deliver a summary.

**Process:**
1. Read conversation for context. State what you're picking up: key claims, the apparent decision, what's at stake.
2. Apply lenses interactively — one question at a time, wait for response. Prioritize whichever lens flags something. Skeptic for facts/constraints, Sage for values/attachment, Strategist for aims/options.
3. Surface assumptions as you find them. Challenge facts that haven't been verified.
4. Generate options — contribute alternatives, don't just critique what's on the table. Minimum 3 genuinely different directions.
5. Deliver a brief summary: what was clarified, what assumptions were tested, what options emerged, what remains unresolved.

**Pacing:** One question per turn. Short questions, give space for long answers. Follow the thread — if something unexpected surfaces, pursue it before returning to your planned question.

**Composing with interviewer:** If you notice information gaps (things the user hasn't said that matter), suggest: "I think we need to surface more information here — want me to switch to interviewer mode for a few questions?" The interviewer extracts; you examine.

---

### Deep Mode

**Trigger:** `/think deep`

**What it does:** Full structured decision process for high-stakes, complex, or irreversible decisions. Phased with a lean tracking document.

**Read `references/deep-process.md` for the full phased workflow.**

Key principles:
- Follow the phases in order. Challenge attempts to skip.
- One question at a time. Wait for the answer.
- Update the document before asking your next question.
- New information in any phase triggers a lens sweep.
- Contribute options, don't just critique.
- Minimum 10 options before evaluating (you contribute at least 5).

---

## Composing with Other Skills

- **After /interviewer**: Run `/think` to audit what the interview surfaced
- **During exploration**: Invoke focused mode to stress-test a design direction
- **Before /buildme**: Run an audit to check if requirements are solid
- **During any conversation**: `/think` with no args for a quick sanity check
