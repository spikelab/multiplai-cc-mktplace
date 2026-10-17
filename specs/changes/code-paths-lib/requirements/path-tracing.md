## ADDED Requirements

### Requirement: One trace agent per traced entry
The library SHALL issue exactly one trace request per distinct entry that remains after discovery de-duplication and the scope pre-filter. Entries that resolve to the same handler SHALL share one trace request. Entries dropped before tracing SHALL NOT produce a trace request.

#### Scenario: One request per surviving entry
- **WHEN** `map_paths` runs on a fixture with 3 distinct entries, all reaching a changed file
- **THEN** the caller's `CallFn` receives exactly 3 trace requests, one per entry

#### Scenario: De-duplicated entries share a trace
- **WHEN** a `[project.scripts]` target and an argparse subcommand resolve to the same handler function
- **THEN** exactly one trace request is issued for that handler
- **AND** the resulting path lists the other invoker in `also_invoked_by`

#### Scenario: Pre-filtered entries are not traced
- **WHEN** changed lines are given and an entry cannot reach any changed file through the static import graph
- **THEN** no trace request is issued for that entry
- **AND** the entry appears among the drops with its reason

### Requirement: Trace agents are read-only
Each trace request SHALL restrict the agent's tools to `Read`, `Grep` and `Glob`. A request SHALL NOT allow any tool that writes files, runs commands or uses the network.

#### Scenario: Allowed tools on every trace request
- **WHEN** a fake `CallFn` records every trace request
- **THEN** the allowed tool set on each request is exactly `{Read, Grep, Glob}`

#### Scenario: Re-ask requests are read-only too
- **WHEN** the trace gate triggers a re-ask for a broken hop
- **THEN** the re-ask request's allowed tool set is also exactly `{Read, Grep, Glob}`

### Requirement: Model calls go only through the caller's CallFn
Every trace request and every re-ask SHALL go through the `CallFn` passed by the caller. The path-tracing code SHALL NOT import or call any model SDK directly. It SHALL NOT retry a failed call or apply a budget; both are left to the `CallFn`.

#### Scenario: No direct SDK use
- **WHEN** the path-tracing modules are imported in a test with no model SDK installed
- **THEN** the import succeeds and `map_paths` runs end to end with a fake `CallFn`

#### Scenario: No library-level retry
- **WHEN** the fake `CallFn` raises an exception for one entry's trace request
- **THEN** the `CallFn` was called exactly once for that entry
- **AND** that entry is recorded as dropped with a reason indicating the trace call failed

### Requirement: Trace prompt carries the entry, the scope and known edges
Each trace request SHALL include:
- the entry's kind, its definition location (file relative to the snapshot root, and line), and its handler symbol;
- the commit being traced;
- the changed files and line ranges, when changed lines are given;
- the known edges that apply to this entry: caller-supplied `places` and the edges found by discovery (handler dispatch line, script target, hook or `SKILL.md` command target);
- the hop limit of 20.

#### Scenario: Prompt contents
- **WHEN** a fake `CallFn` captures the prompt for an argparse subcommand entry, with changed lines in `pkg/core.py`
- **THEN** the prompt contains the entry's file and line, its handler symbol, the commit id, `pkg/core.py` with its changed line ranges, the dispatch edge found by discovery, and the number 20

#### Scenario: No changed lines given
- **WHEN** `map_paths` runs with no changed lines
- **THEN** each trace prompt states that there is no change scope and asks for the chain to the entry's last effect

### Requirement: An answer is one linear chain of hops
A trace answer SHALL be one ordered list of hops from the entry to its last effect. Each hop SHALL have the same fields as the walkthrough `Hop` (`walkthrough_pipeline/models.py:122-128`), defined on the `code_facts` `_Model` base and not imported from the walkthrough. Each hop SHALL carry a `Citation` on the head side. The answer model SHALL NOT allow branching: it has no nested hops and no alternative chains. One entry SHALL produce at most one path.

#### Scenario: Valid linear answer
- **WHEN** the fake `CallFn` returns 5 hops, each with a head-side citation
- **THEN** the traced path, before gating, has 5 hops in the returned order

#### Scenario: Branching answer is rejected
- **WHEN** the fake `CallFn` returns an answer with two alternative hop lists for one entry
- **THEN** the answer fails validation
- **AND** the entry is recorded as dropped with a reason indicating a malformed trace answer
- **AND** no path is produced for that entry

#### Scenario: Entry with several dispatch targets
- **WHEN** an entry's handler calls two different functions and the fake model returns one chain through the first
- **THEN** exactly one path is produced for that entry

### Requirement: Hops cite head-side code only
Every hop citation SHALL refer to the head side of the commit. A hop that cites the base side, or that cites code that does not exist at head, SHALL NOT be accepted as a valid hop.

#### Scenario: Base-side citation
- **WHEN** the fake model returns a chain whose hop 3 cites the base side
- **THEN** hop 3 is treated as broken and passed to the trace gate as a break at hop 3

#### Scenario: Citation outside the snapshot
- **WHEN** a hop cites a file path that does not exist under the snapshot root at head
- **THEN** that hop is treated as broken

### Requirement: Hop limit of 20
A path SHALL contain at most 20 hops. If an answer returns more than 20, the library SHALL keep the first 20, discard the rest, and record `stops_at` as hop 20 with `break_reason` `"hop_limit"`. An answer with exactly 20 hops SHALL NOT be marked as cut.

#### Scenario: Exactly 20 hops
- **WHEN** the fake model returns 20 valid hops
- **THEN** the path has 20 hops and no `break_reason`

#### Scenario: More than 20 hops
- **WHEN** the fake model returns 23 valid hops
- **THEN** the path has the first 20 hops in order
- **AND** `stops_at` refers to hop 20
- **AND** `break_reason` is `"hop_limit"`

### Requirement: Empty or malformed answers are dropped, not fatal
An answer with zero hops, or one that fails to parse or validate, SHALL cause only that entry to be dropped with a recorded reason. Other entries' traces SHALL be unaffected, and `map_paths` SHALL still return.

#### Scenario: Zero hops
- **WHEN** the fake model returns an empty hop list for one of 3 entries
- **THEN** that entry is recorded as dropped with a reason indicating an empty trace
- **AND** the other 2 entries are traced normally

#### Scenario: Unparseable answer
- **WHEN** the fake model returns text that is not valid against the trace answer model
- **THEN** that entry is recorded as dropped with a reason indicating a malformed trace answer
- **AND** `map_paths` returns without raising

### Requirement: Every answer passes through the trace gate
Every validated trace answer SHALL be handed to the trace gate before it becomes a path. Path tracing SHALL NOT itself accept or repair broken hops. Re-asks and cuts are decided by the trace gate.

#### Scenario: Gate is applied
- **WHEN** a trace answer has a hop whose citation does not re-read at the commit
- **THEN** the resulting path reflects the trace gate's outcome: either a re-asked chain, or a chain cut with `stops_at` and `break_reason` set

### Requirement: Bounded concurrency
Trace requests SHALL run through the existing `bounded(...)` helper. A caller-supplied concurrency limit SHALL cap the number of requests in flight at once. When the caller supplies no limit, the library's existing default SHALL apply.

#### Scenario: Limit is respected
- **WHEN** `map_paths` runs with a concurrency limit of 2 over 6 entries, using a fake `CallFn` that records how many calls are in flight
- **THEN** the maximum observed number of calls in flight is 2
- **AND** all 6 entries are traced

#### Scenario: Limit of 1 runs serially
- **WHEN** the concurrency limit is 1
- **THEN** no two trace requests overlap in time

### Requirement: Answer cache keyed independently of order
Trace answers SHALL be cached using the `ask`/`run_tasks` cache pattern in `code_facts/explore.py`. The cache key SHALL be derived from the commit, the snapshot-relative entry identity (kind, file, line, handler), the sorted change scope, the sorted known edges for that entry, and the trace prompt version. It SHALL NOT depend on the order in which entries, changed lines or places are supplied, or on the order in which calls finish. Re-ask answers SHALL also be cached, under a key that includes the broken hop and the gate's reason.

#### Scenario: Reordered inputs hit the cache
- **WHEN** `map_paths` is run once, then run again with the same inputs except that the seed entries, changed lines and places are in reverse order
- **THEN** the second run makes zero `CallFn` calls
- **AND** its output is byte-identical to the first run's output

#### Scenario: Changed scope misses the cache
- **WHEN** a second run adds one changed line to a file
- **THEN** every entry whose trace prompt includes that file's scope is re-requested

#### Scenario: Prompt version change misses the cache
- **WHEN** the trace prompt version is bumped between two runs
- **THEN** every entry is re-requested

### Requirement: Resumable runs
An interrupted run SHALL leave the cached answers for completed traces in place. A rerun with the same inputs SHALL reuse them and request only the traces that did not complete.

#### Scenario: Resume after interruption
- **WHEN** a run over 5 entries is interrupted after 3 trace answers are cached, and the run is repeated with the same inputs
- **THEN** the rerun makes exactly 2 trace calls
- **AND** the final output equals that of an uninterrupted run with the same fake answers

### Requirement: Snapshot-relative locations
All hop and entry locations SHALL be relative to the snapshot root, including snapshots that contain several repositories. Absolute paths and paths that escape the snapshot root SHALL NOT appear in a trace prompt or a path.

#### Scenario: Multi-repository snapshot
- **WHEN** the snapshot root contains `repo-a/` and `repo-b/`, and an entry in `repo-b/cli.py` is traced
- **THEN** every hop location in the resulting path starts with `repo-b/` or another directory under the snapshot root
- **AND** no location is absolute

#### Scenario: Path escaping the root
- **WHEN** the fake model returns a hop citing `../outside.py`
- **THEN** that hop is treated as broken

### Requirement: Deterministic trace output
Given the same inputs and the same cached or faked answers, path tracing SHALL produce identical hop lists, regardless of concurrency limit or completion order.

#### Scenario: Concurrency does not change output
- **WHEN** `map_paths` runs twice with the same fake answers, once with concurrency 1 and once with concurrency 8, and the fake `CallFn` adds random delays
- **THEN** the two `paths.json` outputs are byte-identical