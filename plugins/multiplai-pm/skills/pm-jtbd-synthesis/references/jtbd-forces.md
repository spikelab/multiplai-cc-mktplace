# Forces of Progress — Canon and Extraction Guide

## Origin

The Forces of Progress framework comes from Bob Moesta and Chris Spiek's work on Jobs-to-be-Done, building on Clayton Christensen's JTBD theory. It models the moment of change as the outcome of four competing forces acting on the customer.

The frame: a customer doesn't buy a product, they "hire" it to make progress in their life. Whether they switch from the old hire to the new one depends on the balance of four forces.

## The Four Forces

```
                  PULL (toward new)
                       │
                       ▼
   PUSH ──────►  [ Switch ]  ◄────── HABIT (of the old)
   (from old)         ▲
                       │
                  ANXIETY (about new)
```

**Push** and **Pull** drive switching. **Anxiety** and **Habit** resist it. Switching only happens when push + pull exceed anxiety + habit.

### 1. Push of the situation

What's broken, painful, frustrating, or no-longer-good-enough about the current way. The dissatisfaction that drives the customer to look for an alternative.

**Surface markers in interview transcripts:**
- Past-tense complaint language: "It was driving me crazy that...", "We kept running into..."
- Specific incidents: "Last Tuesday I spent six hours...", "We missed the quarter because..."
- Cost language: time wasted, money lost, opportunities missed
- Emotional friction: "embarrassing," "frustrating," "humiliating"

**Distinguishing from generic complaints:** Push is *contextual and concrete*. "It's slow" is weak. "It's slow enough that I gave up trying to use it during the morning standup" is push — there's a moment, a stake, a cost.

**Common trap:** Confusing dissatisfaction-in-general with dissatisfaction-driving-change. Customers complain about lots of things they'll never switch to address. Push requires evidence the dissatisfaction is *causing them to look*.

### 2. Pull of the new solution

What's appealing about the new way, the imagined better state. Often a customer has a *vision* of a future where they don't have this problem.

**Surface markers:**
- Future-tense or hypothetical language: "If I could just...", "Imagine if it worked like..."
- Aspirational comparisons: "Like how X handles it...", "Why can't it be like..."
- Specific outcomes desired: "I want to be able to walk into the meeting and know..."

**Distinguishing from feature requests:** Pull is about the *outcome*, not the mechanism. "I want a Kanban board" is a feature request. "I want to walk into Monday standup and instantly know what's stuck" is pull — and a Kanban board may or may not be the right solution.

**Common trap:** Inflating polite interest into pull. The customer being willing to listen to a pitch is not pull. Pull requires evidence of *wanting* the outcome unprompted.

### 3. Anxiety of the new solution

What worries them about switching. The cognitive friction of the unknown, the fear of regret, the risk of looking bad.

**Surface markers:**
- "I'm worried about...", "What if it doesn't..."
- Comparisons to past bad switches: "Last time we changed tools we lost..."
- Stakeholder worries: "I don't know how my team would react"
- Reputational risk: "If this fails, I'm the one who picked it"
- Switching costs that aren't financial: data migration, retraining, political capital

**Distinguishing from objections:** An objection is a stated reason for not buying ("too expensive"). Anxiety is often unstated — it's the worry behind the objection. "Too expensive" sometimes means "I'm worried I can't justify it to my boss," which is anxiety, not price sensitivity.

**Common trap:** Mistaking absence of stated anxiety for absence of anxiety. Customers under-report anxiety because admitting fear is socially awkward. Look for hedges, hesitations, "I guess we could..."

### 4. Habit of the present

The inertia of the current way. Not just "we're used to it" but the entire ecosystem of muscle memory, integrations, sunk-cost identity, and "good enough"-ness that holds the customer in place.

**Surface markers:**
- "It works fine," "It does the job," "We make it work"
- "We've always done it this way"
- Workarounds that have become invisible: "Oh, I just use a spreadsheet for that part"
- Implicit endorsements: customer can't articulate what they *like* but also can't articulate any reason to leave

**Distinguishing from satisfaction:** Habit is not satisfaction. A customer can be unhappy AND still be held in place by habit. The test: ask "what would have to be true for you to seriously evaluate switching?" If the answer is implausible or vague, habit is strong.

**Common trap:** Underestimating habit. Habit is the most often-missed force because it's invisible to the customer themselves. The customer who says "I'd switch in a heartbeat" but hasn't switched in 5 years is held by habit.

## Extraction Heuristics

### Listen for the *moment*

Push, pull, and switching triggers usually attach to a specific moment in time. "When I realized..." "The day that..." "Once we hit..." Quote the moment verbatim. Moments are more credible signal than generalizations.

### Separate forces from features

When the customer talks about features (ours or competitors'), ask: what *job* is that feature standing in for? Map back to forces. A customer saying "I love feature X" is reporting a pull, not a feature request. Translate it.

### Cross-check forces

Strong jobs have all four forces in play. If you have strong push and pull but no anxiety and no habit, you may be hearing performative enthusiasm rather than a real switching story. If anxiety is absent, the customer hasn't seriously considered switching yet.

### Speaker discipline (dialog)

Interviewer leading questions can manufacture fake forces. "It sounds like you're frustrated with how slow it is — is that right?" followed by "Yes" is a manufactured push, not a real one. Only count a force when the customer surfaces it without leading.

## Confidence Calibration

| Confidence | Bar |
|------------|-----|
| STRONG | Customer states the force unprompted, in their own words, attached to a specific moment or incident, with affective signal (emotion, urgency). Or: 2+ customers independently state the same force. |
| SUPPORTED | Customer states the force unprompted but generically, without a moment or affect. Or: stated once with moment but in only one transcript. |
| WEAK | Force is hedged ("kind of," "a little") or surfaced only in response to a leading question. Or: inferred from context, not stated. |

When in doubt, downgrade. Inflated confidence in synthesis reports is the #1 reason teams ship the wrong thing.

## Worked Example

Customer (B2B SaaS buyer) says:

> "Last quarter we missed our number by like 8% and the board meeting was brutal. I was sitting there trying to explain why the forecast was off and I didn't have a good answer. (L142–145) I keep thinking, if I'd known two weeks earlier, we could have done something. (L147) I've looked at a couple of forecasting tools but honestly Salesforce reports kind of work, they're just ugly. (L155) The thing is, even if we picked a new tool, the data migration would take six months and I don't have six months." (L160-163)

Extract:

- **Push** STRONG: missed-the-number moment + board meeting humiliation — `L142-145`
- **Pull** SUPPORTED: "if I'd known two weeks earlier" — wants earlier signal — `L147`
- **Habit** STRONG: "Salesforce reports kind of work" — held by good-enough-ness — `L155`
- **Anxiety** STRONG: six-month migration cost, no time budget — `L160-163`

All four forces in play → strong job candidate. Hedges preserved ("kind of work," "honestly"). Moments anchored. Confidence calibrated.
