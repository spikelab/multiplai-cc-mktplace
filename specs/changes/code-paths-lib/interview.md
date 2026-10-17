# Interview

## Plan

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

## Interview

Add entry-point discovery, path tracing with a citation gate, scope filtering with a cap, e2e test listing and a paths.json contract to the shared code_facts library. PR 1 of 4; no skill behaviour changes.

## Questions

1. What is this PR meant to prove on a real repository, beyond the fixture tests? For example, should running `map_paths` on this repo (or on PR #295's diff) have to find the `pr_pipeline` subcommands and the paths behind R-009/R-017/R-042/R-056 before this counts as done, or are fixture tests and green CI enough?
2. `map_paths` takes one `snapshot`, but the walkthrough traces across several repos (its `trace_gate` takes a `repos` dict and paths are `<repo key>/<path>`). Should discovery and tracing handle multi-repo snapshots now, so PR 2 can migrate without changing the API, or is single-repo enough for PR 1?
3. The proposed `map_paths` signature has no parameter for the gate's 'known edges' (caller-supplied places such as routes, tasks and cross-repo references). How should the caller pass them, and should code discovery ever produce its own edges, such as a dispatch dict or a hook's command pointing at a script?
4. For `skill-command` and `hook` entries, what are the entry's `path` and `line`, and so what must hop 1 contain? Is it the line in SKILL.md or hooks.json, or the handler or `__main__` line in the target module?
5. When the same code is reachable from several entries (a `[project.scripts]` function, its argparse subcommand, a SKILL.md command running it, a hook calling the script), should those be merged into one entry or path, or traced and counted separately against the cap? If merged, which `kind` and `invoked_by` win?
6. Is a path always one per entry with one linear chain of hops, or can an entry that branches (for example a subcommand handler that writes a file and also makes a network call) produce several paths? Is there a maximum hop count per trace?
7. The scope filter runs after tracing, so every discovered entry costs one agent call even if it never reaches the change. Should code narrow the entries before tracing (for example by import or call reachability to the changed files)? Is there a budget or concurrency limit on trace agents for a repo with many subcommands?
8. How should the scope filter treat a change that only deletes lines, or whose key edits are removed lines on the base side? There are no head lines for a hop to cover, so would such a path be dropped as 'outside the change'?
9. How should ties in `changed_lines` be broken at the cap? And are the `p1`, `p2`, ... ids given before or after ordering? Review-viewer and resumed runs may need the same id for the same path across runs.
10. In the e2e search, how loose can matching be? Common subcommand names like `edit` or a function name can produce false positives, and the mock rule only looks at hop 2. Is listing a test that only mentions the name acceptable, and should parametrised or class-based tests be recorded as `file::Class::test[param]`?

## Answers

**Q: What is this PR meant to prove on a real repository, beyond the fixture tests? For example, should running `map_paths` on this repo (or on PR #295's diff) have to find the `pr_pipeline` subcommands and the paths behind R-009/R-017/R-042/R-056 before this counts as done, or are fixture tests and green CI enough?**

Under 'Done means', passing fixture tests and green CI are enough. No skill uses the library until PRs 2 to 4, so the real-repo check belongs there. Two things would still help in this PR. First, add a cheap deterministic test that runs code discovery on `pr_pipeline` and expects `open`, `edit` and `render`. That needs no model. Second, record a one-off check against PR #295 in the PR description, with no pass/fail rule attached. One finding: `pr_pipeline` sends each subcommand to its code by checking `args.command`, not through `set_defaults(func=...)` or a dispatch dict. Under the plan's rule each entry would therefore sit on its `add_parser` line. I could not find the lessons file the plan cites (`INBOX/review-lessons-dynamic-workflows-2026-10-16.md`) in this worktree, so I can't confirm R-009/R-017/R-042/R-056.

_Grounded in: plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/__main__.py:53, plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/__main__.py:72; Plan 'Done means': uv run ... pytest tests/ passes ... full repository CI suite still passes_

**Q: `map_paths` takes one `snapshot`, but the walkthrough traces across several repos (its `trace_gate` takes a `repos` dict and paths are `<repo key>/<path>`). Should discovery and tracing handle multi-repo snapshots now, so PR 2 can migrate without changing the API, or is single-repo enough for PR 1?**

Keep the API able to handle several repos, but don't add multi-repo code beyond that. code_facts already avoids picking a layout: paths are in whatever form the caller's `resolve(path, side)` understands, and the walkthrough uses `<repo key>/<path>`. The gate should therefore check citations only through `resolve`/`check_citation`, never through a `repos` dict. `snapshot` is the agents' working directory and the root that discovery walks. If it holds one subdirectory per repo, discovery should emit paths relative to the snapshot root, which then match `<repo key>/<path>`. Single-repo fixtures are enough for the tests, plus one test with two repo directories under the snapshot.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/models.py:3-6; plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:74-81_

**Q: The proposed `map_paths` signature has no parameter for the gate's 'known edges' (caller-supplied places such as routes, tasks and cross-repo references). How should the caller pass them, and should code discovery ever produce its own edges, such as a dispatch dict or a hook's command pointing at a script?**

Add a keyword argument `places: Iterable[tuple[str, int]] = ()` with the same shape as the walkthrough's `boundary_places`, so PR 2 can pass its list unchanged. Discovery should also add its own places. These are the line a handler is reached from (a `set_defaults(func=...)` or dispatch-dict entry), the target of a `[project.scripts]` entry, and the script a hook or SKILL.md command runs. Without them, a hop from a config or markdown line to Python can never pass the 'defines' rule. The gate takes the union of caller places and discovered places.

_Grounded in: plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py:50-52; plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/gates.py:212-213_

**Q: Is a path always one per entry with one linear chain of hops, or can an entry that branches (for example a subcommand handler that writes a file and also makes a network call) produce several paths? Is there a maximum hop count per trace?**

Keep one linear path per entry for PR 1. The plan says 'one agent per entry point writes the hops', and the gate checks a single chain. The agent should follow the branch that leads to the main effect and mention other effects in hop notes. Splitting branches can come later if review needs it. Cap the hops in the schema and prompt (for example 20). The walkthrough's prompt says six to twelve hops is typical and to stop at the first effect that leaves the process.

_Grounded in: plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/prompts.py:105; Plan: 'One agent per entry point writes the hops from the entry to its last effect'_

**Q: The scope filter runs after tracing, so every discovered entry costs one agent call even if it never reaches the change. Should code narrow the entries before tracing (for example by import or call reachability to the changed files)? Is there a budget or concurrency limit on trace agents for a repo with many subcommands?**

Yes, when `changed` is given. Before tracing, drop entries whose module cannot reach any changed file through the static `ast` import graph (package-local imports, conservative). Record each in `dropped` with reason `cannot import a changed file`. Keep entries whose reachability is unknown. Run trace calls through the existing `bounded(...)` with a `concurrency` keyword. Spending limits stay with the caller's `CallFn`, because the library never retries or budgets itself. Cap the number of traced entries (for example 3×cap) and drop the rest with a reason.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:38-58; plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:3-6_

**Q: How should the scope filter treat a change that only deletes lines, or whose key edits are removed lines on the base side? There are no head lines for a hop to cover, so would such a path be dropped as 'outside the change'?**

The caller should map each deletion to the head line where it happened (the line next to where the removed lines were) and pass that in `changed`. A hop covering that line then counts the deletion. Without this, a path that only deletes lines would be wrongly dropped. Tracing stays on the head side only (`Citation.side` defaults to `head`). A path that matters only through base-side code that no longer exists is out of scope for PR 1, and the docstring should say so.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/models.py:35-37_

**Q: How should ties in `changed_lines` be broken at the cap? And are the `p1`, `p2`, ... ids given before or after ordering? Review-viewer and resumed runs may need the same id for the same path across runs.**

Break ties first by hop count (fewer first), then by kind, then by the entry's (path, line, name). That order is fully deterministic. Give `p1..pN` after ordering and capping, the same way code_facts gives `f<N>`/`c<N>` ids in pass order. The answer-cache key for an entry should not depend on order: use something like `trace:<kind>:<path>:<line>:<name>`, so a resumed run with the same inputs reuses the answers and gets the same ids.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/models.py:8-9; plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:211-216_

**Q: In the e2e search, how loose can matching be? Common subcommand names like `edit` or a function name can produce false positives, and the mock rule only looks at hop 2. Is listing a test that only mentions the name acceptable, and should parametrised or class-based tests be recorded as `file::Class::test[param]`?**

Match on structure, not just the name. A subcommand counts only as an exact string element of an argv list or of `main([...])`/subprocess args. It must also appear with the program or module name (for example `pr_pipeline`) or in a test file that imports the entry's module. A function counts only as a call (`ast.Call`) of the name, imported from the entry's module. A test that only mentions the name is not listed. Record pytest-style ids without parameters (`path::Class::test_name`). Parameter ids would need pytest collection, and nothing runs tests. Keep the mock rule to hop 2, as specified.

_Grounded in: Plan step 7: 'the CLI or subcommand name in an argv list or subprocess call, `main([...])` with the subcommand, a call of the entry function by name ... Nothing runs any test.'_

## Assumptions

Nothing in the repo, the ticket or the context decides these; each answer is an assumption to confirm.

**Q: For `skill-command` and `hook` entries, what are the entry's `path` and `line`, and so what must hop 1 contain? Is it the line in SKILL.md or hooks.json, or the handler or `__main__` line in the target module?**

The entry's `path`/`line` is the place that declares it: the command line inside the fenced block in SKILL.md, or the `command` entry in hooks.json. Hop 1 must contain that line, as the gate already requires for every entry. Hop 2 is the target module's handler or `__main__` block, and the gate accepts it as a discovered place (see Q3). This keeps 'who invokes this, and where' traceable to a citation, and lets the same handler be reached from different entries.

**Q: When the same code is reachable from several entries (a `[project.scripts]` function, its argparse subcommand, a SKILL.md command running it, a hook calling the script), should those be merged into one entry or path, or traced and counted separately against the cap? If merged, which `kind` and `invoked_by` win?**

Remove duplicates before tracing, keyed on where the entry resolves in code: the same handler file and line, or the same subcommand of the same module. Trace once and count once against the cap. The most specific code entry becomes `entry`, with `cli` (a subcommand) ranking above `script` and `script` above `skill-command`/`hook`. Record the other invokers in an `also_invoked_by` list (kind, path, line, invoked_by), so review still sees that an agent or a hook runs it. Without this, one handler could take several of the 21 slots.
