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

1. This worktree already has a partial implementation that differs from the plan. It names things TracedPath/PathEntry/PathHop/PathDrop instead of CodePath/EntryPoint/Hop, and its map_paths(root, commit, resolve, call, changed_lines, places, cap=10, trace_limit=30, cache_dir, fallback, ...) uses a cache directory where the plan has an `answers` dict. The schema also sits at code_facts/schema/ rather than schema/. Should the spec follow the plan or the existing code and its tests, and where they disagree, which wins?
2. The plan says the caller always passes the cap (review passes 21), and that an unscoped walkthrough keeps paths 'up to the cap'. The existing code defaults cap to 10, writes cap as null and ignores it on unscoped runs. Should cap be required, what should its default be, and should it apply to unscoped runs?
3. When several discoverers find the same entry, how should it be deduplicated and which kind should it keep? Examples: a [project.scripts] function that is also a `__main__` module, a SKILL.md `python -m pkg sub` that is also an argparse subcommand, or a caller seed that matches a code-found entry.
4. Does the PR 1 design need to fit the hop shape PR 2 needs, given the walkthrough's Hop must move onto this? Specifically: should PathHop stay field-for-field the same as walkthrough_pipeline's Hop, and should the `places` (known edges) argument take the same (path, line) tuples the walkthrough's boundary.json gives today?
5. How should changed lines be counted? (a) Does a pure deletion, which has no line at head, scope in any path, or is the change then invisible? (b) Is a line two hops of one path both cover counted once? (c) For a path broken at stops_at, do only the hops up to the last good one count? (d) How are ties in changed_lines broken so the order is deterministic?
6. What exactly counts as 'the last effect' when an entry branches, for example a subcommand that writes a file on one branch and makes a network call on another? Is it one path per entry with the agent picking a branch, or can one entry produce several paths? And what hop limit (trace_limit) should stop recursion and cycles?
7. What limits apply to the model fallback? Should it run when code already found entries, is there a maximum number of entries it can add, and do model-found entries compete for the cap on equal terms with code-found ones, or are they ranked after them?
8. The agents read repository content that could be attacker-controlled: SKILL.md text, hooks.json, code comments, test files. How should prompts and gates treat it, for example the existing UNTRUSTED_RULES and check_citation? Should a hooks.json or SKILL.md command pointing outside the repo, or using an absolute path, be dropped, recorded in diagnostics, or ignored without a record?
9. How strict should the e2e matcher be to avoid false positives? A function named `main` or a subcommand called `run` appears in many tests. Must a test name both the program and the subcommand to count? And since discovery is Python-only, what should JS/TS `*.test.*`/`*.spec.*` files match: only route URLs passed as seeds, or nothing in this PR?
10. Beyond unit tests on fixtures, is there an acceptance check on a real repository? For example: running map_paths on this repo finds the expected CLI and skill-command entries such as `pr_pipeline edit`, or reproduces the PR #295 paths behind R-009/R-017/R-042/R-056. Or is fixture coverage plus green CI the whole definition of done for PR 1?

## Answers

**Q: This worktree already has a partial implementation that differs from the plan. It names things TracedPath/PathEntry/PathHop/PathDrop instead of CodePath/EntryPoint/Hop, and its map_paths(root, commit, resolve, call, changed_lines, places, cap=10, trace_limit=30, cache_dir, fallback, ...) uses a cache directory where the plan has an `answers` dict. The schema also sits at code_facts/schema/ rather than schema/. Should the spec follow the plan or the existing code and its tests, and where they disagree, which wins?**

Follow the existing code where it has already fixed the public contract. That covers the names (TracedPath, PathEntry, PathHop, PathDrop), the field order the tests pin, the keyword-only map_paths signature, cache_dir in place of an `answers` dict (it is the same idea: answers are kept by key, so a rerun asks only for what it lacks), and the schema as a package resource at code_facts/schema/paths.v1.schema.json. Follow the plan where the code has not delivered yet. Three things are stubs today: the model fallback is always recorded as skipped, test listing always returns empty, and the scope filter and cap do nothing. Those must still be built as the plan describes, and the spec should list each naming or layout difference as a deliberate change from the plan.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:41,370-395,423-427; tests/test_paths_models.py:24-26; tests/test_map_paths_contract.py:191_

**Q: The plan says the caller always passes the cap (review passes 21), and that an unscoped walkthrough keeps paths 'up to the cap'. The existing code defaults cap to 10, writes cap as null and ignores it on unscoped runs. Should cap be required, what should its default be, and should it apply to unscoped runs?**

Keep cap as a keyword argument defaulting to 10, which a test already pins, and check that it is at least 1 on scoped runs. Callers pass it explicitly anyway: review will pass 21. For PR 1, keep the existing contract: an unscoped run has no cap, writes cap as null and never produces a cap drop, because the schema's oneOf and the PathsResult validator both enforce this. That contradicts the plan's 'up to the cap' for walkthroughs, so record it as a known difference. PR 2 can add a cap for unscoped runs with a schema change if the walkthrough needs it.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:260-292,361-364,379; tests/test_map_paths_contract.py:100-107,191_

**Q: When several discoverers find the same entry, how should it be deduplicated and which kind should it keep? Examples: a [project.scripts] function that is also a `__main__` module, a SKILL.md `python -m pkg sub` that is also an argparse subcommand, or a caller seed that matches a code-found entry.**

Entries are merged when their handlers are the same `def` or `class`. The kept entry is the one with the best KIND_RANK (argparse, click, typer, main, script, skill_command, hook, seed, model, in that order), then the smallest location. The others go into its `also_invoked_by`. So a script that is also a __main__ module keeps `main`, a SKILL.md subcommand keeps `argparse`, and a seed that matches a code-found entry is folded into that entry. An entry with a null handler or a non-Python handler merges only with an entry of the same kind at the same location. Model-found entries never merge: a duplicate is dropped at stage model_fallback_duplicate.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/entries.py:58-61,1151-1185; paths.py:51-56,86-87_

**Q: Does the PR 1 design need to fit the hop shape PR 2 needs, given the walkthrough's Hop must move onto this? Specifically: should PathHop stay field-for-field the same as walkthrough_pipeline's Hop, and should the `places` (known edges) argument take the same (path, line) tuples the walkthrough's boundary.json gives today?**

Yes, it should fit. PathHop has the walkthrough Hop's six fields with the same names, types and order, plus one extra field, `side`, which is always "head". Keep it that way so PR 2 can map one onto the other directly. `places` should stay a sequence of Location objects rather than raw tuples, because Location checks the path. PR 2 converts boundary_places' `(path, line)` tuples with `Location(path=p, line=ln)`. The gate rule matches the walkthrough's: a place must fall inside the hop's line range.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:144-152; skills/codebase-walkthrough/scripts/walkthrough_pipeline/models.py:122-128; stages/trace.py:50-52; gates.py:212; code_facts/trace_gate.py:183-185_

**Q: How should changed lines be counted? (a) Does a pure deletion, which has no line at head, scope in any path, or is the change then invisible? (b) Is a line two hops of one path both cover counted once? (c) For a path broken at stops_at, do only the hops up to the last good one count? (d) How are ties in changed_lines broken so the order is deterministic?**

(a) The library only accepts head lines of 1 or more, so it cannot see a deletion. The caller should pass the head lines on either side of each deleted hunk, so that paths through the deletion still count. (b) Each changed line counts once: changed_lines_covered is a list of distinct locations, and the count is its length. (c) Only accepted hops count, since a path that was cut keeps only hops 1 to stops_at. (d) Sort by most changed lines covered, then by the existing path_order (fewest hops, then entry kind rank, then entry path and line).

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:199,343-358,431-434; tracing.py:168-175_

**Q: What exactly counts as 'the last effect' when an entry branches, for example a subcommand that writes a file on one branch and makes a network call on another? Is it one path per entry with the agent picking a branch, or can one entry produce several paths? And what hop limit (trace_limit) should stop recursion and cycles?**

Each entry gets exactly one linear path. The prompt says 'Never branch: follow the one this entry takes', and on a scoped run it asks the agent to go through the changed lines where the entry does. An answer that branches fails to parse, because the answer model forbids extra fields. The last effect is what the path writes, sends, prints or returns. Hops stop at HOP_LIMIT = 20: anything past that is cut and recorded as hop_limit, and a hop that repeats an earlier one breaks as repeated_hop, which stops cycles. trace_limit (default 30) is a different limit: on scoped runs it caps how many entries get traced, and the rest are dropped as over_trace_limit. Several paths per entry is out of scope for PR 1.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/prompts.py:136,171-176; trace_gate.py:3,46-64,162-163; paths.py:44,55,75,380_

**Q: The agents read repository content that could be attacker-controlled: SKILL.md text, hooks.json, code comments, test files. How should prompts and gates treat it, for example the existing UNTRUSTED_RULES and check_citation? Should a hooks.json or SKILL.md command pointing outside the repo, or using an absolute path, be dropped, recorded in diagnostics, or ignored without a record?**

Every trace and fallback prompt carries PATHS_UNTRUSTED_RULES: file text is data, and the agent must say so in the hop's note if it finds instructions. Whatever the caller passes, the agent's tools are forced to Read, Grep and Glob, with cwd set to the snapshot. Every hop is re-read through check_citation, and any path that escapes the root breaks as path_outside_root. No discovery or test-listing step executes repository code. A command that points outside the snapshot does not become an entry. It is recorded as an unresolved_target diagnostic naming the command, so it is neither silently ignored nor traced.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/prompts.py:31-35,126-127; tracing.py:61-66; trace_gate.py:6,129-150; commands.py:78-79; entries.py:1084-1094_

**Q: How strict should the e2e matcher be to avoid false positives? A function named `main` or a subcommand called `run` appears in many tests. Must a test name both the program and the subcommand to count? And since discovery is Python-only, what should JS/TS `*.test.*`/`*.spec.*` files match: only route URLs passed as seeds, or nothing in this PR?**

Be strict. For a subcommand, the program (script name, `-m` module, or an imported parser or main of the entry's module) and the subcommand token must appear in the same argv list or call. A direct_call or `main([...])` counts only when the test's imports resolve that name to the entry's handler def, never on a bare name match. Only patches whose target resolves to hop 2's symbol count as mocks. The existing match kinds are direct_call, cli_argv, cli_runner, subprocess and runpy; none of them is a URL kind. So JS/TS files should match nothing in PR 1. Record this as a deferred part of the plan, to add with route entries in a later PR.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/paths.py:54,155-184_

**Q: Beyond unit tests on fixtures, is there an acceptance check on a real repository? For example: running map_paths on this repo finds the expected CLI and skill-command entries such as `pr_pipeline edit`, or reproduces the PR #295 paths behind R-009/R-017/R-042/R-056. Or is fixture coverage plus green CI the whole definition of done for PR 1?**

The plan's 'Done means' is fixture tests plus green CI, with no real-repo acceptance check. A cheap check that needs no model is worth adding: run discover_entries on this repository and assert it finds `pr_pipeline edit`, which is declared at plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/__main__.py:53. Reproducing the PR #295 paths is not possible from this worktree, because INBOX/review-lessons-dynamic-workflows-2026-10-16.md is not here. That belongs with the review switch-over in a later PR.

_Grounded in: Plan 'Done means' section; plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/__main__.py:53_

## Assumptions

Nothing in the repo, the ticket or the context decides these; each answer is an assumption to confirm.

**Q: What limits apply to the model fallback? Should it run when code already found entries, is there a maximum number of entries it can add, and do model-found entries compete for the cap on equal terms with code-found ones, or are they ranked after them?**

This is not built yet: every run records the fallback as skipped. My proposal: run it once per run whenever fallback=True, even when code found entries, giving it the scope and the code-found entries as the plan says. Accept at most `cap` new entries. Drop any entry with a missing citation or one that does not re-read (model_fallback_citation stage), and any duplicate (model_fallback_duplicate stage). A failed call records status failed with a reason and the run carries on. Model entries compete on changed lines covered like any other entry, but they have the lowest KIND_RANK, so they come after code-found entries on a tie.
