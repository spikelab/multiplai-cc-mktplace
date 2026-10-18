## ADDED Requirements

### Requirement: Paths stage runs before find on change reviews
The review pipeline SHALL include a paths stage. On every change review (a diff between a base and a head), the paths stage SHALL run, and it SHALL complete before the `find` stage starts. No flag is needed to enable it on a change review.

#### Scenario: Change review runs paths before find
- **WHEN** a change review runs with default options
- **THEN** the paths stage executes
- **AND** the recorded stage order shows paths completing before `find` starts

#### Scenario: Paths stage is not disabled by --extra-finders
- **WHEN** a change review runs with `--extra-finders`
- **THEN** the paths stage still executes before `find`

### Requirement: Paths stage is opt-in for tree reviews
For `--tree` and `--dir` reviews, the paths stage SHALL run only when `--paths` is given. Without `--paths`, the stage SHALL be skipped. A skipped stage SHALL NOT call `map_paths`, SHALL NOT spend money, SHALL NOT write `paths.json`, and SHALL NOT record an error.

#### Scenario: Tree review without --paths skips the stage
- **WHEN** a `--tree` review runs without `--paths`
- **THEN** `map_paths` is not called
- **AND** no `paths.json` exists in the output directory
- **AND** `state.errors` contains no entry from the paths stage

#### Scenario: Dir review with --paths runs the stage
- **WHEN** a `--dir` review runs with `--paths`
- **THEN** the paths stage executes before `find`
- **AND** `paths.json` is written beside `findings.json`

### Requirement: Change review maps paths from changed lines at head
On a change review, the paths stage SHALL call `code_facts.map_paths` once, with the changed lines of the diff expressed as head-side file paths and head-side line numbers, and with `cap=21`. Files whose diff only removes lines, including deleted files, SHALL contribute no changed lines. A renamed file SHALL be passed under its head path.

#### Scenario: Changed lines and cap passed to map_paths
- **WHEN** a change review's diff adds lines 10–12 of `a.py` at head and modifies line 40 of `b.py` at head
- **THEN** `map_paths` is called with changed lines covering `a.py` lines 10–12 and `b.py` line 40, using head line numbers
- **AND** `map_paths` is called with `cap=21`

#### Scenario: Deleted file contributes no changed lines
- **WHEN** the diff deletes `old.py` entirely
- **THEN** the changed lines passed to `map_paths` contain no entry for `old.py`

#### Scenario: Renamed file uses head path
- **WHEN** the diff renames `x/old.py` to `x/new.py` and changes line 5
- **THEN** the changed lines passed to `map_paths` reference `x/new.py` line 5 and do not reference `x/old.py`

### Requirement: Tree review maps paths from every reviewed line
On a `--tree` or `--dir` review with `--paths`, the paths stage SHALL call `map_paths` with `cap=21` and with every line of every file under review passed as a changed line.

#### Scenario: All lines of reviewed files passed as changed
- **WHEN** a `--dir` review with `--paths` covers `pkg/a.py` (30 lines) and `pkg/b.py` (12 lines)
- **THEN** `map_paths` is called with changed lines covering `pkg/a.py` lines 1–30 and `pkg/b.py` lines 1–12
- **AND** `map_paths` is called with `cap=21`

### Requirement: Paths stage writes paths.json beside findings.json
When `map_paths` returns a result, the paths stage SHALL write it as `paths.json` in the same directory as `findings.json`, using `code_facts.write_paths_file`, so that the file conforms to `code_facts`' `paths.v1.schema.json`. A result with zero paths SHALL still be written. `paths.json` SHALL be a top-level file of the run's target directory so that kept rounds retain it.

#### Scenario: paths.json written and schema-valid
- **WHEN** the paths stage completes with a `PathsResult` containing 4 paths
- **THEN** `paths.json` exists in the directory containing `findings.json`
- **AND** it validates against `paths.v1.schema.json`
- **AND** it lists the same 4 path ids as the `PathsResult`

#### Scenario: Empty result still written
- **WHEN** `map_paths` returns a `PathsResult` with no paths
- **THEN** `paths.json` is written and validates against `paths.v1.schema.json` with an empty path list

#### Scenario: paths.json survives a kept round
- **WHEN** a review round that wrote `paths.json` is kept and a new round starts
- **THEN** the kept round's directory contains a copy of `paths.json`

### Requirement: Path mapping answers are cached in run state
The paths stage SHALL store `map_paths`' answer cache in the run's state folder. Every model answer obtained during mapping SHALL be persisted as soon as it returns. On `resume`, the paths stage SHALL reuse stored answers and SHALL request only answers that are missing. Once the stage has completed and written `paths.json`, `resume` SHALL NOT call any model for the paths stage.

#### Scenario: Resume after partial mapping asks only for missing answers
- **WHEN** a run stops during the paths stage after 5 of 9 mapping answers were stored, and the run is resumed
- **THEN** the resumed paths stage makes model calls only for the 4 missing answers
- **AND** the resulting `paths.json` reflects all 9 answers

#### Scenario: Resume after completed stage makes no mapping calls
- **WHEN** a run completed the paths stage and is resumed in a later stage
- **THEN** the paths stage makes no model calls
- **AND** `paths.json` is unchanged

#### Scenario: Cache lives in run state folder
- **WHEN** the paths stage stores a mapping answer
- **THEN** the answer is stored under the run's state folder and not in a location shared across runs

### Requirement: Paths stage spend counts toward the budget
All model spend by the paths stage SHALL be recorded in the run's cost ledger and SHALL count toward `--max-cost-usd`. When the ledger reaches the budget during the paths stage, the budget stop SHALL propagate and end the run, and every answer paid for before the stop SHALL remain stored for `resume`.

#### Scenario: Paths spend recorded in ledger
- **WHEN** the paths stage makes model calls costing 3.20 USD
- **THEN** the run's ledger total increases by 3.20 USD attributed to the paths stage

#### Scenario: Budget stop during paths propagates
- **WHEN** `--max-cost-usd` is reached during the paths stage
- **THEN** the run stops with a budget stop and does not run `find`
- **AND** the budget stop is not converted into a paths-stage coverage entry or `state.errors` entry

#### Scenario: Resume with higher budget completes without repaying
- **WHEN** a run stopped on budget during the paths stage is resumed with a higher `--max-cost-usd`
- **THEN** answers stored before the stop are not requested again
- **AND** the paths stage completes and `find` runs

### Requirement: Partial mapping problems are recorded and the review continues
When the `PathsResult` reports dropped entries or broken traces, the paths stage SHALL record each one in coverage and in `state.errors`, SHALL still write `paths.json`, and the review SHALL continue with the paths that were mapped.

#### Scenario: Dropped entry recorded
- **WHEN** `map_paths` drops one entry point
- **THEN** coverage contains an entry identifying the dropped entry and the reason
- **AND** `state.errors` contains an entry from the paths stage for it
- **AND** the review proceeds to `find`

#### Scenario: Broken trace recorded
- **WHEN** `map_paths` returns a path whose trace is broken
- **THEN** coverage and `state.errors` each record the broken trace with its path id
- **AND** `paths.json` is written

### Requirement: Failed or empty mapping does not stop the review
If `map_paths` raises any error other than `RepoTrustError` or a budget stop, the paths stage SHALL record the failure in coverage and `state.errors`, SHALL NOT write `paths.json`, and the review SHALL continue without paths. If mapping returns zero paths, the stage SHALL record the empty mapping in coverage and `state.errors` and the review SHALL continue with no path finders.

#### Scenario: Mapping exception recorded and review continues
- **WHEN** `map_paths` raises a `RuntimeError`
- **THEN** coverage and `state.errors` each record a paths-stage failure including the error message
- **AND** no `paths.json` is written
- **AND** `find` runs with no path finders
- **AND** the run's exit status is not a failure caused by the paths stage

#### Scenario: Empty mapping recorded
- **WHEN** `map_paths` returns zero paths on a change review
- **THEN** coverage and `state.errors` each record that mapping produced no paths
- **AND** `find` runs with no path finders

### Requirement: RepoTrustError propagates from the paths stage
A `RepoTrustError` raised during the paths stage SHALL propagate and end the run. It SHALL NOT be recorded as a recoverable paths-stage error, and the review SHALL NOT continue to `find`.

#### Scenario: Repo trust failure stops the run
- **WHEN** `map_paths` raises `RepoTrustError`
- **THEN** the run ends with that error
- **AND** `find` does not run
- **AND** no `paths.json` is written

### Requirement: Downstream stages read paths from the stage output
Later stages that use paths (path finders, path ids on findings, and the report's Paths section) SHALL take path data only from the paths stage's stored result for the run. When the stage was skipped or failed, these consumers SHALL behave as if there are zero paths.

#### Scenario: Skipped stage yields no path consumers
- **WHEN** a `--tree` review runs without `--paths`
- **THEN** no path finders run
- **AND** no finding in `findings.json` has a `paths` list with entries

#### Scenario: Resumed run uses stored paths
- **WHEN** a run is resumed after the paths stage completed
- **THEN** path finders are created for the path ids in the stored result, without calling `map_paths` again