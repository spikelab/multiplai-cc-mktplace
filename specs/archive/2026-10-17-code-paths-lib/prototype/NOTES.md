# Prototype notes: code-paths-lib

## What this is

`sample-output.json` is a hand-written `paths.json` (paths.v1) for one scoped
`map_paths` run on this repository's own `pr` skill. Every cited line, quote,
test and hook comes from the live tree. Only the commit sha, the fallback answer
and the model's notes are made up. It is the document `write_paths_file` would
write for this call:

```python
await map_paths(
    root=<git-archive snapshot of the repo root>, commit="9c41e07b…",
    resolve=..., call=..., cache_dir=...,
    changed_lines={
        "plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/commands.py": [188, 192],  # cmd_open draft/title
        "plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/github.py": [270],         # run_write body
    },
    seeds=[{"path": "plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/github.py", "symbol": "run_write"}],
    cap=2,   # set low on purpose, to exercise over_cap
)
```

How to read it:
- **Top level:** the nine fields from D16, in D16 order.
- **Entry and path fields:** in Global-Constraints order.
- **`dropped`:** sorted by stage, then path, line, kind and name.
- **Accounting:** E = 12, made of 4 pr entries, 4 shown unreachable entries, 1 seed and 3 fallback items. The output keeps 2 paths and drops 10, so it balances. A real run would also list about 30 more `import_reachability/unreachable` drops: the other skills' argparse subcommands, `__main__` modules and SKILL.md commands. I left them out because they all look the same.

## What drawing it showed

1. **`Hop` parity (D4) doesn't work as written.** The walkthrough's `Hop` is flat: `symbol_called, path, line_start, line_end, quote, note` (`walkthrough_pipeline/models.py:122-128`). It has no `citation` field and no `side`. So "same field names" and "citation typed as `Citation`" can't both be true. The sample nests `citation`, because `base_side_citation` needs `side` and `check_citation` needs a `Citation`. Nesting causes three more problems:
   - `code_facts.models.Citation` is `extra="ignore"` and not frozen, so `hop.citation.line_start = 9` succeeds. That breaks the "assigning any attribute raises" test.
   - Its `id` field leaks into every hop as `"id": ""`.
   - The schema then has to declare `id`.
   
   Fix: add a frozen, `extra="forbid"` output citation model (for example `HopCitation(Citation)`, with or without `id`). The parity test should compare `{symbol_called, note}` plus the citation's fields.
2. **Every hook collapses into one entry.** Each command in `plugins/multiplai-context/hooks/hooks.json` is `sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "<script>.py"`. Under D6 handler rule 4, every handler becomes `run.sh:1`. The D7 merge key then joins all 8 hooks into one `hook_command` entry, and the real Python script argument is ignored. D6 also never says how a hook command string is split into tokens, or what a hook entry's `name` is. The sample uses the event name `Setup`.
3. **A package `__main__` anchored at line 1 can't be traced.** `pr_pipeline/__main__.py:1` is a docstring line, and no known edge leaves it. So hop 2 can never connect unless hop 1 cites the whole file.
   - Also, the "single called function" rule fails on this repo's own `__main__`, which calls `pin_session_settings` and `main`. The handler is therefore `None`.
   - The SKILL.md command merges into this dead entry, so the skill invocation is also untraceable. See the `main_module` drop and the diagnostic at `:104`.
   - Fix: anchor at the `if __name__` line, or add a known edge from line 1 to that block. Pick the call that `sys.exit(...)` wraps, or the last call.
4. **The connectivity rule forces hops as long as whole functions.** Known edges land on `def` lines, and hop n+1 must be named inside hop n's cited range. So a handler hop has to span from its `def` to the call site:
   - `cmd_open` is 180–213, 34 lines.
   - `cmd_edit` would be 266–383, 118 lines, to reach `run_write`. The `edit` path in the sample breaks on exactly this (`unconnected_hop`).
   
   The gate needs a "same enclosing def" connection, using `SnapshotIndex`, or hops that allow a `def` line plus a call line.
5. **E2E listing finds almost nothing in the motivating repo.**
   - `test_commands.py` has about 15 real end-to-end tests. All of them go through a module-level helper, `run(argv) -> commands.run(build_parser().parse_args(argv))`, which D15a does not see.
   - `test_spend.py` passes `argv` in through `parametrize`.
   - The only test listed for p1 (`test_open_prints_spend_when_it_cannot_start`) exits at `load_env` before it reaches `cmd_open`.
   - `test_spend.py:106` patches `commands.run`, which is the real dispatcher. It doesn't count as "mocks hop 2" because the add_parser→handler edge skips `run`.
   
   At minimum, one level of same-module helper should be inlined.
6. **The add_parser edge skips the real runtime path, and the fallback brings it back as a near-duplicate.** At runtime the path is `__main__:104 → main:70 → _main:81 → commands.run:403 → :409 → cmd_open`. p1 jumps from `add_parser("open")` straight to `cmd_open`. The model fallback then proposes `commands.run`, and its path, p2, repeats p1's hops 2–5 exactly. Both cap slots go to one route, and the seed the caller asked for is cut by `over_cap`.
7. **The import graph has to include imports inside functions.** `__main__.py`'s only import of `commands` is `from . import commands` inside `_main` (line 93). If D13 only scans module-level imports:
   - D6 rule 3 (a dispatch dict reachable through the import graph) fails, and `open`/`edit`/`render` lose their handlers.
   - The pr entries also become unreachable from `commands.py`.
   
   The design doesn't say which way it goes.
8. **SKILL.md uses a placeholder for the subcommand.** The command is `python -m pr_pipeline <command> ...`, so the "first positional selects the subcommand" rule gets the literal `<command>`. D6 doesn't say what happens then. The sample emits `unresolved_target` and falls back to the module.
9. **Drop records lose the structure.** `EntryRef` has no `also_invoked_by`, `stops_at` or `break_reason`. The 8-hook merge and the cut details survive only in the free-text `detail`.

PROVES: The exact paths.v1 document. That means the nine top-level fields in order, the entry, path and hop field order, how `stops_at`/`break_reason` read on kept and dropped entries, the `tests.e2e`/`tests.mocks_hop_2` record shape, the `{stage, reason, detail, entry}` drop shape with its sort order, and the accounting invariant on a real run. It also shows that D6 rule 3 resolves the live pr_pipeline `open`/`edit`/`render` to `cmd_*` through `COMMANDS` and the `commands.py:409` edge, and that a CLI repo yields real traced paths with citations that re-read.
DISPROVES: (1) D4's Hop parity: the walkthrough Hop is flat and has no citation or side, and a nested `code_facts.Citation` is neither frozen nor forbid and leaks `id`. (2) D6 hook rule 4 plus the D7 merge key collapse all 8 real `sh run.sh <script>.py` hooks into one entry. (3) D6 `__main__` anchoring at line 1, plus the "single called function" rule, makes pr_pipeline's main_module and the SKILL.md command merged into it untraceable. (4) The D11 connectivity rule forces hops as long as the function (118 lines for cmd_edit), and short citations break with `unconnected_hop`. (5) D15a's literal-only matching lists none of the ~15 real pr e2e tests, which go through a `run(argv)` helper, and lists one that never reaches the handler. (6) D13 says nothing about function-local imports, which the live pr_pipeline needs for D6 rule 3 and for reachability.
OPEN_QUESTIONS: Are seeds exempt from `cap`? Should paths that share an identical hop suffix (p1 and p2 here) be deduplicated, or should the fallback treat "reaches a known handler via a known edge" as `duplicate`? What is a hook entry's `name`, and what is an argparse entry's `invocation` (null, or `python -m pr_pipeline open`)? Should drop records carry structured `stops_at`/`break_reason`/`also_invoked_by` instead of free text? Should `Citation.id` appear in hop output? Is it intended that each skill's `scripts/pyproject.toml` makes it a separate "repository" for module resolution, so that "same-repository" means "same skill"?
STATUS: DONE_WITH_CONCERNS
