# JTBD vs. Personas — When to Use Which

## Short Answer

**Jobs come first.** Personas come second. If you have a choice, do JTBD synthesis before persona codification. Personas built on top of jobs are durable; personas built from intuition or marketing segmentation rot fast.

## Why Jobs First

The JTBD framework was, in part, a reaction to overuse of personas in product. Christensen's argument: personas describe *who* the customer is (demographics, role, attributes), but products are hired to do *jobs* (situations + motivations + outcomes). The same person hires different products for different jobs in different situations. Personas anchored on demographics fossilize the wrong abstraction.

Job stories deliberately leave personas out for this reason ("When [situation]... I want to..." rather than "As a [persona], I want to...").

But personas haven't gone away — because in B2B especially, **who is buying and who is using matters**, and product teams still need a shared mental model of the human at the other end of the screen.

## The Reconciliation

A persona, done right, is **a stable bundle of jobs + decision context + emotional posture** that a real human exhibits, given a recognizable situation. It's the *job-bearer*, not the job.

The job is "I want to know whether my team's onboarding actually worked."
The persona is "The Plate-Spinner Manager" — the human who hires that job *along with 4 others*, in a recognizable work context.

Personas earn their keep when they let you:
1. **Bundle multiple jobs together.** A real human has multiple co-occurring jobs. Personas model that.
2. **Carry decision context.** Buying power, stakeholder politics, budget authority — these don't live in jobs.
3. **Capture emotional posture.** What's their default anxiety? Their tolerance for risk? Their relationship to the existing way of doing things?

## Mapping Jobs to Personas

A persona is **not** "one job per persona." That's a smell. The mapping is usually:

- **Multi-to-one (most common):** one persona hires multiple jobs. The Flow Builder hires "keep workflows operable for downstream consumers" AND "translate stakeholder asks into technical specs" AND "avoid being blamed when things break."
- **One-to-multi (less common but real):** one job is hired by multiple personas, each for different reasons. The job "make our forecast more credible" is hired by The Trust-First Buyer (so they can defend the number) and by The Embedded Operator (so they don't get questioned in standup). Different personas, same job, different motivations.
- **One-to-one (rare and suspicious):** one persona, one job. If this happens, either the persona is too narrow (it's actually just a job-bearer) or the job is too broad (it should be decomposed).

## Order of Operations

1. **Discovery interviews** → transcripts
2. **`pm-jtbd-synthesis`** → forces, job clusters, OST stub
3. **`pm-persona-codifier`** → personas, each tagged with which job clusters they hire
4. **Strategy / PRD / roadmap docs** reference personas by ID

If step 2 is skipped, step 3 is still possible but the personas will be shakier. Flag in Open Questions: "These personas were built without prior JTBD synthesis — re-confirm against interview evidence."

## When Personas Are the Wrong Frame

Some product decisions are better made on pure JTBD without bringing personas in:

- **Cross-persona features** (e.g. notifications, search, navigation) — the job is what matters, the persona is incidental
- **B2C products with very broad audiences** — the demographic noise overwhelms the signal, and personas become marketing artifacts rather than product artifacts
- **Discovery phase** when you don't yet know who the customer is — leading with personas pre-commits to assumptions the data hasn't earned

When personas ARE the right frame:

- **B2B** with distinct buyer / user / blocker roles
- **Stakeholder-rich decisions** (multi-decider purchases)
- **Sales enablement** (sales teams need persona language to communicate)
- **Org alignment** (when product, sales, marketing need a shared vocabulary)

## Honest Output

If the user asks for personas but the underlying data doesn't support meaningful clustering, say so:

> "Three transcripts is not enough to draw stable personas. I've drafted hypotheses but each is under-evidenced. Recommend 5-10 more interviews across the segments before locking these in."

Better to ship a thin, honest output with flagged uncertainty than a confident-sounding but fabricated set of personas.
