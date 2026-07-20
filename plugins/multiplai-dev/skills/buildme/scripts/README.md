# Build Pipeline

Deterministic Python pipeline for the `/buildme` skill. Replaces 4,370 lines of prompt-based orchestration across 4 SKILL.md files with code-driven orchestration + focused LLM calls.

## Architecture

```
SKILL.md (thin wrapper — handles interview, plan review)
  └── python -m build_pipeline build
        ├── orchestrator.py    — mode detect, phase sequence, bootstrap
        ├── spec_generator.py  — proposal → specs → design → tasks → rubric
        ├── tdd_engine.py      — per-block TDD with agent spawning
        ├── apply.py           — manual single-agent implementation
        ├── change_manager.py  — manages specs/ directory
        └── shared: config, state, models, gates, sdk, logging, env, progress
```

**Code drives orchestration. LLM handles creative work.** Gates are code assertions (not suggestions). State persists to disk (crash recovery). Agents are spawned via `claude_code_sdk.query()` with explicit tool allowlists and timeouts.

## Subcommands

```bash
# Full build (the main entry point — invoked by SKILL.md wrapper)
python -m build_pipeline build --mode scratch --change my-feature --project-dir /path

# Spec generation only (dev/debug)
python -m build_pipeline spec-generate --change my-feature --project-dir /path

# TDD engine only (dev/debug)
python -m build_pipeline tdd --change my-feature --project-dir /path

# Manual apply (dev/debug)
python -m build_pipeline apply --change my-feature --project-dir /path
```

## Model-Adaptive Behavior

The pipeline detects the running model tier and adapts:

| Behavior | Advanced (Opus 4.5+) | Standard (Sonnet/Haiku) |
|----------|---------------------|--------------------------|
| Task format | Coarse blocks (1 per spec) | Micro-checkboxes |
| TDD agents | 1 test-writer + 1 implementer per block | 3 agents per task (test + impl + refactor) |
| Refactor | Merged into implementer | Separate phase |

## Dependencies

Declared in `pyproject.toml` (multiplai-core, claude-agent-sdk, pydantic,
pyyaml, python-dotenv) and resolved automatically by `uv run --directory` —
no manual install step. The pipeline is invoked as:

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  python -m build_pipeline --help
```

## Enforcement — what fails a block

The pipeline structurally enforces red-green TDD. Every one of these is a code
gate, not a prompt instruction, and each one can fail a block:

| Gate | Where | Behavior |
|------|-------|----------|
| **RED gate** | after test-writer, before implementer | Runs the suite and requires a non-zero exit failing for the right reason (`FAILED`/`AssertionError`/`NotImplementedError`/missing attribute, plus runner-agnostic signatures: Jest/Vitest `FAIL` markers and non-zero `N failed` summary counts). A passing suite means the tests prove nothing → one test-writer retry (`rewrite_tests`); a collection/syntax error → retry with `fix_tests`. Second failure fails the block. RED and GREEN output are stored as block evidence and fed to the reviewer. |
| **Test quality** | after test-writer | Static weak-test scan (`assert True`, existence-only assertions, mock-assertion-only and mock-setup-dominant tests, fixed sleeps). At a weak ratio ≥ 0.2 the LLM auditor adjudicates; if it confirms, one retry, then the block fails. No advisory-only path. |
| **Agent STATUS** | after test-writer and implementer | Agents close their report with `STATUS:`. `NEEDS_CONTEXT`/`BLOCKED` fails the block with the agent's stated reason logged — the pipeline never proceeds on an admitted non-result. |
| **Integration circuit breaker** | after implementer | Up to 3 fix attempts. Attempts 1–2 use the normal fix prompt; attempt 3 switches to a question-the-architecture prompt and escalates to `config.review_model`. Exhausted → block fails with a diagnosis in `build-progress.md`. |
| **Two-verdict review** | per block | Passing requires BOTH a clean spec-compliance verdict (nothing Missing/Misunderstood) AND the weighted score threshold (≥ 3.5, no dimension at 1). Review exhaustion fails the block — `--lenient-review` restores the old accept-and-continue behavior for unattended overnight runs. |
| **Final review** | end of build | Structured verdict over the full-build diff. A FAILED verdict fails the build, and so does an unverifiable review (an exception yields `passed=False` with the error surfaced) — never a silent pass. Not marked done on failure, so a resume re-runs it. `--lenient-review` continues past both. |

Design docs carry a REQUIRED `## Global Constraints` section and task blocks carry
`Interfaces:` (`Produces:`/`Consumes:` signatures); both are injected verbatim into
every agent and review prompt so implementers use exact signatures rather than
re-deriving them. Generated tasks are scanned deterministically for placeholders
(TBD/TODO/"add appropriate error handling"/"similar to block N").

Gates that execute the repo's own test command require an explicit trust opt-in
(`BUILDME_TRUST_REPO=1` or `--trust-repo`) — the test command is arbitrary argv and
pytest runs any `conftest.py` in the tree at collection time.

## Testing

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT}/skills/buildme/scripts \
  --extra dev python -m pytest tests/ -xvs   # ~341 tests, ~3s
```

## Key Design Decisions

1. **Single entry point** — only `/buildme` is user-facing
2. **specs/ directory** — `change_manager.py` owns the layout (changes/, registry/, archive/)
3. **`llm_steps/`** — focused LLM call functions, not "nodes"
4. **Scored rubric** — per-change evaluation rubric with weighted dimensions (threshold: 3.5)
5. **Mandatory logging** — all components use `setup_logging()` per the logging standard
