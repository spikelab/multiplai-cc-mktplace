---
name: buildme
description: Builds a change from the specs /multiplai-pm:pm-spec marked ready - research, codebase analysis, design, tasks, audits and autonomous TDD implementation via a deterministic Python pipeline, ending in a PR. Refuses specs that are not ready, and sends requirements problems back to pm-spec in spec-issues.md.
when_to_use: 'Triggers: build me, buildme, bootstrap, full build, /buildme'
---

# BuildMe

Build a change whose "what" is already written, via a deterministic Python
pipeline: read the ready specs → research → plan the "how" (design, unknowns,
tasks, rubric) → audit and review the final plan → implement/test and review
fixes → prepare docs and archive → verify the final revision → publish.

**The step before is `/multiplai-pm:pm-spec`.** pm-spec writes the change's
proposal, use cases and requirements in `specs/changes/<name>/` (with the
interview, the ticket or PRD intake, the epic split and the prototype), checks
them, marks them ready in `.change.yaml` and commits the change dir. buildme
reads those three documents and never writes them: a build whose specs are not
marked ready, or changed after they were, refuses with `SPEC:NOT_READY` (exit
`2`) and writes nothing. A requirements problem buildme finds goes back to
pm-spec in `spec-issues.md` (`SPEC:ISSUES`, exit `4`), for
`/multiplai-pm:pm-spec revise <name>`.

Before any spec is written, the pipeline reads the repo it is about to change
(architecture, patterns, integration points) and resolves the reference docs
for the detected stack and framework, so `design.md` extends the code that
exists and follows the conventions the project already builds to.

Builds use an isolated worktree and branch by default. `--no-worktree` and
`--no-push` remain supported — see [Git lifecycle](#git-lifecycle). Publication
consumes the revision that passed final checks.


## Completion and recovery contract

- `RESULT:SUCCESS` means the configured test command passed after the last
  content writer. The checkpoint records the command, parallel-test setting,
  planning revision, Git HEAD, index and content fingerprint. Missing commands,
  unavailable evidence, timeouts, or tests that change tracked inputs fail the build.
- `RESULT:SUCCESS:spec-only` means planning completed; it says nothing about code
  verification. `VERIFICATION:passed` and `PUBLICATION:published|skipped|failed`
  distinguish verified code from push/PR delivery. A publication failure keeps a
  resumable checkpoint and does not repeat paid implementation when inputs agree.
- A completed historical checkpoint never republishes. Old in-progress plans
  revalidate their documents and reviews; a phase ordinal or old completion flag
  alone cannot prove review. Future checkpoint versions require an upgrade.
- Recovery compares staged blobs/modes and working files separately. An old stop
  record without index evidence cannot authorize discard. Preserve both versions
  in a stash or backup, inspect them, then restart in a clean checkout. WIP refs
  keep working files in their own tree and the index in their second parent;
  saving one leaves the caller's HEAD and index intact.
- The proposal, use cases and requirements are inputs, checked and never
  generated: a missing one stops the build and names pm-spec. A change to them
  after pm-spec marked them ready refuses the build until pm-spec marks them
  ready again; then buildme's design and everything after it are written again.
  Interrupted planning retries missing members. Rewrites invalidate dependent
  tasks, rubric, sizing and reviews; unresolved blocking findings stop the build.
- For pytest, RED requires a behavioral failure attributable to changed tests,
  after a healthy baseline. Collection/import/setup/environment errors do not
  count. Other runners remain usable and report RED classification as unverified.
- Budget limits apply before every provider attempt, including planning and
  retries, inside the concurrency slot. Resumes retain incurred usage. Already
  admitted calls can finish above the cap; no further calls are admitted once it
  is exhausted. Child-skill and epic aggregate spend remain separately accounted.

Optional-stage defaults are unchanged. The paired evaluation harness and its
pending live decision are in [evals/README.md](scripts/evals/README.md); deterministic
fixture runs establish harness behavior, not model quality.

## Prerequisites

- **`uv`** (https://docs.astral.sh/uv/) — the pipeline runs via `uv run`.
- **Network + git on first run** — the first invocation fetches the
  `multiplai-core` dependency.
- **The `multiplai-pm` plugin** — `/multiplai-pm:pm-spec` writes and readies
  the specs every build starts from (see [Step 0](#step-0-the-specs-come-from-pm-spec)).
- **Optional: the `multiplai-research` plugin** — the **Research** phase invokes
  `/deep-research`, which ships in `multiplai-research`. Without it, skip deep
  research (run with `--skip-research`).

## Flags

| Flag | Effect |
|------|--------|
| `--auto` | Skip review checkpoint (overnight/autonomous runs) |
| `--spec-only` | Stop after the design, the tasks, the design audit and the plan review — everything up to the review checkpoint, no TDD build |
| `--skip-research` | Skip the research phase |
| `--lenient-review` | Accept-and-continue when a block exhausts its review iterations without passing, instead of failing the build. Unattended overnight runs only — the default is to fail, so low-scoring work is never silently marked done. |
| `--skip-explainers` | Skip the unknowns/edge-case explainer pass for dependencies new to this project. `unknowns.md` is still written, recording the skip. The pass is **off by default**: its dependency scan flags spec words such as `assert` or `followed` as dependencies (#375), so the flag only matters when `explainers: {enabled: true}` turns it on. |
| `--accept-unknown-spend` | Resume after interrupted calls whose usage is unknown. Preserves the unknown records; the cap covers known spend only. Epic builds forward it to children and fixers for this invocation. Pass only when the user explicitly accepts that uncertainty. |
| `--no-worktree` | Build in the project dir on the current branch instead of creating a worktree + branch. **Required when the calling session is already inside a worktree** — see [Git lifecycle](#git-lifecycle). Also disables push and PR (they only ever act on a branch the pipeline created). |
| `--no-push` | Do not push the build's branch to `origin`. |
| `--no-pr` | Do not open a pull request after pushing. |
| `--pr-ready` | Open the PR ready-for-review instead of the default draft. |
| `--research-path <file>` | Research to build on. Its content is saved as `research.md` in the change dir and fed to the design (fenced as untrusted). Without it, the pipeline decides whether research is needed — see [Step 2](#step-2-research). |
| `--accept-size` | Build a plan the size gate says should be split, as one PR. The build remembers it, so a later retry or resume does not stop on size again. |
| `--force` | Start a new build even if this change already published. The new build gets the next free branch (`buildme/<change>-2`, …) and leaves the published one alone. Pass it only after the user said yes to the `BUILD:COMPLETE` question (below). A stopped build with a `.build-state.json` resumes with or without it. |
| `--approve-split` | Approve the epic split the build printed as `EPIC:SPLIT:<split.md>`, as `split.md` and `plans/` are on disk now. See [Epic mode](#epic-mode-one-input-several-changes). |
| `--no-epic-split` | Build the input as one change: no `EPIC_SPLIT` decision, no model call. An epic's child builds pass it. |
| `--epic-child` | Build as an epic's child: no spec-extract (the parent ran it), and the PR targets the default branch. The epic's build passes it to every child, so the child's saved command keeps it when run again by hand. `BUILDME_EPIC_CHILD=1` in the environment does the same: a child's command saved before 0.84.2 lacks the flag, so pass one of them when running it by hand. |
| `--base-ref <sha>` | Branch a new worktree from this commit instead of the source checkout's HEAD. An epic's child builds get origin's default branch, or the parent's commit that holds its `specs/registry` baseline (below); a build that re-binds to its worktree ignores it. When the commit is not on the default branch, the PR targets the branch that holds it (`--base`), except under `--epic-child` (or `BUILDME_EPIC_CHILD=1`), where the PR targets the default branch. Exits `1` when the `specs/config.yaml` committed at HEAD is not the version at that commit (push yours, or pull origin's, first). |
| `--trust-repo` | Required for every run that reaches a tool-using agent — which now includes the reviews. The per-block code review, the design audit, the tasks audit and the plan review all run as subagents that `Read`/`Grep`/`Glob` the repo, so a `spec-generate` or review-only run needs this flag too, not just a full `build`. `BUILDME_TRUST_REPO=1` is the environment equivalent. Without it the step stops and says so. An approved epic vouches for its own repo: it passes `--trust-repo` to each child build, review and fixer, and its merge step's one agent call (under `epic.merge: auto`) runs trusted for that call only. |

`specs/config.yaml` equivalents (CLI flags win): `explainers: {enabled: false}` (the default; #375),
`respec: {halt_on_contradiction: false}` (on: a `SPEC_IMPACT: contradicts` stops the
build with `SPEC:ISSUES`, below),
`git: {worktree: true, push: true, pr: draft|ready|none}`,
`mutation: {enabled: true, min_score: 0.8, timeout: 30, max_mutants: 300}`
(see [Mutation testing](#mutation-testing)).

### `specs/config.yaml` keys for buildme's stages

`/buildme setup` writes some of these (see [Setup](#setup)). Every default
reproduces a repo without the key; an invalid value logs a warning and keeps
the default. `intake.*`, `interview` and `prototype` are pm-spec's keys:
buildme ignores them.

| Key | Values | Default | Used by |
|---|---|---|---|
| `architecture` | repo-relative path | `""` | stored only, for now |
| `pr.template` | `auto` or a path | `auto` | stored only, for the PR stage |
| `pr.max_change_lines` | int | `1000` | stored only, for the PR stage |
| `findings.authors` | `collaborators` or a list of GitHub logins | `collaborators` | `/multiplai-dev:buildme-findings`: whose PR comments are acted on. `collaborators` means anyone whose `role_name` on the repo is `admin`, `maintain` or `write` |
| `findings.max_rounds` | int | `3` | `/multiplai-dev:buildme-findings`: fix-and-re-review rounds before it stops (`findings --max-rounds` overrides it) |
| `findings.allow_bots` | list of bot logins | `[]` | `/multiplai-dev:buildme-findings`: bots whose comments are acted on; any other bot's are listed and skipped |
| `mutation.enabled` / `mutation.min_score` | bool / float | `true` / `0.8` | the mutation-testing step; `timeout` and `max_mutants` too (see [Mutation testing](#mutation-testing)) |
| `models.<key>` | a step, a stage (`spec`, `agent`, `review`) or `default`, set to a model spec | `{}` | every model call; see [Tuning model and effort](#tuning-model-and-effort-multiplaiconf) |
| `tier` | `advanced` \| `standard` | unset | the tier, only when `models.default` is not a Claude model |
| `codebase_analysis_max_units` | int | `12` | the codebase analysis: the most explore agents it runs, one per slice of the source files plus one for the manifest and config files (none when the limit is 1). Over the limit, it keeps the files the change's documents name, then the most imported, and lists the rest under "Not analysed" in `codebase-analysis.md` |
| `block_facts` | bool | `false` | the TDD engine: the test writer and implementer of each block also get the facts from `facts.json` about the files the block's tasks name, each re-checked at the current HEAD (a fact whose quoted lines changed is dropped, and counted). Off until real builds show it helps |
| `regenerate_walkthroughs` | bool | `false` | after the build: re-run each walkthrough the build made stale under `docs/walkthroughs/` (see [Step 4](#step-4-report-results)) and commit it to the build branch. Only with `--trust-repo`, and only for a walkthrough that read the project's repo alone |
| `walkthrough_max_usd` | number | `10` | the spend limit of each walkthrough `regenerate_walkthroughs` re-runs; a run that reaches it leaves the old file in place |
| `spec_extract` | bool | `true` | the codebase analysis phase: when `specs/registry/` is empty, run `/multiplai-dev:spec-extract` on the project's Python code at the build's starting commit with `--apply`, and commit the registry files it writes (`SPEC_EXTRACT:` lines in [Step 4](#step-4-report-results)). Only with `--trust-repo`; never in an epic's child build (the parent runs it; a child with no dependency, in the epic's own repository, branches from the parent's commit that holds the baseline, and a child in another repository builds without one) |
| `spec_extract_max_usd` | number | `80` | spec-extract's `--max-usd`: its spend limit for the run folder, across every resume. Capped by spec-extract, not by buildme's budget; what it spends is in buildme's `SPEND:` line |

**Commit `specs/config.yaml`.** The build's worktree branches from `HEAD`, so an
uncommitted config never reaches it. With `git.worktree` on, the pipeline stops
with `ERROR: specs/config.yaml is not committed; the build's worktree would not
see it. Commit it first.` With `--base-ref` (every epic child) the worktree
branches from that commit instead, and the build still reads the checkout's
file, so the version committed at `HEAD` must be the one at that commit:
otherwise it stops with `... Push it, or pull origin's, first.` An epic checks
this before each child's first launch and launches nothing for it, so the next
run, after the push or pull, fetches again and goes on.

### Reference docs — the standards the specs are written against

Every build resolves engineering-standards docs from
`$CLAUDE_CONFIG_DIR/reference/dev/` and **inlines them into the
spec-generation prompts**. Inlining (rather than pointing at a path) is forced:
the spec-gen system prompt allows no tools, so the generator cannot go read
anything. This is why buildme has its own mechanism and does not rely on the
`DEV REFERENCES` pointer block that multiplai-context injects into ordinary
sessions — the two cover different phases and neither replaces the other.

Resolution, in order:

1. **Stack** — the manifest filename (`pyproject`, `package`, `Package`,
   `Cargo`, `go`).
2. **Frameworks** — read out of the manifests, not their names, because a
   Django app and a plain library are both `pyproject.toml`: `manage.py` or a
   `django` dependency → `django`; a `fastapi` dependency → `fastapi`; a
   `react` **or `next`** dependency → `react`.
3. **`reference_docs:` in `specs/config.yaml`** replaces the built-in list for
   whichever keys it names, leaving the rest alone:

```yaml
reference_docs:
  pyproject: [uv-python-best-practices.md, our-house-style.md]
```

Built-in map (`_DEFAULT_REFERENCE_DOCS` in `build_pipeline/config.py`):
`pyproject` → uv + project-structure · `django` → `django-drf-best-practices.md`
· `fastapi` → `fastapi-best-practices.md` · `package` → bun/vite/react ·
`react` → `react-nextjs-best-practices.md` · `Package` → swift + swift-testing ·
`Cargo`/`go` → none yet.

A name with no file on disk is skipped, and **renaming a doc without updating
the map silently removes it from every build** — the Django and React entries
were dead for a month that way. The run prints `REFERENCES:<names>` (or
`REFERENCES:(none)`) so what actually reached the generator is visible, and a
detected stack that resolves *nothing* logs a warning: it means the specs below
were written with no conventions to build to.

**Long docs are reduced, not cut.** A doc over `REFERENCE_DOC_CHAR_LIMIT`
(24000) is passed through `summarize_reference_doc`, which keeps whole sections
plus an index of **every** section in the doc — including the ones whose bodies
did not fit, so the generator knows what it did not receive, and the TDD phases
(which do hold `Read`) can fetch it. The old behaviour was a flat character cut
that ended mid-sentence, which presents a truncated rule as a complete one.

**The reviewer gets its conventions from two separate places.** The first is
automatic: buildme hands it the repo-root `CLAUDE.md` plus the `CLAUDE.md` (and
`CLAUDE.local.md`) in every directory that is an *ancestor* of a file the block
changed — `config.convention_files()`. A sibling directory's file is never
included; its rules do not govern this change, and feeding them in manufactures
violations out of rules that never applied. Nothing has to be configured for
this. The second is `standards_files` in `specs/config.yaml`, opt-in and for
style guides that are not a `CLAUDE.md`; it resolves separately (absolute path,
then project dir, then `reference/dev/`, then `$CLAUDE_CONFIG_DIR`) and is not
capped. The reference docs above reach the spec generator, not the reviewer.

## Git lifecycle

A build **creates its own git worktree and branch** and never writes to the
calling checkout:

- Branch `buildme/<change-name>` (collisions get `-2`, `-3`, …).
- Worktree at `$WORKSPACE/.worktrees/buildme-<change-name>` (falls back to
  `<repo>/../.worktrees/…` when `WORKSPACE` is unset).
- The worktree is created in BOOTSTRAP from `HEAD`, which must hold the
  ready specs: a change dir with uncommitted changes refuses with
  `SPEC:NOT_READY` (the worktree would not hold them), since pm-spec commits
  the change dir when it marks it ready. Every artifact buildme writes is born
  on the branch, and all later phases operate inside it.
- The build refuses to start (hard failure, no silent fallback) when the repo has
  uncommitted tracked changes, or the target branch already exists with unmerged
  commits. Commit or stash first.
- **A failed build re-runs where it failed.** Running the same command again
  (or an epic re-running a failed child) uses the failed build's own branch
  and worktree, keeping its commits and any commits added there since. It
  stops with exit `1`, touching nothing, when the worktree is on another
  branch (switch back, then re-run), or holds an uncommitted change the
  failed run did not leave (an edit made after the failure): commit or stash
  it, then re-run. A build that failed under 0.70.1 or earlier, with no
  commit of its own, re-runs in a new `-2` worktree as it did then. A re-run
  stopped before its bootstrap finished resumes in the same worktree, and a
  build that moved to a `-2` worktree re-runs in that one.
- **A build that published is not re-run without asking.** Its checkpoint is
  kept as `.build-state.complete.json` (in the change dir, or under
  `specs/archive/` after the archive move). Running the same command again
  writes nothing and exits `3` with `BUILD:COMPLETE:<path>` (see the marker
  table). `--force` starts a new build on the next free branch. A
  `--spec-only` build or a standalone `tdd` run keeps its checkpoint the same
  way, but that file does not stop the next run: a build without
  `--spec-only` continues from it.
- **`build-progress.md` is append-only.** A resumed run adds a `RESUMED`
  section under the existing header; a stop by SIGTERM, SIGHUP or Ctrl-C adds
  `INTERRUPTED by <signal>` and saves the checkpoint with the spend so far;
  every `build` and `findings` run ends with a `BUDGET` section (tokens and
  USD per step). The header names the model and effort the run started with
  and where each came from.
- **The worktree survives the run.** The pipeline never removes a worktree —
  including its own — never merges (except an epic child's PR under
  `epic.merge: auto`; see "Merging the children's PRs"), and never force-pushes. Deleting it is the
  calling session's decision, made from the workspace root.
- PUBLISH pushes the branch and opens a **draft** PR through the pr skill
  (`/multiplai-dev:pr`, run as `pr_pipeline open --no-push`), so the PR's
  description follows the repo's PR template, or the skill's standard one when
  the repo has none. Push/PR failure is non-fatal: the branch and worktree stay
  intact and the exact manual commands land in `build-progress.md`.
- One hook adds sections to the PR description without touching the pr skill:
  `PRE_PUBLISH_CONTRIBUTORS` in `orchestrator.py`, run by
  `_pre_publish_sections` after the last block and before publish. Each
  contributor writes a file under `<change dir>/pr-sections/` (never committed)
  whose first line is a Markdown heading, and the file is passed as
  `--section`. A contributor that fails is logged and skipped. The list is
  empty today; its first intended user is the walkthrough step that lists
  stale walkthroughs.

Commits you will see on the branch, per block: `test(block-N)`, `impl(block-N)`,
and — when the block's refactor pass survives verification — `refactor(block-N)`.
After the last block, one conservative whole-change pass lands as
`refactor: simplify across blocks`.

## The refactor phase

Every block runs test → implement → **refactor**, on both tiers. The refactor
step is verified rather than trusted: after the agent finishes, the pipeline
re-runs the suite and re-hashes every test file. If the suite is not green, or
any test file changed, the entire refactor diff is discarded (the block is reset
to its `impl(block-N)` commit) and the build continues with the implementation
that was already green. A refactor can improve a block or do nothing; it can
never break one. There is no "the test needed changing" escape hatch inside a
refactor window — that hatch belongs to the implementer.

After the last block, one **whole-change** pass runs over the cumulative diff,
before the entry-point check. Its charter is deliberately narrow: remove cross-block
duplication, dead code and needless indirection, and make naming consistent —
module boundaries and public signatures stay as designed. It carries the same
harness (suite green + tests unchanged, or the whole pass is reverted), is
budget-checked like a block boundary, and is recorded in the checkpoint so a
resume does not pay for it twice.

buildme reviews each block, not the whole change. The whole-change review is
`/multiplai-dev:review`, run on the PR.

### 🚨 MUST: pass `--no-worktree` when you are already inside a worktree

**Before invoking the pipeline, check whether the current session is running
inside a git worktree** (e.g. `git rev-parse --git-common-dir` differs from
`git rev-parse --git-dir`, or the cwd is under `$WORKSPACE/.worktrees/`).

**If it is, you MUST add `--no-worktree` to the pipeline command.** Without it
the pipeline tries to create a worktree of a worktree, which is not what anyone
wants. This is not optional and not a preference — it is the one case where the
default (`worktree on`) is wrong.

Note that `--no-worktree` implies **no push and no PR** — the pipeline only ever
pushes a branch it created itself. In that mode the commits land on your current
branch, and pushing / opening the PR is yours to do.

### What to tell the user afterwards

Parse and relay, from stdout: `WORKTREE:<path>`, `BRANCH:<name>`, `PUSHED:<branch>`,
`PR:<url>`. Always report the worktree path and that it was left in place
deliberately, plus the PR URL (or, on `PUBLISH_DIAGNOSIS:<reason>`, the manual
push/PR commands from `build-progress.md`). The PR's description comes from the
pr skill and follows the repo's PR template; these lines are unchanged by it.

## Test and model settings

The normal path uses the configured suite, block TDD, independent review and a
final verification after all writers. See the [test, mutation and model reference](scripts/README.md#test-and-model-reference)
for `tdd.narrow_tests`, `tdd.parallel`, mutation settings, `models:`, `efforts:`
and multiplai.conf examples. These defaults have not changed in this repair.

## Scale Assessment (MANDATORY)

After understanding what needs to be built, assess scale before choosing a path:

| Scale | Criteria | Path |
|-------|----------|------|
| **Trivial** | Single file, < ~20 lines, no design decisions | Just do it (no plan needed) |
| **Small** | 2-5 files, clear structure, no novel architecture | **Plan → Build directly** |
| **Medium+** | 6+ files, new architecture, TDD valuable | **Full pipeline** (pm-spec → buildme: design → TDD) |

**HARD RULE: Planning is never skipped unless the task is trivial.** A "small" task
(new skill, script with multiple files, config + templates) MUST get a plan even if
the full TDD pipeline is overkill. The failure mode is skipping straight to code
because "it's not big enough for buildme" — that's wrong. The plan catches structural
mistakes before you write code.

### Small path (Plan → Build)

1. Enter plan mode and create a plan covering: file structure, key design decisions,
   integration points, and what "done" looks like.
2. Present the plan for review.
3. Build directly (no pipeline subprocess needed).
4. Commit incrementally.

### Full pipeline path

Use for medium+ work where TDD and spec generation add value.

## Execution (Full Pipeline)

### Setup

**`/buildme setup`** configures buildme for this repo. Ask the user, one
question at a time: an architecture doc path if they have one, the PR template
(`auto` or a path), and whose PR comments count as findings (`collaborators` or
a comma-separated list of logins). Then run, passing only the flags the user
answered:

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  python -m build_pipeline setup --project-dir "{project_dir}" \
  [--architecture PATH] [--pr-template auto|PATH] \
  [--findings-authors collaborators|login,login]
```

It edits `specs/config.yaml` in place and keeps its comments and key order. A
flag you pass sets its key; a key you do not pass is left alone; nothing is
removed. It prints `ADDED:` / `CHANGED:` lines, then `SETUP:WROTE:<path>`. If it
also prints `SETUP:COMMIT_REQUIRED:<path>`, tell the user to commit the file
before the next build. The ticket system and the interview mode are pm-spec's
settings now; `config-get` does not know their keys (exit `2`).

### Step 0: The specs come from pm-spec

A build starts from a change whose specs `/multiplai-pm:pm-spec` marked ready.
A ticket, a PRD, a plan written with the plan skill, or a prompt all go to
pm-spec first; it runs the interview, splits an epic into children, writes the
proposal, use cases and requirements, and prints
`SPEC:READY:<change_dir>:<revision>` with the next step,
`/multiplai-dev:buildme <name>`.

When the user asks to build something with no ready change for it, run
`/multiplai-pm:pm-spec` for it first and come back with the change name it
used. When the change dir exists, the build itself says whether it is ready:
`SPEC:NOT_READY:<reason>` and the pm-spec command to run (Step 4).

### Step 1: Choose the change

The change name is the directory under `specs/changes/` that pm-spec wrote.
If the user did not name one, list `specs/changes/` and ask which. A change
whose `.change.yaml` lists `children` is an epic: building it builds its
children (see [Epic builds](#epic-builds-a-parent-and-its-children)).

### Step 2: Research

Unless `--skip-research`, run the pipeline without `--research-path` and it
makes one model call, on the ready specs, to decide whether research is
needed. If not, it writes the reason to `research.md` and continues. If it
is, it writes the topics to `research-request.md` and exits `3` with
`RESEARCH:NEEDED:<path>`. Then — this needs the `multiplai-research` plugin:
```
Invoke /deep-research with the topics in research-request.md.
Use --auto for autonomous mode, --quick for lightweight research.
Example: /deep-research --auto --preset standard "implementation patterns for [topic]"
```
and re-run the same build command with `--research-path <the research output>`.
If `multiplai-research` isn't installed, say so to the user and re-run with
`--skip-research` instead. The design reads `research.md`.

### Step 3: Invoke the pipeline

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  python -m build_pipeline --session-id "{session_id}" build \
  --change "{change_name}" \
  --project-dir "{project_dir}" \
  [--research-path "{research_output_path}"] [--accept-size] \
  [--auto] [--spec-only] [--skip-research] [--lenient-review] \
  [--skip-explainers] [--accept-unknown-spend] [--force] [--trust-repo] \
  [--no-worktree] [--no-push] [--no-pr] [--pr-ready]
```

**Important:** Always pass `--session-id` with the current session ID for log correlation.

**Important:** Add `--no-worktree` if this session is already running inside a
git worktree (see [Git lifecycle](#git-lifecycle)).

**Important:** Pass `--research-path` as an absolute path, or relative to
`{project_dir}`. The pipeline runs from its own scripts directory
(`uv run --directory`), so a path relative to your cwd is not found. A
`--research-path` that does not exist stops the run with exit `2` before
anything is built. On a resume, research the build has already saved as
`research.md` in the change dir is no longer needed: re-running the same
command works after a scratch file under `/tmp` is gone.

The pipeline handles: the readiness check, worktree/branch setup, bootstrap,
research, codebase analysis, the plan (design, unknowns with the explainer
pass, tasks, rubric, via change_manager) and the design audit — which
critiques the plan for consistency *and* quality (over-engineering, task
granularity, testability, uncovered edge cases) and folds every
critical/major gap back into one regeneration pass of `design.md` and
`tasks.md` before re-auditing once. The plan review
reads `tasks.md` against the rubric, the constraints and the use cases and
regenerates `tasks.md` at most once. Each re-audit is told which findings the
rewrite was asked to fix and stops the run when one is not `resolved` or when
it finds a new critical problem; new major and minor findings are reported
only. A stopped step prints those findings with their statuses. Resuming
without editing the plan stops again at no cost; editing the plan gives the
step one more rewrite. TDD implementation (test-writer + implementer agents per block),
integration gates, scored quality reviews, entry point verification, the
documentation update, the respec proposal, and publishing (push + draft PR).

**Requirements problems go back to pm-spec.** buildme never edits the
proposal, use cases or requirements. Three things stop the build with
`SPEC:ISSUES:<path>` (exit `4`) after appending one `## ` section per problem
to `spec-issues.md` in the build's change dir and setting `spec_issues` in its
`.change.yaml`:

- the design step answers a `SPEC_ISSUE: requirements/<capability>.md:
  <requirement>: <problem and fix>` line (the design is not written);
- a design-audit finding whose fix needs a requirements change
  (`needs_requirements_change`), at the first audit or the recheck;
- a TDD agent reports `SPEC_IMPACT: contradicts` while
  `respec: {halt_on_contradiction: true}` is set.

While `spec_issues` is set every run refuses the same way, at no cost. Run
`/multiplai-pm:pm-spec revise <name> --project-dir <the build's worktree>`
(the line after the marker names it): it fixes the requirements, clears
`spec_issues`, marks the specs ready again and commits them. Then re-run the
same build command: buildme's design and everything after it are written
again from the revised requirements. When the requirements changed after the
TDD build began, the build refuses with `Planning inputs changed after
implementation` instead, as for any edit to the plan's inputs then.

### Epic mode: one input, several changes

After the codebase analysis, one model call (`EPIC_SPLIT`) decides whether the
input is one change or several. One change goes on exactly as before and writes
nothing new. Several — an epic, a PRD with separate features, an issue with
sub-issues or a task list — stops the build before any spec is written: the
pipeline writes one plan per child (with the children it depends on) to
`<data_dir>/buildme/epics/<owner>-<repo>--<change>/` (`epic.json`, `split.md`,
`plans/<id>.md`) and exits `3` with `EPIC:SPLIT:<split.md>`.

Show `split.md` to the user, or post it on the ticket (see the marker table in
[Step 4](#step-4-report-results)), and stop. The user may edit the table in
`split.md` or any `plans/<id>.md` before approving; approval reads them as they
are then. Re-run the same command with `--approve-split`: it checks the table
and every plan again (ids, dependencies, no cycle, `# Plan:` and
`## Done means` in each) and records each file's hash. A failed check exits `1`
naming the file. After approval, the build prints
`EPIC:APPROVED:<epic.json>` and runs the children. A file changed after approval exits `2` naming it, and so
does an `epic.json` whose children no longer match the approved table and plans. Pass
`--no-epic-split` to build the input as one change without asking; it applies
only before the decision is made, so for a change that already split, start
again under a new `--change` name.

Only one process at a time writes an epic's directory. A second one exits `2`
with `epic <name> is running now (pid <n> on <host>, since <time>)`.

**Children in other repositories.** The `repo` column of `split.md`'s table
is empty (`—`) for a child of the epic's own repository. For a child that
belongs in another one, it is the path of that repository's local checkout,
absolute or relative to the epic's repository root (`../billing`). Approval
exits `1` naming the child and its repo when the path is not a directory, is
a directory inside another checkout rather than the top of one, or has no
`origin` remote, and approves nothing. The child builds in that
checkout (`--project-dir <checkout>`, based on that repository's origin
default branch); its own preflight refuses tracked uncommitted changes there,
as for any build. Its PR is reviewed, fixed and merged in its own worktree,
and merges in different repositories never wait on each other. A child that
depends on a child in another repository waits for that PR to merge. A
checkout that is gone when a run starts, or before its child launches, parks
that child with `repo <path> not found`, and it stays parked on later runs;
the reviews its PR already had still count toward `epic.max_usd`. When `git`
times out or does not start there, nothing parks: the child is `unknown` for
that run, and the next run asks again.

**Running the children.** The approved build runs each child as its own
`build --mode brief --change <epic>-<id> --plan-file <epic dir>/plans/<id>.md`
subprocess with `--auto --trust-repo --no-epic-split --epic-child --pr-ready`, in the
parent's project directory (or the child's own `repo`, above), from origin's default branch (`--base-ref`), so
each child gets its own worktree, branch and ready PR. When the parent's
codebase analysis committed a `specs/registry` baseline (spec-extract), a
child with no dependency in the epic's own repository branches from that
commit instead, so it builds on the baseline and the registry reaches main
with the first merged child PR; its PR still targets the default branch. It
does so only when origin's default branch holds every commit before that one
and the commit changes nothing outside `specs/registry`: a parent run from a
feature branch or with unpushed commits, or a commit added before a resume,
gives every child origin's default branch, without the baseline, so no child
PR carries those commits. Once origin's default branch holds a registry (the
first child PR merged), a child launched later branches from it, with main's
newer code. A child that waits on a dependency still branches from origin's
default branch, after that merge. A `specs/config.yaml`
(the parent's, or that of a child's own `repo`)
that sets `git.worktree` or `git.push` to `false` would build the children in
place or open no PR, so the build exits `2` before launching any. The parent's
`research.md` reaches each child as `--research-path` (a parent that skipped
research passes `--skip-research`), so no child decides on research again. A child starts once
every child it depends on has **merged**; dependent children never stack on an
open PR. Before a child's first launch the build runs `git fetch origin`; when
that fails for a child with dependencies, the child is `unknown` and is not
launched, since its base could lack their merges. At most `epic.max_parallel`
build at once (default `2`). Each run
writes `agents/<NNN>-build-<id>.{started.json,result.json,log}` and ends with
`runs/<NN>/summary.json` (each child's status, PR and reason), printing
`EPIC:SUMMARY:<path>`. A child that exits `3` (for example `SPLIT:REQUIRED`) or
exits `0` without a PR is parked, and the children that depend on it are
skipped; a child that fails is relaunched once per run. While a child waits on
an open PR, the build exits `3` with `EPIC:MERGE_WAITING:<urls>`. Re-running the
same command continues from what is on disk: a child still running from an
earlier run is waited for, never started twice, and a PR a stopped child
opened is found and kept. When `gh` or `ps` fails while checking a stopped
child, or `gh pr view` fails for a PR another child waits on, that child is
`unknown`: nothing is launched for it or its dependants, and the run exits
`1`; re-run once they work. A failed or `unknown` child makes the run exit `1` even beside a PR that only waits for a person to merge it, and `3` while a child waits on a dependency's open PR. Each failed or `unknown` child is named on stderr
(`EPIC: child <id> <status>: <reason>`), beside `EPIC:MERGE_WAITING` too. Ctrl-C (or SIGTERM) stops every child, those
started by an earlier run included, and exits `130` (`143`). SIGHUP (a closed
terminal) ends only the run, with exit `129`: the children keep running, and
the next run waits for them. An ignored SIGHUP or SIGTERM (SIGHUP under
`nohup`) stays ignored, and the run goes on.

`specs/config.yaml` takes an `epic:` block: `epic.max_parallel` (default `2`),
`epic.max_review_rounds` (default `3`), `epic.round_worth` (`breaks` or
`any`, default `breaks`; which Code MEDIUM earns another review round, below),
`epic.merge` (`auto` or `manual`,
default `manual`; under `manual` a human merges each child's PR, and under
`auto` buildme merges the default branch into it, tests and pushes, waits for
the PR's required checks, and merges it with `gh pr merge --merge`;
a version, CHANGELOG or lockfile conflict there costs one agent call, with
Read, Edit and Bash in the child's worktree, counted against `epic.max_usd`) and
`epic.max_usd` (one ceiling for the whole epic, default none; each child keeps
its own `budget.max_usd` too). `build` and `findings` print
`SPEND:<usd>:<tokens>` as their last line on every exit but a kill (SIGTERM
and SIGHUP included): what that process spent, the PR body's model calls and a
walkthrough regeneration included (the pr skill and the walkthrough print
their own `SPEND:` line, which the build adds), so a resumed build reports only
its own spend and a several-round `findings` reports every round. When one of
those skills printed none (killed or timed out), the line ends `:incomplete`.
The epic's spend is each child attempt's `SPEND:`, plus what each merge
agent call spent, plus the reviews of the
children's PRs in the review ledger (`<data_dir>/reviews/ledger.jsonl`: each
review run once, its resumes in it, a fresh review of the same head added);
`epic.json` and each summary hold it as `spent_usd`, with
`spend_complete: false` when an attempt was killed before it printed `SPEND:`,
its `SPEND:` ends `:incomplete`, a merge agent call was interrupted, a review
has not finished, or a review was killed and then run again fresh instead of
resumed. Before each launch, once
`epic.max_usd` is reached, nothing more is launched: the run waits for the
children already running and exits `3` with `EPIC:BUDGET:<spent>`. A fixer
runs several rounds in one launch, so it is given what is left of
`epic.max_usd` as `findings --max-usd`: the cap less the epic's spend and
less what the other fixers still running were given and have not yet spent
on their PRs' reviews. While running fixers hold all that is left, another
fixer waits for them, and no build, review, review resume or merge agent
starts; once they end, their real spend decides. A fixer still running from
an earlier run (one killed outright, or a fixer that outlived its stop) holds
nothing in this run: this run waits for it only before that child's next fix,
so the epic can spend past `epic.max_usd` by up to that fixer's own
`--max-usd`, and `spend_complete` is false. A fixer stops before a
round with something to act on once its own spend and its reviews' reach
its share. If the epic's spend is still under the cap, other fixers held the
rest: it runs again with its share recomputed once they end. Once the spend
has reached the cap, the run exits `3` with `EPIC:BUDGET` as well, the child
is not parked, and a re-run after `epic.max_usd` is raised runs the fixer
again. A fixer whose last round left
nothing to act on ends as done even past its share, so its PR goes on to
the merge step. The round that crosses a fixer's share finishes, so the epic
can end up to one fix round (a fix and a review) per fixer running at once
past the cap, plus what children already running when it was reached go on
to spend. A fixer an earlier,
stopped run left running is not counted in another fixer's share.

**Reviewing the children's PRs.** Each child's open PR is reviewed with
`/multiplai-dev:review` (`python -m review_pipeline review --repo <child
worktree> --pr <n> --trust-repo --fetch --extra-finders`, run as a
subprocess; `--fetch` brings in a base a sibling's merge moved, and
`--extra-finders` adds one finder per ~600 changed lines, at most 6, on the
PR's first review round only), at most once per head:
when the review ledger (`<data_dir>/reviews/ledger.jsonl`) already holds a
complete review of the PR at its current head, no review starts and the
record says `reused: true`. While a review of that head is running (its
`started` ledger line, its run lock held), nothing is launched: the
coordinator looks again every 60 s and then uses what it finished. A review
of the PR another process is running that the ledger does not show yet
(exit `2`, `is running now`) is waited for, asking again every 60 s; an
interrupted one (exit `4` with the resume hint) is continued with
`review_pipeline resume`. The review reads the PR's head itself, so a commit
pushed as it starts is the head it reviews; the coordinator then reads the PR
again and uses that review. The epic's budget is checked before each of these
launches. When the review cannot read git or GitHub before reviewing anything
(exit `2`: its `git fetch` or `gh pr view` failed, or the PR's head or base is
not in the clone after its fetch), nothing parks: the child is `unknown`, as
when the coordinator's own read fails, and the next run asks again. So is a
child whose review, or whose post of the review's findings, cannot start
(`uv` is not on PATH, or its worktree was removed as it starts). When another child depends on it, the same run reads
its PR again and waits on it as an open PR, so the run ends with
`EPIC:MERGE_WAITING` (exit 3) rather than naming it `unknown`; the next run
still reviews it. A child whose worktree was removed is reviewed from the
source repo.
Any other failure parks the child with the end of the review's output. Each
review writes `agents/<NNN>-review-<id>.{started.json,result.json,log}`; the
result holds `head_sha`, `base_sha`, `review_dir`, `cost_usd`, `pr_round` (the
number of distinct heads of this PR the ledger holds a complete review of),
`reused`, `conflicts` (from `gh pr view`; recorded, never blocking) and
`blocking` (the open Code findings at HIGH or MEDIUM) and, when it has
blockers, `next` and `next_reason`: what follows it, below. An open Code HIGH
at `epic.max_review_rounds` rounds parks the child, and it stays parked. A review counts against `epic.max_parallel` and `epic.max_usd` like a
build, and the summary's `rounds` is each PR's `pr_round`. Ctrl-C or SIGTERM
stops a review this run started, with the children; a review an earlier run
left running, or one another process runs (your own `/multiplai-dev:review`,
say), is not signalled: it keeps running, and the next run uses what it
finished. SIGHUP leaves a review running, and the next run uses what it
finished.

**Is another round worth it?** After each review that leaves blockers, the
coordinator records on the review's record what follows it
(`next`) and why (`next_reason`), from the review's own fields, with no model
call:

- `round` (another fix and review): a Code HIGH is open, or, below
  `epic.max_review_rounds`, a Code MEDIUM earns a round. Under
  `epic.round_worth: breaks` that is a MEDIUM the review's verifier rated
  `breaks-users` or `breaks-business`, or did not rate; under `any`, every
  MEDIUM, as before 0.74.
- `final_fix`: only Code MEDIUMs are left and none earns a round, or the PR
  is at `epic.max_review_rounds` with only Code MEDIUMs. One fixer runs one
  fresh round with `--no-review` (`--max-rounds` one more than every round in
  the change's `.findings-state.json`, finished or not, as `findings` first
  finishes a round an earlier fixer left open): it fixes, pushes and replies,
  and nothing reviews its commits. The head it leaves goes to the merge step (under
  `epic.merge: manual` it waits for a human merge, exit 3), and buildme files
  one issue on the child's repository, titled `buildme epic <epic>: what the
  final fix of #<n> left`, listing each blocker with its finding id and saying
  the last fixes had no review. When the fixer finds no new comment to act on
  (every blocker was already on the PR, from an earlier round), it runs no
  round and ends `FINDINGS:DONE`; the PR still goes to the merge step, and the
  issue says no fix ran on what it lists. A run that finds an issue with that
  title, open or closed, files none.
- `park`: a Code HIGH is open at `epic.max_review_rounds`.

The reviews are its own and each fixer's (below). Every fixer launch runs one
round, whose review is the PR's next, so a decision follows every review.

A failing test is caught where it was before. Under `epic.merge: auto`, the
merge step's tests and the PR's checks park the child. Under `manual`, buildme
runs neither, and the person who merges the PR reads its checks. A fixer stopped part-way is run
again once with the `--max-rounds` it was launched with, so a round it
finished is never paid for twice.

**Fixing the children's PRs.** A review whose `next` is `round` runs the fix
loop. First the review's findings are
chosen as `--auto` chooses them, kept to what the epic blocks on (the Code
findings at HIGH or MEDIUM, in `buildme-decisions.json` next to the review's
`findings.json`, outside the worktree) and posted with `python -m
review_pipeline post <review dir> --decisions <that file>`, run in the child's
project directory (`agents/<NNN>-post-<id>.*`). Then the fixer runs there:
`python -m build_pipeline findings --pr <n> --change <epic>-<id> --project-dir
<child project dir> --auto --trust-repo --max-rounds <M> --severities
HIGH,MEDIUM --code-only --no-escalation [--max-usd <left>]` (`agents/<NNN>-fix-<id>.*`;
`<left>` is its share of `epic.max_usd`, above, only when `epic.max_usd` is
set; its re-reviews post only HIGH and MEDIUM Code findings, so it runs no
round for a finding the epic would merge with), where `<M>` is every round in
the change's `.findings-state.json`, finished or not, plus one, so the fixer
runs one round (finishing first a round an earlier fixer left open) and
stops. When it ends, the coordinator reads the PR again and
reuses the fixer's last review (no new review launch): blockers on it get
the same decision, above. A head with a fix
after its review is not fixed again: when that fixer exited 0 with
`FINDINGS:DONE` or `FINDINGS:MAX_ROUNDS` and left the head where it was, the
child parks with the blockers, and each later run parks it again until a new
commit is pushed. `FINDINGS:FAILED:<step>:<reason>` (also
when found in the log of a fixer whose end was not recorded), any
other failing exit of the fixer, a failed post, or a review folder with no
`findings.json` parks the child with the reason, and it stays parked. On a
re-run, a head whose post succeeded runs the fixer, not a second post or a
review; a post or fixer an earlier run left running is waited for (never two
at once), then a post runs again, and a fixer runs again with the `--max-rounds` it
was launched with, and `findings` resumes itself; a fixer, post or review killed or
stopped by SIGKILL, SIGTERM or SIGHUP (a negative exit, or 137, 143 or 129,
as `findings` and `uv run` report it) is run again by the next run. Before
each fixer launch, any `findings` on the PR's number is waited for, one
started by hand without `--change` included. The post and the
fixer count against `epic.max_parallel` and `epic.max_usd` like a build, and
Ctrl-C or SIGTERM stops a fixer this run started.

**Merging the children's PRs.** A PR whose review at its current head left
no blockers is waited on as a dependency's PR is. A PR whose current head no
review finished on (its review was stopped, or it got a new head after a
clean review) is waited on only when another child depends on it. The run
names it on stderr (`EPIC: child <id> built: its PR's head <first 12
characters of the sha> has no finished review; the next run reviews it`)
and exits `1`, unless it exits `3` with `EPIC:MERGE_WAITING` or
`EPIC:BUDGET`. Under `epic.merge: manual` (the default)
nothing more happens: the run exits `3` with `EPIC:MERGE_WAITING:<urls>`
naming every such PR still open, and a re-run once a person merged them goes
on. Under `auto`, the default branch is first merged into each such PR, in its
worktree, one merge at a time per repository. The worktree must hold nothing
but buildme's own files and be at the reviewed head (or it plus the merge step's own merges), else the child parks naming what it found; then
`git merge --no-ff --no-commit origin/<default>`. Conflicts only in version
fields, CHANGELOGs and lockfiles go to one agent call (step `merge`,
`agents/<NNN>-merge-<id>.*`, counted against `epic.max_usd`), and so does a
version both sides raised alike, which git merges cleanly at the default
branch's value: the agent writes the next version after the default branch's,
keeps both CHANGELOG entries in full with the child's under that version, and
regenerates a lockfile from the default branch's copy. A conflict in any other
file (a deleted or binary side included), any other file the agent changed,
staged or not, or a change the merge did not make, parks the child; when the
agent changed other files, the hint names the `checkout` or `rm` that undoes
them before the abort. The project's tests then run in the worktree
(`tdd.test_command`, else the command buildme discovers, with
`BUILDME_TRUST_REPO=1`, `agents/<NNN>-merge-tests-<id>.*`; SIGHUP ends them with the run), and a failure
parks. The merge commit is pushed, never forced; a push rejected because origin's branch moved parks naming
where it now is and the `fetch` and `reset --keep` to run; any other failed push, or a failed fetch, leaves the child `unknown` (exit `1`), and the next run tries again. When `epic.max_usd` is reached and the merge would need the agent, nothing is merged. A merge commit holding only git's own merge plus such resolutions (checked against each file's three sides: a version file is git's merge with each version value the side's that changed it or, where both did, the next after the default branch's; a CHANGELOG is the default branch's with every line the child added; a lockfile in a format read as data (TOML, JSON, pnpm's YAML) loads with no key twice, holds only either side's keys and values, the written version or a new hex manifest hash, and pins each package, under its own name, as one side does; another lockfile holds only either side's lines or the written version), as the merge step's own do, is not
reviewed again; any other, such as a person's "Update branch" with other edits, is. Then
buildme waits for the PR's required checks (`gh pr checks --required`, read
every 30 seconds while one is pending; when no check is required, the PR's
own checks gate instead; a failed or cancelled check parks the child, naming
it). It reads the checks only once GitHub shows the commit it checked as
the PR's head: for a moment after a push GitHub still shows the previous
commit and its checks. Another commit still shown after two minutes, such
as one someone else pushed, parks the child. Right after its own push it
treats "no checks reported" as pending for two minutes, since GitHub has
not registered the new commit's checks yet.
Checks still pending after an hour leave the PR open: the run exits 3 with
`EPIC:MERGE_WAITING`, and the next run reads them again. Then it runs
`gh pr merge <n> --merge --match-head-commit <sha>`, never `--admin`, so a
commit pushed to the PR after the step checked it is refused, not merged.
The child counts as merged only when `gh pr view` says `MERGED`: a merge
queue that only queued the PR leaves it open and waited on. A merge
GitHub refuses as `BEHIND` (the default branch moved again) starts the merge
step over, merging the new commit in; any other refusal parks the child with
GitHub's message and `mergeStateStatus`, and a refused merge is never retried
within one run. A merged child's dependants then start. A run stopped during the
merge saves what the worktree held (`merge_stop` in `epic.json`); the next
run aborts the stopped merge and merges again only when nothing else changed
in the worktree since, and otherwise parks with `git -C <worktree> merge
--abort, then re-run`, leaving everything as it is. A merge commit made but
not pushed, holding only such resolutions, is tested again and pushed. A child the merge step parked is
tried again by a later run once its worktree has no merge in progress and no
uncommitted change (after `merge --abort`, as the hint says), and parks again
if the merge fails again; every other parked child stays parked.

### The size gate (after plan review)

After the plan review, the pipeline checks the final `tasks.md` again with the
same split triggers (more than 10 blocks or more than 5 top-level packages by
default; `[buildme.plan_review] PLAN_SPLIT_BLOCK_TRIGGER=` /
`PLAN_SPLIT_PACKAGE_TRIGGER=` in `multiplai.conf` override them). A plan that
should be split — it is over a trigger and its blocks come apart into
independent groups, or it mixes high-risk work with unrelated features — stops
the build: the pipeline writes `split-proposal.md` (the proposed PRs, the cuts,
the reason) and exits `3` with `SPLIT:REQUIRED:<path>`. Re-running stops the
same way until `--accept-size` is passed.

### The documentation phase (always on)

After the build and before the respec proposal, `DOCS_UPDATE` brings the
project's own documentation back in line with the code that was just written —
`README*`, `CHANGELOG*` and `docs/**`, discovered in the project rather than
assumed. It gets its own `docs(<change>): update documentation` commit, so the
documentation lands **in the same PR as the code it describes** instead of being
left for the reviewer to write.

There is no flag and no config toggle: a build that ships code with stale docs
is the thing this phase exists to prevent. A failed writer blocks the build
because it may have left partial edits. The agent never invents features: `DOCS_IMPACT:
none` is a legitimate outcome for a change with no user-visible delta. When the
build changed source, the project keeps a changelog, and nothing was documented,
the pipeline prints `DOCS_WARNING:<reason>`; relay it, it is a warning and never
a failure.

### Step 3b: The review checkpoint (non-`--auto` runs)

When the pipeline pauses for review, it prints paths in a deliberate order.
Work through them **in this order**, and do not skip the first one:

1. **`REVIEW:READ_FIRST:<path to unknowns.md>` — read it, actually read it.**
   `unknowns.md` is the explainer for every dependency, tool, or data source
   that is new to this project: its contract, its edge cases and failure modes,
   and the assumptions the build is about to make. **This is the anti-slop
   mechanism of the whole pipeline** — the edge cases written here are what the
   test-writer turns into tests. Skipping it is the lazy button: the build will
   still run, and it will build on assumptions nobody checked. Surface its
   contents to the user, not just the path.
2. `REVIEW:CODEBASE_ANALYSIS:<path>` — what the design was written against:
   the existing architecture, patterns, interfaces and conventions, as cited
   facts. One explore agent per slice of the project's source files at HEAD,
   plus one for its manifest and config files (at most
   `codebase_analysis_max_units` in all), returns claims with `path:line`
   citations; a claim is kept only when a citation is found at that commit,
   and each one in the file links to its lines. Dependencies holds what the
   manifest and config files declare (dependencies, configuration, external
   services), then the interfaces other code calls. Conventions includes test
   style: each source slice's agent also reads up to two tests that import
   its modules (the first slice reads the first two when none does), for
   that alone; tests are not otherwise analysed. Files left out by the
   limit, and slices whose agent failed, are listed under "Not analysed". The same
   facts are in `facts.json` beside it. Read it before `design.md`, because
   "why does it extend that module rather than add a new one" is answered
   here, not there. Absent for a greenfield project (nothing to analyze) —
   that absence is itself worth noting to the user.
3. `use-cases.md` (pm-spec's) — the personas this change is for and the
   outcomes each one can observe from outside the system. Read it before
   `tasks.md`: the plan review checks that every use case here is delivered by
   some block, and a use case with no block is the cheapest thing to catch at
   this checkpoint.
4. Then `design.md`, `tasks.md`, `rubric.md`. `proposal.md` and
   `requirements/` are pm-spec's: a problem in them goes to
   `/multiplai-pm:pm-spec revise <name>`, never into an edit here (an edit
   makes the next build refuse with `SPEC:NOT_READY`).

A prototype, when the change has a UI or an output format, is pm-spec's step
now, before the specs are marked ready.

### Step 4: Report results

Parse pipeline stdout for progress lines:
- `PHASE:<name>:COMPLETE` — phase transitions (`BOOTSTRAP`, `RESEARCH`,
  `CODEBASE_ANALYSIS`, `SPEC_GENERATION`, `DESIGN_AUDIT`, `PLAN_REVIEW`,
  `REVIEW`, `TDD_BUILD`, `DOCS_UPDATE`, `RESPEC`, `ARCHIVE`, `PUBLISH`)
- `REFERENCES:<doc names>` — the stack/framework reference docs the specs were
  generated against, or `(none)`. Emitted once per run
- `BLOCK:<n>/<total>:<name>:COMPLETE` — block progress
- `BOARD:<change>:<Column>` — kanban column transitions (see
  [`docs/dark-factory-board.md`](docs/dark-factory-board.md))
- `WORKTREE:<path>` / `BRANCH:<name>` / `PUSHED:<branch>` / `PR:<url>` — git lifecycle.
  PUBLISH opens the PR with the pr skill, from the repo's PR template; these
  lines and `PUBLISH_DIAGNOSIS:` are the same as before it did
- `BUDGET:WARN:<percent>:<usd>` — the build has spent 80% of its budget; the
  same warning is in `build-progress.md` as a `BUDGET_WARNING` section. Tell
  the user; the build goes on
- `DOCS_WARNING:<reason>` — source changed, the project keeps a changelog, and
  the documentation phase updated nothing (a warning, never a failure)
- `PUBLISH_DIAGNOSIS:<reason>` — push/PR could not finish; manual commands are in
  `build-progress.md` (the build still succeeded)
- `WALKTHROUGH:STALE:<path>` with one `WALKTHROUGH_FAIL:<path>: <citation>: <reason>`
  per citation the build broke, then `WALKTHROUGHS:<n> checked, <m> stale`
  (and `, <k> already stale at the build's base`) — before publishing, every
  `docs/walkthroughs/*-walkthrough.md` in the project is re-checked at the
  build's HEAD with codebase-walkthrough's model-free `check --at-head`, and
  each stale one again at the commit the build started from; a `STALE` one
  cites lines the build moved. `WALKTHROUGH:ALREADY_STALE:<path>` was stale
  before the build and is never regenerated; `WALKTHROUGH:UNCHECKED:<path> — <why>`
  could not be checked (for example, a repo it reads is not where its header
  says). The same list goes in the PR body as "Stale walkthroughs". With `regenerate_walkthroughs`
  on, the `STALE` line ends with what its re-run did. `WALKTHROUGHS:NONE:` when
  the project has no walkthroughs there. Tell the user which are stale; they
  run only when the build publishes (not with `--no-push` or `--no-worktree`)
- Mutation testing is a sub-step of `TDD_BUILD`, so it has no `PHASE:` line.
  It shows in `build-progress.md` as a `MUTATION` entry (`SKIPPED — …`,
  `PASSED — score …` or `FAILED: …`); a failure ends with `ERROR:` like any
  failed build
- `SPEC_EXTRACT:BASELINE <n>` — the codebase analysis phase ran
  `/multiplai-dev:spec-extract` on a project whose `specs/registry/` was empty
  and committed the `<n>` capability files it applied to the build branch;
  spec generation reads them. `SPEC_EXTRACT:BASELINE <n> uncommitted` — the
  files are in `specs/registry/` but not committed yet (no build branch under
  `--no-worktree`, or the commit failed): after a spec-only build, tell the
  user they are left for them to commit; a build that went on to the TDD build
  committed them with its first block's commit, so tell the user that commit
  holds them. `SPEC_EXTRACT:NOT_RUN exit=<n> <last line>` —
  spec-extract failed and the build went on without a baseline: report the
  line. `SPEC_EXTRACT:RUNNING <message>` — another spec-extract run still holds
  this commit's run folder (often one a killed build left running) and the
  build exited `1` without leaving the phase: re-run the same command once that
  run has ended. An `ERROR:` naming uncommitted changes in `specs/registry/`
  after a failed spec-extract also exits `1`: tell the user to stash or commit
  them, or set `spec_extract: false`, and re-run
- `RESULT:SUCCESS` — build complete
- `SPEC:NOT_READY:<reason>`, then `Run /multiplai-pm:pm-spec [resume ]<name> first`
  — the specs are not marked ready, changed after they were, or (for a build
  that branches a worktree) are not committed. Nothing was written; exit `2`.
  Run the pm-spec command the second line names (`resume` checks specs already
  on disk, from an older buildme build or edited by hand, and marks them ready
  without rewriting them), then re-run the same build command
- `SPEC:ISSUES:<path>`, then `Run /multiplai-pm:pm-spec revise <name> --project-dir <repo>, then run the build again`
  — buildme found a requirements problem and wrote it to `<path>`
  (`spec-issues.md`); exit `4`. Show the user the file, run the revise line
  (only with the user's go-ahead, as for any pm-spec run), then re-run the
  same build command
- `EPIC:APPROVED:<epic.json>` — the epic split is approved; the children build next
- `EPIC:SUMMARY:<path>` — an epic run ended; `<path>` holds each child's status, PR and park reason. Then exit `0`; `1` when a child failed or is `unknown` (`3` while a child waits on a dependency's open PR), and when a child's PR head has no finished review and neither `EPIC:MERGE_WAITING` nor `EPIC:BUDGET` is printed; `3` with `EPIC:MERGE_WAITING` or `EPIC:BUDGET`; `130`/`143` when stopped by a signal
- `SPEND:<usd>:<tokens>` — the last line of every `build` and `findings` run: what that process spent, the pr skill, a walkthrough run and spec-extract included (`:incomplete` follows when one of them printed no spend)
- `ERROR:<message>` — failure

**Exit codes.** `0` done, `1` failure (a failed TDD build prints `ERROR:TDD build failed (exit <n>)`), `2` usage error or a refusal that wrote
nothing (`SPEC:NOT_READY`, above; or another process holds the epic's lock:
the `ERROR:` line names the process; report it, and never change the flags to
get past it), `4` with `SPEC:ISSUES` the requirements go back to pm-spec
(above), and without it an agent timed out (a failure: report it like `1`), `130`, `143` or `129` an epic run stopped by Ctrl-C, SIGTERM or SIGHUP (re-run to continue; `143` and `129` also end any `build` or `findings` stopped by SIGTERM or SIGHUP), and `3` — **waiting on a human or on you**. Each `3` prints one marker. Handle it, then re-run the
**same** `build --change <name>` command (plus the flag named below) to resume:

| Marker | What happened | What you do |
|---|---|---|
| `RESEARCH:NEEDED:<path>` | the research decision asked for research; topics in `<path>` | Run `/deep-research` on them and re-run with `--research-path <output>` — or, without `multiplai-research`, say so and re-run with `--skip-research`. |
| `SPLIT:REQUIRED:<path>` | the size gate wants the plan split; proposal in `<path>` | Show `<path>` to the user, or post it on the change's ticket (`ticket_refs` in its `.change.yaml`, which pm-spec wrote; plane `comment` / `gh issue comment --body-file`), and stop. Re-run with `--accept-size` only when the user chooses to build it as one PR. |
| `BUILD:COMPLETE:<path>` | this change already published; `<path>` is its kept checkpoint, and stderr names its branch and PR. Nothing was written | Ask the user with AskUserQuestion whether to start a new build of it on a new branch. On yes, re-run the same command with `--force`; on no, stop. Never add `--force` without the user's yes. |
| `EPIC:MERGE_WAITING:<urls>` | a child waits for a PR another child opened to merge, or a PR passed review and waits for a person to merge it under `epic.merge: manual`, or, under `epic.merge: auto`, buildme left a PR open itself: its checks were still pending after an hour, or a merge queue only queued it (comma-separated `<urls>`) | Tell the user which PRs wait for a merge, and any child the `EPIC: child` lines on stderr name, and stop. Under `manual`, re-run the same command once they are merged. Under `auto`, do not ask the user to merge by hand. Checks still pending: re-run the same command, which reads them again and merges. A queued PR: re-run once the queue has merged it, since a re-run while it is still queued runs the merge step again, which merges origin's default branch in and pushes when that branch has moved (see [Merging the children's PRs](#epic-mode-one-input-several-changes)). A PR closed without a merge parks the children that wait on it. |
| `EPIC:BUDGET:<spent>` | the epic's spend (USD) reached `epic.max_usd`; nothing more was launched | Tell the user what was spent and which children did not start (the `EPIC:SUMMARY` file), and stop. A re-run stops again until the user raises `epic.max_usd` in `specs/config.yaml`; never raise it yourself. |
| `EPIC:SPLIT:<path>` | the input is several changes; the split is in `<path>` (`split.md`), one plan per child in `plans/` beside it | For a GitHub ticket, post **one** comment whose body is the line `<!-- buildme-epic-split:<sha256 of split.md> -->` (`shasum -a 256 <path>`) followed by the content of `<path>`, and only when no comment on the issue already starts with that line (`gh issue view <n> --json comments --jq '.comments[].body'`): a re-run prints the same line for the same `split.md`, so it never posts twice. For any other input, show `<path>` to the user. Then stop. Re-run with `--approve-split` only when the user approves the split. Never create, close or edit a ticket for a child. |

`INTERVIEW:WAITING`, `SPLIT:REQUIRED` and `EPIC:SPLIT` are the only cases where you post on a ticket. The pipeline itself
never posts and never talks to a ticket system.

Also point the user at the build's companion artifacts inside the change
directory: `unknowns.md`, `implementation-notes.md` (what each
agent found surprising), `mutation.md` (Python builds: the mutation score and
surviving mutants), and `respec.md` (a *proposed* spec delta — it is never
applied automatically).

Report the progress file path for monitoring:
```
Build progress: tail -f {project_dir}/build-progress.md
```

## Context Management

The pipeline runs as a subprocess — context stays light. The interview is
pm-spec's, before any build; research runs from this wrapper between runs.

If context grows large during research, use `/compact` before Step 3.
