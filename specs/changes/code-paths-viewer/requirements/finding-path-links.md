## ADDED Requirements

### Requirement: Finding detail lists the paths the finding lies on
When a review has a loaded `paths.json`, the detail view of each finding SHALL include a "Paths" section. The section lists every path id recorded on the finding, in the order the finding records them. Duplicate ids SHALL appear only once, at their first position. Each id that matches a path in the loaded `paths.json` SHALL be shown with that path's entry point name and kind, and its path id.

#### Scenario: Finding on one known path
- **WHEN** a review with a loaded `paths.json` is open
- **AND** a finding records exactly one path id, which matches a path in `paths.json`
- **THEN** the finding's detail shows a "Paths" section with one entry
- **AND** that entry shows the matching path's entry point name, kind and id

#### Scenario: Finding on several known paths keeps the finding's order
- **WHEN** a finding records path ids `p2`, `p1`, in that order
- **AND** both ids match paths in the loaded `paths.json`, where `p1` appears before `p2`
- **THEN** the "Paths" section lists `p2` first and `p1` second

#### Scenario: Duplicate path ids are listed once
- **WHEN** a finding records path ids `p1`, `p3`, `p1`
- **AND** all of them match paths in the loaded `paths.json`
- **THEN** the "Paths" section lists exactly two entries: `p1` first, then `p3`

### Requirement: Known path entries link to the Paths tab
Each entry in a finding's "Paths" section that matches a path in the loaded `paths.json` SHALL be a link. Activating the link SHALL switch the viewer to the Paths tab and bring that path's card into view. The card SHALL be marked as the current target, so the reviewer can tell which card the link points to.

#### Scenario: Following a path link from a finding
- **WHEN** the reviewer activates the link for path `p1` in a finding's detail
- **THEN** the Paths tab becomes the active tab
- **AND** the card for `p1` is scrolled into view
- **AND** the card for `p1` is marked as the target and no other card is marked

#### Scenario: Following a link to a path that sorts later
- **WHEN** the reviewer activates the link for a path whose card is not among the first cards in the Paths tab
- **THEN** the viewer scrolls to that path's card, not to the top of the tab

### Requirement: Unknown path ids appear as plain text marked "not in paths.json"
A path id recorded on a finding that does not match any path in the loaded `paths.json` SHALL still appear in the finding's "Paths" section. It SHALL be shown as its raw id in plain text, not as a link, and marked "not in paths.json". The id SHALL keep its position in the finding's order. An unknown id SHALL NOT cause an error or hide the other entries.

#### Scenario: Mixed known and unknown ids
- **WHEN** a finding records path ids `p1`, `p9`
- **AND** the loaded `paths.json` contains `p1` but not `p9`
- **THEN** the "Paths" section shows `p1` as a link to its card
- **AND** it shows `p9` second, as plain text, marked "not in paths.json"
- **AND** `p9` has no link

#### Scenario: Every id is unknown
- **WHEN** every path id recorded on a finding is missing from the loaded `paths.json`
- **THEN** the "Paths" section shows each id as plain text marked "not in paths.json"
- **AND** the section contains no links

### Requirement: No Paths section when the finding records no path ids
A finding that records no path ids SHALL show no "Paths" section in its detail. This applies whether the path id field is absent or empty. No placeholder text shall appear either.

#### Scenario: Finding with an empty path id list
- **WHEN** a review with a loaded `paths.json` is open
- **AND** a finding's path id list is empty
- **THEN** the finding's detail contains no "Paths" section and no path-related text

#### Scenario: Finding with no path id field
- **WHEN** a review with a loaded `paths.json` is open
- **AND** a finding has no path id field at all
- **THEN** the finding's detail renders without a "Paths" section and without an error

### Requirement: No Paths section when paths.json is not loaded
When a review has no loaded `paths.json`, no finding's detail SHALL show a "Paths" section. This applies when the file is absent, when its `commit` does not equal the review's `head_sha`, and when it fails `paths.v1` validation. It applies even if findings record path ids. Finding details SHALL then render as they did before this change.

#### Scenario: Review without paths.json
- **WHEN** a review is opened that has no `paths.json` beside its `findings.json`
- **AND** its findings record path ids
- **THEN** no finding's detail shows a "Paths" section, a path id or the text "not in paths.json"

#### Scenario: paths.json ignored for a commit mismatch
- **WHEN** a review's `paths.json` has a `commit` that differs from the review's `head_sha`
- **AND** its findings record path ids
- **THEN** no finding's detail shows a "Paths" section, a path id or the text "not in paths.json"

#### Scenario: paths.json ignored for failing validation
- **WHEN** a review's `paths.json` fails `paths.v1` validation
- **AND** its findings record path ids
- **THEN** no finding's detail shows a "Paths" section, a path id or the text "not in paths.json"

### Requirement: Path links appear on every finding, refuted ones included
The "Paths" section SHALL appear in the detail of every finding that records path ids. This includes refuted findings and findings the Paths tab leaves out of its counts. Excluding refuted findings from path finding counts SHALL NOT remove path links from those findings' details.

#### Scenario: Refuted finding still lists its paths
- **WHEN** a refuted finding records path id `p1`
- **AND** `p1` exists in the loaded `paths.json`
- **THEN** the refuted finding's detail shows a "Paths" section with a link to `p1`

### Requirement: Finding-to-path resolution is a pure helper in logic.js
`logic.js` SHALL export a pure function. Its inputs are a finding's recorded path ids and the loaded paths, or the absence of paths. It returns the ordered, de-duplicated list of entries. Each entry carries the id and either the matching path or a "not in paths.json" marker. When no paths are loaded, the function returns an empty list. The function SHALL NOT read the DOM or global state, and SHALL NOT change its inputs. `scripts/tests/logic.test.js` SHALL cover it using `scripts/tests/fixtures/paths.example.json`.

#### Scenario: Resolving against the example fixture
- **WHEN** the helper receives ids that exist in `paths.example.json` and one id that does not
- **THEN** it returns one entry per distinct id, in input order
- **AND** each existing id's entry references its path from the fixture
- **AND** the missing id's entry is marked "not in paths.json"

#### Scenario: Resolving with no paths loaded
- **WHEN** the helper receives a non-empty list of ids, and no paths are loaded
- **THEN** it returns an empty list

#### Scenario: Inputs are left unchanged
- **WHEN** the helper is called with an id list and a paths object
- **THEN** both inputs are deeply equal to their state before the call