## ADDED Requirements

### Requirement: Locate paths.json beside findings.json
When the viewer loads a review, it SHALL look for `paths.json` in the same location as that review's `findings.json`. It SHALL read the file with the same mechanism it uses for `findings.json`, including the `REVIEW_FILES` handling in `review_viewer/gitdata.py`. It SHALL NOT look for `paths.json` anywhere else.

#### Scenario: paths.json present beside findings.json
- **WHEN** a review directory contains both `findings.json` and `paths.json`
- **THEN** the viewer reads that `paths.json` as the review's path data candidate

#### Scenario: paths.json present only in another location
- **WHEN** a review's `findings.json` has no sibling `paths.json`, but a `paths.json` exists in another directory, such as the repository root or another review's directory
- **THEN** the viewer treats the review as having no path data
- **AND** the viewer does not read the other `paths.json`

#### Scenario: paths.json read from git-backed review storage
- **WHEN** a review is loaded through the git-backed review files handled by `gitdata.py`, and `paths.json` is stored with that review
- **THEN** the viewer reads `paths.json` through the same `REVIEW_FILES` handling used for `findings.json`

### Requirement: Absent paths.json leaves review loading unchanged
When `paths.json` does not exist beside `findings.json`, the viewer SHALL load the review exactly as it does without this change. It SHALL log no warning or error about `paths.json`, and it SHALL serve no path data.

#### Scenario: Review without paths.json
- **WHEN** a review is loaded whose directory has `findings.json` but no `paths.json`
- **THEN** the review data payload has `paths` set to `null`
- **AND** every other field of the payload equals the payload produced before this change for the same review
- **AND** no log entry mentions `paths.json`

### Requirement: Accept paths.json only on an exact full head_sha match
The viewer SHALL accept `paths.json` only when its `commit` field is a string exactly equal to the review's full `head_sha`. The comparison SHALL be a case-sensitive string equality check. The viewer SHALL NOT accept a SHA prefix, an abbreviated SHA, or a match against `base_sha`.

#### Scenario: Commit equals head_sha
- **WHEN** `paths.json` is valid `paths.v1` and its `commit` equals the review's full 40-character `head_sha`
- **THEN** the viewer accepts the file and includes its path data in the review data payload

#### Scenario: Commit is a prefix of head_sha
- **WHEN** `paths.json` is valid `paths.v1` and its `commit` is a 7-character prefix of the review's `head_sha`
- **THEN** the viewer ignores the file and the payload has `paths` set to `null`

#### Scenario: head_sha is a prefix of commit
- **WHEN** the review's `head_sha` is shorter than the file's `commit`, and the `commit` starts with `head_sha`
- **THEN** the viewer ignores the file and the payload has `paths` set to `null`

#### Scenario: Commit equals base_sha
- **WHEN** `paths.json` is valid `paths.v1` and its `commit` equals the review's `base_sha`, but not its `head_sha`
- **THEN** the viewer ignores the file and the payload has `paths` set to `null`

#### Scenario: Commit differs only in letter case
- **WHEN** `paths.json` has a `commit` that equals the review's `head_sha` except for letter case
- **THEN** the viewer ignores the file and the payload has `paths` set to `null`

#### Scenario: Review has no head_sha
- **WHEN** the loaded review has no `head_sha`, or its `head_sha` is empty, and a `paths.json` is present beside it
- **THEN** the viewer ignores the file and the payload has `paths` set to `null`

### Requirement: Log and ignore a commit mismatch
When `paths.json` is present but its `commit` does not match the review's `head_sha`, the viewer SHALL write one log entry that names the file, the file's `commit` and the review's `head_sha`. It SHALL then continue loading the review without path data. A mismatch SHALL NOT cause the review load to fail or return an error response.

#### Scenario: Mismatched commit is logged
- **WHEN** a review is loaded whose `paths.json` has a `commit` different from the review's `head_sha`
- **THEN** exactly one log entry is written for that file
- **AND** the entry contains the `paths.json` location, the file's `commit` value and the review's `head_sha`

#### Scenario: Mismatch does not break the review
- **WHEN** a review is loaded whose `paths.json` has a mismatched `commit`
- **THEN** the review loads with a successful response
- **AND** findings, checks and walkthrough data in the payload equal what they would be without the `paths.json` file

### Requirement: Validate paths.json against the paths.v1 contract
The viewer SHALL validate `paths.json` with `code_facts.PathsResult`, which follows `code_facts/schema/paths.v1.schema.json`. It SHALL NOT use a separate copy of the schema or hand-written field checks in place of `PathsResult`. A file SHALL be accepted only if it passes this validation and the commit match.

#### Scenario: Valid paths.v1 file
- **WHEN** `paths.json` matches `head_sha` and `code_facts.PathsResult` validates it without error
- **THEN** the viewer accepts the file

#### Scenario: Validation uses code_facts
- **WHEN** the viewer validates `paths.json`
- **THEN** the validation calls `code_facts.PathsResult`, imported from the `code-facts` package declared in review-viewer's `pyproject.toml`

#### Scenario: Schema version other than paths.v1
- **WHEN** `paths.json` has a schema identifier other than `paths.v1`, and its `commit` matches `head_sha`
- **THEN** the viewer treats the file as invalid

### Requirement: Log and ignore an invalid paths.json
When `paths.json` cannot be parsed as JSON, cannot be read, or fails `paths.v1` validation, the viewer SHALL write one log entry that names the file and the reason it was rejected. It SHALL then continue loading the review without path data. An invalid file SHALL NOT cause the review load to fail or return an error response.

#### Scenario: Malformed JSON
- **WHEN** `paths.json` contains text that is not valid JSON
- **THEN** the viewer writes one log entry naming the file and reporting a parse failure
- **AND** the review loads successfully with `paths` set to `null`

#### Scenario: Missing required field
- **WHEN** `paths.json` is valid JSON with a matching `commit`, but a path entry lacks a field required by `paths.v1`
- **THEN** the viewer writes one log entry naming the file and reporting a validation failure
- **AND** the review loads successfully with `paths` set to `null`

#### Scenario: Wrong field type
- **WHEN** `paths.json` has a field whose type differs from the `paths.v1` schema, such as a string where a list of hops is required
- **THEN** the viewer writes one log entry and the review loads with `paths` set to `null`

#### Scenario: Unreadable file
- **WHEN** `paths.json` exists, but reading it raises an I/O error
- **THEN** the viewer writes one log entry naming the file and the error
- **AND** the review loads successfully with `paths` set to `null`

#### Scenario: Missing commit field
- **WHEN** `paths.json` is valid JSON but has no `commit` field
- **THEN** the viewer ignores the file, logs one entry, and the payload has `paths` set to `null`

### Requirement: Serve accepted path data in the review payload
When `paths.json` is accepted, the viewer SHALL include its validated content in the review data payload under `paths`. It SHALL serve the content in the same response that serves the review's findings and checks. The served content SHALL keep the order of paths as listed in `paths.json`.

#### Scenario: Accepted file appears in payload
- **WHEN** a review is loaded with an accepted `paths.json`
- **THEN** the review data payload's `paths` field contains the file's path entries
- **AND** each entry keeps its id, entry point, hops, trace status and e2e tests

#### Scenario: Path order preserved
- **WHEN** an accepted `paths.json` lists paths in the order A, B, C
- **THEN** the payload's `paths` list contains A, B, C in that order

#### Scenario: Single request
- **WHEN** the front end requests a review's data
- **THEN** path data arrives in the same response as findings, so no separate request for `paths.json` is needed

#### Scenario: Accepted file with zero paths
- **WHEN** `paths.json` is valid `paths.v1`, matches `head_sha` and lists no paths
- **THEN** the payload's `paths` field contains an empty path list, not `null`

### Requirement: Allow file views for files on accepted paths
When `paths.json` is accepted, the viewer SHALL add to the file-view allowed set each path's:

- entry point file;
- handler file;
- hop files.

It SHALL add them with the same normalization and restrictions it applies to citation paths in `allowed_paths(...)`. Files from an ignored `paths.json` SHALL NOT be added.

#### Scenario: Hop file becomes viewable
- **WHEN** an accepted `paths.json` has a hop in `src/cli/run.py`, and that file is not otherwise in the allowed set
- **THEN** a file-view request for `src/cli/run.py` at the review's commit succeeds

#### Scenario: Entry and handler files become viewable
- **WHEN** an accepted `paths.json` has a path whose entry point is in `hooks/pre_commit.py` and whose handler is in `lib/hooks/handler.py`
- **THEN** file-view requests for both files succeed

#### Scenario: Files from ignored paths.json stay blocked
- **WHEN** `paths.json` is ignored because of a commit mismatch or invalid content, and it names `src/only_in_paths.py`
- **THEN** a file-view request for `src/only_in_paths.py` is refused, as it was before this change

#### Scenario: Path outside the repository is not allowed
- **WHEN** an accepted `paths.json` names a hop file whose path resolves outside the repository, such as `../../etc/passwd`
- **THEN** the file is not added to the allowed set and a file-view request for it is refused, as citation paths are

#### Scenario: Allowed set unchanged without paths.json
- **WHEN** a review is loaded with no `paths.json`
- **THEN** the file-view allowed set equals the set produced before this change for the same review

### Requirement: Loading paths.json never calls a model
Loading, validating and serving `paths.json` SHALL NOT call a language model or any network model service.

#### Scenario: No model call during load
- **WHEN** a review with an accepted `paths.json` is loaded while all model clients are stubbed to fail on any call
- **THEN** the review loads successfully with path data in the payload
- **AND** no model client is called

### Requirement: Python tests cover paths.json loading outcomes
The viewer's Python tests SHALL cover:

- a `paths.json` whose `commit` matches `head_sha`;
- a `paths.json` whose `commit` does not match;
- an invalid `paths.json`.

For each case, the tests SHALL assert the payload's `paths` value and whether a log entry was written.

#### Scenario: Test suite includes the three cases
- **WHEN** the review-viewer Python test suite runs
- **THEN** it includes passing tests for the matched, mismatched and invalid `paths.json` cases
- **AND** the matched case asserts that `paths` is populated
- **AND** the mismatched and invalid cases assert that `paths` is `null` and one log entry was written