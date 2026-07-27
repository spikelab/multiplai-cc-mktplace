# multiplai-pm

Product-management skill pack for Claude Code: **JTBD synthesis, persona
codification, PR/FAQ, strategy memos, job applications, and landing pages**.
Part of the [`multiplai`](../../README.md) marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-pm@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `pm-jtbd-synthesis` | Synthesize Jobs-to-be-Done from customer interview transcripts — Forces of Progress with verbatim quote attribution, job clusters, job stories, Opportunity-Solution Tree stub. |
| `pm-persona-codifier` | Codify customer/user personas into canonical, source-attributed persona docs — one file per persona plus an INDEX. |
| `pm-pr-faq` | Draft Amazon-style Working Backwards documents — fictional press release + internal FAQ, with an adversarial FAQ generator and stress-test pass. |
| `pm-strategy-memo` | Leadership-grade strategy memos using Minto Pyramid, with a Working Backwards stress-test and a fresh-Claude reader-test. |
| `job-application` | Draft tailored resumes and cover letters, then generate PDF applications. |
| `landing-page` | Landing-page copy: create from scratch, audit an existing page (CRO review), or iterate copy variations for specific sections. |

## Composition

The discovery skills form a chain, each output the next skill's strongest input:

```
transcribe (multiplai-media) → pm-jtbd-synthesis → pm-persona-codifier → pm-pr-faq
```

- `pm-strategy-memo` composes downstream of `interviewer` / `extract-insights`
  (multiplai-research) and of `pm-jtbd-synthesis` / `pm-persona-codifier` when
  the memo references discovery evidence; `pm-pr-faq` composes downstream of
  `pm-strategy-memo` when strategy implies a launch.
- `landing-page` uses `interviewer` (multiplai-research) for discovery when
  available.

## Compatibility

All skills run on vanilla Claude Code, any OS. Personal memory files are
optional — skills ask for source material when absent. See the
[compatibility matrix](../../README.md#compatibility-matrix).
