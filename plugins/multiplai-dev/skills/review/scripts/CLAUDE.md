# review scripts — orientation

A pipeline of separate agents with gates written in Python between them. The
agents find, verify, prescribe and check; the gates decide what survives, by
re-reading the reviewed commit with git. No gate calls a model.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m review_pipeline --help
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m pytest tests/ -q
# CI form
cd plugins/multiplai-dev/skills/review/scripts && \
  uv run --project ../../../../.. --package review-pipeline --extra dev python -m pytest tests/ -q
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `review`, `batch`, `rollup`, `resume`, `post`. Calls `setup_logging` once. Owns the stdout contract and the exit codes. |
| `models.py` | Pipeline models. `Finding.citations` has `min_length=1`; `Finding.id` is computed with the v1 rule, never taken from a model. `STAGES` is the run order. |
| `target.py` | Resolves `--branch` / `--pr` / `--range` to shas, writes `diff.patch`, `commits.txt`, `files.txt`; `target_gate`; `snapshot_head()` (`git archive` of head into `<slug>/tree/`). Fixed argv, no shell, no checkout. |
| `gates.py` | `citation_gate`, `finding_gate`, `verdict_gate`, `premise_gate`, `symbol_consumer_gate`, `fix_gate`. Each takes `TargetInfo` and returns `GateResult`. |
| `sdk.py` | `agent_call_structured`: trust gate, `Read`/`Grep`/`Glob` allow-list with its complement denied, one re-ask on a bad answer. The only caller of `run_agent`. |
| `budget.py` | Per-target ledger in a `ContextVar` (a batch runs targets concurrently) and the circuit breaker. |
| `config.py` | `review.yaml` > `multiplai.conf` > session model, for per-stage models, effort, concurrency. |
| `stages/` | `find`, `verify`, `prescribe`, `check_fix`. Each `run_<stage>(state, ctx)` returns at once when the state is past it and skips items it already has. |
| `prompts/` | One module per stage; shared blocks in `__init__.py`. `prescribe.PREMISE_CONTRACT` is the text the gates enforce. |
| `export.py` | `ReviewState` → `findings.json` v1, key by key (the contract rejects unknown keys). |
| `render.py` | `review-<slug>.md`, the short `summary-<slug>.md` the session pastes into chat, and the `<SEV>-only.md` rollups, all from v1 dicts. |
| `post.py` | One `gh pr comment`; with `--decisions`, only findings whose decision is `accept`. |
| `orchestrator.py` | target → find → verify → prescribe → check_fix → export → render, saving `review-state.json` after each; `resume`; `batch`. |
| `state.py`, `progress.py` | Atomic checkpoint; the tailable `progress.log` (`STARTED`, `STAGE`, `DONE`, `FAILED`). |

## Why the agents read a snapshot

The gates check quotes against `git show <head_sha>:<path>`. The agents' tools
see a directory, and the repository's working tree is whatever is checked out.
So `snapshot_head()` extracts the head tree with `git archive` (which writes
nothing to the repo) and every agent runs with `cwd` there. Paths the agents
return are made repo-relative in `stages/__init__.py`. The snapshot is deleted
when the run finishes.

## The gates

| Gate | Passes when | On failure |
|---|---|---|
| `citation_gate` | the whitespace-normalised quote is inside lines `line_start..line_end` at head | building block |
| `finding_gate` | every citation passes; the file changed (unless `dimension == "pre-existing"`); severity is known | finding → `rejected` |
| `verdict_gate` | a `confirmed` verdict has at least one citation that passes | verdict → `unverifiable`, severity lowered one step |
| `premise_gate` | `in_repo` has a passing citation; `external` has none | fix re-asked |
| `symbol_consumer_gate` | the citation covers a line that *uses* the symbol | fix re-asked |
| `fix_gate` | at least one premise, and every premise passes the two above | re-asked once, then "no verified fix" |

`symbol_consumer_gate` takes `premise.symbol` when it is UPPER_CASE (a function or
variable name is exempt), or the all-caps names in the
statement (`\b[A-Z][A-Z0-9_]{3,}\b`) that the repo defines. A hit is a
definition when the line matches `^\s*SYMBOL\s*[:=]` or contains
`config('SYMBOL'` / `getenv('SYMBOL'`; every other `git grep -w` hit is a use.
It checks provenance, not truth; `check_fix` judges truth. This is the gate
the DB2038 fix failed: it derived a keyword from a setting after reading only
the setting's definition.

Gate function names must not start with `test` (pytest would collect them).

## Logging

`setup_logging("review-pipeline", propagate_loggers=("review_pipeline", "multiplai_core"))`
writes `review-pipeline.log`. `log_event("review", …)` fires `start`, `stage`,
`gate_reject`, `budget_stop`, `done`, `post`. No field holds finding or prompt
text: `gate_reject` records which rule fired (`orchestrator.reason_kind`), not
the reason string.

## Tests

`tests/fixture_repo.py` builds the DB2038 regression repository from
`tests/fixtures/settings_consumer_repo/{base,head}` with fixed dates, so its
shas are stable. Every agent call is monkeypatched; nothing here calls a model.
`test_export.py` validates against
`../../review-viewer/schema/findings.v1.schema.json`, and `test_models.py`
checks this package's `finding_id` against the ids in the viewer's own
fixture. The package never imports `review_viewer`.
