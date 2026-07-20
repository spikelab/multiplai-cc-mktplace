---
name: buildme
description: Full bootstrap conductor - from idea to working code. Orchestrates interview, research, spec generation, and autonomous TDD implementation via a deterministic Python pipeline.
when_to_use: 'Triggers: build me, buildme, bootstrap, full build, /buildme'
model: claude-opus-4-7
effort: xhigh
---

# BuildMe

Orchestrate the complete journey from idea to working code via a deterministic
Python pipeline. Interview → Research → Specs → Design Audit → TDD Build.

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
  [--auto] [--spec-only] [--skip-research] [--lenient-review]
```

**Important:** Always pass `--session-id` with the current session ID for log correlation.

The pipeline handles: bootstrap, spec generation (via change_manager), design audit,
TDD implementation (test-writer + implementer agents per block), integration gates,
scored quality reviews, entry point verification, and E2E testing.

### Step 4: Report results

Parse pipeline stdout for progress lines:
- `PHASE:<name>:COMPLETE` — phase transitions
- `BLOCK:<n>/<total>:<name>:COMPLETE` — block progress
- `RESULT:SUCCESS` — build complete
- `ERROR:<message>` — failure

Report the progress file path for monitoring:
```
Build progress: tail -f {project_dir}/build-progress.md
```

## Context Management

The pipeline runs as a subprocess — context stays light. Interactive phases
(interview, plan review) happen in the SKILL.md wrapper before the pipeline
launches.

If context grows large during interview, use `/compact` before Step 3.
