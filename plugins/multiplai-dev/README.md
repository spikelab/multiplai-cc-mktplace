# multiplai-dev

Developer skill pack for Claude Code: **spec-driven builds, executable plan
authoring, code/security review, refactoring, walkthroughs, e2e tests, cloud
ops, and skill authoring**. Part of the [`multiplai`](../../README.md)
marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-dev@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `buildme` | Full bootstrap conductor — from idea to working code via interview, research, spec generation, and autonomous TDD implementation (deterministic Python pipeline). |
| `plan` | Author self-contained, executable implementation plans with verifiable "Done means" criteria — hand the file to a fresh session, a goal runner, or buildme. |
| `deepen` | Find deepening opportunities — collapse shallow modules into deep ones; idiom packs for Python, Swift, TypeScript, React. |
| `codebase-walkthrough` | Interactive walkthrough of any codebase — Markdown doc plus self-contained HTML with step-through navigation. |
| `learn-stack` | Generate an interactive framework learning guide from any codebase. |
| `e2e-test` | End-to-end testing for web apps — frontend (browser-based) and backend (API) modes. |
| `think` | Critical-thinking toolkit — audit for assumptions, biases, premature convergence; quick, focused, and deep modes. |
| `devops-gcp` | Working operator's knowledge of GCP — Cloud Run, Cloud SQL, IAM, Terraform, logging/monitoring. |
| `swift-build` | Build, test, and manage iOS/macOS projects from any environment; handles the container→host SSH bridge. |
| `skill-creator` | Guide for creating (or updating) effective skills. |
| `propose-skill` | Analyze session patterns and propose new skills for recurring workflows. |
| `analyze-context-router` | Analyze memory-retrieval logs for routing accuracy, false negatives, and token efficiency. |

## Composition

- `buildme` composes with `deep-research` (multiplai-research) for its research
  phase — use `--skip-research` if that plugin isn't installed.
- `plan` output is designed to be fed to `buildme` or a fresh session unchanged.
- `analyze-context-router` and `propose-skill` operate on `multiplai-context` —
  install it first.

## Compatibility

Runs on vanilla Claude Code, any OS, with these exceptions:

- `swift-build` — macOS only (Swift/Xcode toolchain); from the multiplai-kit
  container it needs the container→host SSH bridge.
- `devops-gcp` — the knowledge pack works anywhere; real operations need your
  `gcloud` auth.
- `e2e-test` — frontend mode needs `agent-browser` (npm); backend mode is plain HTTP.
- `buildme` — needs `uv` + network.

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
