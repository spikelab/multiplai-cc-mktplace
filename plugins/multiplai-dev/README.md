# multiplai-dev

Developer skill pack for Claude Code: **spec-driven builds, executable plan
authoring, refactoring, walkthroughs, e2e tests, cloud ops, and skill
authoring**. Part of the [`multiplai`](../../README.md) marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-dev@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `buildme` | Builds a change from the specs `/multiplai-pm:pm-spec` marked ready — research, design, tasks, audits and autonomous TDD implementation (deterministic Python pipeline), ending in a PR. |
| `plan` | Author self-contained, executable implementation plans with verifiable "Done means" criteria — hand the file to a fresh session, a goal runner, or `/multiplai-pm:pm-spec` (as its input) before buildme. |
| `deepen` | Find deepening opportunities — collapse shallow modules into deep ones; idiom packs for Python, Swift, TypeScript, React. |
| `review` | Code review as a pipeline of separate agents (find, verify, merge duplicates) with gates in code that reject any finding whose cited lines are not at the reviewed commit; each finding says what correct behaviour would be, and no fix is proposed; writes a markdown review, a short summary it pastes into chat, rollups and a `findings.json` (under `.multiplai/data/reviews/<repo>/<object>/`, such as `multiplai-cc-mktplace/pr-283/`, every round kept, every run in `ledger.jsonl`), then opens it in `review-viewer`. |
| `review-viewer` | Local web page for a PR, a branch, unpushed commits, a commit range or a directory: a summary the session writes (its verdicts and the risk of merging, no explanation of the code), a link to the codebase-walkthrough page of what is viewed (written only when asked, at up to $50), and a code review's findings beside the code when one exists; questions and accept/reject decisions typed there reach the live session, and its answers appear in the page; files can be ticked off as viewed. |
| `codebase-walkthrough` | Explains a module, or a PR or commit range (`--scope pr`), end to end as a pipeline: code finds every repository that calls it, what it calls, its data stores, config and vendor docs; agents explain them; gates in code drop any claim whose cited lines are not at the commit. Writes an explainer, as Markdown and an HTML page to open in a browser: what the code is for, use cases with examples, a flow diagram, what it creates with example payloads, its configuration and gotchas, with the details behind clicks (or compact Markdown for coding agents). `check` re-verifies a finished one, `render` rewrites a finished run's files with no model call, and `resume --rewrite` writes a finished run again paying only for the writers; the review-viewer opens it on request. |
| `learn-stack` | Generate an interactive framework learning guide from any codebase. |
| `e2e-test` | End-to-end testing for web apps — frontend (browser-based) and backend (API) modes. |
| `think` | Critical-thinking toolkit — audit for assumptions, biases, premature convergence; quick, focused, and deep modes. |
| `devops-gcp` | Working operator's knowledge of GCP — Cloud Run, Cloud SQL, IAM, Terraform, logging/monitoring. |
| `skill-creator` | Guide for creating (or updating) effective skills. |
| `propose-skill` | Analyze session patterns and propose new skills for recurring workflows. |
| `analyze-context-router` | Analyze memory-retrieval logs for routing accuracy, false negatives, and token efficiency. |

## Composition

- `buildme` builds from the specs `/multiplai-pm:pm-spec` (multiplai-pm) wrote
  and marked ready: a ticket, a PRD, a plan or a prompt goes to pm-spec first,
  and buildme refuses a change whose specs are not ready (`SPEC:NOT_READY`).
  Requirements problems it finds go back to pm-spec in `spec-issues.md`. It
  composes with `deep-research` (multiplai-research) for its research phase —
  use `--skip-research` if that plugin isn't installed.
- `plan` output is designed to be fed to `/multiplai-pm:pm-spec` (which writes
  the specs buildme builds from) or a fresh session unchanged.
- `analyze-context-router` and `propose-skill` operate on `multiplai-context` —
  install it first.

## Compatibility

Runs on vanilla Claude Code, any OS, with these exceptions:

- `devops-gcp` — the knowledge pack works anywhere; real operations need your
  `gcloud` auth.
- `e2e-test` — frontend mode needs `agent-browser` (npm); backend mode is plain HTTP.
- `buildme` — needs `uv` + network, and specs from `/multiplai-pm:pm-spec`; `gh` to open its PR.
- `review` — needs `uv` + network; `gh` for `--pr` and `post`.

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
