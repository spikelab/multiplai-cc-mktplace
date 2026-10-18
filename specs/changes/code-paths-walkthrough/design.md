# Design: walkthrough path tracing through `code_facts.map_paths`

## Context

**What exists today.** The trace stage is `stages/trace.py` in the codebase-walkthrough pipeline. It picks web seeds itself with `pick_seeds`, in this order: webhook routes, routes called from other repositories, Celery beat entries, then other routes. It traces at most `--scenarios` of them, which defaults to 6. It gates each answer with the walkthrough's own `trace_gate` in `gates.py`. That gate requires hop 1 to contain the seed line, and requires each later hop's `symbol_called` to appear in the previous hop's quote. Results are stored in two state fields: answers in `trace_answers` and gated scenarios in `scenarios`.

Hop citations get ids of the form `s<N>h<M>` through `citation_index` in `models.py`. The write stage renders an "End-to-end scenarios" section. The coverage table has two columns, `Measure | Value`, and includes a `Scenarios cut short` row that reads `state.scenarios`.

`check --at-head` (`check.py`) re-reads every `path:start-end` link in the Markdown, whatever heading it sits under. It prints `check: N checked, M failed` on stdout, and `__main__.py` documents that line as a contract.

**What PR 1 (#374) added.** `code_facts` now provides:

- `async map_paths(*, root, commit, resolve, call, changed_lines=None, seeds=(), places=(), cap=10, trace_limit=30, concurrency=4, cache_dir=None, fallback=True, ...) -> PathsResult`, where:
  - `root` is the snapshot to scan;
  - the only writes go under `cache_dir`, and answers land in `<cache_dir>/paths-answers/`.
- `write_paths_file(result, path)`. It validates against `paths.v1.schema.json`, raises `ValueError` naming the JSON pointer on an invalid document, then writes atomically.

The `paths.v1` output models forbid unknown keys and are frozen. Facts the prototype confirmed about them:

- `PathEntry.invoked_by` is required and is built by code.
- A cut-short path sets `stops_at` and `break_reason` together.
- Every `MockedE2ETest` lives in `tests.mocks_hop_2`.
- `PathDiagnostic.line` can be `null`.
- `ListingSkip` has no line.
- `seed_not_found` is reported as a **diagnostic**, not as a `PathDrop`.

Ordering:

- Kept paths are sorted by most changed lines covered, then fewest hops, then entry kind rank, then location.
- Drops are sorted by stage order, then location.
- `discover_entries` raises `ValueError` for a seed path that is absolute or leaves `root`.
- Agents run by `map_paths` get only Read, Grep and Glob, whatever `call_kwargs` say.
- `map_paths` has **no** `fatal=` argument.

**Stage contract.** `stages/__init__.py` says only `RepoTrustError` and `BudgetExceededError` escape a stage, and every other failure goes to `state.errors`. The orchestrator saves state after each stage. `WalkState` uses `_Model`, which ignores unknown keys, so old state files with `trace_answers` or `scenarios` load without error.

**What the prototype showed.** Before this design, a prototype built the Paths section, the hop anchors and the `check` changes end to end. The Markdown shape below is the one it produced. Where an earlier draft disagreed with the prototype, the prototype wins. Those corrections are folded into D3, D7, D8, D9, D11 and D12.

## Decisions

### D1. `run_trace` calls `map_paths` exactly once and owns nothing else

**Decision.** `stages/trace.py` keeps a single entry point, `async def run_trace(state, ctx) -> WalkState`. It returns early when `state.past("trace")` is true or when `state.mode == "agent"`. Otherwise it builds the arguments below and awaits `code_facts.map_paths` once.

```python
result = await code_facts.map_paths(
    root=ctx.snapshot_dir / state.target.repo_key,   # target repo snapshot
    commit=state.target.head_sha,
    resolve=ctx.repo_resolver,                        # same resolver explore uses
    call=_trace_call(ctx, latch),                     # D5
    changed_lines=module_lines(state, root),          # D2
    seeds=web_seeds(state),                           # D3
    places=known_places(state),                       # D4
    cap=state.options.scenarios,                      # D6
    trace_limit=TRACE_LIMIT,                          # 30
    concurrency=ctx.config.concurrency,
    cache_dir=ctx.run_dir / "paths-cache",
)
```

The following are deleted:

- `pick_seeds`;
- the trace loop;
- `gates.trace_gate` and its tests;
- `prompts.trace_scenario`;
- the `Scenario` and `ScenarioHop` answer models.

`code_facts` owns trace prompts, gating and ranking. This covers the spec scenarios "Trace stage calls map_paths" and "Removed walkthrough tracing code is gone".

**Alternatives considered.**

- *Keep `pick_seeds` and use `map_paths` only for non-web entries.* This leaves two tracers and two gates, which is the duplication the proposal removes.
- *Call `map_paths` once per seed kind.* This breaks the single ranking and the single cap, and costs N index builds.

**Why chosen.** One call gives one ranking, one cap and one cache. It is the integration point PR 1 built, and it mirrors how `stages/explore.py` delegates to `code_facts.explore`.

### D2. Module scope as `changed_lines`

**Decision.** `module_lines(state, root) -> dict[str, set[int]]` builds the scope:

- It iterates the target file list that `list_target_files` recorded in state. That is the explained file set, which already excludes lockfiles, generated files, binaries and counted migrations.
- For each file it counts lines in the **snapshot**, not the working tree, and maps the repo-relative path to `set(range(1, n + 1))`.
- Zero-line files are left out.
- Keys are repo-relative, with the repo key stripped, because `root` is the target repo snapshot.
- The function always returns a dict and never `None`. A module whose files are all empty yields `{}`, which is still a scoped run.

This covers "Every line of every module file is in scope", "None is never passed", "Empty files in the module" and "Files outside the module are not scoped".

**Alternatives considered.**

- *`changed_lines=None`.* This would be an unscoped run that traces every entry in the repo, and the proposal forbids it.
- *Python files only.* Rejected because `code_facts` already ignores non-Python lines for coverage, and filtering here would duplicate that knowledge.

**Why chosen.** This is the literal meaning of "the module being explained", and it reuses the file list the partition gate already validated.

### D3. Web seeds become caller `Seed`s, located at the handler `def`

**Decision.** `web_seeds(state) -> list[Seed]` takes the four boundary-entry kinds `pick_seeds` used to select and keeps today's kind order. It builds seeds **only from entries whose definition is in the target repo**:

- Routes, webhook routes and Celery beat entries are defined there.
- A "route called from another repository" is a target-repo route, and the other repo only names its caller.

This invariant keeps every seed path under `root`. `discover_entries` would raise `ValueError` for a path outside it.

**Seed location (open question c).** The seed's location is the **route or task handler `def`**, as `Seed(path=<handler file>, symbol=<handler name>)`. The import line is used only to pick which file holds the place that reaches the module.

- If the handler symbol cannot be resolved from the boundary entry, the seed falls back to the call line into the module. If that is missing too, it falls back to the route's registration line.
- A seed that reaches the module only over HTTP or a queue has no importing place. It is still passed, at its handler. `code_facts` then reports it as `seed_not_found` (a diagnostic) or `unreachable` (a drop). Either way it shows in the Left out table (D8), so it is never silently absent.

**Seed name (open question a).** `Seed.name` is the label a reader recognises. It serves as both the entry name and the `invoked_by` text.

| Boundary kind | `Seed.name` |
|---|---|
| webhook route, other route | `<METHOD> <path>`, e.g. `POST /hooks/plaid` |
| route called from another repo | `<other repo key>: <METHOD> <path>` |
| Celery beat entry | `<task name> (beat)` |

So "invoked by `POST /hooks/plaid`" reads as "an HTTP request to that route", which is correct. The echo between the name and the invoker is accepted.

**Spec deviation, flagged.** The scenario "Route that imports into the module" expects the `Location` to point to `api/hooks.py` line 7, the import line. The prototype showed that an import line makes a poor entry: hop 1 then cites an `import` statement and no call follows from it. This design uses the handler `def` in the same file instead. The scenario should be amended to "points to the handler in `api/hooks.py`". The requirement text ("that place") is still met at file level.

**Alternatives considered.**

- *Import line.* Rejected for the reason above.
- *Call line.* This is better than the import line, but it starts the trace mid-function and loses the handler as an entry. It is used only as a fallback.

### D4. Boundary edges as known places

**Decision.** `known_places(state) -> list[Location]` collects the target-repo side of every cross-repository reference, route and task found by the boundary stage. It strips the repo key and drops duplicates while keeping first-seen order. Places that exist only in another repo are not passed, because they cannot be located under `root`. If there are no edges, the function returns `[]`. This covers "Known places include boundary edges" and "No boundary edges".

**Alternatives considered.** *Pass every boundary entry, including outbound data and config.* Rejected because those are not call edges, and they would loosen the gate.

### D5. Model-call wiring and the fatal-error latch

**Decision.** `_trace_call(ctx, latch)` returns an async `CallFn` that does two things:

1. It forwards to `sdk.agent_call_structured` with:
   - `allowed_tools=sdk.TRACE_TOOLS`;
   - the trace stage's model and effort from `WalkConfig`;
   - `cwd` set to the snapshot;
   - `budget_label="trace"`.

   This is the same wiring `stages/explore.py` uses, so spend lands in the same ledger that the `--max-usd` breaker checks.
2. It implements a **fatal latch**:
   - Before each call, if `latch.exc` is set, it re-raises that exception immediately, so no further spend happens.
   - If the call raises `RepoTrustError` or `BudgetExceededError`, it stores the exception in `latch.exc` and re-raises it.

After `map_paths` returns **or** raises, `run_trace` checks `latch.exc` first and re-raises it if set. This is needed because `map_paths` has no `fatal=` argument, so the walkthrough cannot rely on how `code_facts` classifies exceptions. If `code_facts` turned a budget stop into a per-entry drop, the latch still restores "trust and budget stops still halt the run".

`run_trace` also calls `budget.check(stage="trace")` once before `map_paths`.

`sdk.AgentCallError` is **not** latched. `map_paths` turns it into a per-entry drop, and the walkthrough renders that drop's reason string unchanged.

Answers are cached by `code_facts` under `<run_dir>/paths-cache/paths-answers/` as each one arrives. That means:

- a budget stop leaves the paid answers on disk;
- `resume` re-runs `map_paths` with the same `cache_dir`, so only missing answers are asked.

This covers "Resume asks only for missing answers" and "Fully cached resume".

**Alternatives considered.**

- *Add a `fatal=` argument to `map_paths`.* This changes PR 1's API in a walkthrough PR, and review and review-viewer would inherit it unreviewed.
- *Wrap `map_paths` in `except BudgetExceededError`.* This does not work if `code_facts` swallows the exception per entry.

### D6. `--scenarios` caps paths, default 21

**Decision.** In `__main__.py`, `--scenarios` keeps its name. It changes as follows:

- It gets `type=_positive_int`. `0` or a negative value raises `argparse.ArgumentTypeError`, so argparse exits with status 2 and an error naming `--scenarios` before any stage runs.
- `default=21`.
- `help="Cap on the number of paths traced (default: 21)."`

The value is stored in the walk options and passed as `cap=` to `map_paths`. `trace_limit` stays at the module constant `TRACE_LIMIT = 30`.

**Ranking (open question h).** With the whole module in scope, "most changed lines covered" ranks the deepest paths first, so web seeds tend to be the first to fall into `over_cap`. This is **accepted** without re-ranking, which is a non-goal. Three things mitigate it:

- every over-cap entry is listed by name in Left out, with reason `over_cap`;
- the cap rose from 6 to 21;
- a user who needs a specific web entry can raise `--scenarios`.

The spec scenario "Web entries traced before the change are still traced" (traced, or dropped `over_cap`) holds when the fixture has at most `trace_limit` entries. Beyond 30, entries are dropped earlier by the trace limit, and that drop's reason is shown unchanged.

### D7. Paths section format (prototype shape)

**Decision.** The **Paths** body is rendered entirely by code (`render.render_paths_section`) from `state.paths`. No writer agent produces it, so it cannot be omitted. It sits at the position "End-to-end scenarios" held in `stages/write.py`'s section list.

**Lead sentence.** Exactly one of the following is used.

| Case | Text |
|---|---|
| N ≥ 1 paths, no left-out entries | `{N} path(s) reach this module.` ("1 path reaches", "N paths reach") |
| N ≥ 1 paths, M ≥ 1 left out | the sentence above, then ` {M} entries were left out; see **Left out** under Coverage.` |
| 0 paths, 0 left out, no failure | `No entry points reaching this module were found.` |
| 0 paths, M ≥ 1 left out | `No paths were traced. {M} entries were left out; see **Left out** under Coverage.` |
| whole-stage failure | `Path finding failed: {state.paths_error}` |

M counts `result.drops` plus `seed_not_found` diagnostics. `dynamic_name` and `e2e_listing` rows are listed in Left out but **not counted**, because they are not entries that were left out.

**Per path** (n is 1-based, in `map_paths` order, never re-sorted):

```markdown
**p2** — `sync` (click command)
- Entry: invoked by `sync` CLI subcommand, at `p2h1`
- Hops:
  - <a id="p2h1"></a>`p2h1` [`DolceEngine/cli/main.py:42-58`](<url> "<quote>")
  - <a id="p2h2"></a>`p2h2` [`DolceEngine/svc/handler.py:57-71`](<url> "<quote>")
- Stops after `p2h2`: `unconnected_hop`
- E2E tests: no e2e test; not counted: `tests/e2e/test_sync.py::test_sync_offline` (mocks `p2h2`, not counted)
```

**Heading line.** It shows `` `PathEntry.name` `` and a kind label taken from `KIND_LABELS` in `render.py`, keyed by the `paths.v1` kind value:

- argparse command, click command, typer command;
- `main` module, script, skill command, hook;
- caller seed.

An unknown kind renders verbatim.

**Entry bullet.** It renders `invoked by` plus `PathEntry.invoked_by`, verbatim, in backticks. `invocation` is a different field and is not shown here.

- If hop 1's line range contains the entry line, the bullet says `at `p{n}h1`` and adds **no second link** (open question b). This avoids a duplicate link that `check` would count twice, and hop 1's link already shows the `path:start-end`.
- Otherwise the bullet ends with `Linker.link` for the entry location.

**Hops bullet.** It holds one nested bullet per hop, in hop order:

```
<a id="p{n}h{k}"></a>`p{n}h{k}` <Linker.link>
```

- `Linker.link` renders `` [`<repo key>/<path>:<start>-<end>`](<url> "<quote>") ``, because hops are line ranges.
- Markdown paths carry the repo key prefix. `paths.v1` paths stay repo-relative.

**Stop bullet** (open question d). The trace is cut *after* the last good hop, so the wording is "after":

- for a cut-short path: `Stops after `p{n}h{k}`: `<break_reason>``, where `p{n}h{k}` is the hop `stops_at` names;
- for a complete path: `Ends at `p{n}h{last}` (trace complete).`

**E2E tests bullet.**

- Each `E2ETest` is listed as a counted test, with its test id in backticks.
- If there are none, the literal `no e2e test` appears, even when mocked tests follow.
- Each `MockedE2ETest` from `tests.mocks_hop_2` is listed after `not counted:` with the fixed label `(mocks `p{n}h2`, not counted)`. The prototype showed the mocked hop is always hop 2, so there is no per-test hop field to read.

**Spec deviation, flagged: "Unknown invoker".** `invoked_by` is a required field built by code, so a `PathEntry` without it fails `paths.v1` validation and cannot reach the renderer. The "unknown" fallback would be dead code, so it is **not implemented**. In its place:

- a test asserts that `PathEntry` rejects a missing `invoked_by`;
- a second test asserts that every rendered Entry bullet has a non-empty invoker.

The spec scenario "Unknown invoker" should be removed or restated as "the invoker is always present".

**Alternatives considered.**

- *Writer-written Paths section.* Rejected because it is non-deterministic, can omit paths, and costs a model call to restate data.
- *A hop chain on one line.* Rejected because `check` associates an anchor with the link on the same line, and one hop per line keeps that association unambiguous.

### D8. Coverage: Measure rows plus a separate **Left out** table

**Decision.** The existing `Measure | Value` table cannot hold per-entry rows.

**Measure table changes.** The `Scenarios cut short` row is removed and replaced by three rows:

- `Paths traced`: the count of `result.paths`;
- `Paths cut short`: the count of paths with `stops_at` set;
- `Entries left out`: M, as defined in D7.

**New subtable.** A `### Left out` subsection follows under Coverage. It is rendered only when there is at least one row:

```markdown
| Entry | Stage | Reason | Detail |
|---|---|---|---|
| `DolceEngine/api/hooks.py:31` | trace | `over_cap` | … |
| `DolceEngine/api/jobs.py` | discover | `seed_not_found` | … |
```

Rows appear in this order:

1. `result.drops`, in `map_paths` order, with Stage set to `PathDrop.stage` and Reason set to `PathDrop.reason`, verbatim. This covers `unreachable`, `over_cap`, the trace-limit reason and per-entry agent failures. An unknown reason renders unchanged.
2. Entry diagnostics: `seed_not_found` and `dynamic_name`, with Stage `discover`.
3. Listing skips, with Stage `e2e` and Reason `e2e_listing`.

The Entry column uses **plain backticked text, not links**, so that `check` does not treat left-out locations as claims:

- with a line: `<repo key>/<path>:<line>`;
- when `PathDiagnostic.line` is `null`, or for a `ListingSkip`, which has no line: `<repo key>/<path>`, or the skip's test id.

Detail is the record's `detail` text with `|` escaped.

**Alternatives considered.** *Appending rows to `Measure | Value`.* The prototype disproved this, because the columns do not fit.

### D9. State, persistence and legacy state

**Decision.** In `models.py`, `WalkState` loses `trace_answers` and `scenarios` and gains two fields:

- `paths: code_facts.PathsResult | None = None`, which stores the validated result so render and resume are deterministic;
- `paths_error: str | None = None`.

Old state files load because `_Model` ignores unknown keys. This covers "Resuming state written before the change".

One case needs special handling. A **legacy state already past `trace`**, in human mode, would load with `paths is None` and `paths_error is None`, and would wrongly render "No entry points …". `resume` detects that combination and rewinds `state.stage` to the stage before `trace`. Old answers are in a different format and are ignored, and the extra spend stays under `--max-usd`.

**Alternatives considered.** *Re-run `map_paths` at render from cache only.* This also works, but it rebuilds `PyIndex` and couples render to the snapshot. Storing the result is simpler.

### D10. Whole-stage failure

**Decision.** `run_trace` wraps `map_paths` in `except Exception as e`. The latch is checked first, so `RepoTrustError` and `BudgetExceededError` propagate. For any other exception:

- `state.errors.append(f"trace: {type(e).__name__}: {e}")`;
- `state.paths = None`;
- `state.paths_error = f"{type(e).__name__}: {e}"`.

The stage then returns, and the write and render stages still run. `progress.log` and `log_event` record only the exception type, through the existing `reason_kind` convention, because messages can quote code.

This covers "map_paths raises" and "Whole-stage failure".

### D11. Hop citations in prose and in the write gate

**Decision.**

- **Citation index.** `citation_index` in `models.py` drops `s<N>h<M>` and adds `p{n}h{k}`, mapping each id to its `PathHop` with the repo key prefixed.
- **Writer prompts.** In `stages/write.py`, prompts gain a "Path hops" block that lists every id with its `path:start-end` and quote. Writers are told to cite hops as `[[p{n}h{k}]]`, just as they cite `[[cN]]`.
- **Write gate.** The id regex in `write_gate` widens to `c\d+|f\d+|p\d+h\d+`. An unknown hop id is rejected through the same path as an unknown `cN`. It is reported as an invalid citation and is never rendered as a link.
- **Rendering.** A valid hop citation renders as `<Linker.link> ([p{n}h{k}](#p{n}h{k}))`.

This covers "Valid hop citation resolved", "Unknown hop citation rejected" and "Prompt includes hop ids".

### D12. `check --at-head`

**Decision.** The prototype showed that `check.py` already re-checks **every** link regardless of heading. So the following need no new code:

- legacy "End-to-end scenarios" links;
- Paths hop links;
- "Hop and scenario-hop verdicts agree" and "Legacy walkthroughs still pass".

A regression test pins this behaviour.

The new code, still reading the Markdown only and making no model, JSON or run-directory reads, is:

1. **Hop prefix on failures.** A link on the same line as `<a id="(p\d+h\d+)"></a>` belongs to that hop. Its failure line becomes `p2h3 <repo>/<path>:<start>-<end>: <reason>`, which is the existing failure format with the hop id prepended.
2. **Unresolved hop citations.** Every `[pNhK](#pNhK)` reference without a matching anchor in the document fails with `unresolved hop citation pNhK`, and counts as one failure.
3. **Hop count (open question e).** The stdout contract line `check: N checked, M failed` stays **byte-for-byte unchanged**, because buildme may parse it. The hop count goes to **stderr** as `check: N hops checked`, where N is the number of anchors. It is printed only when N > 0, so legacy and agent-mode documents produce identical output to today.
4. **No Paths requirement.** A missing Paths section, or a Paths section with no anchors, is never a failure.

### D13. `<name>-paths.json`

**Decision.** In `render.py`, `paths_file_for(md: Path) -> Path` returns `md.with_name(md.name.removesuffix("-walkthrough.md") + "-paths.json")`. In human mode, after `<name>-walkthrough.md` is written:

- **If `state.paths` is set:** call `code_facts.write_paths_file(state.paths, target)`.
  - On `OSError` or `ValueError`: append `paths file <target>: <ExcType>: <msg>` to `state.errors` and unlink any stale target (`missing_ok=True`).
- **If `state.paths is None`** (stage failure): unlink any stale target.
- **Agent mode:** do not call the writer and do not touch the file.

The walkthrough never serialises `paths.v1` itself.

**Printing the file (open question f).** The path is appended to the list `run_state` returns, after the Markdown path. It is therefore printed with the existing `walkthrough:` output-line format. buildme's `walkthroughs.py` matches `*-walkthrough.md` only, so it ignores the new file.

**Alternatives considered.** *Write `paths.json` before the Markdown.* Rejected because a JSON failure must never cost the walkthrough, and writing it second makes that ordering obvious.

### D14. Docs, release, tests

**Docs and release.**

- `SKILL.md` stage 5 is rewritten for `map_paths` and documents:
  - the Paths section;
  - hop ids;
  - the Left out table;
  - `<name>-paths.json`: written beside the walkthrough, `paths.v1` schema, human mode only, not read by `check` or buildme.
- multiplai-dev is bumped to `0.81.0` in `marketplace.json`, with a `CHANGELOG.md` entry, as `scripts/check_changelog.py` requires.

**Test fixtures.** Tests build real git fixture repos in `tmp_path` with a fixed author:

- a click CLI;
- a SKILL.md `python -m` command;
- web routes, including one reached only over HTTP;
- 25 click commands, for the cap test.

**Test style.** `sdk.agent_call_structured` is monkeypatched to a fake that records calls and returns fixed trace answers. A resume test fakes a stop after 3 of 5 answers and asserts that exactly 2 calls follow.

**Schema tests.** `paths.json` is validated against `paths.v1.schema.json`, including for an empty result.

**Check tests** cover:

- a passing Paths section;
- a moved hop line;
- a deleted hop file;
- a line past end of file;
- one bad hop among many;
- an absent or corrupted `paths.json`;
- an empty Paths section;
- an unresolved `p4h1`;
- a legacy "End-to-end scenarios" document;
- an agent-mode document.

A mutation-style test removes hop-anchor handling and asserts that the moved-hop test fails.

**CI form.** Suites run with `uv run --project plugins/multiplai-dev/skills/codebase-walkthrough/scripts --package walkthrough-pipeline python -m pytest`.

### Integration contracts (new or modified)

| Module | Symbol | Status |
|---|---|---|
| `stages/trace.py` | `run_trace(state, ctx) -> WalkState`, `module_lines`, `web_seeds`, `known_places`, `_trace_call`, `TRACE_LIMIT = 30` | modified; `pick_seeds` removed |
| `gates.py` | `trace_gate` | removed |
| `prompts.py` | `trace_scenario` | removed |
| `models.py` | `WalkState.paths`, `WalkState.paths_error`, hop ids in `citation_index` | modified; `trace_answers` and `scenarios` removed |
| `stages/write.py` | section list (`"Paths"`), hop block in prompts, write-gate id regex | modified |
| `render.py` | `render_paths_section`, `render_left_out`, `KIND_LABELS`, `paths_file_for` | new |
| `check.py` | hop-anchor association, unresolved-hop check, stderr hop count | modified |
| `__main__.py` | `--scenarios` type, default and help | modified |
| `orchestrator.py` | legacy-state rewind on `resume`, paths file added to the returned outputs | modified |

## Global Constraints

- Section title: `Paths`. New human-mode documents never contain a heading `End-to-end scenarios`.
- The `<name>-walkthrough.json` step title is `Paths`. No other change to the walkthrough v1 shape.
- Hop id format: `p{n}h{k}`, with n and k 1-based. Regex: `p\d+h\d+`.
- Hop line in Markdown: ``<a id="p{n}h{k}"></a>`p{n}h{k}` `` followed by one space and `Linker.link` output.
- `Linker.link` form: `` [`<repo key>/<path>:<start>-<end>`](<url> "<quote>") ``.
- Writer hop citation token: `[[p{n}h{k}]]`. Rendered as `<Linker.link> ([p{n}h{k}](#p{n}h{k}))`.
- Path heading line: ``**p{n}** — `<entry name>` (<kind label>)``.
- Entry bullet prefix: `- Entry: invoked by `.
- Stop bullet, cut short: ``- Stops after `p{n}h{k}`: `<break_reason>` ``.
- Stop bullet, complete: ``- Ends at `p{n}h{k}` (trace complete).``
- No-test literal: `no e2e test`.
- Mocked-test label: ``(mocks `p{n}h2`, not counted)``.
- Lead sentence, no entries: `No entry points reaching this module were found.`
- Lead sentence, all left out: `No paths were traced. {M} entries were left out; see **Left out** under Coverage.`
- Lead sentence, failure: `Path finding failed: {message}`.
- Measure rows: `Paths traced`, `Paths cut short`, `Entries left out`. The row `Scenarios cut short` is removed.
- Left-out subsection heading: `### Left out`.
- Left-out table columns: `| Entry | Stage | Reason | Detail |`.
- Drop reasons are rendered verbatim from `code_facts`, e.g. `seed_not_found`, `unreachable`, `over_cap`.
- Markdown paths are prefixed with the repo key. `paths.v1` paths are repo-relative.
- Paths file: `<name>-paths.json` in the same directory as `<name>-walkthrough.md`, written only through `code_facts.write_paths_file`.
- Trace cache dir: `<run_dir>/paths-cache`. `code_facts` writes answers under `<run_dir>/paths-cache/paths-answers/`.
- `--scenarios` default: `21`.
- `--scenarios` help text: `Cap on the number of paths traced (default: 21).`
- `--scenarios` values below 1 are rejected by argparse.
- `trace_limit` passed to `map_paths`: `30`.
- Agent tool set for trace calls: `sdk.TRACE_TOOLS`. `budget_label="trace"`.
- `state.errors` entry for stage failure: `trace: <ExceptionType>: <message>`.
- `state.errors` entry for paths-file failure: `paths file <path>: <ExceptionType>: <message>`.
- `progress.log` stage summary: `trace: <N> paths, <C> cut short, <M> left out`.
- `progress.log` stage failure: `trace: failed (<ExceptionType>)`.
- `check` stdout contract, unchanged: `check: N checked, M failed`.
- `check` stderr hop count, only when N > 0: `check: N hops checked`.
- `check` hop failure line: `p{n}h{k} <repo>/<path>:<start>-<end>: <reason>`.
- `check` unresolved citation line: `unresolved hop citation p{n}h{k}`.
- Only `RepoTrustError` and `BudgetExceededError` escape `run_trace`.
- multiplai-dev version: `0.81.0`, with a `CHANGELOG.md` entry.
- Test command form: `uv run --project plugins/multiplai-dev/skills/codebase-walkthrough/scripts --package walkthrough-pipeline python -m pytest`. Never `--directory`.
- Every new module starts with a docstring, then `from __future__ import annotations`, and logs through `log = logging.getLogger(__name__)`.

## Risks / Trade-offs

- **More spend per run.** Up to 21 traced paths instead of 6 means more model calls. This is bounded by the existing `--max-usd` breaker ($50 default) and by `trace_limit=30`. The on-disk cache means `resume` never pays twice.
- **Web seeds may be squeezed out.** Whole-module scope favours deep paths in the ranking, so web seeds tend to land in `over_cap` first. This is accepted, because the non-goals forbid re-ranking and every left-out seed is named in Left out. If users complain, the fix belongs in `code_facts`' ranking, where review and review-viewer would share it.
- **Latch depends on the call boundary.** The fatal latch assumes every model call goes through the walkthrough's `CallFn`. That is true today: `code_facts` never calls a model itself. If a future `code_facts` added an internal call path, budget and trust stops could again be swallowed. A test pins the latch by raising `BudgetExceededError` from the fake on entry 2 and asserting that the run stops.
- **`check` hop count on stderr.** The count is less discoverable there than on stdout. It was chosen so that buildme and any other parser of `check: N checked, M failed` sees no change. Tests read stderr.
- **New `walkthrough:` output line.** Any consumer that treats every `walkthrough:` line as a Markdown file would mis-read the new `.json` line. buildme scans by `*-walkthrough.md` suffix, and the Markdown line is still printed first.
- **Legacy state rewind re-spends.** Resuming a run that finished `trace` under the old code re-runs path finding with no cache. This is accepted for correctness and stays capped by the budget.
- **Spec items that are infeasible or contradictory**, to be amended in the spec registry rather than implemented literally:
  - *"Unknown invoker"* (walkthrough-paths-section): `invoked_by` is required by `paths.v1`, so the case cannot occur and no fallback branch is built (D7).
  - *"Route that imports into the module … line 7"* (walkthrough-path-tracing): the seed is placed at the handler `def`, not the import line (D3).
  - *"`seed_not_found` … as a drop"* (walkthrough-paths-file, "Unlocatable web seed appears as a drop"): `paths.v1` reports `seed_not_found` under **diagnostics**, not `drops`. The paths file records it there, and the Left out table counts it with the drops.
  - *"Web entries traced before the change are still traced"*: this holds only while at most 30 entries are eligible. Past `trace_limit`, entries are dropped with the trace-limit reason, not `over_cap`.
  - *"Hop and scenario-hop verdicts agree"* and *"Legacy scenario walkthroughs still pass"*: these are satisfied by existing heading-agnostic `check` behaviour. No new code is written for them, only regression tests.
- **No links in Left out.** Rendering left-out locations as plain text means a moved line there never fails `check`. This is intended, because those rows are not claims. The cost is that a reader cannot click through from that table.