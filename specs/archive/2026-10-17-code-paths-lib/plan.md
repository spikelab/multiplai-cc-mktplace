# Plan: code paths in the shared code_facts library

## Why

Code review, codebase-walkthrough and review-viewer each need the same answer: which
entry points (a CLI command, a script, a hook, a route, a function another program or
agent calls) reach the code being looked at, and the call path from each one to its last
effect. On PR #295, one review agent per path found 4 real code bugs that per-file review
agents missed in 3 of 3 runs (R-009, R-017, R-042, R-056; see
`INBOX/review-lessons-dynamic-workflows-2026-10-16.md`). Today only
codebase-walkthrough traces, and only from web entry points (webhooks, routes, Celery;
`skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py:1-10`), so it
finds nothing in a CLI or skill repository. This change puts path finding into
`plugins/multiplai-dev/lib/code_facts/` so all three can use one implementation.

This is PR 1 of 4. The walkthrough, review and review-viewer switch to it in later PRs.
This PR does not change any skill's behaviour.

## What to build

All in `plugins/multiplai-dev/lib/code_facts/`.

1. **Models** (`code_facts/models.py` or a new `code_facts/paths.py`):
   - `EntryPoint`: `kind` (one of `cli`, `script`, `skill-command`, `hook`, `route`,
     `task`, `function`), `name` (for example `pr_pipeline edit`), `path`, `line`,
     `invoked_by` (free text: a human, an agent, another skill, buildme, a webhook),
     `found_by` (`code` or `model`).
   - `Hop`: `symbol_called`, `path`, `line_start`, `line_end`, `quote`, `note`. The same
     shape as the walkthrough's `Hop` (`walkthrough_pipeline/models.py:122-128`) so the
     walkthrough can move onto it.
   - `CodePath`: `id` (`p1`, `p2`, ...), `entry: EntryPoint`, `title`, `hops`,
     `stops_at` (1-based hop index where the trace broke, or none), `break_reason`,
     `changed_lines` (for a change: how many changed lines the hops cover),
     `e2e_tests` and `e2e_tests_mocked` (below).
   - `PathsResult`: `paths`, `dropped` (each with entry and reason, for example
     `over the cap of 21`), `entries_found`, `errors`.
2. **Entry-point discovery by code** (`code_facts/entries.py`), no model call, from a
   snapshot directory:
   - argparse: `add_parser("<name>")` under `add_subparsers`, giving one entry per
     subcommand, at the line of the handler when it can be found (`set_defaults(func=...)`
     or a dispatch dict), else the `add_parser` line;
   - click `@<x>.command(...)` / `@click.command`, typer `@<app>.command(...)`;
   - `[project.scripts]` in every `pyproject.toml`, resolved to the function's file and
     line;
   - modules with `if __name__ == "__main__":` and packages with `__main__.py`;
   - commands a `SKILL.md` tells an agent to run: fenced shell blocks that run
     `python -m <module> [<subcommand>]` (directly or behind `uv run ...`); kind
     `skill-command`, `invoked_by` = `an agent following <SKILL.md path>`;
   - hooks declared in `hooks.json` / `hooks/hooks.json` / `.claude/settings*.json`
     (`command` entries pointing at a script in the repo);
   - the caller may pass extra seeds (the walkthrough passes its web entries).
   Python is parsed with `ast`; unparseable files are skipped and counted in `errors`.
3. **Model fallback for entries code cannot find.** One agent gets the scope (for a
   change: the changed hunks) and the entries code found, and lists further entry points
   that reach the scope, typically functions another program, skill or agent calls. Each
   must cite its definition line; an entry whose citation does not re-read at the commit
   is dropped (use the existing citation checking in `explore.py`). `found_by: model`.
4. **Tracing.** One agent per entry point writes the hops from the entry to its last
   effect: a file written, a command run, a network call, output printed, a value
   returned to the caller. It reads the snapshot with `Read`, `Grep`, `Glob` only, like
   every code_facts agent. Calls go through the caller's `CallFn`, as `explore.py` does.
5. **The trace gate**, in Python, no model: the same rules as the walkthrough's
   `trace_gate` (`walkthrough_pipeline/gates.py:188-215`): every hop's citation re-reads
   at the commit; hop 1 contains the entry's line; for k > 1, hop k-1's quote contains
   the name hop k is called as and hop k's lines define it, or hop k's lines hold a
   known edge (caller-supplied places). A break gets one re-ask naming the hop; if it
   still breaks, the path keeps its hops up to the last good one and records `stops_at`
   and `break_reason`. Write it against code_facts' own `Resolve`/`Citation` types so it
   needs nothing from the walkthrough package.
6. **Scope and cap.** For a change the caller passes the changed lines per file at head.
   Keep a path only when one of its hops covers a changed line. Order the kept paths by
   `changed_lines`, most first, then keep at most `cap` (the caller passes it; review
   will pass 21). Every path not kept goes to `dropped` with its reason. For a module or
   tree walkthrough the caller passes no changed lines and every entry into the scope is
   kept up to the cap.
7. **End-to-end tests per path**, in Python, no model. For each kept path, search the
   repository's test files (`test_*.py`, `*_test.py`, files under `tests/`, and
   `*.test.*` / `*.spec.*` for JS/TS) for tests that invoke the path's entry: the CLI or
   subcommand name in an argv list or subprocess call, `main([...])` with the
   subcommand, a call of the entry function by name, or the route's URL. Record each as
   `path::test_name` with its line. A test that also replaces the path's second hop with
   a mock (`patch(...)` / `monkeypatch.setattr(...)` / `mocker.patch(...)` naming hop 2's
   symbol) goes to `e2e_tests_mocked` instead and does not count. Nothing runs any test.
8. **Output and contract.** `write_paths_file(result, path)` writes `paths.json`, and
   `schema/paths.v1.schema.json` describes it (follow the style of
   `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`). Include the commit,
   the scope, the cap, the paths, the dropped entries and the errors.
9. **Public API**, exported from `code_facts/__init__.py`, along the lines of
   `async def map_paths(snapshot, call, resolve, *, changed=None, cap, seeds=(),
   answers=None) -> PathsResult`, where `answers` caches agent answers by key so a
   resumed run asks only for the rest (the pattern `explore.py`'s `ask`/`run_tasks`
   already use).

## Constraints

- No skill changes in this PR: codebase-walkthrough, review and review-viewer keep their
  current behaviour. They move onto this in PRs 2 to 4.
- Agents stay read-only: `Read`, `Grep`, `Glob`. No shell, no edits, no web.
- Discovery, the gate, the scope filter and the e2e search are deterministic Python and
  never call a model.
- Follow the library's existing style: pydantic models with `_Model`, async calls through
  the caller's `CallFn`, prompts in `prompts.py`.

## Done means

- `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/`
  passes, with new tests covering:
  - discovery of argparse subcommands, click and typer commands, `[project.scripts]`,
    `__main__` modules, `SKILL.md` commands and hooks on small fixture repositories;
  - the trace gate: a good path, a hop whose quote does not re-read, a hop whose
    previous quote does not name it, the re-ask, and `stops_at`;
  - the scope filter and the cap: paths outside the changed lines are dropped, the cap
    keeps the paths crossing the most changed lines, and each drop has a reason;
  - the e2e search: a test that runs the CLI subcommand counts; a test that mocks hop 2
    goes to `e2e_tests_mocked`; a path with no test has empty lists;
  - `paths.json` validates against `schema/paths.v1.schema.json`;
  - `map_paths` end to end with a fake `CallFn` (no real model call).
- The full repository CI suite still passes.
- No skill's behaviour changes.
