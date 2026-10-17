## ADDED Requirements

### Requirement: Plan-only output states the expected piece-finder cost
When `--plan-only` is given for a change review, the output SHALL include an estimated model cost for the per-piece finders. The estimate SHALL be calibrated on the PR #295 reference, where a 227 KB diff costs about 14 USD for pieces. It SHALL scale linearly with the number of diff characters that go into pieces, so lockfiles kept out of the diff text are excluded. The amount SHALL be labeled as approximate and shown in USD, rounded to whole dollars.

#### Scenario: Reference-sized diff
- **WHEN** `--plan-only` runs on a change review whose piece diff text totals 227 KB
- **THEN** the output contains a piece-finder estimate of about 14 USD, within ±1 USD
- **AND** the estimate is labeled as approximate

#### Scenario: Smaller diff scales down
- **WHEN** `--plan-only` runs on a change review whose piece diff text totals about 113 KB
- **THEN** the piece-finder estimate is about half the 227 KB reference figure, within ±1 USD

#### Scenario: Lockfile changes excluded from the estimate
- **WHEN** a change review's diff includes a 100 KB lockfile change and 50 KB of other diff text
- **THEN** the piece-finder estimate is computed from the 50 KB only

### Requirement: Plan-only output states the expected path-finder cost
When the paths stage would run, `--plan-only` output SHALL include an estimated model cost for the path finders. The paths stage runs on every change review, and on tree or dir reviews only with `--paths`. The estimate SHALL be calibrated on the PR #295 reference of about 24 USD for paths on a 227 KB diff. It SHALL assume the path cap of 21 paths, because `--plan-only` does not call `map_paths`. The output SHALL also show the sum of the piece and path estimates. It SHALL state that this total excludes the dimension finders, verification and the paths-mapping stage.

#### Scenario: Change review shows path estimate and total
- **WHEN** `--plan-only` runs on a change review with a 227 KB piece diff
- **THEN** the output contains a path-finder estimate of about 24 USD, within ±1 USD
- **AND** the output contains a combined piece-plus-path estimate of about 38 USD, within ±2 USD
- **AND** the output states that the total excludes dimension finders, verification and paths mapping

#### Scenario: Tree review without --paths
- **WHEN** `--plan-only` runs on a `--tree` review without `--paths`
- **THEN** the output contains no path-finder estimate
- **AND** any combined total equals the piece-finder estimate only

#### Scenario: Tree review with --paths
- **WHEN** `--plan-only` runs on a `--tree` review with `--paths`
- **THEN** the output contains a path-finder estimate based on the cap of 21 paths

### Requirement: Plan-only spends nothing to produce the estimate
Producing the cost estimate SHALL NOT make model calls, call `map_paths`, or write a ledger entry. It SHALL NOT add to the spend counted against `--max-cost-usd`.

#### Scenario: No spend during plan-only
- **WHEN** `--plan-only` runs on a change review
- **THEN** no model or agent call is made
- **AND** `map_paths` is not invoked
- **AND** the run's recorded spend is 0 USD

### Requirement: Budget warning when the estimate reaches the budget
When the combined piece-plus-path estimate is greater than or equal to the effective `--max-cost-usd`, the review SHALL print a warning. The warning SHALL name the estimate and the budget. It SHALL say that the run is expected to stop partway, and that it can be continued with `resume` and a higher `--max-cost-usd`. The warning SHALL appear in `--plan-only` output and at the start of a real run.

#### Scenario: Estimate exceeds explicit budget in plan-only
- **WHEN** `--plan-only` runs with `--max-cost-usd 20` on a change review whose combined estimate is about 38 USD
- **THEN** the output contains a warning naming about 38 USD and 20 USD
- **AND** the warning mentions `resume` with a higher `--max-cost-usd`

#### Scenario: Estimate exceeds budget at real run start
- **WHEN** a real change review starts with `--max-cost-usd 20` and its combined estimate is about 38 USD
- **THEN** the same warning is printed before the paths stage begins

#### Scenario: Estimate below budget
- **WHEN** a change review's combined estimate is 10 USD and `--max-cost-usd` is 50
- **THEN** no budget-exceeded warning is printed

### Requirement: Default-budget notice for change reviews
When a change review runs with the default 50 USD budget, meaning `--max-cost-usd` was not given, the review SHALL state a notice in `--plan-only` output and at the start of a real run. The notice SHALL say that the default budget can stop a large change review partway through, because dimension finders and verification add to the piece and path cost. It SHALL say that `resume` with a higher `--max-cost-usd` continues the run without losing paid results. The notice SHALL be printed even when the combined estimate is below 50 USD.

#### Scenario: Default budget on a change review
- **WHEN** `--plan-only` runs on a change review without `--max-cost-usd`
- **THEN** the output states that the 50 USD default may stop a large change review partway
- **AND** the output mentions `resume` with a higher `--max-cost-usd`

#### Scenario: Explicit budget suppresses the default notice
- **WHEN** `--plan-only` runs on a change review with `--max-cost-usd 120` and a combined estimate of 38 USD
- **THEN** the default-budget notice is not printed
- **AND** no budget-exceeded warning is printed

### Requirement: Cost warnings never block a review
Cost estimates, budget warnings and default-budget notices SHALL be advisory only. A real run SHALL start and proceed to the paths stage and the find stage regardless of the estimate. The exit status of `--plan-only` SHALL NOT depend on whether a warning was printed. The `--max-cost-usd` ledger SHALL remain the only mechanism that stops spending.

#### Scenario: Run proceeds despite a warning
- **WHEN** a real change review starts with `--max-cost-usd 5` and a combined estimate of 38 USD
- **THEN** the warning is printed
- **AND** the review starts its stages without asking for confirmation
- **AND** it stops only when the ledger reaches the 5 USD budget, recording that stop as a budget stop

#### Scenario: Plan-only exit status unaffected
- **WHEN** `--plan-only` prints a budget-exceeded warning
- **THEN** the command exits with the same status as a `--plan-only` run that prints no warning

### Requirement: Resume does not repeat the default notice as a new run start
When `resume` continues a stopped review, the review SHALL print the budget-exceeded warning only if the remaining estimated cost is greater than or equal to the new effective budget minus the spend already recorded. The remaining estimated cost covers pieces and paths whose results are not yet stored in state.

#### Scenario: Resume with sufficient budget
- **WHEN** a review stopped at 50 USD with all piece reports stored and half the path reports stored, and is resumed with `--max-cost-usd 100`
- **THEN** no budget-exceeded warning is printed, because the remaining path estimate of about 12 USD is below 50 USD of remaining budget

#### Scenario: Resume with insufficient budget
- **WHEN** a review stopped at 50 USD with no path reports stored, and is resumed with `--max-cost-usd 55`
- **THEN** a budget-exceeded warning is printed naming the remaining estimate and the 5 USD remaining budget

### Requirement: SKILL.md documents expected cost
`plugins/multiplai-dev/skills/review/SKILL.md` SHALL include a cost section with the following content:

- It states the reference figures: about 14 USD for piece finders plus about 24 USD for path finders on a 227 KB diff.
- It states that these figures come before dimension finders and verification.
- It warns that the 50 USD default will stop a large change review partway through.
- It explains how to continue with `resume` and a higher `--max-cost-usd`.
- It states that the review warns about cost but never refuses to start.

#### Scenario: SKILL.md contains the figures and guidance
- **WHEN** the review test suite checks `SKILL.md`
- **THEN** the file contains the 14 USD piece figure, the 24 USD path figure and the 227 KB reference size
- **AND** the file contains a warning about the 50 USD default stopping large change reviews
- **AND** the file mentions `resume` with `--max-cost-usd`

#### Scenario: Figures match the plan-only calibration
- **WHEN** the test suite compares the reference figures in `SKILL.md` with the calibration constants used by `--plan-only`
- **THEN** the piece and path figures are equal when rounded to whole dollars