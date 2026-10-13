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
        ├── epic/              — EPIC_SPLIT: one change or several child plans, the approval, running and reviewing the children
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

# Archive a completed change (merge delta specs → main registry)
python -m build_pipeline archive --change my-feature --project-dir /path

# Fix a PR's review comments in rounds (the /multiplai-dev:buildme-findings skill)
python -m build_pipeline findings --pr 42 --project-dir /path [--auto] [--max-rounds 2]
```

`findings` fixes each code defect through the TDD engine, asking the fixer to
look for the same mistake elsewhere (`SEARCHED:`), then, when the engine
finishes with nothing stopped after the blocks, checks the round's own fix
commits once with a read-only agent and fixes what it lists as one more
block (which skips the whole-change refactor and mutation testing). Under
`--auto` it posts the re-review's listed Code findings, and Tests findings
on source files, through `buildme-decisions.json` in the review's
folder (`build_pipeline/findings/README.md`).

### Epic mode

After the codebase analysis, `build` decides whether the input is one change or
several (`BuildPhase.EPIC_SPLIT`, one structured call). Several writes
`<data_dir>/buildme/epics/<owner>-<repo>--<change>/` — `epic.json`, `split.md`,
`plans/<id>.md`, `.lock` — prints `EPIC:SPLIT:<split.md>` and exits 3.
`build ... --approve-split` re-checks `split.md`'s table and the plans as on disk,
saves `state: approved` with each file's sha256, and prints
`EPIC:APPROVED:<epic.json>`, then runs the children (`epic/coordinator.py`):
each is its own `python -m build_pipeline build` subprocess in its own process
group, launched once its dependencies merged, at most `epic.max_parallel` at
once, from origin's default branch (`--base-ref`). Records go to
`agents/<NNN>-build-<id>.{started.json,result.json,log}` (`epic/records.py`)
and each run ends with `runs/<NN>/summary.json` (`EPIC:SUMMARY:<path>`). A
child waiting on an open PR exits 3 with `EPIC:MERGE_WAITING:<urls>`. `build`
and `findings` print `SPEND:<usd>:<tokens>` last (SIGTERM and SIGHUP raise
`SystemExit` for the whole run, so those stops print it too), from a process
total that `budget.reset()` and `load_state` leave alone and that
`git_ops._run_skill` adds the pr skill's and a walkthrough run's own `SPEND:`
line to (`:incomplete` when one printed none); each result record keeps it as
`spend`, and `epic/spend.py` adds those to the review ledger's spend for the
children's PRs. Once that reaches `epic.max_usd`, nothing more launches and
the run exits 3 with `EPIC:BUDGET:<spent>`. Each child's open PR is reviewed
by the review skill as a subprocess (`uv run --directory <review scripts>
python -m review_pipeline review --repo <worktree> --pr <n> --trust-repo --fetch`;
`epic/review.py`, `Coordinator._review`), at most once per head: a head the
review ledger already holds a complete review of is reused, a review of the
head still running (its `started` line and its run lock held) is waited for
without a launch, one the ledger does not show yet (exit 2 `is running now`)
is waited for by asking again, and an interrupted one
(exit 4 with the hint) is continued with `resume`. A review that exits 2
because its `git fetch` or `gh pr view` failed, or a PR commit is not in the
clone after its fetch, leaves the child `unknown`, and the next run asks
again. Its `review` record holds
the head, the base, the review directory, the cost, `pr_round`, `reused`,
`conflicts` (never blocks) and `blocking` (open Code HIGH/MEDIUM findings); at
`epic.max_review_rounds` with blockers, or on any other failure, the child
parks. A review counts against `epic.max_parallel` and `epic.max_usd` like a
build. A review with blockers below the cap runs the fix loop (`epic/fix.py`,
`Coordinator._fix`): `findings.decisions.write_decisions(<review dir>)`, then
`review_pipeline post <review dir> --decisions <review dir>/buildme-decisions.json`
(record kind `post`), then `python -m build_pipeline findings --pr <n> --change
<epic>-<id> --project-dir <worktree + project offset> --auto --trust-repo
--max-rounds <rounds in .findings-state.json + rounds left>` (kind `fix`), both
with cwd the child's project directory; then the PR is read again and the
fixer's last review reused. A head with a `fix` record after its review is
not fixed again; one with a `post` that exited 0 and no `fix` runs the fixer
only. A `fix` with no result is waited for while `ps` shows it (`fix_pid`),
then gets an `interrupted` result and runs again (`_fix_pending`), as does one
that died of a signal. `FINDINGS:FAILED`, another failing exit, or a failed
post parks the child. A file
changed after approval, another process holding the epic's lock, or a
`specs/config.yaml` turning off `git.worktree` or `git.push`, exits 2.
`--no-epic-split` skips the decision.

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
