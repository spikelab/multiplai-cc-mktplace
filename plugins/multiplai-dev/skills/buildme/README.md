# BuildMe — From Idea to Working Code

BuildMe is a deterministic Python pipeline that orchestrates the journey from idea to production code. It replaces prompt-based orchestration with code-driven sequencing, keeping LLM calls focused and intentional.

**Entry point:** `/buildme` (Claude Code skill)  
**Pipeline:** `scripts/build_pipeline/` (Python, invoked as subprocess)

## How It Works

BuildMe has two paths depending on task scale:

| Scale | Criteria | Path |
|-------|----------|------|
| **Trivial** | Single file, ~20 lines | Just do it |
| **Small** | 2-10 files, clear structure | Plan → Build directly |
| **Medium+** | 10+ files, new architecture | Full pipeline (below) |

The full pipeline runs as a subprocess to keep the parent context lean:

```
Interview → Research → Spec Generation → Design Audit → Review → TDD Build
```

Each phase checkpoints state to disk. If the build crashes, restarting resumes from the last completed phase.

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
│   └── tasks.md  (requires: requirements + design)
│       └── rubric.md  (requires: tasks)
```

Each artifact is a focused LLM call with the right context. If generation is interrupted, completed artifacts are skipped on resume.

### Phase 4: Design Audit

Three parallel analysis agents check for gaps:
1. **Spec coverage** — missing edge cases
2. **Design consistency** — architectural mismatches
3. **Implementation feasibility** — dependency/tool issues

### Phase 5: Review Checkpoint

Pipeline pauses for human review (unless `--auto`). You can iterate on specs/design before building.

### Phase 6: TDD Build

For each block in `tasks.md`:

1. **Test writer** creates failing tests that define expected behavior
2. **Weak test detection** catches tautologies (`assert True`, empty bodies)
3. **Implementer** writes code to make tests pass
4. **Refactorer** cleans up (standard tier only — advanced tier writes clean code from the start)
5. **Code review gate** scores against rubric (threshold: weighted avg >= 3.5, no dimension at 1)
6. **Integration gate** runs full test suite to verify nothing is broken

If review scores are too low, the implementer retries with feedback (up to 3 iterations). If integration fails, a fix agent repairs the damage (up to 2 attempts).

### Phase 7: Final Review

Full code review across the entire change, plus entry-point verification (can the app actually run?).

### Phase 8: Archive

With `--auto`, the change is archived automatically at the end:
- Delta requirements from `changes/{name}/requirements/` are merged into the main `registry/`
- The change directory is moved to `archive/{YYYY-MM-DD}-{name}/`

Without `--auto`, the change stays in `changes/{name}/` so you can review it first. Archive manually when ready:

```bash
python -m build_pipeline archive --change my-feature --project-dir .
```

Or use `--no-merge` to archive without touching the main registry.

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
│       ├── proposal.md            # Why this change exists
│       ├── design.md              # How to implement (architecture decisions)
│       ├── tasks.md               # Block-by-block work breakdown
│       ├── rubric.md              # Evaluation criteria
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
| `--spec-only` | Stop after spec generation + design audit |
| `--skip-research` | Skip the research phase |
| `--block N` | Resume TDD from specific block (tdd/apply only) |

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
PHASE:BOOTSTRAP:COMPLETE
PHASE:SPEC_GENERATION:COMPLETE
BLOCK:1/2:Reset Request:COMPLETE
BLOCK:2/2:Token Redemption:COMPLETE
RESULT:SUCCESS
```

## Quality Gates

Gates are pure functions (no LLM calls) that return pass/fail decisions:

| Gate | When | Fail Action |
|------|------|-------------|
| Baseline test | Before block 1 | Abort (existing tests broken) |
| Weak test detection | After test writer | Retry with feedback |
| Code review | After implementer | Retry implementation (max 3) |
| Security review | After implementer | Warn + continue |
| Integration | After block done | Integration fix agent (max 2) |
| Entry point | Post-TDD | Warn (manual step needed) |

## State & Recovery

State is checkpointed to `.build-state.json` after every phase transition:

```json
{
  "change_name": "password-reset",
  "mode": "scratch",
  "tier": "advanced",
  "phase": "tdd_build",
  "spec_gen": {
    "completed_artifacts": ["proposal", "specs", "design", "tasks", "rubric"]
  },
  "tdd": {
    "blocks": [
      {"number": 1, "name": "Reset Request", "status": "done"},
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
| `gates.py` | Quality gate assertions (pure code) | No |
| `sdk.py` | `llm_call()` + `agent_call()` wrappers | Yes |
| `rubric.py` | Rubric generation, change type detection | Via sdk |
| `progress.py` | Tail-able progress file writer | No |
| `env.py` | .env loading, model resolution | No |
| `llm_steps/spec_steps.py` | Artifact generation, design audit | Yes |
| `llm_steps/tdd_steps.py` | Test writer, implementer, refactorer | Yes |
| `llm_steps/review_steps.py` | Code review, security review | Yes |
| `prompts/*.py` | Prompt templates with `{placeholders}` | — |

## Testing

```bash
cd skills/buildme/scripts
PYTHONPATH=. python -m pytest tests/ -xvs
```

166 tests covering config, state, models, gates, change manager, spec generator, and TDD engine. All tests mock LLM calls — no API keys needed.
