# Opportunity-Solution Tree (OST) — Canon

## Origin

The Opportunity-Solution Tree is Teresa Torres's framework from *Continuous Discovery Habits* (2021). It's a visual scaffold for product discovery that connects four layers:

```
              [ DESIRED OUTCOME ]            ← business outcome you're driving
                     │
        ┌────────────┼────────────┐
        │            │            │
   [Opportunity] [Opportunity] [Opportunity]   ← customer needs / pains / desires
        │            │            │
    ┌───┼───┐    ┌───┼───┐    ┌───┼───┐
    [S] [S] [S]  [S] [S] [S]  [S] [S] [S]      ← candidate solutions
       │   │       │   │       │   │
      [E] [E]     [E] [E]     [E] [E]          ← experiments / assumption tests
```

The tree forces structural choices: *which* opportunity to target, *which* solution to bet on, *which* assumption to test first. Without the tree, teams jump straight from outcome to solution and skip the opportunity layer — which is where most product decisions actually go wrong.

## The Four Layers

### 1. Desired Outcome

A single, measurable business outcome (not an output). One per tree. If you have multiple outcomes, build multiple trees.

**Good:** "Increase activation rate of new signups from 22% to 35% by Q3"
**Bad:** "Improve onboarding" (not measurable, not outcome — it's a project)

### 2. Opportunities

Customer needs, pains, or desires that, if addressed, would move the outcome. Sourced from research — including JTBD synthesis. Each opportunity is stated **in the customer's voice**, not the team's.

**Good:** "New users can't tell whether they've completed the setup correctly"
**Bad:** "Improve setup confirmation UX" (team voice, solution-flavored)

Opportunities are **not** problems-to-solve in the engineering sense. They're *spaces of possibility* — each opportunity could be addressed by many solutions. Avoid prematurely narrowing.

### 3. Solutions

Concrete candidate ideas for addressing an opportunity. **Multiple per opportunity.** This is the most-skipped step: teams pick the first solution that comes to mind. The tree forces you to enumerate alternatives so the chosen solution is a real choice, not a default.

**Rule:** at least 3 candidate solutions per opportunity. Generate broadly, then narrow.

### 4. Experiments / Assumption Tests

For each solution under consideration: what's the riskiest assumption? What's a cheap test? An experiment is not "build it and see"; it's a falsifiable check on an assumption (usability test, fake door, smoke test, prototype with N users, etc.).

## OST Stub Format (for pm-jtbd-synthesis file 3)

A stub is intentionally incomplete. It seeds the tree from JTBD synthesis output. The product team fills in solutions and experiments. **The job of pm-jtbd-synthesis is to produce a credible outcome + opportunity layer, with placeholders for the lower two layers.**

```markdown
# OST Stub — [topic / customer segment]

**Date:** YYYY-MM-DD
**Derived from:** `INBOX/jtbd-synthesis-YYYY-MM-DD.md`
**Status:** Stub — solutions and experiments TBD by product team.

## Desired Outcome (proposed)

> [One sentence, measurable, time-bounded. Phrased as a hypothesis if the team hasn't committed yet: "If we address the top opportunities below, we expect [outcome]."]

**Source of outcome proposal:** [team-stated / inferred from job cluster forces / TBD — flag honestly]

## Opportunities

Derived from the top job clusters in the parent synthesis. Cap at 5. Stated in customer voice.

### Opportunity 1 — [Short label]

> "[customer-voice statement, ideally pulled from a verbatim quote or paraphrased close to the customer's language]"

- **Source job(s):** [Job #N from synthesis]
- **Evidence:** [N transcripts; STRONG/SUPPORTED]
- **Forces snapshot:**
  - Push: ...
  - Pull: ...
  - Anxiety: ...
  - Habit: ...
- **Anchor quote:**
  > "..." — `transcript.txt L#-#`
- **Candidate solutions (TBD):** _[ to be filled by product team ]_
- **Riskiest assumption to test (suggested):** [one sentence — usually whether the opportunity is real for enough customers, or whether the desired outcome would actually move if it's addressed]

(Repeat per opportunity, cap 5.)

## Opportunities NOT Promoted (and why)

Job clusters from the synthesis that did NOT make the OST stub, with the reason. This forces discipline.

- **[Job]** — not promoted because [single-source / low confidence / out of scope for current outcome / better addressed by adjacent team / etc.]

## Open Questions

What discovery would have to find before this tree is buildable. 3–5 questions max.

1. [Question]
2. ...
```

## Stub Discipline

1. **Don't fill in solutions.** The temptation is huge. Don't. The product team owns that layer. The synthesis skill provides outcomes + opportunities + evidence. Solutions are downstream.

2. **Opportunities ≠ jobs verbatim.** Jobs are progress the customer wants to make ("I want to know my forecast is accurate"). Opportunities are *spaces where the team could intervene* to enable that progress ("Forecast accuracy signals aren't visible early enough"). Translate, don't copy.

3. **Cap opportunities at 5.** More than 5 means the synthesis didn't cluster hard enough or the outcome is too broad.

4. **Outcome is proposed, not declared.** The synthesis skill doesn't have authority to set the team's outcome. Phrase it as a proposal with the team's stated outcome (if known) or as a hypothesis to be confirmed.

5. **Forces snapshot under each opportunity.** This is what makes the tree usable — when the team sits down to brainstorm solutions, they need to see what's pushing the customer, what's pulling them, what's holding them back, and what they're afraid of. Solutions that don't address those forces won't work.

## Common Failure Modes

- **Outcome too broad.** "Grow revenue" can't be a tree's outcome. Tighten until it's measurable, time-bounded, and bounded to a segment.
- **Opportunities phrased as solutions.** "Add onboarding checklist" is a solution. The opportunity behind it is "New users can't tell where they are in setup."
- **One opportunity per cluster.** If 5 job clusters produce 5 opportunities with no overlap, you're treating the synthesis output as the answer. The tree should compress further — sometimes 5 clusters collapse to 2 opportunities at the right altitude.
- **Premature solutioning.** Once a tree has solutions in it, it's no longer a stub — it's a plan. Stubs are deliberately incomplete to keep the team's optionality open.

## Further Reading

- Teresa Torres — *Continuous Discovery Habits* (2021)
- Teresa Torres — Product Talk blog (producttalk.org)
