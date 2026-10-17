## ADDED Requirements

### Requirement: Single fallback agent per run
After deterministic discovery finishes, `map_paths` SHALL make at most one model-fallback agent call per run. The call SHALL go through the caller-supplied `CallFn`. The agent SHALL list further entry points that reach the scope. The scope is the changed files when changed lines are given, and otherwise the caller's scope paths.

#### Scenario: One fallback call per run
- **WHEN** `map_paths` runs against a fixture repository using a recording `CallFn` stub
- **THEN** the stub records exactly one call whose prompt is the entry-point fallback prompt

#### Scenario: Fallback runs after deterministic discovery
- **WHEN** `map_paths` runs with a recording `CallFn` stub on a fixture repository that has two argparse subcommands
- **THEN** the fallback prompt sent to the stub lists both deterministically discovered entries
- **AND** the prompt asks only for entry points not already listed

#### Scenario: Scope is included in the prompt
- **WHEN** `map_paths` is called with changed lines in `pkg/core.py`
- **THEN** the fallback prompt names `pkg/core.py` as the scope the entry points must reach

### Requirement: Fallback agent is read-only
The fallback agent SHALL be granted only the `Read`, `Grep` and `Glob` tools. It SHALL NOT be granted any tool that writes files, runs shell commands or runs code.

#### Scenario: Tool allow-list
- **WHEN** a recording `CallFn` stub captures the fallback call
- **THEN** the call's allowed tools are exactly `Read`, `Grep` and `Glob`

### Requirement: Fallback response shape
The fallback agent's answer SHALL be parsed into a pydantic model derived from the `_Model` base in `code_facts/models.py`. Each listed entry SHALL include:

- an entry `kind` from the same set used by deterministic discovery, or `other`;
- a display name;
- a `Citation` pointing at the entry's definition line, on the head side by default;
- a short reason explaining how the entry reaches the scope.

#### Scenario: Well-formed answer is parsed
- **WHEN** the stub returns one entry with kind `route`, name `GET /items`, a citation `app/routes.py:12`, and a reason
- **THEN** the parsed result holds one entry with those fields, and its citation side is `head`

#### Scenario: Unknown kind is normalised
- **WHEN** the stub returns an entry whose kind is not in the known set
- **THEN** the entry is kept with kind `other`, provided its citation verifies

#### Scenario: Entry missing a citation is dropped
- **WHEN** the stub returns an entry with no citation
- **THEN** the entry is not traced
- **AND** the output's dropped-entries list records it with reason `missing_citation`

### Requirement: Malformed fallback answer does not fail the run
If the fallback answer cannot be parsed into the response model, the run SHALL continue using deterministic entries only. The output SHALL record that the fallback failed and why. This failure is about the answer's content. Errors raised by `CallFn` itself are covered by the next requirement.

#### Scenario: Unparseable answer
- **WHEN** the stub returns text that is not valid against the fallback response model
- **THEN** `map_paths` completes and returns paths only from deterministic entries
- **AND** the output records a fallback status of `failed`, with a non-empty reason

#### Scenario: Empty answer
- **WHEN** the stub returns a valid answer listing zero entries
- **THEN** `map_paths` completes, adds no model entries, and records a fallback status of `ok`

### Requirement: CallFn errors are not retried
The fallback step SHALL NOT retry or budget model calls itself. An exception raised by the caller's `CallFn` during the fallback call SHALL propagate to the caller of `map_paths`.

#### Scenario: CallFn raises
- **WHEN** the stub raises `RuntimeError` on the fallback call
- **THEN** `map_paths` raises that `RuntimeError`
- **AND** the stub records exactly one fallback call attempt

### Requirement: Citation must re-read at the commit
Each fallback entry's citation SHALL be checked with the existing `code_facts` citation re-reading at the run's commit. An entry whose citation does not re-read SHALL be dropped before tracing, with reason `citation_unverified`. Reasons this can happen include:

- the file does not exist;
- the line is out of range;
- the quoted text does not match.

#### Scenario: Valid citation is kept
- **WHEN** the stub returns an entry citing `app/routes.py:12` with text that matches line 12 at the commit
- **THEN** the entry is passed on for scoping and tracing, and is marked `source: "model"`

#### Scenario: Nonexistent file
- **WHEN** the stub returns an entry citing `app/missing.py:3`
- **THEN** the entry is dropped with reason `citation_unverified`, and no trace call is made for it

#### Scenario: Line out of range
- **WHEN** the stub returns an entry citing line 9999 of a 40-line file
- **THEN** the entry is dropped with reason `citation_unverified`

#### Scenario: Quoted text mismatch
- **WHEN** the stub returns an entry citing a real line, but the quoted text differs from that line at the commit
- **THEN** the entry is dropped with reason `citation_unverified`

#### Scenario: Citation outside the snapshot root
- **WHEN** the stub returns an entry citing `../outside.py:1` or an absolute path
- **THEN** the entry is dropped with reason `citation_unverified`

### Requirement: Paths are relative to the snapshot root
Fallback citations SHALL be interpreted and recorded relative to the snapshot root, including snapshots that hold several repositories.

#### Scenario: Multi-repository snapshot
- **WHEN** the snapshot root contains `repo_a/` and `repo_b/`, and the stub cites `repo_b/cli.py:5` with matching text
- **THEN** the kept entry's location is recorded as `repo_b/cli.py:5`

### Requirement: Fallback entries do not duplicate deterministic entries
A fallback entry SHALL be dropped with reason `duplicate` in either of these cases:

- its cited definition line is the same as that of an entry already found by deterministic discovery or by caller seeds;
- it resolves to the same handler as such an entry.

Deterministic and seeded entries always win over model entries. Two fallback entries that cite the same line SHALL be collapsed into one.

#### Scenario: Model repeats a deterministic entry
- **WHEN** deterministic discovery found an argparse subcommand at `cli.py:20`, and the stub also returns an entry citing `cli.py:20`
- **THEN** only the deterministic entry is traced
- **AND** the model entry is recorded as dropped with reason `duplicate`

#### Scenario: Model lists the same entry twice
- **WHEN** the stub returns two entries that cite the same verified line
- **THEN** exactly one model entry for that line is passed on for tracing

### Requirement: Fallback entries go through the same scope filter
Verified fallback entries SHALL be subject to the same reachability filter, trace limit and post-trace coverage filter as deterministic entries. Any drop of a fallback entry at those stages SHALL record the same reasons used for deterministic entries.

#### Scenario: Model entry cannot reach changed file
- **WHEN** changed lines are given in `pkg/core.py`, and a verified fallback entry's module cannot reach `pkg/core.py` through the static import graph
- **THEN** the entry is dropped before tracing, with the same unreachable reason that deterministic entries use

### Requirement: Fallback is deterministic and resumable
The fallback answer SHALL be cached using the existing `ask` answer-cache pattern. The cache key SHALL be built from the prompt inputs and SHALL NOT depend on the order in which deterministic entries were discovered. Kept fallback entries SHALL be ordered by location, independently of the order in which the model listed them.

#### Scenario: Resumed run reuses cached answer
- **WHEN** `map_paths` runs twice with the same inputs and the same cache directory
- **THEN** the second run makes zero fallback calls to the `CallFn` stub, and produces identical output

#### Scenario: Model order does not affect output
- **WHEN** two stubs return the same verified entries in different orders
- **THEN** the resulting `paths.json` files are byte-identical

### Requirement: Fallback can be disabled by the caller
`map_paths` SHALL accept a parameter that skips the model fallback. When the fallback is skipped, no fallback call SHALL be made, and the output SHALL record a fallback status of `skipped`. *(Assumption to confirm.)*

#### Scenario: Fallback disabled
- **WHEN** `map_paths` is called with the fallback disabled
- **THEN** the `CallFn` stub records no fallback call
- **AND** the output records a fallback status of `skipped`