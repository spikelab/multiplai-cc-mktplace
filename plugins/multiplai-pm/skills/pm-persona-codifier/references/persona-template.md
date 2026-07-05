# Persona Template — Canonical Schema

Every persona file uses this exact structure. Section order is fixed. Section labels are fixed. This is so downstream skills can grep specific sections without parsing prose.

---

## Section-by-Section Guidance

### 1. Snapshot

Max 3 sentences. The reader should be able to identify whether a real person matches this persona after reading just the snapshot.

**Good:** "The Flow Builder is an internal RevOps person or external consultant who actually operates the data tooling on behalf of an enterprise customer. They are technical enough to think procedurally about workflows but rarely write code. Their day is dominated by data triage and stakeholder handholding."

**Bad:** "Sarah is a busy professional who values efficiency." (No identifying signal.)

### 2. Jobs to be Done (top 3, prioritized)

Use the canonical job story format: `When [situation], I want to [motivation], so I can [outcome].`

If JTBD synthesis exists, reference job IDs from the synthesis. If not, draft from interview evidence.

Order matters — #1 is the *primary* job, the one the persona would defend if asked to trade off.

```
1. When [situation], I want to [motivation], so I can [outcome].
   Source: transcript.txt L#-#
2. ...
3. ...
```

### 3. What They Actually Want (vs. What They Say They Want)

This is the highest-leverage section. Customers usually ask for the thing one level removed from what they actually want. Force the translation with source evidence.

```
| They say they want | What they actually want | Evidence |
|--------------------|-------------------------|----------|
| A builder UI       | Control and proof       | "I want to see how it works" — L142 |
| Faster onboarding  | To not look incompetent in front of their team | L201-205 |
```

3–5 rows. If the column 1 and column 2 entries are the same, the row isn't useful — delete it.

### 4. What "Good" Looks Like for Them

How this persona defines a successful outcome. The metrics, signals, or felt experiences they use to evaluate whether they're winning.

Important: this is **persona-specific**, not the company's KPIs. The buyer's "good" is closing a deal under budget; the user's "good" is not getting yelled at in standup. Different.

3–5 bullets, each anchored to source where possible.

### 5. Decision-Making Power and Process

- **Role in the buying process:** decider / influencer / blocker / user / champion
- **Budget authority:** yes / partial / no
- **Who they have to convince:** [list]
- **Who has to convince them:** [list]
- **Typical evaluation process:** [paragraph]
- **Deal-breakers:** [bullets]

For B2C personas, adapt: decider, influencer (e.g. spouse, peer), gatekeeper (e.g. platform policy).

### 6. Quotes (3–5, with source attribution)

Verbatim, with line number anchors. The quotes should be *characteristic* — a reader skimming should hear the persona's voice in them.

```
> "Honestly the UI scares me, I just want to know it works." — `transcript.txt L142-145`
> "If this fails, I'm the one who has to explain it." — `transcript.txt L201`
```

If you can't reach 3, the persona is under-evidenced. Flag in Open Questions.

### 7. Implications for Product (3 bullets)

What the product team should do *because* this persona exists. Each implication should be specific enough to be argued against.

**Good:** "Trial flow must produce visible 'proof of work' artifacts within 30 minutes — for this persona, time-to-screenshot-able-output is the trust signal."

**Bad:** "Make the UI better." (Too vague to act on.)

Cap at 3. Don't write a product strategy in this section — that's `pm-strategy-memo`'s job.

### 8. Anti-Persona (what they are NOT)

Lookalikes that pattern-match to this persona but aren't actually it. The anti-persona is the disqualifier test.

```
- NOT [adjacent role that resembles them but differs in [axis]]: differs because [...]
- NOT [behavior that resembles them but differs in [axis]]: differs because [...]
```

If you can't write 2 distinct anti-personas, the persona definition is too loose. Tighten it.

### 9. Open Questions

What the persona definition can't answer with current evidence. 2–5 questions, each answerable by interview.

```
1. What % of [persona] are external consultants vs. internal hires? Affects channel strategy.
2. When [persona] says "I want a UI," is that always trust-coded, or sometimes actual UI desire?
3. ...
```

---

## File Structure

```markdown
---
id: persona-<slug>
name: The <Role/Behavior>
status: draft
sources:
  - path/to/source-1
  - path/to/source-2
related_personas: [other-persona-id]
last_updated: YYYY-MM-DD
---

# The <Name>

## 1. Snapshot

...

## 2. Jobs to be Done

...

## 3. What They Actually Want

...

## 4. What "Good" Looks Like

...

## 5. Decision-Making Power and Process

...

## 6. Quotes

...

## 7. Implications for Product

...

## 8. Anti-Persona

...

## 9. Open Questions

...
```

## INDEX File Structure

```markdown
# Personas — INDEX

**Last updated:** YYYY-MM-DD
**Status:** all draft / mixed / all locked

## Personas

| ID | Name | Snapshot (1 sentence) | Status |
|----|------|----------------------|--------|
| persona-flow-builder | The Flow Builder | One-line condensation of the snapshot. | draft |
| persona-trust-buyer | The Trust-First Buyer | ... | draft |

## Cross-Persona Tensions and Notes

Where personas disagree, overlap, or interact in ways the product team needs to know.

- **[Tension]** — [one sentence]
- ...

## Anti-Personas Across the Set

Lookalikes that resemble multiple personas. These are the global disqualifiers.

- ...

## Confidence Notes

Which personas are well-evidenced (3+ quotes, multiple sources) vs. under-evidenced (single source, few quotes).
```
