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

1. The worktree already has most of this built (code_facts/paths.py with map_paths, entries.py, scope.py, trace_gate.py, tracing.py, fallback.py, cache.py, e2e_tests.py, schema/paths.v1.schema.json and tests), and it differs from the plan in several places. Which one is the spec: should the code be changed to match the plan, or should the plan be updated to describe the code as it now stands?
2. The plan's entry kinds are cli/script/skill-command/hook/route/task/function. The code publishes argparse/click/typer/main/script/... and has a separate EntryKind list for discovery. Which vocabulary should paths.v1 lock in, given that review, review-viewer and the walkthrough will depend on it in PRs 2 to 4? Should route and task be valid kinds even though only caller seeds can produce them?
3. Before any model call, the code drops entries that have no static import path to a changed file, ranks the rest by import distance, and stops at trace_limit (default 30). The plan has neither step. Do you want this pre-trace limit, and with what default? Entries reached through SKILL.md commands, hooks or subprocess have no import edge. Should they be exempt from being dropped as unreachable, since they are the CLI and skill paths this change is meant to find?
4. In a walkthrough run with no changed lines, the plan still applies the cap ('kept up to the cap'). The code sets cap to null and keeps everything. Which is right, and if unscoped runs are capped, what order decides which entries survive when there is no changed-line count to rank by?
5. What is the most model spend per run you will accept: fallback agent, one tracing agent per entry, plus one re-ask per broken path? What should happen at the limit: stop with an error, or return partial results with the untraced entries listed in dropped? Is concurrency 4 a reasonable default?
6. For resuming, the plan specifies an in-memory `answers` mapping in the style of explore.py. The code keeps a disk cache under `cache_dir/paths-answers/`. Which does each later caller need? What should the cache key include so that answers saved for an older commit or an older prompt version are never reused?
7. When one handler is reachable from several entries, should it become one path or several? Examples: a `[project.scripts]` target that is also a click group, or a SKILL.md `python -m x sub` that is also an argparse subcommand. If it becomes one path, how should the other invokers appear in paths.json (the code has `also_invoked_by` and a `duplicate` drop)?
8. The walkthrough's trace_gate works across several repos (a `repos` dict, with boundary places for cross-repo references). code_facts takes a single snapshot root. Does the walkthrough need multi-repo snapshots and cross-repo hops when it moves onto this in PR 2? If so, should the API and schema allow for them now?
9. The plan says `changed_lines` is a count. The code records `changed_lines_covered` as a list of locations. Which form do review and review-viewer need? How should paths that cover the same number of changed lines be tie-broken, so that the cap at 21 is deterministic?
10. Besides the fixture tests, should done include running map_paths on the PR #295 snapshot and checking that the kept paths cover the code behind R-009, R-017, R-042 and R-056? Or are passing unit tests and CI enough for PR 1, with real-world checking left to the PRs that switch the skills over?

## Answers

**Q: The plan's entry kinds are cli/script/skill-command/hook/route/task/function. The code publishes argparse/click/typer/main/script/... and has a separate EntryKind list for discovery. Which vocabulary should paths.v1 lock in, given that review, review-viewer and the walkthrough will depend on it in PRs 2 to 4? Should route and task be valid kinds even though only caller seeds can produce them?**

Lock in the code's output kinds (argparse, click, typer, main, script, skill_command, hook, seed, model). Keep the discovery-only EntryKind internal. Callers would also lose detail if seeds all publish as a generic `seed`, so let `Seed` carry an optional kind, limited to route, task or function, and publish that kind. Then the walkthrough's web entries keep their meaning in PR 2. Route and task should only ever come from seeds.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:52-53 (DiscoveredKind/EntryKind); paths.py:295-300 (Seed has no kind); entries.py:96-103_

**Q: Before any model call, the code drops entries that have no static import path to a changed file, ranks the rest by import distance, and stops at trace_limit (default 30). The plan has neither step. Do you want this pre-trace limit, and with what default? Entries reached through SKILL.md commands, hooks or subprocess have no import edge. Should they be exempt from being dropped as unreachable, since they are the CLI and skill paths this change is meant to find?**

Keep the pre-trace limit with a default of 30, because it is what bounds model spend in a scoped run. Partly exempt the entries without import edges. A skill_command or hook that resolves to a Python handler already gets measured from that handler's module. One whose handler is null or not Python (a shell script at line 1, say) should not be dropped as `unreachable`. Rank it after every entry with a known distance and still count it against trace_limit. Code reached only through subprocess has the same problem; do not let a missing import edge drop it either.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/scope.py:24-36 (entry_distance measures from handler or location module); scope.py:52-71; entries.py:45-53_

**Q: What is the most model spend per run you will accept: fallback agent, one tracing agent per entry, plus one re-ask per broken path? What should happen at the limit: stop with an error, or return partial results with the untraced entries listed in dropped? Is concurrency 4 a reasonable default?**

With the defaults, a scoped run makes at most 1 fallback call plus 30 traces plus up to 30 re-asks, so 61 calls. That is acceptable. Unscoped runs need the bound from Q4. At the limit, return partial results and list every untraced entry in `dropped` with a reason. Do not raise an error: a review should go ahead on the paths it has. Concurrency 4 is a reasonable default.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:379-381 (cap=10, trace_limit=30, concurrency=4); scope.py:65-68 (over_trace_limit drops)_

**Q: For resuming, the plan specifies an in-memory `answers` mapping in the style of explore.py. The code keeps a disk cache under `cache_dir/paths-answers/`. Which does each later caller need? What should the cache key include so that answers saved for an older commit or an older prompt version are never reused?**

Keep the disk cache. Review and buildme resume in a new process, and with no `cache_dir` the cache already works as an in-memory mapping. The keys already include the prompt version, commit, entry and handler, changed lines, known edges, places and hop limit. The re-ask key also chains to the first answer. Two gaps remain. `commit` is a free-form label, so require a full SHA. Add the model identifier, so a different model never reuses an older model's answers.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/tracing.py:82-102 (trace_key/reask_key); fallback.py:75-79; cache.py:44-57; paths.py:390 ('commit is the label written to the result')_

**Q: When one handler is reachable from several entries, should it become one path or several? Examples: a `[project.scripts]` target that is also a click group, or a SKILL.md `python -m x sub` that is also an argparse subcommand. If it becomes one path, how should the other invokers appear in paths.json (the code has `also_invoked_by` and a `duplicate` drop)?**

Make it one path per handler, as the code does now. The entry with the best KIND_RANK owns the path and the others are listed in `also_invoked_by` with their kind, name and location. The `duplicate` drop is only for model-fallback entries whose cited line a discovered entry already holds. Keep it, because it makes visible a model entry that would otherwise vanish silently.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/entries.py:58-61 and 1277-1309 (merge_entries); paths.py:127-141 (Invoker, also_invoked_by); fallback.py:15_

**Q: The walkthrough's trace_gate works across several repos (a `repos` dict, with boundary places for cross-repo references). code_facts takes a single snapshot root. Does the walkthrough need multi-repo snapshots and cross-repo hops when it moves onto this in PR 2? If so, should the API and schema allow for them now?**

Yes, the walkthrough needs cross-repo hops. A single root can already hold them: make it the workspace directory with each repo as a subdirectory, so paths look like `<repo>/<file>`, which matches the walkthrough's workspace paths. PyIndex already resolves imports across the repository roots under the snapshot, and `places` carries the cross-repo boundary edges. Leave the schema as it is. If the walkthrough's repos have different commits, PR 2 will need a per-repo commit field; that should be a later additive schema change.

_Grounded in: plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/gates.py:26-57,188-194; plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py:354-373; paths.py:378 (places)_

**Q: The plan says `changed_lines` is a count. The code records `changed_lines_covered` as a list of locations. Which form do review and review-viewer need? How should paths that cover the same number of changed lines be tie-broken, so that the cap at 21 is deterministic?**

Use the list. Review-viewer needs the locations to highlight them, and the count is just the list's length. Keep the code's tie-break: most changed lines covered, then fewest hops, then KIND_RANK, then entry path, then line. Entries are merged per handler and fallback duplicates are dropped, so this order is deterministic.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:199 (changed_lines_covered: list[Location]); paths.py:453-459 (order_paths); scope.py:45-49_

## Assumptions

Nothing in the repo, the ticket or the context decides these; each answer is an assumption to confirm.

**Q: The worktree already has most of this built (code_facts/paths.py with map_paths, entries.py, scope.py, trace_gate.py, tracing.py, fallback.py, cache.py, e2e_tests.py, schema/paths.v1.schema.json and tests), and it differs from the plan in several places. Which one is the spec: should the code be changed to match the plan, or should the plan be updated to describe the code as it now stands?**

Update the plan to describe the code. The code is already built and more precise than the plan: it has closed drop and reason codes, a generated schema and tests. Change the code only where the answers below say so: exempt non-import entries from the reachability drop (Q3), bound the cost of unscoped runs (Q4/Q5), and add a model identifier to the cache key (Q6). The plan's `invoked_by`/`found_by` fields and the `changed_lines` count should be replaced by what the code has: `kind: model`, `also_invoked_by` and `changed_lines_covered`.

**Q: In a walkthrough run with no changed lines, the plan still applies the cap ('kept up to the cap'). The code sets cap to null and keeps everything. Which is right, and if unscoped runs are capped, what order decides which entries survive when there is no changed-line count to rank by?**

Today an unscoped run applies neither a cap nor trace_limit, so every discovered entry is traced and the model cost has no upper bound. Allow an optional cap in unscoped runs, defaulting to none so module walkthroughs still see every entry. Apply it before tracing so it actually limits cost. Order by KIND_RANK, then entry path, then line. After tracing, keep the code's order_paths order. Dropped entries go to the existing `trace_limit`/`over_trace_limit` stage, which would then have to be allowed in unscoped documents.

**Q: Besides the fixture tests, should done include running map_paths on the PR #295 snapshot and checking that the kept paths cover the code behind R-009, R-017, R-042 and R-056? Or are passing unit tests and CI enough for PR 1, with real-world checking left to the PRs that switch the skills over?**

Passing unit tests and CI are enough for PR 1, because the plan's 'Done means' lists only those and no skill uses this code yet. The PR #295 check belongs in the PR that switches review over, where finding R-009, R-017, R-042 and R-056 is the actual goal. A one-off, non-blocking smoke run on that snapshot before merging PR 1 would still be cheap and worth doing.
