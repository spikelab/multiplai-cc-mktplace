## 1. Accepted paths.json reaches the page as a Paths tab with a count

This slice loads `<review_dir>/paths.json` beside `findings.json`, where `review_dir` is the same `Path(box).parent` passed to `checks_beside`. It validates the file with `code_facts.PathsResult` and accepts it only on an exact `head_sha` match. The accepted document is served under the `paths` payload key. The page then shows a `Paths` tab (`data-tab="paths"`) immediately after the Checked tab, with count `target.paths.paths.length` and an empty panel section.

**Dependency and release**
- Add `code-facts` to review-viewer's `[project].dependencies`, with a comment that the viewer reads code_facts' `paths.v1` contract.
- Add `[tool.uv.sources] code-facts = { path = "../../../lib/code_facts", editable = true }`.
- Regenerate `uv.lock`.
- Bump the multiplai-dev version in `marketplace.json` and add a `plugins/multiplai-dev/CHANGELOG.md` entry, so `scripts/check_changelog.py` passes.

**Loader behaviour**
- `inspect_paths` checks the file in this order and stops at the first failure:
  1. File does not exist: `absent`, with no log.
  2. `OSError` on read: `invalid`, detail is the error.
  3. `json.JSONDecodeError`: `invalid`, detail is `parse failure: …`.
  4. The top level is not an object, or `commit` is missing or not a `str`: `invalid`, detail is `no commit`.
  5. `head_sha` is falsy, or `commit != head_sha`: `stale`, detail is `commit=<commit> head_sha=<head_sha>`.
  6. `PathsResult.model_validate` raises: `invalid`, detail is the location and message of the first error only.
  7. Otherwise: `accepted`.
- The comparison is plain `==`. There is no prefix match, no case folding and no `base_sha` match.
- `paths_beside` writes exactly one `log.warning("paths.json ignored (%s): %s %s", status, path, detail)` for `stale` and `invalid`. It writes nothing for `absent`. It never raises.
- `REVIEW_FILES` is not touched in either `review_viewer/gitdata.py` or `review_pipeline/target.py`.
- The import is `from code_facts.paths import PathsResult` only. The loader never calls `serialise_paths`, and nothing under `lib/code_facts` changes.

**Payload**
- The payload value is `state.paths.model_dump(mode="json")`. This is a dict with keys `schema_version`, `commit`, `scoped`, `cap`, `fallback`, `paths`, `dropped`, `diagnostics` and `test_listing_skips`.
- When no file is accepted, the value is `null`.
- A valid file with zero paths serves `{"paths": [], …}`, not `null`.
- Path order from the file is preserved.

**Front end**
- Move the inline reset at `app.js:423` into `resolveTab` in `logic.js`.
- Show the tab, its count and the summary slot only when `target.paths != null`. Hide them the same way Checked is hidden.
- Slack share and export code is not touched.

**Fixtures**
- Create `scripts/tests/fixtures/paths.example.json` as valid `paths.v1`, with `commit` equal to `findings.example.json`'s 40-character `head_sha`. It contains:

| Path | Contents |
|---|---|
| `p1` | Non-null handler. `tests.e2e` has one entry with `node_id` `tests/e2e/test_run.py::test_run_cli`. `tests.mocks_hop_2` has one entry with `node_id` `tests/e2e/test_run.py::test_run_cli_mocked`. `stops_at: null`. |
| `p2` | `stops_at: 2`, with 2 hops kept and a valid `CutReason` as `break_reason`. One e2e test. |
| `p3` | `mocks_hop_2` only. |
| `p4` | No tests. |
| `p5` | `handler: null`. One e2e test. |
| `p6` | One e2e test. |

- `paths.example.json` also has a non-empty `test_listing_skips`.
- Add `paths` to findings in `findings.example.json`, in `paths.json` order with no duplicates, so that:

| Finding | `paths` | Other state |
|---|---|---|
| F-A | `["p1","p4"]` | — |
| F-B | `["p1"]` | — |
| F-C | `["p6"]` | Refuted |
| F-D | `["p2"]` | Labelled low-value, so hidden by `shownFindings` |
| At least one finding | No `paths` field | — |

**Tests**

Run Python tests with `cd plugins/multiplai-dev/skills/review-viewer/scripts && uv run --extra dev pytest`. Run Node tests with `node --test tests/logic.test.js`.

Each `inspect_paths` / `paths_beside` case below asserts both the payload `paths` value and the count of matching `caplog.records`:

| Case | Expected status | `paths` in payload | Log records |
|---|---|---|---|
| Exact match | `accepted` | populated | 0 |
| 7-char prefix of `head_sha` | `stale` | `null` | 1 |
| `head_sha` is a prefix of `commit` | `stale` | `null` | 1 |
| `commit == base_sha` | `stale` | `null` | 1 |
| Case-only difference | `stale` | `null` | 1 |
| Empty or missing `head_sha` | `stale` | `null` | 1 |
| Missing `commit` | `invalid` | `null` | 1 |
| Non-string `commit` | `invalid` | `null` | 1 |
| Malformed JSON | `invalid` | `null` | 1 |
| Monkeypatched `read_text` raising `OSError` | `invalid` | `null` | 1 |
| Missing required field | `invalid` | `null` | 1 |
| Wrong field type (string for hops) | `invalid` | `null` | 1 |
| Schema id other than `paths.v1` | `invalid` | `null` | 1 |
| Hop `../../etc/passwd` (rejected by `REL_PATH`) | `invalid` | `null` | 1 |
| Zero paths | `accepted` | `{"paths": []}` | 0 |
| File absent | `absent` | `null` | 0, and no record mentions `paths.json` |

Further Python tests:
- **Payload equality:** a payload with a rejected file equals the payload with no file, including findings, checks and walkthrough.
- **Same response:** the payload carries `paths` in the same `/api/targets/<slug>` response as findings and checks.
- **No model, walkthrough as stored:** monkeypatch `multiplai_core.run_agent` to raise. Load a review that has an accepted fixture and a stored walkthrough naming no paths. Assert that:
  - the payload `paths` is populated;
  - the payload walkthrough equals the stored walkthrough exactly, in stored order;
  - `run_agent` was never called.
- **Fixture validity:** `paths.example.json` validates as `PathsResult`, its `commit` equals `findings.example.json`'s `head_sha`, and every finding path id exists in it.
- **Drift guard:** walk `PathsResult.model_fields` through nested models and assert each of these exists:
  - `commit`, `paths[].id`
  - `entry.name`, `entry.kind`, `entry.location.path`, `entry.location.line`
  - `entry.invoked_by`, `entry.also_invoked_by`
  - `entry.handler.path`, `entry.handler.line`
  - `hops[].path`, `hops[].line_start`, `hops[].line_end`
  - `stops_at`, `break_reason`
  - `tests.e2e[].node_id`, `tests.e2e[].file`, `tests.e2e[].line`
  - `tests.mocks_hop_2[].node_id`
  - `test_listing_skips`

Node tests:
- `resolveTab` returns `"summary"` for `"checked"` without checks.
- `resolveTab` returns `"summary"` for `"paths"` without paths.
- `resolveTab` returns any other tab unchanged.
- The share/export helper output is deep-equal with and without a paths doc.

Manual check: open a review with the fixture and see `Paths 6` after Checked.

Interfaces:
- Produces: `class PathsStatus(NamedTuple): status: Literal["accepted", "stale", "invalid", "absent"]; result: PathsResult | None; detail: str` (in `review_viewer/server.py`)
- Produces: `def inspect_paths(review_dir: Path, head_sha: str | None) -> PathsStatus`
- Produces: `def paths_beside(review_dir: Path, head_sha: str | None) -> PathsResult | None`
- Produces: `TargetState.paths: PathsResult | None = None`
- Produces: `GET /api/targets/<slug>` payload key `"paths": None | state.paths.model_dump(mode="json")` (dict with keys `schema_version, commit, scoped, cap, fallback, paths, dropped, diagnostics, test_listing_skips`)
- Produces: `resolveTab(tab: string, {checks, paths}) -> string` (logic.js, listed in `api`)
- Produces: `scripts/tests/fixtures/paths.example.json` (paths p1–p6 as above) and `findings.example.json` path ids F-A `["p1","p4"]`, F-B `["p1"]`, F-C refuted `["p6"]`, F-D low-value `["p2"]`

Satisfies: viewer-paths-loading — Locate paths.json beside findings.json (as corrected by D12: `<review_dir>/paths.json` only, no `REVIEW_FILES` change); Absent paths.json leaves review loading unchanged; Accept paths.json only on an exact full head_sha match (all scenarios); Log and ignore a commit mismatch; Validate paths.v1 against the paths.v1 contract; Log and ignore an invalid paths.json (all scenarios); Serve accepted path data in the review payload (all scenarios); Loading paths.json never calls a model; Python tests cover paths.json loading outcomes; Path outside the repository is not allowed (via `REL_PATH` rejection). viewer-paths-panel — Paths tab visibility (all scenarios); Paths tab count (Count matches the number of paths); Excluded paths.json fields (Share and export unchanged). walkthrough-path-guidance — Viewer does not generate path text in the walkthrough (Walkthrough rendered as stored with paths loaded; No model call).

## 2. Path cards with entry point, hop file links, broken trace, findings and e2e tests

This slice fills the Paths panel with one card per path, ordered with paths that have counted findings first. Each card shows:
- the entry name, kind and invokers;
- hops in order, each linking into the file view at `line_start` and highlighting `line_start..line_end`;
- a `trace broken` marker;
- the counted findings, each linking to its finding detail through the existing finding-selection handler;
- e2e tests.

**Server changes**
- `allowed_paths` gains `extra`. Pass `_path_files(state.paths)` when `state.paths` is set, and `()` otherwise.
- `_path_files` collects each path's entry `location.path`, `handler.path` (skipped when the handler is `None`) and every `hops[*].path`.
- `file_view` gains `extra_ranges`, merged into the cited-range handling with ±12 folding for files over 4000 lines.
- The `_file` route builds the ranges with `_path_ranges(state.paths, path)`:
  - each hop gives `(line_start, line_end)`;
  - the entry gives `(location.line, location.line)`;
  - the handler gives `(line, line)`.

**Card rendering**
- Rendering lives under a `// --- paths ---` section in `app.js` and uses only `el(...)` / `textContent`.
- Each card has DOM id `path-card-<id>`.
- A broken trace shows `trace broken`, the `break_reason` and the file:line of `hops[stops_at-1]`, and renders `hops.slice(0, stops_at)`.
- A card with no counted findings shows no findings section and no heading.
- Mocked tests are marked `not counted`.
- `no e2e test` shows when `tests.e2e` is empty.
- The card model has no keys for `dropped`, `diagnostics`, fallback, `scoped`, `cap` or `changed_lines_covered`.

**Python tests**
- `allowed_paths(..., extra=())` equals today's set.
- In a tree review of `src/a/`:
  - an accepted file's hop in `src/b/x.py` is viewable;
  - with a stale `paths.json` naming `src/b/x.py`, the request is refused;
  - a file outside `src/a/` that is not cited and not on any path is refused.
- With `listed = []`, hop, entry and handler files are viewable through `extra`.
- A null handler is skipped without error.
- `extra_ranges` keeps hop lines unfolded in a file of more than 4000 lines.

**Node tests, on the fixture**
- The fixture contains at least one path of each kind:
  - with counted findings and a counted test (p1);
  - broken trace (p2);
  - mocked-only (p3);
  - no tests (p4).
- Every finding path id resolves in `paths.example.json`.
- `findingsByPath(countedFindings(shownFindings(...)))` maps:
  - p1 to [F-A, F-B];
  - p4 to [F-A].
- `findingsByPath` has no entry for p6 (F-C is refuted) and none for p2 (F-D is not shown).
- A finding with no `paths` field is tolerated.
- `sortPaths` returns p1, p4, p2, p3, p5, p6 on the fixture.
- An inline A, B, C, D case with findings on B and D returns B, D, A, C.
- An empty `byPath` returns document order.
- `sortPaths` leaves its inputs deep-equal to a `structuredClone` taken before the call.
- `pathCardModel(p1)` returns:
  - `tests.counted === ["tests/e2e/test_run.py::test_run_cli"]`;
  - `tests.mocked === ["tests/e2e/test_run.py::test_run_cli_mocked"]`;
  - `noE2e === false`;
  - `broken === null`.
- `pathCardModel(p2)` gives 2 hops and `broken` set to the file:line of `hops[1]`.
- `pathCardModel(p3)` and `pathCardModel(p4)` give `noE2e === true`. p4 also gives empty `counted` and `mocked`.
- `pathCardModel(p5)` gives `handler === null`.
- The model key set excludes every forbidden field.
- An entry name containing `<script>` is kept as a literal string.
- A source guard asserts that the `// --- paths ---` section of `app.js` contains no `innerHTML`.

Interfaces:
- Consumes: Block 1: `TargetState.paths: PathsResult | None = None`
- Consumes: Block 1: `GET /api/targets/<slug>` payload key `"paths": None | state.paths.model_dump(mode="json")`
- Consumes: Block 1: `scripts/tests/fixtures/paths.example.json` (paths p1–p6) and `findings.example.json` path ids F-A `["p1","p4"]`, F-B `["p1"]`, F-C refuted `["p6"]`, F-D low-value `["p2"]`
- Produces: `def allowed_paths(target, findings, listed, extra: Iterable[str] = ()) -> set[str]` (gitdata.py)
- Produces: `def _path_files(result: PathsResult) -> set[str]` (server.py)
- Produces: `def _path_ranges(result: PathsResult, path: str) -> list[tuple[int, int]]` (server.py)
- Produces: `def file_view(target, path, findings=None, allowed=None, extra_ranges: Sequence[tuple[int, int]] = ()) -> FileView` (gitdata.py)
- Produces: `isRefuted(f) -> boolean`; `countedFindings(shown: Finding[]) -> Finding[]`; `findingsByPath(counted: Finding[]) -> Map<string, Finding[]>`; `sortPaths(paths: Path[], byPath: Map<string, Finding[]>) -> Path[]`; `hasCountedE2e(path) -> boolean` (`path.tests.e2e.length > 0`) (logic.js, all listed in `api`)
- Produces: `pathCardModel(path, byPath) -> {id, domId: "path-card-" + id, entry: {name, kind, invokedBy: string[], file, line}, handler: {file, line} | null, hops: [{file, lineStart, lineEnd}], broken: null | {reason, file, line}, findings: [{id, title}], tests: {counted: string[] /* node_id */, mocked: string[] /* node_id */, noE2e: boolean}}`

Satisfies: viewer-paths-panel — Path card ordering (all scenarios); Counted findings (all scenarios); Path card entry point (Entry point details shown; Text is escaped; card addressable via `path-card-<id>`); Path card hops (all scenarios, restated for tree reviews and failed listings per D12); Broken trace indication (both scenarios, location = `break_reason` + `hops[stops_at-1]`); Path card findings (both scenarios); Path card e2e tests (Counted tests; Only mocked tests; Counted and mocked tests; No tests); Excluded paths.json fields (Excluded fields not shown); Fixture-backed logic tests (Fixture covers the required cases; Logic tests pass on the fixture). viewer-paths-loading — Allow file views for files on accepted paths (Hop file becomes viewable; Entry and handler files become viewable, handler only when non-null; Files from ignored paths.json stay blocked; Allowed set unchanged without paths.json).

## 3. Paths summary line, unlisted-tests note and zero-paths message

This slice adds a summary line at the top of the Paths panel in the form `<n> path(s), <m> with findings, <k> with no e2e test`. The line uses `path` when n is 1 and `paths` otherwise. It is computed from the same `byPath` that orders the cards, and a mocked-only path counts toward `k`.

**Panel additions**
- When `test_listing_skips` is non-empty, show a muted note below the summary line reading `Some test files could not be listed, so e2e test results may be incomplete.`
- When the accepted document lists zero paths, the panel shows the summary line and the single message `No entry points reach the changed code.`, with no cards.

**Node tests**
- On the fixture: `pathsSummary` returns `"6 paths, 2 with findings, 2 with no e2e test"`.
- Inline four-path case (1 with findings, 1 mocked-only, 1 with no tests): `"4 paths, 1 with findings, 2 with no e2e test"`.
- Inline single path with one counted test: `"1 path, 0 with findings, 0 with no e2e test"`.
- Empty case: `"0 paths, 0 with findings, 0 with no e2e test"`.
- A refuted-only path (p6) is not counted in `m`.
- `hasUnlistedTests` is true on the fixture and false when `test_listing_skips` is `[]`.

**Manual check:** the fixture panel shows the line and the note, and a zero-path `paths.json` shows `Paths 0` with only the message.

Interfaces:
- Consumes: Block 1: `GET /api/targets/<slug>` payload key `"paths": None | state.paths.model_dump(mode="json")`
- Consumes: Block 2: `countedFindings(shown: Finding[]) -> Finding[]`; `findingsByPath(counted: Finding[]) -> Map<string, Finding[]>`; `hasCountedE2e(path) -> boolean`
- Produces: `pathsSummary(paths: Path[], byPath: Map<string, Finding[]>) -> string` (logic.js, listed in `api`)
- Produces: `hasUnlistedTests(doc) -> boolean` (`doc.test_listing_skips.length > 0`, logic.js, listed in `api`)

Satisfies: viewer-paths-panel — Paths summary line (Mixed paths; Single path; Zero paths); Unlisted test files note (both scenarios); Paths tab count (Zero paths: single message, no cards).

## 4. Finding detail "Paths" section linking to path cards

This slice adds a "Paths" section to every finding's detail when `target.paths` is non-null, refuted findings included. The section is computed from the finding itself, not from the counted set. It is omitted when the finding has no ids or when no `paths.json` was accepted.

**Entries**
- Known ids show entry name, kind and id as a link that calls `showPathCard(id)`.
- `showPathCard(id)` sets `state.tab = "paths"` and `state.targetPathId = id`, re-renders, then calls `document.getElementById("path-card-" + id)?.scrollIntoView({block: "start"})`.
- Rendering applies `is-target` only to the card whose id equals `state.targetPathId`.
- Switching targets clears `state.targetPathId`.
- Unknown ids render as plain text with `not in paths.json` and keep their position.

**Node tests**
- `resolvePathRefs`:
  - `["p2","p1"]` keeps finding order;
  - `["p1","p3","p1"]` gives p1 then p3;
  - `["p1","p9"]` gives p1 linked and p9 `{path: null, missing: true}` second;
  - all-unknown ids give all missing;
  - `[]`, `null` ids and a `null` doc each give `[]`;
  - inputs are deep-equal to their `structuredClone` after the call.
- `findingPathsSection`:
  - returns `null` for `paths === null`, for a missing `paths` field and for `[]`;
  - marks unknown ids with `link: false, note: "not in paths.json"`;
  - for refuted F-C returns a p6 entry with `link: true`.
- Server tests must not build `findings.json` with empty or duplicate ids. Those cases live only in these helper tests.

**Manual check (PR test plan)**
- Clicking F-A's p4 link switches to Paths, scrolls to `path-card-p4` and marks only that card.
- Switching targets clears the mark.
- A review with a stale or invalid `paths.json` shows no "Paths" section, path id or `not in paths.json` text in any finding.

Interfaces:
- Consumes: Block 1: `GET /api/targets/<slug>` payload key `"paths": None | state.paths.model_dump(mode="json")`
- Consumes: Block 2: `pathCardModel(path, byPath) -> {id, domId: "path-card-" + id, entry: {name, kind, invokedBy: string[], file, line}, handler: {file, line} | null, hops: [{file, lineStart, lineEnd}], broken: null | {reason, file, line}, findings: [{id, title}], tests: {counted: string[] /* node_id */, mocked: string[] /* node_id */, noE2e: boolean}}`
- Produces: `resolvePathRefs(ids: string[] | null, doc) -> Array<{id, path} | {id, path: null, missing: true}>` (logic.js, listed in `api`)
- Produces: `findingPathsSection(finding, doc) -> null | {entries: [{id, label, kind, link: boolean, note: null | "not in paths.json"}]}` (logic.js, listed in `api`)
- Produces: `showPathCard(id: string) -> void` and page state `state.targetPathId: string | null` (app.js)

Satisfies: finding-path-links — Finding detail lists the paths the finding lies on (all scenarios); Known path entries link to the Paths tab (both scenarios); Unknown path ids appear as plain text marked "not in paths.json" (both scenarios); No Paths section when the finding records no path ids (both scenarios); No Paths section when paths.json is not loaded (all three scenarios); Path links appear on every finding, refuted ones included; Finding-to-path resolution is a pure helper in logic.js (all scenarios). viewer-paths-panel — Path card entry point (Card is addressable by path id).

## 5. Walkthrough guidance gated on `walkthrough status` paths line

This slice adds one line per target to `walkthrough status`, built from `inspect_paths`. The line is one of:
- `paths: accepted <N> (<file>)`
- `paths: stale <commit> (<file>)`
- `paths: invalid (<file>)`
- `paths: none`

**`SKILL.md` subsection**
- Add a subsection to `review-viewer/SKILL.md` immediately after the `facts.json` walkthrough section, with no heading between them.
- It applies the path guidance only when status prints `paths: accepted`. That means a `paths.json` beside `findings.json` whose `commit` equals the full `head_sha` and that validates as `paths.v1`.
- In that case it gives three instructions:
  1. The overview names every path by its entry-point name as recorded in `paths.json`.
  2. Steps may follow a path's hops in hop order through changed code where that explains the change better than file order. File order stays the default.
  3. A hop not in changed code gets no step of its own.
- For `none`, `stale` or `invalid`, the walkthrough is written as before and names no paths.
- It never tells the session to add a path-id field.

**`SKILL.md` schema note**
- Names `paths.v1`, `code_facts/schema/paths.v1.schema.json` and `code_facts.PathsResult`.
- States that review-viewer defines no paths schema of its own.

**Pytest**
- `walkthrough status` prints each of the four `paths:` lines for accepted (fixture, `N` = 6), stale, invalid and absent files.
- A `SKILL.md` text guard asserts that:
  - the condition, the three instructions and the schema-note strings are present;
  - there is no `#`/`##` heading between the `facts.json` section and the path guidance;
  - there is no `path_ids` instruction.
- No file matching `*paths*schema*` exists under `plugins/multiplai-dev/skills/review-viewer/`.
- `walkthrough put` accepts a `walkthrough.v1` document whose overview names the fixture's entry points in prose and whose steps follow a hop order.
- `walkthrough put` treats an added `path_ids` field exactly as it treats any unknown field today.
- The `walkthrough.v1` schema file and `walkthrough put` are unchanged against the base.

**Manual PR test-plan item:** write a walkthrough following `SKILL.md` against the fixture review and confirm the overview contains all six entry-point names.

Interfaces:
- Consumes: Block 1: `def inspect_paths(review_dir: Path, head_sha: str | None) -> PathsStatus`
- Consumes: Block 1: `class PathsStatus(NamedTuple): status: Literal["accepted", "stale", "invalid", "absent"]; result: PathsResult | None; detail: str`
- Produces: `def paths_status_line(status: PathsStatus, path: Path) -> str` (returns one of `paths: accepted <N> (<file>)`, `paths: stale <commit> (<file>)`, `paths: invalid (<file>)`, `paths: none`; printed by `walkthrough status`)

Satisfies: walkthrough-path-guidance — Walkthrough overview names the paths (Guidance present in SKILL.md; Overview written under the guidance, via manual test-plan item); Walkthrough steps may follow a path's hops (both scenarios); Path guidance applies only to a paths.json the viewer would load (all scenarios); Path guidance sits beside the facts.json guidance; SKILL.md documents that the viewer reads code_facts' paths.v1 (both scenarios); Walkthrough contract is unchanged (all scenarios).