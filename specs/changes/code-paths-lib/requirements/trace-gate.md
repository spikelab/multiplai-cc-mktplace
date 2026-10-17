## ADDED Requirements

### Requirement: Gate is deterministic and makes no model call
The trace gate SHALL decide whether each hop is accepted or broken using only the hops, the commit snapshot, the entry, and the known edges. It SHALL NOT call the model. Re-asks are issued by the tracing step, not by the checking function. The same inputs SHALL always produce the same result.

#### Scenario: Repeated evaluation is identical
- **WHEN** the gate checks the same hop list against the same snapshot, entry and known edges twice
- **THEN** both results are equal, including the accepted hops, `stops_at` and `break_reason`

#### Scenario: No model call during checking
- **WHEN** the gate checks a hop list and the supplied `CallFn` is a stub that fails the test if called
- **THEN** the check completes and the stub is never invoked

### Requirement: Gate does not import the walkthrough pipeline
The gate SHALL implement its rules against `code_facts`' own `Resolve` and `Citation`. It SHALL NOT import any module from `walkthrough_pipeline`.

#### Scenario: No walkthrough import
- **WHEN** the import statements of every module under `plugins/multiplai-dev/lib/code_facts/` are parsed
- **THEN** none of them imports `walkthrough_pipeline` or any submodule of it

### Requirement: Break reasons come from a fixed set
Every broken hop SHALL be reported with a `break_reason` code from this fixed set:

- `malformed_answer`
- `empty_trace`
- `path_outside_root`
- `missing_file`
- `line_out_of_range`
- `text_mismatch`
- `base_side_citation`
- `entry_mismatch`
- `unconnected_hop`
- `repeated_hop`
- `hop_limit`

A human-readable detail string MAY accompany the code.

#### Scenario: Reason is always a known code
- **WHEN** the gate checks any fixture trace that breaks
- **THEN** `break_reason` is one of the codes in the fixed set

### Requirement: Malformed or empty answers break at hop 1
The gate SHALL report a break at hop 1 in two cases: when an agent answer cannot be parsed into a list of hops, and when it parses to zero hops.

#### Scenario: Unparseable answer
- **WHEN** the agent answer does not validate against the hop model
- **THEN** the gate reports `stops_at` = 1 and `break_reason` = `malformed_answer`

#### Scenario: Empty hop list
- **WHEN** the agent answer parses to an empty hop list
- **THEN** the gate reports `stops_at` = 1 and `break_reason` = `empty_trace`

### Requirement: Hop paths stay inside the snapshot root
Each hop's citation path SHALL be relative to the snapshot root and SHALL resolve inside it. This includes snapshots that hold several repositories.

#### Scenario: Absolute path rejected
- **WHEN** a hop cites `/etc/passwd`
- **THEN** that hop is broken with `break_reason` = `path_outside_root`

#### Scenario: Parent traversal rejected
- **WHEN** a hop cites `../outside/file.py`, or a path that normalises to a location outside the snapshot root
- **THEN** that hop is broken with `break_reason` = `path_outside_root`

#### Scenario: Nested repository path accepted
- **WHEN** a snapshot holds `repo_a/` and `repo_b/`, and a hop cites `repo_b/pkg/mod.py` with a correct line and text
- **THEN** the citation part of the check passes for that hop

### Requirement: Every hop citation re-reads at the commit
Each hop's citation SHALL be re-read through `Resolve` at the commit under trace. The citation check fails if any of these is true:

- the file does not exist;
- the cited line range falls outside the file;
- the quoted text does not match the file content at the cited lines.

Text is compared after the same whitespace normalisation that `code_facts` citation checking already uses.

#### Scenario: Missing file
- **WHEN** a hop cites a file that does not exist at the commit
- **THEN** that hop is broken with `break_reason` = `missing_file`

#### Scenario: Line out of range
- **WHEN** a hop cites line 500 of a 40-line file
- **THEN** that hop is broken with `break_reason` = `line_out_of_range`

#### Scenario: Quoted text differs
- **WHEN** a hop cites an existing line, but its quoted text differs from that line at the commit
- **THEN** that hop is broken with `break_reason` = `text_mismatch`

#### Scenario: Citation matches
- **WHEN** a hop's path, line range and quoted text all match the commit
- **THEN** the citation part of the check passes for that hop

### Requirement: Citations are head-side only
A hop citation with no side specified SHALL be treated as head side. A hop citation that explicitly names the base side SHALL be broken.

#### Scenario: Default side is head
- **WHEN** a hop citation omits the side
- **THEN** the gate re-reads it against the head commit

#### Scenario: Base-side citation rejected
- **WHEN** a hop citation names the base side
- **THEN** that hop is broken with `break_reason` = `base_side_citation`

### Requirement: Hop 1 is anchored at the entry
The first hop SHALL be at the entry's definition location. This means it cites the same file as the entry, with a line range that includes the entry's definition line. For entries that discovery resolved to a handler, hop 1 MAY instead be at the handler's definition line.

#### Scenario: Hop 1 at entry definition
- **WHEN** the entry is defined at `pkg/cli.py:12` and hop 1 cites `pkg/cli.py` lines 12–14
- **THEN** hop 1 passes the anchoring check

#### Scenario: Hop 1 elsewhere
- **WHEN** the entry is defined at `pkg/cli.py:12` and hop 1 cites `pkg/other.py:3`
- **THEN** hop 1 is broken with `break_reason` = `entry_mismatch`

#### Scenario: Hop 1 at resolved handler
- **WHEN** discovery resolved the entry's dispatch line `pkg/cli.py:30` to the handler `pkg/cmds.py:8`, and hop 1 cites `pkg/cmds.py:8`
- **THEN** hop 1 passes the anchoring check

### Requirement: Consecutive hops are connected by a reference or a known edge
For each hop n+1 with n ≥ 1, the gate SHALL accept the hop as connected only if at least one of these holds:

- **(a) Name reference:** the symbol name of hop n+1 appears as an identifier in the cited text of hop n.
- **(b) Known edge:** there is a known edge from hop n's location to hop n+1's location.
- **(c) Known place:** hop n+1's location matches a caller-supplied place.

Otherwise the hop is broken.

#### Scenario: Connected by name reference
- **WHEN** hop 2 cites `def load_config(...)` and hop 1's cited text contains `load_config(path)`
- **THEN** hop 2 passes the connectivity check

#### Scenario: Unconnected hop
- **WHEN** hop 3's symbol name does not appear in hop 2's cited text, and neither a known edge nor a known place covers hop 3
- **THEN** hop 3 is broken with `break_reason` = `unconnected_hop`

#### Scenario: Name in a comment only
- **WHEN** hop 2's symbol name appears in hop 1's cited lines only inside a comment or string literal
- **THEN** hop 2 is broken with `break_reason` = `unconnected_hop`, unless a known edge or a known place applies

### Requirement: Caller-supplied places are accepted as connections
The gate SHALL accept a set of places from the caller. Each place is a snapshot-relative path plus a line. A hop SHALL be accepted as connected from its predecessor when its cited line range includes a place. The hop's citation SHALL still re-read.

#### Scenario: Place covers a framework boundary
- **WHEN** the caller supplies the place `app/routes.py:40`, and hop 4 cites `app/routes.py` lines 40–42 with no name reference in hop 3
- **THEN** hop 4 passes the connectivity check

#### Scenario: Place does not excuse a bad citation
- **WHEN** hop 4 matches a supplied place, but its quoted text does not match the commit
- **THEN** hop 4 is broken with `break_reason` = `text_mismatch`

### Requirement: Discovery edges are accepted as connections
The gate SHALL accept, as known edges, the edges that entry-point discovery produced:

- argparse, click or typer dispatch line → handler definition;
- `[project.scripts]` target → target function definition;
- `hooks.json`, `.claude/settings*.json` or `SKILL.md` command → the invoked module's `__main__` definition or entry function.

#### Scenario: Script target edge
- **WHEN** discovery recorded the edge `pyproject.toml:[project.scripts] tool = "pkg.cli:main"` → `pkg/cli.py:5`, and the next hop cites `pkg/cli.py:5`
- **THEN** that hop passes the connectivity check

#### Scenario: Hook command edge
- **WHEN** discovery recorded the edge from a `hooks.json` command `python -m pkg.hook` to `pkg/hook/__main__.py:1`, and the next hop cites that location
- **THEN** that hop passes the connectivity check

#### Scenario: `args.command` dispatch
- **WHEN** a fixture mirrors `pr_pipeline` dispatching on `args.command`, and discovery landed an entry on its `add_parser` line
- **THEN** hop 1 at that `add_parser` line passes anchoring
- **AND** the next hop is accepted only through a name reference, a known edge or a known place

### Requirement: Repeated hops break the path
A hop whose path and line range match an earlier hop in the same trace SHALL be broken.

#### Scenario: Cycle back to an earlier hop
- **WHEN** hop 5 cites the same path and line range as hop 2
- **THEN** hop 5 is broken with `break_reason` = `repeated_hop`

### Requirement: Traces are capped at 20 hops
The gate SHALL accept at most 20 hops. Hops beyond the 20th SHALL be discarded and SHALL NOT trigger a re-ask.

#### Scenario: Over-long trace
- **WHEN** an answer contains 25 hops and the first 20 are valid
- **THEN** the result has 20 hops, `stops_at` = 20 and `break_reason` = `hop_limit`
- **AND** no re-ask is issued

#### Scenario: Exactly 20 hops
- **WHEN** an answer contains exactly 20 valid hops
- **THEN** the result has 20 hops, and both `stops_at` and `break_reason` are null

### Requirement: The first broken hop decides the outcome
The gate SHALL check hops in order and SHALL stop at the first broken hop. When a hop fails several rules, the reported `break_reason` SHALL be the first failing rule in this order:

1. path_outside_root
2. missing_file
3. line_out_of_range
4. text_mismatch
5. base_side_citation
6. entry_mismatch
7. unconnected_hop
8. repeated_hop

#### Scenario: Later hops ignored after a break
- **WHEN** hop 3 is broken and hop 6 is also broken
- **THEN** `stops_at` = 3 and `break_reason` reflects hop 3 only

#### Scenario: Multiple failures on one hop
- **WHEN** a hop cites an out-of-range line and also has no connection to its predecessor
- **THEN** `break_reason` = `line_out_of_range`

### Requirement: A broken trace gets exactly one re-ask
When the first gate check reports a break, the tracing step SHALL issue one re-ask through the caller's `CallFn`. The exception is `hop_limit`, which gets no re-ask. The re-ask SHALL state the broken hop index, the `break_reason` and its detail, and the accepted prefix. The re-asked answer SHALL be checked by the gate from hop 1. No second re-ask SHALL be issued.

#### Scenario: Re-ask repairs the trace
- **WHEN** the first answer breaks at hop 4, and the re-asked answer passes every check
- **THEN** exactly two `CallFn` calls are made for that entry
- **AND** the result uses the re-asked hops, with null `stops_at` and `break_reason`

#### Scenario: Re-ask prompt carries the reason
- **WHEN** a re-ask is issued for a break at hop 4 with `text_mismatch`
- **THEN** the prompt passed to `CallFn` contains the hop index 4 and the string `text_mismatch`

#### Scenario: No second re-ask
- **WHEN** both the first answer and the re-asked answer break
- **THEN** exactly two `CallFn` calls are made for that entry

#### Scenario: Valid first answer needs no re-ask
- **WHEN** the first answer passes every check
- **THEN** exactly one `CallFn` call is made for that entry

### Requirement: An unrepaired trace is cut at the break
If the re-asked answer still breaks, the path SHALL keep only the re-asked answer's hops before its first broken hop. The result SHALL record:

- `stops_at`: the 1-based index of the first broken hop;
- `break_reason`: that hop's code.

#### Scenario: Cut after re-ask
- **WHEN** the re-asked answer breaks at hop 3 with `unconnected_hop`
- **THEN** the path holds hops 1–2 of the re-asked answer, `stops_at` = 2 and `break_reason` = `unconnected_hop`

#### Scenario: Cut at hop 1
- **WHEN** the re-asked answer breaks at hop 1
- **THEN** no path is returned for the entry
- **AND** the entry is recorded as dropped with stage `trace` and the hop-1 break code as its reason

#### Scenario: Valid path fields
- **WHEN** a path passes every check
- **THEN** both `stops_at` and `break_reason` are null

### Requirement: Re-asks use the resumable answer cache
Re-asks SHALL go through the same `ask`/`run_tasks` cache as first asks. The re-ask key SHALL be derived from the entry, the commit, the first answer and the break, and SHALL NOT depend on the order in which entries are processed. Re-running with a warm cache SHALL make no new calls.

#### Scenario: Resume makes no calls
- **WHEN** a run that included re-asks completes, and the same run is repeated with the same cache directory
- **THEN** `CallFn` is not invoked and the gated results are identical

#### Scenario: Order independence
- **WHEN** the same entries are traced in two different orders with fresh caches and deterministic stub answers
- **THEN** the gated results for each entry are identical

### Requirement: CallFn failures during a re-ask propagate
The gate SHALL NOT retry, swallow or budget a failing re-ask call. Any exception raised by the caller's `CallFn` during a re-ask SHALL propagate unchanged.

#### Scenario: Re-ask call raises
- **WHEN** the `CallFn` raises `RuntimeError("budget")` on a re-ask
- **THEN** that same exception reaches the caller of `map_paths`
- **AND** no further call is made for that entry