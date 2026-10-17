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

1. The scope filter keeps a path only when a hop covers a changed line at head. What should happen to a change that only deletes or rewrites code, where the affected entry points reached base lines that no longer exist at head? Should paths be traced on the base side too (Resolve already takes a `Side`), or is head-only acceptable for PR 1?
2. For a module or tree walkthrough with no changed lines, what decides whether an entry 'reaches the scope'? Must some hop land inside the scope's spans, or is every discovered entry in the snapshot kept up to the cap? And which order decides what the cap cuts in that mode?
3. The cap is applied after tracing, so every discovered entry gets its own tracing agent. In a large CLI or skill repo that could be hundreds of model calls. Is there a cost or concurrency budget, or a cheap pre-filter before tracing (for example, static reachability or the files an entry's module imports), or is tracing every entry acceptable?
4. How should one entry with several branches be handled, such as a subcommand whose handler can write a file, call the network or print, depending on flags? Should the tracer produce one path per entry, one path per distinct last effect, or only the branch that crosses the changed lines? Is there a maximum hop count?
5. How should duplicates be resolved: the same entry found by both code and the model fallback, an argparse subcommand that is also listed in `[project.scripts]` or run by a SKILL.md command, or two entries whose paths share all hops after the first? Which one wins, and do the others go to `dropped` with a reason?
6. Is discovery deliberately Python-only (argparse/click/typer/ast) while the e2e search also covers JS/TS test files? Should hooks or `[project.scripts]` that point at shell or JS scripts become entries (and be traced), be skipped, or be recorded in `errors`?
7. The snapshot is described as one directory, but the walkthrough gate works over a dict of repos and code_facts' Resolve maps a path to a repo. Must `map_paths` and `paths.json` support multi-repo snapshots now, so PR 2 can move the walkthrough over, or only a single repo?
8. How strict should the e2e matching be? A function name or subcommand like `run` or `edit` will match many unrelated tests. Is it acceptable to over-report, or should a match require the CLI or module name too? And when hop 2 can't be identified (the path stopped at hop 1), how should mocked tests be classified?
9. SKILL.md files, hooks.json and repository source are attacker-reachable text that the discovery code parses and the fallback and tracing agents read. Should the new prompts reuse the existing UNTRUSTED_RULES as-is, and should discovery refuse or flag hook commands that point outside the repo or at unusual paths?
10. 'Done' lists tests for discovery, the gate, scope/cap, e2e and `map_paths`, but none for the model fallback (dropping an entry whose citation doesn't re-read) or for resume via `answers`. Are those required too? Should `paths.json` path ids and ordering be deterministic across reruns, including tie-breaks on equal `changed_lines`?

## Answers

**Q: For a module or tree walkthrough with no changed lines, what decides whether an entry 'reaches the scope'? Must some hop land inside the scope's spans, or is every discovered entry in the snapshot kept up to the cap? And which order decides what the cap cuts in that mode?**

The plan says 'every entry into the scope is kept up to the cap', so a path must still have at least one hop inside the scope's spans. A whole-file Scope makes this a simple check of the file. An entry that never enters the scope goes to `dropped` with reason 'does not reach the scope'. With no changed lines to rank by, order by the number of hops inside the scope (most first), then by entry kind in a fixed order (code-found before model-found), then by entry path:line. The cap cuts from the end of that order.

_Grounded in: Plan step 6: "For a module or tree walkthrough the caller passes no changed lines and every entry into the scope is kept up to the cap."_

**Q: The cap is applied after tracing, so every discovered entry gets its own tracing agent. In a large CLI or skill repo that could be hundreds of model calls. Is there a cost or concurrency budget, or a cheap pre-filter before tracing (for example, static reachability or the files an entry's module imports), or is tracing every entry acceptable?**

Concurrency is the caller's choice, as it is everywhere in code_facts (`bounded`/`run_tasks` take `concurrency`). The cost budget is enforced by the caller's CallFn: explore.py says the package never retries and lets budget errors propagate. Add a cheap deterministic pre-filter: before any tracing, drop entries whose module cannot statically reach a scope file through its transitive imports (an ast import graph), each with reason 'no import path to scope'. Also accept an optional `max_traces` limit, with the overflow recorded in `dropped`. This keeps discovery and filtering free of model calls, as the constraints require.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:38-58,133-142_

**Q: How should one entry with several branches be handled, such as a subcommand whose handler can write a file, call the network or print, depending on flags? Should the tracer produce one path per entry, one path per distinct last effect, or only the branch that crosses the changed lines? Is there a maximum hop count?**

Keep one path per entry, matching the walkthrough's one-scenario-per-seed model and the `CodePath.entry` shape. When there are changed lines, the prompt tells the tracer to follow the branch that crosses them. Otherwise it follows the main or default branch, and it names other last effects in the path's `title` or hop `note`. Cap the hops at a fixed constant (about 15) stated in the prompt. Hops beyond the cap are cut in code, with stops_at/break_reason 'hop limit'.

_Grounded in: plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py:97-114_

**Q: How should duplicates be resolved: the same entry found by both code and the model fallback, an argparse subcommand that is also listed in `[project.scripts]` or run by a SKILL.md command, or two entries whose paths share all hops after the first? Which one wins, and do the others go to `dropped` with a reason?**

Deduplicate entries before tracing, keyed by (path, line) of the definition, which follows the walkthrough's `taken`-set approach in pick_seeds. Code beats model. Among code kinds the more specific one wins (cli/subcommand over script over skill-command). The losers go to `dropped` as 'duplicate of <winner>', and their `invoked_by` text is merged into the winner's. Two paths that differ only in hop 1 are both kept, because different entries are what review needs.

_Grounded in: plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py:25-47_

**Q: Is discovery deliberately Python-only (argparse/click/typer/ast) while the e2e search also covers JS/TS test files? Should hooks or `[project.scripts]` that point at shell or JS scripts become entries (and be traced), be skipped, or be recorded in `errors`?**

Yes. The plan lists only Python parsers, plus SKILL.md and hooks config, so code discovery is Python-centred. The JS/TS test patterns are there for route URLs that the caller seeds. A hook whose command points at an in-repo script that is not Python (shell, JS) should still become a `hook` entry at line 1 of that script and be traced, because the agent can read any file. A hook whose target cannot be found in the repo goes to `dropped` with a reason, not to `errors`, which are kept for parse failures.

_Grounded in: Plan step 2: "hooks declared in hooks.json ... (`command` entries pointing at a script in the repo)"; "Python is parsed with `ast`; unparseable files are skipped and counted in `errors`."_

**Q: The snapshot is described as one directory, but the walkthrough gate works over a dict of repos and code_facts' Resolve maps a path to a repo. Must `map_paths` and `paths.json` support multi-repo snapshots now, so PR 2 can move the walkthrough over, or only a single repo?**

Yes. Support them through the existing abstraction. `snapshot` is the agents' working root, and every citation goes through the caller's `resolve(path, side)`. That already handles `<repo key>/<path>` for the walkthrough and plain repo-relative paths for a single repo. The gate must use only Resolve/Citation, not the walkthrough's `repos` dict. paths.json should record `head_sha` (and `base_sha` when there is one), following facts.v1's shape. Per-repo shas can wait for PR 2.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/models.py:3-6; plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:74-81_

**Q: SKILL.md files, hooks.json and repository source are attacker-reachable text that the discovery code parses and the fallback and tracing agents read. Should the new prompts reuse the existing UNTRUSTED_RULES as-is, and should discovery refuse or flag hook commands that point outside the repo or at unusual paths?**

Reuse UNTRUSTED_RULES unchanged in the fallback and trace prompts, as every walkthrough prompt already does. Agents stay limited to Read/Grep/Glob. Discovery only parses with ast/json/tomllib and never executes anything. A hook or script command whose resolved path leaves the snapshot (absolute, `..`, symlink out) is not made an entry. It goes to `dropped` with reason 'points outside the repo', so it is reported but never followed.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/prompts.py:23-26; plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/prompts.py:41_

**Q: 'Done' lists tests for discovery, the gate, scope/cap, e2e and `map_paths`, but none for the model fallback (dropping an entry whose citation doesn't re-read) or for resume via `answers`. Are those required too? Should `paths.json` path ids and ordering be deterministic across reruns, including tie-breaks on equal `changed_lines`?**

Yes to both. Add a test where a fake CallFn returns a fallback entry with a bad citation and the entry ends up in `dropped`. Add a resume test where pre-filled `answers` mean the fake CallFn is not called for those keys. Output must be deterministic, as gate_answers is: sort by changed_lines descending, then entry path, then line, then name. Assign ids p1.. after sorting, so reruns with the same answers produce identical paths.json.

_Grounded in: plugins/multiplai-dev/lib/code_facts/code_facts/explore.py:135-138,168-170_

## Assumptions

Nothing in the repo, the ticket or the context decides these; each answer is an assumption to confirm.

**Q: The scope filter keeps a path only when a hop covers a changed line at head. What should happen to a change that only deletes or rewrites code, where the affected entry points reached base lines that no longer exist at head? Should paths be traced on the base side too (Resolve already takes a `Side`), or is head-only acceptable for PR 1?**

Head-only is acceptable for PR 1. Paths are traced and gated at head. Each file that has only deletions should count as 'changed' at the head line next to the deletion, meaning the line the hunk header points at. Then a path whose hop spans that spot is still kept and the deletion does not disappear without trace. Base-side tracing (Citation.side='base' is already supported) is left to a later PR. When a change's scope has deletion-only hunks, record one line in `errors` or `dropped`, so a reader knows the deleted behaviour was not traced.

**Q: How strict should the e2e matching be? A function name or subcommand like `run` or `edit` will match many unrelated tests. Is it acceptable to over-report, or should a match require the CLI or module name too? And when hop 2 can't be identified (the path stopped at hop 1), how should mocked tests be classified?**

A match needs a qualifier as well as the bare name. A subcommand counts only when the same argv list, subprocess call or `main([...])` also names the program or module, or the test file imports the entry's module. A function call counts only when the test imports that function or its module. Under-reporting is safer here, because a false 'covered' hides a missing test. When there is no hop 2 (stops_at ≤ 1 or a single hop), no test can be classed as mocking it. Matching tests go to `e2e_tests`, and break_reason already shows the path is incomplete.
