# PR-FAQ Stress-Test — Specifics

A condensed stress-test for PR-FAQ specifically. For the general working-backwards stress-test pattern applied to strategy memos, see the `pm-strategy-memo` skill's `references/working-backwards-stress-test.md` — it has the deeper canon. This file covers the PR-FAQ-specific failure modes.

## Five PR-FAQ-Specific Tests

### Test 1 — The Summary-Only Test

Cover everything except the summary paragraph. Read just the summary.

Question: **Does a reader who only reads the summary understand what the customer gets?**

PASS markers:
- Customer outcome is named with a concrete verb of change.
- The persona is identifiable from the language.
- The summary stands alone — no "see below" dependencies.

FAIL markers:
- Summary describes the technology, not the outcome.
- Persona is abstract ("teams," "businesses," "users").
- You have to read the problem paragraph to know what the announcement is.

If FAIL: rewrite the summary paragraph. The headline + subhead + summary together should be the elevator pitch.

### Test 2 — The Persona Recognition Test

Hand the PR to a sales rep (real or imagined). Ask: "Which of your accounts maps to the customer in this PR?"

PASS marker:
- The rep can name 2-3 specific accounts within 30 seconds.

FAIL marker:
- The rep says "uh, lots of them, I guess" or names accounts that contradict each other.

If FAIL: the persona is too broad. Tighten the subhead and the customer quote to constrain to one persona. If the initiative serves multiple personas, write multiple PRs.

### Test 3 — The Tradeoff Admission Test

Scan the FAQ. List every tradeoff explicitly admitted.

PASS markers:
- At least 2-3 tradeoffs are named (something cut, deferred, narrowed, or not built).
- Each tradeoff includes a reason ("we deferred X because Y").
- The tradeoffs are non-trivial — not "we deferred a feature name change" but "we deferred multi-step approval workflows, which covers ~30% of our base."

FAIL markers:
- Zero tradeoffs admitted.
- Tradeoffs are face-saving ("we deferred a minor edge case").
- Tradeoff questions are answered with marketing language.

If FAIL: a PR-FAQ with zero admitted tradeoffs is dishonest by omission. Force yourself to name what was cut.

### Test 4 — The Riskiest Assumption Test

Find the FAQ question that explicitly names the riskiest assumption. Read the answer.

PASS markers:
- A single, specific assumption is named (not a list).
- The assumption is falsifiable — there's a way to know if it's wrong.
- The answer includes a mitigation ("if this is wrong, we'll do X").

FAIL markers:
- No riskiest-assumption question exists in the FAQ.
- The "riskiest assumption" is a generic risk ("adoption could be slow") rather than a specific bet.
- No mitigation is named.

If FAIL: add the question and the honest answer. The riskiest-assumption question is the most diagnostic FAQ entry — its absence is a tell that the team hasn't thought hard enough.

### Test 5 — The Competitor Rebuttal Test

Imagine a competitor reads this PR and publishes a "Why [Company]'s [Launch] Is Wrong" blog post the same day. What would they attack?

PASS markers:
- The attack would have to engage with specific claims (forcing the competitor to argue substance, not slogans).
- The PR's claims are defensible — backed by data, real customer evidence, or honest scoping.
- The competitor's strongest attack is already addressed in the FAQ.

FAIL markers:
- The attack would be "they don't actually solve this — here's why [obvious counter]" and the counter is something the FAQ doesn't address.
- The claims are overstated, allowing the competitor to find weak spots with little effort.
- The persona claim is so broad that competitors can claim "we serve them too" credibly.

If FAIL: tighten claims. Add the unaddressed counter to the FAQ.

## Scoring

Run all five tests. Tally PASS / FAIL.

| Score | Diagnosis |
|-------|-----------|
| 5/5 PASS | Ship-ready. The PR-FAQ is doing its job. |
| 4/5 PASS | Revise the one FAIL before shipping. |
| 3/5 PASS | The PR-FAQ has structural issues. Revise at least one structural test (persona, tradeoffs, riskiest assumption) before re-running. |
| ≤2/5 PASS | The underlying initiative may not be ready for a PR-FAQ. Surface this to the user. Consider going back to discovery / strategy-memo phase. |

## Stress-Test Grid Format

Place at the bottom of the output file under "Method Notes":

```markdown
### Stress-Test Grid

| # | Test | Result | Notes |
|---|------|--------|-------|
| 1 | Summary-only test | PASS / FAIL | [if FAIL: what's missing] |
| 2 | Persona recognition test | PASS / FAIL | [if PASS: which accounts; if FAIL: where it fails] |
| 3 | Tradeoff admission test | PASS / FAIL | tradeoffs admitted: [list] |
| 4 | Riskiest assumption test | PASS / FAIL | assumption: [single sentence] |
| 5 | Competitor rebuttal test | PASS / FAIL | strongest attack: [single sentence] |

**Score:** N/5 PASS

**Recommended revisions before final:**
- [list]
```

Surface the grid to the user along with the final doc — not buried in method notes only.

## What This Test Misses

This stress-test is shape-focused. It does NOT test:
- Whether the underlying market actually wants this (that's discovery, not PR-FAQ)
- Whether the engineering work is feasible (that's the technical-spike, not PR-FAQ)
- Whether the business model works (that's financial modeling, not PR-FAQ)

If any of those are in doubt, the PR-FAQ can pass all 5 tests and the initiative can still fail. Use the PR-FAQ as a *clarity test*, not a *viability test*.
