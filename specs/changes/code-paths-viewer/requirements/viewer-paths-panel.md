## ADDED Requirements

### Requirement: Paths tab visibility
The review-viewer page SHALL show a Paths tab next to the Checked tab only when the loaded review has a `paths.json` that the server accepted. The server accepts the file only when it is valid `paths.v1` and its `commit` equals the review's full `head_sha`. When the review has no accepted `paths.json`, the page SHALL hide the tab, its count and the paths summary line. It SHALL hide them the same way the Checked tab is hidden when there is no `checks.json`. In that case the rest of the page SHALL render exactly as it does without this change.

#### Scenario: Accepted paths.json shows the tab
- **WHEN** a review is opened whose data payload includes paths from an accepted `paths.json`
- **THEN** a tab labelled "Paths" is visible
- **AND** it sits immediately after the Checked tab in the tab bar

#### Scenario: Absent paths.json hides the tab
- **WHEN** a review is opened that has no `paths.json` beside its `findings.json`
- **THEN** no Paths tab, Paths count or paths summary line is in the rendered page
- **AND** the tab bar and the finding list match the output for the same review rendered before this change

#### Scenario: Rejected paths.json hides the tab
- **WHEN** a review is opened whose `paths.json` was rejected by the server, either because its `commit` does not match `head_sha` or because it fails `paths.v1` validation
- **THEN** no Paths tab, Paths count or paths summary line is in the rendered page

#### Scenario: Checked tab hidden, Paths tab shown
- **WHEN** a review has an accepted `paths.json` but no `checks.json`
- **THEN** the Paths tab is visible
- **AND** the Checked tab stays hidden

### Requirement: Paths tab count
The Paths tab SHALL show a count equal to the number of paths in the accepted `paths.json`.

#### Scenario: Count matches the number of paths
- **WHEN** an accepted `paths.json` lists 4 paths
- **THEN** the Paths tab shows the count 4

#### Scenario: Zero paths
- **WHEN** an accepted `paths.json` lists no paths
- **THEN** the Paths tab is visible with the count 0
- **AND** the panel shows a single message stating that no entry points reach the changed code, and shows no cards

### Requirement: Path card ordering
The panel SHALL show one card per path. Paths with at least one counted finding SHALL come first. Within each group, the cards SHALL keep their order from `paths.json`. The order SHALL be computed by a pure helper in `logic.js` that does not change its input.

#### Scenario: Paths with findings come first
- **WHEN** `paths.json` lists paths A, B, C, D in that order
- **AND** only B and D have counted findings
- **THEN** the cards appear in the order B, D, A, C

#### Scenario: No path has findings
- **WHEN** no path has a counted finding
- **THEN** the cards appear in `paths.json` order

#### Scenario: Sort helper does not mutate its input
- **WHEN** the sort helper is called with a paths array and a findings array
- **THEN** it returns a new array in the order defined above
- **AND** the input arrays have the same order and contents as before the call

### Requirement: Counted findings
The findings counted for a path SHALL be those that `shownFindings` returns for the review, excluding refuted findings, whose recorded path ids include that path's id. The same rule SHALL apply to card ordering, the findings listed on a card and the "with findings" figure in the summary line.

#### Scenario: Refuted finding is not counted
- **WHEN** the only finding that lists path P is refuted
- **THEN** P's card lists no findings
- **AND** P is not sorted among the paths with findings
- **AND** P is not counted in the summary's "with findings" figure

#### Scenario: Finding hidden by shownFindings is not counted
- **WHEN** a finding lists path P but is not among the findings that `shownFindings` returns
- **THEN** that finding does not appear on P's card and does not count toward P having findings

#### Scenario: Finding on several paths
- **WHEN** a counted finding lists path ids P1 and P2
- **THEN** the finding appears on both P1's card and P2's card

#### Scenario: Finding with no path ids
- **WHEN** a counted finding records no path ids, or its path id field is absent
- **THEN** the finding appears on no path card, and the panel renders without error

### Requirement: Path card entry point
Each card SHALL show the path's entry point, the entry point's kind and who invokes it, as recorded in `paths.json`. The kind can be, for example, a CLI command, hook, skill command or script. Each card SHALL be addressable by its path id, so that other views can link to it.

#### Scenario: Entry point details shown
- **WHEN** a path's entry point records a name, a kind and an invoker
- **THEN** the card shows all three values

#### Scenario: Card is addressable by path id
- **WHEN** the Paths tab is opened through a link that targets path id P
- **THEN** the Paths tab is selected and P's card is scrolled into view

#### Scenario: Text is escaped
- **WHEN** an entry point name, an invoker or a test name contains HTML markup such as `<script>`
- **THEN** the card shows the markup as literal text
- **AND** no element is created from it

### Requirement: Path card hops
Each card SHALL list the path's hops in order. Each hop SHALL link to its file at its line in the viewer's file view. The server SHALL add each path's entry, handler and hop files to the file-view allowed set, the same way citation paths are added, so that every hop link opens.

#### Scenario: Hops listed in order with links
- **WHEN** a path has three hops
- **THEN** the card lists the three hops in `paths.json` order
- **AND** each hop links to the file view at that hop's file and line

#### Scenario: Hop file outside the diff opens
- **WHEN** a hop's file is not changed in the review and is not cited by any finding
- **THEN** following the hop's link opens that file at the hop's line instead of returning a not-allowed error

#### Scenario: File not on any path stays disallowed
- **WHEN** the file view is requested for a file that is not changed, not cited, and not an entry, handler or hop file of any path
- **THEN** the server refuses the request as it does today

### Requirement: Broken trace indication
When a path's trace is broken, its card SHALL say so and SHALL show where the trace stopped, as recorded in `paths.json`. The card SHALL still list the hops that were reached.

#### Scenario: Broken trace shown
- **WHEN** a path records that its trace broke at a given location
- **THEN** the card shows a "trace broken" marker together with that location
- **AND** the card lists the hops reached before the break

#### Scenario: Complete trace has no marker
- **WHEN** a path's trace is complete
- **THEN** the card shows no "trace broken" marker

### Requirement: Path card findings
Each card SHALL list the counted findings on the path. Each listed finding SHALL link to that finding's detail in the findings view.

#### Scenario: Findings listed on the card
- **WHEN** two counted findings list path P
- **THEN** P's card lists both findings, each linking to its finding detail

#### Scenario: No findings on the card
- **WHEN** no counted finding lists path P
- **THEN** P's card shows no findings section and no empty findings heading

### Requirement: Path card e2e tests
Each card SHALL list the path's counted e2e tests. It SHALL list mocked tests marked "not counted". When the path has no counted e2e test, the card SHALL show "no e2e test". This applies both when the path has no tests at all and when it has only mocked tests.

#### Scenario: Counted tests
- **WHEN** a path has one counted test and no mocked tests
- **THEN** the card lists that test without a "not counted" mark
- **AND** the card does not show "no e2e test"

#### Scenario: Only mocked tests
- **WHEN** a path has only mocked tests
- **THEN** the card lists each mocked test marked "not counted"
- **AND** the card shows "no e2e test"

#### Scenario: Counted and mocked tests
- **WHEN** a path has one counted test and one mocked test
- **THEN** the card lists the counted test unmarked and the mocked test marked "not counted"
- **AND** the card does not show "no e2e test"

#### Scenario: No tests
- **WHEN** a path has no tests
- **THEN** the card shows "no e2e test" and lists no tests

### Requirement: Paths summary line
The panel SHALL show a summary line with three figures: the number of paths, the number with counted findings, and the number with no counted e2e test. A path with only mocked tests SHALL count as having no e2e test. The line SHALL be produced by a pure helper in `logic.js` in the form `<n> path(s), <m> with findings, <k> with no e2e test`. The helper SHALL use the singular "path" when `<n>` is 1 and "paths" otherwise.

#### Scenario: Mixed paths
- **WHEN** there are 4 paths
- **AND** 1 path has counted findings
- **AND** 1 path has only mocked tests and 1 path has no tests
- **THEN** the summary line reads "4 paths, 1 with findings, 2 with no e2e test"

#### Scenario: Single path
- **WHEN** there is 1 path, with no counted findings and one counted test
- **THEN** the summary line reads "1 path, 0 with findings, 0 with no e2e test"

#### Scenario: Zero paths
- **WHEN** there are no paths
- **THEN** the summary line reads "0 paths, 0 with findings, 0 with no e2e test"

### Requirement: Unlisted test files note
When `paths.json` records that some test files could not be listed, the panel SHALL show a muted note below the summary line. The note SHALL say that some test files could not be listed, so the e2e test results may be incomplete. When nothing is recorded as unlisted, the note SHALL be absent.

#### Scenario: Some test files unlisted
- **WHEN** `paths.json` records one or more test files that could not be listed
- **THEN** a muted note saying some test files could not be listed appears below the summary line

#### Scenario: All test files listed
- **WHEN** `paths.json` records no unlisted test files
- **THEN** no such note appears

### Requirement: Excluded paths.json fields
The panel SHALL NOT render the following `paths.json` data:

- `dropped`
- `diagnostics`
- fallback status
- `scoped`
- `cap`
- `changed_lines_covered`

The panel SHALL NOT add paths content to the Slack share or the export.

#### Scenario: Excluded fields not shown
- **WHEN** an accepted `paths.json` has a non-empty `dropped` list, diagnostics, a fallback status, `scoped`, `cap` and `changed_lines_covered`
- **THEN** none of those values appear anywhere in the Paths panel

#### Scenario: Share and export unchanged
- **WHEN** a review with an accepted `paths.json` is shared to Slack or exported
- **THEN** the shared or exported content is identical to the content for the same review without `paths.json`

### Requirement: Fixture-backed logic tests
`scripts/tests/fixtures/paths.example.json` SHALL be valid `paths.v1` and SHALL match `findings.example.json`. It SHALL include at least:

- a path with counted findings and a counted test;
- a path with a broken trace;
- a path with only mocked tests;
- a path with no tests.

`scripts/tests/logic.test.js` SHALL use this fixture to test sorting, the summary line, the mapping of findings to paths and the "no e2e test" rule.

#### Scenario: Fixture covers the required cases
- **WHEN** `logic.test.js` loads `paths.example.json` and `findings.example.json`
- **THEN** it finds at least one path of each of the four kinds listed above
- **AND** every path id recorded on a finding in `findings.example.json` exists in `paths.example.json`

#### Scenario: Logic tests pass on the fixture
- **WHEN** the logic tests run against the fixture
- **THEN** the expected sorted order, the summary line, the finding-to-path mapping and the "no e2e test" results all match the asserted values