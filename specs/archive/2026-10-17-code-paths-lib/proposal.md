## Why

Code review, codebase-walkthrough and review-viewer all need to know which entry points reach a piece of code, and the call path from each one to its last effect. An entry point can be a CLI subcommand, a script, a hook, a command a `SKILL.md` tells an agent to run, a route, or a function another program calls. Today none of them can get that answer in a form the others can reuse:

- **Only codebase-walkthrough traces, and only from web entry points.** It starts from webhooks, routes and Celery tasks (`skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py:1-10`). On a CLI or skill repository such as this one, it finds no entry points.
- **Review has no path view.** It splits work by file or by diff piece. On PR #295, one finder per call path found 4 code bugs (R-009, R-017, R-042, R-056). Per-piece finders (prototype A) missed all 4 in every one of 3 runs. The paths arm found all 4 high-severity bugs in every run. Combined with A in the same run, it went from 16 to 20 distinct bugs across 3 runs, at about $24 a run. This comes from the 2026-10-16/17 PR #295 benchmark: one PR, one model scorer per run, and an answer key built from Codex findings.
- **Review-viewer has nothing to show** a reader about how a change is reached.

If all three need the same facts, they should share one implementation. The obvious home is the shared `code_facts` library, which already provides read-only agents, citation re-reading and resumable answer caching. This is PR 1 of 4: build the capability once, then move the three skills onto it in PRs 2 to 4.

## What Changes

`plugins/multiplai-dev/lib/code_facts/` gains a path-mapping capability:

- **Entry-point discovery in Python, with no model call.** It finds:
  - argparse subcommands;
  - click and typer commands;
  - `[project.scripts]`;
  - `__main__` modules and packages;
  - `python -m` commands in `SKILL.md` fenced shell blocks;
  - hook commands in `hooks.json` and `.claude/settings*.json`;
  - seeds passed in by the caller.

  Entries that resolve to the same handler are traced once. The most specific kind is kept, and the other invokers go in `also_invoked_by`. *(Assumption to confirm.)*
- **Model fallback for entry points.** One read-only agent lists further entry points that reach the scope. Each must cite its definition line, and an entry whose citation does not re-read at the commit is dropped.
- **Tracing.** One read-only agent per entry writes one linear chain of hops, up to 20, from the entry to its last effect. Calls go through the caller's `CallFn`, with bounded concurrency and cached answers keyed independently of order.
- **A deterministic trace gate.** It checks hops with the walkthrough's rules, rewritten against `code_facts`' own `Resolve`/`Citation`. It accepts known edges from the caller (`places`) and from discovery: handler dispatch lines, script targets, and hook or `SKILL.md` command targets. A broken hop gets one re-ask. If it still breaks, the path is cut there, and the result records `stops_at` and `break_reason`.
- **Scope and cap.** This applies only when changed lines are given:
  - Entries that cannot reach a changed file through the static import graph are dropped before tracing, and so is anything over a trace limit.
  - After tracing, only paths whose hops cover a changed line are kept.
  - Kept paths are ordered deterministically by changed lines covered, then hop count, kind and location. At most `cap` are kept, and ids `p1..pN` are assigned after ordering.
  - Every drop records a reason.
- **End-to-end test listing in Python.** It lists tests that structurally invoke each path's entry. Tests that mock hop 2 are listed separately. No test is run.
- **Output contract.** `paths.json` is described by `schema/paths.v1.schema.json`. It is written by `write_paths_file`, and `map_paths(...)` is exported from `code_facts/__init__.py`.

**Goals**
- One path-finding implementation that review, codebase-walkthrough and review-viewer can adopt without API changes. This includes snapshots that hold several repositories, with paths relative to the snapshot root.
- Entry points found in CLI and skill repositories, not only web services.
- Every path backed by citations that re-read at the commit, and every dropped entry or path explained.
- Deterministic, reproducible output: stable ordering, stable ids, and resumable runs.

**Non-Goals**
- Changing any skill's behaviour. Codebase-walkthrough, review and review-viewer move onto the library in PRs 2 to 4.
- Proving recall on a real repository. Fixture tests and green CI are the bar for this PR. A one-off PR #295 check goes in the PR description, with no pass/fail rule.
- Branching paths (several paths from one entry), or tracing base-side code that no longer exists at head. Callers map deletions to adjacent head lines.
- Running tests, running code, or giving agents anything beyond `Read`, `Grep` and `Glob`.
- Budgeting or retrying model calls. Both stay with the caller's `CallFn`.

## Capabilities

### New Capabilities
- `entry-point-discovery`
- `entry-point-model-fallback`
- `path-tracing`
- `trace-gate`
- `path-scope-and-cap`
- `e2e-test-listing`
- `paths-output-contract`

### Modified Capabilities

## Impact

- **Affected code:** `plugins/multiplai-dev/lib/code_facts/` only.
  - New modules: `code_facts/entries.py`, and `code_facts/paths.py` (or additions to `code_facts/models.py`).
  - New agent prompts in `code_facts/prompts.py`.
  - New exports in `code_facts/__init__.py`.
  - A new schema at `schema/paths.v1.schema.json`.
  - New fixture repositories and tests under `tests/`.
- **Reused internals:** citation checking, `bounded(...)`, and the `ask`/`run_tasks` answer-cache pattern in `code_facts/explore.py`, plus the `_Model` base and `Citation` (head side by default) in `code_facts/models.py`. These must stay compatible. No behaviour change to existing exports.
- **Shapes mirrored but not imported:**
  - `Hop` from `walkthrough_pipeline/models.py:122-128`.
  - The `trace_gate` rules from `walkthrough_pipeline/gates.py:188-215`.
  - `boundary_places` from `walkthrough_pipeline/stages/trace.py:50-52`.

  Until PR 2 moves codebase-walkthrough onto the library, the gate logic exists in two places.
- **Schema style:** follows `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`.
- **Known discovery case:** `pr_pipeline` dispatches on `args.command` (`skills/pr/scripts/pr_pipeline/__main__.py:53,72`), not on `set_defaults(func=...)` or a dispatch dict. Its `open`, `edit` and `render` entries therefore land on their `add_parser` lines. A deterministic test covers this.
- **Dependencies:**
  - Python stdlib: `ast` for parsing, `tomllib` for `pyproject.toml`, `json` for hooks and output.
  - Existing: `pydantic`.
  - Tests: `pytest`, run through `uv`. Schema validation needs a JSON Schema validator such as `jsonschema`; check whether it is already a dev dependency of the library.
  - Model calls go only through the caller-supplied `CallFn`. There is no new SDK dependency.
- **Systems:** none outside the library. No skill, hook, CI workflow or plugin manifest changes. The full repository CI suite must still pass.