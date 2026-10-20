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
        ├── epic/              — EPIC_SPLIT: one change or several child plans, the approval, running, reviewing and merging the children
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
once, from origin's default branch (`--base-ref`). A child whose `split.md`
row names a `repo` (a local checkout, absolute or relative to the epic's
repository root; approval refuses one that is not the top of a checkout with
an `origin` remote, `store.repo_errors`) builds there instead: its
`--project-dir`, base fetch, PR lookup, review slug, fixer and merge lock are
that repository's (`Coordinator._place`), its `specs/config.yaml` gets the same
`git.worktree`/`git.push` check as the epic's, and a checkout gone later parks
it for good (`repo_park` in `epic.json`) while its saved review slug keeps its
reviews in the epic's spend (with none saved, the child predates PR F and
was built in the epic's repository, so the slug is the epic's). Records go to
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
python -m review_pipeline review --repo <worktree> --pr <n> --trust-repo --fetch
--extra-finders`; `epic/review.py`, `Coordinator._review`; `--extra-finders`
adds one finder per ~600 changed lines on the PR's first round only), at most
once per head: a head the
review ledger already holds a complete review of is reused, a review of the
head still running (its `started` line and its run lock held) is waited for
without a launch, one the ledger does not show yet (exit 2 `is running now`)
is waited for by asking again, and an interrupted one
(exit 4 with the hint) is continued with `resume`. A review that exits 2
because its `git fetch` or `gh pr view` failed, or a PR commit is not in the
clone after its fetch, leaves the child `unknown`, and the next run asks
again. Its `review` record holds
the head, the base, the review directory, the cost, `pr_round`, `reused`,
`conflicts` (never blocks) and `blocking` (open Code HIGH/MEDIUM findings),
and with blockers `next` and `next_reason` (`review.decide`, no model call):
`round` when a Code HIGH is open or, below `epic.max_review_rounds`, a Code
MEDIUM earns one (`epic.round_worth: breaks`: the verifier rated it
`breaks-users`/`breaks-business` or did not rate it; `any`: every one);
`final_fix` when only Code MEDIUMs are left and none earns a round, or at
the cap; `park` for an open HIGH at the cap. A park, or any other failure,
parks the child, except a failed read (`READ_FAILED`) and a review, resume or
post that cannot start, which leave it `unknown`. A review counts against `epic.max_parallel` and
`epic.max_usd` like a build. `final_fix` runs one fixer round with
`--no-review` and `--max-rounds <rounds in its findings state, finished or
not, + 1>` (kept on a relaunch);
its result keeps `final_head`, which goes to the merge step with no review,
and after the merge (or while it waits under `manual`) one issue lists what
it left, unless an issue with its title exists. A final fix whose fetch found
no new comment (the blockers were already on the PR) ends `FINDINGS:DONE`
with no round; its PR goes to the merge step all the same, and the issue says
no fix ran on what it lists. A review whose `next` is
`round` runs the fix loop (`epic/fix.py`,
`Coordinator._fix`): `findings.decisions.write_decisions(<review dir>)`, then
`review_pipeline post <review dir> --decisions <review dir>/buildme-decisions.json`
(record kind `post`), then `python -m build_pipeline findings --pr <n> --change
<epic>-<id> --project-dir <worktree + project offset> --auto --trust-repo
--max-rounds <rounds in .findings-state.json, finished or not, + 1>` (kind `fix`:
one round per launch, so a decision follows every review), both
with cwd the child's project directory; then the PR is read again and the
fixer's last review reused. A head with a `fix` record after its review is
not fixed again; when that fix exited 0 with `FINDINGS:DONE` or
`FINDINGS:MAX_ROUNDS` (`fix.ended`), its head still has blockers and the
child parks, again on each later run until a new commit. One with a `post`
that exited 0 and no `fix` runs the fixer only. A `post` with no
result is waited for while `ps` shows it (`post_pid`), then gets an
`interrupted` result and posts again. A `fix` with no result is waited for
while `ps` shows it (`fix_pid`: any `findings` on the PR's number whose
`--project-dir`, if absolute, is in the same repository, so one started by
hand counts too; it is checked before every fixer launch), then gets an
`interrupted` result and runs again (`_fix_pending`), as does one killed or
stopped by SIGKILL, SIGTERM or SIGHUP (a negative exit, or 137, 143 or 129,
`fix.stopped`); one whose log holds `FINDINGS:FAILED` parks instead. A post
or review stopped the same way (`uv run` reports 128 + signal) gets an
`interrupted` result, and the next run posts or reviews again. `FINDINGS:FAILED`, another failing exit, or a failed post parks the
child. A PR whose review at its current head left no blockers is waited on
as a dependency's is (`Coordinator._waited_on`, `_passed`), so every run with
one open exits 3 with `EPIC:MERGE_WAITING` (1 beside a failed or `unknown`
child when no child waits on a dependency's PR). One whose current head no
review finished on is waited on only as a dependency's, is named on stderr,
and makes the run exit 1 when it prints neither `EPIC:MERGE_WAITING` nor
`EPIC:BUDGET` (`_unreviewed`). Under `epic.merge: auto` the merge step runs
first (PRs E1 and E2, `epic/merge.py`, `Coordinator._merge`), under one
`asyncio.Lock` per repository and on the worktree root: a merge commit made
and not pushed, holding only what `merge.merges_onto` accepts, is checked (`stop_record.others_work`), tested and pushed; a
merge left in progress is aborted only when `others_work` passes against
`merge_stop`, else it parks with the hint `git -C <W> merge --abort, then
re-run`, and after the abort restarts at the fetch (a change the abort keeps outside the merge parks before the tests: `merge.outside_merge`); else `merge.ready` (the same check, no record, and HEAD must be the reviewed head or it plus merges `merge.merges_onto` accepts), `git fetch`, `git
merge --no-ff --no-commit origin/<default>`, one `merge` agent for version,
CHANGELOG and lockfile conflicts and for a version both sides raised alike (`merge.plan`; record kind `merge`; any other conflicted file, a deleted or binary side included, or other file it changed, staged or not, parks, the hint naming the `checkout`/`rm` that undoes those edits: `merge.undo_hint`),
the tests with `BUILDME_TRUST_REPO=1` (kind `merge-tests`, killed when the step stops), `git commit` and
`git push` (rejected: parks; other failures, and a failed fetch: `unknown` for the rest of the run, `MERGE_UNKNOWN`). An exception or signal saves `merge_stop`; any park saves
`merge_park` in `epic.json`, which `_recover` keeps until `<W>` has no `MERGE_HEAD` and no uncommitted change, then retries (`_merge_retry`). A head that is the
reviewed head plus merges holding only git's own merge and what the step may resolve (`merge.merges_onto`, which checks each file against its three sides, as `merge.not_resolutions` also checks the agent's resolution: a version file is git's merge with its version values aside, each value the changing side's or, where both changed it, the next after main's; a CHANGELOG is main's with every line the child added, the child's old version in them replaced by the one written; a lockfile loads and holds only either side's keys and values (a manifest hash aside) or a written version, and pins each package as one side does) is not reviewed again. After the push,
or with nothing to merge, the step waits for the PR's required checks
(`Coordinator._checks_pass`: `gh pr checks --required` every `CHECKS_WAIT`
seconds, or every check of the PR when none is required; failed or cancelled
parks naming it, an unreadable answer is `unknown`; checks are read only
while `gh pr view` shows the head 40.1 checked as the PR's head, since
GitHub shows the previous head for a moment after a push, and another head
is pending for `CHECKS_APPEAR` seconds, then parks; after the step's own push
"no checks reported" is pending for `CHECKS_APPEAR` seconds; still pending
after `CHECKS_LIMIT` seconds, the PR is left open, `built`, for a re-run) and
runs `gh pr merge <n> --merge --match-head-commit <the head 40.1 checked and
the step pushed>` (`Coordinator._merge_pr`): merged once `gh pr view` says
`MERGED` (exit 0 with the PR still open, as a merge queue leaves it, is
waited on), or refused `BEHIND`, which restarts at `merge.ready` and merges the
new commit (nothing new to merge parks with the refusal: a refused merge is
never retried in one run), or any other refusal, which parks with GitHub's
stderr and `mergeStateStatus` (PR E2). A file
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
