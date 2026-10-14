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

## [Unreleased]

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
