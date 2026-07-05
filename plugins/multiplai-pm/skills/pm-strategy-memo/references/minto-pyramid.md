# Minto Pyramid Principle — Canon

## Origin

Barbara Minto, *The Pyramid Principle* (first published 1973, McKinsey consulting standard). The pyramid is the structural backbone of nearly every effective McKinsey, BCG, and Bain memo. Adopted at Amazon and adapted into the 6-pager format.

## The Principle

Ideas at any level in writing should be summaries of the ideas grouped beneath them. The reader should be able to start at the top, get the full picture in one sentence, then descend only as deep as their interest warrants.

```
              GOVERNING THOUGHT (one sentence — the whole point)
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
   Supporting        Supporting        Supporting
   Argument 1        Argument 2        Argument 3
        │                 │                 │
    ┌───┴───┐         ┌───┴───┐         ┌───┴───┐
   Evidence Evidence Evidence Evidence Evidence Evidence
```

This is inverted from how most people *think* (bottom-up, accumulating evidence into conclusions) but it is how readers *read* (top-down, deciding whether to keep reading based on the top).

## The Four Rules

### Rule 1 — Ideas at each level summarize the level below

The governing thought is the summary of the 3-5 supporting arguments. Each supporting argument is the summary of the evidence beneath it. If the summary doesn't actually cover the children, the structure is broken.

**Diagnostic:** read just the governing thought + the supporting argument headlines. Does it produce a coherent argument? If you need to read the evidence to make sense of the headlines, the headlines are too vague.

### Rule 2 — Ideas at each level are the same kind of thing

Don't mix reasons, recommendations, and observations at the same level. If Argument 1 is "the market wants this," Argument 2 should also be a reason, not "we should hire 3 engineers" (which is a recommendation).

**Diagnostic:** ask "what question does the level above answer?" If the children all answer the same question (why? how? what? when?), the level is clean. If they answer different questions, the level is mixed.

### Rule 3 — Ideas at each level are logically ordered

Three valid orderings:
- **Deductive** — premise 1, premise 2, therefore conclusion. Used for arguments where one piece must precede the next.
- **Time** — first, then, finally. Used for sequences.
- **Importance** — most → least, or least → most. Most common in strategy memos.

Pick one and apply it consistently within each level. Mixing orderings is a tell that the writer doesn't know which order matters.

### Rule 4 — MECE: Mutually Exclusive, Collectively Exhaustive

The supporting arguments must not overlap (mutually exclusive) and must together cover the territory the governing thought claims (collectively exhaustive).

**ME violation:** Argument 1 is "customers want this" and Argument 2 is "users want this." In B2B these overlap.
**CE violation:** Arguments cover the upside of a decision but not the downside, or cover three of four affected stakeholder groups but not the fourth.

MECE is a high bar in practice. The pragmatic version: no significant overlap, no glaring omission.

## The Governing Thought Test

The governing thought is the single most important sentence in the memo. The pyramid stands or falls on it.

**Tests it must pass:**

1. **Subject-verb-object.** "We should reframe positioning as a data-product-factory by Q3" — works. "Positioning is important" — doesn't work, has no verb of action.

2. **One sentence, no "ands" between independent clauses.** "We should reframe positioning AND restructure the team" is two governing thoughts. Two memos.

3. **Specific enough to be argued against.** "We should improve quality" is unarguable, therefore meaningless. "We should reject the Attento initiative and shift those resources to observability features for trust-building" is arguable, therefore meaningful.

4. **Directional.** It moves the reader from one state to another — from X to Y, from doing A to doing B. If the governing thought doesn't imply a change, the memo doesn't have an ask.

5. **Restate-able by someone who hasn't read the memo.** Read the governing thought to a colleague. Can they restate it back accurately? If they can't, the language is too dense or the thought is too compressed.

## The Question-Answer Discipline

Minto's deeper insight: every level of the pyramid must answer a question the reader is silently asking. The governing thought answers "what is this memo's point?" Each supporting argument answers "why should I believe the governing thought?" Each piece of evidence answers "why should I believe this argument?"

If a paragraph doesn't answer a silent reader question, cut it. The discipline of asking "what question am I answering here?" is the most underrated editing technique.

## Common Failure Modes

### Multiple governing thoughts

Symptom: the TL;DR has 3 sentences and they're each claiming something different. Memo loses the reader by paragraph 3.
Fix: pick one. The others become supporting arguments or get cut.

### Arguments that aren't arguments

Symptom: section headlines are topics ("Positioning," "Personas," "UI Strategy") not claims.
Fix: rewrite each as a complete sentence stating a claim. If you can't, the section probably doesn't have a real argument.

### Evidence without claim

Symptom: paragraphs full of data with no clear "therefore." Reader is left to draw the conclusion themselves.
Fix: lead each paragraph with the claim. Use the evidence to support it. Don't make the reader do your synthesis work.

### Pre-emptive defensiveness

Symptom: memo opens with caveats, qualifications, and acknowledgments before stating the point.
Fix: lead with the governing thought. Caveats belong in Objections, not Introduction.

### Burying the ask

Symptom: the memo argues a point but doesn't say what it wants the reader to do.
Fix: the governing thought should imply the ask. If it doesn't, restate the ask explicitly at the end of the TL;DR.

## Pyramid Sketch Format (for Step 2 of pm-strategy-memo)

```
GOVERNING THOUGHT:
[One sentence. SVO. No ands. Specific. Directional.]

ARGUMENT 1: [complete-sentence claim]
  - Evidence: [source-anchored fact]
  - Evidence: [source-anchored fact]

ARGUMENT 2: [complete-sentence claim]
  - Evidence: ...
  - Evidence: ...

ARGUMENT 3: [complete-sentence claim]
  - Evidence: ...
  - Evidence: ...

(Cap at 5 arguments. 3 is typically tighter.)

CHECKS:
- [ ] Governing thought passes the 5 tests
- [ ] Arguments are claims, not topics
- [ ] Arguments are MECE (no overlap, no glaring omission)
- [ ] One ordering scheme applied consistently
- [ ] Reader who reads only governing thought + arguments gets the argument
```

Confirm this sketch with the user before drafting prose. Editing the pyramid is cheap. Editing prose around a broken pyramid is expensive.

## Further Reading

- Barbara Minto — *The Pyramid Principle*
- McKinsey internal communications style guide (publicly leaked excerpts available)
- Amazon 6-pager guide (related but more structured — see `pm-pr-faq` skill references for that)
