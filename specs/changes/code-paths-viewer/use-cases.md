# Use cases — who this change is for

## Personas

### reviewer
- **Who they are:** An engineer on the multiplai-dev repo. They have opened a teammate's change review in review-viewer after the review pipeline (with PR 3) wrote `findings.json`, `checks.json` and `paths.json`.
- **The job they are doing:** Deciding whether the change is safe to merge. They need to know which user-facing features the change touches (CLI commands, hooks, skill commands, scripts) and which of those features the findings actually affect.
- **The constraint they are under:** They have limited time and read the review in the browser, not the code. Today they see each finding only as a file and line, with no link to the feature that runs it.

### change author
- **Who they are:** The engineer who wrote the change. They are reading their own review in review-viewer before asking for approval or merging.
- **The job they are doing:** Choosing which end-to-end tests to run for this change, and spotting affected features that have no real e2e coverage.
- **The constraint they are under:** They cannot afford to run the whole e2e suite on every change. Tests that mock hop 2 look like coverage but do not count, so the author needs the viewer to tell real tests apart from mocked ones.

### walkthrough reader
- **Who they are:** A reviewer new to the changed area. They open the walkthrough that a Claude session wrote for the review in review-viewer.
- **The job they are doing:** Understanding what the change does by following how a feature's call reaches the changed code, not by reading files in arbitrary order.
- **The constraint they are under:** They do not know the codebase well enough to rebuild the call flow themselves. The walkthrough is written by a session that follows `SKILL.md` and never by the viewer, which calls no model.

### returning reviewer
- **Who they are:** An engineer who reopens an older review, or a tree review run without `--paths`. These reviews have no `paths.json`. The same engineer may also open a review whose `paths.json` is from a different commit or is invalid.
- **The job they are doing:** Reading a review they rely on, in the same layout they already know.
- **The constraint they are under:** They cannot regenerate the review. They should not be shown an empty tab, a broken panel, or paths that belong to another commit.

## Use cases

- reviewer wants to see every entry point the change reaches, with its kind and who invokes it, so that they can name the user-facing features affected by reading the Paths tab alone.
- reviewer wants paths that carry findings listed first, so that the features with problems are at the top of the Paths tab without scrolling or filtering.
- reviewer wants each path card to list the findings that lie on it, linked to those findings, so that they can click from a feature to the specific finding and read it.
- reviewer wants each finding's detail to list the paths it lies on, linked to the Paths tab, so that they can see which feature a finding breaks without leaving the finding.
- reviewer wants each hop of a path shown as a link to the code at that line, so that clicking a hop opens the file view at that line. A hop whose file cannot be served shows as plain `file:line` text instead.
- reviewer wants a broken trace to show where it stopped, so that they can see on the card how far the path was checked before it broke.
- reviewer wants a summary line above the paths, so that they can read at a glance how many paths there are, how many carry findings and how many have no e2e test.
- reviewer wants the counts and the order of paths to stay the same while they change the findings status filter, so that the summary line and card order do not shift as they filter.
- reviewer wants a finding's path ids that are missing from the loaded `paths.json` shown as plain text marked "not in paths.json", so that they can tell a stale id from a valid path because the stale one has no link.
- change author wants each path card to show "E2E tests:" followed by the counted tests, so that they have a list of the end-to-end tests to run for this change.
- change author wants tests that mock hop 2 listed as "mocked (not counted)", so that they can see on the card that those tests do not exercise the real path.
- change author wants a path with no counted tests to read "no e2e test", including when it has only mocked tests, so that they can count from the page the affected features that lack real e2e coverage.
- change author wants a muted note under the summary when some test files could not be listed, so that they know a "no e2e test" result may be incomplete before acting on it.
- walkthrough reader wants the walkthrough overview to name the paths the change lies on, so that they read which features are affected before the first step.
- walkthrough reader wants steps to follow a path's hops through the changed code, naming the path id in the text, so that the steps read in the order a call reaches the change rather than in file order.
- returning reviewer wants a review without `paths.json` to render as it does today, so that the page shows no Paths tab, no empty panel and no path lines in any finding's detail.
- returning reviewer wants a `paths.json` from a different commit than the review's head, or one that fails validation, to be ignored, so that the page shows no Paths tab instead of paths that do not match the code under review.