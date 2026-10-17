## Why

PR 3 makes every change review write `paths.json` beside `findings.json`. The file lists the entry points that reach the changed code. Each entry carries:

- its checked call path;
- the end-to-end tests that invoke it.

Each finding records the ids of the paths it lies on. review-viewer ignores all of this today. Someone opening a review in the viewer cannot answer three questions:

- **Which features are affected?** Which user-facing features (CLI commands, hooks, skill commands, scripts) does the change reach?
- **Which features carry findings?** A finding is shown by file and line, with no link to the feature that runs it.
- **Which e2e tests apply?** Which tests should be run for this change, and which affected features have no counted e2e test at all?

The data is already produced, so the gap is only in what the viewer shows. This is PR 4 of 4. It stacks on PR 3 and completes the path work started in PR 1 (`code_facts.map_paths`, the `paths.v1` schema).

## What Changes

The review-viewer skill will:

- **Load the file.** It reads `paths.json` beside a loaded review's `findings.json`, but only when the file's `commit` exactly equals the review's full `head_sha` and the file validates as `paths.v1`. The viewer serves the file to the page with the rest of the review's data. A mismatched or invalid file is logged and ignored.
- **Show a Paths tab.** A new Paths tab sits next to Checked, with a count. It shows one card per path, listed in `paths.json` order, with paths that carry findings first. Each card shows:
  - the entry point, its kind and who invokes it;
  - the hops, linked to the code at their lines;
  - where a broken trace stopped;
  - the findings on the path;
  - the e2e tests: the counted ones, the mocked ones marked "not counted", or "no e2e test".
- **Summarise the paths.** A summary line gives the number of paths, the number with findings, and the number with no e2e test. A path with only mocked tests counts as having no e2e test. When some test files could not be listed, a muted note says so.
- **Link findings to paths.** Each finding's detail lists the paths it lies on, linked to the Paths tab. Ids that are missing from a loaded `paths.json` appear as plain text marked "not in paths.json".
- **Guide the walkthrough.** `SKILL.md` will tell the session-written walkthrough to name the paths in its overview. Steps may follow a path's hops through changed code where that explains the change better than file order.

**Goals**

- A reviewer can see from the viewer alone which features a change affects and which of them carry findings.
- A reviewer can see which e2e tests to run and which affected features have none.
- The viewer reads exactly the `paths.v1` contract that `code_facts` writes, so the two cannot drift apart.
- A review without `paths.json` renders exactly as it does today, with no empty tab and no extra text.
- The viewer still never calls a model.

**Non-Goals**

- Showing `dropped`, `diagnostics`, fallback status, `scoped`/`cap` or `changed_lines_covered`. Dropped and broken traces already appear in the `checks.json` coverage records.
- Adding paths to the Slack share or the export.
- Adding a path-id field to `walkthrough.v1` or to `walkthrough put`.
- Matching on `base_sha`, or accepting a SHA prefix.
- Changing how `paths.json` is produced.

## Capabilities

### New Capabilities

- `viewer-paths-loading`
- `viewer-paths-panel`
- `finding-path-links`
- `walkthrough-path-guidance`

### Modified Capabilities

## Impact

- **Server.** The changes are in `plugins/multiplai-dev/skills/review-viewer/scripts/review_viewer/server.py`:
  - review loading, next to the `checks_beside` SHA check (around lines 179–196);
  - the review data payload;
  - the file-view allowed set, which gains each path's entry, handler and hop files the same way citation paths are added (around lines 540–544).
- **Git data.** `review_viewer/gitdata.py` gains `paths.json` in the `REVIEW_FILES` handling (line 589). `allowed_paths(...)` (lines 224–257) gains the extra path files.
- **Front end.** The changes are in `review_viewer/static/index.html`, `app.js` and `logic.js`:
  - `index.html` gains the new tab, next to Checked (line 119);
  - `app.js` hides the tab the way Checked is hidden (lines 422–424);
  - `logic.js` gains new pure helpers for sorting paths, counting their findings and writing the summary line. The findings counted are those `shownFindings` returns, minus refuted ones.
- **Dependency.** review-viewer's `pyproject.toml` gains `code-facts` as a dependency, with an editable path source to `../../../lib/code_facts`. This mirrors `skills/review/scripts/pyproject.toml`. The viewer validates the file with `code_facts.PathsResult` against `code_facts/schema/paths.v1.schema.json`. The workspace lock (`uv.lock`) changes accordingly.
- **Tests.** The tests change as follows:
  - The viewer's Python tests cover a commit match, a commit mismatch and an invalid `paths.json`.
  - `scripts/tests/logic.test.js` covers sorting, the summary line, mapping findings to paths and the "no e2e test" text.
  - A new fixture, `scripts/tests/fixtures/paths.example.json`, matches `findings.example.json`. It includes a path with findings and a counted test, a broken trace, a path with only mocked tests, and a path with no tests. The same fixture supplies the PR screenshot.
- **Docs.** `review-viewer/SKILL.md` changes in two places:
  - the walkthrough guidance next to the `facts.json` section (lines 216–235);
  - a `schema` note that the viewer reads code_facts' `paths.v1`.
- **Release.** A multiplai-dev version bump and a `CHANGELOG.md` entry are required by `scripts/check_changelog.py`.
- **Unchanged.** The `paths.v1` contract (`paths-output-contract`), the review pipeline and `walkthrough.v1` do not change.