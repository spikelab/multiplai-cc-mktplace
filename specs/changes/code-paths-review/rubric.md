# Evaluation Rubric

Score each dimension 1–5. Levels 4 and 2 sit between the anchors below. The weighted total is Σ(score × weight) / Σ(weight × 5).

Scope covers all nine task blocks:

1. Piece splitting
2. Piece finders and report-then-convert
3. `--extra-finders` no-op
4. Paths stage
5. Path finders
6. Path ids on findings
7. `## Paths` report section
8. Binding docs
9. Cost guidance and release

## Code Architecture (weight: 2)

Score against the repository's own documented standards first: `CLAUDE.md`, `coding-standards.md`, and any linked docs these reference. Where the repo documents a pattern, it wins over the fallback baseline, and you suppress any smell it endorses. Use the twelve-smell baseline only where the repo is silent. Phrase every baseline item as a heuristic ("possible Feature Envy"), never as a hard violation. Skip anything that ruff, the type checker or the formatter already enforces.

Points to check for this change:

- **Separation of concerns.** Each module should own one job:
  - `chunks` handles diff parsing and piece packing.
  - `reports` drives report-then-convert.
  - `paths_io` loads paths, assigns ids and computes anchors.
  - `stages/paths.py` handles stage orchestration.
  - `render` handles Markdown output.
  - `binding_docs` collects linked docs.
  - `cost` holds the estimate arithmetic.
  - `config` handles the notice.
- **Shared driver.** Piece and path finders should share `reports.run_report_finder` rather than duplicate the find → store → write → convert flow.
- **Error dispatch.** The `RepoTrustError`, `BudgetExceededError` and `AgentCallError` ordering should live in one clear place per call site and not be scattered.
- **Single source for cost constants.** `REFERENCE_DIFF_KB`, `PIECE_USD_REFERENCE`, `PATH_USD_REFERENCE`, `PATHS_CAP` and `PIECE_MAX_CHARS` should each be defined once. `cap=21` should not be duplicated as a magic number.

| Score | Criteria |
|-------|----------|
| 5 | **Boundaries:** Module boundaries match the task interfaces exactly, with signatures as specified (`split_pieces`, `run_report_finder`, `to_findings`, `is_pending`, `assign_paths`, `anchors_for`, `render_paths_section`, `inline_code`, `collect`, `piece_estimate`, `path_estimate`, `messages`).<br>**Shared driver:** Piece and path finders share one driver. The only kind-specific parts are the prompt builder, the key and the labels.<br>**Removals:** `finder_chunks`, `build_extra` and the `extra@` dispatch are fully removed, with no dead branches left.<br>**Pure helpers:** `assign_paths`, `to_findings`, `anchors_for` and the cost helpers are pure functions with no I/O. Rendering makes no subprocess, model or `map_paths` call.<br>**Types:** Domain concepts use named types (`PieceSpec`, `FinderReport`, `ReportItem`, `ConvertOutput`, the `paths_status` Literal) rather than bare dicts or strings.<br>**Constants:** Each constant is defined once and imported where used.<br>**Smells:** No meaningful baseline smells remain unaddressed. Repo conventions are followed throughout. |
| 3 | **Overall:** The structure is broadly right, with some coupling or duplication.<br>**Examples:**<br>- Piece and path finder flows partly copy each other.<br>- Error-type ordering is repeated in several places.<br>- `render` reaches into stage internals.<br>- A constant such as `21` or `50_000` is restated as a literal.<br>- One or two possible baseline smells (Data Clumps, Primitive Obsession on status strings, Shotgun Surgery across stages) are present but contained.<br>**Leftovers:** Some leftover dead code from the old chunker or extra finders. |
| 1 | **Overall:** Responsibilities are tangled.<br>**Examples:**<br>- The report-then-convert logic is inlined separately into the piece and path paths.<br>- The renderer calls `map_paths` or the model.<br>- `assign_paths` mutates its inputs or reorders findings.<br>- Old `finder_chunks` or `extra@` dispatch still runs alongside the new code.<br>- Status and kind values are untyped strings compared ad hoc.<br>- Interfaces diverge from the task contract, so dependent blocks cannot consume them as specified. |

## Test Quality (weight: 1)

Expected suites:

- `test_pieces.py`
- `test_piece_finders.py`
- `test_reports.py`
- `test_extra_finders.py`
- `test_paths_stage.py`
- `test_path_finders.py`
- `test_path_ids.py`
- `test_render_paths.py`
- `test_binding_docs.py`
- `test_cost.py`
- `_viewer_contract.py`
- the `SKILL.md` mention tests

Tests should assert observable outcomes, such as:

- piece ids, chars and meta flags
- `findings.json`, `checks.json` and `paths.json` contents
- report files on disk
- stderr notice lines
- exact prompt text and fence counts
- coverage entries
- the number of SDK calls made on resume
- rendered Markdown
- exit codes

Mock-call assertions alone do not count. Stubs for `agent_call_text`, `agent_call_structured` and `map_paths` must honor the real contracts:

- raising the real exception types
- `RepoTrustError` subclassing `AgentCallError`
- `map_paths` taking `recoverable=` and `cache_dir=`
- `PathsResult` fixtures built through the real model

Score down for these anti-patterns:

- `.called` or `assert_called_*` used with no outcome assertion
- mock setup outweighing assertions
- contract-violating fakes
- production hooks added only for tests
- fixed sleeps

| Score | Criteria |
|-------|----------|
| 5 | **Edge cases:** Every listed edge case has a test with a meaningful assertion:<br>- empty diff and lockfile-only diff<br>- 50,000/50,001 boundary<br>- `\x0c`/`\u2028` phantom sections and the `++ b/x` header confusion<br>- C-quoted `café` paths and Latin-1 decoding<br>- shallow-clone exit 128<br>- `.GITIGNORE` and `LICENSES/MIT.txt` not meta<br>- deterministic re-split<br>- budget stop at 5 of 9 and 3 of 8, followed by an exact resume launch count<br>- stop between finder and convert, giving exactly one convert call<br>- unparseable convert retried without a finder call<br>- `RepoTrustError` precedence<br>- `extra@1` checkpoint resumed twice with no model call<br>- `True` rejected as a line number<br>- a `model_construct` hop with no line<br>- a legacy `findings.json` that still validates<br>- schema rejection of `"p1"`, `["p1","p1"]` and `[""]`<br>- injection strings rendered only in code spans<br>- `outside-repo`, `unreadable` and `budget` binding-doc cases against a real throwaway git repo<br>- golden-file `cmp` of the no-link prompt<br>- `nan`, `inf` and negative `--max-cost-usd` exiting 2<br>- `--plan-only` with SDK and `map_paths` patched to raise<br>**Stubs:** Stubs honor the real signatures and exception hierarchy.<br>**Assertions:** Assertions target persisted state, files and output text. |
| 3 | **Coverage:** Main scenarios are covered, but several edge cases are missing or weakly asserted.<br>**Examples:**<br>- Resume tests check that the call happened without checking which keys were launched.<br>- Fence tests use substring presence instead of exact marker counts.<br>- Some stubs return shapes the real SDK would not return.<br>- A few tests interrogate only mocks.<br>- Binding-doc tests mock git instead of using a fixture repo for some cases. |
| 1 | **Coverage:** Tests are mostly absent, trivial or mock-only.<br>**Examples:**<br>- Boundary, resume, budget-stop and injection cases are untested.<br>- Stubs ignore the exception hierarchy, so trust-error precedence is never exercised.<br>- Tests pass only because of test-only production methods.<br>- Sleeps are used for sequencing.<br>- Schema and viewer contract validation is skipped. |

## Spec Compliance (weight: 3)

Verify each WHEN/THEN scenario in all nine specs, as amended by D17 and D18. Each scenario must be both implemented and tested.

The amendments to check:

- keys are `piece@1..`
- a lockfile-only meta piece launches no finder and is recorded as `skipped` with reason `lockfiles-only`
- 'Duplicate path ids are not launched twice' is deleted
- 'Path with no recorded invoker' and `unknown invoker` are deleted
- the tree review without `--paths` prints no estimate
- path-id placement uses range form
- the uncited-item and citation-reorder scenarios are covered

| Score | Criteria |
|-------|----------|
| 5 | **Piece splitting**<br>- Splitting runs only on change reviews.<br>- Grouping uses head markers, base markers for deleted files, and the head path for renames.<br>- Test pairing follows the stated order and tie-breaks.<br>- Packing is greedy up to `PIECE_MAX_CHARS` inclusive. Oversized pairs split with the module first, and an oversized single file is not truncated.<br>- Meta-file rules are case-sensitive, and the `.github` component rule applies.<br>- Lockfiles go in the first meta piece's `omitted` list at 0 chars.<br>- `--plan-only` prints the exact `pieces:` and `piece <id>:` lines with no model call.<br>**Piece finders**<br>- Finders use the exact tools, `cwd`, budget labels and prompt contents: no web text, no "Never read a whole file" and no HIGH/MEDIUM-only rule.<br>- Dimension finders are unchanged.<br>**Report-then-convert**<br>- Blank text gives an empty report with no convert call.<br>- `extra="forbid"` is set.<br>- Severity is case-normalized, and missing severity defaults to `LOW`.<br>- The first changed-file citation moves to index 0.<br>- Items are never filtered or merged.<br>- On convert failure, `findings=None` and only the convert call is retried.<br>- A missing report file is rewritten from state on resume.<br>**`--extra-finders` no-op**<br>- The option is accepted in every form, including `false`, `0` and `null`.<br>- The notice prints once per process.<br>- The tree refusal is removed.<br>- Retired `extra@` keys never loop.<br>**Paths stage**<br>- The stage runs before `find`.<br>- It is idempotent once `paths_status` is set.<br>- The empty, failed, skipped and coverage table states are correct.<br>- `paths.json` is written even with zero paths, and not on failure.<br>- `clear_stale_outputs` deletes only the strictly matching files.<br>**Path finders**<br>- One finder runs per path, in file order.<br>- Broken traces still get a finder.<br>- The prompt includes every field, fenced.<br>**Path ids**<br>- `paths` is omitted when `None` via the wrap serializer.<br>- It is never part of `finding_id`.<br>- Ids are in `paths.json` order and deduplicated.<br>- `.review-deps/` citations are ignored.<br>**`## Paths` section**<br>- Each `paths_status` gives the exact literal text.<br>- Entry format follows the spec.<br>- Counts are per id.<br>- The gaps line lists only non-zero parts.<br>- `inline_code` escaping is correct.<br>**Binding docs**<br>- Link forms are handled correctly.<br>- Target normalization and the `outside-repo` and `..notes.md` rules are correct.<br>- Files are read through git objects, and symlinks are followed once.<br>- Binding docs are ordered first.<br>- The whole-or-skip budget includes the rule block.<br>- The no-link prompt is byte-identical.<br>**Cost guidance**<br>- The formulas are exact, and 21 paths give `~$24`.<br>- The warning appears under the stated conditions.<br>- The default-budget notice never appears on resume.<br>- `--plan-only` makes no SDK, `map_paths` or ledger call.<br>**Docs and release**<br>- `SKILL.md` covers every documented item.<br>- `marketplace.json` is bumped, `CHANGELOG.md` has an entry, and `check_changelog.py` passes. |
| 3 | **Overall:** Most requirements are met, but some scenarios deviate or are untested.<br>**Examples:**<br>- An off-by-one at the 50,000 boundary.<br>- The meta check is case-insensitive.<br>- Severity is not normalized.<br>- The notice prints twice in `batch`.<br>- Resume re-maps paths.<br>- The gaps line lists zero parts.<br>- The budget charge omits the rule block.<br>- Cost estimates use bytes instead of code points.<br>- One or two D17 amendments are not applied.<br>- `SKILL.md` or CHANGELOG items are missing. |
| 1 | **Overall:** Major requirements are missing or wrong.<br>**Examples:**<br>- Pieces are computed for tree reviews.<br>- Piece finders are gated behind a flag.<br>- Finders return structured output with no report.<br>- Convert filters or merges items.<br>- Paths run after `find` or not at all.<br>- Path ids are assigned during merge or change finding order.<br>- The `## Paths` section calls the model.<br>- Binding docs follow URLs or escape the repo.<br>- `--plan-only` makes model calls.<br>- Many scenarios lack any test. |

## Integration Coherence (weight: 2)

Check how the pieces fit end to end. The chain to follow is:

1. CLI and config flags (`--paths`, `--extra-finders`, `--max-cost-usd`)
2. `ReviewState` fields (`pieces`, `finder_reports`, `paths_status`, `paths_reason`, `paths_requested`)
3. The stage order: `explain` → `paths` → `find` → merge → `assign_paths` → write `findings.json`
4. The persisted outputs: `paths.json`, `report-*.md`, `findings.json`, `checks.json`
5. The viewer `Finding` and `ReportItemEntry` models, with dimensions `piece` and `path`
6. The regenerated JSON schemas
7. The Markdown renderer

Also check that:

- Resume semantics are consistent everywhere.
- Trust and budget errors propagate the same way through the paths stage, piece finders and path finders.
- Untrusted text is fenced at every model-prompt boundary: piece diffs, reports going to convert, path data and binding docs.
- Every `paths.json` value is rendered inertly in Markdown.

| Score | Criteria |
|-------|----------|
| 5 | **Producers and consumers**<br>- Every producer and consumer in the interface chain agrees on field names, types and optionality.<br>- The schemas are regenerated with `_schema_text`.<br>- `_viewer_contract.py` validates both files with `jsonschema`.<br>- `checks.json` piece and path items carry `{kind, severity, finding_id}`.<br>**Old checkpoints**<br>- Old checkpoints and an old `findings.json` still load.<br>**Resume**<br>- Resume behaves consistently everywhere: a pending check uses `is_pending`, and the paths stage does not re-run.<br>- Retired keys are recorded once.<br>- Report files are rewritten from state when missing.<br>**Error propagation**<br>- `RepoTrustError` is caught before `AgentCallError` at every call site, and is re-raised after the drain.<br>- `BudgetExceededError` passes through `map_paths` unchanged.<br>- Each `run_find` call gets a fresh stop flag.<br>**Untrusted text**<br>- All untrusted text goes through `untrusted_block` with `defang`.<br>- All rendered values go through `inline_code`.<br>**Cost messages**<br>- Cost messages appear at the specified points: `--plan-only`, and before the paths stage on real runs and resumes.<br>- They never change the exit status. |
| 3 | **Overall:** The components connect, with isolated mismatches.<br>**Examples:**<br>- A schema is not regenerated, or the viewer is not updated for `path`.<br>- `paths_requested` is not persisted, so resume drops `--paths`.<br>- One call site checks `AgentCallError` before `RepoTrustError`.<br>- One prompt or render path skips fencing or escaping.<br>- The budget warning is missing on resume.<br>- An old checkpoint loads, but a retired `extra@` key is recorded repeatedly. |
| 1 | **Overall:** The components disagree.<br>**Examples:**<br>- The viewer or schema rejects the new findings or `checks.json` output.<br>- Old checkpoints fail to load or loop on resume.<br>- Trust errors are swallowed as recoverable `PathsCallError`.<br>- Path ids reference paths absent from `paths.json`.<br>- Untrusted report, path or doc text reaches prompts unfenced, or reaches Markdown unescaped.<br>- Stage order lets `find` run before `paths`, or lets `findings.json` be written before `assign_paths`. |