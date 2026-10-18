## ADDED Requirements

### Requirement: Paths section is mandatory in human mode
In human mode, every rendered `<name>-walkthrough.md` SHALL contain exactly one section titled **Paths**. It SHALL appear whether or not the trace stage produced any paths. The section SHALL occupy the position that "End-to-end scenarios" held in the section list (`stages/write.py`). The writer SHALL NOT be able to omit it.

#### Scenario: Paths section present when paths were traced
- **WHEN** a human-mode walkthrough runs on a module where `map_paths` returns at least one traced path
- **THEN** the rendered `<name>-walkthrough.md` contains exactly one heading with the text `Paths`
- **AND** that heading is at the position "End-to-end scenarios" previously held in the section order

#### Scenario: Paths section present when writer output omits it
- **WHEN** the writer agent returns section content that does not include a Paths section
- **THEN** the rendered `<name>-walkthrough.md` still contains the Paths heading
- **AND** the Paths body is rendered from the trace results

### Requirement: Paths section replaces "End-to-end scenarios"
New human-mode walkthroughs SHALL NOT contain a section titled "End-to-end scenarios". In `<name>-walkthrough.json` (v1), the step that was titled "End-to-end scenarios" SHALL be titled "Paths". The JSON shape SHALL be otherwise unchanged.

#### Scenario: Old section title absent from new Markdown
- **WHEN** a human-mode walkthrough is generated after this change
- **THEN** `<name>-walkthrough.md` contains no heading with the text `End-to-end scenarios`

#### Scenario: JSON step renamed only
- **WHEN** a human-mode walkthrough is generated and `<name>-walkthrough.json` is written
- **THEN** exactly one step has the title `Paths`
- **AND** no step has the title `End-to-end scenarios`
- **AND** the file still validates against the existing walkthrough v1 schema with no new required or removed keys

### Requirement: Agent mode has no Paths section
In agent mode, the walkthrough SHALL NOT render a Paths section.

#### Scenario: Agent mode output
- **WHEN** a walkthrough runs in agent mode
- **THEN** the output contains no heading with the text `Paths`
- **AND** it contains no hop ids matching `p\d+h\d+`

### Requirement: Paths are listed in code_facts rank order
The Paths section SHALL list traced paths in the order `code_facts.map_paths` returns them. Paths SHALL be numbered from 1 in that order. The walkthrough SHALL NOT re-sort, regroup or re-rank them.

#### Scenario: Order preserved
- **WHEN** `map_paths` returns traced paths for entries A, B, C in that order
- **THEN** the Paths section shows A as path 1, B as path 2 and C as path 3, in that order

#### Scenario: Web seeds not promoted
- **WHEN** a caller seed derived from a web route is ranked by `map_paths` after a CLI entry
- **THEN** the CLI entry's path appears before the web route's path in the Paths section

### Requirement: Each path states its entry point, kind and invoker
For each traced path, the Paths section SHALL state three things:
- the entry point's name and its `path:line` location;
- the entry kind as reported by `code_facts`, for example an argparse command, a click command, a typer command, `main`, a script, a skill command, a hook or a caller seed;
- who invokes the entry.

When `code_facts` provides no invoker, the section SHALL explicitly state that the invoker is unknown. It SHALL NOT omit the field.

#### Scenario: CLI entry described
- **WHEN** a traced path starts at a click command `sync` defined at `cli/main.py:42`
- **THEN** that path's entry line names `sync`, links `cli/main.py:42`, states the click command kind and states who invokes it

#### Scenario: Unknown invoker
- **WHEN** a traced path's entry has no invoker information
- **THEN** that path's entry description contains an explicit "unknown" invoker statement rather than an empty field

### Requirement: Hops are rendered as path:line links with stable ids
Each hop of path *n* SHALL be rendered in hop order as a Markdown link whose text is `path:line`. Each hop SHALL have the id `p{n}h{k}`, where:
- *n* is the 1-based path number;
- *k* is the 1-based hop index within that path.

Ids SHALL be unique within the walkthrough. Ids SHALL be deterministic for the same `map_paths` result. The id SHALL be shown next to its hop.

#### Scenario: Hop ids assigned
- **WHEN** path 2 has three hops at `a.py:10`, `b.py:20` and `c.py:30`
- **THEN** the section renders links with text `a.py:10`, `b.py:20` and `c.py:30` in that order
- **AND** they carry ids `p2h1`, `p2h2` and `p2h3` respectively

#### Scenario: Deterministic ids on re-render
- **WHEN** the same cached trace answers are rendered twice, for example after `resume`
- **THEN** both renders assign identical hop ids to identical hops

### Requirement: Each path states where and why the trace stopped
For each traced path, the Paths section SHALL state where the trace stopped, as a `path:line` link or as the last hop's id. It SHALL also give the stop reason reported by `code_facts` for that path.

#### Scenario: Stop location and reason shown
- **WHEN** a traced path stops at `store/db.py:88` with a stop reason reported by `code_facts`
- **THEN** that path's entry in the section links `store/db.py:88` and includes that stop reason text

### Requirement: Each path lists its e2e test coverage
For each traced path, the Paths section SHALL show which e2e tests exercise it. This section SHALL list `E2ETest` items as counted tests. It SHALL list `MockedE2ETest` items separately, labelled as not counted, and SHALL name the hop that the test mocks. When the path has no counted e2e tests, the section SHALL contain the literal text `no e2e test` for that path. This SHALL hold even if mocked tests are listed.

#### Scenario: Counted tests listed
- **WHEN** a traced path has two `E2ETest` items
- **THEN** both tests are listed under that path as counted

#### Scenario: Mocked test listed as not counted
- **WHEN** a traced path has one `MockedE2ETest` that mocks the next hop after the entry, and no `E2ETest`
- **THEN** that test is listed under that path, labelled as not counted, and names the mocked hop
- **AND** the path also shows `no e2e test`

#### Scenario: No tests at all
- **WHEN** a traced path has neither `E2ETest` nor `MockedE2ETest` items
- **THEN** that path shows the literal text `no e2e test`

### Requirement: Writers cite hops by id
The writer prompts SHALL give the writer agents the hop ids. They SHALL instruct the writers to cite path hops by `p{n}h{k}` id. The rendered Markdown SHALL resolve each valid hop-id citation to that hop's `path:line` link. A citation of a hop id that does not exist in the current trace results SHALL be reported as an invalid citation, in the same way as other invalid citation ids. It SHALL NOT be rendered as a working link.

#### Scenario: Valid hop citation resolved
- **WHEN** a writer cites `p1h2` in another section and path 1 hop 2 is at `svc/handler.py:57`
- **THEN** the rendered Markdown shows a link to `svc/handler.py:57` at that citation

#### Scenario: Unknown hop citation rejected
- **WHEN** a writer cites `p9h1` and there is no path 9
- **THEN** the citation is reported as invalid
- **AND** no link to a fabricated location is rendered

#### Scenario: Prompt includes hop ids
- **WHEN** the write stage builds its writer prompts and there are traced paths
- **THEN** the prompt text contains every assigned hop id with its `path:line`

### Requirement: Left-out entries are shown in the coverage table
When `code_facts` returns `PathDrop` items, the walkthrough SHALL list every dropped entry in the coverage table with its stage and reason. Drop reasons include `seed_not_found`, `unreachable` and `over_cap`. A trace that fails for a single entry is also a drop. When any entries were dropped, the Paths section SHALL state how many entries were left out and refer to the coverage table.

#### Scenario: Over-cap drops visible
- **WHEN** `map_paths` finds 25 entries with `--scenarios` 21
- **THEN** the Paths section shows 21 paths
- **AND** the coverage table lists 4 entries with stage and reason `over_cap`
- **AND** the Paths section states that 4 entries were left out

#### Scenario: Seed not found and unreachable drops visible
- **WHEN** `map_paths` returns one drop with reason `seed_not_found` and one with reason `unreachable`
- **THEN** both appear in the coverage table with their stage and reason

#### Scenario: Per-entry trace failure visible
- **WHEN** the trace for one entry raises an agent call error and the other entries succeed
- **THEN** the other entries render as paths
- **AND** the failed entry appears in the coverage table with its stage and a failure reason

### Requirement: Empty Paths section explains why
When there are no traced paths, the Paths section SHALL still render. It SHALL state why there are no paths, in one of three ways:
- no entry points were found;
- entries were found but all were left out, with the count and a reference to the coverage table;
- path finding failed.

When path finding failed for the whole stage, the section SHALL include the error recorded in `state.errors`. The run SHALL still complete.

#### Scenario: No entries found
- **WHEN** `map_paths` returns no entries and no drops
- **THEN** the Paths section contains text stating that no entry points reaching the module were found
- **AND** it contains no hop ids

#### Scenario: All entries dropped
- **WHEN** `map_paths` returns zero traced paths and three drops
- **THEN** the Paths section states that no paths were traced and that 3 entries were left out, with a reference to the coverage table

#### Scenario: Whole-stage failure
- **WHEN** the trace stage raises a non-fatal error that is recorded in `state.errors`
- **THEN** the walkthrough is still written
- **AND** the Paths section states that path finding failed and includes that error's message

#### Scenario: Fatal errors still stop the run
- **WHEN** the trace stage raises `RepoTrustError` or the `--max-usd` budget breaker trips
- **THEN** the run stops as it does today, without rendering a Paths section that claims success