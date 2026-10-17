## ADDED Requirements

### Requirement: Paths section is rendered from the run's paths file
When the run's target directory contains a `paths.json` written by the paths stage, the Markdown report renderer SHALL include a section headed `## Paths`. The renderer SHALL build the section only from `paths.json` and the final `findings.json` of the same run. It SHALL NOT call `code_facts.map_paths` or any model.

#### Scenario: Change review with mapped paths
- **WHEN** a change review completes and `paths.json` holds 3 paths
- **THEN** the Markdown report contains exactly one `## Paths` heading, followed by exactly 3 path entries

#### Scenario: Rendering makes no model or mapping calls
- **WHEN** the report is rendered from a fixture `paths.json` and `findings.json`, with `map_paths` and the model client patched to raise if called
- **THEN** rendering succeeds and neither patched function is called

#### Scenario: Re-rendering after resume uses the stored paths file
- **WHEN** a run is resumed after the paths stage finished, and the report is rendered again
- **THEN** the Paths section matches the contents of the existing `paths.json`, and the paths stage records no new path answers

### Requirement: Each path entry shows its entry point
For each path, the section SHALL show the path id and the path's entry point. The entry point SHALL be shown as its name together with its file and line as recorded in `paths.json`.

#### Scenario: Entry point is displayed
- **WHEN** `paths.json` contains path `p07` with entry `review_cmd` at `cli.py` line 42
- **THEN** the entry for `p07` shows the id `p07`, the name `review_cmd` and the location `cli.py:42`

### Requirement: Each path entry shows who invokes it
For each path, the section SHALL list the invokers recorded for the path's entry in `paths.json`, such as a CLI command, a skill or a hook. When no invoker is recorded, the section SHALL show the text `unknown invoker` for that path.

#### Scenario: Recorded invokers are listed
- **WHEN** path `p02` has the invokers `/multiplai-dev:review` and `review_pipeline.__main__`
- **THEN** the entry for `p02` lists both invokers

#### Scenario: Path with no recorded invoker
- **WHEN** path `p03` has an empty invoker list
- **THEN** the entry for `p03` shows `unknown invoker`

### Requirement: Each path entry shows the number of findings that lie on it
For each path, the section SHALL show the number of findings in the final `findings.json` whose `paths` list contains that path's id. A finding SHALL count at most once per path, however many of its citations fall on the path. A finding that has no `paths` field SHALL count toward no path.

#### Scenario: Findings are counted by path id
- **WHEN** `findings.json` has 4 findings, 2 of which list `p01` in `paths`
- **THEN** the entry for `p01` shows a finding count of 2

#### Scenario: Finding with several citations on one path counts once
- **WHEN** one finding lists `p01` in `paths` and has 3 citations that each lie on `p01`
- **THEN** that finding adds 1 to the count for `p01`

#### Scenario: Finding on two paths counts for both
- **WHEN** one finding lists both `p01` and `p04` in `paths`
- **THEN** that finding adds 1 to the count for `p01` and 1 to the count for `p04`

#### Scenario: Path with no findings
- **WHEN** no finding lists `p05` in `paths`
- **THEN** the entry for `p05` is still shown, with a finding count of 0

#### Scenario: Findings without a paths field
- **WHEN** `findings.json` has findings that lack the `paths` field
- **THEN** rendering succeeds, and those findings do not add to the count for any path

### Requirement: Each path entry lists its e2e tests by classification
For each path, the section SHALL list the e2e tests recorded in `paths.json`. Each test SHALL be marked as `counted` or as `mocked (not counted)`, according to the mocked flag in `paths.json`. When a path has no e2e test recorded, or has only mocked tests, the entry SHALL show `no e2e test`, and it SHALL still list any mocked tests.

#### Scenario: Real e2e test is counted
- **WHEN** path `p01` has one e2e test `tests/e2e/test_review.py::test_full_run` that is not marked mocked
- **THEN** the entry for `p01` lists that test, marked `counted`, and does not show `no e2e test`

#### Scenario: Mocked test is listed but not counted
- **WHEN** path `p02` has only one e2e test, and that test is marked mocked
- **THEN** the entry for `p02` lists the test, marked `mocked (not counted)`, and also shows `no e2e test`

#### Scenario: Path without any e2e test
- **WHEN** path `p03` has an empty e2e test list
- **THEN** the entry for `p03` shows `no e2e test`

#### Scenario: Mixed counted and mocked tests
- **WHEN** path `p04` has one test that is not mocked and one test that is mocked
- **THEN** the entry for `p04` lists both tests with their own markers, and does not show `no e2e test`

### Requirement: Rendering the Paths section never runs tests
Producing the Paths section SHALL NOT run any test, test runner or subprocess. Test classification SHALL come only from `paths.json`.

#### Scenario: No subprocess during rendering
- **WHEN** the report is rendered with `subprocess.run`, `subprocess.Popen` and `os.system` patched to raise if called
- **THEN** rendering succeeds and none of the patched functions is called

### Requirement: Path entries follow the order of the paths file
The section SHALL list paths in the order they appear in `paths.json`. Each path SHALL be listed exactly once.

#### Scenario: Order is preserved
- **WHEN** `paths.json` lists the paths `p03`, `p01` and `p02`, in that order
- **THEN** the Paths section lists `p03`, then `p01`, then `p02`

### Requirement: Paths section is omitted when the paths stage did not run
When the paths stage did not run for a review, the report SHALL NOT contain a `## Paths` section. This covers a `--tree` or `--dir` review started without `--paths`.

#### Scenario: Tree review without --paths
- **WHEN** a `--tree` review completes without `--paths`
- **THEN** no `paths.json` is written and the Markdown report has no `## Paths` heading

### Requirement: Paths section states when no paths are available
When the paths stage ran but produced no usable paths, the report SHALL still contain the `## Paths` heading. Under it, the report SHALL show a one-line notice that no paths were mapped, followed by the reason recorded in coverage. This covers a failed mapping, an empty mapping, or a run in which every entry was dropped. The report SHALL render without raising an error in these cases.

#### Scenario: Mapping failed
- **WHEN** the paths stage recorded a failed mapping in coverage and `state.errors`, and the review continued without paths
- **THEN** the report contains `## Paths`, followed by a notice that no paths were mapped and the recorded failure reason, and the rest of the report is rendered

#### Scenario: Mapping returned no paths
- **WHEN** `paths.json` exists and holds zero paths
- **THEN** the report contains `## Paths`, followed by a notice that no paths were mapped

### Requirement: Paths section notes dropped and capped entries
When coverage records path entries as dropped, as having broken traces, or as excluded by `cap=21`, the Paths section SHALL show the number of such entries in one summary line after the path entries.

#### Scenario: Entries were dropped
- **WHEN** coverage records 2 dropped entries and 1 broken trace for the paths stage
- **THEN** the Paths section ends with one line that reports 2 dropped entries and 1 broken trace

#### Scenario: Nothing was dropped
- **WHEN** coverage records no dropped, broken or capped path entries
- **THEN** the Paths section contains no summary line about dropped entries

### Requirement: Path text from the paths file is rendered inertly
Text in the Paths section that comes from `paths.json` SHALL be rendered so that it cannot change the structure of the Markdown report. This covers entry names, invokers and test ids. Each such value SHALL be shown as inline code with backticks escaped. It SHALL NOT be able to begin a heading, link, HTML tag or list item.

#### Scenario: Entry name containing Markdown syntax
- **WHEN** a path's entry name is ``## Injected [x](http://evil) `tick` ``
- **THEN** the report contains no extra heading or link from that value, and the value is shown as escaped inline code within the path's entry

#### Scenario: Invoker containing HTML
- **WHEN** an invoker value is `<script>alert(1)</script>`
- **THEN** the rendered report shows the value as inline code, and contains no raw `<script>` tag outside a code span