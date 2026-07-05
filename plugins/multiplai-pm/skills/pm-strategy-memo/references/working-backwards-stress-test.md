# Working Backwards Stress-Test — Adversarial Pre-Flight

## Origin

The Working Backwards process at Amazon (canonized by Colin Bryar and Bill Carr in *Working Backwards*, 2021) treats every meaningful product or strategy document as an artifact that must survive **adversarial reading** before it ships. The 6-pager review process embeds this: meetings open with silent reading, then attendees attack the doc.

The stress-test below is an internalization of that posture. The skill plays the role of the adversarial reader *before* the actual reader does.

## The Posture

You are NOT helping the writer succeed. You are an exec who has read a thousand strategy memos, most of them bad, and you are looking for reasons to dismiss this one. You are time-constrained, impatient, and pattern-matching against past failures.

**This is uncomfortable on purpose.** A friendly stress-test produces no signal. The point is to surface what a hostile reader would surface, *before* they do.

## The 10-Hardest-Questions Method

After the memo draft is complete:

1. **Generate 10 questions a skeptical exec would ask.** Not "good questions" or "clarifying questions." The hardest, most-likely-to-derail questions.
2. **Locate where the memo addresses each.** Quote the addressing passage.
3. **Grade each question:** ANSWERED / PARTIAL / NOT ADDRESSED.
4. **Decide what to do about gaps.** Three options: revise the memo to address, add to Objections section explicitly, or accept as a known limitation and note in Open Questions.

### Where to find the 10 questions

Pull from these categories. You should land 1-2 questions from each:

**Evidence challenges**
- "Where's the data behind [load-bearing claim]?"
- "This is based on N interviews — what about the rest of the population?"
- "How do you know this isn't survivorship bias / anchoring on loud customers / recency?"

**Alternatives**
- "Why this option and not [X]? Did you consider [X]?"
- "What's the do-nothing option? What happens if we ignore this?"
- "Has [competitor] tried this? What happened?"

**Cost / risk**
- "What does this cost us — money, time, opportunity?"
- "What's the worst case if we're wrong?"
- "What are we giving up by saying yes to this?"

**Execution**
- "Who actually does this work? Do they agree?"
- "What's the first 30 days look like?"
- "How will we know if it's working?"

**Reversal**
- "What would make you change your mind?"
- "What are you assuming that, if false, breaks the whole argument?"
- "What's the strongest argument against this position?"

### The Grade

| Grade | Bar |
|-------|-----|
| ANSWERED | The memo directly addresses the question; quote the passage. |
| PARTIAL | The memo touches the topic but the answer is weak, evasive, or implicit. |
| NOT ADDRESSED | No passage in the memo answers this. |

Aim for: 8+ ANSWERED, 0 NOT ADDRESSED on critical questions.

If you can't get to 8+ ANSWERED, the memo isn't ready. Either revise to address the gaps, or be honest in the Objections section about what the memo doesn't answer.

## Stress-Test Grid Format

Present the stress-test to the user in this format before reader-test:

```markdown
## Stress-Test Grid

| # | Question | Grade | Where addressed | Action |
|---|----------|-------|----------------|--------|
| 1 | "How do we know customers want X and not Y?" | ANSWERED | TL;DR + Section 2 (cites JTBD synthesis cluster 3) | none |
| 2 | "What does Attento cost in engineering time?" | PARTIAL | Section 4 mentions "significant cost" but no number | revise: add estimate |
| 3 | "Why three months and not six?" | NOT ADDRESSED | — | add to Objections |
| 4 | ... | ... | ... | ... |

**Score:** N ANSWERED / N PARTIAL / N NOT ADDRESSED

**Recommended actions:**
- [list of revisions before reader-test]
```

## What the Stress-Test Catches That Editing Doesn't

- **The unspoken assumption.** Editing makes prose tighter; the stress-test surfaces the assumption underneath the prose that the writer never realized was assumed.
- **The missing alternative.** Most memos argue *for* a choice without explaining why the alternatives lose. The stress-test forces this.
- **The unowned objection.** Writers naturally hide weaknesses. The adversarial frame forces them onto the page.
- **The fragile evidence base.** Memos that rest on a single anecdote or unverified claim feel solid until cross-examined. The grid exposes them.

## Common Failure Modes

### Friendly stress-test

Symptom: most questions are clarifying or curious rather than hostile.
Fix: re-read the categories above. Force yourself into the skeptic role. Imagine a board member who's been burned before by overconfident strategy decks.

### Generic stress-test

Symptom: questions are generic ("what's the ROI?") rather than memo-specific.
Fix: questions should reference specific claims in the memo. "What's the ROI?" is generic. "The memo claims observability will drive trust — what evidence do we have that trust is the actual lever vs. one of several?" is specific.

### Sycophantic grades

Symptom: most questions graded ANSWERED. Memo passes stress-test on first try.
Fix: a first-draft memo passing stress-test on its first run is a stress-test that wasn't honest. Raise the bar.

### Stress-test buried

Symptom: stress-test grid lives in a private notes file the user never sees.
Fix: surface the grid in the conversation BEFORE the reader-test. Two reasons: (a) the user may know answers the skill doesn't, (b) they should see the gaps before the memo ships, not after.

## Relation to Working Backwards (PR-FAQ)

The `pm-pr-faq` skill applies a similar adversarial pass, but to a *future-state* document (press release + internal FAQ). The stress-test here is for a *present-state* strategy memo. Different artifacts, same posture. If a project goes through both (strategy memo first, PR-FAQ for the launch), the stress-tests will pick up different things — the strategy memo's stress-test focuses on "is this the right direction," the PR-FAQ's on "would this launch land."

## Further Reading

- Colin Bryar & Bill Carr — *Working Backwards*
- Jeff Bezos shareholder letters (2017 onward — the "six-pager" memo culture)
