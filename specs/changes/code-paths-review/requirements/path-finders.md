## ADDED Requirements

### Requirement: One path finder per mapped path
The find stage SHALL launch exactly one path finder for each path in the run's `paths.json`, in the order the paths appear there. No other limit applies beyond the `cap=21` the paths stage already enforces.

#### Scenario: Change review with mapped paths
- **WHEN** a change review's paths stage writes `paths.json` with 5 paths
- **THEN** the find stage launches exactly 5 path finders, one per path id, in the order the paths appear in `paths.json`

#### Scenario: Mapping at the cap
- **WHEN** `paths.json` holds 21 paths
- **THEN** the find stage launches 21 path finders and no more

#### Scenario: Duplicate path ids are not launched twice
- **WHEN** two entries in `paths.json` share the same path id
- **THEN** only one path finder runs for that id
- **AND** the duplicate is recorded in coverage

### Requirement: No path finders without paths
The find stage SHALL launch no path finders when the run has no usable paths. Find SHALL still run its piece and dimension finders as usual in that case.

#### Scenario: Empty mapping
- **WHEN** the paths stage records a failed or empty mapping in `state.errors`
- **THEN** the find stage launches zero path finders
- **AND** the piece and dimension finders still run
- **AND** the review does not raise an error

#### Scenario: Tree review without `--paths`
- **WHEN** a `--tree` or `--dir` review runs without `--paths`
- **THEN** no paths stage runs and no path finders are launched

#### Scenario: Tree review with `--paths`
- **WHEN** a `--tree` review runs with `--paths` and the paths stage maps 3 paths
- **THEN** the find stage launches 3 path finders

### Requirement: Path finder keys are stable
Each path finder SHALL be keyed by its path id from `paths.json`. The key SHALL be distinct from every piece key and every dimension key, so its state, cost entries, report and `checks.json` entries can be found across rounds and resumes.

#### Scenario: Key derived from path id
- **WHEN** a path finder runs for path id `p07`
- **THEN** its finder key contains `p07`
- **AND** the key differs from every piece and dimension finder key in the same run

#### Scenario: Same key across rounds
- **WHEN** path `p07` is present in two find rounds of the same run
- **THEN** both rounds use the same finder key for it

### Requirement: Path finder prompt carries the full path
Each path finder's prompt SHALL include the following data for its path from `paths.json`:
- the entry point
- who invokes it
- every hop in order, with file and line
- the changed lines that lie on the path
- the e2e tests listed for the path

#### Scenario: Prompt contents
- **WHEN** a path finder is built for a path with an entry and 4 hops
- **THEN** the prompt names the entry's file and line, its invoker, and all 4 hops in trace order with their files and lines

#### Scenario: Path with no e2e test
- **WHEN** the path has no listed e2e test
- **THEN** the prompt states that no e2e test is listed for the path

### Requirement: Path data is fenced as untrusted
All path data placed into a path finder prompt SHALL be fenced and defanged through `multiplai_core.untrusted`. This covers:
- entry and invoker names
- hop descriptions
- file paths
- test names

#### Scenario: Instruction-like text in path data
- **WHEN** a hop description in `paths.json` contains text such as "ignore previous instructions"
- **THEN** that text appears in the prompt only inside an untrusted-content fence, in its defanged form

#### Scenario: Fence delimiter in path data
- **WHEN** a path field contains the fence's own delimiter sequence
- **THEN** the defanged prompt cannot close the fence early, as checked by `multiplai_core.untrusted`'s fence parser

### Requirement: Path finder follows the path end to end
The path finder prompt SHALL tell the agent to do three things:
- follow the path from its entry through every hop to its end
- report defects that lie on the path, or that arise from how the path uses changed code
- not report unrelated defects elsewhere in the snapshot

#### Scenario: Scope instruction present
- **WHEN** a path finder prompt is built
- **THEN** it instructs the agent to trace from entry to the last hop
- **AND** it limits reports to defects on the path or in the path's use of changed code

#### Scenario: Cross-file defect along a path
- **WHEN** a path finder runs on a fixture path spanning 3 files, with a seeded defect that appears only when the values passed between the second and third hop are combined
- **THEN** the converted findings include a finding that cites lines in at least two of those files

### Requirement: Path finders read the whole snapshot with read-only tools
Path finders SHALL be allowed to read any file in the review snapshot. They SHALL be limited to the Read, Grep and Glob tools, and SHALL NOT be able to run code or shell commands.

#### Scenario: Allowed tools
- **WHEN** a path finder agent is configured
- **THEN** its allowed tool list is exactly Read, Grep and Glob

#### Scenario: No partial-read restriction
- **WHEN** a path finder prompt is built
- **THEN** it does not contain the instruction "Never read a whole file"

### Requirement: Path finders report all severities
Path finders SHALL be allowed to report items of any severity, including LOW, and advice.

#### Scenario: Severity instruction
- **WHEN** a path finder prompt is built
- **THEN** it does not restrict reports to HIGH and MEDIUM

#### Scenario: Low-severity item kept
- **WHEN** a path finder's report contains one item that states no severity
- **THEN** after conversion that item becomes a `Finding` with LOW severity attributed to the path finder

### Requirement: Path finders use report-then-convert
Each path finder SHALL produce a plain-text report. That report SHALL be converted into findings by a separate convert call, as defined by `report-then-convert`. The report SHALL be stored in run state when it returns, and written as `report-path-<path id>.md` in the target directory.

#### Scenario: Report file written
- **WHEN** the path finder for `p07` returns its report
- **THEN** `report-path-p07.md` exists in the target directory with the report's text
- **AND** the run state holds the report under the `p07` finder key before the convert call starts

#### Scenario: Resume after the report is stored
- **WHEN** a run stops after `p07`'s report is stored but before its convert call finishes, and the review is resumed
- **THEN** the resume makes no new path finder call for `p07`
- **AND** it makes only the convert call

### Requirement: Path finders run every find round
Path finders SHALL run in every find round of a run, not only the first, using the paths in the run's `paths.json`.

#### Scenario: Second round
- **WHEN** a review runs two find rounds and `paths.json` holds 4 paths
- **THEN** each round launches 4 path finders

### Requirement: Path finder spend is bounded by the ledger
Path finder and convert calls SHALL count toward `--max-cost-usd`. When the ledger stops the run, the following SHALL hold:
- no further path finders are launched
- every report and finding already returned is kept in state
- the budget stop propagates

#### Scenario: Budget stop mid-path finders
- **WHEN** the ledger stops the run after 3 of 8 path finders have returned reports
- **THEN** the 3 reports remain in state and on disk
- **AND** no further path finder is launched
- **AND** the run ends with the budget stop

#### Scenario: Resume with a higher budget
- **WHEN** that run is resumed with a higher `--max-cost-usd`
- **THEN** only the 5 path finders without stored reports are launched
- **AND** convert calls run only for reports not yet converted

### Requirement: A failing path finder does not stop the review
If a single path finder or its convert call fails, the failure SHALL be recorded in coverage and `state.errors`, and the review SHALL continue with the remaining finders. A `RepoTrustError` or a budget stop SHALL propagate instead.

#### Scenario: One path finder errors
- **WHEN** the path finder for `p03` raises a model error and the others succeed
- **THEN** `state.errors` and coverage record the `p03` failure
- **AND** findings from the other path finders are merged

#### Scenario: Trust error propagates
- **WHEN** a path finder raises `RepoTrustError`
- **THEN** the review stops with `RepoTrustError` and does not record it as an ordinary finder failure

### Requirement: Dimension and piece finders are unaffected by path finders
Adding path finders SHALL NOT change the following:
- the set, prompts or keys of the dimension finders `diff-bugs`, `callers`, `history`, `conventions`, `tests` and `trim`
- the piece finders

#### Scenario: Same dimension finders with and without paths
- **WHEN** the same change is reviewed once with an empty mapping and once with 3 mapped paths
- **THEN** both runs launch the same dimension finders with the same keys and prompts
- **AND** they differ only by the 3 path finders