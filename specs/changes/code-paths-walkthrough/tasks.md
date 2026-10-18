## 1. Trace through `map_paths` and render the Paths section for traced paths

`stages/trace.py` is rewritten so that `run_trace` returns early when `state.past("trace")` or `state.mode == "agent"`; otherwise it awaits `code_facts.map_paths` exactly once with these arguments:
- `root=ctx.snapshot_dir / state.target.repo_key`
- `commit=state.target.head_sha`
- `resolve=ctx.repo_resolver`
- `call=_trace_call(ctx)`, which forwards to `sdk.agent_call_structured` with `allowed_tools=sdk.TRACE_TOOLS`, the trace model and effort from `WalkConfig`, `cwd` set to the snapshot, and `budget_label="trace"`
- `changed_lines=module_lines(state, root)`, which maps each repo-relative target file to `set(range(1, n + 1))`, leaves zero-line files out and never returns `None`
- `seeds=[]`, `places=[]`, `cap=state.options.scenarios`, `trace_limit=TRACE_LIMIT` (30), `concurrency=ctx.config.concurrency`
- `cache_dir=ctx.run_dir / "paths-cache"`

The result is stored in the new `WalkState.paths`. The following are deleted: `pick_seeds`, the trace loop, `gates.trace_gate` and its tests, `prompts.trace_scenario`, the `Scenario` and `ScenarioHop` models, and the `trace_answers` and `scenarios` state fields. Old state files carrying those fields still load, because `_Model` ignores unknown keys.

`render.render_paths_section` produces the code-rendered **Paths** body at the section-list position "End-to-end scenarios" held in `stages/write.py`. The `<name>-walkthrough.json` step is retitled `Paths` with no other change to the v1 shape. The body has these parts:
- **Lead sentence:** `1 path reaches this module.` or `{N} paths reach this module.`; with no paths and no drops, `No entry points reaching this module were found.`
- **Path heading:** ``**p{n}** — `<entry name>` (<kind label>)``. The label comes from `KIND_LABELS`, and an unknown kind renders verbatim.
- **Entry bullet:** ``- Entry: invoked by `<invoked_by>` ``. If hop 1's range contains the entry line, it ends with ``at `p{n}h1` `` and no second link; otherwise it ends with `Linker.link` for the entry location.
- **Hop lines:** one line per hop in the form ``<a id="p{n}h{k}"></a>`p{n}h{k}` [`<repo key>/<path>:<start>-<end>`](<url> "<quote>")``.
- **Stop bullet:** ``- Stops after `p{n}h{k}`: `<break_reason>` `` for a cut-short path, or ``- Ends at `p{n}h{last}` (trace complete).``
- **E2E tests bullet:** counted `E2ETest` ids are listed. The literal `no e2e test` appears when there are none. Each `MockedE2ETest` from `tests.mocks_hop_2` follows `not counted:` with the label ``(mocks `p{n}h2`, not counted)``.

The coverage `Scenarios cut short` row becomes `Paths traced` and `Paths cut short`. In addition:
- `SKILL.md` stage 5 is rewritten for `map_paths`.
- multiplai-dev is bumped to `0.81.0` in `marketplace.json`, with a `CHANGELOG.md` entry that later blocks extend.
- `KIND_LABELS` keys are restricted to `paths.v1` kind values, which keeps the spec deviation "Unknown invoker" honest: a test asserts that `PathEntry` rejects a missing `invoked_by`, and another asserts that every rendered Entry bullet has a non-empty invoker. No "unknown" fallback branch is built.

To exercise the slice, run `uv run --project plugins/multiplai-dev/skills/codebase-walkthrough/scripts --package walkthrough-pipeline python -m pytest` against these `tmp_path` git fixtures (fixed author) with a monkeypatched recording fake for `sdk.agent_call_structured`:
- **Click-CLI fixture:** produces a Paths heading with a `p1h1` anchor and a click-command kind label, and the result order matches `map_paths` order.
- **SKILL.md `python -m` fixture:** produces a skill-command path.
- **Writer output with no Paths section:** the section still renders.
- **Agent mode:** `map_paths` is never called, no Paths heading or `p\d+h\d+` id appears, and nothing is written under `paths-cache`.
- **Resume after 3 of 5 cached answers:** makes exactly 2 calls.
- **Fully cached resume:** makes 0 calls.
- **Saved state:** has no `trace_answers` or `scenarios` key.
- **Hop ids:** identical across two renders of the same result.

Interfaces:
- Produces: `async def run_trace(state, ctx) -> WalkState`
- Produces: `def module_lines(state: WalkState, root: Path) -> dict[str, set[int]]`
- Produces: `def _trace_call(ctx) -> code_facts.CallFn`
- Produces: `TRACE_LIMIT: int = 30`
- Produces: `WalkState.paths: code_facts.PathsResult | None = None`
- Produces: `def render_paths_section(state: WalkState, linker: Linker) -> str`
- Produces: `KIND_LABELS: dict[str, str]`

Satisfies:
- walkthrough-path-tracing:
  - Trace stage delegates path finding to code_facts (Trace stage calls map_paths; Removed walkthrough tracing code is gone)
  - Module scope is passed as changed lines (all scenarios)
  - Non-web entry points are traced (CLI fixture repository; Skill-command fixture repository; Repository with no web entries)
  - code_facts ranking order is preserved (all scenarios)
  - Trace answers are cached under the run directory (all scenarios)
  - Model calls are wired like the explore stage (Read-only tool set; Trace limit unchanged; Spend counted against the breaker)
  - Agent mode does not trace
- walkthrough-paths-section:
  - Paths section is mandatory in human mode
  - Paths section replaces "End-to-end scenarios"
  - Agent mode has no Paths section
  - Paths are listed in code_facts rank order (Order preserved)
  - Each path states its entry point, kind and invoker (CLI entry described; Unknown invoker restated as invoker always present)
  - Hops are rendered as path:line links with stable ids
  - Each path states where and why the trace stopped
  - Each path lists its e2e test coverage
  - Empty Paths section explains why (No entries found)

## 2. Cap paths with `--scenarios` and list left-out entries

In `__main__.py`, `--scenarios` keeps its name and changes as follows:
- it uses `type=_positive_int`, so `0` or a negative value raises `argparse.ArgumentTypeError`, exits with status 2 and an error naming `--scenarios`, and runs no stage;
- `default=21`;
- `help="Cap on the number of paths traced (default: 21)."`

Its value flows unchanged to `map_paths(cap=...)`.

`render.render_left_out` adds a `### Left out` subsection under Coverage, rendered only when it has at least one row, with columns `| Entry | Stage | Reason | Detail |`. Rows appear in this order:
1. `result.drops` in `map_paths` order, with `PathDrop.stage` and `PathDrop.reason` verbatim. This includes `unreachable`, `over_cap`, the trace-limit reason, and reasons the walkthrough does not know.
2. `seed_not_found` and `dynamic_name` diagnostics, with Stage `discover`.
3. Listing skips, with Stage `e2e` and Reason `e2e_listing`.

Entries are plain backticked text, never links:
- `<repo key>/<path>:<line>` when a line is known;
- `<repo key>/<path>` when `PathDiagnostic.line` is null, or the test id for a `ListingSkip`.

In the Detail column, `|` is escaped.

`left_out_count` counts `result.drops` plus `seed_not_found` diagnostics, but not `dynamic_name` or `e2e_listing` rows. It drives:
- the new Measure row `Entries left out`;
- the lead-sentence suffix ` {M} entries were left out; see **Left out** under Coverage.`;
- the all-dropped lead `No paths were traced. {M} entries were left out; see **Left out** under Coverage.`;
- the progress line `trace: <N> paths, <C> cut short, <M> left out`.

To exercise the slice:
- a 25-click-command fixture with the default cap traces 21 paths, shows 4 `over_cap` rows, and states `4 entries were left out`;
- `--scenarios 3` gives at most 3 paths;
- `--help` shows the new text;
- `--scenarios 0` exits non-zero before any stage runs;
- a fake result with a `seed_not_found` diagnostic, an `unreachable` drop and an unknown reason string renders all three rows unchanged.

Interfaces:
- Produces: `def _positive_int(value: str) -> int`
- Produces: `def render_left_out(result: code_facts.PathsResult, repo_key: str) -> str`
- Produces: `def left_out_count(result: code_facts.PathsResult) -> int`
- Consumes: Block 1: `async def run_trace(state, ctx) -> WalkState`
- Consumes: Block 1: `def render_paths_section(state: WalkState, linker: Linker) -> str`

Satisfies:
- walkthrough-path-tracing:
  - --scenarios caps the number of paths (Default cap; Explicit cap; Help text; Invalid cap)
  - Entries over the cap are reported
  - Every dropped entry appears in coverage with stage and reason (seed_not_found as a diagnostic; unreachable; Reason unknown to the walkthrough)
  - code_facts ranking order is preserved (No walkthrough-side filtering)
- walkthrough-paths-section:
  - Left-out entries are shown in the coverage table (Over-cap drops visible; Seed not found and unreachable drops visible)
  - Empty Paths section explains why (All entries dropped)

## 3. Pass web entry points as caller seeds and boundary edges as known places

`web_seeds` builds `code_facts.Seed` objects from the four boundary-entry kinds, in the old kind order: webhook routes, routes called from other repositories, Celery beat entries, then other routes. Only target-repo definitions become seeds, so no seed path leaves `root`.

**Location.** Each seed is located at the handler `def` as `Seed(path=<handler file>, symbol=<handler name>)`. If the handler is unresolved, it falls back to the call line into the module, then to the route registration line. This deviates from the spec's import-line scenario, and the test asserts that the handler is in `api/hooks.py`.

**Name.** `Seed.name` depends on the entry kind:

| Entry kind | `Seed.name` |
|---|---|
| Webhook route or other route | `<METHOD> <path>` |
| Route called from another repository | `<other repo key>: <METHOD> <path>` |
| Celery beat entry | `<task name> (beat)` |

**Known places.** `known_places` returns the target-repo side of every cross-repository reference, route and task, with the repo key stripped. Duplicates are dropped in first-seen order, places that exist only in another repo are left out, and the function returns `[]` when there are none. `run_trace` passes both lists as `seeds=` and `places=`.

To exercise the slice, use a web fixture with one route importing the module, one route reaching it only over HTTP, and one beat task:
- the recorded `map_paths` kwargs show 3 caller seeds at handler `def`s and 4 known places (1 cross-repo reference, 2 routes, 1 task);
- the HTTP-only seed appears either as a traced path or as a `seed_not_found` or `unreachable` row in Left out;
- every web entry is either traced or `over_cap`, provided no more than 30 entries are eligible; past that, the trace-limit reason is shown verbatim;
- a fixture with no boundary edges calls `map_paths` with `seeds=[]` and `places=[]` and does not raise;
- a CLI entry ranked before a web seed renders first.

Interfaces:
- Produces: `def web_seeds(state: WalkState) -> list[code_facts.Seed]`
- Produces: `def known_places(state: WalkState) -> list[code_facts.Location]`
- Consumes: Block 1: `async def run_trace(state, ctx) -> WalkState`
- Consumes: Block 2: `def render_left_out(result: code_facts.PathsResult, repo_key: str) -> str`

Satisfies:
- walkthrough-path-tracing:
  - Web entry points are passed as caller seeds (Route that imports into the module, amended to the handler location; Seed reaching the module only over HTTP or a queue; Web entries traced before the change are still traced)
  - Boundary edges are passed as the gate's known places (all scenarios)
- walkthrough-paths-section:
  - Paths are listed in code_facts rank order (Web seeds not promoted)

## 4. Contain trace failures: per-entry drops, whole-stage errors, fatal latch and legacy state

**Fatal latch.** `_trace_call` gains a `_FatalLatch`:
- before each call, it re-raises `latch.exc` if set;
- it stores and re-raises `RepoTrustError` or `BudgetExceededError`.

**`run_trace` order.** `run_trace` does the following:
1. calls `budget.check(stage="trace")` before `map_paths`;
2. after `map_paths` returns or raises, re-raises `latch.exc` first, so only those two exception types ever escape;
3. on any other exception, appends `trace: <ExceptionType>: <message>` to `state.errors`, sets `state.paths = None` and `state.paths_error = "<ExceptionType>: <message>"`, logs `trace: failed (<ExceptionType>)`, and returns so that write and render still run.

**Rendering and resume.**
- The Paths lead sentence renders `Path finding failed: {state.paths_error}`.
- `sdk.AgentCallError` is not latched: it surfaces as a `map_paths` drop whose reason is rendered verbatim in Left out.
- On `resume`, a human-mode state past `trace` with `paths is None` and `paths_error is None` is rewound to the stage before `trace`, so it re-traces instead of claiming "No entry points".

To exercise the slice:
- **Per-entry failure:** a fake raising `AgentCallError` on entry E2 yields traced E1 and E3, an E2 row in Left out, and a run that continues.
- **Whole-stage failure:** a `map_paths` monkeypatched to raise `RuntimeError` still writes the walkthrough, with the error in `state.errors` and in the Paths section.
- **Fatal stops:** a fake raising `BudgetExceededError` on entry 2, or a resolver raising `RepoTrustError`, stops the run with today's exit behaviour; no later stage runs, and answers cached so far stay under `paths-cache/paths-answers/`.
- **Legacy state:** a legacy state file past `trace` re-runs path finding on resume.

Interfaces:
- Produces: `@dataclass class _FatalLatch: exc: BaseException | None = None`
- Produces: `def _trace_call(ctx, latch: _FatalLatch) -> code_facts.CallFn`
- Produces: `WalkState.paths_error: str | None = None`
- Produces: `def rewind_legacy_trace(state: WalkState) -> WalkState`
- Consumes: Block 1: `async def run_trace(state, ctx) -> WalkState`
- Consumes: Block 1: `def render_paths_section(state: WalkState, linker: Linker) -> str`
- Consumes: Block 2: `def render_left_out(result: code_facts.PathsResult, repo_key: str) -> str`

Satisfies:
- walkthrough-path-tracing:
  - A trace failure for one entry becomes a drop
  - Whole-stage failure is recorded and does not stop the run
  - Trust and budget stops still halt the run
  - Trace answers are cached under the run directory (Resuming state written before the change)
- walkthrough-paths-section:
  - Left-out entries are shown in the coverage table (Per-entry trace failure visible)
  - Empty Paths section explains why (Whole-stage failure; Fatal errors still stop the run)

## 5. Let writers cite path hops by id

**Citation index.** In `models.py`, `citation_index` replaces `s<N>h<M>` ids with `p{n}h{k}` ids, each mapped to its `PathHop` with the repo key prefixed.

**Writer prompts.** `stages/write.py` adds a "Path hops" prompt block. It lists every hop id with its `<repo key>/<path>:<start>-<end>` and quote, and instructs writers to cite hops as `[[p{n}h{k}]]`.

**Write gate.** The write-gate id regex widens to `c\d+|f\d+|p\d+h\d+`. An unknown hop id is rejected through the same invalid-citation path as an unknown `cN`, and is never rendered as a link.

**Rendering.** A valid citation renders as `` [`<repo key>/<path>:<start>-<end>`](<url> "<quote>") ([p{n}h{k}](#p{n}h{k})) ``.

To exercise the slice, use the fake writer:
- a writer citing `[[p1h2]]` renders a link to path 1 hop 2 (e.g. `svc/handler.py:57`) followed by `([p1h2](#p1h2))`;
- a writer citing `[[p9h1]]` with no path 9 is reported as an invalid citation, with no link rendered;
- the captured prompt text contains every assigned hop id with its `path:start-end`.

Interfaces:
- Produces: `def citation_index(state: WalkState) -> dict[str, Citation | code_facts.PathHop]` (adds `p{n}h{k}` keys, removes `s<N>h<M>` keys)
- Produces: `def hop_prompt_block(result: code_facts.PathsResult, repo_key: str) -> str`
- Produces: `CITATION_ID: re.Pattern[str] = re.compile(r"c\d+|f\d+|p\d+h\d+")`
- Consumes: Block 1: `WalkState.paths: code_facts.PathsResult | None = None`
- Consumes: Block 1: `def render_paths_section(state: WalkState, linker: Linker) -> str`

Satisfies:
- walkthrough-paths-section: Writers cite hops by id (Valid hop citation resolved; Unknown hop citation rejected; Prompt includes hop ids)

## 6. Write `<name>-paths.json` beside the walkthrough

**Target path.** `render.paths_file_for` returns `md.with_name(md.name.removesuffix("-walkthrough.md") + "-paths.json")`.

**Writing, in human mode only.** After `<name>-walkthrough.md` is written:
- **If `state.paths` is set:** call `code_facts.write_paths_file(state.paths, target)`. On `OSError` or `ValueError`, append `paths file <target>: <ExceptionType>: <message>` to `state.errors` and unlink the target with `missing_ok=True`.
- **If `state.paths is None`:** unlink any stale target.
- **Agent mode:** never call the writer and never touch the file.

The written path is appended to `run_state`'s returned outputs after the Markdown path and printed with the existing `walkthrough:` line format. No walkthrough code builds `paths.v1` JSON itself.

`SKILL.md` documents `<name>-paths.json`: written beside `<name>-walkthrough.md`, `paths.v1` schema, human mode only, and not read by `check` or buildme. The `0.81.0` `CHANGELOG.md` entry is extended to match.

To exercise the slice:
- **Schema and alignment:** a CLI and skill-command fixture run writes `out/billing-paths.json`, which validates against `paths.v1.schema.json`. Path n, hop k matches the Markdown `p{n}h{k}` hop (repo-relative in JSON, repo-key-prefixed in Markdown), and the path count and e2e and mocked test lists agree.
- **Empty result:** an empty `map_paths` result still writes a valid file with an empty paths list.
- **Left-out entries:** over-cap entries appear as `over_cap` drops, an unlocatable web seed appears under diagnostics as `seed_not_found` (the flagged spec deviation from "drop"), and an `AgentCallError` entry appears as a drop.
- **Failures and stale files:** a `write_paths_file` monkeypatched to raise `OSError` still yields the Markdown and an error entry. A whole-stage failure removes a stale file.
- **Agent mode:** no file is written.
- **Re-runs:** a re-run after the module changes overwrites the file, and a resume from fully cached answers makes no trace calls and writes a byte-identical file.
- **Walkthrough JSON:** `<name>-walkthrough.json` still validates against v1 and has no field referring to the paths file.
- **buildme:** buildme's `walkthroughs.py` scan of `x-walkthrough.md` and `x-paths.json` considers only the Markdown.

Interfaces:
- Produces: `def paths_file_for(md: Path) -> Path`
- Produces: `def write_paths_output(state: WalkState, md: Path) -> Path | None`
- Consumes: Block 1: `WalkState.paths: code_facts.PathsResult | None = None`
- Consumes: Block 4: `WalkState.paths_error: str | None = None`

Satisfies:
- walkthrough-paths-file:
  - Paths file is written in human mode
  - Paths file is produced by the shared writer
  - Paths file conforms to the paths.v1 schema
  - Paths file content matches the Paths section
  - Paths file records dropped entries (with seed_not_found recorded under diagnostics)
  - Paths file is not written in agent mode
  - Whole-stage failure writes no paths file
  - Paths file write failure is reported without losing the walkthrough
  - Paths file is rewritten on re-run and resume
  - Paths file is not consumed by check or buildme (buildme ignores paths files)
  - Walkthrough JSON shape is unchanged by the paths file
  - Paths file is documented

## 7. Report hop ids and unresolved hop citations in `check --at-head`

`check.py` keeps reading the Markdown only, with no JSON, run-directory or model access, and changes in four ways:
- **Hop prefix.** A link on the same line as `<a id="(p\d+h\d+)"></a>` belongs to that hop, and its failure line becomes `p{n}h{k} <repo>/<path>:<start>-<end>: <reason>`.
- **Unresolved citations.** Every `[pNhK](#pNhK)` with no matching anchor in the document fails as `unresolved hop citation pNhK`, counting one failure.
- **Hop count.** The stdout line `check: N checked, M failed` stays byte-for-byte unchanged. When at least one anchor exists, stderr gets `check: N hops checked`, where N is the anchor count.
- **No Paths requirement.** A missing or anchor-free Paths section is never a failure.

The existing heading-agnostic link checking is pinned by regression tests in this block, covering legacy "End-to-end scenarios" documents, agent-mode documents, and Paths and scenario hops that cite the same `path:line` and get the same verdict. The `0.81.0` changelog entry is extended.

To exercise the slice, run `uv run --project plugins/multiplai-dev/skills/codebase-walkthrough/scripts --package walkthrough-pipeline python -m pytest` on committed git fixtures covering:
- an unchanged section with `p1h1`, `p1h2` and `p2h1`, which passes with today's success status;
- a 21-path section, whose stderr count equals its anchor count;
- an edited line, which prints `p2h3`;
- a deleted file, which names `p1h1`;
- a line past end of file;
- one bad hop among many, where only that hop is reported;
- an absent or corrupted `paths.json`, which gives an identical verdict;
- a fresh clone with no run directory and no model credentials, where no agent call is attempted;
- an unresolved `p4h1` alongside a resolved `p1h2`;
- the "No paths found" and "Path finding failed" sections;
- legacy documents, passing and with a moved line;
- an agent-mode document, with no stderr hop line;
- a mutation test that disables hop-anchor handling and asserts the moved-hop test then fails.

Interfaces:
- Produces: `HOP_ANCHOR: re.Pattern[str] = re.compile(r'<a id="(p\d+h\d+)"></a>')`
- Produces: `HOP_REF: re.Pattern[str] = re.compile(r"\[(p\d+h\d+)\]\(#(p\d+h\d+)\)")`
- Produces: `def unresolved_hop_citations(markdown: str) -> list[str]`
- Consumes: Block 1: `def render_paths_section(state: WalkState, linker: Linker) -> str`
- Consumes: Block 5: `CITATION_ID: re.Pattern[str] = re.compile(r"c\d+|f\d+|p\d+h\d+")`

Satisfies:
- walkthrough-path-hop-check:
  - Path hops are re-checked at HEAD (all scenarios)
  - Moved or removed hop lines fail the check (all scenarios)
  - Hop re-check uses the Markdown alone (all scenarios)
  - Hop citations resolve to defined hops (all scenarios)
  - Paths sections with no hops pass (all scenarios)
  - Legacy scenario walkthroughs still pass (all scenarios)
  - Agent-mode walkthroughs are unaffected
  - Hop re-check is covered by the test suite (Suite runs in CI form; Moved-hop test detects a regression)
- walkthrough-paths-file: Paths file is not consumed by check or buildme (Check ignores a missing paths file; Check ignores a corrupted paths file)