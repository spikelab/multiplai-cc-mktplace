# Implementation notes: code-paths-viewer

Where the code departs from `design.md`, and why. `tasks.md` was written before the
design was last revised, so the two disagree on names and a few rules. The tests come
from `tasks.md`, so where the two conflict on an interface, the code follows `tasks.md`.
Each item says which document the code follows.

## Loading paths.json

1. **The loader lives in `server.py`, not in a new `pathsdata.py` (D1).** `inspect_paths`,
   `PathsStatus`, `paths_beside`, `paths_status_line`, `_path_files` and `_path_ranges` sit
   next to `checks_beside`, as `tasks.md` names them. D1 moved them out because
   `walkthrough status` needs the same verdict and an import from `walkthrough.py` into
   `server.py` could be circular. `walkthrough status` is in `__main__.py`, which already
   imports `server`, so that circular import cannot happen.
2. **The checks run in `tasks.md`'s order, with its four statuses (D2).** The order is:
   absent; unreadable; not JSON; no string `commit`; `commit` not exactly `head_sha`
   (stale); then `PathsResult` validation. The statuses are `absent`, `invalid`, `stale` and
   `accepted`. The log line is
   `paths.json ignored (<status>): <path> <detail>`, not D2's five reasons. Every rejected
   file still writes exactly one record, and an absent one writes none. A file that is both
   stale and invalid reports as stale. A `UnicodeDecodeError` on read is treated like an
   `OSError` (invalid), so a non-UTF-8 file cannot raise.
3. **`gitdata.REVIEW_FILES` is unchanged (D1, and the viewer-paths-loading scenario
   "paths.json read from git-backed review storage").** `REVIEW_FILES` does not read
   files. It only decides whether a folder is a review folder, it must match
   `review_pipeline.target.REVIEW_FILES`, and every review folder already has
   `findings.json`. `paths.json` is read from the same directory as `findings.json`, which
   is the only place the viewer reads a review from. `tasks.md` also says not to touch it.
4. **`TargetState.paths` holds a `PathsResult`, not a dict, and the payload is
   `model_dump(mode="json")`.** `paths.v1` has no field aliases, so this is the same output
   as D2's `by_alias=True`.

## Files and lines a path opens

5. **`allowed_paths(extra=…)` adds the files without re-normalising them (D4).**
   `paths.v1`'s `REL_PATH` already rejects absolute paths and `.`/`..` segments when the
   file is validated. A file whose hop is `../../etc/passwd` is therefore invalid as a
   whole and adds nothing. A test covers this case.
6. **`file_view` takes `extra_ranges`, not `ranges` (D5).** This is the name `tasks.md`
   uses. These ranges keep lines in view in a file over 4000 lines, but they are not added
   to `cited_ranges`, so the page does not mark hop lines as cited.

## The page

7. **The JS helpers use `tasks.md`'s names:**
   - `findingsByPath` (a `Map`), not `pathFindingMap`;
   - `resolvePathRefs` and `findingPathsSection`, not `findingPathLinks`;
   - `hasUnlistedTests` plus `UNLISTED_TESTS_NOTE`, not `pathsNote`;
   - `showPathCard`, not `showPath`.

   The card id is `path-card-<id>` (D6 said `path-<id>`). Two helpers were added beyond
   both documents: `pathTestLines` produces the e2e lines, so the "no e2e test" text is
   tested, and `NO_PATHS_TEXT` holds the zero-paths message.
8. **The tab button is `id="tab-paths"`, like every other tab, with no `data-tab`
   attribute.** `tasks.md` asked for `data-tab="paths"`, but no other tab has that
   attribute.
9. **The card model follows D7 where `tasks.md` gives less:**
   - `invokedBy` is the one string, and `alsoInvokedBy` is a list (`tasks.md` said
     `invokedBy: string[]`);
   - counted tests carry their match kind, and mocked tests their patch site, as D10
     shows them;
   - findings are `{id, severity, claim}`, because findings have no `title`;
   - `broken` also holds the full marker text.
10. **Every `paths.v1` kind has a label (D9f):**
    - `argparse`, `click` and `typer` → `CLI command`;
    - `main` → `main module`;
    - `script` → `script`;
    - `skill_command` → `skill command`;
    - `hook` → `hook`;
    - `seed` → `chosen entry point`;
    - `model` → `entry point`.

    All eight `CutReason` values have text. Node tests read the schema's enums to keep
    both maps complete.
11. **The unlisted-tests note reads "…so the e2e test results may be incomplete."** This
    is the wording in the requirement and D10. `tasks.md` dropped "the".
12. **The e2e lines are in this order: counted tests, then `no e2e test`, then mocked tests
    marked `not counted`.** On a mocked-only card, the answer to "is this covered?" comes
    first.

## walkthrough status and SKILL.md

13. **The status line has `tasks.md`'s four forms (D11 had different ones).** The forms
    are `paths: accepted <N> (<file>)`, `paths: stale <commit> (<file>)`,
    `paths: invalid (<file>)` and `paths: none`. For an accepted file, one indented line
    per path follows, giving its id, entry name and raw kind. These lines give the session
    the names D11 wanted it to have, without a second label table in Python.
14. **There is no git-based test that the `walkthrough.v1` schema and `walkthrough put` are
    unchanged against the base.** CI checkouts may not have the base ref. Two existing
    checks cover this instead: the schema-export test, and a test that `put` treats a
    `path_ids` field exactly like any other unknown field. At implementation time,
    `git diff origin/buildme/code-paths-review` showed no change to `schema/`,
    `review_viewer/walkthrough.py`, `lib/code_facts` or `skills/review`.

## Release and process

15. **The version bump (0.82.0 → 0.83.0) and the CHANGELOG entry are a separate commit
    after block 5.** `tasks.md` put them in block 1.
16. **A small CSS fix after block 5** wraps long pytest node ids inside a card. The
    problem showed up on a real review (paths-review-r1), not on the fixture.
17. **Not checked in CI.** These items are left for a manual check:
    - the walkthrough-path-guidance scenario "Overview written under the guidance";
    - clearing `is-target` when the target changes, which is covered only by a source
      check of `loadTarget`;
    - the scroll from a finding's Paths link to its card.

    A headless run against the paths-review-r1 review showed `path-card-p5` marked after
    its link from finding `f56e51a81c` was clicked.
