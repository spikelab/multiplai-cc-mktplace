# Build Pipeline

Deterministic Python pipeline for the `/buildme` skill. Replaces 4,370 lines of prompt-based orchestration across 4 SKILL.md files with code-driven orchestration + focused LLM calls.

## Architecture

```
SKILL.md (thin wrapper — the research request, plan review; pm-spec writes the specs first)
  └── python -m build_pipeline build
        ├── orchestrator.py    — readiness check, phase sequence, bootstrap
        ├── readiness.py       — SPEC:NOT_READY / SPEC:ISSUES, spec-issues.md
        ├── spec_generator.py  — pm-spec's proposal + requirements → design → unknowns → tasks → rubric
        ├── tdd_engine.py      — per-block TDD with agent spawning
        ├── apply.py           — manual single-agent implementation
        ├── change_manager.py  — manages specs/ directory
        ├── epic/              — an epic pm-spec split: running, reviewing and merging its children
        └── shared: config, state, models, gates, sdk, logging, env, progress
```

**Code drives orchestration. LLM handles creative work.** Gates are code assertions (not suggestions). State persists to disk (crash recovery). Agents are spawned via `claude_code_sdk.query()` with explicit tool allowlists and timeouts.

## Subcommands

```bash
# Full build (the main entry point — invoked by SKILL.md wrapper)
python -m build_pipeline build --change my-feature --project-dir /path

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

`build --change <parent>` on a change whose `.change.yaml` lists `children`
(pm-spec's split) goes to `epic.phase.run_epic_from_specs` before anything
else (`epic.phase.is_epic`). It reads each child's `depends_on` from its own
`.change.yaml` and refuses with `SPEC:NOT_READY`, exit 2 and nothing written,
when a child is misnamed, missing, depends on an unlisted child, is in a
cycle or is not ready, or when the parent's or a child's change dir is
uncommitted or not in HEAD. Then it reads HEAD's sha once, takes the lock on
`<data_dir>/buildme/epics/<owner>-<repo>--<change>/` and loads or starts
`epic.json` with the children as the change dirs list them now, and runs the
children (`epic/coordinator.py`): each is its own `python -m build_pipeline
build --change <parent>-<id> --auto --trust-repo --epic-child --pr-ready
--project-dir <P> [--base-ref <base>] [--skip-research]` subprocess in its own
process group, launched once its dependencies merged, at most
`epic.max_parallel` at once. A child with no dependency gets HEAD's sha as
`--base-ref` (`Coordinator._base`), so its PR carries the specs commit and
nothing is pushed first; a dependent one gets origin's default branch after a
fetch, and is failed with a reason instead of launched when that branch does
not hold its change dir as the specs commit does. A child's build is found by
`--change` and `--project-dir` (`build_pid`). Every child builds in the
epic's own repository. Records go to
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
`asyncio.Lock` (`merge_lock`) and on the worktree root: a merge commit made
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
stderr and `mergeStateStatus` (PR E2). Another process holding the epic's
lock, or a `specs/config.yaml` turning off `git.worktree` or `git.push`,
exits 2.

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


## Test and model reference

## Which tests a block runs

Inside each block's TDD loop, buildme runs a **narrow test command** instead of
the project's whole test command. The RED check, and the block's test writer,
implementer and refactorer, run:

1. the test files the block added or changed (its git diff since the block
   started, untracked files included);
2. for each source file the block changed, the test file named after it:
   `test_<module>.py` or `<module>_test.py` for pytest, `<name>.test.<ext>` or
   `<name>.spec.<ext>` for jest and vitest. When several packages have a test
   file of that name, the one nearest the source file is taken.

Only files the whole command collects are taken: never one under `.venv`,
`venv`, `node_modules` or another tool directory, and for pytest only one under
the config's `testpaths` (when pytest runs in the config's own directory) and
outside its `norecursedirs` and any `--ignore` / `--ignore-glob`, because pytest
runs any file named on its command line even when the whole run skips it.

Tests are never picked by what they import. The paths are appended to the test
command (`tdd.test_command`, else the discovered one) only when its runner
accepts them: pytest, or jest or vitest run directly or through an `npm test`
script that is a single jest or vitest command (the paths go after `--`).
The whole command runs instead when:

- the command is anything else, including a task runner such as tox or nox,
  even behind `uv run` or `python -m`, or a chain (`pytest && ruff check .`);
- the block selects no test file;
- the block changed a file in tests that this naming cannot pick out (Django's
  `tests.py`, a file under `__tests__/`, a helper module under `tests/`), so its
  own tests are never left out;
- the run collects coverage (`--cov` in the command, `PYTEST_ADDOPTS` or the
  pytest config; for Node, `coverage` in the command, the `npm test` script,
  package.json's `jest` key or a jest, vitest or vite config), because a narrow
  run can miss a coverage floor the whole suite meets;
- a Node test path holds a character jest reads as a regex (`[id]`, `(auth)`);
- the command already names what to run: a path in the command, in pytest's
  `addopts` or in `PYTEST_ADDOPTS`, a jest or vitest filter (`jest src/unit`,
  `vitest run unit`, `--testPathPattern`, `--findRelatedTests`), or a pytest
  option that points at another config or root (`-c`, `--rootdir`,
  `--config-file`, `--pyargs`), because appended paths would only add to it;
- the project sets pytest's `python_files`, or its pytest config cannot be read;
- a selected pytest file sits below another pytest config file or `setup.py`
  than the one the whole run uses, because pytest finds its config from the
  paths it is given; or under a `conftest.py` pytest loads (none above the
  directory of the whole run's config) whose `collect_ignore` or
  `collect_ignore_glob` could name it or a directory above it, or that has a
  `pytest_ignore_collect` hook, because pytest does not apply them to a path
  named on its command line. A list that is not written out as strings, or a
  conftest that does not parse, counts as one that names it; a mention in a
  comment or a string does not;
- the selection would make the command longer than 20,000 characters.

So RED never passes on a run that left out the block's own tests. The agents'
command is fixed before they change anything, so each agent is also told to
add the test files it writes and the test file named after each module it
changes.

The checks that decide whether work is done still run the **whole** command:
the baseline gate, the integration gate after each block (which also checks a
block's refactor), the whole-change refactor, mutation testing and epic mode's
merge step.

The baseline gate, the integration gate and epic mode's merge tests run a
pytest command with `-n auto` (pytest-xdist, one worker per CPU) when
`python -c "import xdist"` succeeds in the environment pytest runs in: the
Python beside the `pytest` script, the one before `-m pytest`, or `python`
under the same `uv run` / `poetry run` / `pdm run` / `hatch run` /
`pipenv run` / `rye run`. A command buildme cannot place in an environment
(`env X=1 pytest`, a task runner) runs as written, and so does one that
already chooses its workers (`-n`, `--numprocesses`, `--dist`, `-p no:xdist`,
`--pdb`, in the command, `addopts` or `PYTEST_ADDOPTS`), or where pytest will
not load xdist (`PYTEST_DISABLE_PLUGIN_AUTOLOAD` or `--disable-plugin-autoload`
with no `-p xdist` or `PYTEST_PLUGINS=xdist.plugin`). The narrow runs in
the loop never add it. jest and vitest already run test files in parallel,
so Node commands are unchanged.

| Key | Values | Default |
|---|---|---|
| `tdd.narrow_tests` | `true` / `false` | `true`; `false` runs the whole command in the loop too |
| `tdd.parallel` | `auto` / `off` | `auto`; `off` runs the full suite serially |
| `tdd.test_writer_max_turns` | positive integer | `30`; the block test writer's turn cap, stated in its prompt |
| `tdd.mutation_test_writer_max_turns` | positive integer | `30`; the mutation round's test writer's turn cap |

## Mutation testing

Python projects only: one of `pyproject.toml`, `setup.py`, `setup.cfg`,
`pytest.ini`, `tox.ini` or `requirements.txt` sits at the project root. A
Python project whose manifest lives in a subdirectory is not detected and
skips the step. The test command must run Python too: a `tdd.test_command` in
`specs/config.yaml` is used as given, and a discovered one must be the pytest
command found from `pytest.ini`, `pyproject.toml` or `setup.py`. A Node project
with a helper `requirements.txt` discovers `npm test` and skips the step.
Right after the whole-change refactor, buildme runs
[cosmic-ray](https://pypi.org/project/cosmic-ray/) 8.7.0 over the **non-test
`.py` lines the build added or modified** since its first block, using the
project's own test command. Files the test suite runs but never imports are
left out: `setup.py`, `noxfile.py`, `fabfile.py` and `manage.py` at any depth,
`docs/` and `doc/` at the project root, and anything under a `migrations/` or
`alembic/` directory. Every mutant on them would survive, and the survivor
round may only edit tests. Mutants that lie wholly inside a type annotation
(a parameter, return or variable annotation) are not run and do not count:
with `from __future__ import annotations` no test can kill them, and
`mutation.md` gives their number. cosmic-ray runs as a pinned tool
(`uvx --from cosmic-ray==8.7.0`), so `uv` must be installed; it is not added
to the project or to buildme's dependencies. The first run downloads it.

If any mutant survives, one fresh test-writer agent gets the survivors (file,
line, operator, the line as it is now, and the mutation's diff) and may add or
edit **tests only**. The implementer is not called. buildme then checks the
round:

- a changed non-test file discards the round and **fails the build**. Files
  the suite writes when it runs (caches, reports, a test database, a golden
  file it writes back to the same bytes) do not count, and Django's
  `<app>/tests.py` is a test;
- a red suite discards the round, and the first score stands;
- a green round is committed as `test(mutation): kill surviving mutants` and
  cosmic-ray runs again for the final score. If the agent committed its own
  tests, buildme moves the branch back to where the round started, keeping the
  files, and checks and commits them the same way.

`specs/config.yaml` keys (all optional):

| Key | Default | Meaning |
|---|---|---|
| `mutation.enabled` | `true` | Turn the step off for this project. |
| `mutation.min_score` | `0.8` | Minimum killed / (killed + survived). Timed-out test runs count as killed. **The default is a starting point:** nobody has measured mutation scores on buildme output yet, so it will be adjusted from measured runs. |
| `mutation.timeout` | `30` | Seconds allowed per test-suite run under one mutant. When the unmutated suite takes longer than a third of this, each mutant gets three times its measured run instead. The unmutated (baseline) run itself is allowed 15 minutes. |
| `mutation.max_mutants` | `300` | Cap on mutants run, after annotation mutants are left out. Above it, a deterministic sample runs, and `mutation.md` says so. |

What fails a build (`EXIT_BUILD_FAILURE`, step not marked done, so a resume
runs it again): a final score below `min_score`, a survivor round that edited
or added a non-test file (any extension, so a new `pytest.ini` counts; caches
such as `__pycache__` or `.pytest_cache`, coverage and junit reports such as
`coverage.xml`, `htmlcov/` or `junit.xml`, and `*.egg-info` do not), cosmic-ray
failing to produce a score (`uv` not installed, cosmic-ray not fetchable
offline or from a private index, red baseline, crash, timeout, or every mutant
on the changed lines erroring; the error says to set `mutation.enabled: false`
to build without the step), uncommitted changes in the tree when the step
starts, or a discard git refused — a survivor round or a
mutated file that could not be thrown away. In that last case buildme commits
nothing more, `mutation.md` included, and the tree is left as it is for you to
inspect. A bad config value keeps its default and logs a
warning. When no mutant lands on a changed line, there is no score and the step
passes. A non-Python project, `enabled: false` or a build that changed no
Python source the test suite imports logs one `SKIP phase=MUTATION` line and
moves on.

Every run that scores writes `specs/changes/<name>/mutation.md`: the score
before and after the survivor round, the threshold and verdict, the files
mutated, what the survivor round did, and every surviving mutant (file, line,
operator). It is committed onto the build branch.

cosmic-ray edits source files in place while it runs, so the tree must be
committed when the step starts. Every verified path leaves it committed, so
uncommitted changes at that point are edits nothing checked: a whole-change
refactor whose verification failed and could not be undone, one skipped on a
red suite or a failed checkpoint, a refactor agent that stopped mid-edit, or a
survivor round that could not be discarded. buildme then fails the step
without committing or resetting anything; review the changes, commit or
discard them, and resume. If a mutated file is still in the tree after a run,
buildme discards back to the starting commit; if that discard fails, the build
stops.

## Tuning model and effort (multiplai.conf)

Model and effort are two axes of one decision. With nothing configured, every
step runs on `MULTIPLAI_MODEL` / `MULTIPLAI_EFFORT` (environment, then
`multiplai.conf`), and otherwise on the model and effort of the Claude Code
session that started the build. With none of those, the CLI picks. Each
step's model and effort can be set in `specs/config.yaml` under `models:` and
`efforts:`; `multiplai.conf` sets per-stage models and efforts without a code
edit. Nothing caps a configured model or effort.

### `models:` in specs/config.yaml

```yaml
models:
  default: opus            # any spec: haiku | sonnet | opus | fable | claude:<x> | pi:<model>@<provider>
  spec: sonnet             # stage defaults
  agent: opus
  review: fable
  test_writer: opus        # step keys
  implementer: pi:deepseek/deepseek-v4-flash@fireworks
```

A value is a model spec:

- a Claude model: `haiku`, `sonnet`, `opus`, `fable`, a `claude-…` ID, or
  either one prefixed `claude:` (or `anthropic:`). A family name runs on its
  current model.
- a Pi model: `pi:<model-id>@<provider>`. It runs through
  `multiplai-core`'s Pi backend, so it **needs Pi installed and configured;
  not covered here**. A missing `pi` binary fails that step; it does not fall
  back to Claude.

An unknown key, or a value that is neither, is dropped with a warning.

Each step resolves its model through this chain, and the first one set wins:

1. `models.<step>`
2. `models.<stage>` (the step's stage, below)
3. the stage's `multiplai.conf` model: `[buildme.spec]`, `[buildme.agent]`
   or `[buildme.review] MODEL=` (`review_model`, which `code_review.model` and
   `BUILDME_REVIEW_MODEL` also set); `[buildme.plan_review] MODEL=` for
   `plan_review`, and none for `adjudication`
4. `models.default`
5. `[buildme] MODEL=` (the whole pipeline's model)
6. `MULTIPLAI_MODEL` (environment, then `multiplai.conf`)
7. the session's model

A stage model reaches every step of its stage.

### `efforts:` in specs/config.yaml

```yaml
efforts:
  default: medium          # low | medium | high | xhigh | max
  review: high             # stage
  implementer: max         # step
```

The keys are the same as `models:`. Each step resolves its effort through this
chain, and the first one set wins:

1. `efforts.<step>`
2. `efforts.<stage>`
3. the stage's `multiplai.conf` effort: `[buildme.spec]`, `[buildme.agent]` or
   `[buildme.review] EFFORT=`; `[buildme.plan_review]`, then
   `[buildme.review]`, for `plan_review`
4. `efforts.default`
5. `[buildme] EFFORT=`
6. `MULTIPLAI_EFFORT` (environment, then `multiplai.conf`)
7. the session's effort

An unknown key or effort name is dropped with a warning.

| Stage | Steps |
|---|---|
| `spec` | `spec_artifact` (design, unknowns and tasks), `rubric`, `design_audit`, `tasks_audit`, `codebase_analysis`, `explainer`, `respec`, `research` |
| `agent` | `test_writer`, `implementer`, `refactor`, `refactor_all`, `mutation_test_writer`, `integration_fix`, `review_fix`, `docs_update`, `merge` |
| `review` | `review`, `adjudication`, `test_quality_audit`, `plan_review`, `integration_fix_escalated` (the last integration-fix attempt), `review_fix_escalated` (the review loop's last fix, which questions the block's approach), `findings_triage` (the findings stage's one read-only agent per PR comment), `findings_self_check` (the findings stage's one read-only check of a round's fix commits) |

The tier (advanced or standard) comes from the default model: `models.default`,
then `[buildme] MODEL=`, then `MULTIPLAI_MODEL`, then the session's model.
Fable, or Opus 4.5 and later, is advanced; a bare `opus` or `fable` counts as
its current model. With no model anywhere the tier is standard. A Pi model's version says nothing about the tier,
so when `models.default` is a `pi:` spec the tier comes from `BUILDME_TIER`
(env) or `tier:` in specs/config.yaml, and is `standard` when neither is set.

### multiplai.conf

| Section | Tunes |
|---|---|
| `[buildme]` | `MODEL=` and `EFFORT=` for the whole pipeline |
| `[buildme.spec]` | `MODEL=` and `EFFORT=` for spec generation, audits, rubric |
| `[buildme.review]` | `MODEL=` and `EFFORT=` for code review, test-quality audit |
| `[buildme.agent]` | `MODEL=` and `EFFORT=` for the agent stage (test writer, implementer, refactorers, fixers, docs update, merge) |

```ini
[buildme]
MODEL=opus
EFFORT=medium

[buildme.review]
EFFORT=high
```

A stage section's `MODEL=` takes a Claude model or a `pi:` spec. A stage
section falls back to `models.default` / `efforts.default`, then `[buildme]`,
then `MULTIPLAI_MODEL` / `MULTIPLAI_EFFORT`, then the session.

RED evidence rejects actual collection, setup, missing-fixture and dependency errors
for every runner, including shell wrappers. Exception names in displayed source
lines are not errors by themselves. Unsupported runners need recognizable failed
test output; a nonzero exit alone is insufficient, and their classification is
still labelled unverified. For a new Python module, assert
`importlib.util.find_spec("new_module") is not None` inside the test before importing
it. This gives behavioral RED while preserving the rule against collection errors;
missing attributes and explicit `NotImplementedError` remain supported RED cases.
