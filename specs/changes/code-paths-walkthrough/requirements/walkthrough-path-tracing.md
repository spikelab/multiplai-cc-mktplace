## ADDED Requirements

### Requirement: Trace stage delegates path finding to code_facts
In human mode, the walkthrough trace stage SHALL find paths by calling `code_facts.map_paths` exactly once per run. The stage SHALL NOT run its own seed picking (`pick_seeds`), its own trace gate, or any tracer other than `map_paths`.

#### Scenario: Trace stage calls map_paths
- **WHEN** a human-mode walkthrough runs the trace stage on a module
- **THEN** `code_facts.map_paths` is called exactly once
- **AND** no function from the removed walkthrough seed-picking or trace-gate code is called

#### Scenario: Removed walkthrough tracing code is gone
- **WHEN** the `walkthrough_pipeline` package is inspected after this change
- **THEN** `stages/trace.py` defines no `pick_seeds` function and no walkthrough-specific trace gate

### Requirement: Module scope is passed as changed lines
The trace stage SHALL pass the whole module being explained to `map_paths` as `changed_lines`. `changed_lines` SHALL map every file in the module to the set of all its line numbers, from 1 to the file's last line. The stage SHALL NOT pass `changed_lines=None`.

#### Scenario: Every line of every module file is in scope
- **WHEN** the module contains `a.py` with 40 lines and `b.py` with 12 lines
- **THEN** `changed_lines` maps `a.py` to lines 1–40 and `b.py` to lines 1–12, with no other files

#### Scenario: None is never passed
- **WHEN** the trace stage runs on any module, including a module with one file
- **THEN** the `changed_lines` argument given to `map_paths` is not `None`

#### Scenario: Empty files in the module
- **WHEN** a file in the module has zero lines
- **THEN** the trace stage either leaves that file out of `changed_lines` or maps it to an empty set
- **AND** the stage does not raise an error

#### Scenario: Files outside the module are not scoped
- **WHEN** the repository has files outside the module being explained
- **THEN** none of those files appear as keys in `changed_lines`

### Requirement: Web entry points are passed as caller seeds
The trace stage SHALL pass each web entry point the walkthrough found before this change to `map_paths` as a caller `Seed`. These entry points are webhook routes, routes called from other repositories, Celery beat entries, and other routes. Where a seed has a place that imports into the module, the seed's `Location` SHALL be that place. A seed that reaches the module only over HTTP or a queue SHALL still be passed as a seed and SHALL NOT be discarded by the walkthrough.

#### Scenario: Route that imports into the module
- **WHEN** a webhook route handler in `api/hooks.py` imports from the module at line 7
- **THEN** a caller `Seed` for that route is passed to `map_paths`
- **AND** its `Location` points to `api/hooks.py` line 7

#### Scenario: Seed reaching the module only over HTTP or a queue
- **WHEN** a route from another repository reaches the module only through an HTTP call or a queued task
- **THEN** a caller `Seed` for it is still passed to `map_paths`
- **AND** the entry is either traced or appears in coverage, and is never silently absent

#### Scenario: Web entries traced before the change are still traced
- **WHEN** the walkthrough runs on a web fixture repository whose web entries were traced before this change
- **THEN** each of those entries is either a traced path in the result, or a drop in coverage with reason `over_cap`

### Requirement: Boundary edges are passed as the gate's known places
The trace stage SHALL pass the walkthrough's existing boundary edges to `map_paths` as the trace gate's known places. Boundary edges are cross-repository references, routes, and tasks.

#### Scenario: Known places include boundary edges
- **WHEN** exploration found one cross-repository reference, two routes and one Celery task
- **THEN** the known places given to `map_paths` include all four of those locations

#### Scenario: No boundary edges
- **WHEN** exploration found no cross-repository references, routes or tasks
- **THEN** `map_paths` is called with an empty set of known places
- **AND** the trace stage does not raise an error

### Requirement: Non-web entry points are traced
Through `map_paths`, the trace stage SHALL produce paths for the non-web entry kinds that `code_facts` discovers. These kinds are argparse, click and typer commands, `main`, scripts, skill commands, and hooks.

#### Scenario: CLI fixture repository
- **WHEN** the walkthrough runs on a fixture repository whose only entry point is a click command that reaches the module
- **THEN** the trace result has at least one path whose entry kind is a CLI command

#### Scenario: Skill-command fixture repository
- **WHEN** the walkthrough runs on a fixture repository whose only entry point is a skill command that reaches the module
- **THEN** the trace result has at least one path whose entry kind is a skill command

#### Scenario: Repository with no web entries
- **WHEN** the repository has no webhook routes, cross-repo routes, Celery beat entries or other routes
- **THEN** `map_paths` is still called, with an empty list of caller seeds

### Requirement: --scenarios caps the number of paths
The `--scenarios` option SHALL keep its name. Its value SHALL be passed to `map_paths` as the cap on the number of traced paths. Its default SHALL be 21. Its `--help` text SHALL say that it caps the number of paths.

#### Scenario: Default cap
- **WHEN** the walkthrough runs without `--scenarios`
- **THEN** `map_paths` receives a path cap of 21

#### Scenario: Explicit cap
- **WHEN** the walkthrough runs with `--scenarios 3`
- **THEN** `map_paths` receives a path cap of 3
- **AND** the result has at most 3 traced paths

#### Scenario: Help text
- **WHEN** a user runs the walkthrough CLI with `--help`
- **THEN** the description of `--scenarios` mentions paths, says that it is a cap, and shows a default of 21

#### Scenario: Invalid cap
- **WHEN** the walkthrough runs with `--scenarios 0` or a negative value
- **THEN** the CLI exits with a non-zero status and an error message naming `--scenarios`
- **AND** no stage runs

### Requirement: Entries over the cap are reported
When `code_facts` finds more entries than the cap, every entry that is not traced SHALL appear in the coverage table with reason `over_cap`.

#### Scenario: 25 entries with the default cap
- **WHEN** `map_paths` ranks 25 eligible entries and the cap is 21
- **THEN** 21 entries are traced
- **AND** the coverage table has 4 rows with reason `over_cap`, one for each remaining entry

### Requirement: code_facts ranking order is preserved
The trace stage SHALL keep traced paths and drops in the order `map_paths` returns them. The stage SHALL NOT re-rank, re-sort or filter entries.

#### Scenario: Order matches map_paths output
- **WHEN** `map_paths` returns paths for entries E3, E1 and E2, in that order
- **THEN** the stage's recorded paths are E3, E1 and E2, in that order

#### Scenario: No walkthrough-side filtering
- **WHEN** `map_paths` returns N traced paths and M drops
- **THEN** the trace stage records exactly N paths and M drops

### Requirement: Every dropped entry appears in coverage with stage and reason
Every `PathDrop` returned by `map_paths` SHALL appear as a row in the walkthrough coverage table. The row SHALL show the entry, the drop's stage, and its reason. This covers at least the reasons `seed_not_found`, `unreachable` and `over_cap`.

#### Scenario: seed_not_found
- **WHEN** a caller seed cannot be located and `map_paths` returns a drop with reason `seed_not_found`
- **THEN** the coverage table has a row for that seed with its stage and reason `seed_not_found`

#### Scenario: unreachable
- **WHEN** an entry does not reach the module and `map_paths` returns a drop with reason `unreachable`
- **THEN** the coverage table has a row for that entry with its stage and reason `unreachable`

#### Scenario: Reason unknown to the walkthrough
- **WHEN** `map_paths` returns a drop with a reason the walkthrough has no special handling for
- **THEN** the coverage table still has a row for that entry, showing the reason string unchanged

### Requirement: Trace answers are cached under the run directory
Trace answers from model calls SHALL be stored in a cache under the run directory. `resume` SHALL reuse cached answers and SHALL make model calls only for answers that are missing. Pipeline state SHALL NOT have `trace_answers` or `scenarios` fields.

#### Scenario: Answers persisted
- **WHEN** a run completes the trace stage after model calls for 5 entries
- **THEN** the answers for those 5 entries exist in files under the run directory

#### Scenario: Resume asks only for missing answers
- **WHEN** a run stops after answers for 3 of 5 entries are cached, and the user runs `resume`
- **THEN** model calls are made for only the 2 entries with no cached answer
- **AND** the 3 cached answers are used unchanged

#### Scenario: Fully cached resume
- **WHEN** every needed answer is already cached and the user runs `resume`
- **THEN** the trace stage makes no trace model calls

#### Scenario: State has no trace_answers or scenarios
- **WHEN** a new run's pipeline state is written to disk
- **THEN** the saved state has no `trace_answers` field and no `scenarios` field

#### Scenario: Resuming state written before the change
- **WHEN** `resume` loads a state file that still has `trace_answers` or `scenarios` fields
- **THEN** the state loads without error and those fields are ignored

### Requirement: Model calls are wired like the explore stage
The trace stage SHALL give `map_paths` the same dependencies `stages/explore.py` uses: `repo_resolver`, `sdk.agent_call_structured` for model calls, and `sdk.TRACE_TOOLS` as the agent tool set. Agents SHALL NOT get tools that write files or run code. `trace_limit` SHALL stay at 30.

#### Scenario: Read-only tool set
- **WHEN** the trace stage makes a model call through `map_paths`
- **THEN** the tools given to the agent are `sdk.TRACE_TOOLS`
- **AND** they include no file-writing, file-editing or command-running tool

#### Scenario: Trace limit unchanged
- **WHEN** the trace stage calls `map_paths`
- **THEN** the trace limit it passes is 30

#### Scenario: Spend counted against the breaker
- **WHEN** trace model calls incur cost
- **THEN** that cost is added to the same spend total checked by the `--max-usd` breaker

### Requirement: A trace failure for one entry becomes a drop
When the trace for one entry fails, for example when `sdk.AgentCallError` is raised for that entry, that entry SHALL become a drop that appears in coverage. The other entries SHALL still be traced.

#### Scenario: AgentCallError on one entry
- **WHEN** the model call for entry E2 raises `sdk.AgentCallError`, and the calls for E1 and E3 succeed
- **THEN** E1 and E3 are traced paths
- **AND** E2 appears in the coverage table as a drop with a reason that names the failure
- **AND** the run continues to later stages

### Requirement: Whole-stage failure is recorded and does not stop the run
When the trace stage as a whole fails, for any exception other than `RepoTrustError` or a budget breaker trip, the failure SHALL be appended to `state.errors`. The run SHALL go on to later stages with no traced paths, and the failure SHALL be available to the Paths section to report.

#### Scenario: map_paths raises
- **WHEN** `map_paths` raises an unexpected exception
- **THEN** `state.errors` gets an entry that names the trace stage and the exception message
- **AND** the write stage still runs
- **AND** the trace result records no paths, plus the failure reason

### Requirement: Trust and budget stops still halt the run
`RepoTrustError` and budget breaker trips raised during the trace stage SHALL stop the run as they do today. They SHALL NOT be turned into drops or `state.errors` entries.

#### Scenario: RepoTrustError during trace
- **WHEN** `repo_resolver` raises `RepoTrustError` during the trace stage
- **THEN** the run stops with the same exit behaviour as before this change
- **AND** no later stage runs

#### Scenario: Budget exceeded during trace
- **WHEN** cumulative spend passes `--max-usd` during the trace stage
- **THEN** the run stops through the existing budget breaker
- **AND** cached answers already written stay on disk for `resume`

### Requirement: Agent mode does not trace
In agent mode, the walkthrough SHALL NOT run path tracing. It SHALL NOT call `map_paths` and SHALL make no trace model calls.

#### Scenario: Agent-mode run
- **WHEN** the walkthrough runs in agent mode
- **THEN** `map_paths` is not called
- **AND** no trace answers are written under the run directory