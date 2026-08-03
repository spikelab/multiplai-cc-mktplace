# BuildMe — From Idea to Working Code

BuildMe is a deterministic Python pipeline that orchestrates the journey from idea to production code. It replaces prompt-based orchestration with code-driven sequencing, keeping LLM calls focused and intentional.

**Entry point:** `/buildme` (Claude Code skill)  
**Pipeline:** `scripts/build_pipeline/` (Python, invoked as subprocess)

## Prerequisites

- **`uv`** (https://docs.astral.sh/uv/) — the pipeline is invoked via `uv run`.
- **Network + git on first run** — the first invocation fetches the
  `multiplai-core` dependency.
- **Optional: the `multiplai-research` plugin** — the Interview and Research
  phases invoke `/interviewer` and `/deep-research` from that plugin. Without it,
  gather requirements inline and skip research (`--skip-research`).

## How It Works

BuildMe has two paths depending on task scale:

| Scale | Criteria | Path |
|-------|----------|------|
| **Trivial** | Single file, ~20 lines | Just do it |
| **Small** | 2-10 files, clear structure | Plan → Build directly |
| **Medium+** | 10+ files, new architecture | Full pipeline (below) |

The full pipeline runs as a subprocess to keep the parent context lean:

```
Interview → Research → Spec Generation (+ unknowns/explainers) → Design Audit
  → Prototype → Review → TDD Build → Respec → Archive → Publish (push + draft PR)
```

Each phase checkpoints state to disk. If the build crashes, restarting resumes from the last completed phase.

The build runs **inside its own git worktree on its own branch** and ends with a
pushed branch and a draft PR — see [Git Lifecycle](#git-lifecycle). Its kanban
column is emitted at every transition — see
[The Board Seam](#the-board-seam) and [`docs/dark-factory-board.md`](docs/dark-factory-board.md).

## The Full Pipeline

### Phase 1: Interview

The SKILL.md wrapper invokes the `/interviewer` skill to surface requirements, constraints, and hidden assumptions. The transcript is summarized and passed to the pipeline.

### Phase 2: Research

Unless `--skip-research`, the wrapper invokes `/deep-research` with topics from the interview. Research output is passed to the pipeline for spec generation.

### Phase 3: Spec Generation

The pipeline generates artifacts in dependency order:

```
proposal.md  (no dependencies)
├── requirements/*.md  (requires: proposal)
├── design.md  (requires: proposal)
│   └── unknowns.md  (requires: design)
│       └── tasks.md  (requires: requirements + design + unknowns)
│           └── rubric.md  (requires: tasks)
```

Each artifact is a focused LLM call with the right context. If generation is interrupted, completed artifacts are skipped on resume.

#### The unknowns / explainer gate

Before the build depends on anything **new to this project**,
`dependencies.detect_new_dependencies()` (a pure function — no LLM) parses the
proposal's `## Impact` and the design's `## Decisions` for named
tools/libraries/services/data sources, then subtracts everything already declared
in the project's manifests (`pyproject.toml`, `package.json`, `Package.swift`,
`Cargo.toml`, `go.mod`, `requirements.txt`) and already imported in its source.

One explainer call per remaining dependency (with `WebSearch`/`WebFetch`) writes
its section of `unknowns.md`: **What it is**, **The contract we rely on**,
**Edge cases & failure modes**, **Assumptions we are making** (each falsifiable),
**How we would find out cheaply**. When nothing is new, `unknowns.md` records
that explicitly rather than being skipped silently.

`unknowns_gate` then fails any dependency whose section has an empty Edge cases
or Assumptions list, triggering exactly **one** regeneration pass for the missing
sections. Completion is recorded as `spec_gen.explainers_done` so a resume does
not re-run it.

`unknowns.md` is threaded into the `tasks` generation context and into the
**test_writer**'s context (and deliberately not the refactorer's) — the payoff is
that documented edge cases arrive as tests. Controls: `--skip-explainers`,
`explainers: {enabled: true}` in `specs/config.yaml`. Default **on**.

### Phase 4: Design Audit

An adversarial audit call reviews the generated artifacts (proposal, specs,
design, tasks) and returns a list of gaps. It checks two things: internal
consistency (spec↔task alignment, design coherence, placeholders) and **plan
quality** — abstractions no requirement asks for, blocks that are mis-sized for
review, decisions with nothing a test can assert on, and edge cases no scenario
covers.

The gaps are not just logged. Every gap at **critical** or **major** severity is
fed back into **one** regeneration pass of `design.md` and then `tasks.md`, which
is committed as its own `docs(specs): regenerate design.md and tasks.md after
design audit`. The audit then runs once more, **report-only**, so the build log
records whether the critique landed:

```
PHASE: design_audit_feedback_applied — 2 artifact(s)
PHASE: design_audit_recheck — clean
```

There is no loop. Documents that still have gaps after one pass stand as they
are — a stubborn gap costs one extra audit call, not an unbounded number. Minor
gaps are reported and left to the review checkpoint. The pass is recorded in the
checkpoint (`spec_gen.design_audit_regen_done`), so a resumed build audits and
reports but never regenerates a second time.

> Note: a separate multi-agent codebase-analysis step and an implementation
> feasibility gate exist in the code (`run_codebase_analysis`, `feasibility_gate`)
> but are **not currently wired into the pipeline**.

### Phase 5: Prototype

The cheapest artifact that proves the shape, **before** the expensive TDD build:
a single self-contained HTML mockup, a sample output file, or a hand-written
transcript of the intended CLI/API exchange — no framework, no build step, no
repo code. One agent writes it, plus `NOTES.md`, inside
`specs/changes/<name>/prototype/`; the write boundary is enforced in code, not
only in the prompt.

- **When it runs:** `prototype_required()` says yes for `frontend` / `fullstack`
  change types, or when the proposal/design mention a user-visible output format
  (report, export, schema, CLI output, document). Otherwise it is skipped **with
  a logged reason**. Override with `--prototype` / `--no-prototype` or
  `prototype: {enabled: auto|true|false}`. `--spec-only` runs include this stage.
- **NOTES.md REQUIRED slots:** `PROVES:`, `DISPROVES:`, `OPEN_QUESTIONS:`,
  `STATUS:`.
- **`prototype_gate`:** at least one artifact file besides `NOTES.md`, and
  non-empty `PROVES:` and `OPEN_QUESTIONS:`. Failure → one retry, then the
  *phase* fails with a diagnosis in `build-progress.md` — never the build.
- **Feedback into the spec:** non-empty `DISPROVES:` / `OPEN_QUESTIONS:` triggers
  exactly **one** regeneration pass of `design.md` and `tasks.md` with the notes
  injected as audit findings. Recorded as `spec_gen.prototype_done` for resume.

### Phase 6: Review Checkpoint

Pipeline pauses for human review (unless `--auto`). You can iterate on specs/design before building.

Paths are printed in a deliberate order: `REVIEW:READ_FIRST:<unknowns.md>` comes
**first, above everything else** — reading it is the anti-slop step — followed by
`PROTOTYPE:file://…` and `PROTOTYPE_NOTES:file://…` (the container's localhost is
not the user's, so the shared-mount `file://` path is the channel that works).

### Phase 7: TDD Build

For each block in `tasks.md`:

1. **Test writer** creates failing tests that define expected behavior
2. **Weak test detection** catches tautologies (`assert True`, empty bodies)
3. **Implementer** writes code to make tests pass
4. **Refactorer** cleans up (standard tier only — advanced tier writes clean code from the start)
5. **Code review gate** scores against rubric (threshold: weighted avg >= 3.5, no dimension at 1)
6. **Integration gate** runs full test suite to verify nothing is broken

If review scores are too low, the implementer retries with feedback (up to 3 iterations). If integration fails, a fix agent repairs the damage (up to 2 attempts).

Each agent report also carries two REQUIRED slots that close the loop back to the
spec:

```
SURPRISES: <what did not match the spec/design, or "none">
SPEC_IMPACT: <none | clarify | contradicts>
```

`contradicts` means the block could only be built by doing something the
spec/design does not say (or says otherwise). Each parsed note becomes an
`ImplementationNote`, is persisted on the block (so it survives resume), and is
appended to `specs/changes/<name>/implementation-notes.md` **as the build runs** —
a crashed build still leaves the learning on disk. A `contradicts` note is logged
as a warning and written to `build-progress.md`; with
`respec: {halt_on_contradiction: true}` (default **false**) it stops the build
with a diagnosis instead of steering around the contradiction.

### Phase 8: Final Review

Full code review across the entire change, plus entry-point verification (can the app actually run?).

### Phase 9: Respec

Reads `implementation-notes.md` plus the current `requirements/*.md` and
`design.md`, and writes `specs/changes/<name>/respec.md`: a proposed delta in the
same ADDED/MODIFIED/REMOVED Requirements format the archive merge already
applies, each entry carrying the note that motivated it.

**Propose only — it never edits the specs.** Applying a delta stays a deliberate
human (or next-change) decision. Non-fatal: an LLM failure logs a warning and the
build still completes. `respec.md` and `implementation-notes.md` travel with the
change into `archive/<date>-<name>/` but are **not** merged into `registry/` —
only `requirements/*.md` merge.

### Phase 10: Archive

With `--auto`, the change is archived automatically at the end:
- Delta requirements from `changes/{name}/requirements/` are merged into the main `registry/`
- The change directory is moved to `archive/{YYYY-MM-DD}-{name}/`

Without `--auto`, the change stays in `changes/{name}/` so you can review it first. Archive manually when ready:

```bash
python -m build_pipeline archive --change my-feature --project-dir .
```

Or use `--no-merge` to archive without touching the main registry.

### Phase 11: Publish

Pushes the build's branch and opens a **draft** PR. Runs **after** the archive
move, so the pushed branch carries the archived layout and the move itself is a
committed change rather than an uncommitted rename sitting in the worktree. See
[Git Lifecycle](#git-lifecycle).

## Git Lifecycle

Every git and `gh` invocation lives in `git_ops.py`: fixed `argv` lists,
`shell=False` everywhere, change names normalized before they reach a branch or
path. It never merges, rebases, resets, force-pushes, or deletes a branch;
`remove_worktree` exists as a documented helper for a *calling session* and the
pipeline never calls it.

**Worktree + branch (BOOTSTRAP).** Created before `specs/` exists, so the
change's artifacts are born on the branch:

- Branch `buildme/<change-name>`; a collision appends `-2`, `-3`, … rather than
  reusing an existing branch.
- Worktree at `$WORKSPACE/.worktrees/buildme-<change-name>` when `WORKSPACE` is
  set, else `<repo>/../.worktrees/buildme-<change-name>`.
- `config.project_dir` is then re-bound to the worktree and `specs_dir`,
  `state_file_path`, `progress_file_path` re-derived, so every later phase
  operates inside the worktree with no other code change. `worktree_path`,
  `branch`, `source_repo` (and later `pr_url`) are persisted in `BuildState`, so
  a **resume re-binds to the existing worktree and never creates a second one**.
- It refuses to start — hard failure with a diagnosis, never a silent fallback —
  when the repo has uncommitted tracked changes, or the requested branch already
  exists with unmerged commits.
- `--no-worktree` keeps the old behavior, including `git init` for a brand-new
  project directory.

**Commits.** Spec-stage commits (after spec generation, after each regeneration
pass, for the build's companion artifacts, and for the archive move) stage
**explicit paths only**. The per-block TDD commits are the pipeline's one
whole-tree stage — a pathspec-limited `git add -A -- . :(exclude)build-progress.md
:(exclude).build-state.json`, never a bare `git add -A`.

> **Known gap (deliberate):** the original plan called for committing the
> agent-reported `FILES:` list per TDD phase. That is not implemented. `FILES:`
> is agent-self-reported, and dropping a produced file is worse than sweeping one
> in — and since the build runs inside its own worktree, "everything under `.`"
> *is* this build's own work.

**Publish.** `git push -u origin <branch>`, then `gh pr create` with a title from
the change name and a body assembled from `proposal.md`'s Why, the block list,
and links to whichever companion artifacts exist (`unknowns.md`,
`prototype/NOTES.md`, `implementation-notes.md`, `respec.md`). `--draft` by
default so nothing looks merge-ready without a human. `pr_url` is recorded in
`BuildState` and `.board.json`, and `PR:<url>` is printed on stdout.

**Non-fatal by construction.** No `origin`, an unauthenticated `gh`, or a network
failure logs a diagnosis, leaves the branch and worktree intact with the exact
manual commands in `build-progress.md`, and the build still reports success for
the code it produced.

**Never auto-merge, never delete.** Nothing merges to `main` or staging,
force-pushes, or removes a worktree — including its own. The worktree survives
the run and its path is printed (`WORKTREE:<path>`); deleting it is the calling
session's decision, from the workspace root.

**Controls.** `--no-worktree`, `--no-push`, `--no-pr`, `--pr-ready`, and
`git: {worktree: true, push: true, pr: draft|ready|none}` in `specs/config.yaml`
(CLI flags win). Defaults: worktree **on**, push **on**, PR **draft**.
`--no-worktree --no-push --no-pr` reproduces the pre-git-lifecycle behavior
exactly; note that `--no-worktree` alone already disables push and PR, because
those only ever act on a branch the pipeline created.

## The Board Seam

A state seam plus a JSON file — nothing renders a board, schedules cards, or
talks to a board service.

- `models.BoardColumn` — the eleven kanban columns (Backlog … Cancelled).
- `board.column_for(phase, block_status)` — the single pure mapping.
- `specs/changes/<name>/.board.json` — `{card_id, change_name, column,
  owner_agent, entered_at, branch, worktree_path, pr_url, history: [{column, at,
  note}]}`, rewritten on every phase transition and block-status change.
- `BOARD:<change>:<Column>` on stdout, emitted only when the card actually moves.

Driven today: **Shaping → Planning → In Development → In Review**, where In Review
is entered only once PUBLISH has pushed the branch *and* opened the PR. Backlog,
Testing, Ready for Prod, Deploying and Deployed are **never** set;
`BlockStatus` never changes the column. Full accounting, with file:line evidence
and the roadmap for the columns nobody drives, in
[`docs/dark-factory-board.md`](docs/dark-factory-board.md).

> In `--auto` runs the archive move precedes PUBLISH, so the final In Review card
> is written under `specs/archive/<date>-<name>/` and that last write sits
> uncommitted — the PR is already open by then.

## Artifact Format

### Directory Structure

Buildme stores everything under a single `specs/` directory at your project root:

```
specs/
├── config.yaml                    # Project context, gate toggles
├── changes/
│   └── my-feature/                # Active change
│       ├── .change.yaml           # Metadata
│       ├── .build-state.json      # Resumable state checkpoint
│       ├── .board.json            # Kanban card (column, branch, pr_url, history)
│       ├── proposal.md            # Why this change exists
│       ├── design.md              # How to implement (architecture decisions)
│       ├── unknowns.md            # Explainer per dependency new to this project
│       ├── tasks.md               # Block-by-block work breakdown
│       ├── rubric.md              # Evaluation criteria
│       ├── prototype/             # Cheap shape proof, written before the build
│       │   ├── NOTES.md           #   PROVES/DISPROVES/OPEN_QUESTIONS/STATUS
│       │   └── mockup.html        #   (or sample output / CLI transcript)
│       ├── implementation-notes.md # Agent SURPRISES/SPEC_IMPACT, appended live
│       ├── respec.md              # Proposed spec delta (never auto-applied)
│       └── requirements/          # BDD scenarios — one file per capability
│           ├── user-auth.md
│           └── email-verification.md
├── registry/                      # Main spec registry (merged from archives)
│   ├── user-auth.md
│   └── email-verification.md
└── archive/
    └── 2026-04-10-my-feature/     # Archived completed changes
```

**To find your design doc, tasks, or requirements:** look in `specs/changes/<your-change-name>/`.

### proposal.md

Describes why the change exists and what capabilities it introduces.

```markdown
## Why

Users cannot reset their password without contacting support.

## What Changes

Add self-service password reset via email link with time-limited tokens.

## Capabilities

### New Capabilities
- `password-reset`: Email-based password reset with token expiry
- `rate-limiting`: Throttle reset requests per email address

### Modified Capabilities
- `user-auth`: Add password_reset_token field to user model

## Impact

New dependency: email sending service (SES). Database migration for token column.
```

### requirements/password-reset.md

Each capability gets a flat requirements file with testable WHEN/THEN scenarios.

```markdown
## ADDED Requirements

### Requirement: Reset request
The system SHALL send a password reset email when requested.

#### Scenario: Valid email
- **WHEN** a user requests password reset for a registered email
- **THEN** a reset email is sent with a token valid for 30 minutes

#### Scenario: Unknown email
- **WHEN** a user requests reset for an unregistered email
- **THEN** HTTP 200 returned (no information leak), no email sent

### Requirement: Token redemption
The system SHALL allow password change with a valid token.

#### Scenario: Valid token
- **WHEN** a user submits a new password with a valid, unexpired token
- **THEN** password is updated, token is invalidated, confirmation email sent

#### Scenario: Expired token
- **WHEN** a user submits with an expired token (>30 minutes)
- **THEN** HTTP 410 returned with message "Token expired"
```

### design.md

Architecture decisions with rationale and alternatives considered.

```markdown
## Context

App uses Django with PostgreSQL. Email sending not yet implemented.

## Goals / Non-Goals

**Goals:**
- Self-service password reset
- Rate limiting to prevent abuse

**Non-Goals:**
- SMS-based reset (future)
- Admin-initiated password reset

## Decisions

### 1. Token storage: Database column on User model
**Rationale:** Simple, no new infrastructure. Token is hashed (SHA-256) before storage.
**Alternatives:** Redis (adds dependency), signed JWT (no revocation possible)

### 2. Email service: Amazon SES via django-ses
**Rationale:** Already have AWS account, cost-effective at our volume.
**Alternatives:** SendGrid (more features, higher cost), SMTP (unreliable)
```

### tasks.md — Advanced Tier (Opus)

Coarse blocks with natural-language descriptions. One block per spec.

```markdown
## 1. Password Reset Request

Implement the reset request endpoint. Accept email, look up user, generate
hashed token with 30-minute expiry, send email via SES. Return 200 regardless
of whether email exists (prevent enumeration). Include rate limiting (5
requests per email per hour).

Satisfies: password-reset, rate-limiting

## 2. Token Redemption

Implement the token redemption endpoint. Validate token exists, not expired,
not already used. Update password (bcrypt hash), invalidate token, send
confirmation email.

Satisfies: password-reset
```

### tasks.md — Standard Tier (Sonnet/Haiku)

Fine-grained checkboxes under each block.

```markdown
## 1. Password Reset Request

- [ ] 1.1 Add `password_reset_token` and `token_expires_at` to User model
- [ ] 1.2 Create `POST /auth/reset-request` endpoint
- [ ] 1.3 Implement token generation (SHA-256 hash of random bytes)
- [ ] 1.4 Send reset email via SES with token link
- [ ] 1.5 Return 200 for both known and unknown emails
- [ ] 1.6 Add rate limit: 5 requests/email/hour via django-ratelimit

## 2. Token Redemption

- [ ] 2.1 Create `POST /auth/reset-confirm` endpoint
- [ ] 2.2 Validate token exists and not expired (<30 min)
- [ ] 2.3 Hash new password with bcrypt, update user record
- [ ] 2.4 Invalidate used token
- [ ] 2.5 Send confirmation email
```

### rubric.md

Auto-generated evaluation criteria, adapted to the change type (backend, frontend, fullstack, infra).

```markdown
## Code Architecture (weight: 2)
| Score | Criteria |
|-------|----------|
| 5 | Reset logic isolated in service layer, clear separation from views |
| 3 | Mostly clean, some view-level business logic |
| 1 | Token generation, validation, and email mixed in one function |

## Test Quality (weight: 1)
| Score | Criteria |
|-------|----------|
| 5 | All WHEN/THEN scenarios covered, edge cases (expired, used, invalid) |
| 3 | Happy paths + some edge cases |
| 1 | Only happy path, or tests don't assert meaningful behavior |

## Spec Compliance (weight: 3)
| Score | Criteria |
|-------|----------|
| 5 | All spec scenarios passing, no information leaks, rate limiting works |
| 3 | Core scenarios passing, minor gaps |
| 1 | Core scenarios missing or broken |
```

Review passes when: **weighted average >= 3.5** and **no dimension scores 1**.

## Model-Adaptive Behavior

The pipeline detects the Claude model at launch and adapts its behavior:

| Aspect | Advanced (Opus 4.5+) | Standard (Sonnet/Haiku) |
|--------|---------------------|------------------------|
| Task format | Coarse blocks (1 per spec) | Micro-checkboxes per block |
| Agents per block | 2 (test writer + implementer) | 3 (test + implement + refactor) |
| Implementer prompt | "Write production-quality code from the start" | "Write minimum code; a refactorer will clean up" |
| Refactor phase | None (merged into implementer) | Separate agent post-implement |

This tunes agent behavior to model capability rather than using one-size-fits-all prompts.

## CLI Usage

The `/buildme` skill wrapper invokes the pipeline as a subprocess:

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  python -m build_pipeline build \
  --mode scratch \
  --change "password-reset" \
  --project-dir /path/to/project \
  --interview-summary "..." \
  [--auto] [--spec-only] [--skip-research]
```

### Subcommands

| Command | Purpose |
|---------|---------|
| `build` | Full orchestrator (default) |
| `spec-generate` | Artifact generation only |
| `tdd` | TDD engine only (specs must exist) |
| `apply` | Manual single-agent implementation (bypasses TDD) |
| `archive` | Archive a completed change (merge delta specs → main registry) |

### Flags

| Flag | Effect |
|------|--------|
| `--mode scratch` | Start from bare idea (interview first) |
| `--mode brief` | Start from docs/research (load then interview) |
| `--mode only` | Specs exist, just build |
| `--auto` | Skip review checkpoint |
| `--spec-only` | Stop after spec generation + design audit + prototype |
| `--skip-research` | Skip the research phase |
| `--skip-explainers` | Skip the unknowns/explainer pass (`unknowns.md` still records the skip) — `build`, `spec-generate` |
| `--prototype` / `--no-prototype` | Force the prototype stage on / off (mutually exclusive) |
| `--no-worktree` | Build in place on the current branch; implies no push and no PR |
| `--no-push` | Do not push the build's branch |
| `--no-pr` | Do not open a PR after pushing |
| `--pr-ready` | Open the PR ready-for-review instead of draft |
| `--lenient-review` | Accept-and-continue instead of failing on low review scores — `build`, `tdd` |
| `--trust-repo` | Opt-in for auto-approving agents — `build`, `spec-generate`, `tdd`, `apply` |
| `--block N` | Resume TDD from specific block (tdd/apply only) |

### specs/config.yaml toggles

| Key | Default | Effect |
|-----|---------|--------|
| `explainers: {enabled}` | `true` | The unknowns/edge-case explainer pass |
| `prototype: {enabled}` | `auto` | `auto` \| `true` \| `false` — the prototype stage |
| `respec: {halt_on_contradiction}` | `false` | Stop the build on a `SPEC_IMPACT: contradicts` note |
| `git: {worktree, push, pr}` | `true, true, draft` | Git lifecycle (`pr`: `draft` \| `ready` \| `none`) |

CLI flags win over `config.yaml` in every case.

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Build failure |
| 3 | Agent timeout |

### Progress Monitoring

The pipeline writes a tail-able progress file:

```bash
tail -f /path/to/project/build-progress.md
```

Output:
```
# Build Progress: password-reset
Mode: scratch | Tier: advanced | Blocks: 2

## [12:00:00] BOOTSTRAP
Created specs/changes/password-reset/

## [12:10:00] SPEC_GENERATION
Artifacts: proposal ✓ specs ✓ design ✓ tasks ✓ rubric ✓

- [12:20:00] Block 1/2: Reset Request — TESTING
  - test_writer: 14 tests written
  - implementer: tests passing
  - Review iter=1 score=4.1 PASS

- [12:35:00] Block 2/2: Token Redemption — TESTING
  - test_writer: 10 tests written
  - implementer: tests passing
  - Review iter=1 score=3.8 PASS
```

### Stdout Protocol

The pipeline emits structured lines for the SKILL.md wrapper to parse:

```
WORKTREE:/Users/me/knowhere/.worktrees/buildme-password-reset
BRANCH:buildme/password-reset
PHASE:BOOTSTRAP:COMPLETE
BOARD:password-reset:Shaping
PHASE:SPEC_GENERATION:COMPLETE
PHASE:DESIGN_AUDIT:COMPLETE
BOARD:password-reset:Planning
PHASE:PROTOTYPE:COMPLETE
REVIEW:READ_FIRST:/…/specs/changes/password-reset/unknowns.md
PROTOTYPE:file:///…/specs/changes/password-reset/prototype/mockup.html
PROTOTYPE_NOTES:file:///…/specs/changes/password-reset/prototype/NOTES.md
PHASE:REVIEW:COMPLETE
BOARD:password-reset:In Development
BLOCK:1/2:Reset Request:COMPLETE
BLOCK:2/2:Token Redemption:COMPLETE
PHASE:TDD_BUILD:COMPLETE
PHASE:RESPEC:COMPLETE
PHASE:ARCHIVE:COMPLETE
PUSHED:buildme/password-reset
PR:https://github.com/me/project/pull/42
BOARD:password-reset:In Review
PHASE:PUBLISH:COMPLETE
RESULT:SUCCESS
WORKTREE:/Users/me/knowhere/.worktrees/buildme-password-reset
```

Other lines: `PHASE:ARCHIVE:PENDING:<change>` (non-`--auto` runs),
`PHASE:PUBLISH:SKIPPED:<branch>` (`--no-push`),
`PHASE:PUBLISH:FAILED:{no-remote|push|pr}` with `PUBLISH_DIAGNOSIS:<reason>`
(non-fatal — the manual commands land in `build-progress.md`),
`ERROR:<message>`.

## Quality Gates

Gates are pure functions (no LLM calls) that return pass/fail decisions:

| Gate | When | Fail Action |
|------|------|-------------|
| `unknowns_gate` | After `unknowns.md` is written | One regeneration pass for the missing sections only (no loop) |
| `prototype_required` | Before the prototype stage | Not a pass/fail gate — decides whether the stage runs, and logs the reason either way |
| `prototype_gate` | After the prototype agent | One retry, then the *phase* fails with a diagnosis — never the build |
| Baseline test | Before block 1 | Abort (existing tests broken) |
| Weak test detection | After test writer | Retry with feedback |
| Test integrity | After GREEN, and after every review-fix | Fail the block (tests were edited after they gated it) |
| Quality review (graded, panel-merged) | After implementer | Retry implementation (max 3) |
| Integration | After block done | Integration fix agent (max 2) |
| Budget | Each block boundary | Stop with a per-phase spend diagnosis |
| Entry point | Post-TDD | Warn (manual step needed) |

`gates.py` also carries two parsers used by the loop back to the spec:
`parse_agent_status` (`STATUS:`/`TESTS_RUN:`/`GREEN:`/`FILES:`) and
`parse_implementation_note` (`SURPRISES:`/`SPEC_IMPACT:` →
`models.ImplementationNote`).

The per-block review is a two-verdict review (spec compliance + rubric
scores), optionally run as a panel of reviewers in fresh contexts and merged.
A separate security-review step exists in the code (`run_security_review`) but
is **not currently wired** — there is no distinct security gate.

### Test integrity

The implementer's tools are per-tool, not per-path, so test files stay
writable for the whole implement phase — and the review-fix agent is the same
implementer, so every fix iteration re-opens that window. An agent measured by
"the tests pass" can make them pass by editing them.

Buildme sha256-hashes the block's test files the moment the RED gate passes
and re-checks them at both windows. A silent change fails the block. An
implementer that genuinely needs a test to change declares it —
`TEST CHANGE REQUIRED: <reason>` in its report — which downgrades the gate to a
flag, re-baselines the hashes, and hands the reason to the reviewer as an
unverified claim to check against the diff.

### Reviewer panel and finding adjudication

Reviewers see the diff and the spec in a fresh context, which is exactly why
they catch what the implementer missed *and* why roughly a quarter of what
they raise is wrong — they cannot see the decisions the build already made.

So a review emits discrete **findings**, and the orchestrator (which does have
the build's context) adjudicates each one before anything acts on it. Rejected
findings are recorded on the block and never reach a fix agent. Turning
adjudication off **drops** findings rather than applying them blind.

With a panel configured, each member reviews independently and the results
merge: dimension scores average, with confidence scaled down by how much the
panel disagreed; identical findings combine confidence (noisy-or) and keep the
harshest severity anyone assigned. Spec verdicts are unioned, not intersected
— the reason to run a panel is that reviewers find disjoint sets.

Two consequences worth knowing before you configure one:

- **A panel can pass a block one harsh reviewer would have failed.** Scores
  disagreeing 5-vs-1 collapse that dimension's confidence to zero, which the
  graded gate reads as "no information" (neutral), not as a verdict — so one
  member's "critical" is fully neutralized by another's "fine". Severity still
  survives on the *findings* path (noisy-or keeps the harshest), which is what
  reaches a fix agent. If you want one dissenter to be able to sink a block,
  use a single reviewer.
- **A member that fails is dropped, not fatal.** The review proceeds on the
  survivors with a warning, and only an all-members-failed panel fails the
  block — otherwise adding members would make the pipeline *less* reliable.
  A dimension only one member scored is discounted for lack of corroboration.

### Budget

Every other loop bound in the pipeline is an iteration count, which does not
bound spend: three review iterations over a huge diff with a three-member
panel costs an order of magnitude more than three over a small one. The
`budget:` ceilings add the missing axis. Spend is checked at block boundaries
(nothing is half-done there), warns once at 80%, and stops with a
per-phase breakdown of where the tokens went. The spend is checkpointed into
`.build-state.json`, so a resumed build does not get a fresh budget.

### Configuring reviews and budget

All optional — the defaults reproduce the pre-existing behavior exactly.

```yaml
# specs/config.yaml
code_review:
  model: opus              # single stronger reviewer (existing)
  panel:                   # OR a panel — one full-diff call per member
    - model: opus
    - model: sonnet
  adjudicate: true         # default; false DROPS findings, never auto-applies
  gate:
    min_weighted_average: 3.5
    critical_score: 1.0

budget:
  max_tokens: 5000000      # omit for unlimited (the previous behavior)
  max_usd: 25
```

## State & Recovery

State is checkpointed to `.build-state.json` after every phase transition:

```json
{
  "change_name": "password-reset",
  "mode": "scratch",
  "tier": "advanced",
  "phase": "tdd_build",
  "worktree_path": "/Users/me/knowhere/.worktrees/buildme-password-reset",
  "branch": "buildme/password-reset",
  "source_repo": "/Users/me/knowhere/PROJECTS/project",
  "pr_url": null,
  "spec_gen": {
    "completed_artifacts": ["proposal", "requirements", "design", "unknowns", "tasks", "rubric"],
    "explainers_done": true,
    "prototype_done": true
  },
  "tdd": {
    "blocks": [
      {"number": 1, "name": "Reset Request", "status": "done",
       "notes": [{"block_number": 1, "role": "implementer",
                  "surprises": "…", "spec_impact": "clarify"}]},
      {"number": 2, "name": "Token Redemption", "status": "testing"}
    ],
    "current_block": 1
  }
}
```

If the build crashes, restarting with the same `--change` name loads state and skips completed phases. Completed blocks are not re-run.

## Module Map

| Module | Purpose | LLM calls? |
|--------|---------|-----------|
| `__main__.py` | CLI entry point | No |
| `orchestrator.py` | Phase sequencing state machine | Delegates |
| `spec_generator.py` | Artifact pipeline (proposal → rubric) | Via llm_steps |
| `tdd_engine.py` | Block-by-block TDD with agent spawning | Via llm_steps |
| `apply.py` | Manual single-agent implementation | Via sdk |
| `change_manager.py` | Directory ops, artifact DAG, archiving | No |
| `config.py` | BuildConfig, tier detection, test discovery | No |
| `state.py` | BuildState with checkpoint/resume | No |
| `models.py` | Pydantic models for structured data | No |
| `gates.py` | Quality gate assertions + agent-report parsers (pure code) | No |
| `dependencies.py` | Detects dependencies new to *this* project (manifests + imports) | No |
| `git_ops.py` | Every `git`/`gh` call: worktree, branch, commits, push, PR | No |
| `board.py` | Board seam: `column_for`, `.board.json`, `BOARD:` line | No |
| `budget.py` | Per-build token/cost accounting + circuit-breaker | No |
| `sdk.py` | `llm_call()` + `agent_call()` wrappers | Yes |
| `rubric.py` | Rubric generation, change type detection | Via sdk |
| `progress.py` | Tail-able progress file writer | No |
| `env.py` | .env loading, model resolution | No |
| `llm_steps/spec_steps.py` | Artifact generation, design audit, per-dependency explainer | Yes |
| `llm_steps/prototype_steps.py` | Prototype agent + folding its notes back into design/tasks | Yes |
| `llm_steps/tdd_steps.py` | Test writer, implementer, refactorer | Yes |
| `llm_steps/respec_steps.py` | Implementation-notes file + the `respec.md` proposal | Yes |
| `llm_steps/review_steps.py` | Per-block code review + panel merge (wired); security review (not wired) | Yes |
| `prompts/*.py` | Prompt templates with `{placeholders}` | — |

## Testing

```bash
cd skills/buildme/scripts
PYTHONPATH=. python -m pytest tests/ -xvs
```

688 tests covering config, state, models, gates, change manager, dependency
detection, spec generator, prototype and respec steps, git lifecycle, board seam,
and the TDD engine. All tests mock LLM calls (and `gh`) — no API keys needed.
