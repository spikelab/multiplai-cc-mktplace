---
name: buildme
description: Full bootstrap conductor - from idea to working code. Orchestrates interview, research, spec generation, and autonomous TDD implementation via a deterministic Python pipeline.
when_to_use: 'Triggers: build me, buildme, bootstrap, full build, /buildme'
model: opus
effort: medium
---

# BuildMe

Orchestrate the complete journey from idea to working code via a deterministic
Python pipeline. Interview → Research → Codebase Analysis → Specs →
Design Audit → Prototype → Review → TDD Build → Respec → Publish (branch
pushed + draft PR).

Before any spec is written, the pipeline reads the repo it is about to change
(architecture, patterns, integration points) and resolves the reference docs
for the detected stack and framework, so `design.md` extends the code that
exists and follows the conventions the project already builds to.

Every build runs **inside its own git worktree on its own branch** and ends by
pushing that branch and opening a draft PR — see [Git lifecycle](#git-lifecycle).

## Prerequisites

- **`uv`** (https://docs.astral.sh/uv/) — the pipeline runs via `uv run`.
- **Network + git on first run** — the first invocation fetches the
  `multiplai-core` dependency.
- **Optional: the `multiplai-research` plugin** — the **Interview** and
  **Research** phases invoke `/interviewer` and `/deep-research`, which ship in
  `multiplai-research`. Without it, gather requirements inline (ask the user
  directly) and skip deep research (or run with `--skip-research`).

## Modes

| Mode | Trigger | Flow |
|------|---------|------|
| From Scratch | Bare idea, no docs | Interview → Research → Specs → Build |
| From Brief | File paths or docs provided | Load docs → Interview → Research → Specs → Build |
| Build Only | "build it", specs exist | Verify specs → Research check → Build |

## Flags

| Flag | Effect |
|------|--------|
| `--auto` | Skip review checkpoint (overnight/autonomous runs) |
| `--spec-only` | Stop after spec generation + design audit |
| `--skip-research` | Skip the research phase |
| `--lenient-review` | Accept-and-continue when a block exhausts its review iterations, or when the final review fails or errors, instead of failing the build. Unattended overnight runs only — the default is to fail, so low-scoring work is never silently marked done. |
| `--skip-explainers` | Skip the unknowns/edge-case explainer pass for dependencies new to this project. `unknowns.md` is still written, recording the skip. Default: **on** — the explainers are the anti-slop mechanism, so skipping is a deliberate choice, not a default. |
| `--prototype` | Force the prototype stage on, whatever the change type. |
| `--no-prototype` | Skip the prototype stage (the skip and its reason are logged). |
| `--no-worktree` | Build in the project dir on the current branch instead of creating a worktree + branch. **Required when the calling session is already inside a worktree** — see [Git lifecycle](#git-lifecycle). Also disables push and PR (they only ever act on a branch the pipeline created). |
| `--no-push` | Do not push the build's branch to `origin`. |
| `--no-pr` | Do not open a pull request after pushing. |
| `--pr-ready` | Open the PR ready-for-review instead of the default draft. |

`specs/config.yaml` equivalents (CLI flags win): `explainers: {enabled: true}`,
`prototype: {enabled: auto|true|false}`, `respec: {halt_on_contradiction: false}`,
`git: {worktree: true, push: true, pr: draft|ready|none}`.

`specs/config.yaml` also takes `reference_docs:` — the docs under
`$CLAUDE_CONFIG_DIR/reference/dev/` that the design and task breakdown are
written against. Keys are the detected stack (`pyproject`, `Package`,
`package`, `Cargo`, `go`) or a detected framework (`django`, `react`); each key
given here **replaces** the built-in list for that key alone:

```yaml
reference_docs:
  pyproject: [uv-python-best-practices.md, our-house-style.md]
```

Frameworks are detected from the manifests, not the manifest *name* — `manage.py`
or a `django` dependency adds the django docs on top of the python ones. A name
with no file on disk is skipped; the run prints `REFERENCES:<names>` (or
`REFERENCES:(none)`) so what actually reached the generator is visible.

## Git lifecycle

A build **creates its own git worktree and branch** and never writes to the
calling checkout:

- Branch `buildme/<change-name>` (collisions get `-2`, `-3`, …).
- Worktree at `$WORKSPACE/.worktrees/buildme-<change-name>` (falls back to
  `<repo>/../.worktrees/…` when `WORKSPACE` is unset).
- The worktree is created in BOOTSTRAP, **before** `specs/` exists, so every
  artifact is born on the branch. All later phases operate inside it.
- The build refuses to start (hard failure, no silent fallback) when the repo has
  uncommitted tracked changes, or the target branch already exists with unmerged
  commits. Commit or stash first.
- **The worktree survives the run.** The pipeline never removes a worktree —
  including its own — never merges, and never force-pushes. Deleting it is the
  calling session's decision, made from the workspace root.
- PUBLISH pushes the branch and opens a **draft** PR (`gh pr create`). Push/PR
  failure is non-fatal: the branch and worktree stay intact and the exact manual
  commands land in `build-progress.md`.

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
push/PR commands from `build-progress.md`).

## Tuning model and effort (multiplai.conf)

Model and effort are two axes of one decision; both are set from
`multiplai.conf` without a code edit. Sections:

| Section | Tunes |
|---|---|
| `[buildme]` | `MODEL=` and `EFFORT=` for the whole pipeline |
| `[buildme.spec]` | `EFFORT=` for spec generation, audits, rubric |
| `[buildme.review]` | `EFFORT=` for code review, test-quality audit, final review |
| `[buildme.agent]` | `EFFORT=` for the TDD agents (test writer, implementer, refactorer, fix) |

```ini
[buildme]
MODEL=opus
EFFORT=medium

[buildme.review]
EFFORT=high
```

A step section falls back to `[buildme]`, which falls back to the SDK default
(unset). The `MULTIPLAI_MODEL` / `MULTIPLAI_EFFORT` ceilings still cap the
result, so a budget run forces everything down and a conf override cannot
escape it.

## Scale Assessment (MANDATORY)

After understanding what needs to be built, assess scale before choosing a path:

| Scale | Criteria | Path |
|-------|----------|------|
| **Trivial** | Single file, < ~20 lines, no design decisions | Just do it (no plan needed) |
| **Small** | 2-5 files, clear structure, no novel architecture | **Plan → Build directly** |
| **Medium+** | 6+ files, new architecture, TDD valuable | **Full pipeline** (Interview → Specs → TDD) |

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

### Step 1: Detect mode and gather context

Classify user input into a mode. If unclear, ask:

```
Use AskUserQuestion tool:
Question: "How would you like to proceed?"
Options:
  - "Start fresh — interview me about requirements"
  - "I have docs/research to feed in"
  - "Specs exist — just build it"
```

### Step 2: Run interactive phases (if needed)

**Interview** (From Scratch / From Brief) — requires the `multiplai-research` plugin:
```
Invoke the interviewer skill.
"Interview me about what I want to build."
```
If `multiplai-research` isn't installed, gather requirements inline by asking
the user directly, then summarize.

After interview, summarize the requirements.

**Research** (unless --skip-research) — requires the `multiplai-research` plugin:
```
Invoke /deep-research with topics from the interview.
Use --auto for autonomous mode, --quick for lightweight research.
Example: /deep-research --auto --preset standard "implementation patterns for [topic]"
```
If `multiplai-research` isn't installed, skip this phase (equivalent to `--skip-research`).

### Step 3: Invoke the pipeline

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  python -m build_pipeline --session-id "{session_id}" build \
  --mode {scratch|brief|only} \
  --change "{change_name}" \
  --project-dir "{project_dir}" \
  --interview-summary "{summary}" \
  --research-path "{research_output_path}" \
  [--auto] [--spec-only] [--skip-research] [--lenient-review] \
  [--skip-explainers] [--prototype|--no-prototype] \
  [--no-worktree] [--no-push] [--no-pr] [--pr-ready]
```

**Important:** Always pass `--session-id` with the current session ID for log correlation.

**Important:** Add `--no-worktree` if this session is already running inside a
git worktree (see [Git lifecycle](#git-lifecycle)).

The pipeline handles: worktree/branch setup, bootstrap, codebase analysis, spec
generation (via change_manager) including the unknowns/explainer pass, the
design audit — which critiques the specs for consistency *and* plan quality
(over-engineering, task granularity, testability, uncovered edge cases) and
folds every critical/major gap back into one regeneration pass of `design.md`
and `tasks.md` before re-auditing once, report-only — the
prototype stage, TDD implementation (test-writer + implementer agents per block),
integration gates, scored quality reviews, entry point verification, the respec
proposal, and publishing (push + draft PR).

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
   the existing architecture, patterns and integration points the three explore
   agents found. Read it before `design.md`, because "why does it extend that
   module rather than add a new one" is answered here, not there. Absent for a
   greenfield project (nothing to analyze) — that absence is itself worth
   noting to the user.
3. `PROTOTYPE:file://<path>` and `PROTOTYPE_NOTES:file://<path>` — open the
   prototype in a browser/editor via the shared mount (the container's
   `localhost` is not the user's, so `file://` is the channel that works) and
   read `NOTES.md`'s `PROVES:` / `DISPROVES:` / `OPEN_QUESTIONS:` slots.
4. Then `design.md`, `tasks.md`, `rubric.md` as usual.

**Prototype-first is the default for anything with a UI or an output format.**
The pipeline auto-detects this (frontend/fullstack change types, or a
user-visible output format named in the proposal/design) and produces the
cheapest artifact that proves the shape — a self-contained HTML mockup, a sample
output file, or a hand-written CLI/API transcript — *before* the expensive TDD
build. Non-empty `DISPROVES:` / `OPEN_QUESTIONS:` triggers one regeneration pass
of `design.md` and `tasks.md`. Use `--no-prototype` only when the change is
genuinely invisible; `--spec-only` runs include the prototype stage.

### Step 4: Report results

Parse pipeline stdout for progress lines:
- `PHASE:<name>:COMPLETE` — phase transitions (now includes
  `CODEBASE_ANALYSIS`, `PROTOTYPE`, `RESPEC`, `PUBLISH`)
- `REFERENCES:<doc names>` — the stack/framework reference docs the specs were
  generated against, or `(none)`. Emitted once per run
- `BLOCK:<n>/<total>:<name>:COMPLETE` — block progress
- `BOARD:<change>:<Column>` — kanban column transitions (see
  [`docs/dark-factory-board.md`](docs/dark-factory-board.md))
- `WORKTREE:<path>` / `BRANCH:<name>` / `PUSHED:<branch>` / `PR:<url>` — git lifecycle
- `PUBLISH_DIAGNOSIS:<reason>` — push/PR could not finish; manual commands are in
  `build-progress.md` (the build still succeeded)
- `RESULT:SUCCESS` — build complete
- `ERROR:<message>` — failure

Also point the user at the build's companion artifacts inside the change
directory: `unknowns.md`, `prototype/`, `implementation-notes.md` (what each
agent found surprising), and `respec.md` (a *proposed* spec delta — it is never
applied automatically).

Report the progress file path for monitoring:
```
Build progress: tail -f {project_dir}/build-progress.md
```

## Context Management

The pipeline runs as a subprocess — context stays light. Interactive phases
(interview, plan review) happen in the SKILL.md wrapper before the pipeline
launches.

If context grows large during interview, use `/compact` before Step 3.
