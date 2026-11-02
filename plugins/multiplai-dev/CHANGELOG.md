# Changelog

All notable changes to the **multiplai-dev** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-dev@<version>`.

Recorded history starts at **0.1.1**; anything earlier is in `git log` only.

Of the versions recorded here, `0.1.1` and `0.5.0`–`0.5.3` carry a git tag;
tagging then lapsed until `0.9.1`, and from there on every release is tagged
when it lands. Dates on untagged versions are the release dates recorded at the
time, not derived from a tag.

## [0.152.0] - 2026-11-02

### Changed

- **buildme**: a build with no test command is now refused before any model
  call (`PREFLIGHT:FAILED:test-command:...`). This happens when
  `specs/config.yaml` sets no `tdd.test_command` and discovery finds no root
  file it knows. Before, every test gate passed without running a test. To
  build without tests anyway, set `tdd.enabled: false`. That key was written
  by bootstrap but never read; now it is. With it set, the build goes on, but
  `build-progress.md` gets a `WARNING`, the baseline is logged `SKIPPED`, and
  the PR body gets a "Warning: no tests ran" section.
- **buildme**: bootstrap's starter `specs/config.yaml` for a project with no
  test command yet (a brand-new one) now says `tdd.enabled: false`, so that
  project's first build is not refused.
- **buildme**: the test command and `tdd.enabled` are read again once the
  build is in its worktree, and discovery runs there. A test setup that is
  only on the branch the build starts from (`--base-ref`, as every epic
  child does) is used, and so is one an earlier block added before a resume.

### Fixed

- **buildme**: a build of a feature branch (`--base-ref origin/<branch>`)
  started from a checkout with no `specs/config.yaml`, while the branch has
  one, now stops with `... Push it, or pull origin's, first.` It used to go
  on and read no config.

## [0.151.0] - 2026-11-01

### Added

- **pipeline-viewer**: shows stop 4 for a change with components, whose one
  implementation plan buildme writes on the feature branch. The page shows
  the plan, `components.md` and `plan-approval.md`. An engineer can approve
  it or ask for rework there; the decision is committed on the feature
  branch and pushed, naming the plan revision `plan-approval.md` asks for.
  An approval made with buildme's `--approve-plan` shows as well.

## [0.150.0] - 2026-11-01

### Changed

- **buildme**: every build now stops for an engineer to approve its
  implementation plan before any code is written (stop 4, `PLAN:WAITING`,
  exit 3), as pm-spec stops for the proposal. Approve with `--approve-plan`
  or an approval commit on the build's branch. Only `--auto` skips it.
  `review.plan_approval` in `specs/config.yaml` is no longer read; a config
  that still sets it gets a warning.
- **buildme**: a change with components now has one `implementation-plan.md`
  for the whole change, beside its proposal, instead of one per component.
  `/buildme <change>` writes it before launching any component, pushes it to
  the feature branch, and waits for its approval there. Each component writes
  its own `tasks.md` from that plan and its `specs.md`, and cannot edit the
  plan. A change whose components were already planned one at a time keeps
  building that way.

### Added

- **buildme**: each block in `tasks.md` now ends with a list of what its
  agents also get: the requirements, the parts of the implementation plan,
  the earlier blocks' signatures, and which agents get the rubric, unknowns,
  prototype, memory files and reference docs. buildme writes it after the
  plan check; it changes no plan revision.
- **pipeline-viewer**: stop 4's row says it applies to every build without
  `--auto`. The page does not yet show the stop for a change-level plan.

## [0.149.0] - 2026-11-01

### Added

- **`explainer`**: builds an explainer page for a pull request, a process, a
  skill or pipeline, a concept, or a codebase, so every explainer looks and
  reads the same way. The page opens with a plain headline and a
  one-sentence answer, then one picture and one card per stage following a
  single worked example, then deeper sections, with detail in fold-outs. It is
  styled with your design system. For code, the facts come from
  codebase-walkthrough's checked Markdown. `explainer_check check` fails a
  page that shows more than 2,000 words before a fold-out is opened, types a
  colour instead of using a token, loads from a host it may not, holds an
  unsafe or unlabelled SVG, or cites code lines that are not at their commit;
  `render` draws its pictures to PNG so the session looks at them before you
  do. Publish it as an Artifact or keep it as a local file.

## [0.148.5] - 2026-10-31

### Fixed

- **buildme stops a build for size only when the plan reviewer says the plan
  is oversized.** The size gate now parks a build with `SPLIT:REQUIRED` only
  on a critical or major `oversized-plan` finding from the plan check, and
  `split-proposal.md` lists the reviewer's proposed tickets. Before, buildme's
  own keyword match decided: a user-management plan, where "login",
  "password" and "permission" appear in nearly every block, stopped as an auth
  change mixed with unrelated work even when the reviewer found nothing to
  split. buildme's keyword and grouping checks still go to the reviewer, and
  still decide alone when no reviewer answer is saved, such as on a build
  resumed from a checkpoint written by an older version.
- **The grouping links a block to the block whose function it consumes.** A
  `Consumes:` line that starts with `Block 2:` or names its source
  (`account-user-api:`) now matches the name the other block's `Produces:`
  line defines, so blocks that depend on each other are no longer proposed as
  separate PRs. Two endpoints under the same path (`POST api/a`, `POST api/b`)
  are no longer joined because both start with `api`.

## [0.148.4] - 2026-10-31

### Fixed

- **buildme**: words that merely contain "sso" ("associated", "lesson", "processor") no longer mark a block as auth work and trigger a split proposal; only the word SSO and "single sign-on" do.
- **buildme**: a Go dependency named by its full module path (`github.com/spf13/cobra`) now gets an explainer.
- **buildme**: a `Satisfies:` entry that names another capability ("billing: Audit") no longer narrows or adds to the security and permission scenarios of a capability with a requirement of the same name.
- **buildme**: the docs writer no longer puts its "no changelog" note in the `DOCS_IMPACT:` slot, which dropped the edited docs from the PR.
- **buildme**: the research decision prompt now fences the change's spec text as untrusted data, as it already said it did.
- **buildme**: the dependency explainer is now told that the docs, changelogs and issues it fetches are data, never instructions.
- **buildme**: the dependency explainer's Read, Glob and Grep now run in the project, so its search of the project's own code finds it.
- **buildme**: the design writer is now told that the fenced web research it is given is data, never instructions.
- **buildme**: a plan check rewrite that edits only the implementation plan no longer leaves `tasks.md` marked out of date, which refused the build after the recheck passed and regenerated the reviewed tasks on resume.
- **buildme**: a resumed build whose unknowns gate rewrites `unknowns.md` now writes the tasks and rubric again, instead of stopping with "Planning artifacts are incomplete".
- **buildme**: an edit to a component's `plan.md` now marks its design, unknowns, tasks and rubric out of date, and the build refuses to run the earlier plan. A build planned before this version records its `plan.md` on resume and is not planned again.
- **buildme**: a model call that times out or fails before its final result now counts as spend of unknown size, so a build with a spending limit stops until `--accept-unknown-spend`, instead of counting it as free.
- **buildme**: a spent budget or an untrusted repo during codebase analysis now stops the analysis, instead of being recorded as failed slices; an epic's shared analysis then writes no completion marker.
- **buildme**: a child of an epic now analyses again the files its `plan.md` names that the epic's shared analysis failed on, instead of reusing the failure.

## [0.148.3] - 2026-10-31

### Fixed

- **buildme**: thirteen fixes from a code review of buildme's pipeline.
  - A Vitest run with failing tests is a test failure, not a crashed runner: buildme reads Vitest's indented `Tests  1 failed (1)` summary and its `Failed to load url` error instead of retrying the suite three times.
  - The RED gate rejects Vitest and Jest tests that fail to load (a syntax or import error) instead of passing them as unverified RED.
  - Preflight passes a `sh`/`bash`/`zsh -c` test command whose script starts with a `cd` the shell expands (`bash -c 'cd $HOME/x && pytest'`).
  - With `tdd.narrow_commands`, the block gate and the end gate run a change that touches two or more directories, instead of failing every time with "Test command failed to run".
  - A default branch whose name has a slash (`release/2026`) keeps its full name as the build's base and the PR's base.
  - Preflight refuses a hand-run sibling child change (`<epic>-<id>`) while its epic runs elsewhere, as it already did for a nested child (`<epic>/<id>`).
  - Discarding a failed step keeps `.build-state.json`, `build-progress.md` and the other bookkeeping files an agent's `git add -A` staged, instead of deleting them.
  - When git refuses the pre-refactor checkpoint commit or the mutation survivor round's commit (a failing pre-commit hook, say), refactor-all is skipped or the round is discarded, instead of the build ending FAILED with unverified edits in the tree.
  - When git refuses the commit of a regenerated walkthrough, the walkthrough's note says so and publish goes on, instead of aborting.
  - Continuing a `--spec-only` build merges origin's default branch into its local branch without pushing a separate merge, so its own push at publish is no longer rejected as non-fast-forward.
  - The PR review and the component review hear what a kept fix on pass left open: findings it fixed are no longer listed as open, and that review's new open and rejected findings are included.
  - A review dimension scored 1 with confidence of at least 0.5 fails the review gate on its own; before, any confidence below 1.0 lifted it above the critical score. The floor is `code_review.gate.critical_min_confidence`.
  - A Python project found by `pytest.ini` or `setup.py` gets the Python reference docs, as one found by `pyproject.toml` does.

## [0.148.2] - 2026-10-31

### Fixed

- **review: resuming after a usage-limit stop keeps `--verify-low` and `--model`.** The `resume` command printed after the stop now carries both, so the resumed run still verifies the LOW findings and runs the verifier and merger on the same model.
- **review: a re-pushed PR no longer carries a finding that cites changed lines.** A finding from the last round is copied unchanged only when every file it cites is the same at both heads. One citing a file that left the change, or that a moved base rewrote, is rechecked instead of being exported with lines that are not at the new head.
- **review: `--plan-only` no longer breaks the resume of a stopped review.** On a directory holding a run that `resume` can continue, `--plan-only` keeps that run's `diff.patch`, `commits.txt`, `files.txt` and rounds as they were.
- **review: `post` refuses to mix two runs.** When a later run rewrote `review-state.json` and stopped before writing its `findings.json`, `post` now exits 2 before any GitHub call and says to finish that run with `resume`. Before, it placed one run's findings on the other run's diff, and GitHub could refuse the whole review.
- **review: a tree review whose every finder failed can be resumed.** `resume` now asks every finder again instead of failing the same way each time.
- **review: the verifier is no longer told that CI blocks no merge when classic branch protection requires checks.** The review now also reads the base branch's classic protection. When that read fails, the verifier is told whether CI blocks a merge is unknown, and the review lists the `gh api` command that would settle it.
- **review: defects a path finder finds in files the change does not touch are kept.** The path finder now marks them `Pre-existing: yes`, as the other finders do, and writes `Could not check:` for what it could not read, which becomes a need.

## [0.148.1] - 2026-10-31

### Fixed

- **buildme**: the preflight runner check no longer fails a `bash -c` test command whose script starts with a shell builtin such as `set -e`. A `sh -c`, `bash -c` or `zsh -c` script that starts with a builtin, a keyword (`if`, `for`, `{`, …) or an assignment (`X=1 …`) is now checked as its shell, so DolceBot's `bash -c 'set -e; …'` no longer stops the build with `PREFLIGHT:FAILED:test-runner:… set is not on PATH`. A script whose own program is missing (`bash -c 'nosuchtool -q'`) still fails as before.

## [0.148.0] - 2026-10-29

### Added

- **buildme: `epic watch` runs the epic until it is done or needs a
  person.** `python -m build_pipeline epic watch --change <parent>
  --project-dir <project> --trust-repo` runs the epic, then runs it again
  after a network failure, an agent timeout or a hit Claude usage limit. For
  the usage limit it waits until the reset time the error names; otherwise it
  waits 5 minutes, then twice as long each time in a row, up to 60. It stops
  when every child is merged, when a person is needed (a manual merge, the
  spend cap, a feature-branch conflict, a spec refusal, a split, a parked
  child or a plan waiting for approval), or on a failure it cannot name, and
  then posts one Slack message through the pipeline-viewer (`SLACK_TOKEN` and
  `pipeline_viewer.slack.channel`; without them the message goes to the
  viewer's `slack-outbox.md`). It never starts a second epic: before each run
  it waits for a running epic and for any child build or fixer an earlier run
  left alive. Every decision is logged in `<epic dir>/watch.log`, and a loop
  stopped while waiting for a usage reset waits until that time when started
  again.
- **pipeline-viewer: `post`.** `python -m pipeline_viewer post --repo <repo>
  --change <name> --key <key>` posts the message on stdin to the viewer's
  Slack channel once per change and key, or writes it to `slack-outbox.md`
  without a token or channel. `epic watch` posts through it.

## [0.147.0] - 2026-10-29

### Added

- **buildme: `epic status`.** `python -m build_pipeline epic status --change
  <parent> --project-dir <project>` prints one JSON line per child of an
  epic: its state, the head of its worktree, the branch its PR targets, its
  PR, what blocks it, what the next epic run or a person does next, and its
  spend, known and unknown. It reads only what is on disk (and `ps`), launches
  nothing, writes nothing and never takes the epic's lock, so it is safe to
  run while the epic runs. A child whose build is still running shows as
  `running`; one whose process stopped with no result and that nothing runs
  shows as `interrupted`. Read it instead of tailing the logs.
- **buildme: the epic moves the children's Plane cards, through an outbox.**
  With `epic.board: plane` in `specs/config.yaml`, each column a child's build
  moves to (`BOARD:<change>:<Column>`) is saved to
  `<epic dir>/board-outbox.jsonl` before it is sent through the plane skill to
  the child's own ticket (`ticket_refs.epic`). A send that fails, or is cut
  off, is sent again from the file by the next drain or the next epic run, in
  order, and never rebuilds anything; a card already in that column is marked
  sent without a second move. `epic.plane_script` points at the plane skill's
  `plane.py` when multiplai-pm is not installed beside multiplai-dev. The
  default, `epic.board: off`, sends nothing, as before.

## [0.146.0] - 2026-10-29

### Added

- buildme: a new `tdd.context` setting in `specs/config.yaml` chooses what
  each block's test writer, implementer and refactorer read. `full`, the
  default, gives them what they get today, byte for byte. `sliced` gives them
  only the design sections the block's name or tasks cite, the requirements
  its `Satisfies:` line names, no rubric (the block review still reads it),
  and only the memory files listed under the new `memory_files.tdd` key, to
  make each prompt smaller. A block
  that cites nothing still gets the whole design, and one whose `Satisfies:`
  line names nothing still gets every requirement. Changing the setting
  between a stop and a resume does not mark the plan stale; the resumed block
  uses the new value.

## [0.145.0] - 2026-10-29

### Added
- **buildme**: `planning.analysis: component` in `specs/config.yaml` puts only part of the codebase analysis in the design prompt: the facts about the files the component's `plan.md` and specs name, and every convention. The "Not analysed" list is left out. The default, `full`, puts the whole analysis in the prompt as before, byte for byte. Measured offline on account-model's inputs (DFACCMGMT-1), the design prompt drops from 584,290 to 108,558 characters. Its plan and specs name no file, so it keeps only the 138 conventions of 1,393 facts. The writer can still open any file with its tools. Changing the setting does not regenerate a design already written.

## [0.144.0] - 2026-10-29

### Added
- **buildme**: `epic.shared_analysis: true` in `specs/config.yaml` makes an epic run the codebase analysis once, at the feature branch, before its first child starts, instead of once per child. Each child reuses it: facts about files that changed since are checked again against the child's code and dropped when their quoted lines moved, and only the files the child's `plan.md` names that the shared analysis did not read are analysed in the child. The shared analysis is written to `codebase-analysis/` in the epic's directory and its spend counts against `epic.max_usd`. A run stopped part way through is analysed again from the start before any child starts. Off by default: each child runs its own analysis, as before. Changing the setting does not change any plan revision.

## [0.143.0] - 2026-10-29

### Added

- **buildme can test security and permission scenarios from the spec alone**
  (`tdd.spec_tests`, off by default). With it on, before a block's test
  writer, a second test writer gets only the block's scenarios marked
  security or permission and the public interfaces they touch, not the
  design, so its tests check what the spec says rather than what the design
  chose. A scenario is marked when the word "security" or "permission" is in
  its `#### Scenario:` heading or in its `### Requirement:` heading (for
  example `[security]`). The block's test writer is told those test files
  and writes the rest; one test commit holds both, and they go through the
  same quality and RED gates. Its spend is recorded as `spec_test_writer`.
  With the setting off, every prompt and step is the same as before.

## [0.142.0] - 2026-10-29

### Added
- **buildme**: `code_review.component_review: true` in `specs/config.yaml` reviews the whole component once, after the last block and the whole-suite run and before the PR. The reviewer reads the component's diff from its base with every requirement, the implementation plan, the rubric and the findings the block reviews left open; a component whose changed paths or specs name auth, roles or tenants also gets a fixed checklist (who can read each new field, how entities are matched, what each migration does on the historical schema). The accepted Critical and Major findings get one fix round, kept only if the whole test command stays green and the test files are unchanged. Findings still open go to the PR body, and to `spec-issues.md` when the fix agent says only a requirements change fixes them. The review is saved to `reviews/component-review.json`; a stopped build resumes the fix round from it without a new review, and a build stopped after the fix commit runs the suite to decide whether to keep the fix.
- **buildme**: with the component review on, the per-block review fails a block only on findings rated `code_review.block_gate_severities` (default `[Critical, Major]`) or a spec-compliance miss, in at most 2 reviews, and a passing block gets no fix on pass: its lower-rated findings go to the component review.

Both settings are off by default: with `component_review` unset or false, the per-block review, its prompts and its records are exactly as before this release, and `block_gate_severities` is ignored. Findings rounds (`/multiplai-dev:buildme-findings`) never run the component review.

## [0.141.0] - 2026-10-29

### Added

- buildme: a component's design and tasks writers and its plan check now read
  the requirements of the components it depends on. The `specs.md` of every
  component in its `depends_on` in `.change.yaml`, and of their dependencies in
  turn, is added to those prompts under `## Requirements of the components this
  one depends on`, up to 60,000 characters; a file left out is named with its
  requirement names. A component with no dependency gets the same prompts as
  before.
- buildme: an edit to the `specs.md` of a component another one depends on
  (directly or in turn) now marks the dependent component's plan stale, as an
  edit to `specs/registry/` already does: its design, unknowns, tasks and
  rubric are planned again. After the upgrade, the plan of each component that
  has a dependency gets a new revision once, so it is planned again on its next
  run; components with no dependency keep their plan revision.

## [0.140.0] - 2026-10-29

### Added
- **review**: `review --dimensions diff-bugs,callers` chooses the dimension finders for one run, over `review.yaml`'s `dimensions`, and `review --finder-model <model>` chooses the finders' model, over `--model`, `review.yaml` and `multiplai.conf`. An unknown dimension name exits `2`.
- **buildme**: `epic.review_dimensions` and `epic.review_finder_model` in `specs/config.yaml` set the dimension finders and their model for each review of an epic child's PR. Unset, the review skill's own settings decide, as before.

### Changed
- **review**: `resume` now keeps the finder model the stopped run started with, as it already kept its dimensions, even when `review.yaml`, `multiplai.conf` or the resume's own `--model` say otherwise. It prints a `NOTICE:` when they differ. `--model` on `resume` still sets the verifier and merger models. A review of a new head uses the current settings.
- **review**: before any finder runs, a piece whose diff changes more than 12 definitions that existed before the change is cut in two for the `callers` finder alone, instead of waiting for that finder to hit the 60-turn limit. The other finders still read the whole piece, and `resume` uses the pieces the run saved. The threshold comes from the callers finders of two reviews that ran while finders could not search, so it will be checked again once they can.

## [0.139.0] - 2026-10-29

### Added

- review: `--verify-low risky` (on `review`, `batch` and `resume`) or
  `verify_low: risky` in `review.yaml` verifies only the LOW findings whose
  failure scenario ends in a crash or an error response, an access or
  data-exposure problem, or lost or wrong data. One call with no tools picks
  them from each LOW finding's claim and scenario; the rest stay unverified.
  The choice is saved before any verifier runs, so `resume` does not ask
  again. `true` and `false` work as before.
- buildme: the epic's PR reviews pass `--verify-low risky`, so a risky LOW
  finding is checked by a verifier instead of being left unverified.

### Changed

- review: a finder note that says it checked something and found no problem
  no longer becomes a finding. The convert step marks such a note as advice
  with no location, and a defect whose claim says no problem was found is
  dropped and listed in the review's errors.
- review: the merge step gives one agent at most 40 findings. A larger group
  of overlapping findings (one held 120) is split by file, then in line
  order.

## [0.138.0] - 2026-10-29

### Changed
- **review**: `review-<slug>.md` now opens with one list of every listed finding, the ones rated `breaks-users` or `breaks-business` first, then by the other impacts and by severity. Each line gives the severity, the impact, whether the change introduced the defect or it was already there, the section when it is not Code, any earlier decision, and a link to the finding in full. The commit and file lists moved from the header to `## Commits and files` near the end; the header keeps their counts.
- **review**: the LOW findings no verifier checked are now in their own file, `review-<slug>-low.md`, and the review's last section, **Not verified (LOW)**, links to it. They are still counted in their sections and named in the opening list. An unverifiable MEDIUM stays in the main review.
- **review**: a citation whose lines sit inside another citation of the same file in the same finding is no longer printed twice in the markdown. `findings.json` keeps every citation.

### Added
- **review**: a finding the finder marked `Pre-existing: yes` has `pre_existing: true` in `findings.json` and a **Pre-existing** line in the review, and keeps the finder's kind as its dimension. A review saved by an earlier version, which marked such a finding with the dimension `pre-existing`, loads with `pre_existing: true`. A pre-existing `tests` finding that no verifier checked is now listed under Tests rather than Code.
- **review-viewer**: `findings.json` v1 accepts the optional `pre_existing` field.

## [0.137.0] - 2026-10-29

### Added

- **review**: `--earlier-decisions <file>` on `review` and `resume` takes a
  JSON list of findings an earlier reviewer of the same change rejected or
  accepted and left unfixed. A finding of this round that describes one of
  them stays listed and blocking, is not labelled a repeat, and carries
  `earlier_decision: {id, decision, reason}` in `findings.json` and an
  **Earlier decision:** line in the review. A file of the wrong shape stops
  the run with exit 2. A resumed review keeps the matches it already saved.
- **buildme**: the review of a PR buildme opened, from the epic and from
  `findings`, now gets `--earlier-decisions` with every finding its per-block
  reviews rejected (with the judge's reason) and every accepted finding still
  open, so the PR review shows which of its findings buildme already decided
  on. The file is written under the multiplai data directory, never in the
  repository, and is passed only when it has an entry.
- **review-viewer**: `findings.json` v1 accepts the optional
  `earlier_decision` object on a finding; the page does not show it yet.

## [0.136.0] - 2026-10-29

### Added
- **buildme**: before research, planning or TDD calls a model, a build now checks with no model that `specs/config.yaml` parses, the test command's program (and each `tdd.narrow_commands` entry's) exists, the PR's target branch is on origin and `gh` can open a PR there (only when the run publishes), the test database answers (new `tdd.db_check`, a command; unset means not checked), and no other process is running the component's epic. Each failed check prints one `PREFLIGHT:FAILED:<check>:<reason>` line and the build exits 1 before spending anything.

### Changed
- **buildme**: every test-command run runs in a process group of its own and streams its output to `<change dir>/.buildme-test-runs/`. A timeout or a stopped build kills the whole group, so no test process outlives the run, and keeps the partial output. The build log gets a `TEST_RUN end ... seconds=<s>` line for each run, which gives the time spent in tests.
- **buildme**: a test run where the runner never got going (it was killed or crashed, the test database is down, it printed nothing) is run again after 10 and 30 seconds instead of being treated as failing tests. A conftest or test file that fails to import or collect (pytest exit 4, or a collection or import error in the output) is not run again: it goes to the fix agent as a failing test, as before. If the third run fails the same way, the block stops with the three saved outputs and no fix agent is spent on it; re-running the build runs the tests again. A test that fails is never run again.

## [0.135.0] - 2026-10-29

### Added
- **buildme** (G20): `epic.accept_size: true` in `specs/config.yaml` makes the epic pass `--accept-size` to every child it launches, and launches again a child that an earlier run left parked on `SPLIT:REQUIRED`. Off by default.
- **buildme** (G22): `epic.max_child_attempts` (default `2`): a child whose build fails is launched again in the same run, up to that many launches, instead of once per run. A child whose planning check is blocked at its plan's current revision is not launched again until its plan changes, because its next build would refuse the same way. Any other failed child is launched again by the next run.

### Changed
- **buildme** (G35): before the epic parks or fails a child, it looks for an open PR from the child's branch into the feature branch, such as one a build run by hand opened. With one, the child counts as built with that PR and its dependants can start; its records are left as they were. A child parked by its review stays parked. When the lookup fails, the child waits, unlaunched, until a lookup works.
- **buildme** (G34): `build --change <epic>/<component>` run by hand builds as the epic's child, on the epic's feature branch, so its PR targets the feature branch instead of the default branch. A different `--branch` is refused with exit 2 and says which branch to use.
- **buildme**: the epic's spend, and so `epic.max_usd`, now also counts what each child's build checkpoint holds beyond the epic's own build records, such as builds run by hand. On the DFACCMGMT-1 state of 2026-10-29 it counts $551.65 where it counted $399.
- **buildme** (G33): a build resumed after a block's test and implementation were committed goes on from the first step with no commit, the refactor or the review. It no longer runs the quality and RED gates again on code that is already implemented, which made the RED gate fail.
- **buildme**: `facts.json`, `codebase-analysis.md` and `plan-edits-*.diff` in a change dir are no longer committed, so they stay off the PR's branch. They stay on disk for the build. A branch that already committed them keeps them as they are, with no delete commit.
- **buildme** (G25): editing the `findings:`, `code_review:` or `test_quality:` sections of `specs/config.yaml` no longer changes the plan revision, so it does not make a planned build plan again. `memory_files:` still does, because its `stacks` picks the reference docs that planning reads. A build planned by an earlier version whose `config.yaml` has one of those three sections sees its plan revision change once; `plan-revision` re-binds it.

## [0.134.0] - 2026-10-29

### Changed
- **review**: a review stopped by the Claude usage limit or by a verifier that gave no usable answer is no longer started over by a plain `review` of the same target and head. That `review` now makes no model call, prints `interrupted run found; continue with: python -m review_pipeline resume <dir>` and exits 4, as it already did for a killed run, so the answers paid for before the stop are not paid for again. buildme's epic already runs that `resume` when it sees the line. `--restart` still starts over, a budget stop still lets a plain `review` start over, and a review of a head the stopped run was not on starts over as before.

## [0.133.0] - 2026-10-29

### Fixed
- **buildme** epic: the fixer now acts on the epic review's own findings. The review posts them as a GitHub App, which `findings.authors` does not allow, so on 2026-10-29 the fixer acted on none of them four times. The epic now passes `findings --from-review <review folder>`: the fixer reads the blockers from the review's `findings.json`, acts on the comment posting each one whoever wrote it, and still replies under each comment. Comments from people are still allowed by `findings.authors` alone.
- **buildme** epic: an open confirmed finding no longer merges on its own. Under `epic.merge: auto`, a PR at `epic.max_review_rounds` with an open Code HIGH, or a confirmed Code MEDIUM not rated `hygiene`, parks with the finding ids instead of getting the final fix and merging. A MEDIUM rated `correctness-only` is no longer treated as harmless: under `epic.round_worth: breaks` it now earns another review round. PR #39 merged with two such findings.

### Added
- **buildme-findings**: `--from-review <review folder>` records one decision for each blocker in `.findings-state.json`: fixed (with its commit), not a defect (with the reason), spec change (with the `spec-issues.md` heading) or still open (with why). A blocker left with no decision ends the run with `FINDINGS:INGESTION_ERROR:<ids>` and exit 1; the epic marks that child `failed` and launches its fixer once more on the next run. A newer review that replaced the folder while a round was open prints `FINDINGS:REVIEW_CHANGED:<old head>→<new head>`.
- **buildme** epic: `epic.accepted_findings` in `specs/config.yaml` (`{<child id>: {'<finding id>': <reason>}}`) lists findings a person accepted. A listed finding does not block that child; a child parked on findings goes to the merge step once every one is listed, and stays parked otherwise until its PR's head changes.
- **buildme**: the review impact `depends-on-data` (added to the review skill separately) counts as critical in `--mode critical`, as the review skill counts it.

## [0.132.0] - 2026-10-29

### Changed
- **review**: the verifier now gives each finding a severity (HIGH, MEDIUM or LOW) for the consequence it checked, using the same severity guide the finders use. Once every finding has a verdict, the review sets each confirmed or unverifiable finding's severity from it, so a severity can now go up as well as down. Two rules hold it to the verifier's impact: a `hygiene` finding is LOW, and a `breaks-users`, `breaks-business` or `depends-on-data` finding is at least MEDIUM. An unverifiable finding is then lowered one step as before. No verdict changes, and no prompt is told to leave anything out. The review shows the finder's severity beside a changed one, as "raised from" or "lowered from". A review stopped and resumed sets each severity once. A verdict saved by an older version has no severity, so its finding keeps the finder's severity, with the two rules applied. `answers` sets the severity the same way after a new verdict.

### Added
- **review**: a new impact, `depends-on-data`. The verifier uses it when default settings reach the code and the harm happens only if production holds data it cannot read, such as duplicate names or a login with two roles, and it names that data in its reason. `--mode critical` lists these findings, and a finding about tests rated this way is lowered to `correctness-only`, as `breaks-*` ones are.
- **review-viewer**: shows the `depends-on-data` impact as a badge, describes it in the help, and lists it under **Critical only**. `findings.json` v1 accepts it.

## [0.131.0] - 2026-10-29

### Fixed

- The review skill's agents can search the code again. Each review copies the
  reviewed commit into a folder under the data directory, and that directory is
  git-ignored, so ripgrep, which the agents' Grep runs on, skipped every file in
  it: in three PR reviews on 2026-10-29, 76 of 102 finder reports say a search
  returned nothing. The copy, and the copy of base files the explain stage
  reads, is now made a git repository of its own (`git init`), which stops
  ripgrep from applying the ignore files above it; the reviewed repository's own
  `.gitignore` still applies. A copy left by an older version gets the `git init`
  on its next run, without being extracted again.
- A review now fails at the start, naming the folder and the ignore file that
  hides it, when ripgrep lists no file in the copy while the change has files.
  Without ripgrep installed it writes one line to `progress.log` and goes on.

### Added

- `round-metrics --counts` also prints, per round, the confirmed findings by
  severity, impact and topic, the number of finder reports, and how many of them
  say a search returned nothing, with a totals line. It still writes nothing and
  calls no model.

## [0.130.0] - 2026-10-29

### Added

- buildme ships `evals/epic_metrics.py`, which reads an epic's records, build
  states, review folders, cost ledger, agent transcripts and git history and
  prints each child's spend (planning, TDD per block, PR review, fix and
  merge), the findings its fixer acted on, confirmed review findings, empty
  finder searches, block-review rounds after a pass, prompt sizes, cache-write
  shares, planning lines on the PR and hand interventions. It calls no model.
  `--self-check` checks its counting on a built-in fixture.

## [0.129.0] - 2026-10-29

### Added
- **buildme**: `tdd.whole_suite: end` in `specs/config.yaml` runs the whole `tdd.test_command` once after the last block instead of several times in every block. The block gates (the integration gate and the runs after each integration fix, review fix and fix on pass) and the whole-change refactor's checks run only the tests of what changed. When the end run is red, buildme first discards the whole-change refactor commit if it is the last commit, then makes up to three fix attempts. If the suite is still red, buildme puts the tree back, saves the fixers' diff and the last run's output, and fails the build with both paths in `build-progress.md`. A stopped end run resumes, but never over uncommitted edits it cannot tell from yours. The default, `per_block`, runs buildme's whole-suite checks as before. Trade-off: a block that breaks another block's tests is caught only at the end.

### Changed
- **buildme** (G23): every failed whole test run (the baseline, a block gate, the whole-change refactor, the end run, the final verification) writes its command, exit code, failing test ids and last 400 lines of output to `<change dir>/.buildme-test-runs/<UTC stamp>-<label>.log`, and the log line and `build-progress.md` name that file. buildme never commits or cleans that folder.
- **buildme** (G31): no agent is told to run the whole test suite. The integration and review fixers, the fix on pass, the whole-change refactorer and the mutation test writer get a narrowed test command instead of the whole one; the integration fixer also gets the failing tests by name. When a command cannot be narrowed, the agent is told not to run it and to run only the test files it wrote or changed.
- **buildme** (G32): the weak-test scan reads a block's Jest test files and counts their `it(`/`test(` calls, so a Jest-only block no longer fails with "No test functions found". The test-quality auditor's verdict and its reason go to `build-progress.md`.

## [0.128.0] - 2026-10-28

### Changed
- **buildme**: the check after each block's refactor runs only the block's own tests, not the whole `tdd.test_command`. The integration gate runs the whole suite on the same tree right after, so the second whole run caught nothing the gate does not; it cost one whole-suite run per block. When the integration gate is red, buildme now discards the block's refactor commit and runs the gate again before starting a fix agent. A refactor that breaks a test outside the block is still discarded, not fixed.

## [0.127.0] - 2026-10-28

### Added
- **buildme**: with `review.plan_approval: true` in `specs/config.yaml`, a build stops after its plan check and before any code is written, until an engineer approves the implementation plan. It writes `plan-approval.md` (the plan to approve, `tasks.md` to read, the plan check's outcome and revision), prints `PLAN:WAITING:<path>` and exits 3. The approval is a commit on the build's branch with `Stop: 4`, `Revision: <plan revision>` and `Approved-by:` (or `Decided-by:`) trailers; `--approve-plan` makes it. An edited plan needs a new approval. In an epic, a component waiting for approval is recorded as `waiting` and relaunched on the next epic run. Off by default; turning it on does not change the plan revision.
- **buildme**: a `pipeline_viewer:` section in `specs/config.yaml` (pipeline-viewer's settings) no longer changes the plan revision, so adding it does not regenerate the plans of builds in flight.

### Changed
- **buildme**: the implementation plan is now written as `implementation-plan.md` instead of `design.md`. A change planned before this version keeps its `design.md`: buildme reads it, keeps the same plan revision, and does not regenerate it. With both files, `implementation-plan.md` is used. buildme never renames or removes `design.md`.
- **pr**: quotes `implementation-plan.md` as evidence, and `design.md` only when the new file is absent.

## [0.126.0] - 2026-10-28

### Added
- **pipeline-viewer** (new skill): a web page, served from the machine running pm-spec and buildme and shareable through a tunnel such as ngrok, where the people on a change follow it and answer it. It lists every change under `specs/changes/` with the stop it is at (proposal, use cases and components, specs, implementation plan, pull request), who it waits on and for how long. A reviewer goes through each stop's documents one `##` section at a time, in full; comments, asks Claude about a section, and approves or asks for rework at the end. Everyone on the page sees every question and Claude's answer. Each comment, acknowledgement, approval, rework request and answer is a commit on the change's branch, named after the person (`--author`) with `Stop:`/`Approved-by:`/`Revision:` trailers. The build view shows the phase, each block's status, review rounds, timeouts, RED/GREEN output and diffs, spend against budget, and an estimate of the time left.
- **pipeline-viewer**: the Claude session reads what people type on the page only through `watch` (its Monitor) and `pending`, which print each row with the person's text inside an `<untrusted-content>` fence, never raw.
- **pipeline-viewer**: while buildme is at work in a build's worktree, the page refuses comments and decisions on that change (`buildme is building <name>; try again when it stops, or comment on the pull request`). Writes go through once the build completes, stops on a failure, or waits at stop 4. This is read from buildme's files, not a lock.
- **pipeline-viewer**: a change dir, `review/` dir or file that is a symlink is never read or written through, and text a person typed is quoted (`> `) in `review/stop-<n>.md` and `spec-issues.md`, so it cannot add a section in someone else's name. A commit that fails leaves nothing behind for the next person's commit to pick up.
- **pipeline-viewer**: each person gets a personal link (`invite`, written to a file, never printed); the page knows who is acting from it.
- **pipeline-viewer**: `notify` posts once per stop in a Slack channel (`pipeline_viewer.slack.channel` in `specs/config.yaml`; needs `SLACK_TOKEN`), @-mentions the stop's reviewers and replies in that thread with acknowledgements, approvals and reminders. Without someone acknowledging a stop within `ack_timeout_minutes`, and approving within `decide_timeout_minutes`, the session is told to decide and go on; a later answer that disagrees becomes rework.

### Changed
- **review-viewer**: its mailbox, network and request checks moved to a shared library (`lib/viewer_kit`) that pipeline-viewer also uses. Nothing changes for a review-viewer user.

## [0.125.0] - 2026-10-28

### Added
- **buildme**: `tdd.narrow_commands` in `specs/config.yaml` lists one runner command per test directory, for a repo with a pytest backend and a jest frontend behind one `tdd.test_command`. Inside a block, buildme and its agents now run only the block's own tests and the tests named after its changed modules, each in its own directory (`cd backend && pytest -q tests/test_x.py`). A directory with no changed file is left out. The baseline, integration, final-verification and epic merge runs still use the whole `tdd.test_command`. An optional `runner: pytest` or `runner: jest` lets a wrapper script take the test paths.
- **buildme**: each block phase prints a `TDD: <change>/<component> block=N/M phase=<PHASE> START|DONE|FAIL elapsed=<s>` line on stdout and in `build-progress.md`. The daily log's records now carry `[change:<name>]`, so two builds started from one session can be told apart.
- **buildme**: `python -m build_pipeline plan-revision --change … --project-dir …` prints the planning values a build planned by an older version needs in its `.build-state.json` to resume after this upgrade. It writes nothing.

### Changed
- **buildme**: editing `tdd:`, `epic:` or `git:` in `specs/config.yaml` no longer changes the plan revision, so it no longer stops a build that is already in TDD. A build planned by 0.124.0 or earlier stops once on resume with `Planning inputs changed after implementation`; SKILL.md ("Re-binding a running build after an upgrade") says which values to write.
- **buildme**: a pytest config whose `python_files` lists only `test_*.py` and/or `*_test.py`, or whose `testpaths` is `.`, no longer stops the block's tests from being narrowed. Before, the first made buildme run the whole suite, and the second made narrowing crash.

## [0.124.0] - 2026-10-28

### Added
- **review**: `python -m review_pipeline answers <review dir>` applies the outputs you saved for the review's commands. Save each command's output as `answers/N1.txt`, `answers/N2.txt`, … in the review's directory; `answers` works out which findings each output bears on, runs the verifier again on only those findings with the output as evidence, rewrites the text of those whose verdict changed, and writes `findings.json` and the review again. Find does not run again, finding ids stay the same, and your decisions in the viewer still apply. It prints what changed (`N2 → <id> unverifiable→confirmed`) and what it cost. Running it again after a stop continues where it stopped.
- **review-viewer**: a page already open picks up a changed `findings.json`, such as the one `answers` writes, without a restart.

### Changed
- **review**: **Needs you** lists one row per command instead of one per need, with an id (`N1`, `N2`, …), the command, one line of why, and the findings it bears on. The same command asked for by several agents shows once. The review's terminal output ends with this list and the `answers` command line.
- **review**: what the review could not check and names no command for is no longer in Needs you. It shows on the finding it weakens as **Not checked:**, or under **Not checked** in the review when it concerns the review as a whole.
- **review**: the finders and the verifier now give a command whenever a read-only `gh`, `gcloud`, `git`, `curl` or other command-line tool can get the fact (`curl -sL <url>` for a web page, `gh api repos/<owner>/<repo>/contents/<path>` for a file in another GitHub repository).
- **review-viewer**: the Needs you tab shows one row per command with the file to save its output in. The **Ask the session** button is gone.

## [0.123.0] - 2026-10-28

### Added
- **buildme**: `carry_block_notes: true` in `specs/config.yaml` makes each block's implementer and review-fix agents write notes for later blocks (conventions chosen, helpers found, behaviour of the existing code the spec does not mention). Every later block's implementer and review-fix agents get those notes; the test writer and refactorer do not. Off by default: with it off, every prompt is unchanged. Turn it on only after the notes-forward benchmark shows it helps.
- **buildme**: `evals/notes_forward_compare.py` runs buildme's TDD step on five multi-block fixtures with the option off and on, and reports acceptance, cost, implementer turns and context size per block. `--self-check` checks the fixtures offline.

## [0.122.0] - 2026-10-27

### Added
- **buildme**: `tdd.test_timeout` in `specs/config.yaml` sets how many seconds each full run of the test suite gets: the baseline before TDD, the integration gates after each block, the final verification and an epic's merge tests. It is 300 when unset, as before. A suite that takes longer than 300 seconds no longer counts as not run on every build.

## [0.121.0] - 2026-10-27

### Changed
- **buildme**: the writer of `design.md` and `tasks.md` now reads the project's code with `Read`, `Grep` and `Glob` before describing it, and checks each name, endpoint or table it writes about. It still cannot change any file. Spec generation therefore needs `--trust-repo` (or `BUILDME_TRUST_REPO=1`), as the plan check already did.

## [0.120.0] - 2026-10-27

### Added
- **buildme**: `planning.max_rewrites` (default `2`) and `planning.max_usd` (default `40`) in `specs/config.yaml`. The plan check stops the build after that many rewrites, or once the planning calls have spent that much. Raising either one and resuming gives a stopped check its rewrites again and regenerates nothing.
- **buildme**: each plan rewrite's changes are saved as `plan-edits-<n>.diff` in the change dir.

### Changed
- **buildme**: a plan rewrite now edits `design.md` and `tasks.md` in place instead of writing them again. Lines no finding names stay as they were. An edit whose text is not found exactly once, or edits that change nothing, stop the build with a blocking finding.
- **buildme**: the recheck after a rewrite is shown the lines the rewrite changed. Findings that still block get another rewrite, up to `planning.max_rewrites`; the check stops early when a rewrite leaves as many blocking findings as the one before.
- **buildme**: the explainer calls are now counted under their own `explainer` label in the budget summary.

## [0.119.0] - 2026-10-27

### Changed
- **buildme**: planning runs one check (`plan_review`) instead of three. The design audit, the tasks audit and the plan review read the same documents for overlapping problems; the one check reads `design.md` and `tasks.md` once against the specs, the use cases, the rubric, the constraints and the code, and covers what all three covered. A clean plan now costs one model call to check, not three.
- **buildme**: a plan-check finding whose fix needs a requirements change stops the build with exit 4 and a `spec-issues.md` section titled `Plan review: <requirement>`, as the design audit's did.
- **buildme**: a rewrite changes `design.md` only when a finding is about it; when every finding is in `tasks.md`, only `tasks.md` (and the rubric) is rewritten.
- **buildme**: a build saved by an earlier version drops its design-audit and tasks-audit steps when you resume it. If its plan review had passed at the current plan, it is not run again; otherwise the one check runs fresh.

### Removed
- **buildme**: the `models:` and `efforts:` keys `design_audit` and `tasks_audit` in `specs/config.yaml` no longer have any effect. buildme logs a warning naming `plan_review`, the key that applies to the one check.
- **buildme**: the `DESIGN_AUDIT_ADVISORY` and `TASKS_AUDIT_ADVISORY` / `TASKS_AUDIT_BLOCKED` progress lines; the one check writes `PLAN_REVIEW_ADVISORY` and `PLAN_REVIEW_BLOCKED`. The `DESIGN_AUDIT` phase is still marked complete, so the board keeps its column.

## [0.117.0] - 2026-10-27

### Changed
- **buildme**: a minor finding from the design audit, the tasks audit or the plan review no longer stops a build or earns a rewrite, whatever its category. Only critical and major findings earn the rewrite. A block that adds no behaviour still stops the build when buildme's own scan finds it, or when the model rates it critical, which the tasks audit's prompt now tells it to do.
- **buildme**: after the rewrite, a major finding stops the build only when the recheck says it is `still_present`. A major finding the recheck calls `partially_resolved` or `cannot_verify` is reported in the progress file (`<STEP>_ADVISORY`) and the build goes on. A critical finding still stops the build unless it is `resolved`.
- **buildme**: each of the three planning checks reports at most 10 findings, most severe first, and reports an edge case only for an input or failure a spec scenario or the plan names. The log line `<check>: kept 10 of <n> findings` says when buildme dropped some.
- **buildme**: a build that an earlier version stopped in planning gets one fresh check and one rewrite under these rules when you resume it, without editing the plan.

## [0.116.0] - 2026-10-27

### Added
- **buildme**: you choose where the code reviewer's standards and the spec writer's reference docs come from, per run, per repo or per machine. Three options: `standards_files` (the reviewer's docs), `reference_docs` (the spec writer's docs) and the new `reference_dirs` (the directories a doc name is looked up in, instead of `$CLAUDE_CONFIG_DIR/reference/dev/`). Each is read from, highest first: the environment (`BUILDME_STANDARDS_FILES`, `BUILDME_REFERENCE_DOCS`, `BUILDME_REFERENCE_DIRS`, lists separated by `:`), `specs/config.yaml`, the multiplai-dev plugin options `standards_files` and `reference_dirs`, and `[buildme] STANDARDS_FILES=` / `REFERENCE_DIRS=` in `multiplai.conf`. The highest one that sets an option replaces the lower ones; lists are not merged.
- **buildme**: a `reference_docs:` entry in `specs/config.yaml` can be a path in the repo (`docs/standards/testing.md`), so a repo's own standards reach the spec writer on every machine, not only on machines that have the doc under `reference/dev/`.
- **buildme**: a `STANDARDS(<source>):<docs>` progress line names the reviewer's standards docs and where the list came from.

### Changed
- **buildme**: the `REFERENCES:` progress line is now `REFERENCES(<source>):<docs>`, where `<source>` is `env`, `config`, `plugin`, `conf` or `built-in`. Anything that matches the line on `REFERENCES:` must match `REFERENCES(` instead.
- **buildme**: a reference doc that you named (in the environment or `specs/config.yaml`) and that does not exist is a warning. A doc from the built-in list that does not exist is still only an INFO line.

### Fixed
- **buildme**: SKILL.md said `standards_files` docs are not capped. Each one is capped at 12,000 characters and reduced on section boundaries, like the `CLAUDE.md` files the reviewer reads.

## [0.115.0] - 2026-10-27

### Changed
- **review**: a second review of a PR or branch now reviews only the files whose patch changed since the last finished round. A rebase that keeps the patch counts as no change. Findings that round left open on the other files are carried over unchanged, with their ids, verdicts and decisions, and cost no agent call. An earlier finding whose file left the change is marked resolved. When nothing changed, the run makes no finder call and exports the carried findings. The review, its summary and review-viewer's Summary tab say "Reviewed N of M files; K findings carried from round N". Pass `--full`, or set `incremental: false` in `review.yaml`, to review every file. A rerun of the same head still reviews every file.

## [0.114.0] - 2026-10-27

### Added
- **review-viewer**: an **Export zip** button saves one zip of a saved review: its files, earlier rounds, decisions and questions, and the transcripts of the Claude Code sessions that ran it, with a `MANIFEST.txt`. The server runs `gitleaks` over everything first and refuses the export when it finds anything, listing each file, line and rule but never the value. There is no override. Export needs `gitleaks` installed (`brew install gitleaks`). The zip never holds the code snapshot or the viewer's token files.

## [0.113.0] - 2026-10-27

### Added
- **review**: a new `write` stage after assess restates each shown finding in plain words: a title of at most 12 words and a short "what happens" text with a concrete example. The review and review-viewer head each finding with its title and show what happens first, with the finder's own claim under it. Each answer is checked in code (length, no file:line, no invented names, no filler phrases); one that fails twice is dropped and the claim is shown instead. The stage costs one call per 10 findings of a file, with no tools, on the assess stage's model.
- **review-viewer**: `findings.json` gains the optional `title` and `what_happens` fields. The copied markdown of a finding uses them too.

## [0.112.0] - 2026-10-27

### Changed
- `/multiplai-dev:review` no longer sends LOW findings to a verifier. A
  finding the finder rated LOW reaches the review with the new status
  `unverified` and the finder's own text, in a last section **Not verified
  (LOW)** of `review-<slug>.md`. HIGH and MEDIUM findings are verified as
  before, and so is a trim finding at any severity. On two Dolcebot reviews
  most of the verify spend went to LOW findings no one acted on.
- `--verify-low` (on `review`, `batch` and `resume`) or `verify_low: true`
  in `review.yaml` verifies LOW findings as before.
- `/multiplai-dev:review-viewer` hides LOW findings until you press **Show N
  low findings** in the list. It remembers the choice in your browser. A LOW
  finding opened by a link or by Prev/Next still shows. The open-findings
  count on the Summary tab counts `unverified` findings like confirmed ones.
- `findings.json` may carry status `unverified`, and `run.counts.unverified`.
  Both are additive to schema v1. buildme's `--auto` posts accepted
  `unverified` findings like the review lists them.

## [0.111.0] - 2026-10-27

### Changed
- `/multiplai-dev:review` names itself and its version in the first line of
  `progress.log`, e.g. `STARTED multiplai-dev:review 0.111.0 <target> …`, so
  a log shows which release made the review.
- `review-state.json` keeps a `runs` list: the version, the Claude Code
  session id and the start time of every run of the round, review or
  resume. A resume adds an entry and never rewrites an earlier one.

## [0.110.0] - 2026-10-25

### Fixed
- `/multiplai-dev:buildme-findings` comes back to a code fix it deferred
  because the fix contradicted the spec. Once `/multiplai-pm:pm-spec revise`
  has cleared the spec issue and recorded a new spec revision, the next
  findings run triages that comment again and, if it is still a code fix,
  fixes it and replies in its thread. It does this once per comment; a
  later revision does not retry it again.

## [0.109.0] - 2026-10-25

### Fixed
- An epic `/buildme` refuses a child with an open spec issue before it
  launches anything, with the same `SPEC:ISSUES:<ref>:<path>` line and exit
  `4` a single build gives, instead of launching the child and relaunching it.
- A child whose own build refuses its specs (`SPEC:NOT_READY` or
  `SPEC:ISSUES`) is parked with that line as its reason, not reported as
  `failed: exit 2` and relaunched on every run. The rest of the epic keeps
  building; children that depend on it wait on it. Once `pm-spec revise` has
  made its specs ready again, the next run builds it.
- A child that merged and was archived on the feature branch is never
  launched again, even when the epic's records of it are gone.
- `spec-issues.md` has one writer, so the build and `buildme-findings` no
  longer give it two different headers or write the same issue twice. A file
  an older `buildme-findings` started keeps its issues; only its header line
  changes.
- `--skip-explainers --help` now says the explainer pass is off by default.

## [0.108.0] - 2026-10-24

### Changed
- Skills set `effort:` in their frontmatter again (still no `model:`), so
  each runs at an effort that fits its work rather than whatever the
  session happens to be set to. The level applies only while the skill
  runs; `CLAUDE_CODE_EFFORT_LEVEL` still overrides it.
  - `medium`: analyze-context-router, buildme, buildme-findings, codebase-walkthrough, design-system, learn-stack, plan, pr, propose-skill, review-viewer, spec-extract
  - `high`: deepen, devops-gcp, e2e-test, review, skill-creator, think
- `review` runs at `high` until comparisons show whether `medium` finds as
  many bugs.

## [0.107.0] - 2026-10-24

### Changed
- A change review now runs its dimension finders (`diff-bugs`, `callers`,
  `history` and `conventions` by default) on every piece of the diff, each
  given its piece's whole diff. Before, each dimension read the whole diff in
  one call, cut at 120,000 characters, so on a large change it never saw the
  rest. `diff-bugs` takes over the broad review the piece finders did, and
  there is no separate piece finder any more. Each finder writes a report
  (`report-<dimension>-<id>.md`) that a second call turns into findings, as
  the piece finders' did. `--plan-only` prints the calls as
  `<P> pieces x <D> dimensions + up to 21 paths`.
- The `tests` and `trim` finders are off by default on a change review. Name
  them in `dimensions`, or set `trim: true`, to run them. On PR #295 they
  found no real bug the others missed and made the review about $25 dearer.
- A review costs more: about $14 per dimension on a 227 KB diff, so about
  $80 with the path finders before verification. The default
  `--max-cost-usd` is now 150 USD (was 50), and the cost line says how many
  dimensions each piece runs.
- Sixteen calls run at once by default (was four).

### Added
- `dimensions` in `review.yaml` chooses which dimension finders run, for
  example `dimensions: [diff-bugs, callers]`. An unknown name stops the
  review with the valid names. `resume` keeps the list the run started with.
- A finder that stops at the turn limit on a piece is asked again on two
  halves of that piece, in the same run, instead of losing the piece.
- `## Not reviewed`, near the top of the review and the summary, lists the
  files a dimension did not review because its calls failed, and why. The
  `DONE` line in `progress.log` ends `; <n> files not reviewed`.
- Before each model call the review checks how much of the Claude usage
  window is left (an internal endpoint, read with the login token in
  `.credentials.json`; the token goes into that request only). It holds a
  call back while the calls already running would use up the window, and
  stops with exit `6`, the reset time and the `resume` command when even one
  call does not fit. When the window cannot be read, it prints one `NOTICE:`
  and runs as before.
- A finder can mark a defect the change did not cause (`Pre-existing: yes`)
  and say what it could not check, which the review lists as a need.

### Fixed
- A fresh round now removes the last round's `report-<dimension>-*.md` files
  after keeping them, as it already did for the piece and path reports.

## [0.106.0] - 2026-10-24

### Added
- codebase-walkthrough names the part of the code that does its main job and
  gives it its own section after How it works, titled for that job (for
  example "How it finds bugs"): what it looks at, what it is given, what it
  produces and what checks it. One call with no tools chooses the part from
  the checked claims, and one read-only agent reads its files closely; a code
  check holds the choice to the target's files and a plain title. Module and
  codebase scope, human mode.
- The HTML page's open body is written by one call that sees every checked
  section and claim at once, so the page no longer repeats itself. A code
  check passes only allowed HTML tags and attributes, CSS that loads nothing
  and types no colour, and citations the pipeline checked; it counts the
  visible words against 3,600 and asks once more when the page fails. A page
  that still fails is drawn by code, as before. `render` rebuilds the page
  from the saved answer with no call.
- The page writer lays out its sections with the page's components: numbered
  stage cards with an icon for each kind of stage, a callout for the check on
  a stage's result, cards side by side and trap cards. Their colours come from
  the design system's tokens.
- `resume --repage` writes only the HTML page again, for the cost of the page
  call.
- The "How it works" picture on the HTML page is drawn by a model as SVG, in
  the style of a hand-drawn diagram: a colour and an icon for each kind of
  stage, stacked cards where many agents run at once, checks drawn small, and
  a dashed arrow back for a loop, with a wide layout for screens and a tall
  one for phones, in the page's light and dark themes. Code renders it to PNG
  and a model looks at it and corrects it, up to three times; code checks
  every version for allowed SVG only, colours from the design system and every
  stage labelled. When drawing fails, or `resvg-py` has no wheel for the
  platform, the page shows the boxes code draws. This adds two to five model
  calls to a run.
- `resume --redraw` draws only the page's picture again, keeping every other
  answer.
- The flow picture at the top of the page draws each stage as a box marked as
  an input, code, an agent, a check, a decision or an output, with stacked
  cards where many agents run at once, and each check drawn small between the
  boxes around it; How it works has one stage card per box, in the picture's
  order.

### Changed
- The walkthrough reads What it is (with the flow picture), How it works, the
  core part, What it creates, Configuration, Use cases, Gotchas. With
  `--scope codebase` the three tables, Reference and How this was made are
  closed fold-outs at the bottom of the page.
- Adjacent citations on the page are one superscript ("1, 2"), and the page
  shows the first citation under each heading only.
- The writers follow plain-language rules (no invented names, verbs over
  nouns, answer first, one idea per sentence), and a title or heading never
  holds a citation.
- `resume --rewrite` also asks the page writer again, and keeps the core
  stage's answers.

### Removed
- The "Which command to run" table of `--scope codebase`, and the survey agent
  that wrote it.

## [0.105.3] - 2026-10-24

### Removed
- buildme's epic run no longer passes `--extra-finders` when it reviews a
  child's PR. The review skill ignored the flag and printed
  `review: --extra-finders has no effect; per-piece finders now run by default`
  on every review; the review itself is unchanged.

## [0.105.2] - 2026-10-23

### Fixed
- buildme's RED gate accepts a test that fails through pytest's own
  `Failed`: `pytest.fail()`, and `pytest.raises` / `pytest.warns` that saw
  nothing (`DID NOT RAISE`). It also reads `--tb=line` and `-rf` output, where
  an assertion is printed as `path:line: assert ...` or
  `FAILED <id> - assert ...`. Such tests were rejected as having "no
  recognizable test-failure signature" and sent back to the test writer.
  A `pytest.fail()` in a fixture or at module level is still a setup or
  collection error, and NameError or TypeError from the test's own code is
  still not a right reason.
- A findings round whose fix block failed and was skipped no longer writes
  "All N blocks implemented successfully" to `build-progress.md`. The
  COMPLETE line now says how many blocks were implemented and names each
  failed, skipped block.
- The review buildme runs of its PR (the epic coordinator's and each
  `/multiplai-dev:buildme-findings` round's) reads only the code, tests,
  docs and config the change touches, and `specs/registry/`. It leaves out
  the change's `specs/changes/**` and `specs/archive/**` documents and
  buildme's runtime files, which made a 30-line change a 2,090-line review
  ($9.22 over 55 calls on the word-freq run).
- That review runs on the review model `specs/config.yaml` names
  (`models.review`, or `[buildme.review] MODEL=`), on `resume` too. It ran on
  the review skill's own setting, Opus on that run, although
  `models.review: sonnet` was set. With no review model named, the review
  skill's `review.yaml` or `multiplai.conf` still chooses.

### Added
- `/multiplai-dev:review --exclude <glob>` (repeatable, change reviews only)
  leaves the changed paths matching a git glob, read from the repository's
  top, out of the review; the review's header lists what it left out.
- `/multiplai-dev:review --model <model>` (on `review` and `resume`) runs
  every stage on one model for that run, over `review.yaml` and
  `multiplai.conf`.

## [0.105.1] - 2026-10-23

### Fixed
- A component's design audit and plan review judge it against its own
  `plan.md`. They read only the change's proposal and use cases, which cover
  every component, so the audit of one component reported its sibling's
  capability as missing (critical), wrote it to `spec-issues.md` and stopped
  the epic with exit 4. Both now get the component's `plan.md` and are told
  that what the plan leaves to another component is not a gap.
- Every pm-spec command buildme prints (in `spec-issues.md`, after a TDD
  contradiction, in the findings round and in the ticket-ref refusals) names
  the change, `revise word-freq`, never the component path
  `word-freq/word-frequencies`, which pm-spec refuses.

## [0.105.0] - 2026-10-23

### Added
- buildme builds a change whose components pm-spec 0.9.0 nests in it
  (`specs/changes/<change>/components/<id>/`, each with its own `plan.md`,
  `specs.md` and `.change.yaml`). `/buildme <change>` runs the epic
  coordinator over the components in their `depends_on` order: each builds
  as `--change <change>/<id>` on its own branch,
  `<component ticket>-<change>-<id>` (the change's ticket when the component
  has none yet, as for an epic's child before), and its PR merges into the
  change's feature branch. `--change <change>/<id>` also builds one
  component on its own.
- buildme reads a change's capabilities from the `# Capability: <name>`
  sections of `specs.md`. A component's proposal and use cases are its
  change's. The tasks audit, the review-findings triage, `spec-issues.md`'s
  requirements file (`specs.md#<capability>`) and the respec audit read
  `specs.md` too, not only `requirements/`. The design step and the design
  audit name a requirement they cannot follow as `specs.md#<capability>:
  <name>` in a change with `specs.md` (`requirements/<capability>.md:
  <name>` in the old layout).
- The agents that write each block's tests and code get the list of files in
  the change's `prototype/` and are told to match what they show of a UI or
  API.
- The design and tasks agents get the component's `plan.md` (what it
  builds, what is out of scope, what done means), beside the change's
  proposal and the component's `specs.md`; for a change with one component,
  the `plan.md` in the change dir.
- When every component has merged into the feature branch, the
  `/buildme <change>` run that sees it archives the whole change there, once: every
  component's capabilities merge into `specs/registry/`, and the change moves
  to `specs/archive/<date>-<change>/`, committed and pushed to the feature
  branch. A component does not archive itself, and `buildme archive
  --change <change>/<id>` is refused: archive the change.

### Changed
- buildme builds from the change's feature branch, where pm-spec 0.8.0
  pushes the specs: new flag `build --branch <name>`, else the branch the
  change dir's `.change.yaml` records, else the one branch on origin named
  `<TICKET>-<change>` that holds the change's specs (or, after an `--auto`
  build, their archive) as their feature branch: another change's branch
  whose name merely ends the same way is not taken. Origin having several
  is refused with `SPEC:NOT_READY`,
  asking for `--branch`; a `--branch` that does not start with the change's
  `<TICKET>-` is refused the same way. The specs are read as that branch
  holds them, so the checkout you run it in need not have them.
- Every branch buildme makes is now named `<TICKET>-<change>`, replacing
  `buildme/<change>`. The ticket is the change's own ticket ref, else its
  nearest parent's (`ENG-40-auth-rework-a` for a child of `ENG-40`). A ticket
  id git refuses in a branch name is refused with the reason. A name origin
  already has is skipped for the next free one (`-2`, …).
- A single change (not an epic) builds on its feature branch itself, with no
  second branch: it pushes that branch, never forced, and opens one pull
  request from it to origin's default branch, left for you to merge. The
  build refuses when that branch is checked out in a worktree that is not
  one of the change's own builds, or when your local copy of it holds
  commits origin lacks. A `--force` rebuild of a published change builds on
  `<branch>-2`, cut where the first build started.
- Pull request titles are `[<target branch>] <TICKET> <title>`, replacing
  `buildme: <change>`; `<title>` is the proposal's first heading. For
  example `[main] ENG-42 Login times out after 5 min`, or
  `[ENG-40-auth-rework] ENG-40 Retry the token refresh` for an epic's child.
  Every pull request still opens through the pr skill, from the repo's PR
  template; the `gh pr create` line is only printed, as the manual fallback.
- Specs whose `.change.yaml` records no ticket ref, of their own or a
  parent's, refuse with `SPEC:NOT_READY` and
  `Run /multiplai-pm:pm-spec resume <name> --ticket-ref <ticket id> first`.
- At the start of every run, once its refusals passed (a refused run
  changes nothing, origin included), buildme merges origin's default branch
  into the feature branch with a merge commit, never a rebase or a force; in an epic
  it does so again before each later wave of children. A first run merges in
  a worktree of its own and pushes; a resume merges in the build's worktree
  and its own push takes the merge (skipped, with a warning, while that
  worktree has uncommitted changes to tracked files). A conflict is aborted,
  leaves the branch as it was, builds nothing, and exits `3` with the new
  marker `FEATURE_BRANCH:CONFLICT:<branch>:<files>`: merge by hand, push,
  and run the build again.
- An epic's children build on their own `<TICKET>-<child>` branches cut from
  the feature branch, and their pull requests target it; a child that
  depends on others starts after they merged into it, so it builds on their
  work and on any specs revision pushed there since. With `epic.merge`
  unset, the children's pull requests now merge automatically (`auto`) when
  the feature branch is not the default branch; `epic.merge: manual` in
  `specs/config.yaml` still leaves them to you. A pull request with no
  checks at all merges once none appear for two minutes after buildme's own
  push. The epic's pull request from the feature branch to your default
  branch is yours to open; buildme never opens or merges it.
- A resume whose specs were revised on the feature branch since its worktree
  was cut refuses with `SPEC:NOT_READY` and the `git merge` to run.
- `buildme findings --pr <n>` finds the change from a `<TICKET>-<change>`
  head branch as well as from `buildme/<change>`.
- A change in the older layout (`requirements/<cap>.md`, children as sibling
  change dirs `<parent>-<id>`) builds and archives as before.
- A `specs.md` whose capability headings repeat a name or are not
  kebab-case counts as missing requirements, so the build stops before
  planning instead of failing part way.

### Fixed
- buildme's mutation step no longer runs mutants that lie wholly inside a
  type annotation (a parameter, return or variable annotation). With
  `from __future__ import annotations` no test can kill them, so a fully
  green build failed `mutation.min_score` on them. `mutation.md` gives
  their number.
- A planning re-audit after a rewrite no longer stops the run on a new major
  or minor finding of a block that adds no behaviour; as SKILL.md says, only
  a new critical finding (or an unresolved listed one, or a finding of
  buildme's own placeholder and no-behaviour scans) stops it. SKILL.md now
  names the scans.
- A project whose `specs/config.yaml` sets `tdd.test_command` gets its
  stack's reference docs again. The stack was detected only while
  discovering a test command, so such a build printed `REFERENCES:(none)`.
- An epic run no longer marks a child `unknown` because `git fetch origin`
  lost a ref lock to another git sharing the repository (a child pushing
  from its worktree): the fetch is run again, up to three times.

### Removed
- The refusals of a change dir with uncommitted changes, of a first-wave
  child whose base carried commits outside the epic's change dirs, and of a
  dependent child whose base lacked its specs commit: every build now reads
  its specs from the feature branch.

### Migration
- Specs handed off by a pm-spec before 0.8.0 (ready and committed in your
  checkout, no feature branch on origin) refuse with
  `Run /multiplai-pm:pm-spec resume <name> first` (with `--ticket-ref
  <ticket id>` when the change records none). Run it, then the build.

## [0.104.2] - 2026-10-23

### Fixed
- **A killed or Ctrl-C'd review keeps every answer that came back.** The
  review saves `review-state.json` after each agent answer in the explain,
  find, verify, recheck and merge steps, with the spend so far, instead of
  only when a step ends or a budget, verify or usage-limit stop ends the run.
  A kill now loses only the calls still running, and `resume` asks only for
  those. The review, summary and checks Markdown files, the rollups and
  `runs.jsonl` are written to a temporary file and renamed into place, so a
  kill never leaves one half written.

## [0.104.1] - 2026-10-23

### Changed
- **codebase-walkthrough's page uses the neutral design system when no
  `--design-system-dir` is given.** The plugin now ships one theme. The
  walkthrough's own `templates/tokens.css`, which had different colours from
  neutral, is deleted.

## [0.104.0] - 2026-10-23

### Added
- **codebase-walkthrough explains a whole tool or plugin.** `run <dir> --scope
  codebase` reads a skill with its scripts, or a CLI with several commands,
  and adds four tables to a module's walkthrough: Which command to run, Loops
  and caps, Exit codes and markers, and State and resume. Three more agents
  each read the whole snapshot for one question (loops, what is kept on disk
  and what a re-run does, which command when), so a cap set in one file and
  checked in another is seen together. The tool's own `SKILL.md`, `README.md`,
  `CHANGELOG.md` and `CLAUDE.md` are read as input and cited like code. Every
  row cites a line at the commit; a flag, number or name in no cited quote
  cuts the row, and a table with no row left says so. It reads at `overview`
  depth unless `--depth` says otherwise. `--mode agent` is refused with it.
- **`--lib <dir>`** (with `--scope codebase`, repeatable) also reads a library
  the code calls, at its HEAD, and cites it; How this was made lists each one
  with its commit. A directory that is missing or outside a git repository
  stops the run before anything is written.

## [0.103.2] - 2026-10-23

### Fixed
- **Subcommands that dispatch with `if args.command == "x":` now have a
  handler.** codebase-walkthrough and review trace paths from each argparse
  subcommand; a subcommand whose CLI compares the subparsers' `dest` with its
  name (an `if`/`elif` chain or a `match` statement) used to get no handler, so
  its trace stopped at the `add_parser` line. Discovery now takes the function
  the branch returns (or exits with, or runs through `asyncio.run`), and starts
  the known edge at the branch line. `set_defaults(func=...)` and dispatch dicts
  still come first, and a branch of inline code still has no handler. A trace
  cached by an earlier run is asked again for each subcommand that gains a
  handler.

## [0.103.1] - 2026-10-23

### Fixed
- **Path tracing no longer stops at the first hop of a subcommand or a CI
  step.** codebase-walkthrough and review trace a path hop by hop, and the
  gate cut a trace with `unconnected_hop` when hop 2 did not follow from hop 1.
  It now also connects an `add_parser` line to the handler discovery resolved
  for it, and a command line in a non-Python file (a shell script, a CI
  `run:` step, a `SKILL.md`) to the script or `python -m` module it runs. The
  trace prompt tells the agent to start an `add_parser` entry at its handler's
  `def` line. Resumed runs re-gate their cached trace answers under the new
  rule, so a resume picks up the fix with no new model call.

## [0.103.0] - 2026-10-23

### Removed
- **review-viewer no longer shows a walkthrough the session writes.** The
  Reviews tab, its steps, the Overview box, the `]`/`[` keys and mermaid are
  gone, and so are `walkthrough put|status` and `walkthrough.v1`. The session
  writes no explanation of the code for the viewer.
- codebase-walkthrough writes no `walkthrough.json` for a module.

### Added
- **A Full walkthrough row on the review-viewer's Summary tab**, for a PR, a
  branch, unpushed commits, a range or a directory. When the codebase-walkthrough
  page of what is viewed exists at the viewed commit, it opens in a new tab,
  served by the viewer in a sandbox at a second secret address. When it does
  not, a dialog says what writing it does (the session runs
  codebase-walkthrough, which sends the repository's code to a model, makes
  paid model calls stopping at $50, searches no other repository, and for a PR
  runs `gh pr view`); Confirm asks the session to run it, and the session
  resumes or re-renders an earlier run of the same commit instead of paying
  twice.

### Changed
- **The session writes `summary.json`** (`summary put|status`, schema
  `summary.v1`): only its assessments of the commits and the tests and the
  risk block. `summary status` also prints the full walkthrough's state, its
  folder, an earlier run of the commit and the command that writes it.
- buildme's `regenerate_walkthroughs` commits the regenerated HTML page and
  `-paths.json` with the Markdown.

## [0.102.0] - 2026-10-23

### Added
- **codebase-walkthrough explains a PR or a commit range.** `run --scope pr
  --pr <N>` (needs `gh`) or `run --scope pr --range <base>..<head>` reads the
  change at its head against the merge-base, from git objects only, and writes
  a change walkthrough as Markdown and an HTML page: What changed and why, The
  path through the change, Who is affected, What it does not touch (written by
  code from the diff), Reference and How this was made. The agents read only
  the lines the change touches, with five lines around each; the callers of the
  changed functions and classes come from `git grep` and Python `ast`; only the
  paths through the added lines are traced. `--expect-head <sha>` refuses to
  start when the change's head is another commit, and `--fetch` runs
  `git fetch --quiet origin` first. The PR's title and description reach the
  writer inside untrusted-content fences. No `walkthrough.json` is written for
  a change.
- **`run --commit <sha>`** explains a module directory at an older commit, with
  no checkout.

### Changed
- review's PR and fetch lookups (`gh pr view`, `git fetch`, `rev-parse`,
  `merge-base`) moved to a new shared library, `lib/git_targets`, which the
  walkthrough also uses. Review behaves as before.

## [0.101.0] - 2026-10-22

### Added
- **codebase-walkthrough styles its HTML page with your design system.**
  `run`, `resume` and `render` take `--design-system-dir <folder>`, the
  folder `/multiplai-dev:design-system`'s `path` command prints. The page then
  inlines that folder's `fonts.css` and `tokens.css`. The folder is checked
  before any model call, so a bad one costs nothing. A missing folder, a
  missing token, or a text colour below WCAG AA exits 1 with
  `ERROR: design system <path>: …`. A run keeps its folder, and `resume` uses
  it unless you pass the option again. `render --design-system-dir` restyles a
  finished run for free, without changing its saved state. Without the
  option the page looks as before.

## [0.100.0] - 2026-10-22

### Added
- **`/multiplai-dev:design-system`: one set of design tokens for every HTML
  page a skill builds.** Keep your design systems in one folder
  (`MULTIPLAI_DESIGN_SYSTEMS`, an absolute path) and name the default
  (`MULTIPLAI_DESIGN_SYSTEM`), in the environment or in `multiplai.conf`.
  With neither set, pages use a neutral theme shipped with the plugin: greys,
  one blue accent, system fonts, light and dark. Each design system is a
  `tokens.css` (colours, fonts, type scale, spacing, radius, measure) plus a
  `README.md`, and optionally `fonts.css` and `components.md`.
  `python -m design_system list | show | path | check | create`:
  - `check` fails a token missing from the light block or from either dark
    block, a text/background pair below WCAG AA (4.5:1), a colour it cannot
    read, and `</style` in a CSS file, and prints every contrast ratio;
  - `create` copies the neutral theme to a new name and never overwrites;
  - a name that does not exist is an error listing the names that do. It
    never falls back to neutral.

### Changed
- **learn-stack's guide and deepen's architecture report use the design
  system's colours and fonts.** learn-stack's page no longer hardcodes a
  teal accent and dark Prism code blocks, and its code theme follows the
  reader's light or dark setting. `render_report.py` takes
  `--design-system-dir`, and without it uses the neutral theme. The report
  now follows the reader's light or dark setting too.

## [0.99.0] - 2026-10-22

### Changed
- **buildme now builds only from specs `/multiplai-pm:pm-spec` marked ready.**
  Run pm-spec first: it writes the proposal, use cases and requirements, runs
  the interview, reads a ticket, a PRD or a plan, splits an epic and makes the
  prototype, then marks the specs ready and commits the change dir. buildme
  reads those three documents and never writes them. A build whose specs are
  not marked ready, changed after they were, or (for a build that branches a
  worktree) are not committed prints `SPEC:NOT_READY:<reason>` and
  `Run /multiplai-pm:pm-spec <name> first`, and exits `2` with nothing
  written. When specs pm-spec never marked are on disk (an older buildme
  build wrote them), the line names `pm-spec resume <name>`, which checks them
  and marks them ready without rewriting them. The check reads the copy the
  build reads: with `--base-ref`, the change dir as that ref holds it, before
  the worktree is cut; on a resume, the copy in the build's worktree, and the
  pm-spec line then names `--project-dir <worktree>`. A resumed worktree build
  also refuses when the change dir in the checkout it was cut from was marked
  ready again since with other specs, and names where they are and how to
  bring them into the worktree, instead of building its older copy.
- buildme: `--change` is now required on `build`. The design reads the
  research directly, and the research decision reads the ready specs.
- buildme: a requirements problem goes back to pm-spec instead of being
  worked around. When the design step reports a `SPEC_ISSUE:` line, a design
  audit finding needs a requirements change to fix, or a TDD agent reports
  `SPEC_IMPACT: contradicts` under `respec: {halt_on_contradiction: true}`,
  the build appends the problem to `spec-issues.md` in the change dir, sets
  `spec_issues` in `.change.yaml`, prints `SPEC:ISSUES:<path>` with the
  `pm-spec revise <name> --project-dir <repo>` line, and exits `4`. The
  checkpoint is kept, not failed: every run refuses the same way, at no cost,
  until `pm-spec revise` clears it; the next build then writes the design and
  what follows it again. Exit `4` also still means an agent timed out: the
  `SPEC:ISSUES` marker tells the two apart.
- buildme-findings: a finding that asks for a requirements change is recorded
  in `spec-issues.md` for `/multiplai-pm:pm-spec revise`; the findings stage no
  longer edits `requirements/*.md` or `specs/registry/`, and makes no model call
  for it. A code fix that contradicts the specs waits for that change, and its
  reply says so.
- buildme epics: `/buildme <parent>` on a parent change pm-spec split (its
  `.change.yaml` lists `children`) builds the children in their `depends_on`
  order, with the same review, fix and merge loops as before. It refuses with
  `SPEC:NOT_READY` when any listed child is not ready, or when the parent's or
  a child's change dir has uncommitted changes. A child with no dependency
  branches from the local commit that holds the ready specs, so its PR carries
  that commit and nothing is pushed before the build; a child that waits on a
  dependency branches from origin's default branch once that dependency merged,
  and is not launched (it fails, naming the commit to merge) when that branch
  does not hold its specs as that commit does. A child with no dependency is
  not launched either (it fails, naming the paths) when a commit between
  origin's default branch and the specs commit touches a path outside the
  epic's change dirs, which its PR would carry. A merged child whose change
  dir its `--auto` build moved to `specs/archive/` stays merged on the next
  run instead of being refused. Each child runs as
  `build --change <parent>-<id>`. An epic now runs no spec-extract, so its
  children build without a `specs/registry` baseline.

### Removed
- buildme `build` flags: `--mode` (`scratch`, `brief`, `only`),
  `--interview-summary`, `--context-files`, `--ticket-file`, `--ticket-ref`,
  `--ticket-url`, `--plan-file`, `--interview-replies`, `--prototype`,
  `--no-prototype`, `--approve-split` and `--no-epic-split`. A saved command
  that passes one of them now stops with a usage error (exit `2`). Pass the
  ticket, PRD, plan or interview to pm-spec instead.
- buildme `setup` flags `--provider`, `--plane-project` and `--interview`, and
  the `intake.provider` and `interview` keys of `config-get`: they are
  pm-spec's settings. buildme ignores `intake`, `interview` and `prototype` in
  `specs/config.yaml`.
- buildme's interview (`INTERVIEW:WAITING`), ticket intake, proposal, use-case
  and requirements generation, prototype stage, and epic split
  (`EPIC:SPLIT`, `split.md` approval). They are pm-spec's steps now.
- buildme epics: children in another repository (the `repo` column of
  `split.md`) are dropped for now. Every child builds in the epic's own
  repository.

### Upgrading
- A build stopped before its specs were generated refuses with
  `Run /multiplai-pm:pm-spec <name> first`. A build stopped later, with a
  proposal and requirements on disk, refuses with
  `Run /multiplai-pm:pm-spec resume <name> first`; after pm-spec marks them
  ready (and they are committed), re-run the same build command and it
  continues from its checkpoint. Nothing is changed by the refusal.
- An epic that an earlier buildme split and approved (`split.md`) is not
  continued: its parent has no `children` list, so it refuses with
  `SPEC:NOT_READY`. Child PRs already open are kept; split the rest again with
  pm-spec. A child's saved command from before this release uses removed flags,
  so re-running it by hand stops with a usage error. Once pm-spec has split the
  parent again, the epic relaunches such a child with a new command instead of
  the saved one.
- A build stopped in its own worktree refuses the same way, and the line names
  `--project-dir <worktree>`: run pm-spec there, then re-run the build.

## [0.98.1] - 2026-10-21

### Changed
- internal: spec format code moved to multiplai-core. buildme now reads and
  merges a change's requirements with `multiplai_core.specs`, the code the
  coming `pm-spec` skill will use too: change names, the capability list in
  `proposal.md`, the requirement check, the merge into `specs/registry/` and
  the archive move. Nothing a build does changes, and there is nothing to do
  on your side.
- spec-extract: a scenario with no text in its name, its WHEN or its THEN is
  now dropped and listed in `report.md`, like a scenario whose citation fails.
  Every registry file it writes now passes buildme's requirement check.

## [0.98.0] - 2026-10-21

### Changed
- buildme: after a planning step rewrites the plan, the re-audit is told which
  findings the rewrite was asked to fix and says for each one whether it is
  resolved. The step stops when one is not resolved, or when the re-audit finds
  a new critical problem. New major and minor findings are logged and written
  to `build-progress.md` under `<STEP>_ADVISORY`, and no longer stop the build.
  This applies to the design audit, the plan review and the tasks audit.
- buildme: when a planning step stops, the checkpoint, `build-progress.md` and
  the console show every finding the rewrite was asked to fix, with an id such
  as `DA-F1` and its status, and every new finding that stopped it. The tasks
  audit's re-audit used to drop its findings and stop with a bare error.
- buildme: resuming a stopped planning step without editing the plan stops
  again with no model call and prints the same findings. Editing the plan gives
  the step one more rewrite; before, an edited plan had to pass with no rewrite
  allowed.

### Fixed
- buildme: the prototype feedback rewrite now runs at most once. A resume after
  a stop used to run it again, with no limit.

## [0.97.1] - 2026-10-21

### Fixed
- codebase-walkthrough: `resume` no longer keeps an old run's path cap. A run
  saved before the default `--scenarios` became 21 resumed with 6 paths; a
  resume now uses the cap you passed with `--scenarios`, and 21 otherwise.
- codebase-walkthrough: paths under `.github/` and other dot-directories are
  traced again. A leading `.` was stripped with the `./`, so the file was not
  found.
- codebase-walkthrough: `check` fails a hop id named in backticks, such as
  `` `p2h3` ``, that has no anchor on the page, as it already did for a hop
  link.
- codebase-walkthrough: the Paths section says when entry discovery was
  incomplete because the model fallback failed.
- codebase-walkthrough: the code quotes and agent notes in the path hops go to
  the writer as fenced, untrusted repository text.

## [0.97.0] - 2026-10-21

### Added
- codebase-walkthrough: human mode writes an HTML page,
  `<name>-walkthrough.html`, beside the Markdown again. It opens straight
  from disk in a browser, in light or dark theme and at phone width: one
  scrolling page with a contents list down the side, every citation a small
  numbered link to the cited lines on GitHub with `path:line` and the quote
  in its tooltip, a link to a path hop (`#p2h3`) opening the closed section
  that holds it, and Mermaid diagrams drawn.
- codebase-walkthrough: in human mode the walkthrough is an explainer for
  someone new to the code. It opens with what the code is for, then use
  cases with example commands or calls (one per way to invoke it), the main
  path as one flow diagram with a short summary per step and the specifics
  behind a click, what the code creates with example payloads behind a
  click, its configuration, and gotchas. The paths, module tour, callers,
  data stores, vendor docs and glossary move under a closed Reference
  section, and the commits read, coverage and cuts under a closed "How this
  was made". Code checks every example's flags and subcommands, every
  diagram box and every key of a JSON or YAML example against the code, and
  cuts what fails.
- codebase-walkthrough: `resume <run dir> --rewrite` writes a finished run
  again in the current format, paying only for the write stage: the explore,
  docs and trace answers are kept. It refuses a run that has not finished
  its trace stage.
- codebase-walkthrough: `render <run dir>` rewrites a finished run's
  Markdown, `walkthrough.json`, `<name>-paths.json` and HTML files from its
  saved state, with no model call and no spend. It gives a run that finished
  before the page existed its page.

### Changed
- codebase-walkthrough: at the end of a run the skill hands over the HTML
  page as a `file://` link instead of opening the review-viewer; it opens
  the viewer only when you ask for it. `walkthrough.json` is still written.
- codebase-walkthrough: "Commits read" lists only the module's repository
  and the repositories with a reference, with one line saying how many were
  searched. Names that appear only inside a quoted string are listed under
  "Mentions in strings (not calls)", apart from the callers.

## [0.96.8] - 2026-10-21

### Fixed
- buildme-findings: on the last, escalated round, a pull request with a long
  review history no longer loses the instructions that make it an
  escalation. The history came first in the fix task and the task was cut at
  20,000 characters, so "question the block's approach", "fix only the
  findings in the brief" and the `RECURRING:` line it asks for were dropped.
  The history is now cut instead, to whole findings, and says how many it
  left out. A history that fits is unchanged.

## [0.96.7] - 2026-10-21

### Fixed
- review: a finder or converter answer whose `other_locations` is `false`, a
  number or an object no longer stops the find stage with a `TypeError`. The
  finding is kept with no other locations. In a list, only the strings are
  read as places, so an object's keys never become a location. A string or a
  list of strings is read as before, and the location gate still checks each
  place.

## [0.96.6] - 2026-10-21

### Fixed
- buildme: a review round record no longer claims the reviewer read HEAD
  when it read uncommitted code, as the first review after an integration
  fix and the review after the fix on pass do. Each record now has a
  `reviewed_sha`, set only when nothing in the project differed from HEAD
  (a submodule the parent's `ignore` setting hides and files marked
  assume-unchanged or skip-worktree included), and otherwise null with the
  reason in `reviewed_sha_unknown`. `review_metrics.py` traces a finding's
  line only from those commits, so it no longer reports a line the fix wrote
  as present at the first review, or the other way round; it reports
  `unknown` instead, as it does for every record written before this
  version. The diff a later review round's adjudicator is shown is left out
  when either review read uncommitted code.

## [0.96.5] - 2026-10-21

### Fixed
- review: a changed file whose name holds a space, a quote, a tab, a
  backslash or, with `core.quotePath` on, a non-ASCII character is no longer
  lost to the paths stage. Its changed lines were filed under the name as git
  writes it in the diff header (with a trailing tab, or in quotes), so no path
  through the file was found and its path finders never ran. The explain
  stage's scope and the record of which cited lines were in the diff now read
  those names the same way, and a name that starts or ends with a space keeps
  it.

## [0.96.4] - 2026-10-21

### Fixed
- buildme: a project with a Git submodule no longer fails its final check with
  `Final verification unavailable: ... Is a directory`. The check now records a
  checked-out submodule's commit, index and every file it tracks or holds
  untracked, nested submodules included. The check passes when the test
  command leaves the submodule alone, and fails, naming it, when the command
  changes it, even with `submodule.<name>.ignore` set. An uninitialized
  submodule passes; a submodule directory holding files without a checkout
  fails the check as unavailable. A repository nested untracked inside the
  project is checked the same way.

## [0.96.3] - 2026-10-21

### Fixed
- buildme: a finding the fix on pass did not fix no longer disappears when
  the review after it passes without mentioning it. That review is now given
  the block's open findings, like any later review round, and keeps each one
  it does not call resolved; the block's findings and `unfixed-findings.md`
  list it with its status. When the block's round records could not be
  written or read, every finding the fix on pass worked on stays open.

## [0.96.2] - 2026-10-21

### Fixed
- review: an earlier finding no longer shows as resolved when this round's
  finding still open against it is merged into a duplicate by the assess
  stage. The merged-away finding's label used to be dropped, so the earlier
  finding kept the recheck's `resolved`, and the surviving duplicate was
  counted as new. Now the earlier finding is `still_present`, and the
  survivor is labelled `still-open` against it, with its `lineage`. When a
  duplicate set is still open against two earlier findings, both stay open.

## [0.96.1] - 2026-10-20

### Fixed
- buildme-findings: a reply no longer says a check failed after the fixes
  when that failure came from an earlier attempt. A failed check after the
  fixes (the whole-change refactor's budget stop, or mutation testing) stops
  the round before anything is pushed, and the skill's table now says so
  instead of saying the fixes are still pushed.
- buildme: the README no longer says a failed plan review never fails the
  build. It stops planning, as the section above it already said.
- review-viewer: the walkthrough schema's badge definition is now
  `WalkthroughAssessment` in `walkthrough.v1.schema.json`'s `$defs`, so it no
  longer shares the name `Assessment` with a finding's label in the code. The
  shape a walkthrough file must have is unchanged.

## [0.96.0] - 2026-10-20

### Added
- buildme-findings: triage now sees the verdicts earlier rounds gave the
  same defect (findings of one lineage in the review's `findings.json`),
  fenced as data, and is told to weigh them against the code as it is now
  rather than copy them. A `not_defect` verdict still never hides a finding
  from the next review.
- buildme-findings: a fix block lists the other places the review found the
  same defect (`other_locations`) and asks for every listed place to be fixed
  and covered by a test.
- buildme-findings: the last round `findings.max_rounds` allows, from round 2
  on, starts every fix block with the escalation text buildme's own last
  review fix uses: every finding lineage of the PR's reviews with what each
  review said about it, a request to question the approach, and a
  `RECURRING:` line naming findings that share a cause. That round's fixer
  runs on the `review_fix_escalated` model and effort. Whether a round is the
  last is decided when it starts and kept on a resume, even if
  `--max-rounds` changed. The new `--no-escalation` flag turns this off; an
  epic passes it to every fixer, since it runs them one round at a time.
- buildme-findings: after `FINDINGS:MAX_ROUNDS`, one
  `FINDINGS_RECURRING:<lineage>:<rounds>` line names each finding lineage
  open in two or more of the PR's reviews, and one
  `FINDINGS_RECURRING_SLOT:<block>:<answer>` line quotes each `RECURRING:`
  answer of the last round.

## [0.95.0] - 2026-10-20

### Added
- review: every finding now names its kind of defect in a few words and
  every other place in the change or the code with the same defect. The
  finders are asked for both, and the review shows them under the finding as
  `class: …; also at: …`, as do the PR comments `post` writes, so whoever
  fixes the finding sees every place to fix. Python checks each other place
  before it is shown: one whose file is not at the reviewed commit, or whose
  line is past the end of the file, is dropped with a line under agent
  failures, and the finding itself is kept. When duplicates are merged, the
  merged finding lists every place its members named. `findings.json`
  carries the two optional keys `defect_class` and `other_locations`, which
  review-viewer's schema accepts; a review stopped before this version
  resumes without asking any finder again.

## [0.94.0] - 2026-10-20

### Fixed
- review: the assess stage no longer merges a finding into one anchored in
  another file, or more than two lines away in the same file. The assess
  agent reads every finding at once and could fold a root bug into a weaker
  finding elsewhere, whose claim then hid it (on PR #295 it lost the
  template slug collision and `--head` reaching `git push`). The members of
  a duplicate set it names are merged only where their lines overlap or
  come within two lines in one file; the rest stay separate findings, and
  each dropped set is logged with its ids. Duplicates across files are left
  to the merge stage, which already groups findings that cite the same lines.

### Added
- review: a review run again on the same PR or branch now says what became
  of each finding the last round listed and nobody rejected. A new recheck
  step, after verification, gives each one to a fresh agent with the diff of
  the files it cites since that round (never this round's findings, and the
  finders still see nothing of earlier rounds), which answers `resolved`,
  `still_present`, `partially_resolved` or `cannot_verify` with lines at the
  new head. A status other than `cannot_verify` stands only when its cited
  lines are there; an earlier finding no agent could check, or that a stopped
  run's resume did not recheck, is `cannot_verify`, never resolved; one a
  finding of this round is `still-open` against is `still_present`. The
  review lists them under "Findings from earlier rounds". Every finding
  carries a `lineage`, the id of its first appearance, kept across rewordings
  through `still-open` and `repeat`; a "Recurring" section names each one open
  in three or more rounds, and the header and summary count new findings
  apart from those still open ("2 new, 1 still open, 3 earlier resolved, 0
  earlier cannot verify"). `findings.json` gains the optional `lineage` per
  finding and a top-level `earlier` list (`findings.v1`, `schema_version`
  still 1), and `checks.json` the `earlier_status_gate` results. A review of
  a tree, or one with nothing open from before, makes no recheck call.
- review: in `findings.json`, a finding that others were merged into lists
  their ids and claims under the new optional `absorbed` field
  (`findings.v1`, `schema_version` still 1; the schema now says additive
  changes keep the version). Reviews whose assess stage finished before this
  version keep their merges and get the field when they are exported.
- review-viewer: a finding's page shows the findings merged into it, by id
  and claim, under "Merged into this finding" after the verdict.

## [0.93.0] - 2026-10-20

### Added
- The review skill has a `round-metrics` command, which reports how a PR's
  repeated reviews went, from the rounds the review already keeps. Run
  `python -m review_pipeline round-metrics [<reviews dir>]` from the review
  scripts directory: it prints a Markdown table per review with each round's
  head, the findings it listed, and how many were labelled `still-open`,
  `repeat` and `low-value`. For each finding listed in round 2 or later it says
  whether the cited line was already there at the first review or was written
  since (from `git blame` and `git merge-base` in the reviewed repository;
  `unknown` when git cannot tell). A totals line gives the mean rounds per
  review with two or more rounds, and the share of later findings present at
  the first review and the share still open, each with its counts. When a
  review's worktree has been deleted, `--repo <checkout>` names another
  checkout of the same repository to read. The command writes nothing, calls
  no model and needs no `--trust-repo`.

## [0.92.0] - 2026-10-20

### Changed
- buildme's per-block review loop now remembers its earlier rounds. Every
  accepted finding gets an id in its round record
  (`reviews/block-NN-round-K.json`, e.g. `B01-R1-F1`), and from the second
  round on the reviewer is shown the findings still open and asked whether
  each is resolved, still present, partly resolved, or cannot be verified. A
  finding it gives no answer for stays open. The judge that accepts or
  rejects findings now also says when a new finding restates an open or an
  earlier-rejected one, and whether it lies within reach of what changed
  since the previous review. A restatement of an open finding is merged into
  it instead of counted as new; a restatement of a rejected one stays
  accepted, with the earlier rejection reason recorded next to it.
- From the second round on, a new finding outside the reach of the last fix
  is listed in the round record's `outside_fix_scope`, and is fixed like any
  other. The fix agent gets the findings still open, each with its status,
  plus the new ones. Round 1 runs as before, and the review still passes or
  fails on its scores alone.
- The last fix of the loop now questions the block's approach: it runs on
  the review model (new step `review_fix_escalated` under `models:` /
  `efforts:`), sees every finding of every round with what each round said
  about it, is told to fix only the findings in its brief, and ends its
  report with a `RECURRING:` line naming the findings
  that share a cause, which the round record keeps. When the loop runs out,
  `build-progress.md` lists the findings still present in two or more rounds
  and that `RECURRING:` answer.
- Reviewers now name each finding's kind of defect and every other place in
  the diff with the same defect; the fix agent is told to fix every listed
  location and cover each one with a test.
- On a block that passes at round 2 or later, the fix on pass works on the
  findings still open plus the new ones, so a finding the
  reviewer left unanswered is fixed there instead of dropped.

## [0.91.0] - 2026-10-20

### Fixed
- `/multiplai-dev:review` stops as soon as the Claude usage limit is hit,
  instead of calling the model until every finder and verifier has failed.
  Before, each verify call was tried up to three times, each with a re-ask,
  and the other stages carried on, so one run could make hundreds of calls
  that each failed in seconds against a limit that resets hours later. Now
  the first call that reports `You've hit your session limit` (or `weekly`)
  stops the run: no stage retries or re-asks that call or starts another,
  and the checkpoint keeps every answer that came back. The run exits `6`
  with the reset time (when the CLI gives one) and, for each target it
  stopped, the command to continue: `python -m review_pipeline resume
  <target_dir> --trust-repo`, with the run's `--max-cost-usd` and
  `--no-deps`. After the reset that resume asks only for what is missing; a
  plain `review` of the same target would start over. The ledger's
  `finished` line says `stopped: usage-limit`. A batch starts no further
  target after the limit and lists those as `not started`; `assess-only`
  does the same and still writes its report. Needs multiplai-core 0.25.0,
  which this release locks.

## [0.88.0] - 2026-10-20

### Added
- buildme ships `scripts/review_metrics.py`, which reports how a build's
  per-block review loop went, from the round records buildme already writes
  (`reviews/block-NN-round-K.json`). Run
  `python3 review_metrics.py <project>/specs` from the buildme scripts
  directory: it prints a Markdown table per change with each block's review
  rounds, the round it passed at, and the accepted and rejected findings of
  every round. For each finding accepted in round 2 or later it says whether
  the flagged line was already there at the first review or was written by a
  fix (from `git blame` and `git merge-base` in the project; `unknown` when
  git cannot tell). A totals line gives the mean rounds-to-pass, the share of
  blocks passing at round 1 and the share of round-2+ findings present at the
  first review. It writes nothing, uses no network and makes no model calls.

## [0.87.0] - 2026-10-20

### Added
- buildme's test writer now names the tests it expects to fail before the
  implementation exists, and what is missing that makes them fail (a
  `RED_EXPECTED:` line in its report). When the RED gate passes, buildme checks
  those names against pytest's failures and records the statement and the
  result (`named tests failed`, `named tests not among the failures: <ids>`,
  `not stated`, or `cannot check for this runner`) at the top of the RED
  evidence that the reviewer and `build-progress.md` see. It never changes
  whether the RED gate passes.

### Changed
- The RED gate's decisions now come from one ordered table in the code, so
  each decision is one row with one test. Every decision it makes is the same
  as before.

## [0.86.0] - 2026-10-20

### Fixed
- buildme verifies the final revision after review fixes, documentation and
  archive preparation. Failed checks or partial writers cannot publish a build;
  publication failures remain separately resumable after successful verification.
- Recovery preserves staged and working versions independently, including nested
  projects. Legacy stop records without index evidence refuse destructive recovery.
- Planning resumes missing capability requirements, includes proposal and use cases
  in task prompts, and invalidates dependent artifacts and reviews after edits.
  Shared, bounded plan repair stops when blocking findings remain.
- Planning, retries and resumed calls obey the same budget admission policy as
  implementation; queued calls cannot dispatch after exhaustion. Admission is
  checked before every provider call, including mid-block; committed RED tests
  and cumulative spend survive resume. Findings rounds now share one run budget.
  Cancellation without provider usage is recorded as unknown and blocks capped
  admission until the user resumes with `--accept-unknown-spend`; the unknown
  records remain and the cap then covers known spend only. Available partial
  usage is charged.
- Required design and plan audits revalidate after each bounded repair. Completed
  prototypes do not rerun after downstream repairs or during implementation resume.
  A failed prototype writer now stops the build at a resumable checkpoint.
  Proposal edits before implementation invalidate a prior prototype skip.
  Blocked audit findings remain in the checkpoint and progress log; unchanged
  blocked plans do not pay for the same audit on each resume.
  Outcomes and repair admission share one revision ledger. Task-audit repairs stage
  the full plan before publishing. Legacy implementation checkpoints without
  revision evidence refuse continuation while preserving code and block state.
- RED checks reject setup/collection errors through command wrappers and accept
  assertion failures whose tracebacks quote exception names. Other runners retain
  an explicitly unverified nonzero fallback, including compiler/setup exits.
  Opaque wrappers with unmatched pytest paths also pass as unverified; pytest and
  Jest collection/setup failures remain blocked. Pytest paths follow literal `cd`
  wrappers. New Python package/function tests can reach RED without import errors.
  Nested review diffs
  use project-relative paths and exclude sibling changes.
- Commits recognize staging created within agent write windows while preserving
  divergent user staging. Commit failures raise instead of appearing as no-ops.
- Epic builds forward explicit unknown-spend acceptance to new/resumed children
  and capped fixers; historical commands cannot silently reuse that acceptance.
- Test commands preserve literal `#`, `|` and other argument punctuation. Mutation
  and epic merge tests share the build runner's literal `cd DIR &&` handling.
- Generated test reports belong in the project's `.gitignore`; final verification
  names changed files and provides that remedy. Commits never run the test suite.
  Direct pytest RED rejects empty collection, usage errors and failures outside
  the changed tests; exit 1 with a suppressed failure summary is unverified.
  Opaque wrappers with unmatched failure paths remain unverified. Recovery guidance
  covers reports already tracked by a block commit: untrack, ignore and commit.
- A tasks-audit finding that a block adds no new behaviour now drives the plan
  repair, and blocks a repaired plan, whatever severity the model gives it.

### Added
- A paired evaluation harness with held-out checks, timeout and recovery measurement.
  The saved dry run validates scoring only. An offline worker regression exercises
  clean bootstrap and provider admission before checking staged-user-work preservation.
  Live effectiveness is pending and optional stage defaults remain unchanged.

## [0.85.5] - 2026-10-20

### Fixed
- buildme epic: `epic.json`'s `spend_complete` is now false when a review of
  a child's PR was killed and then run again fresh instead of resumed. Before,
  the fresh run's `finished` line made the spend look complete though the
  killed run's spend was never counted. A review that ended with no run
  record and was then resumed now counts as complete; before, it stayed
  incomplete for good.
- buildme epic: reading a child's repository slug and checkout root runs git
  with buildme's git timeout, so a hung `git` no longer stalls the
  coordinator. A `git` that times out or does not start there leaves the
  child `unknown` for that run instead of parking it as "not a git checkout",
  and approval names it as a git error.
- buildme findings: `--max-usd` no longer allows one more paid round when the
  spend equals the ceiling. Adding the run's own spend and its reviews' costs
  as floats could come out a hair under the ceiling ($0.10 + $0.70 gave
  0.7999999999999999 under $0.80); the spend is now rounded to 6 decimals
  first, as the epic rounds its own.

### Changed
- buildme epic: the skill's instructions now say that a fixer still running
  from an earlier run that was killed holds no share of `epic.max_usd` in the
  new run, so the epic can spend past the cap by up to that fixer's own
  `--max-usd`.

## [0.85.4] - 2026-10-20

### Fixed
- buildme-findings: the review step now runs `/multiplai-dev:review` with
  `--fetch`, so a PR whose base moved on GitHub since the last fetch is
  reviewed instead of refused.
- buildme epic mode: when a child's review or the post of its findings cannot
  start (for example `uv` is not on PATH), that child is `unknown` and the
  other children go on. The epic run no longer stops with a traceback, and
  the next run starts the review, or posts the findings, again. Before, this
  failed every later run.
- buildme epic mode: a child whose earlier review is reused is judged on that
  review's own findings, read from its kept round, rather than on the latest
  run in the same review folder, which may be for another head.
- buildme epic mode: a run no longer exits `0` while a child's PR is at a head
  no review finished on (its review was stopped, or the PR got a new head
  after a clean review). The run names that child on stderr and exits `1`,
  or `3` when it also prints `EPIC:MERGE_WAITING` or `EPIC:BUDGET`; the next
  run reviews the new head.

### Changed
- buildme: SKILL.md says what happens when another child depends on a child
  left `unknown` by its review: the same run reads its PR again and waits on
  it, so the run ends with `EPIC:MERGE_WAITING`.

## [0.85.3] - 2026-10-20

### Fixed
- buildme epic mode: when the merge step finds that main and a child raised
  the version to the same value, and only the child edited its CHANGELOG, the
  merge agent now moves the child's entry to the new version it writes. Before,
  the entry stayed under main's version, and the merge commit was pushed with
  it there.
- buildme epic mode: the merge step no longer parks a child whose regenerated
  uv.lock is correct when the project asks for a git dependency in its
  `pyproject.toml`. It read the `git` URL in the project's own lock entry as a
  pin and wanted that entry unchanged, so the version the merge wrote, or a
  dependency list merged from both sides, failed the check.
- buildme epic mode: the merge agent is now told to raise only the package's
  own version, the one the merge step checks. Before, when main and a child
  both raised a dependency's version alike (such as
  `[dependencies.<name>]`), the agent raised that one too, the check refused
  it, and the child parked. `[tool.commitizen]`'s `version` counts as the
  package's own, so the agent raises it with `[project]`'s.

### Changed
- buildme docs: SKILL.md and the scripts' notes now say that an epic's spend
  includes the merge agent's calls, and that an interrupted merge agent call
  marks the spend incomplete.

## [0.85.2] - 2026-10-20

### Fixed
- buildme: an implementer's `TEST CHANGE REQUIRED:` declaration now counts
  when it is written as a markdown heading (`### TEST CHANGE REQUIRED: ...`)
  or with the whole line in inline code. Before, the block failed although the
  change was declared. A sentence that only mentions the marker in inline code
  and goes on after it still declares nothing.
- buildme: a `TEST CHANGE REQUIRED:` reason written wholly in parentheses
  now counts as a declaration. Before, it was read as an empty answer and the
  block failed.
- buildme: a `conftest.py` no longer turns test narrowing off just by
  mentioning `collect_ignore` or `pytest_ignore_collect` in a comment or a
  string, by ignoring files no block selected (`collect_ignore =
  ["setup.py"]`), or by sitting above the directory of the whole run's pytest
  config, where pytest never loads it. A `pytest_ignore_collect` hook, or an
  ignore list buildme cannot read, still runs the whole test command.
- buildme-findings: when the check of a round's fixes finds a problem, the
  PR description's round section now quotes it and says whether the extra
  block fixed it (with the commit) or why not. Before, the problem appeared
  only in the change directory, because no review thread answers it.
- buildme-findings: a check that answers `PROBLEMS: None found.` or lists
  `- Nothing found` is read as finding nothing. Before, that answer became a
  problem and started a fix block with nothing to fix.

### Changed
- buildme-findings: the fixer's brief asks for the `SEARCHED:` section after
  the other report slots and before any test-change declaration, which is the
  order the declaration prompt already asks for.

## [0.85.1] - 2026-10-20

### Fixed
- buildme: when the tasks.md rewrite after the shape audit fails, the first
  pass stands as before, and the log now names any block in it that adds no
  behaviour (`PHASE: no_behaviour_blocks_remain`), so a TDD RED-gate failure
  on that block is no longer a surprise.
- buildme: a block that says "no new code changes to the core" or "no new
  code needed in the core" (scoped to another part of the code) is no longer
  flagged as a block that adds no behaviour, so it no longer costs a
  tasks.md rewrite.
- buildme: a block of test steps such as "Update `tests/test_words.py` with
  tab cases" or "Add a case for an empty string" is flagged again as a block
  that adds no behaviour, and gets its tasks.md rewrite before the TDD RED
  gate fails it. A test-first block whose code step ends "so the tests pass",
  or is "Make the failing tests pass", is not flagged, nor is one whose code
  step is "Add case-insensitive matching".
- buildme: the README and SKILL.md now give the right number of tasks.md
  rewrites the plan review can cause.
- buildme: the plan review now rates a block that adds no behaviour from its
  severity scale, as every other finding, instead of being told it is
  critical; the tasks audit, which rewrites on any finding, no longer asks for
  a fixed severity either. The tasks prompt now says where criteria that
  only confirm earlier behaviour belong, instead of forbidding such blocks.

## [0.85.0] - 2026-10-20

### Fixed
- `/multiplai-dev:review`: a link on a `CLAUDE.md` or `coding-standards.md`
  line that says `binds` (as in "**binds on** any skill") now counts as
  binding, so that doc goes to the conventions finder before the
  non-binding ones. On a multiplai-dev change this puts
  `docs/untrusted-content.md` and `docs/degradation-contract.md` in the
  prompt, where `docs/degradation-contract.md` used to be cut for length.
- `/multiplai-dev:review`: a linked doc left out of the conventions finder's
  prompt for length is now named there, one line each ("Not included for
  length; open it if it applies: `<path>`"), so the finder can open it
  itself. Before, it left no trace in the prompt.

## [0.84.2] - 2026-10-20

### Fixed
- buildme: an epic child's command now carries `--epic-child`, so running a
  child's saved command again by hand still skips spec-extract and still opens
  the PR against the default branch. Before, such a re-run could start
  spec-extract and target a sibling child's branch once that sibling had
  pushed.
- buildme SKILL.md: the `--base-ref` row says that an epic child's PR targets
  the default branch, and the `EPIC:MERGE_WAITING` row names the two cases
  under `epic.merge: auto` where buildme left the PR open itself (checks
  pending after an hour, or a merge queue only queued it). In those cases the
  next step is a re-run, not a merge by hand: at once for pending checks, and
  once the queue has merged the PR for a queued one.
- buildme SKILL.md: the `--epic-child` row says that `BUILDME_EPIC_CHILD=1`
  does the same, for a child's command saved before this release.

## [0.84.1] - 2026-10-18

### Changed
- review: a test now checks that a `## Critical paths` list kept in a
  subdirectory's `coding-standards.md` (for example `billing/coding-standards.md`)
  still marks a trim suggestion on that path's tests as important. Review
  behaves as before; a change that made it read only the root
  `coding-standards.md` would now fail the test suite instead of quietly
  suggesting the removal of tests on a critical path.

## [0.84.0] - 2026-10-18

### Added
- review-viewer: a review whose `paths.json` names exactly the reviewed head
  commit and validates as code_facts' `paths.v1` gets a **Paths** tab after
  Checked. One card per entry point that reaches the change shows its kind
  and who runs it, its handler and hops (each opens the code at that line),
  where a broken trace stopped, the findings on it, and its e2e tests: the
  counted ones, mocked ones marked "not counted", or "no e2e test". Paths with
  findings come first, under a line counting the paths, those with findings
  and those with no e2e test, and a note when some test files could not be
  listed.
- review-viewer: each finding's page lists the paths it lies on, refuted
  findings included, each linked to its card; an id the file lacks shows as
  "not in paths.json".
- review-viewer: `walkthrough status` prints a `paths:` line (accepted, stale,
  invalid or none) with each accepted path's entry name, and the skill tells
  the session to name every path in the walkthrough's overview and lets steps
  follow a path's hops through the changed code.

### Changed
- review-viewer: a hop, entry or handler file of an accepted path can be
  opened in the code pane even when the review does not list it (a tree
  review of one directory, or a repository git could not list), and in a file
  over 4000 lines the lines a path points at stay in view.
- review-viewer: a review without `paths.json`, or with one for other commits
  or one that does not validate, opens exactly as before; a rejected file is
  logged once. review-viewer now depends on code_facts, only to validate the
  file.

## [0.83.0] - 2026-10-18

### Added
- review: every change review now maps its code paths before the finders run
  (a `--tree` or `--dir` review only with the new `--paths`), from each entry
  point through the functions it calls, and writes them to `paths.json` beside
  `findings.json`. At most 21 paths are kept, the ones covering the most
  changed lines first. What the mapping could not do is listed in
  `checks.json`'s new `coverage` list; it never stops the review.
- review: two new kinds of finder run by default on a change review, besides
  the dimension finders. A piece finder reviews one part of the diff (whole
  files, grouped by package), and a path finder follows one mapped path from
  its entry point to its last hop. Each writes a plain-text report
  (`report-piece-<n>.md`, `report-path-<id>.md`) that a second call turns into
  findings, which then pass the same citation gates, verification and merge.
  A failed or stopped call is asked again by `resume`, which never pays twice
  for a report it kept.
- review: each finding in `findings.json` lists the ids of the mapped paths it
  lies on (`paths`), and `review-<slug>.md` gains a `## Paths` section: each
  path's entry point, who invokes it, the findings on it and its e2e tests,
  then a line counting what the mapping left out.
- review: the `conventions` finder also reads the Markdown docs its
  `CLAUDE.md` and `coding-standards.md` files link, one level deep, and
  reports a changed line that breaks one of them.
- review: `--plan-only` and the start of a review print the piece and path
  finders' approximate cost (about $38 on a 227 KB diff, before the dimension
  finders and verification), a `WARNING:` when that is likely to pass the
  budget, and on the 50 USD default a `NOTICE:` that a large change review can
  stop partway. A budget stop says how many finder reports it kept; `resume`
  with a higher `--max-cost-usd` continues without paying for them again.
- review-viewer: `findings.json` findings may carry `paths`, and `checks.json`
  may carry `coverage` and each convert call's `items`; the viewer accepts
  them.

### Changed
- review: `--extra-finders` and `review.yaml`'s `extra_finders` have no
  effect and print a notice; the piece finders replace them.
- review: a change review costs more than before, because the piece and path
  finders run on every one; the 50 USD default can stop a large one partway.

## [0.82.0] - 2026-10-18

### Changed
- codebase-walkthrough: the trace stage now finds its entry points with
  code_facts' `map_paths`, so a walkthrough of a CLI or skill repository
  traces its argparse, click and typer commands, scripts, `__main__` modules,
  skill commands and hooks, not only web routes. The whole module is the
  scope: every entry point whose path reaches it is traced. Web routes,
  routes called from other repositories and Celery beat entries are still
  traced, now starting at their handler function.
- codebase-walkthrough: the "End-to-end scenarios" section is replaced by a
  **Paths** section, rendered by code. For each path it gives the entry
  point, its kind and who invokes it, the hops as `path:line` links with ids
  (`p2h3`), where the trace stops and why, and the e2e tests that run it, or
  "no e2e test". The coverage table's "Scenarios cut short" row becomes
  "Paths traced" and "Paths cut short". Each path still opens with one or two
  paragraphs written from its checked hops; a hop cited in prose links to
  its line and to the hop in the Paths section.
- codebase-walkthrough: `--scenarios` now caps the number of paths, and its
  default rises from 6 to 21; a value below 1 is rejected. Every entry point
  left out, over the cap or for another reason, is listed with its stage and
  reason in a new **Left out** table under Coverage, and counted in the
  "Entries left out" row.
- codebase-walkthrough: a trace that fails for one entry point no longer
  costs the others; it is listed under **Left out**. If path finding fails as
  a whole, the Paths section says why and the walkthrough is still written.
  Resuming a run saved by an earlier version, after its trace stage, traces
  again rather than reporting no paths.
- codebase-walkthrough: trace agents can now read only the target repository's
  snapshot (`read_roots`), not any absolute path. Before, the working directory
  set where relative paths started but did not limit reads.

### Added
- codebase-walkthrough: in human mode a run also writes `<name>-paths.json`
  beside the walkthrough: the paths, the entries left out and their e2e
  tests as a `paths.v1` document, written by code_facts' `write_paths_file`.
  The run prints its path as another `walkthrough:` line. If it cannot be
  written, the walkthrough is kept and the error is recorded.
- codebase-walkthrough: `check` (and `check --at-head`, which buildme uses to
  find stale walkthroughs) names a failing path hop by its id, as in
  `FAIL p2h3 <repo>/<path>:<lines>: …`, fails a hop cited in prose whose
  anchor is missing, and prints `check: N hops checked` on stderr. Its stdout
  summary line is unchanged, and walkthroughs with the older "End-to-end
  scenarios" section are checked as before.

## [0.81.0] - 2026-10-18

### Added
- code_facts: `map_paths` maps the code paths of a snapshot, from each entry
  point to the code it reaches, and `write_paths_file` writes the result as a
  `paths.v1` document (contract: `lib/code_facts/code_facts/schema/paths.v1.schema.json`).
  No skill uses it yet.
  - Entry points are found statically, without importing or running the code.
    They are argparse subcommands, click and typer commands,
    `[project.scripts]` keys, `__main__.py` files and `if __name__ ==
    "__main__":` guards, `python -m` commands in a `SKILL.md`'s shell fences,
    `"type": "command"` hooks in `hooks.json` or `.claude/settings*.json`, and
    any seeds the caller passes. Entries that run the same function become one
    entry, with the others listed as its invokers. A candidate that is examined
    but not emitted is reported as a diagnostic.
  - Each entry says who invokes it, in `invoked_by`: an agent following a
    `SKILL.md`, Claude Code on a hook's event, a person or a script running a
    command, the caller of a seed, or, for an entry the agent found, another
    program, skill or agent.
  - With `fallback=True` (the default), one read-only agent is asked for entry
    points the scan missed. An entry it lists is kept only when its citation
    re-reads at the commit and no discovered entry already holds that line or
    that function.
  - Each entry is traced into a linear path of up to 20 hops. Every hop's
    citation is checked, and the agent is asked again once when a hop fails.
  - Given `changed_lines`, the run is scoped to the change. Entries whose
    imports cannot reach a changed file are dropped. At most `trace_limit`
    entries (default 30) are traced, and at most `cap` paths (default 10) that
    cover a changed line are kept. Every dropped entry is listed with its
    stage and reason.
  - Each kept path lists the tests that invoke its entry, found by reading
    them and not by running them: a direct call, an argparse argv, a click or
    typer runner, `subprocess` or `runpy`. Tests that patch the symbol at hop 2
    are listed separately. Tests that patch out the entry's handler are not
    listed.
  - With `cache_dir`, every model answer is kept under
    `<cache_dir>/paths-answers/`, so a rerun asks only the questions it has no
    answer for. A failed call is asked again on the next run.

### Changed
- code_facts depends on `jsonschema`, which `write_paths_file` uses to
  validate the document before it writes it.

## [0.80.0] - 2026-10-18

### Added
- `tdd.test_writer_max_turns` and `tdd.mutation_test_writer_max_turns` in
  `specs/config.yaml` set each test writer's turn cap (default 30), and each
  prompt now says how many turns the agent has.
- The test writer's context lists the signatures of the existing Python code
  its block names: classes, fields, methods, functions and constants, the
  fixtures and helpers of the nearest `tests/` directory, the tests that
  already call that code, and one short test file, in at most 40,000
  characters. A block that names `stages/trace.py` now also matches that
  file in the codebase analysis and the block facts.

### Changed
- A test writer that runs out of turns with test files on disk no longer
  fails its block: the files go to the quality and RED gates. A retry is told
  which files are its drafts. A mutation round that runs out of turns keeps
  tests that pass the round's checks.
- A resumed block whose tests are committed and passed the RED gate goes
  straight to the implementer instead of writing its tests again.
- A FAILED build's resume no longer runs the interview again when
  `interview.md` and its summary are saved.

## [0.79.2] - 2026-10-17

### Changed
- buildme no longer writes `unknowns.md` by default. Its dependency scan treats
  every backtick-quoted word in the proposal and design as a new dependency,
  so it ran one research call per word such as `assert`, `followed` or `hops`.
  It then pasted the result, up to 1.1 million characters, into every test
  writer's prompt. No build checked had a real new dependency among its entries.
  `explainers: {enabled: true}` in `specs/config.yaml` turns it back on.
  The fix is tracked in #375.

## [0.79.1] - 2026-10-16

### Security
- `/multiplai-dev:review`'s agents can no longer read files outside the
  review's snapshot. Before, an agent could open any file you can read by
  giving `Read`, `Grep` or `Glob` an absolute path, although the skill said
  each agent reads a snapshot. Now multiplai-core 0.23.0 denies any of those
  calls whose path resolves outside the stage's snapshot folder, including
  through `..` or a symlink. The dependency source in `.review-deps/` is inside
  the snapshot, so finders can still follow calls into it. `WebFetch` and
  `WebSearch` are unchanged.
- multiplai-core 0.23.0 also sends a large prompt inline. A review prompt over
  80,000 bytes no longer gives its agent an extra `Read` of a temporary file.

## [0.79.0] - 2026-10-16

### Added
- `/multiplai-dev:review` now gives its finders and verifiers the source of the
  reviewed repository's git dependencies, at the commits its `uv.lock` pins. It
  clones each one (over `https` or `ssh`) into the skill's state folder and puts
  its files in the snapshot under `.review-deps/<name>@<commit>/`, so a finder
  can follow a call from the change into, say, `multiplai-core`. A finding may
  cite a dependency's line after a line of the repository, and that line is
  checked against the dependency's git history at the pinned commit. A finding
  is never about a dependency's code alone.
- A dependency the review cannot clone, or whose source is over 50 MB
  (`deps_max_mb` in `review.yaml`), is listed under **Needs you** and the review
  goes on without it.
- `review --no-deps` and `resume --no-deps`, or `deps: false` in `review.yaml`,
  turn the dependency source off.

## [0.78.2] - 2026-10-16

### Security
- `/multiplai-dev:pr`'s model call now has no tools at any prompt size. Before,
  a description prompt over 80,000 bytes made multiplai-core write the prompt
  to a file and give the model `Read`, which could open any file you can read.
- `/multiplai-dev:pr edit <url>` now writes only to the PR at that URL. It
  names the PR to `gh pr edit` and `gh pr comment` by its URL, and refuses,
  before reading the diff or writing anything, when the URL's repository is not
  the checkout's `origin` or when `gh` gives no URL. Before, the description of
  PR 7 in one repository could be written onto PR 7 in another.
- `/multiplai-dev:pr edit --push` now pushes only to the PR's own branch. It
  refuses a PR whose head is on a fork (push to the fork yourself, then run
  `edit` without `--push`) and a head branch that is not in the checkout, and
  pushes `refs/heads/<head>` to the branch of the same name. Before, a fork's
  PR pushed your unrelated same-named branch to `origin`.
- `/multiplai-dev:pr open` and `edit` refuse a branch name that is not a valid
  Git branch name or that starts with `-`. Before, a `--head` or a PR head such
  as `--all` reached `git push` as an option.
- `/multiplai-dev:pr` template discovery skips a template file, template
  directory or location whose resolved path is outside the repository, such as
  a symlink to another folder, and says so in a `PR_DIAGNOSIS:` line. Before,
  that file's text was copied into the PR description.

## [0.78.1] - 2026-10-16

### Fixed
- `/multiplai-dev:codebase-walkthrough` no longer deletes `progress.log` when you
  run it again on the same module and commit. The earlier run's log moves to
  `progress-<UTC time>.log` in the same run directory, and the new run writes a
  fresh `progress.log`.
- `/multiplai-dev:review` no longer deletes a `progress.log` that no earlier
  round in `rounds/` holds, such as one from a run that stopped before it saved
  any state. That log moves to `progress-<UTC time>.log` beside the rounds. A
  log that a kept round already holds is still deleted, as before.

## [0.78.0] - 2026-10-16

### Added
- `/multiplai-dev:buildme`: `efforts:` in `specs/config.yaml` sets a step's,
  a stage's or every step's effort, with the same keys as `models:`.

### Changed
- buildme runs every step that no setting names on `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT`, else on the model and effort of the Claude Code session
  that started the build, else on the CLI's default. Before, it ran Opus.
  `models:`, `efforts:`, `[buildme.<stage>]` and `[buildme]` in
  `multiplai.conf` still win, in that order.
- The steps that passed no effort (merge, findings triage, spec delta and
  self-check, prototype, respec, explainers) now take their stage's effort.
  `docs_update` takes the agent stage's effort instead of the spec stage's.
- buildme's tier follows that model; `CLAUDE_MODEL` is no longer read. With
  no model anywhere the tier is `standard`.
- spec-extract, review, codebase-walkthrough and pr follow the same order
  (their own setting, then `MULTIPLAI_MODEL` / `MULTIPLAI_EFFORT`, then the
  session).
- Skills no longer set `model:` or `effort:` in their frontmatter: each runs
  on the model and effort of the Claude Code session you start it from.

## [0.77.0] - 2026-10-16

### Added
- `/multiplai-dev:buildme`: `--force` starts a new build of a change that
  already published, on the next free branch (`buildme/<change>-2`, …).
  Without it, running `build` again for a published change exits `3` with
  `BUILD:COMPLETE:<path>` and a line naming its branch and PR, and writes
  nothing; the skill asks you before it re-runs with `--force`.
- buildme prints `BUDGET:WARN:<percent>:<usd>` when a build has spent 80% of
  its budget, and writes the warning to `build-progress.md`.

### Changed
- buildme keeps a finished build's checkpoint as
  `.build-state.complete.json` instead of deleting it, and never deletes
  `build-progress.md`. A resumed run appends a `RESUMED` section under the
  same header; a stop by SIGTERM, SIGHUP or Ctrl-C writes `INTERRUPTED by
  <signal>` and saves the checkpoint with the spend so far; every `build` and
  `findings` run ends with a `BUDGET` section listing tokens and USD per step.
  A resume in any phase keeps the saved spend.
- The progress file's header names the model and effort the build started
  with and where each came from (the session, `MULTIPLAI_MODEL`,
  `multiplai.conf`, or the CLI default).
- A failed TDD build prints `ERROR:TDD build failed (exit <n>)`. A
  standalone `tdd` run gets the same stop handling and `SPEND:` line as
  `build`.
- buildme, spec-extract, review, review-viewer, codebase-walkthrough and pr
  write every line their own modules log, and multiplai-core's, to one log
  file per day (`<data_dir>/logs/<name>-<UTC date>.log`), kept 365 days (`MULTIPLAI_LOG_RETENTION_DAYS`, multiplai-core 0.22.0).
  Each run logs the model and effort it pinned from the session at start.
- A model or effort set for a step is used as given; multiplai-core 0.21.0
  no longer lowers it to a ceiling.
- An epic child whose earlier attempt already published is counted as built
  when its PR is found, instead of failing on `BUILD:COMPLETE`.

## [0.76.0] - 2026-10-15

### Added
- `/multiplai-dev:buildme`: the full-suite test runs (the baseline gate, the
  integration gate after each block, and epic mode's merge tests) add
  `-n auto` to a pytest command when pytest-xdist is installed in the
  environment pytest runs in, so the suite runs on every CPU. `tdd.parallel:
  off` in `specs/config.yaml` keeps the serial run (default `auto`). A command
  that already chooses its workers (`-n`, `--dist`, `-p no:xdist`, `--pdb`,
  also in `addopts` or `PYTEST_ADDOPTS`), one where pytest will not load
  xdist (`PYTEST_DISABLE_PLUGIN_AUTOLOAD` or `--disable-plugin-autoload`
  without `-p xdist`), or one whose Python buildme cannot tell, runs as
  written. The narrow runs inside a block's loop are
  unchanged, and so are jest and vitest, which already run files in
  parallel.

### Changed
- buildme's own suite: `pytest-xdist` is in the `dev` extra, and CI runs the
  suite with `-n auto`.

## [0.75.0] - 2026-10-15

### Added
- `/multiplai-dev:review`: `--extra-finders` (or `extra_finders: true` in
  `review.yaml`) adds finders sized by the change to its first review round:
  one per ~600 changed lines in diff order, a larger file alone, lockfiles
  left out, at most 6. Each reads only its part of the diff, the enclosing
  functions and the direct callers and callees, and lists every HIGH or
  MEDIUM correctness defect there; its findings are verified, merged and
  assessed like the others'. A later round of the same change runs none (a first
  review that stopped before writing its findings does not count), and
  `resume` asks only the chunks whose finder had not returned. Off by
  default; `--tree` and `--dir` refuse it.
- `/multiplai-dev:buildme`: an epic child's PR review passes
  `--extra-finders`, so each PR's first review gets these finders.

## [0.74.0] - 2026-10-15

### Changed
- `/multiplai-dev:buildme` epic mode: each child PR gets at most 3 reviews
  (`epic.max_review_rounds` now defaults to `3`, down from `6`). Each fixer
  launch runs one fix round, where it used to run every round the PR had
  left, so after every review that leaves Code HIGH or MEDIUM findings
  buildme records whether another round is worth it (`next` and `next_reason` on the review's record,
  decided from the review's own fields, with no model call). An open HIGH
  earns another round, and parks the child at the cap, as before. A MEDIUM
  earns one only when the review's verifier rated it `breaks-users` or
  `breaks-business`, or did not rate it (new `epic.round_worth: breaks`, the
  default; `any` keeps the old rule, every MEDIUM). Otherwise, and at the cap
  with only MEDIUMs left, where the child used to park, one final fixer round
  runs with no review after it (a fresh round, past any round an earlier
  fixer left open), the PR goes to the merge step (under `manual`
  it waits for you, as before), and buildme files one issue on the child's
  repository listing each finding left and saying the last fixes had no
  review. When every finding was already on the PR from an earlier round,
  that fixer finds nothing new to act on and runs no round; the PR still
  goes to the merge step, and the issue says no fix ran.
- `/multiplai-dev:review`: the verifier rates a defect `breaks-users` or
  `breaks-business` only when normal use reaches it, it does harm with no
  second failure, and nothing else in the code, CI or the documented workflow
  stops it first; otherwise `correctness-only`.

### Added
- `/multiplai-dev:buildme-findings`: `--no-review`, a round that fixes, pushes
  and replies with no re-review. The epic's final fix uses it.

## [0.73.0] - 2026-10-15

### Changed
- `/multiplai-dev:buildme`: the test writer is told to write fewer, more
  valuable tests (every WHEN/THEN scenario still gets one; beyond that, only a
  test that would catch a real defect on a path that matters, and none another
  test already covers), and the test writer and both implementer tiers are told
  to keep comments short: say why, never narrate the next line or describe
  history, and stay shorter than the code. The mutation test writer and the
  refactorers keep their prompts.

## [0.72.0] - 2026-10-15

### Added
- `/multiplai-dev:review-viewer`: the Findings list shows a review's advisory
  **Trim** section after Docs, holding the trim finder's suggestions, and
  reads each finding's section from `findings.json` when the review wrote one,
  so a trim finding is never listed under Code. Reviews made before 0.71 have
  no section field and are split by topic as before. The help names the
  `trim` finder, says Trim never blocks a merge, and says a review's own
  section wins over the finding's topic.

## [0.71.0] - 2026-10-15

### Added
- `/multiplai-dev:review`: a `trim` finder judges the tests and comments a
  change adds or edits, and lists in a new **Trim (advisory: never blocks a
  merge)** section the tests worth removing, merging or shortening and the
  comments that are longer than their code, narrate what the code does, or
  describe history. Its findings are always LOW and are posted to a PR only
  when accepted in a decisions file. A test is never suggested for removal
  when the verifier rates what would break without it `breaks-users` or
  `breaks-business`, or when it cites a path listed under `## Critical paths`
  in a `coding-standards.md`; those suggestions go to the appendix, labelled
  `important`, also when the merge stage folded such a suggestion into
  another. A trim finding never stands in for the other finders: when every
  one of them fails, the review fails as before. A change review runs the
  finder unless `review.yaml` sets
  `trim: false`; `--tree --trim` and `--dir --trim` run it over the existing
  tests and comments.
- `findings.json` carries a `section` field (`code`, `tests`, `docs` or
  `trim`) on every finding, and the assessment label `important`
  (`review-viewer/schema/findings.v1.schema.json`). review-viewer never shows
  or counts an `important` finding, as with `low-value`, and its help explains
  the label. Older files have neither.

### Changed
- `/multiplai-dev:buildme`: an epic reads each finding's `section`, so a trim
  finding never blocks a child PR's merge and is never posted by `--auto`.
  A findings.json without the field is read as before.

## [0.70.2] - 2026-10-14

### Fixed
- `/multiplai-dev:buildme`: a build that failed after its first commit can
  be run again. The re-run uses the failed build's own branch and worktree
  instead of stopping with `Branch ... already exists with N commit(s)`, so
  an epic's failed child can be relaunched too. A build that failed before
  any commit also re-runs in its own worktree, no longer in a new `-2` one.
  A re-run stopped before its interview finished resumes in that worktree
  too, and a build that earlier moved to a new `-2` worktree re-runs there
  (the worktree it last saved its state in) when it fails again.
  The re-run stops with exit `1`, and changes nothing, when the worktree is
  no longer on the build's branch, or holds an uncommitted change the failed
  run did not leave. A build that failed under 0.70.1 or earlier has no
  record of what it left: with no commit of its own it re-runs in a new `-2`
  worktree as before, and with a commit and uncommitted changes it stops
  until they are stashed.
- `/multiplai-dev:buildme`: a build resumed from inside its own worktree
  commits its spec stage on the build's branch again; before, every
  spec-stage commit was skipped.

## [0.70.1] - 2026-10-14

### Fixed
- buildme: an implementer that correctly fixes a wrong test no longer fails
  its block. The test-integrity gate accepts a changed test only when the
  agent's report says `TEST CHANGE REQUIRED: <reason>`, but no prompt told
  the agent so: in a live epic test, two builds of the same child each fixed
  a test that no implementation could pass, explained why, and failed. The
  implementer prompts (which also serve the review-fix and fix-on-pass
  agents) and both integration-fix prompts now name the marker and say to
  put one such line per changed test at the end of the report. The
  integration fixer's report is now read by the gate too.
- buildme: since the prompts now show the marker, only a line that starts
  with `TEST CHANGE REQUIRED:` counts as a declaration, and a line left as
  `<reason>`, `none`, `no` or empty does not, also with a note after it
  (`None (no test files modified)`). Before, a sentence that only mentioned
  the marker, or an echo of the template, passed a silent test change. The
  line may open with a bullet, a number (`1.`, `2)`) or a quote (`>`), the
  bold may close before the colon (`**TEST CHANGE REQUIRED**:`), and a marker
  alone on its line takes the next non-blank line of the same agent's report
  as its reason: the gate parses each agent's report on its own, so a marker
  left bare at the end of the implementer's report never takes an integration
  fixer's first line as its reason.
- buildme: a `TEST CHANGE REQUIRED:` line after a report's `SEARCHED:` or
  `SURPRISES:` slot is no longer read as part of that slot, so it no longer
  shows up in the review-round record, the findings reply or
  implementation-notes.md.

## [0.70.0] - 2026-10-14

### Added
- `/multiplai-dev:buildme-findings` takes `--max-usd <usd>`: before each round
  with comments to act on, it stops once the run has spent that much,
  counting its own model calls and the reviews of the PR it ran (from the
  review ledger), and prints `FINDINGS:BUDGET:<rounds>:<usd>`. A round that
  has started always finishes. Each run counts from 0.
- `/multiplai-dev:buildme-findings` takes `--severities <list>` (for example
  `HIGH,MEDIUM`) and `--code-only`: with `--auto`, a re-review posts only
  findings of those severities, or only Code findings, so no round runs for
  the others.

### Fixed
- buildme epic mode: `epic.max_usd` now bounds a fixer once it has started.
  Before, the cap was checked only before each launch, and a fixer started
  under it ran all its rounds: in a live test with `epic.max_usd: 20`, one
  child's fixer took the epic to $74. The fixer is now given what is left of
  `epic.max_usd`, less what other fixers still running were given; while
  they hold all of it, a fixer waits, and no build, review or merge agent
  starts, including a review step's own next review or resume. A fixer that
  stops at its share while the epic's spend is under the cap runs again,
  with its share recomputed, once the other fixers end. When the spend has
  reached the cap the run exits 3 with `EPIC:BUDGET:<spent>` and launches
  nothing more. The child is not parked: a re-run after `epic.max_usd` is
  raised runs the fixer again. A fixer whose last round
  left nothing to act on ends as done, so its PR still merges. The round
  that crosses a fixer's share still finishes, so an epic can end up to one
  fix round (a fix and a review) per fixer running at once past the cap.
- buildme epic mode: the fixer no longer runs rounds for findings the epic
  would merge with. The epic blocks a merge only on HIGH and MEDIUM Code
  findings, but its post and its fixer's re-reviews posted every Code
  finding and Tests findings on source files, and the fixer acted on each
  one. Both now post only HIGH and MEDIUM Code findings.

## [0.69.0] - 2026-10-14

### Added
- buildme epic mode, `epic.merge: auto`: buildme now merges each child's PR
  itself. After merging the default branch into the PR's branch, testing and
  pushing, it waits for the PR's required checks (`gh pr checks --required`,
  re-read every 30 seconds while one is pending; when no check is required,
  the PR's own checks gate instead) and runs
  `gh pr merge <n> --merge --match-head-commit <sha>`, never `--admin`, so a
  commit pushed to the PR after the merge step checked it is never merged.
  Right after its own push it waits up to two minutes for GitHub to show
  the new commit as the PR's head and report its checks, and reads no
  checks of an earlier commit; a PR whose head is still another commit
  after that parks the child. Checks still pending after an hour leave the PR
  open: the run exits 3 with `EPIC:MERGE_WAITING`, and the next run waits
  again. A failed or cancelled check parks the child, naming the check. The
  child counts as merged only once GitHub says the PR is `MERGED`, so a PR
  a merge queue only queued is waited on. A merge GitHub refuses as
  `BEHIND` starts the merge step over and merges the default branch's new
  commit in; any other refusal parks the child with GitHub's message and the
  PR's `mergeStateStatus`, and a refused merge is not tried again within one
  run. Merges in one repository still run one at a time, so two children
  that both raised the version merge in turn: the second merges the first's
  change in, and one agent call raises its version past it and keeps both
  CHANGELOG entries. Under `manual` (still the
  default) nothing changes: the run exits 3 with `EPIC:MERGE_WAITING` until
  a person merges.

## [0.68.0] - 2026-10-14

### Added
- buildme writes a baseline of what the code already does before it plans a
  change. On a project whose `specs/registry/` is empty, the codebase analysis
  phase now runs `/multiplai-dev:spec-extract` on the project's Python code at
  the build's starting commit, with `--apply`, and commits the capability files
  it writes to the build branch as `docs(specs): baseline registry from
  spec-extract at <sha12>`, printing `SPEC_EXTRACT:BASELINE <n>` (with
  `uncommitted` after it when there is no build branch to commit to, as with
  `--no-worktree`, or the commit failed; a spec-only build then leaves the
  files for you to commit, and a full build commits them with its first
  block's commit). It needs a trusted repository (`--trust-repo`), never
  runs in `only` mode or in an epic's child builds, and is turned off with
  `spec_extract: false` in `specs/config.yaml`. In an epic the parent runs it,
  and each child with no dependency, in the epic's own repository, branches
  from the parent's commit that holds the baseline, so the registry reaches
  main with the first merged child PR; every child's PR targets the default
  branch. A child that waits on a dependency branches from origin's default
  branch after that merge, as does every child once that branch holds a
  registry, and every child when the baseline commit would carry anything
  else origin's default branch does not hold (a feature branch, unpushed
  commits, a commit added before a resume, other files in the same commit),
  and a child built in another repository builds without a baseline. `spec_extract_max_usd`
  (default `80`) caps its spend; what it spends is counted in buildme's
  `SPEND:` line, but not in an epic's `epic.max_usd`, which counts only the
  children's builds, fixes, merges and reviews. A stopped build resumes
  spec-extract at the same commit, so it pays only for the calls that had not
  returned, and does not run the codebase analysis again. When spec-extract
  fails the build goes on without a baseline (`SPEC_EXTRACT:NOT_RUN exit=<n>`);
  when another spec-extract run still holds the folder
  (`SPEC_EXTRACT:RUNNING`), or `specs/registry/` holds uncommitted changes
  after a failure, the build exits `1` without leaving the phase.
- spec-extract prints `SPEND:<usd>:<tokens>` last: what that invocation's model
  calls cost.
- Spec generation reads `specs/registry/`. When the registry holds any
  capability, the proposal prompt gets the registry's capability names and is
  told to list a capability the change alters under Modified Capabilities with
  exactly that name; the requirements prompt gets the registry file of the
  same name and is told to write a changed requirement under
  `## MODIFIED Requirements` and a removed one under `## REMOVED Requirements`;
  the design and tasks prompts get the registry files of the change's own
  capabilities. Each names the other capabilities, and the registry text is
  capped at 60,000 characters, dropping whole files and naming them with their
  requirement names. With an empty registry the prompts are unchanged.

## [0.67.0] - 2026-10-14

### Changed
- buildme no longer runs the project's whole test command inside each
  block's TDD loop. The RED check, and the test writer, implementer and
  refactorer for a block, now run only the test files the block added or
  changed, plus the test file named after each source file it changed
  (`test_<module>.py` / `<module>_test.py` for pytest, `<name>.test.<ext>` /
  `<name>.spec.<ext>` for jest and vitest). Tests are never picked by what
  they import. The integration gate, the baseline gate and the epic merge
  step still run the whole command, so a block is still only done when the
  whole suite passes.
- The narrow run applies only when the test command is pytest, or jest or
  vitest (directly or through an `npm test` script that is a single jest or
  vitest command, whose paths go after `--`). Any other command (a task
  runner such as tox, even behind `uv run`, or a chained command), a block
  that selects no test file, a changed test file the naming cannot pick out
  (Django's `tests.py`, `__tests__/`), a run that collects coverage, a Node path
  jest would read as a regex, a command that already names what to run
  (paths, a jest or vitest filter, pytest `addopts` paths, another pytest
  config or rootdir, a project `python_files`), a pytest config that cannot
  be read, a selected pytest file below another pytest config or `setup.py`
  than the whole run's, or a selected pytest file under a `conftest.py` that
  sets `collect_ignore` or `pytest_ignore_collect` runs the whole command as
  before. pytest's config is read from `pytest.toml`, `.pytest.toml`,
  `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini` or `setup.cfg`, in
  pytest's own order. Set
  `tdd.narrow_tests: false` in `specs/config.yaml` to always run the whole
  command.
- The narrow run takes only files the whole command collects: never one under
  `.venv`, `venv`, `node_modules` or another tool directory, and for pytest
  only one under its `testpaths` and outside its `norecursedirs` and
  `--ignore`, because pytest runs any file named on its command line.
- The test writer, implementer and refactorer are also told how to add the
  test files they write and the test file named after each module they
  change, since their command is fixed before they start. The implementer
  prompts now say to run the given test command, not "the full test suite".

## [0.66.1] - 2026-10-14

### Fixed
- buildme now tells the task writer not to write a block that only confirms
  or verifies what an earlier block already built, and catches such blocks
  before the TDD build. Such a block's tests passed before it was implemented,
  so the TDD RED gate failed it and the whole build stopped. Every block must
  now add behaviour its tests can fail on. The tasks-shape audit, the plan
  review and the prototype feedback pass (the last rewrite of `tasks.md`
  before the build) flag a block as `no-new-behaviour` when it produces
  nothing and its own text says it adds no code: it says an earlier block
  already satisfies it, it says it needs no code, it is test work that
  changes only test files, or it adds tests and leaves every file it names
  unchanged, with no step that writes code. A block that verifies a
  signature or confirms an overwrite is not flagged for the verb, nor for
  naming only its test file, nor for writing its tests before its code, nor
  for saying another block already handles a side concern. A flagged block sends `tasks.md`
  through one rewrite, which is told to fold the block into the one that
  builds the behaviour or, if it does add behaviour, to name its code. The
  plan review still runs this check, and rewrites what it flags once per
  build, when its model call fails; a resumed build then asks the model
  again. A block still
  flagged after the rewrite is named in the log as
  `PHASE: no_behaviour_blocks_remain`.

## [0.66.0] - 2026-10-14

### Added
- Epic mode builds children in other repositories. A child's `repo` in
  `split.md` is the path of a local checkout, absolute or relative to the
  epic's repository root. `--approve-split` exits `1` naming the child when
  that path is not the top of a checkout with an `origin` remote. The child
  builds, is reviewed, fixed and merged in that repository, and its
  repository's `specs/config.yaml` is refused (exit `2`) when it turns off
  `git.worktree` or `git.push`, as the epic's own is; merges in two
  repositories run at the same time; a child that depends on one in another
  repository waits for its merge. A checkout gone after approval parks its
  child with `repo <path> not found`, for good; the reviews its PR already
  had still count in the epic's spend.

### Changed
- Epic mode: a child's `repo` is now a local checkout's path, no longer
  `owner/name`. 0.65.0 asked the split for `owner/name` and ignored it,
  building every child in the epic's own repository. An epic approved on
  0.65.0 with an `owner/name` repo parks that child on its next run with
  `repo <path> not found`; split it again under a new `--change` name.
  The reviews that child's PR already had, which ran in the epic's own
  repository, still count in the epic's spend.

## [0.65.0] - 2026-10-14

### Added
- Epic mode's merge step (`epic.merge: auto`) now resolves conflicts that
  are only in the package's own version fields (a marketplace manifest's
  plugin versions included; a nested component's, such as a devcontainer
  feature's, or a compose file's format version, is not one), CHANGELOGs
  and lockfiles, with one agent call (model step `merge`, counted against
  `epic.max_usd`). It also raises a version that
  the child and the default branch both raised to the same value, which git
  merges without a conflict, to the next one after the default branch's. A
  conflict in any other file, a deleted or binary side included, still parks
  the child, as does the agent changing any other file, staged or not; the
  park then names the `checkout` or `rm` that undoes those changes before
  `git merge --abort`. The resolution is checked against each file's base
  and both sides: each version is the next after the default branch's where
  both raised it, both CHANGELOG entries are kept in full with the child's
  under the version written, and a regenerated lockfile holds only either
  side's packages and values (its manifest hash aside), loads, and pins each
  package (by hash or git revision), under its own name, as one side does.
  A merge commit holding only such resolutions is not reviewed again. With
  `epic.max_usd` reached, only a merge that would need the agent is held
  back. A change left in the worktree that the merge did
  not make parks the child before the tests run.

## [0.64.0] - 2026-10-13

### Added
- Epic mode merges the default branch into each child's PR once its review
  leaves no blockers, under `epic.merge: auto`: in the child's worktree, one
  merge at a time per repository, only when the worktree holds nothing but
  buildme's own files. Any conflict parks the child, naming the files; this
  release resolves none. The project's tests then run (`tdd.test_command`,
  else the discovered command), and the merge commit is pushed, never forced.
  A failed test run, a push rejected because the branch moved, or a refused
  check parks the child; a later run tries it again once its worktree has no
  merge in progress and no uncommitted change. Any other failed push, or a
  failed fetch, leaves the child `unknown` (exit `1`) for the next run to try
  again. A merge commit holding more than git's own merge, such as a
  person's, is reviewed again. A run stopped during the merge saves the
  worktree's state; the next run aborts and merges again only when nothing
  else changed there, and otherwise parks with the command to run. The PR
  itself is not merged yet: it waits for a person.

### Changed
- Under `epic.merge: manual` (the default), a child PR whose review left no
  blockers is now waited on: the run exits `3` with
  `EPIC:MERGE_WAITING:<urls>` naming every such PR still open, where it
  used to exit `0` with the PR open. A failed or `unknown` child still exits `1`.

## [0.63.0] - 2026-10-13

### Added
- Epic mode fixes each child's PR. A review that leaves open Code HIGH or
  MEDIUM findings below `epic.max_review_rounds` posts the findings `--auto`
  would accept (`review_pipeline post --decisions`, the decisions file next to
  the review, outside the worktree), then runs `/multiplai-dev:buildme-findings`
  unattended in the child's worktree (`findings --auto --trust-repo
  --max-rounds <M>`, where `<M>` is the fixer's rounds whose review finished
  plus the review rounds the PR has left). When the fixer ends, its last review
  is reused and the round count goes on from the review ledger; blockers on it
  start one more post and fix, and at the cap the child parks. A failed fixer
  (`FINDINGS:FAILED`, with its reason, also read from the log of one whose end
  was not recorded), a failed post, or a review folder with no
  `findings.json` parks the child. A re-run after the post runs the fixer,
  not a review; a post or fixer left running by a stopped run is waited for
  and then run again, never two at once; a fixer, post or review stopped by
  SIGKILL, SIGTERM or SIGHUP (also as `uv run` reports it, 128 + signal) is
  run again by the next run. A fixer that ends with `FINDINGS:DONE` or
  `FINDINGS:MAX_ROUNDS` and leaves the head where it was parks the child with
  its blockers. Any `findings` on the PR's number, one started by hand
  included, is waited for before a fixer starts. The post and the fixer count
  against `epic.max_parallel` and `epic.max_usd`.

## [0.62.0] - 2026-10-13

### Added
- Epic mode reviews each child's PR with `/multiplai-dev:review`, run as a
  subprocess, at most once per head. A head the review ledger already holds a
  complete review of is not reviewed again (the record says `reused: true`); a
  review another process is running is waited for, asking every 60 s; an
  interrupted one is continued with `review_pipeline resume`. Each review's
  record holds the head, base, review directory, cost, `pr_round`, `reused`,
  `conflicts` (recorded, never blocking) and `blocking` (open Code HIGH and
  MEDIUM findings). At `epic.max_review_rounds` with blockers, or when the
  review fails any other way, the child parks with the reason and stays
  parked. A review counts against `epic.max_parallel` and `epic.max_usd`, and
  the summary's `rounds` is each PR's review round count. SIGHUP leaves a
  running review running, as it leaves the children. The review fetches
  (`--fetch`), so a base moved by a sibling's merge does not park the child.
  While a review of the head holds the review skill's run lock, nothing is
  launched; the coordinator waits and reads the ledger once the lock is free,
  so a review that ends meanwhile is never run twice. A commit pushed as a
  review starts no longer parks the child: the review reviews the new head,
  and the coordinator reads the PR again and uses that review. Ctrl-C or
  SIGTERM stops a review this run started; a review an earlier run left
  running, or one another process runs, is not signalled and keeps running.
  A review that cannot fetch, or cannot read the PR from GitHub, does not park
  the child: the child is `unknown` (the run exits 1) and the next run asks
  again. A child whose worktree was removed is reviewed from the source repo
  instead of parking.

### Changed
- Five assertions in the epic B1 tests now look only at build records, and the
  B1 stop-and-resume tests expect each child's review records after its builds.
  What they check for builds is unchanged.
- The epic tests' fake build adds its PR to the shared PR file under a lock,
  so two builds ending at once both keep their PR.

### Fixed
- Epic records past number 999 are seen, in number order.

## [0.61.0] - 2026-10-13

### Added
- `build` and `findings` print `SPEND:<usd>:<tokens>` as their last line on
  every exit but a kill (success, failure, exit 3, an error, Ctrl-C, SIGTERM,
  SIGHUP): what that process spent on model calls. A resumed build reports
  only what it spent itself, and `findings` with several rounds reports all
  of them. SIGTERM and SIGHUP now end `build` and `findings` the way Ctrl-C
  does (exit 143 or 129), so an epic stopped by SIGTERM, which passes the
  signal on to its children, keeps what each child spent. An epic stopped by
  SIGHUP still leaves its children running for the next run to wait for, and
  now writes its run summary and exits 129. Under `nohup`, where SIGHUP is
  ignored, a closed terminal still does not stop the epic.
- The pr skill's `open` and `edit`, and the codebase walkthrough's `run` and
  `resume`, print `SPEND:<usd>:<tokens>` last: what their own model calls
  cost. buildme adds that line to its own `SPEND:`, so the PR description's
  model calls and a walkthrough regeneration count; when one of them printed
  none (killed or timed out), buildme's line ends `:incomplete`.
- `epic.max_usd` is enforced. An epic's spend is what each child build
  printed on its `SPEND:` line plus the reviews of the children's PRs in the
  review ledger: each review run once, so a review resumed after its own
  budget stop counts once, and a fresh review of an already reviewed head is
  added.
  Before each launch, once the spend has reached `epic.max_usd`, nothing more
  is launched: the build waits for the children already running and exits 3
  with `EPIC:BUDGET:<spent>`. A re-run stops again until the ceiling is
  raised.
- `epic.json` and each `runs/<NN>/summary.json` hold the epic's
  `spent_usd`, and `spend_complete: false` when a child was killed before it
  printed `SPEND:`, its `SPEND:` line ends `:incomplete`, or a review of a
  child's PR has not finished.

## [0.60.0] - 2026-10-13

### Added
- An epic's children branch from origin's default branch. Before each first
  launch the build runs `git fetch origin` and passes that commit as
  `build --base-ref <sha>`, so a child that starts after another child
  merged builds on top of it. A relaunched child keeps the command it was
  first started with. The fetch does not hold up Ctrl-C, SIGTERM or the
  watching of running children, and is killed when the run stops. When it
  fails, a child with dependencies is `unknown` and is not launched.
- `build --base-ref <sha>` branches a new worktree from that commit instead of
  the source checkout's HEAD; a build that re-binds to its worktree ignores
  it. When the commit is not on the default branch, the PR targets the branch
  that holds it (`--base`). It exits 1 when the `specs/config.yaml` committed
  at HEAD is not the version at that commit, unpushed or behind origin, since
  the build reads the checkout's file and its worktree holds the commit's.
  An epic checks this before a child's first launch and records nothing for
  it, so after a push or pull the next run fetches again and launches it.
- Each epic run ends with `runs/<NN>/summary.json` in the epic directory (each
  child's status, PR and park reason, and totals) and prints
  `EPIC:SUMMARY:<path>`, on every exit past the `git.worktree`/`git.push`
  refusal: done, a failed child, `EPIC:MERGE_WAITING`, a stop by Ctrl-C or
  SIGTERM, and an unexpected error.

## [0.59.0] - 2026-10-13

### Added
- An approved epic builds its children. After `EPIC:APPROVED`, the same
  command runs each child as its own buildme build (`--mode brief`, the
  child's plan, `--auto --no-epic-split --pr-ready`) from the source
  checkout's HEAD, so each gets its own worktree, branch and ready PR, and the
  parent's research as `--research-path` (or `--skip-research` when the
  parent skipped it), so no child decides on research again. A `specs/config.yaml` with `git.worktree` or `git.push` set to
  `false` is refused (exit 2) before any child starts. A child starts
  once every child it depends on has merged; at most `epic.max_parallel` build
  at once (default 2). While a child waits on an open PR the build exits 3
  with `EPIC:MERGE_WAITING:<urls>`; re-run it once they are merged. A PR closed
  without a merge parks the children waiting on it.
- `epic.json` keeps each child's status, PR and park reason. Every child
  build leaves `agents/<NNN>-build-<id>.{started.json,result.json,log}`.
- Re-running an epic continues from those records: a child still running from
  an earlier run is waited for, never started twice; a PR a stopped child
  opened is found (`gh pr list --head`) and kept; a child that exited 3 or
  without a PR stays parked and its dependants are skipped; a failed child is
  relaunched once per run. When `gh` or `ps` fails while checking a stopped
  child, or `gh pr view` fails for a PR another child waits on, nothing is
  launched for it and the run exits 1. Failed and unknown children are named
  on stderr, beside `EPIC:MERGE_WAITING` too. Ctrl-C or SIGTERM
  stops every child, one started by an earlier run included, and exits 130
  or 143.
- `specs/config.yaml` takes an `epic:` block: `max_parallel`,
  `max_review_rounds`, `merge` (`auto` or `manual`, default `manual`) and
  `max_usd`. `max_usd` is read but not enforced yet. Enforcing it, and
  reviewing, fixing and merging the children's PRs, come in later releases.

### Changed
- An approved epic's build no longer exits 0 right after `EPIC:APPROVED`: it
  runs the children, and exits 0 when none is left to build, 1 when a child
  failed, or 3 while one waits on a merge.

## [0.58.0] - 2026-10-13

### Added
- `/multiplai-dev:spec-extract --apply [--specs-dir <dir>]`: after render, the
  run copies each new capability file into `<specs-dir>/registry/` (default
  `<repo>/specs`; `--dir` needs `--specs-dir`, and without it the run prints
  `SPEC_EXTRACT:APPLY_NEEDS_SPECS_DIR` and exits 2 before writing anything).
  It commits nothing. A file already in the registry is never changed: with
  the same bytes it counts as applied, with other bytes it is left as it is
  and the run prints `SPEC_EXTRACT:SKIPPED_EXISTING <capability> <file>`.
  Before copying, it runs buildme's uncommitted-work check on the registry
  directory: any uncommitted change there stops the apply with exit 2 naming
  the files, and nothing is written. A stopped apply, re-run, finishes the
  copy without counting its own earlier files as someone else's changes.
  Prints `SPEC_EXTRACT:APPLIED <n>`. A `--specs-dir` outside any git work
  tree, or inside the run folder, is refused before any model call.

## [0.57.0] - 2026-10-13

### Added
- `/multiplai-dev:spec-extract`, third part: after exploring, a run now writes
  the specs. One call groups the public symbols into capabilities (a program
  then makes the names kebab-case and unique, puts every symbol in exactly one
  capability, and splits any capability over 40 symbols by module); one
  read-only agent per capability writes requirements with WHEN/THEN
  scenarios; every scenario's cited lines are re-read at the commit, and a
  scenario whose citations all fail is dropped and listed with the reason.
  So is a requirement whose name repeats a kept one in its capability, or is
  a prefix of one, or has one as a prefix: buildme matches requirement names
  by prefix when it removes or replaces them. The run writes
  `registry/<capability>.md` in buildme's `specs/registry/` format, each
  scenario marked "pinned by test" or "read from code only",
  plus `capabilities.json`, `summary.json`, and a `report.md` listing every
  dropped item and every public symbol no scenario specifies. It ends with
  `SPEC_EXTRACT:RENDERED <files> <run>/registry` and deletes `snapshot/`.
  The group and write calls follow explore's retry, budget and resume rules:
  a re-run pays for no call whose answer was saved. No group answer after two
  attempts ends the run with exit 1 (`SPEC_EXTRACT:NOT_GROUPED`). New
  `multiplai.conf` key `spec_extract_write_model` (group and write). Files are
  still written into the run folder only; copying them into a repository comes
  in a later version.

## [0.56.0] - 2026-10-13

### Added
- An epic split can be approved. Edit `split.md`'s table or any
  `plans/<id>.md`, then re-run the same build with `--approve-split`: buildme
  checks them again as they are on disk and prints
  `EPIC:APPROVED:<epic.json>`. A failed check exits 1 naming the file; a file
  changed after approval exits 2 naming it, and so does an `epic.json` whose
  children no longer match the approved table and plans. Building the
  children comes in a later release.
- Only one process at a time writes an epic's directory; a second exits 2
  with the pid, host and start time of the one running. A split another
  build of the same change saved while this one was deciding is kept, with
  any edit made to it.
- The wrapper fetches every GitHub issue's sub-issues (all pages) into the
  ticket, and posts the split on the ticket as one comment, never twice for
  the same split. Its exit-code list now says what exit 2 means for an epic.

## [0.55.0] - 2026-10-13

### Added
- `/multiplai-dev:buildme-findings`: each code-fix block now asks the fixer to
  search the same function, its other branches, its callers and the rest of
  the PR's diff for the same mistake, fix each one, and end its report with a
  `SEARCHED:` section. The reply on the PR quotes where it searched (the
  fixer's own section, also from a long report the engine trimmed, and never
  one a later review-fix or fix-on-pass agent wrote); the PR
  description's "Review rounds" section does not, to stay under the pr
  skill's word limit.
- When the TDD engine finishes a round's blocks with nothing stopping after
  them, one read-only agent checks the round's own fix commits, once per
  round; a round whose whole-change refactor or mutation testing stops after
  the blocks is published unchecked, as before, and a round with no fix
  commit left makes no call. The agent ends its answer with `NO PROBLEMS` or
  a `PROBLEMS:` list, and the last of these decides, so narration before it
  is not read as a problem. A problem it finds is written to
  `findings-r<N>-self-check.md`, committed with the tasks file, and fixed as
  one more block through the same TDD engine; that block skips the
  whole-change refactor and mutation testing, which already ran for the
  round. Its model is the new
  `findings_self_check` step (`review` stage) under `models:`.
- `findings --max-rounds <n>` overrides `findings.max_rounds` for one run.

### Changed
- `findings --auto` no longer posts the re-review's findings by the review
  skill's section rule. Of the findings the review lists (never a `repeat`
  or `low-value` one, and in `critical` mode only one rated as breaking), it
  posts every Code finding and every Tests finding that cites a source file
  other than a test file (in Python, Go, Swift, JS/TS, the JVM, .NET, Ruby,
  Elixir or PHP naming), through
  `review post --decisions buildme-decisions.json`; the file is written next
  to the review's `findings.json` in the data directory, never in the PR's
  worktree. A review folder without a readable `findings.json` now fails the
  review step (`FINDINGS:FAILED:review:cannot choose what to post: …`)
  instead of posting.

## [0.54.0] - 2026-10-13

### Added
- buildme decides, after the codebase analysis, whether its input is one
  change or several. One change builds exactly as before. Several (an epic, a
  PRD with separate features, an issue with sub-issues or a task list) stops
  before any spec is written: buildme writes one plan per child, with the
  children each depends on, to
  `<data_dir>/buildme/epics/<owner>-<repo>--<change>/` and exits 3 with
  `EPIC:SPLIT:<split.md>`. A re-run prints the same line and makes no new
  model call, and keeps any plan you edited meanwhile. A retry of a one-change
  build that failed does not ask again, nor does one that skipped the question or predates it. Approving the split and building the children come in later
  releases.
- `--no-epic-split` builds the input as one change without asking.

## [0.53.0] - 2026-10-13

### Added
- `/multiplai-dev:spec-extract`, second part: after resolving its target, a
  run lists the Python files and their public functions, classes and methods,
  then asks one read-only agent per slice of code (at most 6,000 lines) what
  the code does. It keeps only facts whose cited lines are at the commit, and
  writes `facts.json`, `report.md` (files left out, files not analysed, with
  reasons) and `plan.txt`. It prints `SPEC_EXTRACT:EXPLORED <answered>/<units>`.
  Writing the specs comes in a later version.
- `--trust-repo` (or `SPEC_EXTRACT_TRUST_REPO=1`) is required before the first
  model call, since the code is sent to a model; without it the run exits 4,
  and the skill tells the agent to ask the user before re-running with it.
- `--max-usd` (default 80) caps the spend of a run folder across every run
  that resumes it; reaching it exits 3 with everything paid for saved, and a
  higher cap continues. `--max-units` (default 20) refuses a target too large
  to explore in one run. `--plan-only` prints the units and call counts and
  stops before any model call.
- A resumed run asks only the slices with no answer, asks a slice at most
  twice in all, and a `--pr` run keeps the base and file list it started with
  even after the base branch moves or the PR merges. It can resume from
  another checkout of the same repository, even when the first one is gone.
  A re-run after explore writes `facts.json` with the same bytes, its
  `generated_at` included.
- A model setting no call can run with (an unknown provider in
  `spec_extract_explore_model`, `claude-agent-sdk` missing, or for a `pi:`
  model the `pi` binary missing or failing to start) exits 1 and counts no
  attempt, so the same command works once it is fixed.

## [0.52.0] - 2026-10-13

### Added
- `/multiplai-dev:spec-extract`, first part: it resolves a commit
  (`--tree [<commit>] [--path <dir>]`), a PR's head (`--pr <n>`) or a directory
  not under git (`--dir`), chooses its run folder
  `<data_dir>/spec-extract/<repo>/<object>/` (or `--out`), and extracts the
  commit for later steps. It prints `SPEC_EXTRACT:RESOLVED <run>` and makes no
  model call yet; writing the specs comes in later versions.
- Running the same command again resumes the same folder, also when an earlier
  candidate folder was renamed or deleted since. A second run of the same
  target while one is running exits 5 and writes nothing. `--restart` renames
  the run folder to `<object>.old-<timestamp>` and starts again; it never
  deletes. An unreadable `state.json` exits 1 and names `--restart`, as does
  a `--dir` run whose copied `source/` no longer holds its import.
- `snapshot/` holds each file as git stores it, whatever the commit's
  `.gitattributes` export rules say. A link that is absolute or leads out of
  the tree is left out and named, instead of failing the run.
- A `--dir` run refuses any `--out` that would put its run folder inside the
  source, including the source's parent. Its import commits every copied file,
  including those the source's `.gitignore` names.
- `--pr` reads the PR from `origin`'s repository, the one `--fetch` fetches
  from and the run folder is named after, also in a fork clone with an
  `upstream` remote.

## [0.51.0] - 2026-10-13

### Added
- buildme's check for uncommitted work a stopped step did not leave can now
  be run by another skill as a command:
  `python -m build_pipeline.stop_record_cli` (from buildme's `scripts/`
  directory) reads a JSON object on stdin naming the project directory and
  the stop record, and prints `{"reason": null}` or `{"reason": "<why not>"}`.
  It refuses exactly what buildme's own resumes refuse.

## [0.50.0] - 2026-10-13

### Changed
- The findings resume and the fix-on-pass resume share one check,
  `stop_record.others_work`, for work a stopped step did not leave. What they
  refuse is unchanged.

## [0.49.0] - 2026-10-13

### Fixed
- buildme in a project inside a larger repo (`--no-worktree` in a monorepo,
  or `/multiplai-dev:buildme-findings` on a subpackage) no longer commits
  files you staged outside the project, and its discards no longer revert
  your edits or drop your commits outside it. Only the project directory is
  committed, kept and discarded.
- A discard whose commits change a file outside the project now refuses
  instead of dropping them; buildme reports it as any refused discard.
- At the repo root, a file you staged before a buildme commit, and that
  buildme did not stage itself, now stays staged instead of going into
  buildme's commit. A buildme commit made while a merge is in progress now
  fails instead of concluding the merge.

## [0.48.0] - 2026-10-13

### Changed
- `/multiplai-dev:review` writes each review to `<out>/<repo>/<object>/`:
  a review of PR 283 of `spikelab/multiplai-cc-mktplace` lands in
  `reviews/multiplai-cc-mktplace/pr-283/`. The object says what was
  reviewed: `pr-<n>`, `branch-<name>`, `range-<base>..<head>`, `tree`,
  `tree-<path>`, or `dir-<name>-<hash>` for `--dir` (under `<name>/`).
- A fork's PR and its upstream's PR with the same number never share a
  directory: the second goes to `<repo>@<owner>/`. Two branches whose
  names clean to the same text (`feat/x`, `feat_x`) get two directories.
  A run going on in the other one does not stop the second from starting;
  a second run of the same target still exits 2.
- The run lock is `<out>/<repo>/.locks/<object>.lock`.
- Nothing already on disk moves. Directories written by 0.46 (named
  `<owner>-<repo>--<target>`), reviews a 0.42 run nested inside a new
  directory, and `reviews/legacy/` stay where they are, are never written
  to, and are read as earlier rounds with their decisions. A 0.46 run of
  the same target left unfinished still stops a plain review with exit 4
  and names its directory, until the new directory has a run of its own.
- `findings.json`, the ledger and the file names keep the slug
  `<owner>-<repo>--<target>`. `resume` and `assess-only` read `review.yaml`
  and write the rollups in the same `<out>` as the review, in either
  layout.
- review-viewer finds reviews in the new layout, the 0.46 one and 0.42
  folders nested in a new one.
- The `HIGH-only.md`, `MEDIUM-only.md` and `LOW-only.md` rollups list the
  new layout and the 0.46 one. A target reviewed in both is listed once,
  with the findings of its new directory; `runs.jsonl` keeps a line for
  every run in both. Rollups written in a review directory that a 0.42 run
  used as `--out` list the 0.42 review nested in it, never that
  directory's own earlier rounds or snapshots.

## [0.47.0] - 2026-10-13

### Changed
- `/multiplai-dev:plan` writes a "Stop and resume states" table into
  every plan: one row per step and stop point, saying what is on disk and
  what resume does, plus rows for files the user edited, a moved origin,
  a step stopped twice and a process still running.
- A plan's Done means now asks for a test that stops each step at each
  row, resumes, and compares the result with a run that never stopped.
- A plan's constraints now forbid implementers and fixers from adding
  resume behaviour the table does not ask for, and require a new row
  before any new saved state.

## [0.46.0] - 2026-10-12

### Changed
- `/multiplai-dev:review` writes to `reviews/` in the multiplai data
  directory (`<workspace>/.multiplai/data/reviews/` in a workspace) instead
  of `INBOX/reviews/`. `--out` still overrides it.
- A review's directory is named after the repository's `origin` remote,
  `<owner>-<repo>`, not the local directory: the same PR reviewed from the
  main checkout and from a worktree now goes to one directory
  (`spikelab-multiplai-cc-mktplace--pr-283`). A repository with no `origin`
  keeps its directory name.
- Each earlier round is kept in full (its `progress.log`,
  `review-state.json`, `diff.patch`, commit and file lists, summaries and
  checks, not only the findings and the review) under
  `rounds/<NN>-<head sha>/`, numbered `01`, `02`, …; a second run on the
  same head gets the next number. A `--dir` review's imported checkout
  (`source/`) is not copied into a round: its git already keeps the commit
  each round names. Rounds kept by earlier versions
  (`<sha>/`, `<sha>-2/`) are still read. The round id in posted PR markers
  is unchanged. A run that stopped before it wrote its findings (a budget
  stop, a verify stop, a failure) is kept as its own round too, instead of
  being written over by the next run.
- The first review of a PR after the move still sees that PR's earlier
  rounds and the rejections recorded on them, read from the old
  `INBOX/reviews/<dirname>--pr-<n>/` directories (and `migrate`'s copies)
  that name the same `origin`.
- `review.yaml` left in the old `INBOX/reviews/` still applies to reviews
  written to the default directory, with a warning, until one exists in the
  new place; `migrate` copies it there.
- `runs.jsonl` lists every run, earlier rounds included, sorted by time.
- review-viewer looks for a review in the new directory, then in the old
  `INBOX/reviews/` and in the copies `migrate` made of it; a review found
  only there loads with a notice naming where it came from. Those old
  reviews are still matched by the checkout's directory name their slugs
  carry.

### Added
- `ledger.jsonl` in the data directory's `reviews/`: one line when a run
  starts and one when it ends (budget stops, resumes and each batch target
  included), with its cost and counts. It is never rewritten, so it keeps
  runs whose directory was deleted.
- A review killed or stopped with Ctrl-C part way (no end line in the ledger) is no longer started
  over by accident: running `review` on the same target and head exits 4
  and prints the `resume` command. `--restart` starts over on purpose.
  When the target directory no longer holds that run's saved state on that
  head (deleted, or written over by a run on another head), `review` starts
  over instead of pointing at a `resume` that would fail or continue the
  wrong head.
  buildme's findings stage resumes such a review of the PR head (one its
  own timeout killed, say) instead of counting it as a failed review. When
  buildme's timeout for the review, pr or walkthrough skill runs out, it now
  kills the skill's whole process group, not only the `uv` launcher: before,
  the skill ran on with its output closed, holding the review's lock.
  A SIGTERM, a SIGHUP (a closed terminal) or a Ctrl-C sent to buildme
  while it waits on one of those skills kills the skill's group the same
  way, so the skill no longer runs on after buildme has stopped.
  A review that is still running is not mistaken for one of these: each
  run holds a lock on its target directory until it ends, and while it
  does, a second `review`, `review --restart`, `--plan-only` or `resume` of
  that target exits 2 saying it is running, so two runs never write the
  same directory at once.
- `python -m review_pipeline migrate --from <dir>` copies old review
  directories (for example `INBOX/reviews`) to `reviews/legacy/` in the
  data directory and records them in the ledger. It never changes or
  deletes the source, and a second run copies nothing.

## [0.45.0] - 2026-10-12

### Added
- buildme keeps every review round of every block. Each one is written to
  `specs/changes/<name>/reviews/block-<NN>-round-<k>.json` with the scores,
  the gate's verdict and reason, the findings the judge accepted and the ones
  it rejected (with its reason), the brief sent to the fix agent, and the fix
  agent's report, commit and where it searched. A new round never replaces an
  earlier file, including across a resume. The records are committed with the
  block and are not part of the diff the next reviewer sees.
- A block that passes review with accepted Critical, Major or Minor findings
  now gets one fix pass on them (`code_review.fix_on_pass` in
  `specs/config.yaml`, on by default). The fix is kept only when the suite stays
  green, the test files are untouched, and the block still passes a second
  review; otherwise the block goes back to where it was. Findings left unfixed
  are listed in `specs/changes/<name>/unfixed-findings.md`, which buildme adds
  to the PR body. A build that stops during this pass rolls it back and runs it
  again when resumed. Before rolling back, the resume checks that the tree
  holds only what the stopped pass left: an edit or commit made since the stop
  (or any uncommitted edit, when the build stopped while the fix agent was
  still writing) stops the resume with the files named and nothing rolled
  back; stash or move them and run the build again. `fix_on_pass: false`
  keeps the old behaviour.

### Changed
- buildme's review-fix agent is now asked to look for the same mistake in the
  other branches of the function and in the rest of the block's diff, to
  re-read its own `git diff` before finishing, and to end its report with a
  `SEARCHED:` section saying where it looked. The section is recorded in the
  round record; a report without one is recorded as `searched: null` with a
  warning, never a failure.
- Each review fix on a block that failed review is now committed as it lands
  (`fix(block-N): …`), rather than once at the end of the block, so its round
  record can name the commit. A block also ends with one more `fix(block-N)`
  commit that carries its round records.

### Fixed
- buildme's per-block reviewer, the judge of its findings, the whole-change
  refactor and the documentation step now see new files that are not
  committed yet. Before, a
  file a fix or refactor agent created (a new `helpers.py`, say) was left out
  of the diff they were given, but was still committed with the block, so it
  reached the branch unreviewed. Ignored files and buildme's own bookkeeping
  stay out, as they stay out of the commit.

## [0.44.0] - 2026-10-12

### Changed
- buildme's codebase analysis (`codebase-analysis.md`) is now made of cited
  facts. It runs the shared `code_facts` step that codebase-walkthrough and
  review's `explain` stage use: one agent per slice of the project's source
  files at HEAD (test, vendored and generated files left out), plus one for
  its manifest and config files (`pyproject.toml`, `package.json`, `*.yaml`
  and the like, lock files left out), returns claims with `path:line`
  citations, and a claim is kept only when one of its citations is found at
  that commit. The report keeps its Architecture, Patterns and Dependencies
  headings: Dependencies holds what the manifests and config files declare
  (dependencies, configuration, external services), then the interfaces
  other code calls. It adds Conventions (naming, error handling, and test
  style, which each source slice's agent reads from up to two tests that
  import its modules; tests are not otherwise analysed), links every claim
  to its lines, and says how many claims were dropped. The facts are also written to
  `facts.json` beside it, in the format review writes. The analysis now reads
  every source file instead of three agents of five turns each, so it costs
  more on a large repo; `codebase_analysis_max_units` (default 12) caps the
  agents, keeping the files the change's documents name and then the most
  imported, and lists the rest under "Not analysed". A project with source
  files and no git commit (a `--no-worktree` build `git init`s without
  committing) gets a report headed "Codebase analysis: not analysed" that says
  why and lists the source files, and the phase is logged as skipped.

### Added
- `block_facts: true` in `specs/config.yaml` (default `false`): each block's
  test writer and implementer also get the facts about the files the block's
  tasks name, each re-checked at the current HEAD; a fact whose quoted lines
  an earlier block changed is dropped, and the section says how many were.
- Before it publishes, a build re-checks every
  `docs/walkthroughs/*-walkthrough.md` in the project at its HEAD with
  codebase-walkthrough's model-free `check --at-head`, checks each stale one
  again at the commit the build started from, prints the ones the build made
  stale with the citations it broke (`WALKTHROUGH:STALE:`, `WALKTHROUGH_FAIL:`)
  and lists them in the PR body. One that was already stale before the build
  is listed apart (`WALKTHROUGH:ALREADY_STALE:`) and never regenerated. A repo
  the walkthrough also reads is checked where it lives beside the main
  checkout; when it is not there, the walkthrough is reported as not checked.
  `regenerate_walkthroughs: true` (default `false`) re-runs each one the build
  made stale the way its header says it was made, capped at
  `walkthrough_max_usd` (default 10) per run, and commits it to the build
  branch; it needs `--trust-repo`, and a walkthrough that read other
  repositories is reported instead of re-run. The re-run keeps the repo key
  the walkthrough's header gave the project, not the build worktree's
  directory name (`buildme-<change>`).
- codebase-walkthrough: `run --repo-key KEY` names the module's repository
  `KEY` in the "Commits read" table and every citation, instead of its
  directory's name.

## [0.43.0] - 2026-10-12

### Added
- New `/multiplai-dev:buildme-findings <pr>` skill (`python -m build_pipeline
  findings --pr <n>`): fixes a PR's review comments through buildme's own
  process. It reads the PR's unresolved review threads, new top-level
  comments, and the findings `/multiplai-dev:review` puts in a review's body
  (those on lines outside the PR's diff), acts only on those from
  `findings.authors` (default: anyone with `admin`, `maintain` or `write` on
  the repo) and the bots in `findings.allow_bots`, and sorts each with a
  read-only agent that sees the comment only as fenced, untrusted text.
  `findings-r<N>.md` in the change directory lists every comment with its
  verdict. A finding that needs a spec change gets one:
  `respec-findings-r<N>.md` is applied to the change's requirements, each
  requirement edited inside its own section, and to `specs/registry/` when the
  change is archived; it is committed as `spec(findings-r<N>)` before any
  code, with or without `--auto`. Each code defect becomes a block run by the
  TDD engine with a failing test first and every gate a build has; a block the
  engine cannot fix is kept under a wip ref, left off the branch, and answered
  "not fixed". A block stopped by an agent that timed out or errored, or by
  the budget, is not answered: re-running the command keeps that attempt's
  edits under `refs/buildme/wip/<change>/r<N>-code-block<k>-stopped` and runs
  the block again. A wip ref is never overwritten: a later attempt at the same
  step goes under the same name with `-2`, `-3`, and so on. When the commit
  that would keep the edits fails (a pre-commit hook rejects it, say), no ref
  is written and the edits are left in the tree, never discarded. When every block
  is done but a step the engine runs after them fails (the whole-change
  refactor's budget stop, or mutation testing), the fixes are still pushed and
  answered, and each reply says what failed after them; a test suite that
  fails before any fix stops the round with a message saying so. The skill then pushes, adds a "Review rounds" section to the PR
  description through `/multiplai-dev:pr`, replies in each thread with the
  fixing commit or the reason nothing changed, and re-runs
  `/multiplai-dev:review`. Rounds repeat until nothing new is left or
  `findings.max_rounds` (default 3) is reached. Review threads are never
  resolved. A re-run resumes at the step that did not finish without re-asking
  a comment or posting a reply twice, and a step that failed part way keeps
  its edits under `refs/buildme/wip/<change>/r<N>-<step>`. Commits someone
  else pushed to the PR are kept. It will not start a round on a worktree with
  uncommitted changes, and it will not reuse a change's state for a different
  PR. A re-run that resumes a step stops with `FINDINGS:FAILED`, and leaves
  the worktree as it is, when a file outside the change directory has an
  uncommitted change the stopped step did not leave there, with the same
  content, when it stopped (a stopped code fix's own edits are kept as
  above). When the step recorded nothing as it stopped (its process was
  killed), any uncommitted change outside the change directory stops it.
  Resuming triage, spec or code also stops when commits were made on the
  branch since the step stopped, other than ones pulled from origin.
  Each round's TDD engine is held to the `budget:` ceilings on that round's
  own spend, also when `--auto` runs several rounds in one process; the
  triage and spec agents' spend is not counted against them.
- `findings.allow_bots` in `specs/config.yaml`.

### Changed
- `/multiplai-dev:pr edit` ends the comment it leaves on a PR opened by hand
  with a hidden marker, so `/multiplai-dev:buildme-findings` does not take that
  comment for a review comment.
- `findings.authors` and `findings.max_rounds` are now read by the findings
  stage; before this release they were stored only.
- buildme commits the edits a block's review fixes make as
  `fix(block-<n>)` in that block. Before, they stayed uncommitted and went
  into whatever buildme committed next.
- A buildme block that timed out and then fails for another reason when the
  build is re-run now ends the build with exit 1. Before, it exited 4, the
  code for an agent timeout, on every later run.

## [0.42.0] - 2026-10-12

### Added
- New `/multiplai-dev:pr` skill: opens a pull request, or refreshes its
  description, so the description follows the repository's PR template. It
  finds the template where GitHub looks for one (`.github/`, the repo root,
  `docs/`, or a `PULL_REQUEST_TEMPLATE/` directory, names in any case), or
  takes `pr.template` from `specs/config.yaml` (`auto`, buildme's default,
  means find it; a path to no file, or outside the repository, is reported
  and the template is found as if it were unset). With no template, or several and no choice made, it uses a
  standard template that ships with the skill:
  Why, Blocks, Documentation, Companion artifacts, Ticket and Size.
- The template's headings, checkboxes and comments are always kept. Sections
  the change's own files answer are filled from them; one model call with no
  tools writes the rest. A checkbox is ticked only when the model ticks it
  and its quote appears word for word in one of the change's files; code
  checks that the quote is there, and the model judges whether it shows the
  item is done. A description over 2,500 words is refused
  before anything is written.
- `edit` rewrites only the sections the skill wrote, marked with hidden HTML
  comments. Your own edits elsewhere in the description stay, and a section
  you deleted stays deleted. On a PR you opened by hand, it adds below your text
  the sections your text lacks (none whose heading is already there, so a PR
  opened from the repo's template gets no heading twice) and leaves one
  comment saying so; without the rights to edit
  the PR, it writes nothing. Once buildme has deleted its `.build-state.json`
  after publishing, `edit` keeps the Documentation and Blocks sections already
  in the description instead of blanking them.
- `edit` measures the diff on the PR's own head commit, fetching it from
  GitHub when your checkout lacks it, so a PR from a fork, or a branch you
  never fetched, gets its own size and file list. When that commit cannot be
  fetched, the Size section says it was not measured, instead of describing
  whatever branch you have checked out.

### Changed
- buildme's PUBLISH stage opens its PR through the pr skill, so a build in a
  repository with a PR template opens a PR that follows it. When that
  template has no Blocks, Documentation or Companion artifacts heading, those
  sections are added after the template's own, so the PR still names the
  documents the build rewrote. Its output lines
  (`PUSHED:`, `PR:`, `PUBLISH_DIAGNOSIS:`), its draft default and its
  resumable PUBLISH step are unchanged: buildme passes `--draft` or `--ready`
  to the skill, so the skill's own `pr.draft` setting cannot override
  `git.pr`. When the PR cannot be opened,
  `build-progress.md` now lists the pr skill's command first, then
  `gh pr create`. A build started from a ticket passes the ticket's link, so
  the PR's Ticket section shows it. A build of a project inside a larger
  repository (a monorepo package) uses the `pr:` settings in that project's
  own `specs/config.yaml`, where `buildme setup` writes them.

### Fixed
- buildme: a `PUBLISH_DIAGNOSIS:` line now holds its whole reason on one
  line. When the pr skill or `git push` failed with output of several lines
  (a traceback, git's push report), only the first reached that line. When
  the pr skill fails before it can say why, buildme now reports the end of
  its output, where a traceback's error is, rather than the first 800
  characters.
- pr: every `PR_DIAGNOSIS:` line holds its whole message on one line. A
  multi-line `gh` error used to put its later lines on stdout with no prefix,
  so buildme dropped them; gh's "a pull request ... already exists:" lost the
  existing PR's URL that way.

## [0.41.0] - 2026-10-12

### Added
- buildme: on Python projects (a `pyproject.toml`, `setup.py`, `setup.cfg`,
  `pytest.ini`, `tox.ini` or `requirements.txt` at the project root, and a
  test command that runs Python: `tdd.test_command`, or the pytest command
  discovered from `pytest.ini`, `pyproject.toml` or `setup.py`), the
  build now runs mutation testing on the lines it added or modified, right
  after the whole-change refactor. Python files the tests never import
  (`setup.py`, `noxfile.py`, `fabfile.py`, `manage.py`, the root `docs/` or
  `doc/` tree, and `migrations/` or `alembic/` directories) are not mutated,
  nor are test modules, Django's `<app>/tests.py` among them. It uses cosmic-ray 8.7.0 as a pinned `uvx`
  tool (`uv` must be installed; the first run downloads it, and without `uv`
  the build stops with a message saying to install it or turn the step off;
  when cosmic-ray cannot be fetched or run, offline or behind a private index,
  the message also says how to turn the step off).
  Stopping the build during the step (Ctrl-C, SIGTERM or SIGHUP), or the step
  timing out, stops cosmic-ray and the test run it had in flight, and puts
  back any file it had mutated.
  Surviving mutants go to one fresh test-writer agent that may only add or
  edit tests; it runs on `models.mutation_test_writer` (an `agent`-stage
  step), and the result is written to `specs/changes/<name>/mutation.md`.
- buildme: a build now fails when its mutation score stays below
  `mutation.min_score` (default `0.8`, to be adjusted once real runs have been
  measured), when the survivor round edits a non-test file (files the test
  suite writes when it runs, such as caches, reports, logs, a test database
  or a golden file it writes back to the same bytes, do not count, even when
  tracked; Django's `<app>/tests.py` counts as a test), when cosmic-ray
  cannot produce a score, or when a change the step has to throw away cannot
  be thrown away (the build then stops without committing anything more).
  It also fails when the tree holds uncommitted changes as the step starts:
  those are edits nothing verified (a whole-change refactor that failed and
  could not be undone, for one), and the step neither commits them nor
  mutation-tests them. Set `mutation: {enabled: false}` in
  `specs/config.yaml` to turn the step off; `timeout` and `max_mutants` tune
  its cost. A test suite slower than `timeout` still works: each mutant gets
  three times the unmutated suite's measured run when that is longer.

### Fixed
- buildme: when the whole-change refactor agent fails or times out, the
  edits it had already made are now rolled back to the commit taken before
  it ran, keeping the build's own review fixes. Before, they stayed in the
  tree unverified, and a resumed build kept stopping on them.

## [0.40.0] - 2026-10-12

### Added
- review: an optional `explain` stage (`explain: true` in `review.yaml`, or
  `review_explain` in `multiplai.conf`; off by default). Before the finders
  it works out what the code under review does with the shared
  `code_facts` step: each changed hunk with 20 lines of context, the lines
  the change removed (read as they were before it), or every file of a
  `--tree` review. Only facts whose cited lines are found at the commit are
  kept, and they are written to `facts.json` beside `findings.json`
  (contract: `review/schema/facts.v1.schema.json`). The stage counts
  towards the spend limit, and `resume` does not ask it twice.
- review: `find_with_facts: true` (needs `explain`; off by default) lists
  those facts, with their ids, in every finder's prompt. Whether it helps
  has not been measured on real reviews yet.
- review-viewer: when a review wrote `facts.json` for the same commits, the
  session builds the walkthrough's steps from those checked facts and
  anchors each step on their cited lines, and the overview says so. The
  page and the walkthrough format are unchanged.
- code_facts: a span of lines removed by a change can name where the old
  file is read, and asks for `side: "base"` citations.

### Changed
- review: an earlier round's `facts.json` is kept under `rounds/` with its
  `findings.json`. `review/SKILL.md` gathers the `review.yaml` and
  `multiplai.conf` settings under a new Configuration section.

## [0.39.0] - 2026-10-12

### Added
- buildme: every step can run on its own model. `models:` in
  `specs/config.yaml` takes a step (`test_writer`, `implementer`, `review`,
  `plan_review`, …), a stage (`spec`, `agent`, `review`) or `default`, set to
  `haiku`, `sonnet`, `opus`, `fable`, `claude:<name>` or
  `pi:<model>@<provider>`. A step takes the first of: its own key, its stage's
  key, the stage's `multiplai.conf` model, `models.default`, the pipeline's
  model. With no `models:` key and no stage model set, every step runs on the
  model it ran on before. SKILL.md § "Tuning model and effort" lists every
  step and its stage.
- buildme runs a `pi:` model through multiplai-core's Pi backend (core
  v0.19.0). This needs Pi installed and configured, which buildme does not
  do; a missing `pi` binary fails the step rather than falling back to Claude.
- buildme: Claude Fable counts as the advanced tier. When `models.default` is
  a `pi:` model, the tier comes from `BUILDME_TIER` or `tier:` in
  `specs/config.yaml`, and is `standard` otherwise, with a log line saying why.

### Changed
- buildme: a stage's `multiplai.conf` model now reaches every step of that
  stage. `[buildme.spec] MODEL=` used to reach only the interview and now also
  reaches spec generation, the audits, the rubric, the explainer and the
  respec proposal; `[buildme.agent] MODEL=` reached no step and now reaches
  the TDD agents, the docs update and the prototype; `review_model`
  (`code_review.model`, `BUILDME_REVIEW_MODEL`, `[buildme.review] MODEL=`)
  now also reaches the test-quality audit. Finding adjudication stays on the
  pipeline's model.
- buildme: a `pi:` model in a `[buildme.*] MODEL=` section of
  `multiplai.conf` is used instead of dropped. A provider buildme has no
  runner for (`openai:`) is still dropped with a warning.
- buildme: `models:` values are no longer stored only; a bare Claude family
  name (`opus`) is now accepted, and an unknown key is dropped with a warning.

## [0.38.0] - 2026-10-12

### Added
- buildme: `/buildme setup` writes or merges `specs/config.yaml` for the repo:
  which ticket system (`intake.provider`: `plane`, `github` or `none`), which
  interview mode, an architecture doc, the PR template and whose PR comments
  count as findings. It keeps the file's comments and key order, never removes
  a key, and tells you when the file still needs a commit.
- buildme takes a ticket as input — `/buildme DB-2267`,
  `/buildme https://github.com/o/r/issues/12`, `/buildme #12` — as well as a
  file or a prompt. A ticket in a repo with no ticket system configured stops
  with "run `/buildme setup`". The ticket is saved as `ticket.md` in the change
  directory, fenced as untrusted text. A plan written with the plan skill can
  be passed as `--plan-file` and is saved as `plan.md`; its text reaches the
  spec in every interview mode, as it did when it was sent as a context file.
- buildme: three interview modes (`interview:` in `specs/config.yaml`).
  `self` interviews itself with no human: one call asks, a separate call
  answers from the repo and the ticket, and anything it could not ground is
  listed as an assumption. `ticket` asks its questions on the ticket and waits
  for replies; when it has no questions, it goes on without waiting. `user` is the interview you had before. `--auto` turns `user`
  into `self`. Every mode saves `interview.md`, committed on the build branch.
- buildme decides whether research is needed instead of asking you up front,
  and saves the reason in `research.md` either way.
- buildme stops after plan review when the plan should be split into several
  PRs, and writes the proposed split to `split-proposal.md`. `--accept-size`
  builds it as one PR anyway, and a later retry of that build does not stop on
  size again.
- buildme: new exit code `3` means "waiting" — on ticket replies
  (`INTERVIEW:WAITING`), on research (`RESEARCH:NEEDED`) or on a split decision
  (`SPLIT:REQUIRED`). Re-running the same command resumes.

### Changed
- buildme: the plan-split triggers are now more than 10 blocks or more than 5
  top-level packages (were 8 and 3). `multiplai.conf` overrides still apply.
- buildme: with a worktree build, an uncommitted `specs/config.yaml` now stops
  the build with a clear error instead of building without it. A failed build
  retried in its own worktree is not stopped by this check.
- buildme depends on `ruamel.yaml` (for `setup`).
- buildme: an agent timeout during the build now exits `4`
  (`EXIT_AGENT_TIMEOUT = 4`; it was `3`, which now means "waiting"). A wrapper
  that treated `3` as a timeout should check for `4`.
- buildme: relative paths given to `--ticket-file`, `--research-path`,
  `--plan-file`, `--interview-replies` and `--context-files` are read from
  `--project-dir`. A ticket, research, plan or replies file that does not
  exist now stops the build with exit `2`, instead of a warning and a build
  without it — unless the build already saved it in the change directory, so
  resuming after the wrapper's scratch file is gone still works.

### Fixed
- buildme: `build --research-path` works. The flag was documented but rejected,
  and research that did arrive reached the proposal as a file path rather than
  its content.
- buildme: the first spec-stage commit now commits the new change directory.
  With git 2.43 it committed nothing, and the specs only reached the branch with
  the first build block.

## [0.37.0] - 2026-10-12

### Removed
- `codebase-walkthrough` no longer writes the step-through HTML page
  (`<name>-walkthrough.html`). In human mode it writes the Markdown
  walkthrough and `<name>-walkthrough.json`, and step 2 opens that file in
  the review-viewer, which is now the only page. Agent mode is unchanged.
- The `markdown` package is no longer a dependency of the walkthrough
  pipeline; only the HTML page used it.

## [0.36.0] - 2026-10-12

### Added
- `codebase-walkthrough` in human mode also writes `<name>-walkthrough.json`
  next to its Markdown, and step 2 opens it in the review-viewer as a tree
  of the module's directory at the commit it read (`serve --tree`). Each
  section of the walkthrough is a step there, anchored on the lines it cites
  in the module; every other file under the module is listed with the reason
  no step covers it. The step-through HTML page is still written, for now.
- The walkthrough's Markdown header names the module directory, the depth
  and the mode it was run with.

### Changed
- `review-viewer`: a complete walkthrough of a tree target (`serve --tree`)
  no longer needs the `commits` and `tests` assessments or the `risk` block,
  since a tree is not a change. It must still cover or skip every file.
  Existing walkthrough files load as before; the schema is unchanged.
- `codebase-walkthrough` now also runs `git ls-tree` (read-only) on the
  module's repository, to list the files the viewer shows.

## [0.35.0] - 2026-10-12

### Added
- `lib/code_facts/`, a package shared inside the plugin that works out what
  code does: it splits line ranges into units, asks one explore agent per
  unit, and keeps only facts whose citations re-read at their commit
  (multiplai-core's `check_quote`). The caller passes in its own model call
  function and path resolver, so each pipeline keeps its retry policy. A
  citation can name the `base` or `head` side of a change, and an explore
  answer can carry `conventions` facts when the caller asks for them. Review
  and buildme will use it next.
- CI runs the code_facts tests.

### Changed
- codebase-walkthrough's explore step runs through `code_facts`. Its output
  is unchanged; the explore prompt's JSON schema now lists the citation's
  `side` and the answer's `conventions`, and the run checkpoint stores them.
- review takes its concurrency limiter from `code_facts` instead of a copy.
- `code_facts` is not a uv workspace member: walkthrough and review reach it
  through an editable `path` source, which resolves both in this repo and in
  an installed copy of the plugin. `scripts/lint_workspace.py` exempts
  `plugins/*/lib/` from its membership check.

## [0.34.4] - 2026-10-11

### Changed
- review and codebase-walkthrough check a citation with multiplai-core
  0.18.0's `check_quote` instead of each carrying its own copy of the
  `git show` reader and the quote match. What a gate accepts or rejects is
  unchanged: review still rejects with "path not at head" and
  `action="reject"`, and walkthrough still checks that a path names a known
  repo before anything else. One difference: a line range that is not valid
  (start below 1, or end before start) now reads as no lines, so a quote
  cited there fails; both pipelines' models already refuse such ranges.
- Both pipelines' `target.py` take `GIT_ARGV` and `git_env` from
  multiplai-core in place of their own identical `_GIT` and `_env`.
- `uv.lock` takes multiplai-core v0.18.0.

## [0.34.3] - 2026-10-11

### Changed
- buildme's `llm_call_structured` and `agent_call_structured` parse an answer
  with multiplai-core 0.17.0's `parse_answer`. The retry policy is unchanged:
  a bad answer still re-runs the whole call, up to `max_retries` times. One
  visible difference: when an answer holds no JSON at all (a markdown
  report, say), the error, and so the retry prompt, now says so ("the answer
  contains no JSON object with the field(s) …") instead of quoting a parse
  error about the first brace in the prose.
- buildme keeps its own spend ledger (`budget.py`). It counts spend per phase
  in tokens, not USD, so it does not move onto multiplai-core's
  `SpendLedger`.

## [0.34.2] - 2026-10-11

### Changed
- codebase-walkthrough takes its checkpoint writer, `progress.log` writer,
  spend ledger, `walkthrough.yaml` helpers and answer parsing from
  multiplai-core 0.17.0 (`checkpoint`, `progress`, `budget.SpendLedger`,
  `stage_config`, `structured`) instead of carrying copies of review's. The
  `progress.log` lines, the $50 default ceiling, the one-reformat re-ask and
  the config precedence are unchanged. The ledger saved in
  `walkthrough-state.json` now also records spend per stage (`by_stage`),
  calls that returned no usage (`no_usage_calls`) and `max_tokens` (null), as
  review's does; a ledger saved before this loads as before.

## [0.34.1] - 2026-10-11

### Changed
- review takes its checkpoint writer, `progress.log` writer, spend ledger,
  `review.yaml` helpers and answer parsing from multiplai-core 0.17.0
  (`checkpoint`, `progress`, `budget.SpendLedger`, `stage_config`,
  `structured`) instead of carrying its own copies. What a review does is
  unchanged: the same `progress.log` lines, the same $50 default ceiling per
  target, the same one-reformat re-ask, and the same config precedence. The
  saved ledger in `review-state.json` gains a `max_tokens` key (always null
  for review); a ledger saved before this loads as before.

## [0.34.0] - 2026-10-11

### Removed
- buildme: the final review is gone. buildme no longer runs a whole-change
  model review after the last block, so a build no longer fails at the end on
  a FAILED or errored final verdict, and no `FINAL_REVIEW` line appears in
  its log. Each block is still reviewed and fixed as before. For a review of
  the whole change, run `/multiplai-dev:review` on the PR buildme opens.
- buildme: `--lenient-review` now covers one case only, a block whose code
  review stays below threshold after all fix rounds.

A checkpoint written by an older buildme still loads and resumes where it was.

## [0.33.1] - 2026-10-11

### Changed
- buildme, review and codebase-walkthrough no longer carry their own copy of
  the tool list their deny-lists were computed from. Each call now passes
  only its allow-list, and multiplai-core's `run_agent` denies every other
  tool (`multiplai_core.deny_list` over core's `TOOL_UNIVERSE`), including
  the `Read` it opens for an oversized prompt. The tools each agent can use
  are unchanged: the three copies held the same 53 names as core's list. A
  tool Claude Code adds later now needs one edit, in multiplai-core.
- review and codebase-walkthrough import `KNOWN_EFFORTS` from multiplai-core
  instead of defining the same five effort names locally.

## [0.33.0] - 2026-10-11

### Changed
- review: `post` now writes one GitHub review on the PR instead of one PR
  comment. Each finding whose lines are in the PR's diff is an inline
  comment on those lines, in its own thread, showing the severity, the claim,
  the failure scenario and the expected behaviour. Findings on lines outside
  the diff go in the review's body with a link, under a count line
  ("Code review: N findings, M inline, K on lines outside this PR's diff").
  The review's event is always `COMMENT`.
- review: with `--decisions`, `post` posts every finding you accepted, at any
  severity and in any section, not only HIGH and MEDIUM. Without it, it posts
  what the review lists at these severities: Code every severity, Tests HIGH
  and MEDIUM, Docs HIGH.
- review: `post` no longer posts a finding twice. It reads the PR's review
  comments and reviews first and skips findings already posted there, and
  `still-open` findings whose earlier finding was posted; the summary line
  lists the skipped ids. If it cannot read the PR, or GitHub refuses the
  review, it posts nothing and exits 2.

### Added
- review: every finding `post` writes ends with a hidden marker,
  `<!-- multiplai-review:finding id=<id> round=<round> v=1 -->`, so other
  tools can tell review findings apart on a PR. The format and its regex are
  in the review skill's SKILL.md under "Marker".
- review-viewer: a finding sent to GitHub as a line comment from the page
  ends with the same marker.

## [0.32.0] - 2026-10-10

### Added
- review: each need can now say where to look (`where`) when no single
  command gets the information: a console page and its path, a dashboard, a
  file outside the repository, or the team that owns it. The finder and
  verifier prompts ask for it whenever the command is empty and tell the
  agent never to leave both empty. `findings.json` carries it on every need
  (an older file without it still loads), and `review-<slug>.md` and
  `summary-<slug>.md` print it as "Where to look:". A need with neither now
  says "The review named no command and no place to look." instead of "No
  command is known."
- review-viewer: an item on the **Needs you** tab with no command and no
  place to look has an **Ask the session** button. It puts a question about
  the item in the chat, for you to read and send.
- review: the verifier labels what each finding is about (`topic`: code,
  tests, docs, config, infra, data, security, performance or process),
  exported in `findings.json`. review-viewer shows it as a badge on the list
  and the finding; for an older review it guesses from the file path and
  shows the guess in italics.
- review-viewer: **Copy as markdown** on a finding copies it for whoever will
  fix it: claim, file and lines (with a GitHub link when the remote is on
  GitHub), failure scenario, explanation, expected behaviour, verdict and
  cited code. **Download .md** saves the same text as a file, to
  attach or send.
- review-viewer: **Show all files** above the file list shows every file in
  the repository at the reviewed commit, grouped by directory, with the
  files the change did not touch greyed. Opening one shows the file whole,
  with no diff. Directories fold, and the filter and Go to (Cmd/Ctrl+K)
  search all files while it is on. Large repositories are cut at 20,000
  files, with a note saying so.
- review-viewer: Cmd+click (Ctrl+click on Linux and Windows) on a name in
  the code lists where it is defined at the reviewed commit, from a text
  search for definitions (`def`, `class`, `function`, `const`, `func`,
  `type`, SQL `CREATE FUNCTION` and the like, and `NAME =`). Each result
  opens that file at the line. Holding the key underlines the name under
  the pointer. It finds only definitions written in a recognisable form, in
  files the page may show.
- review-viewer: **GitHub** and **Slack** buttons on a finding open a dialog
  with the finding as markdown, which you can edit, and the choice of a PR
  comment or a comment on the finding's line (for a review of a PR), or a
  Slack recipient and an optional note. **Send** in the dialog is the
  confirmation: the session posts the text exactly as written, once, and
  asks nothing more in the terminal. Slack needs the `multiplai-messaging`
  Slack skill in the session (`serve --share slack`); without it, or for a
  review that is not of a PR, the button says why it is off.

- review: `--mode critical|full` on `review` and `batch` (default `full`;
  `resume` keeps the mode; recorded as `mode` in `findings.json`). `full`
  lists every finding to act on, as before. `critical` lists and counts only
  the findings the verifier rates as breaking users or the business after
  merge; the rest go to the appendix of `review-<slug>.md`, and review-viewer
  shows **Critical only** and hides them. Both modes run the same finders and
  verifiers, so they cost the same. Use `critical` before a merge or release,
  when only what will break matters.
- review: the verifier rates each finding's `impact`: what goes wrong in
  production if the change is merged as it is. The values are
  `breaks-users`, `breaks-business` (money, a failing deploy or job, access
  wider than intended, a wrong audit or billing record, or a document that
  would lead an operator to a wrong action in production),
  `correctness-only` (wrong, but no path after merge reaches anyone; every
  missing or weak test) and `hygiene`. It is exported in `findings.json` and
  printed under each finding. review-viewer shows it as a badge beside the
  topic.
- review, review-viewer: findings are listed in **Code**, **Tests** and
  **Docs** sections by topic, with severity read within each section. A
  HIGH test gap is listed and counted under Tests, not beside the code
  defects. The counts in `review-<slug>.md`, `summary-<slug>.md` and the
  final `review finished:` line are per section. In review-viewer each
  section folds: Code starts open, Tests and Docs folded, and picking a
  finding opens its section.
- review-viewer: hovering a finding in the list shows ✓ and ✕ to accept or
  reject it without opening it, and **Accept all**, beside the "N of M still
  need a decision" count, accepts every finding that still needs one after
  a confirmation. Refuted and decided findings are left as they are.
- review-viewer: once the Findings list scrolls out of sight, a bar under
  the tabs offers **Back to the list**, with Prev and Next and where you
  are.
- review-viewer: hovering a status, topic, impact or label badge shows the
  help's definition of its word. The help now defines each topic.

### Changed
- review-viewer: **Needs you** is its own tab, with a count, instead of a
  block at the top of the Summary tab, so the Summary opens on what the
  change is about. Items are grouped by what they block, so a finding's
  claim shows once above its needs; the review's own gaps come first.
- review-viewer: an accepted or deferred finding now leaves the Findings
  list, as a rejected one already did, so the list holds only what still
  needs a decision. The checkbox is now **Show decided and refuted**.
  j/k go on from a finding a decision just hid. Deciding the open finding
  opens the next one that still needs a decision and scrolls back up to the
  list; after the last, the finding page is empty. A decided finding keeps
  its marker in the code and its Go to entry; only a refuted one loses them.
- review-viewer: findings a gate rejected, and findings the review labelled
  low-value or a repeat of one you rejected, are no longer shown at all. No
  list, count, code marker, Go to result or risk score includes them, and a
  complete walkthrough need not link them. The Checked tab still lists the
  gate-rejected ones with the rule that rejected them. Before, repeats and
  low-value findings sat in a folded group and were counted.
- review: `review-<slug>.md`, `summary-<slug>.md` and the `<SEV>-only.md`
  rollups list and count only findings to act on: confirmed or unverifiable,
  and not low-value or a repeat. Low-value and repeat findings move to the
  appendix of `review-<slug>.md`, with the refuted, gate-rejected and merged
  ones; the summary no longer lists dropped findings. `findings.json` keeps
  every finding.
- review: a rerun on the same commit now keeps the previous run as an
  earlier round (`rounds/<sha>/`, then `<sha>-2/` for a second one), so it
  is checked for repeats against your decisions on that run. Before, a
  same-commit rerun had nothing to compare with.
- review: a verify call that fails is now tried up to three times, and an
  answer without an impact, or without a topic from the list, is re-asked.
  A finding still without a verdict stops the run with exit code 5 after the
  other verifiers finish, keeping their verdicts; `resume` asks again only
  for that finding. Before, such a
  finding was recorded as `unverifiable` with the reason "the verifier
  failed", lowered one step, and shown.
- review: `post` without `--decisions` posts the HIGH and MEDIUM findings
  the review lists, so never a low-value or repeat finding, and in critical
  mode only `breaks-*` ones.
- review: a finding labelled a repeat of one you rejected takes that
  rejection under its new wording. The next round matches the same wording
  by id, with no agent call, and the repeat stays hidden however many rounds
  it comes back.
- review-viewer: lines are picked to ask about on the line numbers only. A +
  shows on hover; click, drag down the numbers, or Shift+click to extend. A
  bar with **Ask about these lines** and ✕ appears, and Esc or ✕ clears it.
  Clicking a changed line no longer puts its block into the chat, and
  selecting code text no longer opens a popup.
- review-viewer: a finding on the Findings tab now reads in this order: the
  claim, **Failure scenario**, **Explanation** (the review's assessment and
  its reason, previously labelled "Assessment" and hidden for a useful
  finding), expected behaviour, verdict, cited code, needs, **Checked by**,
  **Explained in the walkthrough**, then the decision.

### Fixed
- review-viewer: a critical review whose findings are all below
  `breaks-*` no longer says they "were rejected by its checks or labelled
  low-value or repeat"; the empty list says how many critical mode left out.
- review-viewer: `serve --share slack` against a running viewer started
  without it now restarts that viewer, so the page offers Slack; before, it
  reused the viewer and the Slack button stayed disabled.
- review-viewer: changed lines no longer show a pointer cursor, since a
  plain click on them does nothing.
- review: the Needs you headings in `review-<slug>.md` and
  `summary-<slug>.md` no longer say every need has a command. A need that
  blocks a finding the review does not list (critical mode, low-value,
  repeat) is printed under that finding in the review's appendix, and the
  summary says how many it left out; before, it was in `findings.json`
  only.
- review-viewer: the count on the selected tab (Reviews, Findings, Checked)
  is legible again. It kept the muted text colour on the tab's selected
  background, 1.2:1 contrast on Solarized; it now takes the tab's own text
  colour.

## [0.31.0] - 2026-10-09

### Added
- review-viewer: a help dialog explains what the page shows. The **?** button
  in the header opens it: what the review does step by step (finders, a
  verifier per finding, the checks in code, merge, repeats, assess), what each
  finder (`diff-bugs`, `callers`, `history`, `conventions`, `tests`) looks
  for and how a tree review runs them, every column of the Checked tab's
  tables, what a merge group is, the `gate` and `seen` marks on a citation,
  the verdicts and labels, and the **Needs you** and **Run** blocks. The
  keyboard shortcuts are its last section, still opened by the `?` key and
  now by a ⌨ button. A small **?** beside each Checked tab heading, and beside
  Needs you and Run, opens the help at that section. Esc closes it and focus
  goes back to the button that opened it.

### Changed
- review-viewer: on the Findings tab, a finding the Checked tab has a
  verifier entry for no longer lists the cited code or the verifier's
  citations. A "Checked by" link near the top opens that entry instead, with
  everything the verifier read and cited. A finding with no such entry (a
  review written without `checks.json`, or a finding no verifier checked)
  still lists both, as before. Selecting a finding still moves the code pane
  to its lines, and a walkthrough step still lists both under each finding it
  links.
- review-viewer: the Summary tab's **Run** block moved to the bottom, below the
  PR description.

## [0.30.0] - 2026-10-09

### Added
- review: review code that is not a change. `--tree [COMMIT]` reviews every
  file of a repository at a commit (default `HEAD`), `--tree --path <dir>` one
  directory of it, and `--dir <path>` a directory that is not under git, which
  is copied into the output directory and committed there (the source is never
  written to; reviewing it again keeps the earlier commit, and two directories
  with the same name get separate reviews). Binary files, files over 200 000 characters, lockfiles and files
  marked `linguist-generated` or `linguist-vendored` are left out and listed in
  `skipped.txt`. The files are split into groups of up to 100 000 characters,
  one top-level directory at a time, and each finder runs once per group;
  `history` does not run. Findings pass the same gates as a change review; a
  problem in a file outside the review, a skipped one included, is reported
  as `pre-existing`. A `--repo` that is a subdirectory of a repository reviews
  that subdirectory, with `--path` relative to it.
  `checks.json` and the Checked tab have one entry per finder per group
  (`<dimension>@<group>`), and a finder's row in `run` sums its groups' time.
- review: `--plan-only` prints the file groups and how many finder calls the
  review would make, writes `plan.txt`, and stops before any model call. For a
  tree or directory, the session runs it first and asks before a run of more
  than 20 finder calls.
- review-viewer: `serve --tree [<commit>] [--path <dir>]` opens a tree review
  of that directory only; a subdirectory `--repo` opens that directory. Every file shows as added, the commit list is empty,
  and the "Risk of merging" pill is hidden.

## [0.29.0] - 2026-10-09

### Added
- review: when the review could not get information it needed, it now says
  so, and gives the command that would get it. Each such **need** names what
  was missing, what it blocks (a finding, or the review as a whole), and why:
  no access, a failed lookup, or not reachable on the web.
  - A verifier that answers `unverifiable` names what it could not read, with
    one read-only command (for example `gcloud run services describe …` or
    `pip download tavily-python==0.8.4 --no-deps`).
  - A finder that could not check something reports it as a need, never as a
    finding.
  - The pipeline's own failed lookups (the base branch's rules from
    `gh api`, an empty field from `gh pr view`) are needs too, with the exact
    command it ran, to be run in the reviewed repository so `gh` picks the
    same repository. Before, they were only a warning in the log.
  - A suggested command is kept only in a read-only form: `gh api` without a
    write method or body, `git log`/`show`/`diff`, `kubectl get`, `aws`
    describe/list/get, `curl` GET, SQL that only reads, and the like. Anything
    else (`git push`, an install, a `DELETE` request) is dropped and the need
    keeps only its description.
- review: `summary-<slug>.md` has a **Needs you** section after the counts,
  and `review-<slug>.md` lists each finding's needs under it. After pasting
  the summary, the session asks you about each need and gives its command as
  `! <command>`, so the output lands in the conversation; it then says whether
  that output confirms or refutes the finding. The session never runs a
  suggested command itself.
- review-viewer: a **Needs you** block at the top of the Summary tab, each
  command in a code block with a Copy button and the label "suggested by the
  review: read it before running". The Findings tab shows a finding's needs.
- `findings.json` gains optional `needs`, at the top level and on each finding.
  `schema_version` stays 1; older files still open.

### Changed
- review: an `unverifiable` finding that has a need is lowered one step but no
  longer below MEDIUM, so it does not sink below findings you can already act
  on.
- review: a suggested command is kept only when it is one line under 300
  characters, starts with a known read-only CLI (`gh`, `gcloud`, `bq`,
  `kubectl`, `aws`, `az`, `terraform` with a read verb, `curl`, `pip`, `npm`,
  `uv`, `git`, `psql`, `mysql`), and chains, pipes or redirects nothing.
  Otherwise the command is dropped and the need is kept.

## [0.28.0] - 2026-10-09

### Added

- **review: what a review cost, in `findings.json`.** Each review's
  `findings.json` now has a `run` object: total cost, tokens (input, output,
  cache read, cache write), agent calls, wall time, the budget ceiling, and
  one row per stage (each finder, `verify`, `merge`, `repeats`, `assess`)
  with its calls, tokens, cost, time, and configured model and effort, and
  counts of what was found, rejected, refuted, merged, labelled `repeat` and
  labelled `low-value`. Wall time leaves out the gap before a `resume`. A call
  that returns no usage counts as 0 and is noted in the errors.
- **review: `runs.jsonl`.** `rollup` (and every review) writes one line per
  review with a `run` to `runs.jsonl` in the output directory, so "what did
  reviews cost this month" or "which stage costs most" is one `jq` query.
  `SKILL.md` gives both. `rollup` prints how many older files it skipped.
- **review-viewer: a Run block on the Summary tab** showing those numbers,
  with a line totalling every loaded review. Hidden for older reviews.

### Changed

- **review: the cost line in `summary-<slug>.md` and `review-<slug>.md`** now
  reads the same `run` object as `findings.json`, adding tokens and wall
  time, so the summary can no longer disagree with the file.

## [0.27.0] - 2026-10-09

### Added
- review: earlier rounds are kept. When a PR (or branch) is reviewed again on
  a new head, the last round's `findings.json`, review and `checks.json` are
  copied to `<out>/<slug>/rounds/<head>/` before the new run writes over them.
  Nothing there is deleted.
- review: a `repeats` stage after merge checks every finding against the ones
  you rejected in earlier rounds (your `viewer/decisions.json`). The same id
  matches directly; one agent finds the reworded ones. A match is labelled
  `repeat`, with the earlier round, your decision and your note.
- review: an `assess` stage reads the remaining findings together, with the PR
  description, the earlier rounds and your notes, and labels each `useful`,
  `still-open` (the same defect as an earlier finding you accepted or left
  open) or `low-value` (true but not worth acting on, for a named reason:
  context, covered or speculative). It may also merge more duplicates. Labels
  are checked in code; a bad one becomes `useful`. At most two more agent
  calls per review, none when there is nothing to compare. Both stages' agent
  calls are recorded in `checks.json` and on the Checked tab, and a finding
  the assess stage merges shows as merged in its finder's entry.
- review: `findings.json` carries each finding's `assessment` (optional; older
  files still validate). The review lists repeats and low-value findings in
  their own section at the end; the summary counts them and lists only useful
  and still-open findings by name. `post` leaves repeats out unless you
  accepted one.
- review: `assess-only <dirs> --report <file>` runs the two new stages on saved
  reviews and reports each label beside the decision you already recorded,
  without changing the reviews. A review that goes over the cost limit or
  fails is listed as skipped, and the report still covers the others.
- review-viewer: repeats and low-value findings fold into a collapsed group
  after the others, each with its reason. A repeat counts as rejected (from an
  earlier round) in the new "still need a decision" line and the merge-risk
  badge until you decide it; you can still accept, reject or defer it.

### Changed
- review: the merge stage also groups findings anchored in different files
  when they cite overlapping lines of any file, so one defect reported from a
  test and from the code it tests reaches the merge agent. This can add merge
  calls.

## [0.26.0] - 2026-10-09

### Added
- review: every review now writes a record of what it checked, so a review
  that finds nothing still shows what it looked at. `checks.json` and
  `checks-<slug>.md`, beside `findings.json`, list every agent the review ran,
  in the order they started: what it was given, every file it read (with the
  line range), every search, URL fetched and web query, its turns and cost, and
  what it concluded. A finder's entry lists every finding it returned, with
  what became of it (kept, a duplicate, merged, or rejected by a gate and
  which rule); a verifier's entry has its verdict and its own citations. Each
  citation says whether its quote is at the reviewed commit and whether the
  agent actually read, searched or was shown those lines. The record holds
  what the agents asked their tools for, never what the tools returned. The
  review's last output line is now `checks: <path>`.
- review: `findings.json` carries each finding's `verifier_citations`, the
  lines the verifier read to reach its verdict.
- review-viewer: a **Checked** tab shows that record: a checklist of finders,
  findings and merge groups, then one entry per agent. Rejected findings
  appear here with the rule that rejected them. The Findings tab lists the
  verifier's citations and links to the verifier's entry. Reviews made before
  this version open as before, without the tab.

### Changed
- Requires multiplai-core at `ee8715c` or later (`AgentRunResult.tool_calls`);
  the lockfile moves to it.

## [0.25.0] - 2026-10-09

### Added
- review: the `conventions` finder reads `coding-standards.md` files as well as
  `CLAUDE.md`, from the repo root down to each changed file's directory. A
  `coding-standards.md` holds rules for the reviewer only, so they stay out of
  the context of the agent writing the code. Its rules are enforced even when
  they state no consequence. All `coding-standards.md` files go in before any
  `CLAUDE.md`, so when the rules pass the 40 000-character cap, `CLAUDE.md` text
  is skipped first.
- review: the `tests` finder also reports tests the change adds or edits that
  cannot catch a change in behaviour: a test asserting a value copied from the
  code, one that reads a source file as text instead of running it, and one that
  mocks the dependency whose failure the code must handle. These are LOW at most,
  unless the test is the only one touching the changed behaviour.

## [0.24.0] - 2026-10-01

### Changed
- codebase-walkthrough is now a Python pipeline instead of a single prompt.
  Pointed at a module, it finds every repository that refers to it (each
  searched from its own root, so nested repositories are not missed), what
  the module calls, its database tables, cache keys, settings and env vars,
  and, with `--docs`, the vendor's documentation and API collection. Agents
  explain what code found; gates in code re-read every cited line at the
  commit and drop what is not there. The walkthrough adds end-to-end
  scenarios that cross repositories, a glossary, questions for the module's
  owner, and a coverage table of files explained, files not explained and
  claims dropped.
- The HTML page keeps the step-through navigation and Mermaid diagrams; the
  interactive force-directed architecture diagram is gone.
- It needs `--trust-repo` and ripgrep (`rg`; a run without it stops before
  any agent call and says how to install it), and stops at a `--max-usd`
  budget (default 50) with a checkpoint that `resume` continues. After a
  docs-gate stop, `resume --docs <url>` or `resume --no-docs` continues with
  the new decision and keeps the answers already paid for.

### Added
- `check`: re-verifies every `path:line` link and snippet in a finished
  walkthrough against the commits in its header; `--at-head` says whether the
  code has moved since.

## [0.23.0] - 2026-10-01

### Added
- review: for `--pr` targets the pipeline now reads the PR title and body and
  GitHub's rules on the base branch (`gh api repos/{owner}/{repo}/rules/branches/<base>`),
  and gives both to every finder and verifier. The description is shown as a
  fenced, untrusted list of claims to check; the rules block says plainly when
  no `required_status_checks` rule exists, so "CI gates the merge" is reported
  as false rather than taken on trust. Reading the rules can fail without
  failing the review.
- review: finders and verifiers have `WebFetch` and `WebSearch`, to look up how
  a dependency behaves at the version the repository pins (provider source,
  library docs, a CI platform's semantics). The merger still only reads. A web
  source can be cited after a repository citation; it is never checked by the
  gates, never the first citation, and never confirms a finding on its own.
- review: the `conventions` finder now looks outside the diff when the change
  retires a fact (it greps for other statements of the old one), when a rule
  says where something must be recorded, and when the change claims something
  about the project's settings or process. `pre-existing` now covers unchanged
  files the change executes, builds or makes stale, documentation included.

- ci: the review pipeline's tests now run on every push and PR, as a
  `review pipeline tests` job next to review-viewer's. They were run by hand
  only.

### Changed
- review: a verifier whose question is only partly settled by the repository
  answers `unverifiable` and names what is missing, instead of `confirmed`
  with a caveat in the text.

## [0.22.1] - 2026-09-30

### Fixed
- review: when the cost limit stops a review during the find stage, the
  findings from finders that already finished are now saved, and `resume` runs
  only the finders that have none. Before, they were lost and `resume` paid for
  every finder again (#251).
- review: an agent answer that does not parse is now re-asked as a one-turn
  reformat of that answer with no tools, not as a fresh run of the whole
  prompt. A finder's re-ask no longer repeats the review and its cost (#253).
- review: an answer with no JSON in it (a markdown report) now fails with
  "the answer contains no JSON object", not a parse error about a brace in its
  prose (#253).
- review: the `tests` finder is asked for untested changed behaviour as
  defects, not for a report on what the tests cover, and every finder is told
  up front that its answer is one JSON object (#253).

## [0.22.0] - 2026-09-30

### Added
- review-viewer: a **risk of merging** score (Low / Medium / High) in the page
  header and at the top of the Summary tab. Fixed rules combine how critical
  the changed code is, whether a revert can undo it, size, PR checks, the
  tests verdict and the confirmed findings nobody rejected; the badge lists
  the rules that fired and every input. It updates as findings are rejected.
- review-viewer: an optional `.review-risk.toml` at a repo's root maps paths
  to criticality tiers. It is read at the base commit, so a change cannot
  edit its own tiers, and it can only raise the session's tier, never lower
  it.
- review-viewer: walkthroughs carry a `risk` block (tier, revertable, and the
  reasons), required once complete.

### Changed
- review-viewer: the `commits` and `tests` assessments are judged by a fixed
  rubric in SKILL.md, and an assessment no longer restates a measured badge.
- review-viewer: assessment titles are at most 32 characters and badges no
  longer wrap. `walkthrough put` rejects `size` and `risk` assessments;
  walkthroughs written before 0.22.0 still load.
- review: findings that describe the same defect are **merged**. After
  verification, confirmed and unverifiable findings in the same file whose
  lines overlap are grouped, and one agent per group decides which are the same
  defect. The merged finding keeps the highest severity, every citation and
  every finder that reported it ("Reported by" in the review); the findings
  merged away are listed in the review's appendix with the finding they went
  into. Before, only word-for-word copies were merged.
- review: **no more proposed fixes.** The prescribe and check-fix stages are
  gone. Each confirmed or unverifiable finding instead carries an **expected
  behaviour**: one sentence from the verifier on what correct behaviour looks
  like, without proposing code. It appears in the markdown review, in the
  `post` PR comment (replacing "Suggested fix") and in `findings.json`.
- review-viewer: a finding shows its **Expected behaviour** where the fix used
  to be. Findings files from earlier reviews still open; their fixes are not
  shown.
- review: the default `--max-cost-usd` is **50** per target, up from 10.
  A 52-file PR spent $25 in the find stage alone, and 10 stopped it before
  any finding was verified.

### Removed
- review: the `prescriber_model` and `checker_model` keys in `review.yaml` and
  `review_prescriber_model` in `multiplai.conf`. `merger_model` sets the model
  of the new merge stage.

### Fixed
- review-viewer: a walkthrough step no longer restates its findings in its
  text; the page already shows them in full under the step. `walkthrough put`
  rejects a step whose text names a finding id.
- review-viewer: the change arrows (▲ ▼) reach every change in a file. Near
  the end of a file, or when the whole file fits on screen, Next used to stay
  on the same change and never turned off.
- review-viewer: the chat button no longer covers the last lines of the
  right-hand panel or the code; both end in space you can scroll clear of it.

## [0.21.0] - 2026-09-29

### Added
- review-viewer: a **"viewed" checkbox** on every changed file (and `v` for the
  open one), with a count and progress bar above the file list. Ticks are kept
  per review in `viewer/viewed.json`.
- review-viewer: each file in the list shows whether it was added, modified,
  deleted or renamed, and its `+`/`−` line counts.
- review-viewer: **split view** (base on the left, head on the right; `s`),
  **line wrap** (`w`), and the changed words inside a changed line are marked,
  not only the whole line.
- review-viewer: above the code, the function or class you are scrolled into,
  "change 2 of 5", and ▲ ▼ buttons (`n` / `p`) to jump between changes.
- review-viewer: **Go to** (`Ctrl+K`) finds a file, review step or finding by
  name.
- review-viewer: `?` opens a list of every shortcut.
- review-viewer: each side panel has a hide button (`Ctrl+B` for the files,
  `Ctrl+Alt+B` for the review panel). A hidden panel leaves a thin strip along
  its edge; click it to bring the panel back. Remembered like the theme.

### Changed
- review-viewer: the chat is a round button in the bottom-right corner instead
  of a message bar across the bottom. The button (or `c`) opens a drawer from
  the bottom with the conversation and the message box; it stays open until
  closed (`Esc`, `c` or Close). The button shows a spinner while an answer is
  being written, a dot for unread answers, and a note for an unsent message.
  Messages show who wrote them and when, and code blocks in answers have a
  Copy button.
- review-viewer: while the session is still writing, the Summary and Reviews
  tabs show placeholder blocks and what is being written, instead of a sentence.
  "Not authorised" and "server not answering" are cards over the page, and the
  page reconnects by itself when the server is back. A request the server
  never answers now gives up after 8 seconds instead of hanging.
- review-viewer: one set of sizes and spacing for the whole page, so controls
  in a row line up; short transitions on the chat, tabs and dialogs (none when
  the system asks for reduced motion).
- review-viewer: the line above the message box always says what the next
  message is about: `Context: <review step | finding | file:lines | the whole change>`.
- review-viewer: the theme, light/dark mode, panel widths and code layout are
  remembered across viewers (a cookie shared by every port, and by every
  OrbStack container). Before, each new viewer forgot them.
- review-viewer: the header no longer shows which session answers.
- review-viewer: a block's 💡 explanation is as wide as the visible code and
  stays in view when the code scrolls sideways; before, a file with long lines
  cut its text off on the right. "Follow up" opens the chat.
- review-viewer: scrolling past the end of a file no longer opens the next one.
  ‹ Prev / Next › buttons beside the file name do it instead. With a review
  step open that covers several files, they move through that step's files
  (and keep the step open); otherwise through every changed file.
- review-viewer: while any GitHub check on the PR is still running, the page
  asks GitHub again every minute (the server calls `gh` at most once a minute
  per PR), so "Checks: N running" turns into passing or failing by itself.
- review-viewer: if no walkthrough has appeared 3 minutes after the page
  loads, the Summary and Reviews tabs stop showing a spinner and say nothing
  has been written, and to ask for one in the chat.
- review-viewer: the server no longer stops itself 30 minutes after the last
  page closes; it runs until `stop` or until its container ends. `serve --idle
  <minutes>` brings the old behaviour back.
- review-viewer: Accept / Reject / Defer now sit at the bottom of the open
  finding, with the decision shown as a badge in the findings list.

## [0.20.0] - 2026-09-27

### Added

- **`review-viewer` has six themes, each light and dark**, picked from a Theme menu in the page
  header: Phosphor (green CRT), Amber (amber CRT), Turbo (DOS text-mode IDE),
  1-bit (black and white), Game Boy (four greens) and Solarized. They change
  colours, fonts and edges, not the layout; Default is the previous look.
  A button beside the menu cycles Auto, Light and Dark: every theme has a
  light and a dark version, and Auto follows the system setting as it
  changes. Both choices are remembered per browser. Pixel fonts are used for headings and tabs;
  code and prose use IBM Plex Mono. All fonts ship with the viewer, and the
  page's content security policy now allows fonts from the viewer itself.

### Changed

- **`review-viewer`: the left column lists only the changed files.** The
  findings list moved into the Findings tab, above the finding it opens, with
  the "Show refuted and rejected" switch. The review picker (shown when the
  viewer has more than one review) moved to the page header.
- **`review-viewer`: the right column can be resized** by dragging its left
  edge, like the left column; the code in the middle takes what is left. The
  width is remembered, and a double-click on the edge resets it.
- **`review-viewer`: the line above the chat box says what a message will be
  about.** "Linked to review: …" now reads "Asking about the open review: …"
  (or "…the open finding: …"): a message sent without an `@` reference
  reaches the session together with the review or finding open on the right.

## [0.19.0] - 2026-09-26

### Added

- **`review-viewer` opens a PR, a branch, your unpushed commits or a commit
  range, and walks you through the change.** Give it a PR number or URL,
  `owner/repo#N`, a branch name, a worktree path, `a..b` or `a...b`. A PR or
  branch is shown against its merge-base, the same commits
  `/multiplai-dev:review` would review. A worktree shows only the commits not
  yet pushed to its upstream.
- **A written walkthrough of the change.** The Claude Code session reads the
  commits and the whole diff and writes an overview and ordered reviews: what the change is for, the core change, its
  callers, tests, then config and generated files. Each review points at the
  lines it explains; clicking it jumps the code there, and `[` / `]` move
  between reviews. Reviews appear while the session is still writing them. A
  review can carry a mermaid diagram when the change alters a flow or a data
  shape.
- **Review findings load when a review of the same commits exists**, and each
  entry on the Reviews tab shows the findings in its code. A review of an older head of the same
  PR or branch is named in a banner and not loaded.
- The page header shows a PR's title, author and GitHub link.
- **The sidebar can be resized.** Drag the line between it and the code, or
  focus it and use the arrow keys; double-click resets it. The width is
  remembered in that browser.
- **Summary, Reviews and Findings tabs.** Summary shows the overview, the
  PR description, and badges for judging the change at a glance. Some badges
  are measured from git and GitHub: files and lines changed, size (small up
  to 400 lines, large past 1000 or 30 files, not counting lock files), tests
  changed against code, commit hygiene, TODOs and lock files added, and for
  a PR its checks, merge conflicts, draft and review state. The rest are
  the session's own assessments: whether the commit messages explain the
  change, and whether the new code is tested. Click a badge for its
  reasoning. Reviews holds the walkthrough steps. Clicking a file there
  opens the review that explains it, and a review's files are marked in the
  sidebar.
- **A 💡 button above every block of changed lines** asks the session to
  explain just that block. The answer appears in a strip above the block and
  is still there after a reload. Nothing is explained until you ask, so no
  tokens are spent on blocks you already understand. "Follow up" puts the
  block into the message box. These explanations stay out of the chat.
- **Questions and answers are a chat in the footer.** Collapsed, it is a
  one-line message box. A spinner beside it shows while the session is
  answering, and a dot marks answers you have not read. Clicking the box
  opens the whole conversation above it, oldest first. Each message is
  labelled with what it was about: a finding, a review, lines, or the whole
  change. Esc or clicking elsewhere collapses it. Clicking a code
  line adds its reference without opening the chat, so more lines can be
  picked; typing opens it. Answers no longer appear in the right-hand panel.
- Clicking a line or block adds a reference only once. A reference to lines
  that overlap or touch one already in the box widens it: a line and then its
  block leave one reference. The label above the box appears only when the
  message will be linked to the open review or finding.
- **The message box is a footer, always in view.** Type `@` for a list of
  the changed files, and add `:12` or `:12-20` for lines. Clicking an added,
  removed or changed line adds its whole block of changed lines as a
  reference, which you can edit. A block that only removes lines is written
  `@path:base:40-52`. The first reference with lines is sent with the
  question.
- **Unchanged lines fold away, as on GitHub.** A file opens showing its
  changes with three lines around each, plus any cited or picked lines; the
  rest folds into a bar that shows all of it, or 20 lines at a time from
  either end. Jumping to lines inside a fold opens it.
- **Scrolling past the end of a file opens the next one**, and past the top
  the previous one, in the sidebar's order; a bar at the bottom names the
  next file and can be clicked. The fling that reaches the end does not count:
  it takes a fresh push. The open file is highlighted in the sidebar and kept
  in view there.

### Changed

- For a PR target, `review-viewer` now runs `gh pr view`, and fetches the
  PR's commits from `origin` when the clone does not have them (objects only;
  no branch or remote-tracking ref is created). `--fetch` runs `git fetch
  origin` first. Other targets still make no network call; the page also
  loads mermaid from cdnjs.
- Diff colours follow GitHub's. Added lines are green and removed lines red,
  with a darker line-number cell, and removed lines are no longer struck
  through. Lines a finding cites get a blue bar in the margin and are never
  recoloured, so a cited added line stays green. Lines a review points at
  flash when you jump to them.
- The changed-files list groups files under their directory and shows each
  file by name; a long directory is shortened to `…/<last two folders>/`. Hover
  a name or a directory for its full path. The filter still matches full
  paths.

### Fixed

- **`review` and `review-viewer` no longer write into the repository you run
  them from.** Without a workspace `INBOX/`, a review went to `./reviews/`
  and a viewer's files to `./review-viewer/`, which is usually the working
  tree of the repository under review. Both now go under `~/.multiplai/`
  (`reviews/`, `review-viewer/`). `--out` and `--reviews-dir` still override
  it.
- **`review-viewer`'s three panes now scroll on their own.** On a long file the
  whole page scrolled instead, so jumping to a finding or a step's lines could
  leave them off screen.
- **The right-hand panel no longer scrolls sideways.** A wide table or a
  long code span in a PR description could push the whole panel's text out
  of view. Wide tables and images now scroll inside their own box, and long
  words wrap.
- The files no review explains are listed on the Summary tab. On the
  Reviews tab they sat between the list of reviews and the one you clicked,
  so every review looked like it said "Not explained". Clicking a review now
  scrolls its text into view.
- The Summary, Reviews and Findings tabs stay at the top of the right-hand
  panel while it scrolls.

## [0.18.0] - 2026-09-25

### Added

- **`review` reviews a branch, PR or commit range, and rejects in code any
  finding or fix it cannot ground in the reviewed commit.** Five finders look
  at the change from different angles; a fresh agent verifies each finding;
  another proposes a fix; a last one checks the fix against the code that
  consumes what it touches. Between the stages, Python re-reads every cited
  line range with `git show` and drops what is not there. A fix premise about
  what a setting means must cite a line that *uses* the setting, not the line
  that defines it; a premise about anything outside the repo is shown as
  "Assumption: … Ask: …". Output is a markdown review per target, a short
  summary per target that the session pastes into chat when the run ends (one
  line per HIGH and MEDIUM finding with its fix status, one line per dropped
  finding), HIGH / MEDIUM / LOW rollups across targets, and a `findings.json` that opens in
  `review-viewer`, where questions go to the session that ran the review.
  `post` puts the findings you accepted in the viewer into one PR comment,
  only when you say yes in the terminal. Agents get `Read`, `Grep` and `Glob`
  only, and the run stops at a per-target cost ceiling (`--max-cost-usd`,
  default 10).

## [0.17.0] - 2026-09-24

### Added

- **`review-viewer` shows a code review's findings next to the code, in the
  browser.** Point it at a `findings.json` and it opens a local page: findings
  grouped by severity, each one beside its file in full at the reviewed
  commit, with the diff marked and the cited lines shaded. A question typed
  under a finding reaches the Claude Code session that started the viewer
  within about a second, and the answer appears in the page as rendered
  markdown. Accept, reject and defer buttons record a decision per finding in
  `decisions.json`. With `serve --repo <path> --range <base>..<head>` it shows
  a plain diff and takes questions about selected lines. The page needs a
  per-start token that is never printed, and refuses requests from other web
  origins. The `findings.json` v1 contract ships as
  `skills/review-viewer/schema/findings.v1.schema.json`. Ported from the
  team's diffview tool.

## [0.16.0] - 2026-09-14

### Changed

- **The plan skill stops and asks when recon shows a mixed scope.** When the
  requested work spans mechanisms with different risk and effort profiles (a
  config change beside a protocol rewrite, a no-tools call path beside a
  tool-using harness), the planner now puts the split to the user as explicit
  choices before writing scope into the plan, instead of picking a piece or
  silently planning all of it. Pieces that can only ship together stay one
  scope.

## [0.15.0] - 2026-08-19

### Changed

- **buildme's reviews now need `--trust-repo`.** The per-block code review and
  the design and tasks audits run as subagents that can `Read`, `Grep` and
  `Glob` your repository, so they cross the same trust gate every other buildme
  agent crosses. A full `build` run already required `--trust-repo` (or
  `BUILDME_TRUST_REPO=1`); a `spec-generate` or review-only invocation did not,
  and now does. Without the flag those steps stop with the message that names
  it. The reviewer holds no `Write`, `Edit`, `Bash` or `NotebookEdit` in any
  step, and stops after 30 turns.
- **The reviewer reads the code, not just the diff.** Because it holds `Read`
  and `Grep` it can open the file a changed line calls into, and check whether
  a rule it wants to cite actually says what it thinks. A finding must now
  quote the rule and name the file it came from.
- **The reviewer reads your project's own `CLAUDE.md`.** buildme collects the
  repo-root `CLAUDE.md` plus the `CLAUDE.md` (and `CLAUDE.local.md`) in every
  directory that is an ancestor of a changed file, and hands them to the
  reviewer as the conventions the change must satisfy. A sibling directory's
  file is never included — its rules do not govern this change, and feeding
  them in manufactures violations. This is automatic and separate from
  `standards_files` in `specs/config.yaml`, which is still the way to push in a
  style guide that is not a `CLAUDE.md`.
- **The review names what a deleted line used to guarantee.** For every line
  the change removes or replaces, the reviewer states the invariant that line
  enforced and points at where it is re-established — or reports that it is
  not.
- **The tasks audit stopped second-guessing the implementer.** It flags gaps on
  the contract surface (a function signature, a file format, a wire protocol,
  an error the caller must handle) and no longer flags helper names, file
  layout or control flow inside a block. Those are the implementing agent's
  call.
- **Rubric generation falls back to twelve named code smells** — Fowler's, via
  the `mattpocock/skills` code-review checklist — when your project documents
  no standards of its own. A standard your repo does document always wins over
  the baseline.
- **The whole-change refactor pass looks for wasted work** — redundant
  computation and repeated I/O across blocks — and reports a wrong-altitude
  implementation without acting on it.
- **The board doc's In Review row now matches the protocol decided on
  2026-08-11**: In Review means a real PR into staging, for everyone, not a
  reviewer fetching a branch and diffing it. No code changed — `board.py`
  already stops at In Review.
- **The reviewer's standards block is capped per document.** Each `CLAUDE.md`
  and each `standards_files` entry is trimmed on section boundaries at 12,000
  characters, with the full section index always emitted so the reviewer (which
  holds `Read`) knows what it did not receive. Uncapped, a large conventions
  chain was rebuilt for every block and then sent to every panel member.
- **A change created before `use-cases.md` existed gets its plan rewritten.**
  Resuming such a change generates `use-cases.md`, and now regenerates
  `tasks.md` and `rubric.md` against it — otherwise the plan the build runs was
  written without the use cases it is required to cover, and the plan review
  spent its one regeneration reporting that.
- **The plan review's rewrite of `tasks.md` is re-audited.** The rewritten plan
  goes through the same vertical-slice shape audit the original does, and
  `rubric.md` is regenerated from it, so per-block review no longer scores new
  blocks against the rubric for the plan they replaced.
- **`[buildme.plan_review]` now actually follows `code_review.model` and
  `BUILDME_REVIEW_MODEL`.** The plan reviewer's model was frozen before either
  override was read, so a project that set `code_review: {model: opus}` still
  got the base model. `[buildme.plan_review] MODEL=` continues to win over both.
- **The oversized-plan check stopped reading English as file paths.** "read/write
  access" and "pass/fail gate" were counted as three top-level packages, firing
  the package-spread trigger on plans that named no path at all. A match now has
  to look like a path — an extension, a third segment, or backticks around it.
- **Blocks written as `def name(...)` no longer collapse into one group.** The
  interface graph keyed on the first token of a signature, so every such block
  looked like it produced the same thing and both split checks went silent.
- **A failed plan review is retried on a resume.** The phase pointer used to
  advance past it regardless, which meant an LLM failure skipped the review for
  the rest of the build.
- **The `many-files` split trigger is gone.** It could never fire (no caller has
  a file count before anything is built) and could not have changed the verdict
  if it had. Plan size is what the block-count and package-spread triggers
  answer.

### Added

- **A plan review runs between the design audit and the prototype.** A
  read-only reviewer reads `tasks.md` against `rubric.md`, the project's
  constraints and the use cases, and reports under six headings:
  `rubric-conflict`, `block-contradiction`, `constraint-violation`,
  `over-prescription`, `use-case-coverage`, `oversized-plan`. Critical and
  major findings drive **one** regeneration of `tasks.md`, then the review
  re-runs report-only and the plan stands. It prints
  `PHASE:PLAN_REVIEW:COMPLETE` and keeps the card in the Planning column. An
  `oversized-plan` finding *proposes* a cut and names the signature boundary it
  crosses; nothing here ever splits a plan, creates a ticket or moves a card.
  A failed plan review never fails the build.
- **`use-cases.md`, a new spec artifact** between `proposal.md` and `tasks.md`.
  It carries `## Personas` (who they are, the job they are doing, the
  constraint they are under) and `## Use cases` (`<persona> wants <goal> so
  that <observable outcome>`), and `tasks.md` now requires it alongside
  requirements, design and unknowns — so the work breakdown is written with the
  use cases already on disk, and the plan review can check that every use case
  is delivered by some block. `proposal.md` keeps Why / What Changes /
  Capabilities / Impact, with What Changes now stating Goals and Non-Goals
  explicitly; `design.md` dropped its `## Goals / Non-Goals` section, which
  restated them. This is a third artifact rather than two more proposal
  sections, decided on 2026-08-19: it diverges from the written plan, which
  called for putting personas and use cases inside `proposal.md`.
- **Per-step model and effort, set from `multiplai.conf`.** Three sections take
  effect today: `[buildme.review] MODEL=` picks the reviewer's model, and
  `[buildme.plan_review] MODEL=` / `EFFORT=` tune the plan review — its effort
  inherits `[buildme.review]` rather than the pipeline-wide value, so raising
  the reviewer raises both. Two more resolve and are readable on the config
  object but **no call site consumes them yet**: `[buildme.spec] MODEL=` and
  `[buildme.agent] MODEL=` change nothing today. `specs/config.yaml` still
  outranks `multiplai.conf`, which is a machine-wide default.
- **Two knobs for the oversized-plan check**, both under
  `[buildme.plan_review]`: `PLAN_SPLIT_BLOCK_TRIGGER` (default 8) and
  `PLAN_SPLIT_PACKAGE_TRIGGER` (default 3). The defaults are placeholders — no
  run archive has been measured — which is why they are knobs and not
  constants.
- **Part of the reviewer's prompt is borrowed, and says so.** Four blocks come
  from `anthropics/claude-plugins-official`'s `pr-review-toolkit` under
  Apache-2.0. They live under `build_pipeline/prompts/vendored/` with the
  upstream `LICENSE` beside them, each pinned by blob SHA, each header naming
  its source path and the fact that it was modified.
  `scripts/check_vendored_prompts.py` re-fetches every pin and reports drift;
  run it before a release.
- **`deepen` records where it left upstream.** It was re-synced against
  `mattpocock/skills` at `885e2ca` (2026-08-19) and now carries a "Divergence
  from upstream" section naming that commit and mapping every file to its
  upstream path.

### Removed

- **The security review step is gone from the pipeline.** The unwired
  `run_security_review()` function, its prompt and the `security_review` config
  toggle are deleted. Security review is out of scope for buildme — use Claude
  Code's built-in `/security-review` on the branch instead.

## [0.14.1] - 2026-08-17

### Fixed

- **`skill-creator`'s copy of the degradation contract no longer names a
  directory that was retired.** Its output-location rule told you never to
  write to `INBOX/`, `PLANS/`, or a workspace directory relative to the session
  cwd; `PLANS/` was retired on 2026-08-11. A skill author following the rule
  was being warned off a directory that does not exist. The rule itself is
  unchanged.

## [0.14.0] - 2026-08-14

### Removed

- **The `swift-build` skill moved to the new `multiplai-apple` plugin**,
  unchanged. It is mac-only capability — the Swift/Xcode toolchain, and from
  the multiplai-kit container the opt-in host SSH bridge — and multiplai-dev
  is a cross-platform pack, so the mac-only skill now ships as an explicit
  add-on. If you use `swift-build`, run
  `/plugin install multiplai-apple@multiplai` to keep it.

## [0.13.0] - 2026-08-13

### Removed

- **The `code-review` and `security-review` skills.** Claude Code ships
  built-ins under the same slash names, so both collided with them; typing
  `/code-review` or `/security-review` now resolves to exactly one skill —
  the built-in. The six language review checklists from
  `code-review/references/` live on in multiplai-kit under
  `dotfiles/reference/review/`. The two `security-review` references
  (`owasp-checklist.md`, `language-security.md`) are retired with the skill.

## [0.12.0] - 2026-08-06

### Fixed

- **buildme's agents can no longer reach a tool nobody remembered to deny.**
  Every buildme LLM call denies the tools it did not explicitly ask for, but
  the list it computed that denial from named ~18 tools out of the ~44 the CLI
  now ships. Everything unnamed stayed available — and auto-approved, because
  these calls run with permissions bypassed. That included a second
  code-execution tool (`REPL`), two ways to send text off the machine
  (`Artifact`, `SendMessage`), and the whole background/scheduled-execution
  surface (`TaskCreate`, `Workflow`, `CronCreate`, …), which is what a denied
  `Bash` is worth nothing against. The list is re-derived from the CLI's own
  schema, and its test now names the tools that carry the risk instead of
  comparing the list to itself.
- **A crash after the TDD build no longer re-runs the whole build.** The TDD
  engine deleted the build's checkpoint on success even when it was running as
  one phase of `buildme` rather than on its own. Everything after it — the
  documentation pass, the respec proposal, the push and PR — then had nothing
  to resume from: a crash there restarted at `tdd_build` with no block state
  and rebuilt every block from scratch. A crash in the moment between marking
  the build complete and deleting the checkpoint was worse still: the resume
  read "complete" and skipped the remaining phases silently, so the docs, the
  respec proposal and the PR never happened. Resuming a build now picks up
  where it stopped.
- **A crash while the checkpoint is being written can no longer corrupt it.**
  `.build-state.json` was written in place, so an interruption mid-write left a
  half-written file that no resume can read — and it is the only record a build
  has. It is now written to a temp file and renamed into place, so a resume
  always finds either the previous checkpoint or the new one.

### Changed

- **`python -m build_pipeline` exposes only the subcommands it documents.** The
  `migrate` (legacy `openspec/` layout) and `apply` (single-agent, non-TDD)
  subcommands are gone — both were undocumented, unreachable from the
  `/buildme` skill, and untouched by the last year of pipeline work. If you
  scripted either of them directly, use `build` / `spec-generate` / `tdd`
  instead.

## [0.11.0] - 2026-08-05

### Fixed

- **buildme loads the Django and React standards again.** Its stack map named
  `django-best-practices.md` and `react-best-practices.md`; the docs
  multiplai-kit ships are `django-drf-best-practices.md` and
  `react-nextjs-best-practices.md`. A name with no file on disk is skipped with
  only an INFO line, so for a month every Django and React build generated its
  specs with **no conventions to build to** while looking like it had them. If
  you have a `reference_docs:` override in `specs/config.yaml` naming the old
  filenames, update it too — an override replaces the built-in list.
- **A long reference doc no longer arrives cut mid-sentence.** The per-doc
  ceiling for inlining into spec-generation prompts was 8000 chars, set when the
  docs were ~20k; the Django doc is 61k, so 87% of it was dropped and the
  surviving 13% ended in the middle of a rule — which reads as a complete rule.
  The ceiling is now 24000 and overflow is reduced on section boundaries: whole
  sections, plus an index of *every* section in the doc so the generator knows
  what it did not receive and the TDD phases (which hold `Read`) can go get it.

### Added

- **FastAPI and Next.js projects resolve their standards.** A `fastapi`
  dependency now adds `fastapi-best-practices.md`, and a project that lists only
  `next` (with React arriving transitively) counts as React instead of resolving
  no frontend standards at all.

## [0.10.3] - 2026-08-05

### Fixed

- **The promotion gate no longer reports a cold dependency cache as a hung
  script.** Every command got the same 30-second budget, and the first
  `uv run` of a run has to build the environment first — resolve the lock,
  clone the git-sourced `multiplai-core` — which regularly exceeded it. The
  gate said `timed out`, which reads as a defect in the skill and was not one;
  about 40% of runs in one batch failed this way. The first uv-mediated command
  now gets a separate, generous budget for that one-off cost. Everything after
  it is still held to 30 seconds, so a script that genuinely hangs still fails,
  and now fails distinguishably — the message names the budget it blew.

## [0.10.2] - 2026-08-05

### Security

- **buildme no longer installs `cryptography` 49.0.0 (CVE-2026-69247, high).**
  If you have buildme installed, updating to 0.10.2 is the fix — the next run
  resolves `cryptography` 50.0.0. There is nothing to change in your own
  config.

  What went wrong: `skills/buildme/scripts/` shipped its own `uv.lock`, left
  behind when this repo consolidated onto a single workspace lock. After that
  consolidation nothing could regenerate it — `uv lock` run from inside the
  skill resolves the whole workspace and rewrites the *root* lock, never the
  nested one — so it stayed frozen on the day's versions while the root lock
  moved on. In this repo that was invisible, because in-repo runs use the root
  lock. On *your* machine there is no workspace above the plugin, so
  `uv run --project` found the frozen lock and used it. Dependabot filed the
  advisory correctly and opened patches; those patches edited the nested lock,
  which is the one file that could not be updated.

  The nested lock is now deleted, so an installed buildme resolves against the
  declared dependency ranges and picks up patched versions on its own. A new
  repo gate (`lint_workspace.py` → nested lockfiles) fails CI if a lockfile
  ever reappears outside the workspace root.

## [0.10.1] - 2026-08-04

### Fixed

- **`buildme`'s pipeline resolves on an installed copy of the plugin.** Its
  `scripts/pyproject.toml` now declares its own git source for
  `multiplai-core`, so `uv run --project <skill>/scripts` works standalone —
  an install is a copy of the plugin subtree only, with no marketplace
  workspace above it to resolve through.

### Added
- **A plugin README** (`plugins/multiplai-dev/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

## [0.10.0] - 2026-08-04

### Fixed
- **The skill promotion gate stopped working on skills in a uv workspace.**
  `promote_skill.py` decided how to run a bundled script by looking for a PEP
  723 header: found one, ran it under `uv run --script`; found none, fell back
  to bare `python3`. For a skill whose dependencies live in a `pyproject.toml`
  instead, that fallback meant every script died on `ModuleNotFoundError`
  before argument parsing, and the gate reported the *skill* as broken.

  It now looks upward for a uv workspace root and runs through it. The PEP 723
  path is unchanged, so single-file skills from elsewhere still work.

### Changed
- **`buildme` and `deepen` take their dependencies from the repo-root uv
  workspace.** Their `scripts/uv.lock` files are gone, replaced by one lock at
  the root. Run them with `uv run --project <repo-root>`; running from inside
  the script directory no longer resolves a project of its own.

## [0.9.1] - 2026-08-04

### Fixed
- **`/buildme` now gets its Claude Agent SDK version floor from
  `multiplai-core`, instead of stating its own.** Nothing was broken for you:
  buildme's hand-written pin and core's floor happened to be the same
  (`>=0.2.116,<0.3`), so the right SDK was installed either way.

  It was a latent version of a bug that already bit `deep-research`, which
  declared the SDK the same way and resolved 0.1.56 — the release that
  misparses the terminal message from modern Claude CLIs and fails a finished
  run with `Claude Code returned an error result: success`. Because every
  buildme LLM call goes through `multiplai_core.run_agent()`, core is the only
  place that floor is meaningfully known; a second copy in buildme's own
  manifest could only ever go stale, and would have kept installing an old SDK
  the next time core raised its floor. Buildme now depends on
  `multiplai-core[sdk]` and names no SDK version of its own.

## [0.9.0] - 2026-08-04

### Added

- **`/buildme` now updates your documentation as part of the build, so it
  arrives in the same pull request as the code.** After the TDD build and
  before the respec proposal, a new documentation phase reads the whole build
  diff plus the implementation notes, takes inventory of the documents your
  project actually keeps (`README*`, `CHANGELOG*`, `docs/**`), and updates
  whatever the change made stale — described behavior, flags, defaults, file
  layouts, and usage examples that would no longer work. Where your project
  keeps a changelog it adds a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
  entry written from the user's point of view; it creates a `CHANGELOG.md` only
  when your project's conventions show one is expected. Previously a build
  shipped code and left the docs for whoever reviewed the PR.

  What it writes lands in its own `docs(<change>): update documentation` commit,
  and the files are named in the PR body under **Documentation** so a reviewer
  knows the docs were rewritten rather than assuming they were untouched.

  The phase is **always on** — there is no flag and no `specs/config.yaml`
  toggle — and it is **non-fatal**: if the model call fails, the build still
  completes with the code it produced. It also does not pad. "Nothing needed
  documenting" is a legitimate outcome for a change with no user-visible delta,
  and the agent says so explicitly.

- **A new `DOCS_WARNING:` line when the documentation may be stale.** When the
  build changed source files, your project keeps a changelog, and the phase
  updated nothing, `/buildme` prints a warning naming the changed files and the
  changelog to check before merging. It is a **warning and never a failure** —
  a finished build is not failed over a judgment call about documentation.

- **A second `DOCS_WARNING:` for anything the documentation phase changed but
  did not report.** The agent can edit any file in the project, but only the
  files it *names* are committed — so an unreported edit (a source file touched
  by accident, a document it forgot to list) would otherwise sit in the working
  tree, absent from the PR and from the diff you review. `/buildme` now names
  those files instead. They are still not committed: the documentation commit
  stays limited to explicitly-named paths, which is what makes it safe to build
  from an agent's own report.

### Changed

- **`PHASE:DOCS_UPDATE:COMPLETE` joins the progress protocol**, and the kanban
  card stays in **In Development** through the documentation phase (updating the
  docs is part of producing the change, not a separate review stage).
- Builds resumed from a checkpoint written by an earlier version load and run
  the new phase; a build already past the respec proposal does not go back for
  it.
- The staleness warning no longer treats *code* that happens to live under a
  `docs/` directory as documentation. A build that only changed
  `docs/generator.py` used to report "nothing to document"; a file under
  `docs/` now has to read like a document (`.md`, `.rst`, `.txt`, …) to count
  as one.

## [0.8.0] - 2026-08-04

### Added

- **`/buildme` now refactors every block, on every tier — and proves it was
  safe.** The refactor step used to be off entirely on the advanced tier and
  unverified on the standard one, so the TDD loop effectively stopped at
  "the tests pass". It now runs after every block on both tiers, and the
  pipeline checks the result rather than taking the agent's word for it: the
  suite is re-run and every test file is re-hashed. If the suite is not green,
  or a single test file changed, the whole refactor is thrown away — the block
  is reset to its `impl(block-N)` commit and the build carries on with the code
  that was already green. A refactor can improve your block or do nothing to
  it; it can no longer break one.
- **A refactor that survives verification is committed**, as
  `refactor(block-N): <name>`, so you can see it in the branch history and
  review it separately from the implementation. Previously the refactor's edits
  were left uncommitted and got folded silently into the next commit.
- **One conservative whole-change refactor pass runs after the last block**,
  before the final review, over the cumulative diff. It exists to catch what
  is invisible while you are building block by block: the same helper written
  twice in two blocks, code nothing calls any more, a wrapper that only
  forwards, one concept named three ways. Its charter stops there — module
  boundaries and public signatures stay as designed, and behavior is preserved.
  It lands as `refactor: simplify across blocks`, carries the same
  suite-and-tests verification (a failure reverts the whole pass), is checked
  against your token budget like any block boundary, and is recorded in the
  checkpoint so resuming a build does not pay for it twice.

### Changed

- **The implementer prompt no longer tells the advanced tier "there is no
  refactor phase".** It says a conservative refactor pass follows, and still
  asks for production-quality code the first time — the later pass tidies, it
  does not rescue.
- **`[buildme.agent]` in `multiplai.conf` also tunes the whole-change refactor
  pass**, alongside the test writer, implementer, per-block refactorer and fix
  agents.

## [0.7.0] - 2026-08-04

### Added

- **`/buildme` now reads your codebase before it writes a spec.** A new
  **Codebase Analysis** phase runs between research and spec generation: three
  agents explore the repo in parallel (module structure and entry points;
  naming, error-handling and test patterns; dependencies, config loading and
  integration points) and write what they find to
  `specs/changes/<name>/codebase-analysis.md`. That report is fed into
  `design.md`, so the design extends the modules you already have instead of
  proposing a parallel structure beside them. At the review checkpoint the
  pipeline prints `REVIEW:CODEBASE_ANALYSIS:<path>` — read it before
  `design.md`, because it is where "why does it extend that module" is
  answered.

  The phase is skipped, with the reason logged, for a project that has no
  source files yet and for `--only` runs (which generate no specs), and a
  failure is never a build failure — the design falls back to treating the
  project as new.

- **Reference docs now reach spec generation, not just the TDD agents.** The
  best-practice docs under `$CLAUDE_CONFIG_DIR/reference/dev/` for your stack
  are inlined into the design and task-breakdown prompts (capped at 8,000
  characters each), so the plan is written to your conventions rather than
  corrected to them afterwards. Every run prints one `REFERENCES:<doc names>`
  line (or `REFERENCES:(none)`) naming exactly what reached the generator.

- **Frameworks are detected, not guessed from the manifest name.** A `manage.py`
  or a `django` dependency in `pyproject.toml`/`requirements.txt` adds the
  Django reference docs on top of the Python ones; `react` in `package.json`
  adds the React docs on top of the Node ones. Previously every Python project
  got the same two docs regardless of what it actually was.

- **`reference_docs:` in `specs/config.yaml`** lets you say which docs a project
  builds to. Keys are the detected stack (`pyproject`, `Package`, `package`,
  `Cargo`, `go`) or framework (`django`, `react`), and a key you set replaces
  the built-in list for that key alone:

  ```yaml
  reference_docs:
    pyproject: [uv-python-best-practices.md, our-house-style.md]
  ```

### Changed

- A reference doc named in the mapping but missing from disk is still skipped
  rather than failing the run — but the skip is now logged, and a project whose
  stack is detected while *none* of its docs resolve gets a loud warning
  instead of silently generating specs with no conventions attached.

### Fixed

- The prototype-feedback pass, which regenerates `design.md` and `tasks.md`
  once from the prototype's notes, now carries the same codebase analysis and
  reference docs the first pass had. Without that, a regeneration quietly
  rewrote the design away from the project's conventions.

## [0.6.0] - 2026-08-03

### Changed

- **`/buildme`'s design audit now fixes the specs it criticises, instead of
  printing the criticism and building anyway.** Until now the audit ran after
  spec generation, listed the gaps it found — a spec scenario with no task
  block, a design decision contradicting a requirement — and the build carried
  on against the same unchanged documents. You would find the warnings in the
  log after the fact, next to code built on top of them.

  Every gap the audit rates **critical** or **major** is now handed back to the
  generator, which rewrites `design.md` and then `tasks.md` once with those
  findings in front of it. The rewrite lands as its own commit
  (`docs(specs): regenerate design.md and tasks.md after design audit`), so
  the branch history shows what the critique changed, and `git diff` on that
  commit is a readable summary of it.

  The audit then runs one more time, **report-only**, and says in the log
  whether the critique landed:

  ```
  PHASE: design_audit_feedback_applied — 2 artifact(s)
  PHASE: design_audit_recheck — clean
  ```

  It is deliberately one pass, not a loop. If gaps remain, they are reported
  and the documents stand — a stubborn gap costs one extra audit call, not an
  open-ended spec-generation bill, and the review checkpoint is still where you
  decide. Minor gaps are reported only; they never trigger a rewrite.

- **The design audit also judges the plan, not just its consistency.** It
  previously cross-referenced the artifacts against each other. It now
  additionally reports: abstractions no requirement asks for (the factory with
  one implementation, the config knob nothing reads), task blocks mis-sized for
  review, design decisions with nothing a test could assert on, and edge cases
  no scenario covers — missing input, a dependency timing out, a retry after
  partial completion. Combined with the change above, this is what turns those
  observations into an actually different plan.

### Fixed

- **A resumed build no longer loses the spec-generation checkpoint.** The
  orchestrator kept an in-memory copy of the build state while spec generation
  wrote its own to disk, then overwrote the disk copy — discarding the record
  of which artifacts and which audits had completed. A resumed build could
  therefore re-run work that had already succeeded. The orchestrator now
  re-reads the state after spec generation, the same way it already did after
  the TDD phase.

- **The build no longer pays for a design audit whose answer it already has.**
  A full build reaches the audit stage twice — once inside spec generation,
  once from the orchestrator's own `DESIGN_AUDIT` phase — and the second
  arrival used to re-read all four artifacts through the model just to
  rediscover that the regeneration had already happened. It now checks the
  checkpoint *before* calling the model, so the second arrival is free. On a
  build whose audit found nothing actionable, this removes a whole audit call
  that previously produced only a log line.

- **The phase pointer can no longer move backwards after spec generation.**
  Re-reading the state (above) also picked up a pointer already sitting at
  `design_audit`, which the orchestrator then reset to `spec_generation`. The
  advance is now guarded, so an interrupted build resumes at the phase it
  actually reached rather than at one it had already finished.

## [0.5.4] - 2026-07-30

### Security

- **buildme's locked dependencies carried 21 known vulnerabilities (9 high);
  all are now patched.** If you run `/buildme`, the pipeline resolves its
  environment from `skills/buildme/scripts/uv.lock`, so these were the
  versions actually executing on your machine. Update the pack to pick up the
  fix — there is nothing else for you to do.

  Every advisory was in a *transitive* dependency, reached through
  `claude-agent-sdk → mcp` and its sub-tree; no dependency buildme declares was
  itself affected. The lock had simply gone a long time without regeneration.

  Worth knowing when judging urgency: most of these advisories describe
  **server-side** attack surface — MCP's HTTP and WebSocket transports,
  Starlette request handling, multipart parsing of untrusted uploads. buildme
  is a local CLI that acts as an MCP *client* and never binds a socket, so
  that surface was not reachable in normal use. The two with local relevance
  were a vulnerable OpenSSL bundled in `cryptography`'s wheels and a PyJWT
  algorithm-confusion flaw (CVE-2026-48526). Patch promptly; no emergency.

  Upgraded: `click` 8.3.2→8.4.2, `cryptography` 46.0.7→49.0.0, `idna`
  3.11→3.18, `mcp` 1.27.0→1.29.0, `pydantic-settings` 2.13.1→2.14.2, `pyjwt`
  2.12.1→2.13.0, `python-multipart` 0.0.26→0.0.32, `starlette` 1.0.0→1.3.1,
  `sse-starlette` 3.3.4→3.4.6, `uvicorn` 0.44.0→0.52.0.

  Deliberately *not* upgraded in the same change: `anthropic`,
  `claude-agent-sdk`, `pydantic` and `pytest`. A blanket refresh resolves
  cleanly but moves the pipeline's own runtime, which is a separate risk from
  a security patch and should be reviewable separately. No behaviour change
  and no API change in this release.

## [0.5.3] - 2026-07-27

### Changed
- **buildme validates effort names against `multiplai-core`'s table instead of
  a copy of it.** 0.5.2 moved the effort *logic* into core's `pick_effort` but
  left the list of valid names hand-mirrored in `build_pipeline/config.py`.
  Core v0.11.0 exports that table, so the copy is gone. No behaviour change
  today — the two lists agreed — but the drift this removes had a nasty shape:
  had they ever disagreed, a `[buildme] EFFORT=<name>` that buildme thought
  valid and core did not would have been resolved to `high` instead of falling
  back to your default, with no warning. Nothing to do on your side.
- **Pin: `multiplai-core` v0.9.0 → v0.11.0** (`pyproject.toml` + `uv.lock`) for
  the exported table. buildme is the only skill in this pack that pins core.

## [0.5.2] - 2026-07-26

### Changed
- **buildme: conf-file effort overrides now delegate to `multiplai-core`'s
  `pick_effort`** instead of a local duplicate, and the pipeline's core pin
  moves `v0.7.0` → `v0.9.0` (additive releases only; the vendored `uv.lock`
  moves with it). One behavior change: a `MULTIPLAI_EFFORT` *global in
  `multiplai.conf`* now caps `[buildme] EFFORT=` overrides the same way the
  `MULTIPLAI_EFFORT` env var always did — the two ceilings no longer disagree.
  Defaults are untouched: with no conf `EFFORT=`, effort stays unset and the
  SDK decides, uncapped, exactly as before.

## [0.5.1] - 2026-07-26

### Added
- **swift-build: `uitest` + `screenshots` verbs** for GUI self-verification
  (#55) — run XCUITest bundles and capture app screenshots from the container,
  with rm-rf guards on result-bundle and screenshot output paths.

### Fixed
- **buildme: explainer detector precision** (#74) — dotted module paths
  collapse to their declared distribution prefix, and builtins/stdlib members
  no longer surface as "new dependencies".

## [0.5.0] - 2026-07-26

Verification overhaul for buildme. The theme: reviewers **propose** and the
orchestrator **disposes**; the tests that gate a block must still be the tests
that gated it; and spend is bounded by money, not just by iteration counts.

The same release carries the skill-engineering gate (#61) — a proposed skill is
executed before it is installed — and the model × effort config axis for buildme
(#59).

### Added
- **Test-integrity gate** (`gates.unchanged_tests_gate`). The implementer's
  tool grant is per-tool, not per-path, so test files stay writable for the
  whole implement phase — and the review-fix agent is the same implementer, so
  every fix iteration re-opens the window. Buildme now sha256-hashes the
  block's test files when the RED gate passes and re-checks them at both
  windows. A silent change fails the block. `TEST CHANGE REQUIRED: <reason>`
  in the implementer's report downgrades it to a flag, re-baselines the
  hashes, and hands the reason to the reviewer as an unverified claim.
- **Structured findings + orchestrator adjudication.** Reviews emit discrete
  `ReviewFinding` claims (claim, severity, confidence, evidence, location).
  `_adjudicate_review_findings` re-judges each one on the main model with the
  full build context that a fresh-context reviewer structurally lacks;
  rejected findings are recorded and never reach a fix agent. Fails open — an
  adjudicator error keeps every finding. `code_review.adjudicate: false`
  **drops** findings rather than applying them blind.
- **Reviewer panel** (`code_review.panel`). Each member reviews the diff in
  its own fresh context, concurrently, and the results merge: dimension scores
  average with confidence scaled down by panel disagreement; identical
  findings combine confidence (noisy-or) and keep the harshest severity
  anyone assigned; spec verdicts are unioned, not intersected.
- **Graded review gate** (`code_review.gate` → `ReviewGatePolicy`). Dimension
  scores are discounted toward neutral by reviewer confidence, so an unsure
  reviewer moves the needle less. Spec compliance stays a hard floor. The
  3.5/1.0 thresholds now live in one place instead of being duplicated
  between `review_score_gate` and `ReviewResult.passed`.
- **Trajectory judgment in the final review.** The final reviewer receives a
  per-block history (review iterations, final weighted scores, rejected
  findings, declared test changes) and is asked to judge drift, scope creep,
  erosion, and test gaming across the whole build — the failures that are
  invisible to per-step review because they are spread thin.
- **Per-build budget** (`budget.max_tokens` / `budget.max_usd`). Every other
  loop bound is an iteration count, which does not bound spend. Spend is
  recorded per phase on every SDK call, checked at block boundaries, warns
  once at 80%, and stops with a breakdown of where the tokens went. It rides
  in `.build-state.json`, so a resumed build does not get a fresh budget.

- **Promotion gate for `/propose-skill` — run the draft, don't vouch for it**
  (`skill-creator/scripts/promote_skill.py`, #61). An approved skill is now
  written to a **draft** directory (`/tmp/skill-draft/<name>/`) and installed
  only after the gate passes. The gate checks frontmatter (required keys, known
  `model`/`effort` values — a typo there is silently ignored at runtime, so the
  skill quietly runs on the wrong tier) and **executes every bundled entry point
  with `--help`, expecting exit 0**. Until this existed, the first person to
  discover a bad import or a raising `--help` was whoever hit it mid-task, days
  later, with the authoring context gone.
  - `--contract` mode runs a skill's `CONTRACT.md` assertions: each case is a
    shell command plus a substring that must appear in its output. Pilots ship
    with multiplai-context's `costs` and `log-doctor`.
  - `quick_validate.py`, `init_skill.py` and `package_skill.py` updated to the
    same frontmatter rules; bundled `.sh` entry points across the marketplace
    now answer `--help` with exit 0 because the gate executes them.
- **`/propose-skill --from-session` — draft from a real trajectory** (#61).
  Diary entries and learnings are *summaries*: they record that a workflow
  happened, not the commands, flag values and dead ends it went through, so a
  skill drafted from them reads plausibly and fails on first use. This mode reads
  the raw transcript and splits what it finds into **stable steps** (literal
  commands) and **judgment points** (criteria for deciding, never a hardcoded
  answer). A step counts as stable only if it was seen done the same way for a
  *different* input — one occurrence is an anecdote. Dead ends are recorded too:
  the command that failed, and why, cannot be reconstructed from the successful
  path.
- **Model × effort as two config axes for buildme** (`config.py`, #59). Both are
  set from `multiplai.conf` with no code edit: `[buildme]` (`MODEL=`, `EFFORT=`)
  for the whole pipeline, plus `[buildme.spec]`, `[buildme.review]` and
  `[buildme.agent]` for per-step `EFFORT=`. A step section falls back to
  `[buildme]`, which falls back to the SDK default. The `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT` ceilings still cap the result, so a conf override cannot
  escape a budget run.
- **Model-upgrade re-test checklist** (`e2e-test/SKILL.md`, #61). Skills are
  prompts, and a prompt tuned against one model is not automatically correct
  against the next — a bump can change output format, verbosity or how literally
  an instruction is followed, none of which raises an error. Run on any
  `MULTIPLAI_MODEL` ceiling change, `model:`/`effort:` frontmatter change, or
  Claude Code default-model move: smoke-invoke every script, run the `CONTRACT.md`
  assertions, re-check the frontmatter tier against the cost report.

### Changed
- `ReviewScore` gained `confidence` (default 1.0) and `effective_score()`.
  Confidence shrinks a score *toward neutral* rather than scaling it down —
  scaling would make an unsure reviewer look harsher, inverting the meaning of
  low confidence. Existing fixtures at the default confidence score exactly as
  before.
- All `llm_call`/`agent_call` sites pass a `budget_label`, so a budget stop can
  name the phase that spent the money.

### Fixed
- **Test-integrity claims are scoped to one window.** `block.implementer_report`
  accumulates every agent's output, so a `TEST CHANGE REQUIRED:` declared during
  the implement phase kept passing the claim parse at every later review-fix
  iteration — one declared change authorized every later silent one, defeating
  the re-baseline it sits next to. Each window now sees only its own agent's
  report.
- **An unavailable test-file list no longer fails the block.** `git` failing
  (index.lock contention, timeout on a large repo) made every snapshotted file
  read as deleted and accused the agent of deleting the suite. The snapshot now
  distinguishes "listed nothing" from "could not list" and reports the gate as
  not-checked.
- **The integrity gate covers non-Python projects.** The path pattern was
  Python-only, so on a Swift/Go/TS repo nothing matched, the snapshot was empty,
  and the gate reported "not checked" for every block. Hashing now uses a
  language-agnostic pattern; the `def test_*` weak-test scan keeps the
  Python-only one.
- **A failing reviewer panel member no longer fails the block.** The panel
  gathered without `return_exceptions`, so N members meant N× the chance that
  one unreachable backend marked the block FAILED. Failed members are dropped
  with a warning and only an all-members-failed panel raises.
- **Failed `llm_call`s are charged to the budget.** Only `agent_call` recorded
  partial usage on failure, so a review that timed out after a 150k-char prompt
  spent real money invisibly.
- `ReviewResult.passed_with(policy)` added; the bare `passed` property is
  documented as the default-policy view and `run_code_review` now logs the
  configured verdict. With `code_review.gate` set, the logged verdict and the
  gate's decision no longer disagree.
- **`EFFORT=xhigh` is accepted.** `config.KNOWN_EFFORTS` mirrors core's private
  `_EFFORT_TIERS` and omitted `xhigh`, which sits between `high` and `max` — so a
  valid SDK effort was rejected with a warning and silently downgraded to the
  default. (Core's own table carries a comment warning against exactly this
  omission when the set is copied.)
- `_merge_scores` rounds half-up explicitly (bare `round()` is banker's
  rounding, so a 2/3 split went to 2 and a 3/4 split to 4) and discounts a
  dimension only one panel member scored — zero spread otherwise read as
  unanimous agreement.

### Notes
- Every new behavior is opt-in via `specs/config.yaml`. With no `panel`, no
  `gate`, and no `budget:` section, the pipeline behaves as it did in 0.4.0.

## [0.4.0] - 2026-07-20

Ports enforcement mechanisms from the `superpowers` plugin's methodology skills
into buildme's code-driven pipeline. The theme: buildme structurally enforces
red-green TDD, never marks failing work done, and reviews against a hardened
two-verdict rubric.

### Added
- **RED gate** (`gates.red_gate`, from *test-driven-development*). Between the
  test-writer and implementer phases the pipeline runs the suite and requires a
  non-zero exit failing for the right reason. A passing suite means the tests
  prove nothing (`rewrite_tests`); a collection/syntax error means broken test
  files (`fix_tests`). One retry each, then the block fails. RED and GREEN
  output are captured as block evidence and fed to the reviewer.
- **Two-verdict review** (from *subagent-driven-development*'s task reviewer).
  `ReviewResult` gained a spec-compliance verdict (`missing`/`extra`/
  `misunderstood`) alongside the scored dimensions; passing requires both.
  `CODE_REVIEW_PROMPT` now treats the implementer's report as unverified
  claims, verifies against the ground-truth diff, cites file:line with
  why-it-matters and how-to-fix, and acknowledges strengths first.
- **Integration circuit breaker** with escalation (from *systematic-debugging*).
  Three fix attempts; the third switches to a question-the-architecture prompt
  and escalates to `config.review_model`. The fix prompt carries the four-phase
  debugging protocol (read the complete error, reproduce, one hypothesis, one
  variable at a time). Exhaustion writes a diagnosis to `build-progress.md`.
- **Global Constraints + Interfaces threading.** `design.md` carries a REQUIRED
  `## Global Constraints` section; task blocks carry `Interfaces:`
  (`Produces:`/`Consumes:` exact signatures). Both are injected verbatim into
  agent and review prompts, plus earlier blocks' Produces signatures, so
  implementers use exact signatures rather than re-deriving them. Generated
  tasks are scanned deterministically for placeholders (TBD/TODO/"add
  appropriate error handling"/"similar to block N"), and the tasks audit gained
  spec-coverage traceability and cross-block signature-consistency checks.
- **REQUIRED report slots.** Test-writer and implementer reports close with
  `STATUS:`/`TESTS_RUN:`/GREEN evidence, plus explicit permission to stop
  ("bad work is worse than no work"). `NEEDS_CONTEXT`/`BLOCKED` fails the block
  with the agent's stated reason surfaced.
- **Testing anti-patterns** (from *testing-anti-patterns*). Mechanical
  detection of mock-assertion-only and mock-setup-dominant tests; the
  test-writer prompt and rubric Test Quality dimension gained the five
  anti-pattern checks as positive gate-function criteria.
- `--lenient-review` restores accept-and-continue on review exhaustion for
  unattended overnight runs.

### Changed
- **No silent DONE.** Confirmed-weak tests now fail the block instead of
  warning (the previously-unwired `TEST_QUALITY_PROMPT` auditor adjudicates the
  static scan, with one test-writer retry). Review exhaustion fails the block
  rather than marking it done regardless. `_run_final_review` uses a structured
  verdict over the full-build diff and fails closed — a FAILED verdict fails
  the build, and so does an unverifiable review (an exception yields
  `passed=False` with the error surfaced); neither is marked done, so a resume
  re-runs the review. `_verify_entry_point` actually smoke-runs the detected
  entry point under the repo-trust guard instead of reporting an unverified
  pass.

## [0.3.3] - 2026-07-18

### Added
- **swift-build: `swift`/`xcodebuild`/`xcrun` passthrough.** `swift-host.sh`
  now accepts these as top-level commands and forwards shell-quoted args to
  the host (from the optional `--package-path` dir). Enables host diagnostics
  and repair (`xcodebuild -runFirstLaunch`, `-version`, `-showBuildSettings`)
  and xcodebuild-free simulator builds (e.g. a SwiftPM cross-compile for
  `arm64-apple-ios17.0-simulator`) without leaving the gateway.
  An opt-in `--xcsift` flag (first passthrough arg) pipes the host output
  through the same trusted `2>&1 | xcsift --format toon --quiet` suffix as
  build/test — errors/warnings survive, build noise is dropped. Off by
  default because diagnostic output (`-version`, `-showBuildSettings`,
  `simctl list`) is the answer, not noise, and xcsift would filter it.

## [0.3.2] - 2026-07-17

Fixes from the 07-12→16 PR audit (`INBOX/pr-audit-multiplai-2026-07-12-to-16.md`).

### Fixed
- **buildme: resumed pre-baseline checkpoints no longer mis-baseline the
  review diff.** `run_block_tdd` stamps `baseline_commit` only when the block
  is genuinely starting (PENDING/TESTING). Resuming an old mid-block
  checkpoint (IMPLEMENTING/REVIEWING with no baseline) previously stamped
  current HEAD — hiding the block's own commits from the quality reviewer;
  now it keeps the documented `git diff HEAD` fallback and logs a warning.
- **buildme: one unreadable standards file no longer fails the block.**
  `BuildConfig.standards_text()` now catches per-file read errors
  (OSError/UnicodeDecodeError), logs, and skips — as its docstring always
  promised.
- **buildme: tasks-shape audit completion is recorded in checkpoint state,
  not inferred from file existence.** A crash mid-audit used to leave
  tasks.md DONE and silently skip the audit on resume; resume now re-runs it
  (idempotent) and logs when it is skipped as recorded-complete. A non-list
  JSON audit response (e.g. object-wrapped findings) now logs a warning
  instead of silently passing as "no findings".
- **buildme: `claude-agent-sdk` floored+capped** (`>=0.2.116,<0.3`) — 0.1.x
  crashed at import (same bug class as the dream hook crash).
- **swift-build: plain Linux (no container markers) now refuses the SSH
  bridge even when `SSH_BUILD_USER` is set**, matching the SKILL.md support
  matrix — the bridge assumes the container↔host identical-path mount.
  Also fixed the false "caches result" comment (the xcsift probe is now
  actually memoized per invocation).
- **skill-creator: the degradation-contract reference is now resolvable for
  installed plugins** — `docs/degradation-contract.md` is vendored into the
  skill's `references/` (repo copy stays canonical) and SKILL.md points at
  it plus the marketplace GitHub URL.

### Changed
- swift-build SKILL.md documents two current gateway limitations (paths with
  spaces / schemes with parens until the container-side unquote fix ships)
  and the `sim screenshot` host-path caveat.

## [0.3.1] - 2026-07-15

### Fixed
- **`swift-build` was unusable over the container→host SSH bridge.** The script
  appends `2>&1 | xcsift --format toon --quiet` to build/test commands, which
  the host gateway rejected as a shell metacharacter (`DENIED: shell
  metacharacter in command`) whenever the host had `xcsift` installed — so
  every containerized `swift build` / `swift test` failed. Paired with a gateway
  change (multiplai-container) that recognizes this one fixed, trusted suffix.
- **`discover_scheme` sent `2>/dev/null` over the bridge** — a latent `>`
  redirect that the gateway also denied, breaking scheme discovery (and thus
  `build`/`test`) for Xcode-project layouts. Removed; the sed/grep parse
  tolerates stderr.
- Corrected the SKILL.md "Gateway Compatibility" note that falsely claimed pipes
  work because the gateway runs `zsh -lc` on the full command.

### Changed
- **`swift-build` is now model-invocable** (`disable-model-invocation: false`),
  so Claude reaches for the skill instead of improvising raw `swift --version` /
  `ssh` calls that the gateway denies.

## [0.3.0] - 2026-07-14

### Added
- **New skill: `plan`** — author self-contained, executable implementation
  plans with a mandatory completion contract: verifiable "Done means"
  criteria, explicit "Constraints / out of scope" (including stop-and-ask
  gates), and a fresh-session self-containedness test. Plan files are
  directly consumable by "implement the plan", goal/autonomous runners,
  or buildme — no parallel goal document needed. Prompt-only, no scripts.

## [0.2.0] - 2026-07-10

Semantic model tiers for the buildme pipeline (requires multiplai-core ≥ v0.7.0).

### Changed
- **buildme now resolves its model from a semantic tier, not a dated literal.**
  `config.DEFAULT_MODEL` is now `pick_model("opus", task="buildme")` — the model
  family lives in `multiplai_core.env.CURRENT_MODEL` (one place to bump per
  quarter), still capped by the `MULTIPLAI_MODEL` ceiling, and retunable per
  task via a `[buildme] MODEL=...` section in `multiplai.conf` with no code edit.

### Fixed
- **Tier detection (DEV-3).** `detect_tier()` now derives advanced/standard from
  the resolved `DEFAULT_MODEL` instead of the `CLAUDE_MODEL` env var, which
  Claude Code never exports to Bash subprocesses — so the tier was permanently
  stuck on `standard` in production regardless of the pinned model. buildme now
  correctly runs the advanced (per-block) TDD path under an opus ceiling.

## [0.1.1] - 2026-07-09

Correctness fixes for the buildme pipeline from the 2026-07-08 code review.
All ~190 tests pass; each fix ships with a regression test.

### Fixed
- **Design/tasks/audit/rubric are grounded in the generated requirements
  again.** Four readers still globbed the never-written legacy
  `change_dir/specs/*/spec.md` while the only writer emits flat
  `change_dir/requirements/<capability>.md`, so every one of those prompts was
  built against an empty directory (`(no specs yet)`). `_read_specs`,
  `run_design_audit`, and the two rubric gatherers now read `requirements/*.md`
  (stem = capability name), mirroring `tdd_engine.assemble_context`. The
  vestigial `"specs"` artifact-id alias in `_build_prompt` is gone.
- **Agent timeouts are observable.** `agent_call` catches `AgentRunTimeout` and
  returns `AgentResult(success=False)` without raising, so every
  `except LLMCallTimeoutError` in the TDD engine was dead — `block.timed_out`
  never flipped and `EXIT_AGENT_TIMEOUT` (3) was unreachable (real timeouts
  exited 1). `AgentResult` gains a `timed_out` flag, the fatal
  test-writer/implementer paths propagate it to `block.timed_out`, and the five
  unreachable except blocks are removed.
- **Tier detection recognizes newer Opus models.** `detect_tier` used a literal
  allowlist (`opus-4-5/4-6/5/6`) that silently downgraded the skill-pinned
  `opus-4-7` to `standard`; replaced with an Opus `>= 4.5` version-range check.
  See the caveat below.
- **Block state is indexed by list position, not `block.number - 1`**, so
  non-contiguous LLM-generated `tasks.md` numbering can't silently no-op a
  status write.
- **Per-block commits no longer leak buildme bookkeeping.**
  `_git_commit_block_phase` excludes `build-progress.md` and `.build-state.json`
  from staging instead of `git add -A`.
- **`--change` can't escape `specs/changes/`.** The change name is normalized
  when resolving `config.change_dir` (shared `normalize_change_name`), so a
  traversal value can't send `archive()`'s `shutil.move` out of tree.

### Known limitation
- Tier detection is **inert in production**: Claude Code v2.1.x does not export
  `CLAUDE_MODEL` to Bash subprocesses (it exports `CLAUDE_EFFORT` but not
  `CLAUDE_MODEL`), and `SKILL.md` invokes the pipeline via a plain `uv run` with
  no `CLAUDE_MODEL=` prefix — so `detect_tier` always sees an empty model and
  returns `standard`, for every model, not just `opus-4-7`. The version-range
  fix is correct in isolation and future-proofs the day the model is plumbed
  through, but the tier stays inert until the skill propagates the model (e.g.
  `CLAUDE_MODEL="{model}" uv run …`) or the pipeline grows an explicit
  `--tier`/`--model` flag. Documented in the `detect_tier` docstring.
