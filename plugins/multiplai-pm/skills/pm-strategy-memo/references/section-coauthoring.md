# Section Coauthoring — Brainstorm / Curate / Draft

## Origin

Adapted from the `doc-coauthoring` skill in the `anthropics/skills` repo. The pattern: don't draft prose until the writer has seen multiple angles and chosen one. It treats the section as a small product with a discovery phase before a build phase.

## Why This Beats Straight-to-Draft

Default behavior when given "write Section 3" is to draft. The draft is usually competent but often wrong-shaped — it argues a point in a way the writer wouldn't have chosen if they'd seen alternatives. Then editing the draft is expensive: the prose is committed, the writer is anchored, and tweaking around the wrong shape produces worse output than starting over.

Brainstorm-then-curate inverts this. The cost is one extra exchange. The gain is a section that matches the writer's intent on draft 1.

## The Pattern (per section)

### Step 1 — Brainstorm 5-10 angles

For the section the pyramid sketch calls for, generate 5-10 distinct ways to make the point. Vary on:

- **Hook** — anecdote, statistic, customer quote, competitor comparison, historical analogy, direct claim
- **Evidence ordering** — strongest first / weakest first / chronological / by impact
- **Tone** — confident-and-flat, measured-and-careful, urgent, contrarian
- **Frame** — "we should X" / "the alternative to X is worse" / "X is the only choice consistent with [shared value]"

Present the angles compactly:

```
Angle 1: Lead with the missed-quarter anecdote, then generalize to the pattern. Tone: measured.
Angle 2: Open with the counter-position ("the obvious move is X — here's why it's wrong"). Tone: contrarian.
Angle 3: Cite the 3 strongest data points up front, anecdotes as colour. Tone: confident.
Angle 4: Start with what stays the same, then what changes. Tone: reassuring.
Angle 5: Open with a customer quote that frames the problem. Tone: empathic.
...
```

Don't draft the angle. Just describe its shape. Drafting comes after curation.

### Step 2 — Surface to user, let them curate

Present the angles. Ask the user to:
1. **Pick 1, occasionally 2.** Most sections work best with one angle.
2. **Reject the others, briefly.** Not for editorial review — for *direction*. "Angle 2 is too aggressive for this audience" is signal you need.
3. **Add what's missing.** Often the user's reaction surfaces an angle you didn't generate.

If the user picks 2 angles, ask: "Are these complementary (cover different parts of the section) or competing (both arguing the same point)? If competing, pick one." Two angles in one section reads as drift unless they cover distinct sub-points.

### Step 3 — Draft the section

Now draft, in the chosen angle, at the chosen length. Tight prose. Lead with claim, follow with evidence. No filler.

Inside the draft, weave in the **anticipated objection** for this argument (from Step 1 of the parent workflow). Don't dump it; either pre-empt it ("a reader might worry that — but...") or fold it into the evidence chain.

### Step 4 — Review the draft

Before moving to the next section, check:
- Does the section lead with the claim?
- Is the evidence source-anchored?
- Did you sneak in any unsourced assertion?
- Does the closing line connect to the next section?

Fix anything that's off before moving on. Sections that need rework after the whole draft is done are 3x more expensive to fix.

## When to Compress the Pattern

This is the full pattern. For short memos (< 600 words total) or sections with obvious framing (e.g. a routine "What I Need From You" section), collapse:

- **Skip brainstorm if there's only one obvious angle.** Most "What I Need From You" sections, decision-log entries, and routine "Background" sections fall here.
- **Combine brainstorm + curate** for short sections — present 3 angles inline and ask the user to pick one in the same message.

Always run the full pattern for: the TL;DR, the strongest argument section, and any section addressing the most-anticipated objection. These are the highest-leverage sections.

## Common Failure Modes

### Brainstorm without divergence

Symptom: the 5 angles are all variations on the same shape.
Fix: deliberately push to angles you initially dismissed. "What if we opened with the counter-position?" "What if we led with the cost, not the benefit?" The point of brainstorm is to surface options the writer hadn't considered, not to pad the choice set.

### Curating mid-draft

Symptom: drafting begins before the user has chosen an angle.
Fix: stop. Present the angles. Wait. The draft is fast once the angle is locked; doing it backwards just creates rework.

### Section drift

Symptom: section starts in chosen angle, ends in a different one.
Fix: re-read the section out loud. Where does the tone shift? Usually around the evidence-to-implication transition. Rewrite that transition.
