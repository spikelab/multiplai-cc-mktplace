## ADDED Requirements

### Requirement: Pieces apply to change reviews only
The review SHALL split the diff into pieces for every change review, meaning a review of a base-to-head diff. The review SHALL NOT compute pieces for `--tree` or `--dir` reviews. Piece splitting SHALL replace `chunks.finder_chunks`, and `finder_chunks` SHALL no longer be called by any stage.

#### Scenario: Change review produces pieces
- **WHEN** a change review runs on a diff that touches at least one non-lockfile file
- **THEN** the find stage receives one or more pieces, each with a 1-based integer id

#### Scenario: Tree review produces no pieces
- **WHEN** a `--tree` or `--dir` review runs, with or without `--paths`
- **THEN** no pieces are computed and no piece finder keys appear in the run state

#### Scenario: Old chunker is not used
- **WHEN** the review test suite searches the pipeline for calls to `finder_chunks`
- **THEN** no stage calls `finder_chunks`

### Requirement: Split only at file boundaries
The splitter SHALL assign each changed file's whole diff text to exactly one piece. It SHALL NOT split one file's hunks across two or more pieces. This SHALL hold for files of any size.

#### Scenario: Every file appears in exactly one piece
- **WHEN** a diff touching N non-lockfile files is split
- **THEN** each of the N file paths appears in exactly one piece, and the union of all pieces' files equals the set of changed non-lockfile files

#### Scenario: All hunks of a file stay together
- **WHEN** a file has several hunks whose total diff text exceeds 50,000 characters
- **THEN** all of that file's hunks appear in the same single piece

### Requirement: Group files by nearest package or skill directory
The splitter SHALL assign each changed file to a group. The group is the file's nearest ancestor directory, at head, that is either a package directory or a skill directory. A package directory contains `pyproject.toml`, `package.json`, `Cargo.toml` or `go.mod`. A skill directory contains `SKILL.md`. When a file has no such ancestor below the repository root, its group SHALL be the repository root. For a file deleted at head, the splitter SHALL use the directory layout at base. Except for the meta piece, a piece SHALL contain files from exactly one group.

#### Scenario: Files in the same skill share a group
- **WHEN** `skills/review/scripts/a.py` and `skills/review/SKILL.md` change, and `skills/review/` contains `SKILL.md`
- **THEN** both files are in the same group, and in the same piece when their combined diff text is at most 50,000 characters

#### Scenario: Nearest marker wins
- **WHEN** a file lies under a directory with `pyproject.toml` that is itself inside a skill directory
- **THEN** the file's group is the directory with `pyproject.toml`

#### Scenario: Files in different groups are not mixed
- **WHEN** two small files in different package directories change
- **THEN** they are placed in two different pieces

#### Scenario: Ungrouped files fall back to the root group
- **WHEN** a changed file has no package or skill ancestor directory
- **THEN** it is assigned to the repository-root group

#### Scenario: Deleted file uses base layout
- **WHEN** a file and its package's `pyproject.toml` are both deleted at head
- **THEN** the file is grouped by the package directory as it existed at base

### Requirement: Pair test files with their module
The splitter SHALL place a changed test file in the same piece as its changed module. A test file pairs with a module when the names match one of these patterns: `test_<name>.py` or `<name>_test.py` with `<name>.py`, `<name>_test.go` with `<name>.go`, or `<name>.test.<ext>` or `<name>.spec.<ext>` with `<name>.<ext>`. When more than one changed module matches, the splitter SHALL choose the module that shares the longest common directory prefix with the test file. Ties SHALL be broken by lexical path order. A paired test file SHALL take its module's group. A test file with no changed matching module SHALL stay in its own group.

#### Scenario: Test in a separate tests directory joins its module
- **WHEN** `pkg/tests/test_chunks.py` and `pkg/src/review_pipeline/chunks.py` both change
- **THEN** both files are in the same piece

#### Scenario: Test without a changed module
- **WHEN** `tests/test_merge.py` changes but no `merge.py` changes
- **THEN** the test file is placed by its own group, and no error is recorded

#### Scenario: Ambiguous module match
- **WHEN** `a/tests/test_util.py` changes along with `a/util.py` and `b/util.py`
- **THEN** the test file is paired with `a/util.py`

#### Scenario: Pair too large to share a piece
- **WHEN** a test file and its module together exceed 50,000 characters of diff text
- **THEN** each file is placed in its own piece, and the module's piece id is less than the test file's piece id

### Requirement: Piece size budget
A piece SHALL hold at most 50,000 characters of diff text. The count is the sum of the unified diff text of its files, including file headers and hunk headers. The only exception is a piece holding a single file whose diff text alone exceeds 50,000 characters. Such a file SHALL get its own piece, and no other file SHALL share that piece. When a group exceeds the budget, the splitter SHALL fill pieces in its deterministic file order. It SHALL start a new piece when adding the next file, or the next test-module pair, would exceed the budget.

#### Scenario: Group within budget is one piece
- **WHEN** a group's files total 30,000 characters of diff text
- **THEN** the group forms exactly one piece

#### Scenario: Group over budget splits at file boundaries
- **WHEN** a group's files total 120,000 characters, and no single file exceeds 50,000
- **THEN** the group forms at least three pieces, and each is at most 50,000 characters

#### Scenario: Oversized single file gets its own piece
- **WHEN** one file has 80,000 characters of diff text
- **THEN** that file is the only file in its piece, and the piece is not truncated

#### Scenario: Budget boundary is inclusive
- **WHEN** a group's files total exactly 50,000 characters
- **THEN** the group forms exactly one piece

### Requirement: No limit on piece count
The splitter SHALL NOT cap the number of pieces. The former cap of 6 in `finder_chunks` SHALL NOT apply, and pieces SHALL NOT be merged or dropped to reach a target count.

#### Scenario: Many groups yield many pieces
- **WHEN** a diff touches files in 20 different groups, each under budget, plus one meta file
- **THEN** 21 pieces are produced, and every changed non-lockfile file is in one of them

### Requirement: Shared meta piece
The splitter SHALL place all changed release and repository metadata files in a single piece marked `meta`. Metadata files include `CHANGELOG.md` files, `plugin.json` and `marketplace.json` manifests, version files, `LICENSE*`, `.gitignore`, `.gitattributes`, and files under `.github/`. Metadata files SHALL NOT appear in any group piece. The meta piece is exempt from the one-group rule. When the metadata files' diff text exceeds 50,000 characters, the meta piece SHALL split at file boundaries under the same budget rule, and every resulting piece SHALL be marked `meta`. When no metadata file changes, no meta piece SHALL exist.

#### Scenario: Metadata from several plugins shares one piece
- **WHEN** `plugins/a/CHANGELOG.md`, `plugins/b/.claude-plugin/plugin.json` and `.github/workflows/ci.yml` change
- **THEN** all three files are in one piece marked `meta`, and none of them is in any other piece

#### Scenario: No metadata changes
- **WHEN** a diff changes only source and test files
- **THEN** no piece is marked `meta`

### Requirement: Lockfiles excluded from diff text
The splitter SHALL leave the diff text of lockfiles out of every piece. Lockfiles include `uv.lock`, `poetry.lock`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock` and `go.sum`. Lockfiles SHALL NOT count toward any piece's size. A changed lockfile's path SHALL be listed in the meta piece's file list with a note that its content is omitted. When only lockfiles and no other metadata change, a meta piece SHALL be created to hold that list.

#### Scenario: Lockfile content omitted
- **WHEN** a diff changes `uv.lock` by 200,000 characters and `src/app.py` by 5,000 characters
- **THEN** no piece contains any `uv.lock` hunk text, the meta piece lists `uv.lock` as omitted, and the piece holding `src/app.py` counts 5,000 characters

#### Scenario: Lockfile-only diff
- **WHEN** the only changed file is `package-lock.json`
- **THEN** exactly one piece exists, it is marked `meta`, and it lists `package-lock.json` as omitted with no hunk text

### Requirement: Deterministic and stable piece ids
The splitter SHALL produce the same pieces, in the same order and with the same ids, for the same diff. Group pieces SHALL be ordered by group path and then by first file path. The meta piece or pieces SHALL come last. Ids SHALL be contiguous 1-based integers. The run state SHALL record the computed pieces, and `resume` SHALL reuse them without splitting again. This keeps finder keys such as `piece-3` and report files such as `report-piece-3.md` stable across resumes.

#### Scenario: Repeat split is identical
- **WHEN** the same diff is split twice
- **THEN** both results have the same ids, file lists and order

#### Scenario: Resume reuses stored pieces
- **WHEN** a run stops after splitting and is resumed
- **THEN** the resumed run uses the pieces stored in state, and piece ids in its finder keys match those of the first run

#### Scenario: Ids are contiguous
- **WHEN** a split produces K pieces
- **THEN** the piece ids are exactly 1 through K

### Requirement: Edge-case diffs
The splitter SHALL handle a diff with no changed files by producing zero pieces and recording no error. A binary file's piece SHALL list the file with a binary-change note in place of hunk text, and the file SHALL count as zero characters. A renamed file SHALL be grouped by its head path.

#### Scenario: Empty diff
- **WHEN** a change review runs on a diff with no changed files
- **THEN** zero pieces are produced, no piece finder runs, and `state.errors` gains no entry from splitting

#### Scenario: Binary file
- **WHEN** a diff changes a PNG file under a skill directory
- **THEN** the PNG is listed in that skill's piece with a binary-change note, and it adds zero characters to the piece's size

#### Scenario: Renamed file
- **WHEN** `old/pkg/mod.py` is renamed to `new/pkg/mod.py` with edits
- **THEN** the file is grouped by `new/pkg/`'s nearest package or skill directory