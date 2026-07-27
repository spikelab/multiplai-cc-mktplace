# multiplai-research

Research skill pack for Claude Code: **code-driven deep-research pipeline,
insight extraction, and structured interviewing**. Part of the
[`multiplai`](../../README.md) marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-research@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `deep-research` | Web research via a code-driven pipeline (plan, diverge, challenge, search, triage, read, reassess, synthesize). Asks clarifying questions first; outputs at three detail levels (gist, structured, detailed). |
| `extract-insights` | Decompose long-form content into core thesis, argument chain, key claims with evidence, and non-obvious implications — extraction, not summarization. |
| `interviewer` | Structured questioning that surfaces assumptions, requirements, and facts that wouldn't come out otherwise — product discovery, technical design, decisions. |

## Composition

- `deep-research` powers the research phase of `buildme` (multiplai-dev).
- `interviewer` and `extract-insights` compose upstream of the multiplai-pm
  skills — e.g. `pm-strategy-memo` uses `interviewer` for context-gathering and
  `extract-insights` to turn a transcript into a brief.

## Compatibility

All skills run on vanilla Claude Code, any OS. `deep-research` is zero-config
via the Agent SDK; optional search-provider keys (Brave, Tavily, Exa, Serper,
You.com) widen coverage.

`deep-research` and `extract-insights` ingest externally-authored text — it
arrives fenced as data, never instructions; see the
[untrusted-content contract](../../docs/untrusted-content.md).
