## ADDED Requirements

### Requirement: Walkthrough overview names the paths
`review-viewer/SKILL.md` SHALL tell the session that writes the walkthrough to name, in the walkthrough overview, every path in the review's `paths.json`. Each path SHALL be named by its entry point as recorded in `paths.json`, so a reader can match the name to its card in the Paths tab.

#### Scenario: Guidance present in SKILL.md
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it contains an instruction that the walkthrough overview names the paths from `paths.json`
- **AND** the instruction says each path is named by its entry point as recorded in `paths.json`

#### Scenario: Overview written under the guidance for a review with paths
- **WHEN** a session writes a walkthrough for a review whose `paths.json` lists three paths, following `SKILL.md`
- **THEN** the overview text contains the entry-point name of each of the three paths

### Requirement: Walkthrough steps may follow a path's hops
`review-viewer/SKILL.md` SHALL permit walkthrough steps to follow a path's hops through changed code, in hop order, where that explains the change better than file order. The guidance SHALL keep file order as the default. It SHALL state that only hops landing in changed code become steps. Hops outside the diff may be mentioned in step text but SHALL NOT get steps of their own.

#### Scenario: Hop-order permission documented
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it states that steps may follow a path's hops through changed code when that explains the change better than file order
- **AND** it states that file order stays the default otherwise

#### Scenario: Unchanged hops excluded from steps
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it states that a hop whose file and line are not in changed code does not become a walkthrough step of its own

### Requirement: Path guidance applies only to a paths.json the viewer would load
`review-viewer/SKILL.md` SHALL limit the path guidance to a `paths.json` that the viewer would load. That means a file beside `findings.json` whose `commit` exactly equals the review's full `head_sha` and that validates as `paths.v1`. When no such file exists, the guidance SHALL tell the session to write the walkthrough exactly as before, with no mention of paths.

#### Scenario: Condition documented
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** the path guidance states that it applies only when `paths.json` exists beside `findings.json`, its `commit` equals the full `head_sha`, and it validates as `paths.v1`

#### Scenario: No paths.json
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it states that when `paths.json` is absent, the walkthrough is written as before and names no paths

#### Scenario: Stale or invalid paths.json
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it states that a `paths.json` whose `commit` differs from `head_sha`, or which fails `paths.v1` validation, is ignored for the walkthrough just as the viewer ignores it

### Requirement: Path guidance sits beside the facts.json guidance
The walkthrough path guidance SHALL appear in `review-viewer/SKILL.md` immediately before or after the existing `facts.json` walkthrough section, and not in a separate part of the document.

#### Scenario: Placement
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** the path guidance is adjacent to the `facts.json` section, with no other top-level or second-level heading between them

### Requirement: SKILL.md documents that the viewer reads code_facts' paths.v1
`review-viewer/SKILL.md` SHALL include a schema note stating three things:
- the viewer reads `paths.json` against code_facts' `paths.v1` schema (`code_facts/schema/paths.v1.schema.json`);
- the viewer validates it with `code_facts.PathsResult`;
- review-viewer defines no schema of its own for paths.

#### Scenario: Schema note present
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** it contains a schema note that names `paths.v1`, `code_facts/schema/paths.v1.schema.json` and `code_facts.PathsResult`

#### Scenario: No local paths schema
- **WHEN** the review-viewer skill directory is searched for a paths schema file
- **THEN** no `paths` schema file exists under `plugins/multiplai-dev/skills/review-viewer/`

### Requirement: Walkthrough contract is unchanged
The path guidance SHALL be carried only in the walkthrough's existing free-text fields. The `walkthrough.v1` schema and the `walkthrough put` command SHALL NOT change, and SHALL NOT gain a path-id field. `SKILL.md` SHALL NOT tell the session to add path ids as structured walkthrough data.

#### Scenario: Schema unchanged
- **WHEN** this change is compared with its base
- **THEN** the `walkthrough.v1` schema file and the `walkthrough put` implementation have no differences

#### Scenario: Walkthrough naming paths is accepted
- **WHEN** `walkthrough put` receives a walkthrough whose overview names paths in prose and whose steps follow a path's hop order
- **THEN** it validates as `walkthrough.v1` and is stored without error

#### Scenario: Extra path-id field rejected or ignored as today
- **WHEN** `walkthrough put` receives a walkthrough with an added `path_ids` field
- **THEN** it behaves exactly as it does today for any field not in `walkthrough.v1`

#### Scenario: No structured path ids in guidance
- **WHEN** `review-viewer/SKILL.md` is read
- **THEN** the path guidance does not tell the session to add a path-id field to the walkthrough

### Requirement: Viewer does not generate path text in the walkthrough
The viewer SHALL render the stored walkthrough as written. It SHALL NOT insert, rewrite or reorder walkthrough content based on `paths.json`, and it SHALL NOT call a model to do so.

#### Scenario: Walkthrough rendered as stored with paths loaded
- **WHEN** a review with a valid, commit-matching `paths.json` and a stored walkthrough that names no paths is opened in the viewer
- **THEN** the rendered walkthrough overview and steps match the stored walkthrough exactly, in stored order, with no path names added

#### Scenario: No model call
- **WHEN** a review with `paths.json` and a walkthrough is loaded and rendered
- **THEN** the viewer makes no model or LLM API call