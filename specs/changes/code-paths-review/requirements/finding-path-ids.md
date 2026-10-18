## ADDED Requirements

### Requirement: Path id assignment runs as a post-merge step
The review SHALL assign path ids to findings in a step that runs after merge completes and before `findings.json` is written. The step SHALL use the paths written to `paths.json` in the same run. It SHALL NOT change any field of a finding other than adding `paths`. It SHALL NOT add, remove, reorder, re-verify or re-gate findings.

#### Scenario: Assignment follows merge
- **WHEN** a change review has a successful paths stage and merge produces 12 findings
- **THEN** `findings.json` contains exactly those 12 findings in merge order
- **AND** every field other than `paths` is identical to the merged output

#### Scenario: Assignment does not alter verification or citation results
- **WHEN** a finding was kept by verification and passed citation gates before assignment
- **THEN** its severity, citations, quotes and verification status are unchanged after assignment

### Requirement: A finding lies on a path when a citation contains a hop or entry line
A finding SHALL lie on a path when at least one of its citations meets both conditions:
- It names the same repository-relative file as the path's entry or as one of the path's hops.
- Its line range, inclusive of start and end, contains that entry's or hop's line.

File names SHALL be compared after normalizing to repository-relative POSIX form, with no leading `./` and no trailing separators.

#### Scenario: Citation range contains a hop line
- **WHEN** path `p03` has a hop at `src/cli.py` line 40
- **AND** a finding cites `src/cli.py` lines 35–45
- **THEN** the finding's `paths` contains `p03`

#### Scenario: Citation range contains the entry line
- **WHEN** path `p01` has its entry at `bin/run.py` line 10
- **AND** a finding cites `bin/run.py` lines 10–10
- **THEN** the finding's `paths` contains `p01`

#### Scenario: Range boundaries are inclusive
- **WHEN** a hop is at `a.py` line 20
- **AND** one finding cites `a.py` lines 20–25 and another cites `a.py` lines 15–20
- **THEN** both findings lie on that path

#### Scenario: Same file but line outside range
- **WHEN** a hop is at `a.py` line 50
- **AND** a finding's only citation is `a.py` lines 10–49
- **THEN** the finding does not lie on that path

#### Scenario: Matching line in a different file
- **WHEN** a hop is at `a.py` line 30
- **AND** a finding's only citation is `b.py` lines 1–100
- **THEN** the finding does not lie on that path

#### Scenario: Path spelling differences are normalized
- **WHEN** a hop names `./pkg/mod.py` line 8
- **AND** a finding cites `pkg/mod.py` lines 5–9
- **THEN** the finding lies on that path

### Requirement: Citations under `.review-deps/` never place a finding on a path
A citation whose repository-relative file path begins with `.review-deps/` SHALL be ignored for path assignment, even when its file and line range match a hop or entry.

#### Scenario: Matching citation in `.review-deps/` is ignored
- **WHEN** a path has a hop at `.review-deps/lib/x.py` line 12
- **AND** a finding's only citation is `.review-deps/lib/x.py` lines 10–15
- **THEN** the finding has no `paths` entry for that path

#### Scenario: Another citation still counts
- **WHEN** a finding cites `.review-deps/lib/x.py` lines 10–15 and `src/y.py` lines 3–6
- **AND** a path has a hop at `src/y.py` line 4
- **THEN** the finding's `paths` contains that path's id

### Requirement: The `paths` list is deduplicated and ordered by `paths.json`
A finding's `paths` list SHALL hold each path id at most once. The ids SHALL be listed in the order their paths appear in `paths.json`. A path SHALL be listed once even when several citations, or several hops of that path, match.

#### Scenario: Multiple matching citations on one path
- **WHEN** two citations of one finding each contain a hop line of path `p02`
- **THEN** the finding's `paths` is exactly `["p02"]`

#### Scenario: Finding on several paths
- **WHEN** `paths.json` lists paths in the order `p01`, `p02`, `p05`
- **AND** a finding matches `p05` and `p01`
- **THEN** the finding's `paths` is exactly `["p01", "p05"]`

### Requirement: `paths` is omitted when a finding lies on no path
A finding that lies on no path SHALL be written without a `paths` key. The key SHALL NOT appear as an empty list.

#### Scenario: Finding off every path
- **WHEN** the paths stage produced paths
- **AND** no citation of a finding matches any entry or hop
- **THEN** that finding in `findings.json` has no `paths` key

### Requirement: Assignment is skipped without path data
When the paths stage did not run, or its mapping failed or produced no paths, the review SHALL skip assignment. No finding SHALL carry a `paths` key, and the review SHALL complete normally.

#### Scenario: Tree review without `--paths`
- **WHEN** a `--tree` review runs without `--paths`
- **THEN** no finding in `findings.json` has a `paths` key
- **AND** the review exits with the same status it would have without this capability

#### Scenario: Failed mapping
- **WHEN** the paths stage records a failed or empty mapping in coverage and `state.errors`
- **THEN** no finding has a `paths` key
- **AND** no additional error is recorded for path assignment

### Requirement: Malformed path entries are ignored for assignment
An entry or hop without a file or a positive integer line SHALL be skipped during assignment. Other entries and hops of the same path SHALL still be matched. Skipping SHALL NOT raise an error or stop the review.

#### Scenario: Hop without a line
- **WHEN** path `p04` has one hop with no line and another hop at `c.py` line 7
- **AND** a finding cites `c.py` lines 1–10
- **THEN** the finding's `paths` contains `p04`
- **AND** the review completes without error

### Requirement: Path ids refer only to paths in the same run's `paths.json`
Every id in a finding's `paths` SHALL equal the id of a path in the `paths.json` written by the same run. Entries dropped or traces recorded as broken by the paths stage SHALL NOT be used for assignment.

#### Scenario: Ids resolve in `paths.json`
- **WHEN** a review writes `findings.json` and `paths.json`
- **THEN** every string in every finding's `paths` matches an `id` in `paths.json`

#### Scenario: Dropped entry is not assigned
- **WHEN** the paths stage dropped an entry point because of `cap=21`
- **AND** a finding cites that entry's file and line
- **THEN** the finding's `paths` does not refer to the dropped entry

### Requirement: Assignment is deterministic across resume
Given the same merged findings and the same `paths.json`, assignment SHALL produce identical `paths` values. A `resume` that reruns merge SHALL recompute `paths` and SHALL NOT keep stale ids from an earlier write.

#### Scenario: Resume yields identical ids
- **WHEN** a review is stopped after the paths stage and resumed to completion
- **THEN** each finding's `paths` equals the result of a single uninterrupted run with the same merged findings and `paths.json`

#### Scenario: Recomputed after merge changes
- **WHEN** a resumed run's merge drops a finding that previously carried `paths`
- **THEN** the new `findings.json` has no entry for it
- **AND** no other finding gains or loses ids except through its own citations

### Requirement: Findings schema allows an optional `paths` array
The findings schema the review writes SHALL define an optional per-finding `paths` property. The property SHALL be an array of unique, non-empty strings. Findings without `paths` SHALL remain valid. No `kind` property SHALL be added to the finding schema.

#### Scenario: Finding with `paths` validates
- **WHEN** a finding has `"paths": ["p01", "p03"]`
- **THEN** it validates against the findings schema

#### Scenario: Existing output still validates
- **WHEN** a `findings.json` written before this change, with no `paths` keys, is validated against the updated schema
- **THEN** validation succeeds

#### Scenario: Invalid `paths` value is rejected
- **WHEN** a finding has `"paths": "p01"` or `"paths": ["p01", "p01"]` or `"paths": [""]`
- **THEN** schema validation fails

#### Scenario: No `kind` on findings
- **WHEN** the updated findings schema is inspected
- **THEN** it defines no `kind` property on a finding