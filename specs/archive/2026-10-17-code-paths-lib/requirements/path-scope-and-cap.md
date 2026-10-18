## ADDED Requirements

### Requirement: Scoping is active only when changed lines are supplied
`map_paths(...)` SHALL apply the reachability pre-filter, trace limit, coverage filter and cap only when the caller passes a changed-lines argument that is not `None`. The changed-lines argument maps file paths, relative to the snapshot root, to sets of 1-based head-side line numbers. When the argument is `None`, every discovered entry SHALL be traced, every traced path SHALL be kept, and no scope-related drop reasons SHALL be recorded. Deterministic ordering and id assignment SHALL apply in both modes.

#### Scenario: No changed lines means no scope filtering
- **WHEN** `map_paths` runs on a fixture repository with 5 discovered entries, with changed lines set to `None` and `cap=2`
- **THEN** the result contains 5 paths, numbered `p1` to `p5`
- **AND** no drop record has reason `unreachable`, `over_trace_limit`, `no_changed_line_covered` or `over_cap`

#### Scenario: Changed lines given activates scoping
- **WHEN** `map_paths` runs on the same fixture with changed lines `{"pkg/core.py": {10}}`
- **THEN** only entries that pass the reachability pre-filter are traced
- **AND** only paths that cover `pkg/core.py:10` are kept

### Requirement: Changed-line paths are validated and normalised
Changed-line file paths SHALL be interpreted relative to the snapshot root, and SHALL be normalised to POSIX separators with no leading `./`. A path that is absolute or that resolves outside the snapshot root SHALL raise `ValueError` before any `CallFn` call. A line number below 1 SHALL also raise `ValueError` before any `CallFn` call. A path that does not exist in the snapshot SHALL NOT raise. Such a path contributes no reachability and cannot be covered.

#### Scenario: Absolute path rejected
- **WHEN** changed lines contain `{"/etc/passwd": {1}}`
- **THEN** `map_paths` raises `ValueError`
- **AND** the recording `CallFn` has received zero calls

#### Scenario: Path escaping the snapshot rejected
- **WHEN** changed lines contain `{"../other/x.py": {3}}`
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

#### Scenario: Non-positive line rejected
- **WHEN** changed lines contain `{"pkg/core.py": {0}}`
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

#### Scenario: Equivalent spellings normalise to one file
- **WHEN** changed lines contain `{"./pkg/core.py": {10}}`
- **THEN** the result behaves identically to changed lines `{"pkg/core.py": {10}}`, including path ids and coverage counts

#### Scenario: Missing file tolerated
- **WHEN** changed lines contain only `{"pkg/deleted.py": {4}}` and that file does not exist at head
- **THEN** `map_paths` returns without raising
- **AND** every entry is dropped with reason `unreachable`

#### Scenario: Multi-repository snapshot paths
- **WHEN** the snapshot root contains `repo_a/` and `repo_b/`, and changed lines are `{"repo_b/src/mod.py": {7}}`
- **THEN** reachability and coverage are evaluated against `repo_b/src/mod.py`
- **AND** every file path in the output is relative to the snapshot root, for example `repo_b/src/mod.py`, never to `repo_b/`

### Requirement: Static import-graph reachability pre-filter
When changed lines are supplied, the library SHALL build a static import graph of the snapshot's Python modules using `ast`, with no code execution and no model call.

An entry SHALL be considered reachable when either of these holds:
- (a) the entry's definition file is a changed file; or
- (b) the module containing the entry's resolved handler transitively imports a changed Python file.

Each unreachable entry SHALL be dropped before tracing with reason `unreachable`, and no `CallFn` call SHALL be made for it. This applies to entries from deterministic discovery, from the model fallback, and from caller seeds.

#### Scenario: Transitively importing entry kept
- **WHEN** entry `cli.main` is in `pkg/cli.py`, `pkg/cli.py` imports `pkg/service.py`, `pkg/service.py` imports `pkg/core.py`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** `cli.main` is traced

#### Scenario: Non-importing entry dropped without a model call
- **WHEN** entry `tools.lint` is in `pkg/tools.py`, which imports nothing that reaches `pkg/core.py`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** `tools.lint` appears in the drop records with reason `unreachable`
- **AND** the recording `CallFn` received no trace prompt for `tools.lint`

#### Scenario: Entry whose definition file changed is kept
- **WHEN** a `SKILL.md` command entry is defined in `skills/x/SKILL.md`, and changed lines are `{"skills/x/SKILL.md": {12}}`
- **THEN** that entry is traced, even though `SKILL.md` is not a Python module

#### Scenario: Relative and package imports resolved
- **WHEN** `pkg/a.py` contains `from . import b`, `pkg/b.py` contains `from .sub import c`, and the changed file is `pkg/sub/c.py`
- **THEN** an entry defined in `pkg/a.py` is considered reachable

#### Scenario: Import cycle terminates
- **WHEN** `pkg/x.py` imports `pkg/y.py` and `pkg/y.py` imports `pkg/x.py`, and neither reaches the changed file
- **THEN** the reachability check terminates
- **AND** entries in both files are dropped with reason `unreachable`

#### Scenario: Unparseable or unresolvable imports do not fail the run
- **WHEN** one module in the snapshot has a syntax error, and another imports a third-party module that is not in the snapshot
- **THEN** `map_paths` completes without raising
- **AND** the module with the syntax error is treated as having no outgoing imports
- **AND** the unresolved third-party import contributes no edge

#### Scenario: Empty changed-lines mapping
- **WHEN** changed lines are `{}`
- **THEN** zero paths are returned
- **AND** every entry is dropped with reason `unreachable`
- **AND** the recording `CallFn` received zero trace calls

### Requirement: Trace limit bounds the number of entries traced
When changed lines are supplied, at most `trace_limit` reachable entries SHALL be traced. Reachable entries SHALL first be put in a deterministic pre-trace order:
1. Shortest import-graph distance to any changed file, ascending. Distance is 0 when the definition file itself changed.
2. Entry kind rank, using the same fixed kind ranking as the output ordering.
3. Definition path, ascending.
4. Definition line, ascending.

Entries beyond the first `trace_limit` SHALL be dropped with reason `over_trace_limit`, and no `CallFn` call SHALL be made for them. A `trace_limit` below 1 SHALL raise `ValueError` before any `CallFn` call.

#### Scenario: Excess entries dropped deterministically
- **WHEN** 6 entries are reachable and `trace_limit=4`
- **THEN** exactly 4 entries are traced
- **AND** the 2 entries last in pre-trace order are dropped with reason `over_trace_limit`
- **AND** repeated runs drop the same 2 entries

#### Scenario: Closer entries traced first
- **WHEN** entry A's definition file is the changed file, entry B reaches it through 3 imports, and `trace_limit=1`
- **THEN** A is traced
- **AND** B is dropped with reason `over_trace_limit`

#### Scenario: Invalid trace limit rejected
- **WHEN** `trace_limit=0` is passed with changed lines supplied
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

### Requirement: Post-trace coverage filter
When changed lines are supplied, a traced path SHALL be kept only if at least one of its retained hops covers a changed line.

A hop covers a changed line when both of these hold:
- the hop's cited file equals the changed file after normalisation; and
- the line falls within the hop's cited line range, inclusive.

Only hops up to and including the path's `stops_at` hop SHALL count. Hops removed by the trace gate SHALL NOT count. A traced path that covers no changed line SHALL be dropped with reason `no_changed_line_covered`.

#### Scenario: Covering path kept
- **WHEN** a traced path has a hop citing `pkg/core.py:8-12`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** the path is kept

#### Scenario: Reachable but non-covering path dropped
- **WHEN** an entry passes the import pre-filter, but its traced hops cite only `pkg/core.py:40-45`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** the path is dropped with reason `no_changed_line_covered`

#### Scenario: Range boundaries inclusive
- **WHEN** a hop cites `pkg/core.py:10-10`, and changed lines are `{"pkg/core.py": {10}}`
- **THEN** the path is kept

#### Scenario: Hops after a gate cut do not count
- **WHEN** the trace gate cuts a path at hop 3 (`stops_at=3`), and only the agent's original hop 5 cited the changed line
- **THEN** the path is dropped with reason `no_changed_line_covered`

### Requirement: Deterministic path ordering
Kept paths SHALL be ordered by these keys, in this order:
1. Number of distinct changed lines covered, descending. A line covered by several hops counts once.
2. Retained hop count, ascending.
3. Entry kind rank, ascending, using a fixed ranking defined in the library and covered by a test.
4. Entry definition path, ascending by string comparison.
5. Entry definition line, ascending.

The ordering SHALL NOT depend on trace completion order, concurrency, or cache state.

#### Scenario: More coverage ranks first
- **WHEN** path X covers 3 changed lines and path Y covers 1
- **THEN** X is ordered before Y

#### Scenario: Duplicate coverage counted once
- **WHEN** path X has two hops both citing changed line `pkg/core.py:10`, and no other changed lines; and path Y covers `pkg/core.py:10` and `pkg/core.py:11`
- **THEN** X's covered count is 1
- **AND** Y is ordered before X

#### Scenario: Tie broken by hop count
- **WHEN** paths X and Y each cover 2 changed lines, X has 4 hops and Y has 6
- **THEN** X is ordered before Y

#### Scenario: Tie broken by kind then location
- **WHEN** paths X and Y tie on coverage and hop count, and have the same kind
- **AND** X is defined at `a/cli.py:5` and Y at `a/cli.py:30`
- **THEN** X is ordered before Y

#### Scenario: Independent of completion order
- **WHEN** the same inputs are run twice with a `CallFn` that returns trace answers in different completion orders
- **THEN** both results contain identical path orderings and identical ids

### Requirement: Cap limits kept paths
When changed lines are supplied, at most `cap` paths SHALL be kept, taken from the front of the deterministic ordering. Each path beyond the cap SHALL be dropped with reason `over_cap`. A `cap` below 1 SHALL raise `ValueError` before any `CallFn` call.

#### Scenario: Paths beyond cap dropped
- **WHEN** 7 paths survive the coverage filter and `cap=5`
- **THEN** 5 paths are returned
- **AND** the 2 paths last in the ordering appear in the drop records with reason `over_cap`

#### Scenario: Fewer paths than cap
- **WHEN** 3 paths survive the coverage filter and `cap=5`
- **THEN** 3 paths are returned
- **AND** no drop has reason `over_cap`

#### Scenario: Invalid cap rejected
- **WHEN** `cap=0` or `cap=-1` is passed with changed lines supplied
- **THEN** `map_paths` raises `ValueError` before any `CallFn` call

### Requirement: Path ids assigned after ordering and cap
Path ids SHALL be assigned as `p1`, `p2`, … `pN` in final output order. Assignment SHALL happen after ordering and capping. The ids SHALL be contiguous, with no gaps, and dropped paths SHALL NOT consume ids.

#### Scenario: Contiguous ids
- **WHEN** 7 paths are ordered and `cap=5` drops 2
- **THEN** the returned ids are exactly `p1`, `p2`, `p3`, `p4`, `p5`, in output order

#### Scenario: Stable ids on rerun
- **WHEN** `map_paths` is run twice on the same commit, with the same changed lines, cap, trace limit and cached answers
- **THEN** each id refers to the same entry and the same hop sequence in both runs

### Requirement: Every scope drop records a reason
Each entry or path removed by scoping SHALL produce exactly one drop record. The record SHALL identify the entry by kind, definition path and definition line. It SHALL carry exactly one reason from the closed set `unreachable`, `over_trace_limit`, `no_changed_line_covered`, `over_cap`.

The drop records SHALL appear in the result and in `paths.json`, and SHALL be sorted by definition path, then line, then kind. No entry SHALL appear both as a kept path and as a drop record.

#### Scenario: Accounting is complete
- **WHEN** `map_paths` runs with changed lines supplied, and E entries are discovered after deduplication
- **THEN** the count of kept paths, plus scope drop records, plus drops recorded by other stages (such as fallback citation failures), equals E

#### Scenario: Single reason per entry
- **WHEN** an entry is dropped with reason `unreachable`
- **THEN** it has no other drop record
- **AND** it is not counted against `trace_limit`

#### Scenario: Drop records validate against the schema
- **WHEN** `paths.json` is written for a run that includes all four drop reasons
- **THEN** the file validates against `schema/paths.v1.schema.json`
- **AND** each drop reason is one of the four allowed values