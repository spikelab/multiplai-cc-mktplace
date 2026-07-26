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
| `gates.py` | Quality gate assertions (pure code) + agent-report parsers (`parse_agent_status`, `parse_implementation_note`). New gates: `unknowns_gate`, `prototype_required`, `prototype_gate`. | No |
| `dependencies.py` | Pure detection of dependencies **new to this project**: parses the proposal's `## Impact` and design's `## Decisions`, subtracts every manifest (`pyproject.toml`, `package.json`, `Package.swift`, `Cargo.toml`, `go.mod`, `requirements.txt`) and every existing import. Feeds the B1 explainer gate. No LLM, no network. | No |
| `git_ops.py` | Every `git`/`gh` invocation: worktree+branch setup, explicit-path commits, push, `gh pr create`. `shell=False`, fixed argv, never merges/force-pushes/deletes. | No |
| `board.py` | Board seam: pure `column_for(phase, block_status)` → `BoardColumn`, `.board.json` card writer, `BOARD:<slug>:<Column>` stdout line. Drives Shaping → Planning → In Development → In Review only; its docstring names the columns it never sets. The `BoardCard`/`BoardEvent` pydantic models live here deliberately (they are the card file's private schema, not shared pipeline data). | No |
| `sdk.py` | `llm_call()` + `agent_call()` adapters over `multiplai_core.run_agent()` | Yes (SDK) |
| `rubric.py` | Rubric generation and change type detection | Via sdk |
| _(logging)_ | Uses shared `log_utils.setup_logging()` from hooks/ | No |
| `env.py` | .env loading, multiplai.conf parsing, model resolution | No |
| `progress.py` | Tail-able progress file writer | No |

## LLM Steps (llm_steps/)

| File | Functions | What They Do |
|------|-----------|-------------|
| `spec_steps.py` | `generate_artifact()`, `run_explainer()`, `run_design_audit()`, `run_tasks_audit()`, `run_codebase_analysis()` | `run_explainer()` is the B1 explainer gate — one call per `dependencies.detect_new_dependencies()` hit, run concurrently with `WebSearch`/`WebFetch`/`Read`/`Glob`/`Grep`, concatenated into `unknowns.md`; `unknowns_gate` then forces at most one regeneration pass (`spec_generator._audit_unknowns`, recorded as `SpecGenState.explainers_done`). Spec generation + adversarial audits (design audit and tasks-shape audit, both wired — the tasks audit forces one regeneration pass on horizontal-decomposition findings). `run_codebase_analysis()` (3-agent) is **not wired**. |
| `prototype_steps.py` | `run_prototype()`, `apply_prototype_findings()`, `primary_prototype_artifact()` | Prototype-first stage (BuildPhase.PROTOTYPE, between DESIGN_AUDIT and REVIEW). One agent writes a mockup / sample output / CLI transcript + `NOTES.md` inside `specs/changes/<name>/prototype/` — the write boundary is enforced in code (`_files_outside`), not only in the prompt. `apply_prototype_findings()` folds the notes' DISPROVES/OPEN_QUESTIONS back into design.md and tasks.md with **one** regeneration pass each. |
| `tdd_steps.py` | `run_test_writer()`, `run_implementer()`, `run_refactorer()`, `run_integration_fix()` | TDD agent spawning with tool allowlists. Reports carry the `SURPRISES:` / `SPEC_IMPACT:` REQUIRED slots parsed by `gates.parse_implementation_note`. |
| `respec_steps.py` | `append_implementation_note()`, `format_implementation_note()`, `notes_path()`, `ensure_delta_sections()`, `run_respec_audit()` | B3 loop back to the spec. Each parsed `ImplementationNote` is appended to `implementation-notes.md` **as the build runs** (a crashed build still leaves the learning on disk). `run_respec_audit()` (BuildPhase.RESPEC, after TDD_BUILD) reads those notes + current requirements/design and writes `respec.md` in ADDED/MODIFIED/REMOVED form — **propose only, never edits the specs**, and non-fatal on LLM failure. |
| `review_steps.py` | `run_code_review()`, `run_security_review()`, `run_review_fix()` | `run_code_review()` is **wired** as the active per-block review — `tdd_engine._run_quality_review` calls it with the block's actual diff, rubric, spec context, and coding standards (honors `config.review_model`). `run_security_review()` / `run_review_fix()` remain **not wired**. |

## Prompt Templates (prompts/)

Templates are Python f-strings with `{placeholders}`. Each template is a constant in its module.

| File | Templates |
|------|-----------|
| `spec_generation.py` | PROPOSAL_PROMPT, SPEC_PROMPT, DESIGN_PROMPT, TASKS_PROMPT |
| `test_writing.py` | TEST_WRITER_PROMPT |
| `implementation.py` | IMPLEMENTER_PROMPT_CLEAN, IMPLEMENTER_PROMPT_MINIMUM, REFACTOR_PROMPT, APPLY_PROMPT |
| `review.py` | CODE_REVIEW_PROMPT, SECURITY_REVIEW_PROMPT |
| `design_audit.py` | DESIGN_AUDIT_PROMPT, TASKS_AUDIT_PROMPT |
| `prototype.py` | PROTOTYPE_PROMPT |
| `explainer.py` | EXPLAINER_PROMPT, UNKNOWNS_REGEN_PROMPT |
| `respec.py` | RESPEC_PROMPT |
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
- Dependencies: manifest/import subtraction, false-positive suppression
- Prototype / Respec steps: write boundary, gate retry, one-pass regeneration, propose-only respec
- Git ops: real temp git repos with `gh` mocked — worktree/branch setup, resume re-binding, explicit-path commits, non-fatal push/PR failure
- Board: every `(phase, block_status)` → column case, including the columns the pipeline never drives

## Prompt Authoring Rules

- **Review prompts stay unbiased.** Never inject "do not flag X", pre-judged
  severities, or any instruction that suppresses findings into a review
  prompt. The reviewer decides severity from the calibration scale; the diff
  and specs are its only ground truth. If a known non-issue keeps getting
  flagged, fix the spec/rubric wording — never blindfold the reviewer.
- **Positive recipes over prohibition lists.** Prompts state what to do and
  the observable condition for doing it (structural REQUIRED slots,
  gate-function checks). Enforcement lives in code gates, not in aggressive
  language.

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
│   ├── .board.json                   — kanban card (board.py)
│   ├── proposal.md
│   ├── design.md
│   ├── unknowns.md                   — explainer per new-to-this-project dependency
│   ├── tasks.md
│   ├── rubric.md
│   ├── prototype/                    — NOTES.md + the cheap shape proof
│   ├── implementation-notes.md       — agent SURPRISES/SPEC_IMPACT, appended live
│   ├── respec.md                     — proposed spec delta (never auto-applied)
│   └── requirements/<capability>.md  — BDD scenarios (one per capability)
├── registry/<capability>.md          — main spec registry (merged from archives)
└── archive/<date>-<name>/            — archived changes
```

The hardcoded `ARTIFACT_DAG` constant defines the dependency graph. Templates and instructions are Python constants in `change_manager.py`.

`unknowns` sits between `design` and `tasks` (`tasks` requires
`["requirements", "design", "unknowns"]`), so the edge cases are on disk while
the task breakdown is written. `implementation-notes.md`, `respec.md`,
`prototype/` and `.board.json` are **not** DAG artifacts — they are written by
their phases. All of them travel into `archive/<date>-<name>/`; only
`requirements/*.md` ever merge into `registry/`.

## Phases

`BuildPhase` (models.py) is a linear enum and `state.is_phase_complete` compares
**positions in it**, so ordinal position is load-bearing and any insertion must
keep old checkpoints loadable (fixtures under `tests/fixtures/` cover this):

```
INIT → BOOTSTRAP → INTERVIEW_DONE → RESEARCH → SPEC_GENERATION → DESIGN_AUDIT
  → PROTOTYPE → REVIEW → TDD_BUILD → RESPEC → PUBLISH → COMPLETE  (or FAILED)
```

`PROTOTYPE`, `RESPEC` and `PUBLISH` are the additions from the dark-factory
lifecycle plan. `PUBLISH` runs **after** the `--auto` archive move, so the pushed
branch carries the archived layout.
