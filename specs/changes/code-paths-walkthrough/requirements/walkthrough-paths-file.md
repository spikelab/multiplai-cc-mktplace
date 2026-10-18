## ADDED Requirements

### Requirement: Paths file is written in human mode
In human mode, the walkthrough pipeline SHALL write a `<name>-paths.json` file in the same directory as `<name>-walkthrough.md`. `<name>` SHALL be the same stem used for the walkthrough Markdown file.

#### Scenario: Human-mode run produces a paths file beside the walkthrough
- **WHEN** a human-mode walkthrough run named `billing` completes and writes `out/billing-walkthrough.md`
- **THEN** a file `out/billing-paths.json` exists
- **AND** no other `*-paths.json` file is created by the run

#### Scenario: Paths file stem follows the walkthrough name
- **WHEN** a human-mode run is invoked with a name that produces `docs/walkthroughs/auth-core-walkthrough.md`
- **THEN** the paths file is written to `docs/walkthroughs/auth-core-paths.json`

### Requirement: Paths file is produced by the shared writer
The pipeline SHALL write the paths file by calling `code_facts.write_paths_file` with the result returned by `code_facts.map_paths` for the run. The pipeline SHALL NOT serialize the paths output itself.

#### Scenario: Shared writer is used
- **WHEN** a human-mode run reaches the render stage with a `map_paths` result
- **THEN** `code_facts.write_paths_file` is called once, with that result and the `<name>-paths.json` target path

#### Scenario: No walkthrough-local serializer exists
- **WHEN** the `walkthrough_pipeline` package source is searched for code that builds `paths.v1` JSON directly, for example by setting a schema version field or dumping `TracedPath` objects
- **THEN** no such code is found outside the `write_paths_file` call

### Requirement: Paths file conforms to the paths.v1 schema
The written paths file SHALL validate against `code_facts`'s `paths.v1.schema.json`.

#### Scenario: Paths file with traced paths validates
- **WHEN** a human-mode run on a fixture repository with CLI and skill-command entries writes `<name>-paths.json`
- **THEN** the file validates against `paths.v1.schema.json` with no errors

#### Scenario: Paths file with zero paths validates
- **WHEN** a human-mode run finds no entry that reaches the module
- **AND** `map_paths` therefore returns no traced paths
- **THEN** `<name>-paths.json` is still written
- **AND** it contains an empty paths list together with any drops
- **AND** it validates against `paths.v1.schema.json`

### Requirement: Paths file content matches the Paths section
The paths file SHALL describe the same `map_paths` result that the Paths section renders. Path `n` in the file SHALL be the path whose hops the Markdown cites as `p{n}h{k}`. Hop `k` of that path in the file SHALL be the hop cited as `p{n}h{k}`.

#### Scenario: Path and hop order agree with citation ids
- **WHEN** a human-mode run renders a Paths section in which hop `p2h3` links to `src/cli.py:42`
- **THEN** the second path in `<name>-paths.json` has, as its third hop, a location at `src/cli.py` line 42

#### Scenario: Path count agrees
- **WHEN** the Paths section lists 5 paths
- **THEN** `<name>-paths.json` contains exactly 5 traced paths, in the same order

#### Scenario: E2E test listing agrees
- **WHEN** the Paths section shows "no e2e test" for path 1
- **AND** it shows one mocked, not-counted test for path 2
- **THEN** in `<name>-paths.json`, path 1 has no counted e2e tests and no mocked e2e tests
- **AND** path 2 has no counted e2e tests and exactly one mocked e2e test, which is the test named in the Markdown

### Requirement: Paths file records dropped entries
The paths file SHALL include every entry that `map_paths` dropped. Each drop SHALL carry the stage and reason that `map_paths` gave, including `seed_not_found`, `unreachable` and `over_cap`.

#### Scenario: Over-cap entries appear as drops
- **WHEN** 25 entries are traceable and `--scenarios` is left at its default of 21
- **THEN** `<name>-paths.json` contains at most 21 traced paths
- **AND** each entry beyond the cap appears as a drop with reason `over_cap`

#### Scenario: Unlocatable web seed appears as a drop
- **WHEN** a web caller seed cannot be located at a place that imports into the module
- **THEN** `<name>-paths.json` contains a drop for that seed with reason `seed_not_found`

#### Scenario: Per-entry trace failure appears as a drop
- **WHEN** the agent trace call for one entry raises `sdk.AgentCallError`
- **AND** the other entries trace successfully
- **THEN** `<name>-paths.json` is written
- **AND** it contains a drop for the failed entry
- **AND** it contains traced paths for the other entries

### Requirement: Paths file is not written in agent mode
In agent mode, the pipeline SHALL NOT write a `<name>-paths.json` file. Agent mode runs no trace.

#### Scenario: Agent-mode run writes no paths file
- **WHEN** an agent-mode walkthrough run completes
- **THEN** no `*-paths.json` file exists in the output directory that the run created or modified
- **AND** `code_facts.write_paths_file` is not called

### Requirement: Whole-stage failure writes no paths file
When the trace stage as a whole fails and no `map_paths` result exists, the pipeline SHALL NOT write a paths file. It SHALL remove any `<name>-paths.json` left by an earlier run at the target path, so the file never describes a different run from the Markdown. The failure SHALL be recorded in `state.errors`, and the Paths section SHALL report it.

#### Scenario: Stage failure leaves no paths file
- **WHEN** `map_paths` raises an error that is not `RepoTrustError` and is not the budget breaker
- **THEN** `<name>-walkthrough.md` is written with a Paths section that states path finding failed
- **AND** `state.errors` contains the failure
- **AND** `<name>-paths.json` does not exist after the run

#### Scenario: Stale paths file is removed on stage failure
- **WHEN** `<name>-paths.json` exists from a previous successful run
- **AND** the trace stage of the current run fails as a whole
- **THEN** `<name>-paths.json` does not exist after the run

#### Scenario: Trust error and budget breaker stop the run before writing
- **WHEN** `RepoTrustError` is raised, or the `--max-usd` breaker trips, during the trace stage
- **THEN** the run stops as it does today
- **AND** no new `<name>-paths.json` is written by that run

### Requirement: Paths file write failure is reported without losing the walkthrough
If writing the paths file fails, the pipeline SHALL record the error in `state.errors`. It SHALL still write `<name>-walkthrough.md`.

#### Scenario: Unwritable paths file target
- **WHEN** `write_paths_file` raises `OSError` for the target path
- **THEN** `<name>-walkthrough.md` is still written
- **AND** `state.errors` contains an entry naming the paths file and the error

### Requirement: Paths file is rewritten on re-run and resume
Each human-mode run that reaches rendering SHALL overwrite `<name>-paths.json` with the current run's result. A resumed run SHALL write the file from the result rebuilt from the cached answers.

#### Scenario: Re-run overwrites previous file
- **WHEN** a human-mode run writes `<name>-paths.json`
- **AND** the module then changes so that a second run finds a different set of paths
- **THEN** after the second run, `<name>-paths.json` reflects only the second run's paths and drops

#### Scenario: Resume produces the same file without re-asking answered calls
- **WHEN** a run is interrupted after all trace answers are cached under the run directory
- **AND** the run is continued with `resume`
- **THEN** no trace agent calls are made for entries whose answers are cached
- **AND** `<name>-paths.json` is written
- **AND** it is equal to the file that an uninterrupted run with the same answers would produce

### Requirement: Paths file is not consumed by check or buildme
`check`, including `check --at-head`, and buildme's stale-walkthrough detection SHALL NOT read `<name>-paths.json`. Their results SHALL NOT depend on whether the file exists or what it contains.

#### Scenario: Check ignores a missing paths file
- **WHEN** `<name>-paths.json` is deleted after a human-mode run
- **AND** `check --at-head` is run on `<name>-walkthrough.md`
- **THEN** the check result is identical to the result with the file present

#### Scenario: Check ignores a corrupted paths file
- **WHEN** `<name>-paths.json` is replaced with invalid JSON
- **AND** `check --at-head` is run on `<name>-walkthrough.md`
- **THEN** the check does not fail because of the paths file

#### Scenario: buildme ignores paths files
- **WHEN** buildme's stale-walkthrough detection scans a directory containing `x-walkthrough.md` and `x-paths.json`
- **THEN** only `x-walkthrough.md` is considered

### Requirement: Walkthrough JSON shape is unchanged by the paths file
Adding the paths file SHALL NOT change the shape of `<name>-walkthrough.json` (v1). The only change to that file is the step title "Paths" replacing "End-to-end scenarios".

#### Scenario: Walkthrough JSON keeps its v1 shape
- **WHEN** a human-mode run writes both `<name>-walkthrough.json` and `<name>-paths.json`
- **THEN** `<name>-walkthrough.json` validates against the existing walkthrough v1 schema
- **AND** it has no field that refers to the paths file

### Requirement: Paths file is documented
`skills/codebase-walkthrough/SKILL.md` SHALL document `<name>-paths.json`. The documentation SHALL state:

- the file's location relative to `<name>-walkthrough.md`;
- that it follows the `paths.v1` schema;
- that it is written only in human mode;
- that `check` and buildme do not read it.

#### Scenario: SKILL.md describes the paths file
- **WHEN** `SKILL.md` is read after the change
- **THEN** it names `<name>-paths.json`
- **AND** it states the file is written beside `<name>-walkthrough.md`
- **AND** it references the `paths.v1` schema
- **AND** it states the file is written in human mode only
- **AND** it states that `check` and buildme do not read the file