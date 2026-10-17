# Design: review-viewer shows paths.json (PR 4 of 4)

## Context

### Current state

PR 3 makes every change review write `paths.json` beside `findings.json`. The file follows code_facts' `paths.v1` contract. Each finding in `findings.json` records the ids of the paths it lies on (`paths_io.assign_paths`). `stages/merge.py` recomputes those ids after merging, so they always come from the same run's `paths.json`.

review-viewer does not read any of this today. These are the parts of the viewer this change touches:

- **Review loading.** In `server.py` (around lines 179–196), `checks_beside` loads `checks.json` from beside `findings.json`. It loads the file only when it validates and names the review's base and head shas. Otherwise it logs a warning and returns `None`, and the page hides the Checked tab. This is the pattern `paths.json` follows.
- **File-view allow-list.** `gitdata.allowed_paths(...)` (lines 224–257) builds the set of files the page may open: changed files plus citation paths. `file_view` refuses anything outside it with `PathNotInReview`. For files over 4000 lines, `file_view` shows only changed lines ±3 and cited ranges ±12.
- **Construction order.** The server builds the allowed set inline, inside the `TargetState(...)` constructor call (around lines 540–544). It does this before any `TargetState` exists.
- **Review files.** `gitdata.REVIEW_FILES` (line 589) names the files read for a review.
- **Front-end logic.** `static/logic.js` holds the pure page logic, exported as `ReviewLogic.<name>` and as a CommonJS module for `scripts/tests/logic.test.js`. `shownFindings` decides which findings count anywhere on the page. `groupFindings` already applies a "refuted" test.
- **Front-end rendering.** `static/app.js` renders the page with `el()` and `textContent`. It hides the Checked tab at lines 422–424 when there is no `checks.json`. `index.html` holds the tab bar (Checked at line 119) and one Help-dialog section per tab, for example `#help-checked`.
- **Dependencies.** review-viewer depends only on plain `multiplai-core` and `pydantic>=2`. It never calls a model.
- **The contract.** `code_facts.paths` defines `paths.v1` as frozen, extra-forbidding pydantic models (`PathsResult`, `TracedPath`, `PathHop`, `PathEntry`, and others). The committed schema is generated from those models. `skills/review/scripts/pyproject.toml` already pulls `code-facts` in as an editable path source.

### What the prototype established

These points correct an earlier draft of this design.

**Corrected facts about the data:**
- Findings have no `title`. A finding is shown by `severity` and `claim`.
- Hops carry `symbol_called`, and the handler carries `symbol`. Without these, a hop list is only bare `file:line` ranges, which a reader cannot follow.
- `entry.invoked_by` is a single string. `entry.also_invoked_by` is a list of `Invoker{kind, name, location}` objects.
- `hops[stops_at-1]` is the last hop that was kept. It is not the point where the trace failed. The broken-trace marker therefore reads "trace broken **after** …: <reason>".

**Corrections to the plan:**
- The Paths tab needs its own Help-dialog section.
- `allowed_paths` cannot receive `extra=_path_files(state.paths)`, because no state exists yet when it runs. The loader must run first, into a local variable.

**The settled UI shape.** This document keeps it:
- the tab label `Paths <n>`;
- the summary line, then the muted unlisted-tests note, then the cards;
- the content of each card;
- the finding-detail "Paths" section;
- the zero-paths message;
- the Summary-tab fallback;
- the `walkthrough status` `paths:` lines;
- the log record.

### Spec items flagged

- **Not testable in CI.** walkthrough-path-guidance › "Overview written under the guidance for a review with paths" describes what a session writes. CI cannot test it. It is covered by checking that the SKILL.md text exists, plus a manual check on the PR screenshot review.
- **Assumption about PR 3.** viewer-paths-panel › "Card is addressable" and finding-path-links both assume that the viewer's findings.v1 `Finding` model accepts the per-finding path-id list (`paths`). PR 3 must already have added it, because the viewer's models forbid extra keys and PR 3 writes the field. This change does not alter findings.v1. If `paths` is missing from `review_viewer/models.py`, that is a PR 3 defect and gets fixed there.
- **Behaviour the spec under-specifies for large files.** viewer-paths-panel › "Hop file outside the diff opens" says a hop link opens the file at the hop's line. For files over 4000 lines, `file_view` folds away lines that are neither changed nor cited, so the hop's line would sit inside a gap row. D5 closes this gap.
- **One extension beyond the proposal's Impact list.** `walkthrough status` gains one `paths:` line (D11). It does not touch `walkthrough.v1` or `walkthrough put`, so the "Walkthrough contract is unchanged" requirement still holds.

## Decisions

### D1. Loader in a new module `review_viewer/pathsdata.py`, called before `TargetState` is built

**Decision.** The new module `review_viewer/pathsdata.py` holds the loader and its helpers. It follows the conventions: a module docstring, then `from __future__ import annotations`, then `log = logging.getLogger(__name__)`.

```python
def paths_beside(findings_path: Path, head_sha: str | None) -> dict | None
def path_files(doc: dict | None) -> list[str]          # entry, handler, hop files
def path_ranges(doc: dict | None, file: str) -> list[tuple[int, int]]
def paths_status_line(findings_path: Path, head_sha: str | None) -> str
```

`server.py` and the `walkthrough status` command (D11) both import this module.

In `server.py`, at the review-construction site (around lines 540–544), the order becomes:

1. `paths_doc = pathsdata.paths_beside(findings_path, head_sha)`, assigned to a local variable.
2. `allowed = gitdata.allowed_paths(..., extra=pathsdata.path_files(paths_doc))`.
3. `TargetState(..., allowed=allowed, paths=paths_doc)`.

`TargetState` gains `paths: dict | None = None`. The review data payload serialises it as `paths`. The server reloads `paths.json` at the same moments it reloads `findings.json`, so a newer file is picked up together with the findings.

`gitdata.REVIEW_FILES` gains `"paths.json"`. As a result, git-backed review folders carry the file the same way they carry `findings.json` and `checks.json`.

**Alternatives considered.**
- *Put `paths_beside` in `server.py` next to `checks_beside`, as the proposal says.* This was rejected because `walkthrough status` needs the same verdict. The server serves `/walkthrough` and so imports the walkthrough code. A second import in the other direction risks a circular import.
- *Pass `extra=` from `state.paths`.* This is impossible because the state does not exist at that point (prototype finding 6).

### D2. Acceptance rules and logging

`paths_beside` performs these steps in order. The `(reason)` strings are the literal reasons it logs.

| Step | Condition | Result | Log |
|---|---|---|---|
| 1 | No file beside `findings.json` (`is_file()` is false) | `None` | none at any level (viewer-paths-loading › "Review without paths.json") |
| 2 | Reading fails (`OSError`, `UnicodeDecodeError`) | `None` | one entry, reason `unreadable` |
| 3 | `json.loads` fails | `None` | one entry, reason `not JSON` |
| 4 | `PathsResult.model_validate(data)` raises `ValidationError`. This covers a missing `commit`, a wrong type, a missing required field, and a schema id other than `paths.v1`. | `None` | one entry, reason `not paths.v1`; the detail is the first error's location and message, at most 200 chars |
| 5 | `head_sha` is falsy | `None` | one entry, reason `no head_sha` |
| 6 | `result.commit != head_sha`, compared as plain `!=` (case-sensitive; no prefix match; `base_sha` is never consulted) | `None` | one entry, reason `commit mismatch`; the detail is `commit=<commit> head_sha=<head_sha>` |
| 7 | Otherwise | `result.model_dump(mode="json", by_alias=True)` | none |

**The log record.** Every rejection writes exactly one record, as `log.warning("paths.json ignored (%s): %s %s", reason, path, detail)`. This one call satisfies two requirements:
- "names the file, the file's commit and the review's head_sha";
- "names the file and the reason".

**Exceptions.** Only the exception types named in the table are caught. There is no broad `except Exception`, so no `# noqa: BLE001` is needed. Any other exception is a bug and should surface through `_dispatch`'s 500 path.

**Why validate before comparing the commit.** A file that is both invalid and mismatched still produces exactly one log entry. Running validation first also means a missing `commit` is reported as `not paths.v1`, which is the "Missing commit field" scenario.

**Validation source.** Validation uses only `PathsResult`. It is imported as `from code_facts import PathsResult` when the package re-exports it, and otherwise from `code_facts.paths`. code_facts itself does not change. There is no local schema copy and no hand-written field check.

**Payload shape.** The payload's `paths` value is either `null` or the full validated document. It is not just the list. A file with zero paths serves `{"commit": …, "paths": [], …}`, not `null` (scenario "Accepted file with zero paths"). The front end reads `data.paths.paths`.

**Alternative considered.** *Serve the raw parsed dict instead of the model dump.* The two are equal under `extra=forbid`. The dump was chosen because the served bytes then follow the contract's field order and defaults by construction.

### D3. `code-facts` dependency

**Decision.** `review-viewer/scripts/pyproject.toml` gains the dependency `code-facts`, unpinned. Its comment reads: "validates paths.json with code_facts.PathsResult so the viewer reads exactly the paths.v1 contract the review writes". It also gains a source entry, `code-facts = { path = "../../../lib/code_facts", editable = true }`, mirroring `skills/review/scripts/pyproject.toml`. `uv.lock` is regenerated.

`lint_workspace.py` needs no change, because `lib/` packages are path sources and not workspace members.

**Trade-off.** code-facts depends on `multiplai-core[sdk]`, so review-viewer now installs `claude-agent-sdk` transitively. The viewer still never calls a model:
- it imports `PathsResult` only;
- no viewer code path reaches `run_agent`;
- a test stubs `multiplai_core.run_agent` (and any `agent_runner` entry it re-exports) to raise, then loads a review that has an accepted `paths.json` (scenario "No model call during load").

**Alternatives considered.**
- *Vendor a copy of the schema and validate with `jsonschema`.* This was rejected by spec ("SHALL NOT use a separate copy"), and a copy is exactly the drift the proposal wants to avoid.
- *Move the paths.v1 models into multiplai-core.* This is out of scope, because it would change how `paths.json` is produced.

### D4. File-view allow-list extension

**Decision.** `gitdata.allowed_paths(...)` gains a keyword `extra: Iterable[str] = ()`. Each item goes through exactly the normalisation and refusals already applied to citation paths: repo-relative, no absolute paths, no `..` escape, and the same `PathNotInReview` behaviour.

`pathsdata.path_files(doc)` returns, for each path, the entry's file, the handler's file (when a handler is recorded) and every hop's file, de-duplicated. It does **not** return:
- `also_invoked_by[].location` files (prototype finding 3);
- test files or `patch_file` (D9c).

This decision implements these scenarios: "Hop file becomes viewable", "Entry and handler files become viewable", "Files from ignored paths.json stay blocked" (an ignored file means `doc is None`, which gives `extra=()`), "Path outside the repository is not allowed", and "Allowed set unchanged without paths.json".

**Alternative considered.** *Add the path files after construction by mutating `state.allowed`.* This was rejected because it bypasses the normalisation and splits the allow-list logic across two places.

### D5. Hop lines stay visible in large files

**Decision.** `gitdata.file_view` gains a keyword `ranges: Iterable[tuple[int, int]] = ()`. These ranges are treated exactly like cited ranges (±12 context) when the file is folded (more than 4000 lines). The server's file route passes `pathsdata.path_ranges(state.paths, path)`, which is the entry, handler and hop line ranges recorded for that file. Files of 4000 lines or fewer are unaffected.

**Alternative considered.** *Accept the gap.* This was rejected because the hop link would land on a gap row, which breaks "opens that file at the hop's line".

### D6. Front-end structure

**`index.html` gains:**
- the tab button `Paths` with a count badge, placed immediately after Checked (line 119);
- the panel container;
- a Help-dialog section `#help-paths` next to `#help-checked`. It explains:
  - what a path is;
  - the ordering (paths with findings first);
  - the meaning of "not counted" and "no e2e test";
  - that hop links open the code at that line.

**`app.js` gains:**
- *Tab visibility.* The Paths tab, its count, the summary line and `#help-paths` are hidden the same way Checked is (lines 422–424) when `data.paths === null`. Nothing else on the page changes: the Summary tab renders as today, and the finding list is untouched (scenario "Absent paths.json hides the tab"). Paths visibility is independent of Checked visibility (scenario "Checked tab hidden, Paths tab shown").
- *Rendering.* `renderPathsPanel(doc, shown)` builds the panel only through `el()` and `textContent`, never `innerHTML`. Every string from `paths.json` therefore renders as literal text (scenario "Text is escaped"). It shows, in order:
  1. the summary line;
  2. the optional note;
  3. either the zero-paths message or one `<article id="path-<id>">` card per path.
- *Finding detail.* The detail view of a finding appends a "Paths" section from `findingPathLinks`. When the helper returns no entries, there is no section at all.
- *Following a path link.* `showPath(id)` does three things:
  1. selects the Paths tab;
  2. finds the card with `document.getElementById("path-" + id)`, which is a lookup and not a selector, so ids cannot inject;
  3. removes `is-target` from every card, adds it to this one, and calls `scrollIntoView({block: "start"})` (scenarios "Following a path link from a finding" and "… sorts later").
- *Existing link functions reused.* Card findings link through the existing finding-selection function. Hop, entry and handler links use the existing citation-link function (file plus line).

**Unchanged:** Slack share and export code paths (scenario "Share and export unchanged").

### D7. Pure helpers in `logic.js`

Each helper sits in a new `// --- paths ---` section with a `/* … */` contract comment and is added to the `api` object. None reads the DOM or globals, and none mutates its inputs. Maps keyed by path id are built with `Object.create(null)`, so ids such as `__proto__` are harmless.

```js
countedFindings(shown)            -> Finding[]   // shown minus refuted, using the refuted test groupFindings uses
                                                 // (factored into isRefuted(f) and exported)
pathFindingMap(paths, counted)    -> {[pathId]: Finding[]}   // counted order; reads Array.isArray(f.paths) ? f.paths : [];
                                                 // a finding on P1 and P2 appears under both
hasCountedE2e(path)               -> boolean     // true iff the counted test list is non-empty
sortPaths(paths, map)             -> Path[]      // new array: paths with map[id].length>0 first,
                                                 // each group in paths.json order (stable)
pathsSummary(paths, map)          -> string      // "<n> path|paths, <m> with findings, <k> with no e2e test"
pathsNote(doc)                    -> string|null // unlisted-tests note text, or null
findingPathLinks(ids, doc|null)   -> [{id, path: Path|null, missing: boolean}]
                                                 // [] when doc is null; dedupe keeping first position;
                                                 // missing:true marks "not in paths.json"
pathCardModel(path, onPath)       -> CardModel
kindLabel(kind)                   -> string
breakReasonText(code)             -> string
```

`CardModel` has this shape:

```js
{ id, name, kind: kindLabel(entry.kind),
  invokedBy: string,                                  // entry.invoked_by
  alsoInvokedBy: [{name, kind: kindLabel(kind), where: "file:line"}],   // plain text
  entry:   {file, line} | null,                       // null → "none recorded"
  handler: {symbol, file, line} | null,               // null → "none recorded"
  hops:    [{n, symbol, file, lineStart, lineEnd}],   // symbol = hop.symbol_called
  broken:  null | {after, file, line, code, text},
  findings:[{id, severity, claim}],
  tests:   {counted: [{id, match}], mocked: [{id, patchFile, patchLine}], none: boolean} }
```

**Caller contract.** `app.js` computes `shown = shownFindings(...)` with its current arguments, including the critical-mode setting. It then passes `countedFindings(shown)` into every paths helper. Path counts therefore follow the same filter as every other count on the page. Decided (but not refuted) findings count, because `shownFindings` keeps them. This matches spec › "Counted findings".

**The `broken` rule** (prototype finding 4):
- `broken` is set only when the path records a break.
- If `stops_at > 0`, then `after` is `hops[stops_at-1].symbol_called` and the location is that hop's file and line.
- Otherwise, `after` is `handler.symbol` at the handler's location.
- If there is also no handler, `after` is `entry.name` at the entry's location.
- The card shows `trace broken after <after> (<file>:<line>): <text>`.
- The card still lists the hops that were reached.

**Refuted findings and path links.** `findingPathLinks` takes the finding's raw ids, not counted findings. Refuted findings therefore keep their links (scenario "Refuted finding still lists its paths").

### D8. Field map and drift guard

The helpers read only the fields below. Every other field (`dropped`, `diagnostics`, fallback status, `scoped`, `cap`, `changed_lines_covered`) is never read. The "Excluded fields not shown" scenario therefore holds by construction.

| Use | paths.v1 field | Confirmed by prototype |
|---|---|---|
| file check | `commit` | yes |
| cards | `paths[]`, `paths[].id` | yes |
| entry | `entry.name`, `entry.kind`, `entry.invoked_by` (string), the entry's location | yes (location key names: take from `PathEntry`) |
| also-invoked | `entry.also_invoked_by[]` → `kind`, `name`, `location` | yes |
| handler | `handler.symbol` and its location; `handler` may be absent | yes |
| hops | `hops[].symbol_called` and each hop's file and line range | yes (range key names: take from `PathHop`) |
| break | `stops_at`, `break_reason` | yes |
| tests | the counted and mocked test lists under `tests`; each test's node id and match kind; a mocked test's `patch_file` and line | yes (list key names: take from the models) |
| note | the record of test files that could not be listed (e2e-test-listing capability) | key name: take from `PathsResult` |

**Drift guard.** Three tests keep the field map and the contract in step:
- *Python.* `test_paths_example_fixture_is_valid_paths_v1` validates `scripts/tests/fixtures/paths.example.json` with `PathsResult`. It also asserts that the fixture's `commit` equals `findings.example.json`'s `head_sha`, and that every finding path id exists in the fixture. Any rename in code_facts therefore breaks the fixture and this test.
- *Node, kinds.* `logic.test.js` reads `../../../../lib/code_facts/code_facts/schema/paths.v1.schema.json`. It walks the schema for the `enum` and `const` values of the entry `kind` property and asserts that `kindLabel` has an explicit label for each one.
- *Node, break reasons.* The same test does the same check for `break_reason` against `breakReasonText`.

`logic.test.js` also asserts that `pathCardModel` produces non-empty `symbol` values for every hop in the fixture. This keeps the card followable (prototype finding 2).

### D9. Answers to the prototype's open questions

**(a) `break_reason` display.**
*Decision:* map the code to words; the raw code goes in the marker's `title` attribute. Known mappings:
- `unconnected_hop` → `the next call could not be connected`
- `base_side_citation` → `a hop cited the old code, not the change`
- `hop_limit` → `the hop limit was reached`

Every other enum value gets a label, enforced by D8. An unknown code at runtime falls back to the raw code.
*Why:* the raw codes are internal names, and the reader needs to know why the trace stopped.

**(b) Refuted-only paths.**
*Decision:* keep the spec. The card lists no findings, the path sorts among paths without findings, and the refuted finding still links to the card.
*Why:* listing the finding muted as "(refuted)" would break viewer-paths-panel › "Refuted finding is not counted" ("P's card lists no findings") and "No findings on the card".

**(c) Linking e2e tests.**
*Decision:* test node ids and patch sites stay plain text. Test files and `patch_file` are not added to the allow-list.
*Why:* the spec limits the allow-list to entry, handler and hop files. The node id is enough to run the test.

**(d) Marking hops that touch changed lines.**
*Decision:* no per-hop marker in this PR.
*Why:*
- The only producer-side signal is `changed_lines_covered`, which is a non-goal.
- A client-side computation would need per-line diff data for every hop file, and the page loads that data per file.
- Following a hop link opens the file view, where changed lines are already highlighted.
- The walkthrough rule is applied by the session, which has the full diff (D11).

**(e) Summary line placement.**
*Decision:* the Paths panel only. The Summary tab is unchanged, which keeps "renders exactly as today" trivially true and keeps share and export untouched.

**(f) Kind labels.**
*Decision:* labelled through `kindLabel`:
- `argparse` → `CLI command`
- `skill_command` → `skill command`
- `hook` → `hook`
- `script` → `script`
- the fallback kind `model` → `entry point`

Every other schema value gets an explicit label (D8).
*Why:* showing the raw `model` would leak fallback status, which is a stated non-goal and is covered by the "Excluded fields not shown" scenario.

**(g) The Ctrl+K palette.**
*Decision:* paths are not added to the "Go to" palette in this PR. The tab, the finding links and the card ids cover navigation.

**(h) Mocked-only cards.**
*Decision:* keep both lines. The card shows `no e2e test` and also lists each mocked test with `not counted`.
*Why:* viewer-paths-panel › "Only mocked tests" requires both. The two lines answer different questions: "is this feature covered?" and "what exists?".

### D10. Card and panel wording

The exact strings are listed under Global Constraints. The layout is the prototype's:

- **Header:** the id, the entry name and the kind badge.
- **Invokers:** the sentence `Invoked by <invoked_by>`, followed by `Also invoked by: <name> (<kind>, <file:line>)` items when present.
- **Entry and handler:** `Entry:` and `Handler:` links, or `none recorded` when absent.
- **Hops:** an ordered list of `<symbol> at <file>:<lineStart>[-<lineEnd>]` links.
- **Break:** the broken marker, when the trace is broken.
- **Findings:** `<severity> <claim>` links, with no heading when there are none.
- **Tests:**
  - counted tests as `✓ <node id> (<match kind>)`;
  - mocked tests as `○ <node id> — not counted (patched at <patch_file>:<line>)`;
  - `no e2e test` when there is no counted test.

### D11. Walkthrough guidance and `walkthrough status`

**`SKILL.md`.** The guidance goes into a new subsection immediately after the `facts.json` walkthrough section (lines 216–235), with no heading in between. It covers these points:

- **Condition.** The guidance applies only when `walkthrough status` prints a `paths: <n> …` line, meaning the file is beside `findings.json`, its `commit` equals the full `head_sha`, and it validates as `paths.v1`. When the line is `paths: none` or `paths: ignored (…)`, the session writes the walkthrough exactly as before and names no paths.
- **Overview.** The overview names every path by its entry name exactly as listed in the status line, so a reader can match each name to its card.
- **Steps.** Steps may follow one path's hops in hop order where that explains the change better than file order. File order stays the default. Only hops whose file and line fall in changed code become steps. Other hops may be mentioned in step text but get no step of their own.
- **No structured path ids.** The guidance never tells the session to add path ids as structured data. Paths appear only in the existing free-text fields.

`SKILL.md` also gets a schema note. It states that the viewer reads code_facts' `paths.v1` (`code_facts/schema/paths.v1.schema.json`), that it validates with `code_facts.PathsResult`, and that review-viewer defines no paths schema of its own. No schema file is added under `plugins/multiplai-dev/skills/review-viewer/`.

**`walkthrough status`.** The status output gains one line from `pathsdata.paths_status_line`. Because it uses the D2 loader, the session never re-implements the validation rules. The line takes one of four forms:

| Form | When |
|---|---|
| `paths: none` | there is no `paths.json` beside `findings.json` |
| `paths: ignored (<reason>)` | the file was rejected; `<reason>` is one of the D2 reasons |
| `paths: 0 (no entry points reach the changed code)` | the file was accepted and lists no paths |
| `paths: <n> — <id> <entry name>; <id> <entry name>; …` | the file was accepted and lists paths |

`walkthrough.v1`, `walkthrough put` and the walkthrough renderer do not change. The viewer renders the stored walkthrough as written (scenario "Walkthrough rendered as stored with paths loaded").

**Alternatives considered.**
- *Have the session read `paths.json` itself.* This was rejected because the session would re-implement the commit and validity rules and could disagree with the viewer.
- *Add `path_ids` to `walkthrough.v1`.* This is a non-goal.

### D12. Tests, fixture and release

**Python tests.** These go in the viewer's test suite, written as sentence-named pytest functions with `tmp_path` and `caplog` record counting:

- *Load outcomes:*
  - matched (`paths` is populated, no log);
  - mismatched (`paths is None`, one `paths.json ignored (commit mismatch)` record);
  - invalid (`paths is None`, one record);
  - absent (no record mentions `paths.json`; the payload is otherwise identical).
- *Commit edge cases:* a 7-char prefix, `head_sha` being a prefix of `commit`, `base_sha`, a case difference, and no `head_sha`.
- *Read edge cases:* unreadable file, malformed JSON, and a schema id other than `paths.v1`.
- *Allowed set:* includes the hop, entry and handler files; excludes `..` paths; ignored files add nothing.
- *File view:* hop ranges stay visible in a file over 4000 lines.
- *Model calls:* none (D3).
- *`walkthrough status`:* the four `paths:` forms.
- *Contracts:* `walkthrough.v1` and `walkthrough put` are unchanged, checked by the existing tests.

**Node tests.** `logic.test.js` uses `paths.example.json` and `findings.example.json` to check:
- sort order;
- summary strings for 0, 1 and n paths;
- `countedFindings` excluding refuted findings and findings not shown;
- `pathFindingMap` with a finding on several paths and a finding with no or absent ids;
- `findingPathLinks` (order, dedupe, missing ids, `null` doc returns `[]`);
- the four e2e test states;
- the `broken` text;
- that no helper mutates its inputs (deep-equal snapshots before and after);
- the D8 enum label coverage.

**The fixture.** `paths.example.json` contains at least:
- a path with counted findings and a counted test;
- a broken trace;
- a path with mocked tests only;
- a path with no tests;
- a non-empty unlisted-tests record.

The same fixture drives the PR screenshot.

**Release.** The multiplai-dev version in `marketplace.json` is bumped, and `plugins/multiplai-dev/CHANGELOG.md` gets an entry, as `scripts/check_changelog.py` requires. Python tests run with `--extra dev`, because review-viewer has no `[dependency-groups]`.

## Risks / Trade-offs

- **A heavier install for a model-free tool.** The `code-facts` dependency pulls in `multiplai-core[sdk]`, and with it `claude-agent-sdk`. It is accepted so that the contract lives in one place. The no-model-call test and the "import `PathsResult` only" rule keep the behaviour guarantee.
- **Contract drift.** The UI depends on field names that code_facts owns. The fixture validation and the schema-enum tests (D8) turn any rename, or any new kind or break reason, into a failing test rather than a blank card or a leaked `model` kind.
- **Count semantics follow page mode.** In critical mode, fewer findings count, so path ordering and "with findings" change with the mode toggle. This is deliberate and matches every other count on the page, but a reviewer may see a path move between modes.
- **Decided findings count.** The spec ties counting to `shownFindings` minus refuted. A finding the reviewer has already decided on therefore still marks its path "with findings".
- **Refuted-only paths look clean.** A path whose only finding was refuted sorts with the paths that have no findings and shows no findings, although that finding links to it (D9b). This is the spec's intent; the link stays useful for context.
- **No "touches changed code" marker.** A reader cannot tell from the card which hops are in the diff (D9d). The hop link mitigates this, because the file view highlights changed lines.
- **Larger file-view allow-list.** Up to 21 paths × (20 hops + entry + handler) of extra files become viewable. They pass the same normalisation as citations and come from a file validated against a commit-matched contract, so the exposure stays read-only and confined to the repository.
- **`file_view` signature change.** Adding the `ranges` keyword changes `gitdata.file_view` for every caller. The default `()` keeps existing behaviour byte-for-byte.
- **Status-line coupling.** The SKILL.md guidance relies on the `walkthrough status` `paths:` line. If the line format changes, the guidance must change with it. Both live in the same skill and are covered by the status-line test.
- **Mocked-only cards show two lines.** `no e2e test` appears together with "not counted" tests. The spec requires this; the Help section explains it.

## Global Constraints

- File read beside `findings.json`: `paths.json`. It is added to `gitdata.REVIEW_FILES`.
- Accepted schema id: `paths.v1`. It is validated only with `code_facts.PathsResult`, with no local schema copy and no hand-written field checks.
- Commit acceptance is `paths_doc["commit"] == head_sha`: an exact, case-sensitive full-string comparison, with no prefix match and no `base_sha` match.
- Rejection log call: `log.warning("paths.json ignored (%s): %s %s", reason, path, detail)`. Exactly one per rejected file. No log at all when the file is absent.
- Rejection reasons, verbatim: `unreadable`, `not JSON`, `not paths.v1`, `no head_sha`, `commit mismatch`.
- Mismatch detail, verbatim: `commit=<commit> head_sha=<head_sha>`.
- Payload key `paths`: the value is `null` or the full validated document (`model_dump(mode="json", by_alias=True)`), never only the list. Zero paths gives a document with `"paths": []`.
- New module: `plugins/multiplai-dev/skills/review-viewer/scripts/review_viewer/pathsdata.py`, exporting `paths_beside`, `path_files`, `path_ranges` and `paths_status_line`.
- `pathsdata.paths_beside(...)` runs into a local variable before `TargetState(...)`. `allowed_paths(..., extra=pathsdata.path_files(paths_doc))`.
- `path_files` includes the entry, handler and hop files only. It never includes `also_invoked_by` locations, test files or `patch_file`.
- `gitdata.allowed_paths` gains `extra: Iterable[str] = ()`. `gitdata.file_view` gains `ranges: Iterable[tuple[int, int]] = ()`. Both defaults preserve current behaviour.
- review-viewer `pyproject.toml` dependency: `code-facts`, unpinned. Source: `code-facts = { path = "../../../lib/code_facts", editable = true }`. `uv.lock` is regenerated.
- The viewer never calls a model. Import only `PathsResult` from code_facts.
- Python modules: a docstring, then `from __future__ import annotations`, then `log = logging.getLogger(__name__)`. No broad `except Exception` in `pathsdata.py`.
- Front-end rendering uses `el()` and `textContent` only, never `innerHTML`.
- Tab label `Paths`, placed immediately after Checked. Help section id `help-paths`.
- Card element id: `path-<id>`. Target class: `is-target`, on exactly one card at a time. Lookup via `document.getElementById`.
- Summary line, verbatim format: `<n> path, <m> with findings, <k> with no e2e test` when n is 1, otherwise `<n> paths, <m> with findings, <k> with no e2e test`.
- Unlisted-tests note, verbatim: `Some test files could not be listed, so the e2e test results may be incomplete.`
- Zero-paths message, verbatim: `No entry points reach the changed code.`
- Marker for unknown ids, verbatim: `not in paths.json`.
- Mocked-test mark, verbatim: `not counted`. No-test text, verbatim: `no e2e test`.
- Broken-trace marker format, verbatim: `trace broken after <symbol> (<file>:<line>): <reason text>`.
- Missing entry or handler text, verbatim: `none recorded`.
- Kind labels: `argparse` → `CLI command`, `skill_command` → `skill command`, `hook` → `hook`, `script` → `script`, `model` → `entry point`. Every schema kind value has a label.
- Break-reason text: `unconnected_hop` → `the next call could not be connected`, `base_side_citation` → `a hop cited the old code, not the change`, `hop_limit` → `the hop limit was reached`. Every schema value has text.
- Counted findings = `shownFindings(...)` result minus refuted findings (shared `isRefuted`).
- New `logic.js` helpers go in a `// --- paths ---` section, are pure, and are each listed in the `api` object.
- `walkthrough status` lines, verbatim forms: `paths: none` | `paths: ignored (<reason>)` | `paths: 0 (no entry points reach the changed code)` | `paths: <n> — <id> <entry name>; …`.
- Not rendered and not shared: `dropped`, `diagnostics`, fallback status, `scoped`, `cap`, `changed_lines_covered`. Slack share and export output are unchanged.
- `walkthrough.v1` schema and `walkthrough put` are unchanged. No `path_ids` field.
- Fixture: `scripts/tests/fixtures/paths.example.json`. It must validate with `PathsResult`, and its `commit` must equal `findings.example.json`'s `head_sha`.
- The Node drift test reads `../../../../lib/code_facts/code_facts/schema/paths.v1.schema.json` relative to `scripts/tests/`.
- Release: bump the multiplai-dev version in `marketplace.json` and add an entry to `plugins/multiplai-dev/CHANGELOG.md`.