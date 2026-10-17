## ADDED Requirements

### Requirement: Piece finders run by default on every change review
A change review SHALL run one piece finder for each piece produced by piece splitting. No flag or config key is needed to enable this. Piece finders SHALL run in addition to the existing dimension finders (`diff-bugs`, `callers`, `history`, `conventions`, `tests`, `trim`). Those dimension finders SHALL keep their current behaviour.

#### Scenario: Default change review spawns one finder per piece
- **WHEN** a change review runs with no finder-related flags and piece splitting yields 7 pieces
- **THEN** the find stage launches exactly 7 piece finders, one per piece
- **AND** each of the 6 dimension finders is launched as it was before this change

#### Scenario: No cap on piece finder count
- **WHEN** piece splitting yields 15 pieces
- **THEN** the find stage launches 15 piece finders
- **AND** it does not truncate, merge or drop any piece to meet a fixed limit such as the former cap of 6

#### Scenario: Diff with no reviewable files
- **WHEN** a change review's diff yields zero pieces, for example because it only changes lockfiles
- **THEN** no piece finder is launched
- **AND** the find stage still runs the dimension finders and completes without error

### Requirement: Piece finders do not run on tree reviews
Piece finders SHALL only run on change reviews, meaning reviews of a diff. Reviews run with `--tree` or `--dir` SHALL NOT launch piece finders.

#### Scenario: Tree review
- **WHEN** a review runs with `--tree`, with or without `--paths`
- **THEN** no find entry keyed as a piece finder appears in the run state or in `checks.json`

### Requirement: Piece finders are keyed separately from dimension finders
Each piece finder SHALL have a stable key that includes the piece's index, for example `piece@<i>`. That key SHALL appear beside the dimension finder keys in the run state and in the find entries of `checks.json`. A piece key SHALL never collide with a dimension key or a path finder key.

#### Scenario: Keys recorded in checks.json
- **WHEN** a change review with 3 pieces finishes the find stage
- **THEN** `checks.json` contains find entries keyed `piece@0`, `piece@1` and `piece@2`, alongside the dimension finder keys
- **AND** no two find entries share a key

#### Scenario: Stable keys across resume
- **WHEN** a run stops partway through the find stage and is resumed against the same snapshot
- **THEN** each piece is assigned the same key it had in the interrupted run

### Requirement: Piece finder prompt scopes ownership, not reading
The piece finder prompt SHALL include:
- the diff text of its piece, fenced and defanged as untrusted content through `multiplai_core.untrusted`
- the list of files in its piece

The prompt SHALL tell the finder that it owns defects in or caused by that piece's changes, and that it may read any file in the review snapshot. The prompt SHALL NOT contain the instruction "Never read a whole file", or any other instruction that limits how much of a file the finder may read.

#### Scenario: Prompt content
- **WHEN** the piece prompt is built for a piece containing `a.py` and `test_a.py`
- **THEN** the prompt contains the diff text for both files inside an untrusted-content fence
- **AND** it names both files as the piece's scope
- **AND** it states that the whole snapshot may be read
- **AND** it does not contain the string "Never read a whole file"

#### Scenario: Hostile diff text is defanged
- **WHEN** the piece's diff contains text that imitates a fence terminator or a system instruction
- **THEN** the built prompt contains that text only in its defanged form, inside the untrusted fence

#### Scenario: Old builder removed
- **WHEN** the review pipeline's prompt module is inspected
- **THEN** `build_extra` no longer exists in `prompts/find.py`

### Requirement: Piece finders may read the whole snapshot with read-only tools
Piece finders SHALL be given the Read, Grep and Glob tools over the full review snapshot, including files outside their piece. They SHALL NOT be given any tool that runs code or shell commands.

#### Scenario: Tool set
- **WHEN** a piece finder agent is configured
- **THEN** its allowed tools are exactly Read, Grep and Glob
- **AND** its working root is the review snapshot, not a directory restricted to the piece's files

#### Scenario: Reading outside the piece is permitted
- **WHEN** a piece finder reads a file that is not in its piece
- **THEN** the read succeeds and is not treated as an error or a violation

### Requirement: Piece finders report all severities and advice
Piece finders SHALL be instructed to report every defect they find at any severity, including LOW. They SHALL also report advice items. Their output SHALL NOT be filtered to HIGH and MEDIUM, either in the prompt or after conversion.

#### Scenario: LOW findings kept
- **WHEN** a piece finder's converted output contains a LOW-severity finding
- **THEN** that finding reaches merge unchanged in severity and is not dropped by any find-stage severity filter

#### Scenario: Prompt does not restrict severity
- **WHEN** the piece prompt is built
- **THEN** it does not tell the finder to report only HIGH and MEDIUM findings

### Requirement: Piece finders produce a report that is converted to findings
Piece finders SHALL write a plain-text report rather than structured findings. Their output SHALL go through report-then-convert, as defined by the `report-then-convert` capability. Each report SHALL be stored under its piece key and written as a top-level file named `report-piece-<i>.md`.

#### Scenario: Report file written
- **WHEN** piece finder `piece@3` returns its report
- **THEN** `report-piece-3.md` exists in the run's target directory and contains that report

### Requirement: Piece finders run in every round
Piece finders SHALL run in every review round, not only the first. In each round, the find stage SHALL launch one piece finder per piece of that round's diff.

#### Scenario: Second round
- **WHEN** a change review runs a second round on a diff that yields 4 pieces
- **THEN** the second round's find stage launches 4 piece finders
- **AND** the first round's piece finder reports remain in the kept round's files

### Requirement: Piece finder spend counts toward the cost budget
Every model call made by or for a piece finder SHALL be charged to the run's ledger and count toward `--max-cost-usd`. This includes the finder call and its convert call. When the budget is exceeded, the run SHALL stop through the existing budget stop. Reports already returned SHALL be kept in state so a later `resume` does not pay for them again.

#### Scenario: Budget stop mid-pieces
- **WHEN** the ledger exceeds `--max-cost-usd` after 5 of 9 piece finders have returned reports
- **THEN** the run stops with the budget stop
- **AND** the 5 returned reports are present in state and as `report-piece-<i>.md` files

#### Scenario: Resume after budget stop
- **WHEN** that run is resumed with a higher `--max-cost-usd`
- **THEN** the 5 pieces whose reports are stored are not sent to a finder again
- **AND** the remaining 4 piece finders are launched

### Requirement: A failed piece finder does not abort the review
A piece finder that fails with an error other than `RepoTrustError` or a budget stop SHALL have its failure recorded in `state.errors` under its piece key. The other finders SHALL continue.

#### Scenario: One piece finder errors
- **WHEN** piece finder `piece@2` raises a model or parsing error and the other pieces succeed
- **THEN** `state.errors` contains an entry for `piece@2`
- **AND** findings from the other piece finders and the dimension finders reach merge

#### Scenario: Repo trust error propagates
- **WHEN** a piece finder raises `RepoTrustError`
- **THEN** the review stops and propagates that error instead of recording it and continuing

### Requirement: `--extra-finders` and `extra_finders` are accepted no-ops
The `--extra-finders` CLI flag and the `extra_finders` config key SHALL still be accepted, so existing invocations do not fail to parse. Their value SHALL be ignored, and the `extra@<i>` finder path SHALL launch nothing. When either is supplied, the review SHALL print a one-line notice on stderr, once per run, saying the option has no effect because per-piece finders now run by default.

#### Scenario: Flag supplied
- **WHEN** a change review is started with `--extra-finders 4`
- **THEN** argument parsing succeeds
- **AND** exactly one line saying `--extra-finders` has no effect is printed to stderr
- **AND** no find entry keyed `extra@<i>` is created
- **AND** the piece finders launched are the same as for the same review without the flag

#### Scenario: Config key supplied
- **WHEN** the review config sets `extra_finders: 0`
- **THEN** piece finders still run on the change review
- **AND** the one-line notice is printed once

#### Scenario: Neither supplied
- **WHEN** a review runs without `--extra-finders` and without `extra_finders`
- **THEN** no notice about `--extra-finders` is printed