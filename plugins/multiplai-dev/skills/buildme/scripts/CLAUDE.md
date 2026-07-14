# Build Pipeline — Development Guide

## Module Map

| Module | Purpose | LLM calls? |
|--------|---------|-----------|
| `__main__.py` | CLI entry point with subcommands | No |
| `orchestrator.py` | Phase sequencing state machine | Delegates |
| `spec_generator.py` | Artifact pipeline (proposal → tasks → rubric) | Via llm_steps |
| `tdd_engine.py` | Block-by-block TDD with agent spawning | Via llm_steps |
| `apply.py` | Manual single-agent implementation | Via sdk |
| `change_manager.py` | Manages specs/ directory (DAG, status, templates, archiving) | No |
| `config.py` | BuildConfig, tier detection, test command discovery | No |
| `state.py` | BuildState with checkpoint/resume | No |
| `models.py` | Pydantic models for all structured data | No |
| `gates.py` | Quality gate assertions (pure code) | No |
| `sdk.py` | `llm_call()` + `agent_call()` adapters over `multiplai_core.run_agent()` | Yes (SDK) |
| `rubric.py` | Rubric generation and change type detection | Via sdk |
| _(logging)_ | Uses shared `log_utils.setup_logging()` from hooks/ | No |
| `env.py` | .env loading, multiplai.conf parsing, model resolution | No |
| `progress.py` | Tail-able progress file writer | No |

## LLM Steps (llm_steps/)

| File | Functions | What They Do |
|------|-----------|-------------|
| `spec_steps.py` | `generate_artifact()`, `run_design_audit()`, `run_codebase_analysis()` | Spec generation + adversarial audit (both wired). `run_codebase_analysis()` (3-agent) is **not wired**. |
| `tdd_steps.py` | `run_test_writer()`, `run_implementer()`, `run_refactorer()`, `run_integration_fix()` | TDD agent spawning with tool allowlists |
| `review_steps.py` | `run_code_review()`, `run_security_review()`, `run_review_fix()` | `run_code_review()` is **wired** as the active per-block review — `tdd_engine._run_quality_review` calls it with the block's actual diff, rubric, spec context, and coding standards (honors `config.review_model`). `run_security_review()` / `run_review_fix()` remain **not wired**. |

## Prompt Templates (prompts/)

Templates are Python f-strings with `{placeholders}`. Each template is a constant in its module.

| File | Templates |
|------|-----------|
| `spec_generation.py` | PROPOSAL_PROMPT, SPEC_PROMPT, DESIGN_PROMPT, TASKS_PROMPT |
| `test_writing.py` | TEST_WRITER_PROMPT |
| `implementation.py` | IMPLEMENTER_PROMPT_CLEAN, IMPLEMENTER_PROMPT_MINIMUM, REFACTOR_PROMPT, APPLY_PROMPT |
| `review.py` | CODE_REVIEW_PROMPT, SECURITY_REVIEW_PROMPT |
| `design_audit.py` | DESIGN_AUDIT_PROMPT |
| `rubric_prompts.py` | RUBRIC_PROMPT |

## Testing

```bash
PYTHONPATH=. python -m pytest tests/ -xvs
```

All tests mock LLM calls — no API keys needed. Tests cover:
- Config: tier detection, test command discovery, gate toggles
- State: checkpoint/resume, phase ordering, block tracking
- Models: review scoring, weighted averages, threshold enforcement
- Gates: all gate functions with pass/fail scenarios
- Change Manager: DAG resolution, archiving, delta spec merging
- Spec Generator: dependency ordering, resume, change type detection
- TDD Engine: block parsing, context assembly, weak test patterns, agent selection

## Adding a New Gate

1. Add the gate function to `gates.py` — pure function returning `GateResult`
2. Wire it into the relevant engine (`tdd_engine.py` or `spec_generator.py`)
3. Add tests to `test_gates.py`

## Adding a New LLM Step

1. Create the prompt template in `prompts/`
2. Create the step function in `llm_steps/` — calls `llm_call()` or `agent_call()`
3. Wire it into the relevant engine
4. Add tests that mock `llm_call` / `agent_call`

## Change Manager — specs/ directory

`change_manager.py` manages the `specs/` directory format. Layout:
```
specs/
├── config.yaml                       — project context
├── changes/<name>/                   — active changes
│   ├── .change.yaml                  — metadata
│   ├── proposal.md
│   ├── design.md
│   ├── tasks.md
│   ├── rubric.md
│   └── requirements/<capability>.md  — BDD scenarios (one per capability)
├── registry/<capability>.md          — main spec registry (merged from archives)
└── archive/<date>-<name>/            — archived changes
```

The hardcoded `ARTIFACT_DAG` constant defines the dependency graph. Templates and instructions are Python constants in `change_manager.py`.
