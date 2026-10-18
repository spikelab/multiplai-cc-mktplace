## ADDED Requirements

### Requirement: Path hops are re-checked at HEAD
`check --at-head` SHALL re-check every hop link in the **Paths** section of a `<name>-walkthrough.md`. A hop link is a `path:line` link whose citation id matches `p{n}h{k}`. The verification SHALL be the same as the existing scenario-hop re-check: a hop passes or fails under exactly the same conditions as a scenario-hop link with the same `path:line` target.

#### Scenario: Unchanged hops pass
- **WHEN** a walkthrough's Paths section contains hop links `p1h1`, `p1h2` and `p2h1`
- **AND** none of the cited files or lines have changed since the walkthrough was written
- **AND** `check --at-head` is run on it
- **THEN** the check reports no failure for any of the three hops
- **AND** it exits with the same success status it uses today for a passing walkthrough

#### Scenario: Hop and scenario-hop verdicts agree
- **WHEN** a Paths hop link and a legacy scenario-hop link cite the same `path:line`
- **AND** `check --at-head` is run on each document against the same HEAD
- **THEN** both links get the same verdict, either both pass or both fail

#### Scenario: Every hop in a multi-path section is checked
- **WHEN** a Paths section has 21 paths with varying hop counts
- **AND** `check --at-head` is run with a verbose or count report where one is available
- **THEN** the number of hop links checked equals the number of `p{n}h{k}` hop links in the section

### Requirement: Moved or removed hop lines fail the check
`check --at-head` SHALL report a failure for a hop whose cited line no longer verifies at HEAD. The failure SHALL name the hop id and the cited `path:line`.

#### Scenario: Cited line edited
- **WHEN** the source line cited by hop `p2h3` is changed in a commit after the walkthrough was written
- **AND** `check --at-head` is run
- **THEN** the check fails
- **AND** the report includes `p2h3` and the cited `path:line`

#### Scenario: Cited file deleted
- **WHEN** the file cited by hop `p1h1` is deleted at HEAD
- **AND** `check --at-head` is run
- **THEN** the check fails
- **AND** the report identifies `p1h1` as the failing hop

#### Scenario: Cited line beyond end of file
- **WHEN** a hop cites `path:line` and the file at HEAD has fewer lines than `line`
- **AND** `check --at-head` is run
- **THEN** the check fails for that hop

#### Scenario: One bad hop among many
- **WHEN** exactly one hop in a Paths section of several paths no longer verifies
- **AND** `check --at-head` is run
- **THEN** the check fails
- **AND** only that hop is reported as failing

### Requirement: Hop re-check uses the Markdown alone
The hop re-check SHALL take every hop from `<name>-walkthrough.md` alone. It SHALL NOT read `<name>-paths.json`, `<name>-walkthrough.json`, the pipeline run directory or the cached trace answers. It SHALL NOT make model calls.

#### Scenario: paths.json absent
- **WHEN** `<name>-paths.json` has been deleted from beside `<name>-walkthrough.md`
- **AND** `check --at-head` is run
- **THEN** the verdict and report are identical to a run with the file present

#### Scenario: paths.json disagrees with the Markdown
- **WHEN** `<name>-paths.json` is edited so that its hops cite different lines from the Markdown hop links
- **AND** `check --at-head` is run
- **THEN** the verdict is decided only by the hop links in the Markdown

#### Scenario: No run directory
- **WHEN** a committed walkthrough is checked in a fresh clone with no pipeline run directory present
- **AND** `check --at-head` is run
- **THEN** the hop re-check completes without error

#### Scenario: No model access
- **WHEN** `check --at-head` is run with no model credentials available
- **THEN** the hop re-check completes
- **AND** no agent or model call is attempted

### Requirement: Hop citations resolve to defined hops
Prose in other sections may cite a hop by its `p{n}h{k}` id. `check --at-head` SHALL report a failure when such a citation names an id that is not defined as a hop link in the Paths section.

#### Scenario: Valid hop citation
- **WHEN** a section other than Paths cites `p1h2`
- **AND** the Paths section defines hop `p1h2`
- **THEN** the check reports no failure for that citation

#### Scenario: Dangling hop citation
- **WHEN** a section cites `p4h1`
- **AND** the Paths section defines no hop `p4h1`
- **AND** `check --at-head` is run
- **THEN** the check fails
- **AND** the report names `p4h1` as an unresolved citation

### Requirement: Paths sections with no hops pass
A Paths section that contains no hop links SHALL NOT fail the hop re-check on that account. This covers the case where no path was found and the case where path finding failed and the section says why.

#### Scenario: No paths found
- **WHEN** a walkthrough's Paths section states that no path reaches the module and contains no `p{n}h{k}` links
- **AND** `check --at-head` is run
- **THEN** the hop re-check reports no failure

#### Scenario: Path finding failed
- **WHEN** a walkthrough's Paths section reports that path finding failed and contains no hop links
- **AND** `check --at-head` is run
- **THEN** the hop re-check reports no failure

### Requirement: Legacy scenario walkthroughs still pass
`check --at-head` SHALL continue to re-check hop links in an "End-to-end scenarios" section exactly as it does before this change. Walkthroughs written before this change SHALL keep passing while their cited lines do not move.

#### Scenario: Committed legacy walkthrough unchanged
- **WHEN** a walkthrough committed before this change has an "End-to-end scenarios" section and no Paths section
- **AND** none of its cited lines have moved
- **AND** `check --at-head` is run with the new code
- **THEN** the check passes

#### Scenario: Legacy scenario hop moved
- **WHEN** a line cited by a hop in a legacy "End-to-end scenarios" section changes at HEAD
- **AND** `check --at-head` is run
- **THEN** the check fails for that hop, as it did before this change

#### Scenario: Legacy walkthrough not required to have Paths
- **WHEN** a legacy walkthrough with no Paths section is checked
- **THEN** the check reports no failure for the missing Paths section

### Requirement: Agent-mode walkthroughs are unaffected
`check --at-head` SHALL NOT require a Paths section or hop links in an agent-mode walkthrough, which runs no trace.

#### Scenario: Agent-mode output checked
- **WHEN** a walkthrough produced in agent mode, with no Paths section and no `paths.json`, is checked
- **AND** its cited lines have not moved
- **AND** `check --at-head` is run
- **THEN** the check passes

### Requirement: Hop re-check is covered by the test suite
The walkthrough test suite SHALL include tests for `check --at-head` on path hops. These SHALL cover a passing Paths section, a moved hop line, a deleted hop file, an absent `paths.json`, an empty Paths section and a legacy "End-to-end scenarios" walkthrough. The suite SHALL run in the CI form `uv run --project ... --package ...`.

#### Scenario: Suite runs in CI form
- **WHEN** the walkthrough suite is run with `uv run --project ... --package ...`
- **THEN** the hop re-check tests run and pass

#### Scenario: Moved-hop test detects a regression
- **WHEN** the hop re-check is changed so that it skips links in the Paths section
- **AND** the walkthrough suite is run
- **THEN** the moved-hop-line test fails