## 1. Paths stage maps paths and writes `paths.json` before find

This block adds `stages/paths.py` with `run_paths` and inserts `"paths"` into `STAGES` immediately before `"find"`. It also adds the `--paths` argparse flag, stored as `ReviewState.paths_enabled`, and new defaulted `ReviewState` fields:
- `paths_status: Literal["not-run","skipped","ok","empty","failed"] = "not-run"`
- `paths_failure: str | None = None`
- `path_ids: list[str] = Field(default_factory=list)`
- `coverage: list[CoverageRecord] = Field(default_factory=list)`

Older checkpoints still load, and a pre-change state saved at or past `find` keeps `"not-run"`.

**When the stage runs and how it calls `map_paths`.**
- **Change review:** the stage always runs, including with `--extra-finders`. It calls `map_paths(root=<snapshot>, commit=head_sha, resolve=<explain resolver>, call=<wrapper over sdk.agent_call_structured, budget_label="paths">, changed_lines=chunks.head_changed_lines(diff), cap=21, trace_limit=30, cache_dir=<target>/paths-cache)`. Changed lines are head-side `+` lines only. Deleted files and pure deletions are absent, and renamed files appear under their head path.
- **`--tree` or `--dir` with `--paths`:** the stage passes lines `1..N` of every `target.tree_files` file.
- **`--tree` or `--dir` without `--paths`:** the stage sets `paths_status="skipped"`. It makes no call, spends nothing, writes nothing and records nothing.

**Writing `paths.json`.** Any returned `PathsResult`, including one with zero paths, is written via `write_paths_file(result, <target>/paths.json)`. The stage sets `path_ids` in file order and `paths_status` to `"ok"` or `"empty"`.

**Classifying drops and broken paths.** `paths_io.classify_drop` and `paths_io.classify_broken` map each problem to a coverage record, which is passed to `checks.add_coverage`:

| Source | Level / reason |
|---|---|
| `import_reachability`, `no_changed_line` | info / `out-of-scope` |
| `trace_limit`, `cap` | info / `over-cap` |
| `trace` | error / `trace-failed` |
| broken path | error / `broken-trace` |
| fallback failure | error / `fallback-failed` |
| zero paths | error / `mapping-empty` |
| duplicate id | error / `duplicate-id` |

`add_coverage` appends a `state.errors` line only for error-level records. The lines are:
- `paths trace <entry>: <detail>`
- `paths broken <id>: stops at hop <n>`
- `paths fallback-failed: <detail>`
- `paths mapping-failed: <message>`
- `paths mapping-empty: no paths were mapped`
- `paths duplicate-id <id>`

This is a flagged spec deviation: scope drops are info only. `checks.json` exports `coverage` as a top-level list. `review_viewer/models.py` gains an optional `Checks.coverage`, and its schema is regenerated.

**Errors and resume.**
- `RepoTrustError` and `BudgetExceededError` propagate. Nothing is recorded and no `paths.json` is written.
- Any other exception sets `paths_status="failed"` and `paths_failure=<message>`, records `mapping-failed`, and writes no `paths.json`. The stage still completes so `find` runs.
- Resume reuses `paths-cache/paths-answers/` and never re-enters a completed stage.

**Stale outputs.** `rounds.clear_stale_outputs(target_dir)` deletes:
- top-level `report-piece-*.md` and `report-path-*.md` files
- `paths.json`
- the `paths-cache/` directory

It runs only on a fresh real run after `prepare()`, never on resume or `--plan-only`.

**Docs and release.** The block adds the `SKILL.md` paths-stage and `--paths` section. It bumps multiplai-dev in `marketplace.json` and adds a `CHANGELOG.md` entry that passes `scripts/check_changelog.py`.

**How to exercise it.** `test_paths_stage.py` uses real tmp git repos and a faked `map_paths`/`sdk.agent_call_structured`. It checks stage order, the `cap=21` and changed-line arguments, schema-valid `paths.json` (including an empty one), the copy in a kept round, ledger spend under `paths`, partial-cache resume and every degradation case.

Interfaces:
- Produces: `async def run_paths(state: ReviewState, ctx) -> ReviewState` (`stages/paths.py`)
- Produces: `def head_changed_lines(diff: str) -> dict[str, list[int]]` (`chunks.py`)
- Produces: `class CoverageRecord(BaseModel): stage: Literal["paths", "find", "conventions"]; subject: str; level: Literal["info", "error"]; reason: str; detail: str = ""` (`models.py`)
- Produces: `def add_coverage(state: ReviewState, record: CoverageRecord) -> None` (`checks.py`)
- Produces: `def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None` (`paths_io.py`; returns `None` unless `state.paths_status in {"ok", "empty"}`)
- Produces: `def classify_drop(drop: PathDrop) -> CoverageRecord` and `def classify_broken(path: TracedPath) -> CoverageRecord` (`paths_io.py`)
- Produces: `def clear_stale_outputs(target_dir: Path) -> None` (`rounds.py`)
- Produces: `ReviewState.paths_enabled: bool = False`, `ReviewState.paths_status: Literal["not-run", "skipped", "ok", "empty", "failed"] = "not-run"`, `ReviewState.paths_failure: str | None = None`, `ReviewState.path_ids: list[str]`, `ReviewState.coverage: list[CoverageRecord]`

Satisfies: review-paths-stage (Paths stage runs before find on change reviews; Paths stage is opt-in for tree reviews; Change review maps paths from changed lines at head; Tree review maps paths from every reviewed line; Paths stage writes paths.json beside findings.json; Path mapping answers are cached in run state; Paths stage spend counts toward the budget; Partial mapping problems are recorded and the review continues — with *Dropped entry recorded* applied to `trace` drops only; Failed or empty mapping does not stop the review; RepoTrustError propagates from the paths stage); path-finders (Duplicate path ids are not launched twice — coverage record)

## 2. Change-review diff is split into pieces and listed by `--plan-only`

This block adds `class Piece` and `chunks.split_pieces`, and deletes `finder_chunks` together with its cap of 6 and merge loop. `FileChange` gains `head_path`, `base_path`, `status` (`added`, `modified`, `deleted`, `renamed` or `binary`) and `text`.

**Splitting rules.**
- Files split at file boundaries only. Size is `len(text)`, including file and hunk headers, and binary files count as 0 with a binary note.
- Lockfiles in `LOCKFILES = {"uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum"}` contribute no text or size. They are listed in the meta piece's `omitted`.
- Meta files go only to `meta=True` pieces placed last. They are basenames `CHANGELOG.md`, `plugin.json`, `marketplace.json`, `VERSION`, `version.txt`, `.gitignore`, `.gitattributes`, any basename starting with `LICENSE`, and any path starting with `.github/`.
- Every other file is grouped by its nearest ancestor containing `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` or `SKILL.md`. The search uses `head_tree`, or `base_tree` for deleted files. Renamed files are grouped by their head path, and files with no such ancestor fall in the root group `""`.

**Test pairing.** Tests pair with a changed module by these patterns:
- `test_<n>.py` or `<n>_test.py` ↔ `<n>.py`
- `<n>_test.go` ↔ `<n>.go`
- `<n>.test.<ext>` or `<n>.spec.<ext>` ↔ `<n>.<ext>`

The module with the longest common directory prefix wins, then lexical order. A test with no changed module stays in its own group and records no error.

**Filling and ordering.**
- Units (a module plus its paired tests, or a single file) fill pieces while `chars <= PIECE_MAX_CHARS = 50_000` (inclusive).
- A unit over budget splits module-first, with the test in a later piece. A single file over 50,000 is alone in its piece and is never truncated.
- Pieces are ordered by `(group, first file)`. Ids run contiguously from `1..K`.
- An empty diff yields `[]` with no error. A lockfile-only diff yields one meta piece.

**Storing and reusing pieces.** The orchestrator computes pieces once per change review during preparation. It uses one `git ls-tree -r --name-only` per side and stores the result in `ReviewState.pieces: list[Piece] | None = None`. Resume reuses them without splitting again. Tree and dir reviews never compute pieces.

**Plan output.** `--plan-only` prints one line per piece and a calls line:
- `piece <n>: <k> files, <C> characters [<group>] <file>, <file>, …`
- Meta pieces show `[meta]`, followed by ` omitted: <lockfile>, …` when there are omitted files.
- `piece and path finder calls: <P> pieces + up to 21 paths (each one report call and one convert call)`
- The existing `finder calls:` total stays dimension-only.

**How to exercise it.** `test_pieces.py` builds tmp git repos covering grouping, pairing, the budget boundary, oversized files, meta, lockfiles, binary, rename, deleted-file base layout and determinism. It also asserts that no stage calls `finder_chunks` and checks the `--plan-only` lines.

Interfaces:
- Produces: `class Piece(BaseModel): id: int; group: str; meta: bool = False; files: list[str]; omitted: list[str]; binary: list[str]; chars: int = 0` (`models.py`)
- Produces: `def split_pieces(diff: str, *, head_tree: frozenset[str], base_tree: frozenset[str]) -> list[Piece]` (`chunks.py`)
- Produces: `def piece_diff_text(diff: str, piece: Piece) -> str` (`chunks.py`)
- Produces: `PIECE_MAX_CHARS = 50_000` and `LOCKFILES: frozenset[str]` (`chunks.py`)
- Produces: `ReviewState.pieces: list[Piece] | None = None`
- Consumes: Block 1: `def clear_stale_outputs(target_dir: Path) -> None` (pieces are recomputed on the same fresh-run path that clears stale outputs)

Satisfies: piece-splitting (all requirements: Pieces apply to change reviews only; Split only at file boundaries; Group files by nearest package or skill directory; Pair test files with their module; Piece size budget; No limit on piece count; Shared meta piece; Lockfiles excluded from diff text; Deterministic and stable piece ids; Edge-case diffs)

## 3. Piece finders run by default through report-then-convert

The find stage now launches the unchanged dimension finders, then one piece finder per `state.pieces` entry keyed `piece@<id>` (1-based; the spec's `piece@0..2` scenario reads as `piece@1..3`), on change reviews only. Every review round starts with an empty `finder_reports` and launches the full set.

**Piece prompt.** `prompts/piece.py:build_piece` includes:
- the shared workspace and citation blocks
- the labelled file list
- the untruncated piece diff inside `prompts.untrusted.fenced("diff", ...)`, which defangs through `multiplai_core.untrusted`
- ownership-not-reading text and a request for every severity plus advice
- the report format: `file:line`, optional `Severity: HIGH|MEDIUM|LOW`, `Kind: defect|advice`, and the sentinel `NO FINDINGS`

The prompt never contains "Never read a whole file" or a HIGH/MEDIUM restriction.

**Report-then-convert driver.** `run_reported` makes these calls:
1. It calls the new `sdk.agent_call_text` with `["Read", "Grep", "Glob"]` and `cwd=<snapshot>`.
2. It stores `FinderReport` in `state.finder_reports[key]`, saves state, and atomically writes `report-piece-<id>.md` before converting.
3. It treats `""` or `NO FINDINGS` as complete with zero findings and no error.
4. Otherwise it calls `agent_call_structured(build_convert(key, text), ConvertOutput, ...)` with the same tools. The report is fenced, and the prompt says "emit exactly one item per defect or advice; do not drop, merge, re-rank or judge; copy exact quotes".
5. `map_converted` turns a null severity into `LOW`, gives findings dimension `piece` or `path`, and records `items: [{kind, severity, finding_id|null}]` only in the `<key> convert` `AgentCheck` in `checks.json`. `finder_rows` folds that check into the `<key>` row, and `kind` never reaches `Finding` or `findings.json`.

Converted findings go through the unchanged gates, verification and merge.

**Errors and resume.**
- `RepoTrustError` and `BudgetExceededError` propagate. Spend for both calls hits the ledger, and stored reports survive.
- A finder failure records find coverage `finder-failed` with the `state.errors` line `<key>: <msg>`.
- A convert failure records `convert-failed` with the line `<key> convert: <msg>`. The report is kept, the convert stays pending, and only the convert is retried on `resume`.
- A converted key makes no calls on resume.

**Removals.** `build_extra`, the `extra@<i>` path and the first-round gate are removed. `--extra-finders` and `extra_finders` are still accepted and ignored. Either prints `review: --extra-finders has no effect; per-piece finders now run by default` once to stderr.

**Viewer models.** `review_viewer/models.py` gains an optional `AgentCheck.items` and the dimension values `piece` and `path`. The schemas are regenerated.

**Docs.** `SKILL.md` gains the finder-kinds section.

**How to exercise it.** `test_reported_finders.py` fakes `sdk.agent_call_text` and `sdk.agent_call_structured`. It checks:
- 7 pieces lead to 7 piece finders plus 6 dimension finders
- 15 pieces are not capped
- 0 pieces lead to none
- report files are written
- resume after the report and after the convert
- a budget stop after 5 of 9 pieces
- an injection-text report is fenced
- one failing key does not stop the others

Interfaces:
- Produces: `async def agent_call_text(prompt: str, *, allowed_tools: list[str], model: str, effort: str, max_turns: int, cwd: Path, budget_label: str, call_timeout: float) -> str` (`sdk.py`)
- Produces: `def fenced(label: str, text: str) -> str` (`prompts/untrusted.py`)
- Produces: `def build_piece(piece: Piece, diff_text: str, *, workspace: str) -> str` (`prompts/piece.py`)
- Produces: `def build_convert(key: str, report: str) -> str` (`prompts/convert.py`)
- Produces: `class ConvertedItem(BaseModel): kind: Literal["defect", "advice"]; severity: Literal["HIGH", "MEDIUM", "LOW"]; finding_id: str | None = None`
- Produces: `class FinderReport(BaseModel): key: str; text: str; converted: bool = False; convert_error: str | None = None; items: list[ConvertedItem]; findings: list[Finding]`
- Produces: `ReviewState.finder_reports: dict[str, FinderReport]`
- Produces: `class ConvertOutput(BaseModel): items: list[ConvertItem]` where `ConvertItem` has `kind`, `severity: Literal["HIGH","MEDIUM","LOW"] | None`, `file: str | None`, `line_start: int | None`, `line_end: int | None`, `claim: str`, `citations: list[Citation]`
- Produces: `async def run_reported(key: str, prompt: str, state: ReviewState, ctx) -> None` (`stages/find.py`)
- Produces: `def map_converted(key: str, out: ConvertOutput) -> tuple[list[ConvertedItem], list[Finding]]` (`stages/find.py`)
- Produces: `def write_report_file(target_dir: Path, key: str, text: str) -> Path` (`stages/find.py`; `piece@3` → `report-piece-3.md`, `path@p7` → `report-path-p7.md`)
- Produces: `READ_ONLY = ["Read", "Grep", "Glob"]` (`stages/find.py`)
- Consumes: Block 2: `def piece_diff_text(diff: str, piece: Piece) -> str`
- Consumes: Block 2: `ReviewState.pieces: list[Piece] | None = None`
- Consumes: Block 1: `def add_coverage(state: ReviewState, record: CoverageRecord) -> None`
- Consumes: Block 1: `def clear_stale_outputs(target_dir: Path) -> None`

Satisfies: piece-finders (all requirements, with keys read as 1-based `piece@<id>`); report-then-convert (all requirements, for piece keys: Piece and path finders produce a plain-text report; A separate convert call turns each report into findings; Convert keeps every item and judges none; Empty report yields no findings without error; Severity mapping for converted items; Item kind is recorded only in checks.json; Report stored in state as soon as it returns; Resume repeats only the missing convert call; Report written as a top-level file; Reports … fenced as untrusted content; Convert spend counts toward the cost limit; Convert failure degrades without losing the report)

## 4. Path finders trace each mapped path end to end

This block adds `paths_io.views`, which reads paths in `paths.json` order with duplicate ids dropped. The path name comes from `entry.name`, `handler` comes from `entry.handler` (`{path, line, symbol}`, possibly `None`), changed lines come from `TracedPath.changed_lines_covered`, and `broken_at` comes from the trace.

**Scheduling.** After the dimension and piece tasks, the find stage launches one `path@<id>` finder per `state.path_ids` entry in file order. Ids are opaque, such as `path@p7`. It reuses the Block 3 `run_reported` driver, so reports go to `report-path-<id>.md` and the same convert, resume, budget-stop and failure rules apply. Because tasks start in list order, a budget stop launches no later path finders.

**Path prompt.** `prompts/path.py:build_path` puts the following inside one `fenced("path", ...)` block:
- the entry name and `file:line`
- the handler, if any
- the invokers
- every hop in order as `file:line — description`
- the changed lines on the path
- the e2e tests with their mocked flags, or `No e2e test is listed for this path.`

Outside the fence, the prompt instructs the finder to:
- trace from the entry through every hop to the last hop
- report defects on the path or in its use of changed code, and not unrelated ones
- "When a defect involves a changed line, cite that changed line first."
- use the `NO FINDINGS` sentinel when there is nothing to report

A broken path adds `The trace stops at hop <n>; continue from there by reading the code.` The prompt has no partial-read rule and no severity restriction.

**When no path finders run.** None launch when `load_paths` returns `None` (status `skipped`, `failed` or `not-run`) or when `path_ids` is empty. Piece and dimension finders still run unchanged, with the same keys and prompts as with an empty mapping. Resume builds path finders from the stored `paths.json` without calling `map_paths`.

**How to exercise it.** `test_reported_finders.py` uses a fake `map_paths` result with 5 paths, one duplicate id and one broken path. It checks:
- 5 finders in order
- keys distinct from every piece and dimension key and stable across rounds
- fenced hostile hop text, where `multiplai_core.untrusted`'s fence parser finds one open and one close
- a budget stop after 3 of 8
- resume launching only the 5 unstored finders
- a `p3` failure recorded as `path@p3: <msg>`

The model-dependent cross-file detection is left to the PR #295 benchmark. CI asserts only prompt content and plumbing.

Interfaces:
- Produces: `@dataclass(frozen=True) class PathView: id: str; name: str; file: str; line: int | None; handler: Anchor | None; invokers: tuple[str, ...]; hops: tuple[Hop, ...]; changed_lines: tuple[tuple[str, int], ...]; e2e: tuple[E2ETest, ...]; broken_at: int | None` (`paths_io.py`; `Hop = (file: str, line: int | None, description: str)`, `E2ETest = (id: str, mocked: bool)`, `Anchor = (path: str, line: int | None, symbol: str | None)`)
- Produces: `def views(result: PathsResult) -> list[PathView]` (`paths_io.py`)
- Produces: `def build_path(view: PathView, *, workspace: str) -> str` (`prompts/path.py`)
- Consumes: Block 1: `def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None`
- Consumes: Block 1: `ReviewState.path_ids: list[str]`
- Consumes: Block 3: `async def run_reported(key: str, prompt: str, state: ReviewState, ctx) -> None`
- Consumes: Block 3: `def fenced(label: str, text: str) -> str`
- Consumes: Block 3: `READ_ONLY = ["Read", "Grep", "Glob"]`

Satisfies: path-finders (One path finder per mapped path; No path finders without paths; Path finder keys are stable; Path finder prompt carries the full path; Path data is fenced as untrusted; Path finder follows the path end to end — prompt scenarios; Path finders read the whole snapshot with read-only tools; Path finders report all severities; Path finders use report-then-convert; Path finders run every find round; Path finder spend is bounded by the ledger; A failing path finder does not stop the review; Dimension and piece finders are unaffected by path finders); report-then-convert (*Path report file*, *Path data fenced in path finder prompt*); review-paths-stage (Downstream stages read paths from the stage output — *Resumed run uses stored paths*)

## 5. Merged findings carry the ids of the paths they lie on

After merge and before `findings.json` is written, `stages/merge.py` calls `paths_io.assign_paths(merged, load_paths(target_dir, state))`. When the input is `None` (status `skipped`, `failed`, `empty` or `not-run`), findings are returned unchanged and no error is recorded.

**Matching.**
- File paths on both sides are normalised with `posixpath.normpath`, with leading `./` and trailing `/` stripped.
- Citations whose path starts with `.review-deps/` are ignored.
- A finding lies on path P when a remaining citation's file equals an anchor's file and `line_start <= anchor.line <= line_end` (inclusive). Anchors are the entry, the non-null `handler`, and every hop.
- Anchors with no file or with a line that is not a positive integer are skipped without error.

**Output.**
- Ids are deduplicated and listed in `paths.json` order.
- Only `model_copy(update={"paths": ...})` is applied, and order is preserved.
- An empty result leaves `paths=None`, and the writer drops a `None` or empty `paths` key, so `[]` never appears.
- Ids are recomputed from the current merge and current `paths.json` on every write, including after resume.

**Schemas.** The pipeline `Finding` gains `paths: list[str] | None = None`. The viewer `Finding.paths` is generated as `{"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true}`, not required. The committed schema is regenerated, and no `kind` property is added.

**How to exercise it.** `test_path_ids.py` checks:
- hop and entry containment, inclusive bounds, a line outside the range and a matching line in a different file
- `./` normalisation
- a `.review-deps/` citation ignored while another citation still counts
- multi-match dedup and ordering
- an omitted key
- a tree review without `--paths`
- a malformed hop
- a dropped entry never assigned
- resume determinism
- schema validation: `"p01"`, `["p01","p01"]` and `[""]` are rejected, and old `findings.json` still validates

Interfaces:
- Produces: `def anchors(view: PathView) -> list[tuple[str, int]]` (`paths_io.py`; entry, handler, hops; malformed skipped)
- Produces: `def assign_paths(findings: list[Finding], result: PathsResult | None) -> list[Finding]` (`paths_io.py`)
- Produces: `Finding.paths: list[str] | None = None` (pipeline `models.py`; viewer `Finding.paths` optional unique non-empty string array)
- Consumes: Block 1: `def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None`
- Consumes: Block 4: `def views(result: PathsResult) -> list[PathView]`
- Consumes: Block 4: `@dataclass(frozen=True) class PathView: id: str; name: str; file: str; line: int | None; handler: Anchor | None; invokers: tuple[str, ...]; hops: tuple[Hop, ...]; changed_lines: tuple[tuple[str, int], ...]; e2e: tuple[E2ETest, ...]; broken_at: int | None`

Satisfies: finding-path-ids (all requirements); review-paths-stage (Downstream stages read paths from the stage output — *Skipped stage yields no path consumers*)

## 6. Markdown report gains a `## Paths` section with a gap line

This block adds `render.render_paths_section`, placed after the Code, Tests and Docs sections and before the Appendix. It reads only state, `paths.json` through `paths_io.load_paths`/`views`, and the final findings. It runs no subprocess, makes no model call and never calls `map_paths`.

**Output by `paths_status`.**

| `paths_status` | Output |
|---|---|
| `skipped` or `not-run` | Nothing. |
| `failed` or `empty` | `## Paths`, then `No paths were mapped: <paths_failure>.`, then the gap line if any. |
| `ok` | One block per path in file order. |

**Per-path block.**
- ``### Path <id>: `<entry.name>` at `<file>:<line>` ``
- `- Invoked by: <inline-code invokers>`, or `unknown invoker` when there are none
- `- Findings on this path: <n>`, followed by inline-code finding ids. Each finding counts once per id in its `paths`, and findings without `paths` count for nothing.
- `- E2E tests:` with each test followed by ` — counted` or ` — mocked (not counted)`. `  - no e2e test` is added when no non-mocked test exists.
- `Trace stops at hop <n>.` for broken paths only.

**Inert rendering.** `inline_code` replaces newlines and control characters with spaces, wraps the text in a backtick run one longer than the longest inner run, and pads with spaces when the text starts or ends with a backtick. Values such as ``## Injected [x](http://evil) `tick` `` or `<script>` therefore add no heading, link or tag.

**Gap line.** `gap_line` counts paths-stage coverage records:
- `over-cap`: `cap` and `trace_limit` drops
- `out-of-scope`: `import_reachability` and `no_changed_line` drops
- `broken`: `trace-failed`, `broken-trace` and `fallback-failed`

It renders `Gaps: <n> entries dropped (<a> over cap, <b> out of scope); <c> broken traces.` Nouns are pluralised (`1 entry dropped`, `1 broken trace`), zero sub-counts and clauses are omitted, and the line is omitted when everything is zero.

**Docs.** `SKILL.md` gains the path-ids and Paths-section text.

**How to exercise it.** `test_paths_section.py` uses fixture `paths.json`/`findings.json` with `map_paths`, the model client, `subprocess.run`, `subprocess.Popen` and `os.system` patched to raise. It checks:
- order, counts and the e2e markers
- the failed-mapping and zero-path notices
- tree review without `--paths`
- re-render after resume matching the stored file
- the hostile-value cases

Interfaces:
- Produces: `def render_paths_section(state: ReviewState, result: PathsResult | None, findings: list[Finding]) -> str` (`render.py`)
- Produces: `def inline_code(text: str) -> str` (`render.py`)
- Produces: `def gap_line(records: list[CoverageRecord]) -> str` (`render.py`; returns `""` when all counts are zero)
- Consumes: Block 1: `def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None`
- Consumes: Block 1: `class CoverageRecord(BaseModel): stage: Literal["paths", "find", "conventions"]; subject: str; level: Literal["info", "error"]; reason: str; detail: str = ""`
- Consumes: Block 4: `def views(result: PathsResult) -> list[PathView]`
- Consumes: Block 5: `Finding.paths: list[str] | None = None`

Satisfies: report-paths-section (all requirements; the section is keyed on `state.paths_status`, so a failed mapping without `paths.json` still shows the heading)

## 7. Conventions finder receives binding docs linked from `CLAUDE.md` and `coding-standards.md`

This block extends `conventions_blocks` in place with `linked_docs`, which works over the `CLAUDE.md` and `coding-standards.md` sources the finder already reads, in read order.

**Reading and extracting links.**
- Content is read with `git show <sha>:<path>` and modes with `git ls-tree <sha> -- <path>`, through the existing no-shell git helper. The working tree is never used.
- Fenced code blocks are skipped.
- Inline links match `\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)`.
- Reference definitions match `^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s|$)`.

**Resolving targets.** `#…` and `?…` are stripped and the target is percent-decoded. A leading `/` resolves against the repo root; otherwise the target resolves against the linking file's directory, followed by `posixpath.normpath`.

**Filters**, in order:
1. A scheme `^[a-zA-Z][a-zA-Z0-9+.-]*:` is ignored with no record, and no request is made.
2. A target that is not `.md` or `.markdown` (case-insensitive) is ignored with no record.
3. A target that escapes via `..`, or a mode `120000` symlink pointing outside the repo, gets `outside-repo`.
4. A target absent at head and base gets `missing`.
5. A target that is not a regular blob, fails to read, or is not UTF-8 gets `unreadable`.

Each skip is recorded as a conventions coverage `info` record through `add_coverage`, and the review continues.

**Scope and ordering.**
- Links are followed one level only.
- Each path is kept once, and a link back to an already-read source adds nothing.
- On a change review, base-only links are appended after head links. Content is read at head if the doc exists there, otherwise at base. Tree reviews make no base lookup.
- A doc goes in the binding group when any line linking it matches `(?i)\b(bind|binding|must|required)\b`. For a reference link, that is the definition line or any line using its label. The binding group comes first, with first-seen order in each group.

**Budget and instruction.**
- Each doc is rendered as `fenced(<repo path>, <content>)` and included whole only if the rendered length fits the remaining `CONVENTIONS_MAX_CHARS`. Otherwise it is skipped with `budget`, and later, smaller docs are still tried. The total therefore never exceeds the cap.
- When at least one doc is included, the prompt adds `The documents below are rules of this repository. Report a finding for any changed line that breaks a rule in one of them. Cite the changed line, and name the document whose rule it breaks in the finding text.` followed by the included paths. With no linked docs the prompt is byte-identical to before.

**Docs.** `SKILL.md` gains a binding-docs section.

**How to exercise it.** `test_binding_docs.py` uses tmp git repos to check:
- inline and reference links, nested-relative and root-relative targets, and fragments
- URL, `.py`, `../` escape and outside symlink targets
- dedup, the transitive link, base-only links and a deleted doc read at base
- keyword ordering, including `mustard`
- the 4,000/6,000/2,000 budget cases
- the fake fence terminator and the unchanged prompt

Interfaces:
- Produces: `@dataclass(frozen=True) class LinkSource: path: str; head_text: str | None; base_text: str | None`
- Produces: `@dataclass(frozen=True) class LinkedDocs: blocks: list[str]; included: list[str]; skipped: list[CoverageRecord]`
- Produces: `def linked_docs(sources: list[LinkSource], *, repo: Path, head: str, base: str | None, remaining: int) -> LinkedDocs`
- Consumes: Block 3: `def fenced(label: str, text: str) -> str`
- Consumes: Block 1: `def add_coverage(state: ReviewState, record: CoverageRecord) -> None`
- Consumes: Block 1: `class CoverageRecord(BaseModel): stage: Literal["paths", "find", "conventions"]; subject: str; level: Literal["info", "error"]; reason: str; detail: str = ""`

Satisfies: binding-rules-docs (all requirements: Linked Markdown docs are added to the conventions finder; Links are followed one level only; Link resolution; Only repository-local Markdown targets are followed; Duplicates and already-read files are not repeated; Links are collected at head and at base; Binding links are ordered first; Total conventions content stays within CONVENTIONS_MAX_CHARS; Skipped docs are recorded; Linked doc content is fenced as untrusted; Violations of linked docs are findings — prompt scenarios)

## 8. Cost estimate, budget warning, default-budget notice and `SKILL.md` cost section

This block adds `review_pipeline/costs.py`, made of pure functions and constants. No model call, `map_paths` call or ledger entry is made.

```python
PIECE_REF_USD = 14.0
PIECE_REF_CHARS = 227_000
PATH_REF_USD = 24.0
PATH_REF_COUNT = 21
OVERHEAD_FACTOR = 1.35
CONVERT_FRACTION = 0.15
```

**Estimates.**
- The piece cost uses `sum(p.chars for p in state.pieces)`, so lockfiles and binary files are excluded.
- The path cost uses 21 paths before mapping and `len(path_ids)` after, and only when the paths stage runs.
- `remaining()` on resume counts unstored keys at full price and stored-but-unconverted keys at `CONVERT_FRACTION`.

**Cost line.** It goes to stderr, rounded to whole dollars:
- Change review: `cost (approximate): pieces $<P> + paths $<Q> (up to 21 paths) = $<T>; excludes dimension finders, verification and paths mapping`
- Tree review with `--paths`: the paths-only form.
- Tree review without `--paths`: no cost line.

**WARNING.** It prints when `budget > 0` and `est.total * OVERHEAD_FACTOR >= budget - recorded_spend`. This is a flagged deviation: the threshold is overhead-adjusted so the 227 KB reference warns under $50, and a budget of 0 means no ceiling and no warning. The text is `WARNING: estimated $<T> for piece and path finders, plus dimension finders, verification and mapping, is expected to exceed the remaining budget of $<B>; the run will stop partway; continue with resume and a higher --max-cost-usd.`

**NOTICE.** It prints only on a non-resume change review whose budget came from the built-in default. `load_config` exposes `max_cost_source`, and a `review.yaml` or `multiplai.conf` value counts as explicit. The text is `NOTICE: the default 50 USD budget can stop a large change review partway, because dimension finders and verification add to the piece and path cost; resume with a higher --max-cost-usd continues without losing paid results.`

**Where the lines print.**
- In `--plan-only` output, with the same exit status either way.
- At real-run start, after pieces are computed and before the paths stage.
- On resume, only the WARNING is evaluated, from `remaining`.

The run never asks for confirmation, and the ledger stays the only stop.

**STOPPED message.** The budget STOPPED message gains `kept <R> finder reports (<C> converted); resume with a higher --max-cost-usd to continue without paying for them again.`

**`SKILL.md` Cost section.** It states:
- the 14 USD piece and 24 USD path figures on a 227 KB diff, before dimension finders and verification
- the warning that the 50 USD default stops large change reviews partway
- `resume --max-cost-usd <higher>`
- that the review warns but never refuses to start

**How to exercise it.** `test_costs.py` checks:
- the 227 KB estimate is about $14 / $24 / $38, the 113 KB estimate about half, and a 100 KB lockfile plus 50 KB diff uses 50 KB only
- tree review with and without `--paths`
- warn at 38 vs 20, silent at 10 vs 50 and 38 vs 120
- resume silent at about 12 vs 50 remaining, warns with 5 remaining
- a 5 USD run proceeds and records a budget stop
- zero spend under `--plan-only`
- the parsed `SKILL.md` figures equal the rounded constants

Interfaces:
- Produces: `@dataclass(frozen=True) class Estimate: pieces_usd: float; paths_usd: float; path_count: int; total: float` (`costs.py`)
- Produces: `def piece_usd(chars: int) -> float` and `def path_usd(n: int) -> float` (`costs.py`)
- Produces: `def estimate(state: ReviewState) -> Estimate` and `def remaining(state: ReviewState) -> Estimate` (`costs.py`)
- Produces: `def cost_lines(state: ReviewState, *, budget: float, budget_is_default: bool, resuming: bool) -> list[str]` (`costs.py`)
- Produces: `max_cost_source: Literal["default", "cli", "config"]` on the object returned by `load_config` (`config.py`)
- Consumes: Block 2: `ReviewState.pieces: list[Piece] | None = None`
- Consumes: Block 2: `class Piece(BaseModel): id: int; group: str; meta: bool = False; files: list[str]; omitted: list[str]; binary: list[str]; chars: int = 0`
- Consumes: Block 1: `ReviewState.paths_enabled: bool = False`, `ReviewState.path_ids: list[str]`
- Consumes: Block 3: `class FinderReport(BaseModel): key: str; text: str; converted: bool = False; convert_error: str | None = None; items: list[ConvertedItem]; findings: list[Finding]`
- Consumes: Block 3: `ReviewState.finder_reports: dict[str, FinderReport]`

Satisfies: find-cost-guidance (all requirements: Plan-only output states the expected piece-finder cost; Plan-only output states the expected path-finder cost; Plan-only spends nothing to produce the estimate; Budget warning when the estimate reaches the budget — overhead-adjusted threshold; Default-budget notice for change reviews; Cost warnings never block a review; Resume does not repeat the default notice as a new run start; SKILL.md documents expected cost); piece-finders (Piece finder spend counts toward the cost budget — STOPPED message on resume guidance)