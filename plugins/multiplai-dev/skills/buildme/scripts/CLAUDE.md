# Build Pipeline — Development Guide

## Module Map

| Module | Purpose | LLM calls? |
|--------|---------|-----------|
| `__main__.py` | CLI entry point with subcommands | No |
| `orchestrator.py` | Phase routing; final preparation, verification, then publication | Delegates |
| `spec_generator.py` | Sole planning publisher: atomic documents, dependency revisions, bounded repair journal | Via llm_steps |
| `tdd_engine.py` | Block-by-block TDD with agent spawning. Phase A (`_write_block_tests`) runs only when `_tests_done` finds no earlier work: `"red"` (`test_commit` an ancestor of HEAD, `red_evidence` set, `_snapshot_test_files` equal to `test_file_hashes`) goes straight to the implementer, `"committed"` (ancestor, otherwise) re-runs the quality and RED gates; the implementer then gets `_unreported_tests` (the RED evidence) as its failing tests. A test writer stopped at the Claude CLI's turn cap (`_hit_turn_limit`, `TURN_LIMIT_ERROR`) with test files on disk (`_uncommitted_test_files`: changed since HEAD or untracked, bookkeeping and specs left out) goes on to the gates (`_kept_after_turn_limit`), in Phase A and in both gate retries; Phase A gets `DRAFTS_SECTION` when it starts with such files; a capped mutation round goes through the round's checks. Each block runs test → implement → **refactor** on every tier; the refactor is verified (suite re-run + `unchanged_tests_gate`) and its diff discarded on failure. `_run_refactor_all` is the one conservative whole-change pass, between the block loop and the entry-point check, guarded by `TDDState.refactor_all_done`. buildme runs no whole-change review: that is `/multiplai-dev:review`, on the PR. | Via llm_steps |
| `signatures.py` | `signatures_section(block, project_dir)`, the test writer's context section (`assemble_context`, role `test_writer` only): for each `.py` source file `code_analysis._named_in` finds in the block text, and the one source file that defines each name in `consumes` or in a backticked name or call (a name with `_` or a capital), its module docstring line, public classes (bases, fields, plain class assignments, public methods and `__init__`), public functions, UPPER_CASE constants and private top-level names the block text names, with signatures and first docstring lines and no bodies; then, for the nearest `tests/` directory of each, the fixtures and public names of its non-test files, the `test_*` functions naming a module symbol as `path::name`, and the shortest test file importing a module (200 lines at most). Files come from `git ls-files --cached --others --exclude-standard` and are parsed from the working tree. Filled in that order up to `MAX_CHARS` (40,000), naming what it cut. Python projects only (`mutation.PYTHON_MARKERS`); never raises. | No |
| `review_rounds.py` | The per-block review loop's records, in the change dir: `reviews/block-<NN>-round-<k>.json` (`record_round` creates the next free number exclusively and never replaces a file; `update_round` / `amend_fix` fill in the fix brief and the fix agent's report, commit, `SEARCHED` list and whether it was kept; a resumed attempt's earlier fix moves to `earlier_fixes`), `snapshot`/`restore` (so a rollback keeps the records), and `unfixed-findings.md` (`append_unfixed`, heading first: it is a PR-body section, `orchestrator.unfixed_findings_section`). `fixable_on_pass` picks the Critical/Major/Minor findings. Each accepted finding gets a `finding_id` (`B<NN>-R<k>-F<n>`), and a record also holds `open_findings`, `prior_assessments` and `outside_fix_scope`; `open_state` reads the latest `kind: review` record back (an older record's `accepted` stands in for `open_findings`), `plan_round` sorts a round's findings against it, and `history_text` / `recurring_ids` feed the escalated last fix and the exhaustion diagnosis. Never raises. | No |
| `mutation.py` | The TDD engine's MUTATION sub-step, Python only (`mutation_applies`: one of `PYTHON_MARKERS` at the project root, and `mutation.enabled` — not `config.stack`, which records only the first marker discovery found and stays `""` when `tdd.test_command` is set). `run_mutation` runs cosmic-ray as a pinned `uvx --from cosmic-ray==8.7.0` tool (never a dependency): `init` → `cr-filter-git` (branch = the first block's `baseline_commit`, so only lines changed since it) → optional deterministic `max_mutants` sample → `baseline` (its own config, timeout `_SETUP_TIMEOUT`, because cosmic-ray applies `timeout` to the baseline too and a timed-out baseline reads as red) → `exec` (per-mutant timeout = `mutant_timeout`: `mutation.timeout` or `_BASELINE_FACTOR`× the measured baseline, whichever is longer) → `dump`, from a temp dir outside the worktree. The test command is wrapped in `env PATH=<buildme's PATH>` because `uvx` puts the tool's `bin/` first. `parse_dump` turns the JSON into a `MutationResult`. Refuses a dirty tree; after the run, a mutated file left in the tree is discarded back to HEAD, and a refused discard sets `MutationResult.discard_failed`. The discard sits in a `finally`, so it also runs on Ctrl-C, and `_exit_on_termination` turns SIGTERM/SIGHUP into `SystemExit` and SIGINT into `KeyboardInterrupt` for the run so they reach it too (SIGINT because `asyncio.run`'s own handler only cancels the main task, which the blocking `communicate` never sees); `_run_tool` kills cosmic-ray's process group (its own session, so the terminal's SIGINT never reaches it) on any exception or timeout before the discard; `_kill_group` first stops that group and kills every process group below it, because cosmic-ray starts each test run in a session of its own and would leave it running. The engine side (`tdd_engine._run_mutation`, guarded by `TDDState.mutation_done`) fails on a dirty tree at entry instead of checkpointing it (every verified path leaves the tree committed, so leftover edits are unverified), runs one survivor round with `run_mutation_test_writer`, folds any commits the agent made back into the tree with `git_ops.unwind_commits_to` (soft reset, ancestor-guarded) so buildme checks and commits them itself, rejects any non-test edit by hashing, and writes `mutation.md`. Any refused discard (`_RoundOutcome.discard_failed`, `discard_failed` on a run) fails the step **before** the report's whole-tree commit, which would otherwise sweep the leftover edits onto the build branch. | No (the survivor round is an agent, spawned by the engine) |
| `narrow_tests.py` | The narrow test command a block's loop runs (`tdd_engine._block_test_command` for RED; `_block_agent_test_command` / `agent_test_command` for the block's test writer, implementer and refactorer, which adds how to append the tests they write or name): `test_command` plus the test files changed since `BlockInfo.baseline_commit` (untracked included, deleted or missing left out) and the test file named after each changed source file (the nearest one), never chosen by import, and only files the whole command collects (never under `tdd_engine._is_tool_cache` directories; for pytest, `_pytest_collects`: under `testpaths`, outside `norecursedirs` and `--ignore`). Only for pytest, or jest/vitest direct or through a single-command `npm test` script (`runner_of`; a task runner before the test runner means unknown); any other runner, an empty selection, a changed file `tdd_engine._TEST_PATH_RE` calls a test but the naming cannot pick out, a coverage setting, a Node path with regex characters, a command that already names paths or filters (`_pytest_collects` / `_node_names_tests`), a pytest config it cannot read (`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`, in pytest 9's order) or a project `python_files`, a selected pytest file below another pytest config or `setup.py` than the whole run's, or under a `conftest.py` pytest loads (none above the whole run's config directory) whose written-out `collect_ignore` / `collect_ignore_glob` could name it, or that has a `pytest_ignore_collect` hook, a list it cannot read or a syntax error (`_pytest_runs_alike`, `_conftest_ignores`), a command over 20,000 characters, no baseline, a git error or `tdd.narrow_tests: false` gives the whole `test_command`. `integration_gate`, `baseline_test_gate`, the whole-change refactor, mutation and `epic/merge.py` keep the whole command. | No |
| `change_manager.py` | Manages specs/ directory (DAG, status, templates, archiving) | No |
| `config.py` | BuildConfig, tier detection, test command discovery, reference-doc resolution (`stack_reference_docs`/`reference_docs_text`: built-in stack map + `reference_docs:` overrides from `specs/config.yaml` + django/fastapi/react manifest detection; `summarize_reference_doc` reduces an over-limit doc on section boundaries and always emits the full section index). Also the per-step tuning knobs: `conf_model`/`conf_int` join `conf_effort` as the multiplai.conf readers that can say "the conf said nothing" (`pick_model` cannot — it always returns a concrete ID), feeding `spec_model`/`agent_model`/`plan_review_model`, `plan_review_effort` and the two `plan_split_*_trigger` integers; `specs/config.yaml` overrides a conf-supplied `review_model`, which is what `_review_model_from_conf` exists to record. `model_for(step)` is the one place a call site gets its model: `models.<step>` → `models.<stage>` → the per-stage field → `models.default` → `model` (`[buildme] MODEL=`) → multiplai-core's `default_model()` (`MULTIPLAI_MODEL`, then the session's model; None leaves the CLI to choose), over the `STEP_STAGES` table (`tests/test_model_per_step.py` fails when a model-bearing call site is not in it). `effort_for(step)` is the same for effort, from `efforts:`: `efforts.<step>` → `efforts.<stage>` → the per-stage effort field (`[buildme.<stage>] EFFORT=`) → `efforts.default` → `effort` → `default_effort()`; every call site passes `effort=config.effort_for(<its step>)` (`test_every_call_site_passes_its_steps_effort`). `detect_tier` reads `models.default`, `[buildme] MODEL=`, then `default_model()`; `CLAUDE_MODEL` is not read. `convention_files()` resolves the project's own `CLAUDE.md` chain for the reviewer. | No |
| `intake.py` | What a build starts from, saved in the change dir: `ticket.md` (fenced untrusted), `plan.md`, `interview.md`, `research.md`. Runs the INTERVIEW phase (`interview: self` / `ticket` / `user`) and the RESEARCH phase (copy `--research-path`, or one decision call). Owns exit code 3 (`WAITING_EXIT`) and its markers `INTERVIEW:WAITING` / `RESEARCH:NEEDED` / `SPLIT:REQUIRED`, and the bootstrap's uncommitted-`specs/config.yaml` check. | Via llm_steps |
| `setup.py` | `setup` (round-trip edit of `specs/config.yaml` with ruamel.yaml — the only ruamel user) and `config-get`. | No |
| `state.py` | BuildState with checkpoint/resume. A finished build is never deleted: `complete` checkpoints and renames `.build-state.json` to `.build-state.complete.json` (`completed_state_path`), which resume never loads; `orchestrator._completed_builds` finds the ones at phase COMPLETE, and a re-run without `--force` exits 3 with `BUILD:COMPLETE:<path>` and writes nothing (`_refuse_published_rerun`); with `--force` their branches count as taken (`git_ops.resolve_new_branch(taken=)`). A SIGTERM/SIGHUP/Ctrl-C stop (`orchestrator._record_interrupt`, and the same in `findings/loop.run_findings`) writes `INTERRUPTED by <signal>` and checkpoints the spend before re-raising. `fail` is the one way to FAILED: it records the tree (`failed_stop`, `stop_record.record`) before the checkpoint. A FAILED build re-run in its own worktree keeps that branch and worktree (`orchestrator._keeps_failed_worktree`), and refuses (exit 1) when the worktree is on another branch or `stop_record.others_work` finds an uncommitted change the record does not hold; a checkpoint with no record, uncommitted changes and no commit of its own goes to `_setup_worktree` (a `-2` worktree) as before the record existed. The kept re-run's bootstrap records its tree in `failed_stop` again, so a stop before the interview finishes (pointer AT BOOTSTRAP) resumes in place; every other bootstrap clears it. `_rebind_to_existing_worktree` binds to the candidate worktree whose checkpoint is newest (`updated_at`), so a build moved to `-2` re-runs there. `test_every_failed_checkpoint_goes_through_fail` scans the AST of `build_pipeline/` for any other write of FAILED. | No |
| `stop_record.py` | The one check every resume that keeps or discards a stopped step's work runs first. `record` saves HEAD and the project's uncommitted paths with a content fingerprint (`StepStop`, which `state.py` re-exports) as a step stops; `others_work` refuses an uncommitted path missing from that record or changed since, any uncommitted path when there is no record, and a commit added since the stop unless HEAD is wholly on origin. It checks only the project directory, minus bookkeeping and the caller's own directory. It is the only module that calls `git_ops.dirty_files` (`test_only_stop_record_compares_stop_records`). | No |
| `stop_record_cli.py` | `python -m build_pipeline.stop_record_cli`: `stop_record.others_work` for another skill, which cannot import buildme and runs it as a subprocess (spec-extract, through `uv run --directory`). Reads one JSON object on stdin (`project_dir`, absolute; `stop`, null or a `StepStop`; optional `ignore`, `head`, `origin_ref`) and prints one line, `{"reason": null}` or `{"reason": "<text>"}`, exit 0; malformed input exits 2 with the error on stderr. Imports `stop_record` and the standard library only, and adds nothing to the check. | No |
| `models.py` | Pydantic models for all structured data | No |
| `gates.py` | Quality gate assertions (pure code) + agent-report parsers (`parse_agent_status`, `parse_implementation_note`, `parse_docs_impact`, `parse_searched` for a review-fix agent's `SEARCHED:` section, `parse_red_expected` for the test writer's `RED_EXPECTED:` slot, which `red_expected_note` checks against the RED run and records without deciding anything). `red_gate` returns the first row of `RED_TABLE` whose outcome holds for `_red_facts` (`tests/test_red_table.py` records every verdict). New gates: `unknowns_gate`, `prototype_required`, `prototype_gate`, `docs_freshness_gate` (warn-only). `parallel_command` (`tdd.parallel`, `BuildConfig.parallel_tests`, parsed by `config.parse_parallel`): `baseline_test_gate` and `integration_gate` take `parallel=` and, only when the repo is trusted, append `-n auto` to a pytest command (`narrow_tests.runner_of`) whose arguments, `addopts` and `PYTEST_ADDOPTS` choose no workers (`_chose_workers`), whose pytest loads xdist (`_xdist_loads`: plugin autoloading is on, or `-p xdist` / `PYTEST_PLUGINS=xdist.plugin` names it) and whose environment imports xdist (`_xdist_probe`: `<python> -m pytest`'s python, `<uv/poetry/pdm/hatch/pipenv/rye> run python`, or the `python` beside the `pytest` script; any other wrapper is not probed); every gate call in `tdd_engine` passes `config.parallel_tests`, and `epic/merge.py` `suite_command` does the same for 40.4. | No (runs the project's Python once per full-suite run) |
| `budget.py` | Per-build token/cost ledger + circuit-breaker (module singleton; tokens and USD per label, both saved in the checkpoint; the 80% warning prints `BUDGET:WARN:<pct>:<usd>` and writes `BUDGET_WARNING` to the progress file registered with `warn_into`; every `build` run ends with a `BUDGET` section, `diagnosis(advice=False)`), and the process total (`ProcessSpend`, `process_spend`, `spend_marker`) that `record()` adds to and neither `reset()` nor `BuildBudget.load_state` touches; `__main__.main` prints `SPEND:<usd>:<tokens>` from it last for `build` and `findings` (`SPEND_COMMANDS`), in a `finally`, holding SIGTERM and SIGHUP as `SystemExit` for the run (`git_ops._exit_on_termination(interrupt=False)`) so a stop the epic's coordinator sends prints it too; `add_process_spend` / `process_spend_unknown` take a skill subprocess's own `SPEND:` line (`git_ops._count_skill_spend`, for `SPEND_REPORTING`: the pr skill's `open`/`edit`, a walkthrough `run`/`resume`, spec-extract's `run`), and a missing one adds `:incomplete` | No |
| `dependencies.py` | Pure detection of dependencies **new to this project**: parses the proposal's `## Impact` and design's `## Decisions`, subtracts every manifest (`pyproject.toml`, `package.json`, `Package.swift`, `Cargo.toml`, `go.mod`, `requirements.txt`) and every existing import. Feeds the B1 explainer gate. No LLM, no network. | No |
| `git_ops.py` | Every `git`/`gh` invocation: worktree+branch setup, explicit-path commits, the TDD loop's whole-tree `commit_tree`/`tree_is_clean`/`discard_to`, push, and the seam that runs the pr skill (`_run_pr_skill`, `open_pr_with_skill`; the argv, `uv run --directory ../../pr/scripts python -m pr_pipeline open --no-push`, is `orchestrator._pr_open_argv`), which opens the PR from the repo's template. `shell=False`, fixed argv, never merges/force-pushes/deletes (the epic's merge step runs `git merge`, `merge --abort` and `commit` through its own wrapper, `epic/merge.py`, and `gh pr checks` / `gh pr merge` through `_run_gh`, under `epic.merge: auto`); the one destructive call (`discard_to`) refuses unless the target sha is still an ancestor of HEAD. Every commit and the discard are limited to the project directory, which may be a subdirectory of a larger repo: `commit_tree` and `commit_paths` commit only their own pathspec (`git commit -- <paths>`), so anything else staged stays staged, and `discard_to` runs `reset --soft`, `restore --source=<sha> --staged --worktree -- .` and the `clean`, and refuses when a commit it would drop changes a file outside the project. `BOOKKEEPING_EXCLUDES` is the single list of buildme's own files (`pr-sections/` included), honored by every commit path and by the discard's `clean`. | No |
| `code_analysis.py` | Checked, cited facts about the project's code, from the shared `code_facts` package (`plugins/multiplai-dev/lib/code_facts`, an editable path source). `analyse` builds the scope (source files at HEAD from `git_ops.files_at`; test files by walkthrough's `--depth overview` rule, vendored/generated/tool directories, binaries left out), adds one dependency slice of manifests and config files (`config_files`, `dependency_slice`; its file and fact entries render under Dependencies), caps the source at `codebase_analysis_max_units` units less that slice (`limit_scope`: files the change's documents name, then the most imported by a text heuristic), points each source unit at up to two tests importing its modules for test style only (`tests_for_units`), runs one agent per unit through the caller's call function (no retry of its own) and gates every citation with `multiplai_core.check_quote` at HEAD, resolving a monorepo subpackage through the repo prefix. `render_markdown` writes Architecture/Patterns/Dependencies/Conventions with `path:line` links plus "Not analysed"; `write_facts_file` writes `facts.json` in review's `facts.v1` format (kind `tree`, base = git's empty tree). `block_facts_section` is the `block_facts` context section: the facts naming a file the block's text names, re-checked at the current HEAD. | No (the caller's call function makes them) |
| `walkthroughs.py` | The first `orchestrator.PRE_PUBLISH_CONTRIBUTORS` entry (`pr_section`): runs codebase-walkthrough's model-free `check --at-head` on `docs/walkthroughs/*-walkthrough.md` through `git_ops._run_walkthrough_skill` (`uv run --directory ../../codebase-walkthrough/scripts`), with `--repo key=path` for each header repo resolved from the main checkout (`_locate`; the project's own maps to the worktree), then checks each stale one at `BuildState.base_sha` (a copy of the file at the base with its header commits pinned) so one already stale before the build is reported apart and never regenerated; prints `WALKTHROUGH:` lines and writes the "Stale walkthroughs" PR-body section. With `regenerate_walkthroughs`, `regenerate` re-runs a stale one from its header's module directory, depth and mode with `--repos <its one repo>` and `--max-usd walkthrough_max_usd`, only when `sdk._repo_is_trusted()`, and commits it with `commit_stage`. `tests/conftest.py` refuses any test that starts a real walkthrough `run`. | No (a regenerated walkthrough's run does, in its own process) |
| `board.py` | Board seam: pure `column_for(phase, block_status)` → `BoardColumn`, `.board.json` card writer, `BOARD:<slug>:<Column>` stdout line. Drives Shaping → Planning → In Development → In Review only; its docstring names the columns it never sets. The `BoardCard`/`BoardEvent` pydantic models live here deliberately (they are the card file's private schema, not shared pipeline data). | No |
| `sdk.py` | `llm_call()` / `llm_call_structured()` (toolless) + `agent_call()` / `agent_call_structured()` (tool-holding) adapters over `multiplai_core.run_agent()`. `agent_call_structured` is the reviewer's entry point: it runs the agent loop, then parses the final message into a pydantic model (`multiplai_core.structured.parse_answer`, as `llm_call_structured` does) with one re-run on a parse failure. Every `agent_call` path goes through `_require_trusted_repo()`, so a tool-holding review needs `--trust-repo` / `BUILDME_TRUST_REPO=1`. | Yes (SDK) |
| `rubric.py` | Rubric generation and change type detection | Via sdk |
| _(logging)_ | `multiplai_core.setup_logging("build-pipeline", packages=("build_pipeline", "code_facts"))` in `__main__.py`: `<data_dir>/logs/build-pipeline-<UTC date>.log`, kept 365 days (`MULTIPLAI_LOG_RETENTION_DAYS`). `pin_session_settings()` runs before `main()`, and the run logs the model and effort it pinned | No |
| `env.py` | .env loading, multiplai.conf parsing, model resolution | No |
| `progress.py` | Tail-able progress file writer, append-only: `initialize` writes the header (with `default_settings_line()`: the model and effort the run started with and their source) only when the file is missing, and otherwise appends a `RESUMED` section. Nothing deletes the file. | No |
| `epic/` | Epic mode, PRs A, B1, B2, C, D2, E1, E2 and F (plan-buildme-epic-mode-2026-10-12.md). PR F: a child's `repo` (split.md's fourth column) is a local checkout, absolute or relative to the epic's repository root (`store.checkout_path`); approval refuses one that is not the top of a checkout with an `origin` remote (`store.repo_errors`, `store.checkout_root`, exit 1, nothing approved); `Coordinator._place` gives each `Child` its `project` (`--project-dir`), `root` (base fetch, `git worktree list`, `gh pr list`, `fix_pid`, the worktree fallback, the merge lock's key) and review `slug`, saved in `child_status` and kept once the checkout is gone, or the epic's own when none was saved, as for a child approved before PR F (F.S11); `gh pr view` keeps the epic's repo as cwd, as it reads the PR by URL; a checkout gone (or no longer the top of a checkout) at a run's start or before a launch parks the child and saves `repo_park`, which `_recover` keeps (`_park_missing`, F.S5); a `git` there that timed out or did not start (`store.GitUnreadable`, exit 124 or 127 from `git_ops._run`) makes the child `unknown` instead, and approval reports it as an error. `split.py`: `epic_split`, the one structured call (`STEP_STAGES["epic_split"]`, spec stage; tests patch it as a module attribute, so callers use `split.epic_split`), its inputs (`ticket.md`, `plan.md`, `interview.md`, `research.md`, the context files, `codebase-analysis.md`) and `check_answer`/`check_children` (unique lowercase hyphenated ids, every `depends_on` names a child, no cycle, each plan holds `# Plan:` and `## Done means`; at least two children). `store.py`: the epic directory `<data_dir>/buildme/epics/<owner>-<repo>--<change>/`, named with the review skill's slug rule copied from `review_pipeline/target.py` (pinned by `test_epic_split.py::TestSlug`), `epic.json` (`Epic`, saved with `multiplai_core.checkpoint.save_model`), `split.md` (its table is what approval reads back) and `plans/<id>.md`, and the approval hashes. `lock.py`: an exclusive `flock` on `<epic dir>/.lock`, taken before the first write and held until the process exits, once per process (`hold` returns at once when this process holds it; a second `flock` on a new descriptor would refuse its own process). `phase.py`: `run_epic_split_phase` (skip for `--no-epic-split` or `only` mode; one call and at most one retry with the errors; one change completes the phase and writes nothing; several takes the lock, saves `epic.json` first, renders the files, then saves `state.epic_dir` and the phase in one checkpoint; entered with `epic.json` present, no call, and only missing files are rendered, so a hand edit stays) and `continue_epic` (`proposed`: `EPIC:SPLIT`, exit 3, until `--approve-split`, never in the run that wrote the split; `approved`: hashes checked, `EPIC:APPROVED`, then `coordinator.run_epic`). `coordinator.py` (PR B1): runs in the approved build's process under the lock it already holds (asserts `lock.held`, never takes it again); each child is a `python -m build_pipeline build --mode brief --change <epic>-<id> --plan-file ... [--research-path <parent research.md> | --skip-research] --auto --trust-repo --no-epic-split --epic-child --pr-ready --project-dir <source project dir, or its own repo's checkout> --base-ref <origin default sha, or the parent's registry commit>` subprocess (refused, exit 2, when the `specs/config.yaml` of the source project or of any child's own repo turns off `git.worktree` or `git.push`: `in_place_reason`) (`python` from PATH, cwd the scripts dir) in its own session, output to its log file, never a pipe; a child is ready when every dependency's PR is `MERGED`; at most `epic.max_parallel` at once, one start at a time (the next waits for `WORKTREE:` or an exit); every wait goes through `CLOCK`; `_tick` and `_spawn` are seams for the row tests. Each run first rebuilds every child's status from the records (`_recover`): a started record with no result whose build is alive (`build_pid`, one `ps -ax -ww` line with `build_pipeline` and `--plan-file <plan>`) is waited for; when `ps`, `git` or `gh` fails while checking a stopped child (`ReadFailed`), the child is `unknown`, nothing is launched or written for it, and the run exits 1; a gone one gets its result from its log (`exit_code: null`, `interrupted: true`) and its PR is looked for (`PR:` line, else `gh pr list --head` for every `BRANCH:` line and `git worktree list` branch of a `WORKTREE:` path); found is `built`, none is relaunched with the started record's argv. Exit 3 or exit 0 without a PR parks (dependants `skipped`); exit 1/2 or a foreign signal is `failed`, relaunched once per invocation. Waited-on PRs are read with `gh pr view --json state` every 30 s while anything runs (`CLOSED` parks; a failed read makes the child `unknown` until a read answers); nothing to run with one open prints `EPIC:MERGE_WAITING:<urls>`, exit 3. Before a child's first launch, `git fetch origin` runs as an asyncio subprocess (killed after `FETCH_TIMEOUT` or when the run is stopped) and `--base-ref` is origin's default branch, except that a child with no dependency whose root is the epic's repository branches from the parent's commit that holds its `specs/registry` baseline (`registry_commit`: the last commit after the parent's `spec_gen.spec_extract_sha` that changed `<specs_dir>/registry`, used only when origin's default sha holds every parent of that commit and the commit changes nothing outside the registry, so a parent started from a feature branch or unpushed commits, or a commit added before a resume, gives its children origin's default branch, and not once origin's default sha holds a `<specs_dir>/registry/*.md`, when a child branches from that newer tip; chosen by `Coordinator._base`), so the registry reaches main with the first merged child PR; every child's PR targets the default branch (`_run_publish` skips `_pr_base_branch` when `orchestrator._epic_child`: `--epic-child` in the argv, so a hand re-run of a saved command keeps it, or `BUILDME_EPIC_CHILD`, which `_spawn` sets), since a sibling's pushed branch also holds that commit; a failed fetch makes a child with dependencies `unknown`, and a `specs/config.yaml` at HEAD that is not the base's version (`intake.config_yaml_off_ref`) makes it `failed`, in both cases with no record, so the next run fetches again. `runs/<NN>/summary.json` and `EPIC:SUMMARY:` end every run that gets past the `git.worktree`/`git.push` refusal (which writes neither), including a stop by a signal or an unexpected error. SIGINT/SIGTERM signal each running child's group, adopted ones included (a session leader's pid is its group), wait 30 s, and write the exited ones' results `interrupted: true` (exit 128 + signal). `records.py`: `agents/<NNN>-<kind>-<child>.started.json` / `.result.json` / `.log`, written atomically (kind and child are read from the started record, which the name must match), and the marker lines read back from a log; each result keeps `spend`, the USD of the log's `SPEND:` line (null without one); a `merge` record has no log line, so its `spend` is the change in `budget.process_spend()` around the merge agent's call (null when the call was interrupted). `spend.py` (PR B2): the epic's spend, the `spend` of every `build`/`fix`/`merge` result (null or no result: counts 0 and makes it incomplete; a `SPEND:` ending `:incomplete` counts and makes it incomplete) plus, from `<data_dir>/reviews/ledger.jsonl` read as a file, among `finished` lines whose slug is a child PR's (`<owner>-<repo>--pr-<n>`), per `(target_dir, head_sha)` and per review run (`run.started_at`, which a `resume` keeps; else the line's `round`), the last `run.cost_usd`, summed over runs; a `started` line with no later `finished`, or a `finished` line with no readable cost, makes it incomplete; so does a run killed (a `started` line followed by another) or ended with `run: null` when the next run of that `(target_dir, head_sha)` to finish is not its `resume` (`_Unaccounted`: a resume's `run.started_at` is before the `started` line that followed the lost run). The coordinator saves it in `epic.json` and each summary (`spent_usd`, `spend_complete`) and, before each launch, once `epic.max_usd` is reached launches nothing more, waits for the running children and exits 3 with `EPIC:BUDGET:<spent>`. `review.py` (PR C): the review of each child's open PR, `uv run --directory <review scripts> python -m review_pipeline review --repo <worktree> --pr <n> --trust-repo --fetch --extra-finders` (argv as `findings/loop.py` builds it, `--fetch` included, plus `--extra-finders`, which the review skill honours on the PR's first round only), run by `Coordinator._review` as a task in its own session with output to its record's log; buildme never imports `review_pipeline` and reads `<data_dir>/reviews/ledger.jsonl` as a file. A head with a `finished`, `stopped: null` line for the PR (slug `<owner>-<repo>--`) is never reviewed again (`_reuse`: writes the open record's result `exit_code: null, interrupted: true, reused: true`, or one pair when no record of the head holds a review's fields; its `review_dir`, which the blockers and the fix step's post read, is `review.review_folder`: the line's kept round `rounds/<round>-<head12>/` once a later run in the folder moved it there, else `target_dir`); while the ledger shows a review of the head `started` and its run lock (`lock_held`) is held, nothing is launched and it looks again every `REVIEW_WAIT`; a review that exits 0 with no complete `finished` line for the head read before it reads the PR again and loops when the head moved (the review reads the head itself); exit 2 `is running now` waits `REVIEW_WAIT` (60 s) and asks again; exit 4 with `review`'s resume hint runs `resume`; exit 2 whose `ERROR:` line says the review's `git fetch` or `gh pr view` failed or a PR commit is not in the clone (`READ_FAILED`) is a failed read, not a park: the record gets a plain result, the child is `unknown` and the next run asks again (C.S20); a review, resume or post that cannot start (`OSError` from `Popen`, such as no `uv` on PATH) gets a result with no exit code and leaves the child `unknown` (`StartFailed` for the review and resume); anything else parks with the last 400 characters. `--repo` and the cwd are the child's worktree, or its repo root (`Child.root`) when the worktree is gone (`_worktree`). `pr_round` is the number of distinct heads among the PR's complete reviews, recomputed before each review and cached with `conflicts` in `epic.json`'s `child_status`; a result with open Code HIGH/MEDIUM findings (`blocking_findings`, the review's own listing rule via `findings.decisions._listed`) also holds `next` and `next_reason` from `review.decide` (plan-review-rounds-and-finders-2026-10-15.md, item 2; reads only the findings' severity and verified `impact`, no model call): `round` for an open HIGH, or below `epic.max_review_rounds` a MEDIUM that `epic.round_worth` says earns one (`breaks`: not rated `correctness-only`/`hygiene`, `HARMLESS_IMPACTS`; `any`: every one); `final_fix` for MEDIUMs alone otherwise, and at the cap; `park` for an open HIGH at the cap, when the result holds `parked`, which `_recover` keeps on later runs. Reviews count against `max_parallel` and the budget; SIGINT/SIGTERM signal the reviews this run started (`Child.rproc`) with the children, never one an earlier run left or another process runs; SIGHUP leaves them running. `fix.py` (PR D2): a recorded review with blockers and no `fix` record after it runs the fix loop (`Coordinator._fix`): `fix.write_decisions` (`findings.decisions.write_decisions` at `review.BLOCKING_SEVERITIES`, `code_only`) on the review's folder, `review_pipeline post <review dir> --decisions <file>` (kind `post`, skipped when a `post` after the review exited 0), then `python -m build_pipeline findings --pr <n> --change <epic>-<id> --project-dir <P> --auto --trust-repo --max-rounds <M> --severities HIGH,MEDIUM --code-only --no-escalation [--max-usd <left>]` (kind `fix`; `<left>` = `Coordinator._left`: `epic.max_usd` less `spent()` and less `_held()`, what the other running fixers' `shares` (in memory) hold beyond their PRs' reviews since launch, only when it is set; a fixer waits while `_left` is 0 or less and the cap is not reached, `_loop` starts no build or review while `_held_back`, and `_unheld` holds back a review step's own review or resume and the merge agent the same way; a fixer that exits 0 with `FINDINGS:BUDGET` (`fix.budget_stopped`) gets an `interrupted` result: with `spent()` at `epic.max_usd` it leaves the child `built` and `budget_stop` is set, so the run exits 3 with `EPIC:BUDGET` and a run under a higher `epic.max_usd` runs it again, D2.S16; under it, it used up only its share and the review step runs it again through `_fix_pending` with its share recomputed, D2.S19), cwd `<P>` (the child's worktree plus the project's offset), `<M>` = every round in the change's `.findings-state.json` (under the normalised change name, as `findings` resolves it), finished or not, plus one (`fix.rounds_started`: one round per launch, so a decision follows every review); when it exits 0 the review step reads the PR again and reuses the fixer's last review. A review whose `next` is `final_fix` runs the same fixer, with the same `<M>`, plus `--no-review` (`fix.FINAL_FLAG`); its exit-0 result keeps `final_head`. The review step sends a head equal to `_final_head` straight to the merge step (R3), and `_clean_head` returns it as the reviewed head. At the end of each run, a final-fixed child that merged, or waits for a human merge under `manual`, gets one issue (`_left_issue`: `gh issue list --search` for its title first, so a re-run files none, R4); a final fix whose log ends `FINDINGS:DONE` (`fix.acted_on_nothing`: its fetch found no new comment, so it ran no round) still goes to the merge step, and its issue says no fix ran on what it lists. `_fix_pending` runs first on every pass: an open `post` is waited for while `post_pid` (a `ps` line with `review_pipeline` and `post <review dir>`) finds it, then gets an `interrupted` result; the last `fix` with no result is waited for while `fix_pid` (a `ps` line with `build_pipeline`, `findings`, `--change` and `--project-dir`) finds it; when its log holds `FINDINGS:FAILED` it parks with the reason, else it gets `exit_code: null, interrupted: true`; that one or an `interrupted` one runs again with its saved `--max-rounds` (`fix.saved_rounds`, row R2), unless it is not a final fix and no review round is left. A post that died of a signal, or a fixer killed or stopped by SIGTERM/SIGHUP (exit 143/129, `fix.stopped`), is `interrupted` and the child stays `built`; `FINDINGS:FAILED` (the reason), another exit, a failed post or an unreadable or unwritable decisions file parks it, and `_recover` keeps a `post` or `fix` park as it keeps a review's. `merge.py` (PR E1): what the merge step reads and runs, every git command on the child's worktree root `<W>` (item 36). A PR whose review at its current head left no blockers (`_passed`: that head is `_clean_head`, or it plus the merge step's own merges) is waited on (`_waited_on`), so a run with one open exits 3 with `EPIC:MERGE_WAITING`; a PR whose current head no review finished on after its review step ran (`_unreviewed`: the review was stopped, or a new head came after a clean review) is named on stderr and makes the run exit 1 unless it exits 3; under `epic.merge: auto`, `Coordinator._merge` first runs under one `asyncio.Lock` per repo root (`merge_locks`): an unpushed merge commit of `origin/<default>` onto the reviewed head (`merge.unpushed_merge`, item 37) is checked with `stop_record.others_work`, tested and pushed; a merge in progress (`MERGE_HEAD`) is aborted only when `others_work(<W>, merge_stop, head=<last clean reviewed head>, origin_ref=refs/remotes/origin/<branch>)` passes (`_abort`, the one function that runs `merge --abort`; a refused abort parks with git's stderr and `git -C <W> checkout -- <files>, then git -C <W> merge --abort`); then 40.1 `merge.ready`, 40.2 (`merge_stop` cleared, `git fetch`, `git merge --no-ff --no-commit origin/<default>`), 40.3 one `merge` agent (`run_merge_agent`, `prompts/epic_merge.py`, step `merge`, agent stage, trusted for that call only) for the files `merge.plan` lists (from `git merge-tree` and the three sides: conflicted lockfiles, CHANGELOGs and files whose conflicts are only in the package's own version fields, `_own`; plus version files both sides raised, which git merges cleanly when both wrote the same value, with the CHANGELOGs and lockfiles both changed and each CHANGELOG only the child changed whose added lines name a version it raised; a deleted or binary side, or any other conflict, is `refused` and parks before any call; over `epic.max_usd`, `agent_ahead` holds back only a merge that would launch the agent), checked for markers, for other changed files (the park's hint, `undo_hint`, undoes them before the abort) and by content against the three sides (`not_resolutions`: a version file is `git merge-file` of the sides with version values aside, each value `_expected`, the next after main's where both raised it, `_next`; a CHANGELOG is main's with the child's added lines, its old version replaced by the one written, `_interleaves`, checked even when git merged it cleanly if those lines name that version; a lockfile read as data loads and holds only either side's keys and values, a written version or a hex manifest hash, and each pinned package is one side's under the same name, `_lockfile`; another holds only either side's lines or a written version; `_only_resolutions` keeps its answer per merge commit sha), run in a thread, then staged; a change the merge did not make parks before the agent (`outside_merge`, E.S28); 40.4 the tests (`merge.suite_command`: `merge.test_command`, which is `tdd.test_command`, else discovery, with `-n auto` from `gates.parallel_command` under `tdd.parallel`) as a `merge-tests` record, run by `_run_step` in its own session with `BUILDME_TRUST_REPO=1` and `TESTS_TIMEOUT`, then `git commit --no-edit` and `git_ops.push_branch` (never forced; rejected: `push rejected: origin/<branch> moved to <sha>`). An exception or signal saves `merge_stop` (`stop_record.record(<W>)`, item 39); every park saves `merge_park`, which `_recover` re-parks on later runs (E.S22). A PR head that is the reviewed head plus only merges of `origin/<default>` (`merge.merges_onto`) goes to the merge step, not to a review. Then (PR E2) 40.5 `_checks_pass` (`_read_checks`: `gh pr checks <pr> --required --json name,bucket` every `CHECKS_WAIT`, and without `--required` when none is required; a `fail` or `cancel` bucket parks naming the check; checks are read only while `gh pr view --json headRefOid` shows the head 40.6 will merge, and another head is pending for `CHECKS_APPEAR` seconds, then parks (GitHub shows the previous head for about 1.5 s after a push); gh's "no checks reported" is none, but pending for `CHECKS_APPEAR` seconds after the step's own push; pending past `CHECKS_LIMIT` leaves the PR open, `built`; another unreadable answer is `unknown`) and 40.6 `_merge_pr` (`gh pr merge <pr> --merge --match-head-commit <HEAD of <W> after 40.1 and the push>`, never `--admin`; then `gh pr view --json state,mergeStateStatus`: `MERGED` is merged; exit 0 with the PR open (a merge queue) leaves it `built`, waited on; on a non-zero exit `BEHIND` restarts at 40.1 through `_merge_default`, which parks with the refusal when it finds nothing new to merge, anything else parks with `gh pr merge refused: <stderr> (mergeStateStatus <value>)`); a merged child's status is `merged`. `_merge_point` is the row tests' seam (`start`, `merged`, `push`, `checks`). Runs `git rev-parse` and `git worktree list` in each child's repo root, and `gh pr view` / `gh pr list` reads and `gh pr checks` / `gh pr merge`, all through `git_ops._run`/`_run_gh` with their timeouts (`git fetch` is the asyncio subprocess above); never `reset`, `dirty_files`, `tree_is_clean` or `is_dirty`. | Via sdk |
| `findings/` | `python -m build_pipeline findings --pr <n>` (the `/multiplai-dev:buildme-findings` skill). `fetch.py` reads a PR's unresolved review threads and new top-level comments through `git_ops._run_gh`, decides whose are acted on (`findings.authors`, `findings.allow_bots`) and flags injection-shaped bodies; `triage.py` runs one read-only agent (`Read`/`Glob`/`Grep`) per allowed comment on the fenced body and writes `findings-r<N>.md`; `spec.py` writes `respec-findings-r<N>.md`, applies it to the change's `requirements/*.md` with `ChangeManager._apply_delta` and commits `spec(findings-r<N>): <ids>`; `code.py` turns each `code_fix` into a block of `findings-r<N>-tasks.md` (whose brief asks the fixer to search the same function, its other branches, its callers and the rest of the PR's diff for the same mistake and to close with `SEARCHED:` before any test-change declaration, recorded per fix as `searched` and quoted in the reply, never gated) and runs `run_tdd_engine(standalone=False)` on it through the `BuildConfig` overrides `change_dir` / `tasks_file` / `state_file`; once the engine returns 0 (not when it stops after the blocks, `code_check_failed`), `loop.do_code` runs one read-only check agent (`code.run_self_check`, step `findings_self_check`) over `git diff <FindingsRound.code_base>..HEAD` (none for an empty range) and saves its answer in `FindingsRound.self_check` before writing anything, so it never runs twice; a listed problem becomes block `k` (`findings-r<N>-self-check.md` and the tasks file in one commit, then `code.add_block`; it skips the whole-change refactor and mutation testing, whose done flags are already set), and every re-render of both files comes from saved state, so a resume commits nothing new; and answers "not fixed" only for a block `failed_block` finds failed on its own work (a block the engine marks `resumable`, stopped by an agent call that timed out or errored or by the budget, is resumed instead); `publish.py` pushes, then refreshes the PR body through the pr skill's `edit --section`, then replies (each reply checkpointed before the next, never a thread resolve); `loop.py` checkpoints each step's start and finish in `<change>/.findings-state.json` (a `BuildState` with only `findings` set: `.build-state.json` is never recreated, because the pr skill reads its presence as a build in progress) and keeps a failed triage/spec step's edits under `refs/buildme/wip/<change>/r<N>-<step>` before `discard_to`, and, before a code step resumes, the stopped engine attempt's edits under `r<N>-code-block<k>-stopped` (`keep_stopped_code_work`); before any resumed step keeps or discards, `others_work` refuses an uncommitted change, or a commit, that the stopped step did not record when it stopped (`FindingsRound.stops`, written by `record_stop` through `stop_record.record`; the comparison is `stop_record.others_work`); under `--auto`, `decisions.write_decisions` writes `buildme-decisions.json` next to the review's `findings.json` (outside the worktree; with `--severities`, only findings of those severities; with `--code-only`, only Code findings) and `post_review` passes it as `--decisions`; with `--no-review` (`FindingsRun.review` false, an epic's final fix) a round's review step runs no review and is recorded finished with no `review_run_dir`; with `--max-usd`, `run_findings` stops after a fetch that found something to act on, before its round, once `spend_meter` (this process's `budget.process_spend` plus the PR's reviews in the review ledger, `review_spend`, each less its value at the start, rounded to 6 decimals) reaches it, exit 0 with `FINDINGS:BUDGET:<rounds>:<usd>`; `run_review` resumes a review of the PR head that was killed part way (the review skill's exit 4 naming `resume <target_dir>`) instead of reporting it as a failed review; `history.py` reads every review of the PR (`findings.json` in each target directory the review ledger records for it, `ledger_dirs`, and in each round's `review_run_dir`, with their kept `rounds/`, each review once, oldest first) for three things: triage's earlier verdicts on the comment's lineage (`earlier_rows`, rebuilt from the saved rounds on every run and fenced as data), a fix block's `other_locations` (task `.4` then adds `REVIEW_FIX_LOCATIONS`, the sentence the engine's review-fix brief uses), and on an escalated round (`FindingsRound.escalated`, saved when the round starts: its number is `max_rounds` and at least 2, and the run has no `--no-escalation`, `FindingsRun.escalate`) `REVIEW_FIX_ESCALATION` over every lineage as task `.0` of every block (`code.escalation_task`, one line, since `parse_blocks` keeps only checkbox lines), with the implementer run as `review_fix_escalated` through `BuildConfig.step_aliases` (`code.round_config`) and each fixer's own `RECURRING:` slot saved in `FindingsRound.recurring` (`code.recurring_from_state`, the report before any later agent's marker); after `FINDINGS:MAX_ROUNDS`, `recurring_lines` prints `FINDINGS_RECURRING:<lineage>:<rounds>` for each lineage open in two or more reviews and `FINDINGS_RECURRING_SLOT:<block>:<answer>` for the last round's slots; `cli.py` resolves change, branch and worktree. `README.md` holds the GitHub calls as checked on a scratch repo. | Via sdk |

## LLM Steps (llm_steps/)

| File | Functions | What They Do |
|------|-----------|-------------|
| `spec_steps.py` | `generate_artifact()`, `run_explainer()`, `run_design_audit()`, `run_tasks_audit()`, `run_codebase_analysis()` | `run_explainer()` is the B1 explainer gate — one call per `dependencies.detect_new_dependencies()` hit, run concurrently with `WebSearch`/`WebFetch`/`Read`/`Glob`/`Grep`, concatenated into `unknowns.md`; `unknowns_gate` then forces at most one regeneration pass (`spec_generator._audit_unknowns`, recorded as `SpecGenState.steps["explainers"]`). Spec generation + adversarial audits (design audit and tasks-shape audit, both wired — the tasks audit forces one regeneration pass on horizontal-decomposition findings, and on `no-new-behaviour` blocks from `spec_generator.scan_no_behaviour_blocks`: a block that produces nothing and whose own text says it adds no code (it says an earlier block already satisfies it, with itself as the subject; it says, unscoped, that it needs no code; it is test work, by its heading or first instruction, changes only test files and has no step that writes code; or it adds tests, leaves every file it names unchanged and has no step that writes code), so its tests would pass before it is built and fail the TDD RED gate; it reads a block's prose and checkboxes, and never flags on `Produces: (none)` or a "Verify"/"Confirm" verb alone; `report_no_behaviour_blocks_left` names any block still flagged after a rewrite, `PHASE: no_behaviour_blocks_remain`). `run_design_audit()` covers consistency **and** plan quality (over-engineering, granularity, testability, edge cases); its critical/major gaps force one regeneration pass of design.md + tasks.md through the single call site `spec_generator.run_design_audit_stage` (recorded as `SpecGenState.steps["design_audit"].repair_attempts`, then a required re-audit; the outcome is tied to its input revision, so an unchanged resume is free). `run_codebase_analysis()` (one `code_facts` explore agent per slice of the source files, through `code_analysis.analyse`, with `_codebase_analysis_call` as its call function; a project with no commit gets `code_analysis.no_commit_report`, a labelled "not analysed" list of its source files) is **wired** as `BuildPhase.CODEBASE_ANALYSIS` — the orchestrator writes its report to `codebase-analysis.md` and records the path on `SpecGenState.codebase_analysis_path`; `spec_generator.read_codebase_analysis` turns that path back into the text the design prompt inlines. |
| `prototype_steps.py` | Optional prototype before final planning review. Findings request a revision from the shared owner. Failed writers block completion. |
| `tdd_steps.py` | `run_test_writer()`, `run_mutation_test_writer()`, `run_implementer()`, `run_refactorer()`, `run_refactor_all()`, `run_integration_fix()` | TDD agent spawning with tool allowlists. Reports carry the `SURPRISES:` / `SPEC_IMPACT:` REQUIRED slots parsed by `gates.parse_implementation_note`. `run_refactor_all()` is the whole-change pass: same `REFACTORER_TOOLS`, more turns/time, `budget_label="refactor_all"`. `run_mutation_test_writer()` is the MUTATION step's one survivor round: `TEST_WRITER_TOOLS`, `budget_label="mutation_test_writer"`. |
| `respec_steps.py` | `append_implementation_note()`, `format_implementation_note()`, `notes_path()`, `ensure_delta_sections()`, `run_respec_audit()` | B3 loop back to the spec. Each parsed `ImplementationNote` is appended to `implementation-notes.md` **as the build runs** (a crashed build still leaves the learning on disk). `run_respec_audit()` (BuildPhase.RESPEC, after TDD_BUILD) reads those notes + current requirements/design and writes `respec.md` in ADDED/MODIFIED/REMOVED form — **propose only, never edits the specs**, and non-fatal on LLM failure. |
| `docs_steps.py` | Documentation writer; failures propagate. Freshness warnings remain advisory after a successful writer. |
| `review_steps.py` | `run_code_review()`, `merge_panel_results()`, `run_review_fix()` | `run_code_review()` calls `agent_call_structured` with `REVIEWER_TOOLS = ["Read", "Grep", "Glob"]` and `REVIEWER_MAX_TURNS = 30`, so the reviewer opens the files a changed line calls into instead of judging a diff string — and the whole step now needs a trusted repo. It is **wired** as the active per-block review — `tdd_engine._run_quality_review` calls it with the block's actual diff, rubric, spec context, and coding standards. Runs every model in `config.review_panel` concurrently (empty panel → one reviewer on `review_model`-or-`model`, byte-identical to the pre-panel behavior), drops members that failed, and folds the survivors with `merge_panel_results()`. `run_review_fix()` remains **not wired**. Security review is out of scope for this pipeline — the dedicated security-review prompt, step function and `security_review` config toggle were deleted; use Claude Code's built-in `/security-review` instead. |
| `interview_steps.py` | `ask_questions()`, `answer_questions()`, `run_self_interview()`, `decide_research()` | `interview: self` is two separate read-only agent calls (`Read`/`Glob`/`Grep`, `budget_label="interview"`): a questioner, then a fresh answerer whose ungrounded answers become assumptions. `ticket` mode uses the questioner alone. `decide_research()` is one toolless `llm_call_structured` (`budget_label="research"`). Each takes `call=` for tests. |
| `plan_review_steps.py` | Read-only final plan review; bounded repairs use the planning owner. Failed/unresolved required review blocks implementation. |

## Prompt Templates (prompts/)

Templates are Python f-strings with `{placeholders}`. Each template is a constant in its module.

| File | Templates |
|------|-----------|
| `spec_generation.py` | PROPOSAL_PROMPT, SPEC_PROMPT, DESIGN_PROMPT, TASKS_PROMPT |
| `test_writing.py` | TEST_WRITER_PROMPT, TEST_QUALITY_PROMPT, MUTATION_TEST_WRITER_PROMPT (both writers' prompts state `{max_turns}`, from `tdd.test_writer_max_turns` / `tdd.mutation_test_writer_max_turns`), DRAFTS_SECTION |
| `implementation.py` | IMPLEMENTER_PROMPT_CLEAN, IMPLEMENTER_PROMPT_MINIMUM, REFACTOR_PROMPT, REFACTOR_ALL_PROMPT |
| `writing_rules.py` | TEST_VALUE_RULE (in TEST_WRITER_PROMPT), COMMENT_RULE (in TEST_WRITER_PROMPT and both implementer prompts): the writing rules, concatenated into those prompts (`tests/test_writing_rules.py`) |
| `review.py` | CODE_REVIEW_PROMPT, FINDING_ADJUDICATION_PROMPT |
| `design_audit.py` | DESIGN_AUDIT_PROMPT, TASKS_AUDIT_PROMPT |
| `prototype.py` | PROTOTYPE_PROMPT |
| `explainer.py` | EXPLAINER_PROMPT, UNKNOWNS_REGEN_PROMPT |
| `docs_update.py` | DOCS_UPDATE_PROMPT |
| `respec.py` | RESPEC_PROMPT |
| `rubric_prompts.py` | RUBRIC_PROMPT |
| `plan_review.py` | PLAN_REVIEW_PROMPT |
| `interview.py` | QUESTIONER_PROMPT, ANSWERER_PROMPT, RESEARCH_DECISION_PROMPT, UNTRUSTED_NOTICE |
| `epic_split.py` | EPIC_SPLIT_PROMPT, EPIC_SPLIT_RETRY |
| `epic_merge.py` | MERGE_PROMPT |
| `findings.py` | TRIAGE_PROMPT, SPEC_DELTA_PROMPT, SELF_CHECK_PROMPT |
| `vendored/` | Prompt text borrowed from `anthropics/claude-plugins-official`'s `pr-review-toolkit` under Apache-2.0 — `reviewer_blocks.py` holds the blocks, `SOURCES.json` pins each one by blob SHA, `LICENSE` sits beside them. Never edit a block to fit a local house rule; `scripts/check_vendored_prompts.py` re-fetches every pin and reports drift, and it is a **run-it-before-a-release** script wired to nothing. |

## Testing

```bash
PYTHONPATH=. python -m pytest tests/ -xvs
```

The full suite runs in parallel with pytest-xdist (in the `dev` extra), as CI
runs it:

```bash
uv run --project ../../../../.. --package build-pipeline --extra dev python -m pytest tests/ -q -n auto
```

All tests mock LLM calls — no API keys needed. Tests cover:
- Config: tier detection, test command discovery, gate toggles
- State: checkpoint/resume, phase ordering, block tracking
- Models: review scoring, weighted averages, threshold enforcement
- Gates: all gate functions with pass/fail scenarios
- Change Manager: DAG resolution, archiving, delta spec merging
- Spec Generator: dependency ordering, resume, change type detection, design-audit feedback (one bounded repair + required re-audit, revision-guarded)
- TDD Engine: block parsing, context assembly, weak test patterns, agent selection
- Dependencies: manifest/import subtraction, false-positive suppression
- Prototype / Respec steps: write boundary, gate retry, one-pass regeneration, propose-only respec
- Docs steps: `DOCS_IMPACT:` parsing, the warn-only freshness gate, failed-writer propagation, and the refusal to stage an agent-reported path from outside the project
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

## Invariants (do not "simplify" these away)

- **A reference-doc filename in `_DEFAULT_REFERENCE_DOCS` must match a file the
  kit actually ships.** Resolution does no fuzzy matching and a miss is only an
  INFO line, so a rename on the kit side silently removes that doc from every
  build while the run still looks healthy — which is exactly what happened to
  the django and react entries between 2026-07 and 2026-08.
  `test_builtin_map_names_only_docs_the_kit_actually_ships` pins the names so
  the next rename breaks a test instead of a build.
- **Reducing an over-limit reference doc never cuts inside a section.** A
  truncated rule reads as a complete rule, and the spec-gen prompt has no tools
  with which to go check. `summarize_reference_doc` keeps whole sections and
  always emits an index of *all* of them, so an omitted section is known to
  exist rather than invisible.

- **Planning has one publisher.** Design-audit, prototype and plan-review
  feedback and task-audit repairs call `spec_generator.rewrite_plan`. It stages
  and validates the set, including placeholders and no-behaviour blocks,
  regenerates rubric and shape evidence, then publishes through a recovery
  journal. A pending journal or intervening edit cannot count as a coherent plan.
- **Evidence belongs to inputs.** `SpecGenState.steps` stores revision, outcome and
  repair attempts. Legacy repair allowances migrate into that ledger; legacy
  done booleans do not certify current inputs. Legacy implementation checkpoints
  without revision evidence refuse continuation while preserving code/block state. Explicit dependency invalidation replaces ordinal
  phase checks as proof. The phase still describes progress.
- **Bounded repair can fail.** Required review errors or remaining blocking
  findings leave resumable blocked evidence. Never log failure and mark it passed.
- **Ignore generated test reports.** Projects put reports in `.gitignore`.
  Gitignored files stay out of verification, clean-tree checks and commits;
  tracked files remain inputs. Commits never run test commands. Final verification
  names changed paths and tells users to ignore untracked reports.
- **Completion has one check.** `gates.final_verification` is used by standalone
  TDD, the full orchestrator after docs/respec/archive/contributors, and findings
  before publication. A failed writer requires verified rollback or build failure.
  Push rejects a changed fingerprint. `BuildState.result` derives the completion
  and publication view for adapters; it introduces no parallel state machine.
  A directory Git lists as one path (a submodule, or a repository nested
  untracked) is never read as a file: `_checkout_input` records its HEAD and a
  hash of its index and of every path it lists (tracked, and untracked not
  ignored), nested submodules included, with
  read-only git commands; an empty gitlink directory is `gitlink:uninitialized`,
  and a directory with files and no checkout raises, so the check is unavailable.
- **Recovery preserves two versions.** Stop records contain independent index
  entries and worktree fingerprints. Missing index evidence refuses discard.
  `save_wip_ref` uses isolated indexes and stash-shaped commits; it does not move
  HEAD or stage into the user's index. Divergent staging requires agent write-window
  evidence matching HEAD, index blobs and final working fingerprints. External
  edits invalidate that ownership. Commit failures raise; None means no changes.
- **Budget admission is inside the semaphore.** Charge successful or partial
  provider usage before releasing it. Nested TDD reuses planning spend; standalone
  entries load persisted totals before dispatch. Budget exhaustion propagates
  through advisory handlers. Charge before write-window evidence publication too.
  Cancellation without usage records an unknown call and incomplete process spend;
  capped admission blocks until --accept-unknown-spend explicitly accepts prior
  unknown calls. Preserve their records and lower-bound totals; later interruptions
  need fresh acceptance. Findings rounds share the run budget.
  Child-process spend has its own aggregate semantics.

- **Adjudication is a correctness requirement, not polish.** Reviewers run in
  fresh contexts, so they cannot see the decisions the build already made and
  roughly a quarter of what they raise is wrong. Findings are *proposals*;
  `_adjudicate_review_findings` (main model, full build context) decides. It
  fails **open** — an adjudicator error keeps every finding.
- **`adjudicate: false` DROPS findings, it does not auto-apply them.** The
  invariant is that nothing unjudged reaches a fix agent. Preserve it in any
  refactor.
- **Test integrity is enforced by hashing, not by prompting.** The SDK's
  allow-list is per-tool, not per-path, and Bash routes around it anyway — so
  a prompt instruction is a hint and `unchanged_tests_gate` is the
  enforcement. It runs at BOTH writable windows (after GREEN, and after every
  review-fix iteration), because the review-fix agent IS the implementer.
- **Every review round is kept, and the fix on pass is all-or-nothing.**
  `_run_integration_and_review` writes a round record (`review_rounds`) after
  each adjudication and commits each failing-score fix as it lands, so the
  record names its commit. A passing block's accepted Critical/Major/Minor
  findings get one fix pass (`code_review.fix_on_pass`, outside
  `MAX_REVIEW_ITERATIONS`): `_fix_on_pass` commits the block and takes HEAD as
  the checkpoint only on a clean tree, checks the budget, saves
  `BlockInfo.fix_pass` (`FixOnPassState`) to the checkpoint file, then
  `_run_fix_on_pass` runs the fix agent, the suite, `_enforce_test_integrity`
  and one more review. That review gets the round memory a later round gets
  (`open_state`'s open findings and rejected ones, `scope_diff=None` because
  the fix is not committed yet) and `plan_round` keeps every open finding it
  does not call `resolved`; with no usable record for the round the pass
  started from (`fp.round_path` is not the latest review record), every
  finding of the pass stays open. Findings it keeps go to
  `unfixed-findings.md` with their status. The fix is kept only when the suite is green and
  `review_score_gate` still passes; anything else goes back to the checkpoint
  through `_discard_keeping_records` (a `discard_to` that puts the round
  records back afterwards) and lands in `unfixed-findings.md`. On resume,
  `run_tdd_engine` sees `fix_pass` and calls `_resume_fix_on_pass`, which rolls
  back first and runs the pass again: never keep a half-applied fix. Before it
  rolls back, `_others_work_in_fix_pass` refuses any uncommitted change or
  commit the stopped pass did not leave: `_record_fix_pass_stop` records HEAD
  and the uncommitted paths (`stop_record.record`) as the pass reaches
  `tests` and `review`, and a pass stopped at `fix_agent` has no record, so
  any uncommitted change there is refused. The comparison is
  `stop_record.others_work`, the one check for every resume that keeps or
  discards: the findings resume calls it too, and later resumes must. A refusal marks the block FAILED and `resumable` with
  `fix_pass` kept, so the next run tries again. Otherwise the pass
  never fails the block, except when the rollback itself is refused.
- **From round 2, the review loop scopes after the reviewer, never in its
  prompt.** The reviewer is shown the open findings and asked for a status
  for each (a missing one is `cannot_verify`, never resolved); the
  adjudicator decides `duplicate_of` and `in_fix_scope` against the earlier
  rounds and `git diff <last head_sha> HEAD`; code then merges restatements
  into the open finding, and lists out-of-scope findings in
  `outside_fix_scope` as a label while still sending them to the fix agent
  (a bug found is fixed wherever it is). Every missing value fails open: not
  a duplicate, in scope. Round 1
  runs as it always did. The loop's last fix (`review_fix_escalated`, on the
  review model) gets `REVIEW_FIX_ESCALATION` with every finding's history and
  records its `RECURRING:` answer, recorded, not gated.
- **`SEARCHED:` is recorded, not gated.** Every review-fix brief ends with
  `prompts.implementation.REVIEW_FIX_SELF_CHECK`. `parse_searched` puts the
  section in the round record; a missing one is `searched: null` and a
  warning. Code cannot check that a search happened, so failing on it would
  only reward agents that write the section without searching.
- **`TEST CHANGE REQUIRED:` is an escape hatch, not an authorization.** It
  downgrades the gate to a flag, re-baselines the hashes (so one declared
  change cannot excuse every later silent one), and hands the reason to the
  reviewer as an unverified claim. The declaration is scoped to **one window**:
  each `_enforce_test_integrity` call is given only the report of the agent that
  wrote during that window, never the accumulated `block.implementer_report` —
  otherwise the re-baseline is defeated by a single implement-phase declaration
  authorizing every later review-fix mutation. The implement window's agents
  are the implementer and any integration fixers, so that call reads the
  implementer's report and each fixer's whole report. Every prompt for an
  agent that writes in a window with the hatch (the two implementer prompts,
  which also serve the review-fix and fix-on-pass agents, and both
  integration-fix prompts) carries `prompts.implementation.TEST_CHANGE_DECLARATION`,
  which is built from `gates.TEST_CHANGE_MARKER`, so the prompts and the gate
  cannot drift (`tests/test_test_change_marker.py`). An agent never told the
  marker cannot use it, and its correct fix to a wrong test fails the block.
  Because every such prompt shows the marker, `parse_test_change_claims`
  counts only a line that starts with it (`gates._TEST_CHANGE_LINE`: after a
  bullet, number or `>` prefix, a heading's `#`s or an inline-code backtick
  closed at the end of the line, and optional bold) and drops `<reason>`,
  `none`, `no` and empty values, so an echo or a sentence that mentions it
  declares nothing. A marker alone on its line takes the next non-blank line
  as its reason, so `unchanged_tests_gate` takes the window's reports as a
  list and parses each on its own: joined, a bare marker ending one report
  would take the next report's first line as its reason. The `SEARCHED:` and
  `SURPRISES:` slots stop at `_TEST_CHANGE_LINE`, so a declaration after them
  is never read into them.
- **A refactor window carries no test-change escape hatch.** Both refactor
  windows (per block, and the whole-change pass) call `unchanged_tests_gate`
  with an **empty report** — deliberately, so `TEST CHANGE REQUIRED:` cannot
  reach it. The hatch exists for an implementer that discovers a genuinely
  wrong test; a refactorer is behavior-preserving by definition, so any
  test-file change there is a failure. This is also why the windows are safe to
  be strict: the implementation is committed first, so a failed refactor is
  discarded (`git_ops.discard_to` back to the impl commit, which leaves
  buildme's bookkeeping in place) and the block keeps the code that was
  already green.
  A refactor must never turn a green block red.
- **A discard only ever moves backwards along the current history, and only
  inside the project.** `git_ops.discard_to` checks `merge-base --is-ancestor
  <sha> HEAD` and refuses otherwise. A reset will jump to any commit, and the
  refactorer holds `Bash` — so it can commit, move HEAD, or switch branches
  between the moment the impl sha was captured and the discard. Resetting
  unchecked would throw away whatever HEAD had actually reached: earlier
  blocks' commits, or under `--no-worktree` the user's own. Unknown ancestry
  (git unavailable) reads as "refuse", never as "safe to destroy" — the caller
  then treats the refactor as *not* reverted. The project may be a
  subdirectory of a larger repo, so the discard never resets the whole repo:
  when the project is a subdirectory it refuses if a commit in `<sha>..HEAD`
  changes a file outside it, then runs `reset --soft <sha>` (the branch moves,
  every file stays), `restore --source=<sha> --staged --worktree -- .` (the
  project's index entries and files go back to `<sha>`; everything outside
  keeps its content and staged state) and the bookkeeping-excluding `clean`.
  The soft reset goes first so that a stop before the restore leaves HEAD at
  `<sha>` with the dropped changes staged, which a resumed step's stop record
  can tell apart from someone else's work.
- **An agent's self-report decides what gets *committed*, never what gets
  *believed*.** `DOCS_IMPACT:` is the only pathspec the documentation commit
  uses (`_docs_paths` resolves it, confines it to the project, and requires a
  real file), because building `git add` argv out of unvalidated agent output is
  how a pathspec-magic string becomes a command. But the agent holds
  `Write`/`Edit` over the whole project, so the report is also checked *against*
  `git status`: anything it changed and did not name is named back to the user
  as a `DOCS_WARNING:`. Widening the commit to sweep those in would trade the
  first property for the second — report both, stage only what was declared.
- **An unavailable snapshot means "not checked", never "everything was
  deleted".** `_snapshot_test_files` returns `None` (distinct from `{}`) when
  Git or a file cannot be read. Both integrity windows block on `None`:
  unavailable evidence cannot authorize a required check.
- **Two test-path regexes, on purpose.** `_TEST_FILE_RE` is Python-only because
  it feeds the `def test_*` weak-test scan; `_TEST_PATH_RE` is
  language-agnostic because integrity hashing never parses source and must
  cover Swift/Go/TS repos or the gate silently no-ops there.
- **Confidence shrinks a score toward neutral; it never scales it.**
  Multiplying would make an unsure reviewer look *harsher* (score 2 at 40% →
  0.8, a hard critical fail), which inverts the meaning of low confidence.
- **Spec compliance is a hard floor.** No confidence weighting applies to
  "the spec behavior is missing" — that is a fact about the diff, not a
  judgment call.
- **Gate names must not start with `test`.** pytest collects any `test*`
  callable imported into a test module as a test case; that is why the test
  integrity gate is `unchanged_tests_gate`.
- **A panel must never be less reliable than one reviewer.** `run_code_review`
  gathers with `return_exceptions=True`, drops failed members with a warning,
  and raises when every member failed. Budget exhaustion and cancellation
  always propagate, even when another member returned. Without that, N members meant N×
  the chance of one exception marking the block FAILED — the feature would make
  the pipeline worse the more you invested in it.
- **`ReviewResult.passed` is the default-policy view; `review_score_gate` is
  authoritative.** Any call site holding a `BuildConfig` uses
  `passed_with(config.review_gate)`, or it reports a verdict the gate doesn't
  apply the moment `code_review.gate` is configured.
- **Failed calls are charged to the budget too.** Both `llm_call` and
  `agent_call` record `e.partial.usage` before propagating; a review that dies
  after a 150k-char prompt is exactly the spend the breaker exists to catch.
- **`budget.record()` must never raise.** Accounting cannot be allowed to
  break a build; admission checks immediately before provider dispatch stop new calls.
  Every `llm_call`/`agent_call` passes a `budget_label` so the stop can say
  which phase spent the money.
- **A reviewer's tools are read-only — `Read`, `Grep`, `Glob`, and nothing
  else.** This holds for `run_code_review`, `run_design_audit`, `run_tasks_audit`
  and `run_plan_review` alike. A reviewer holding `Write` or `Edit` would fix
  the thing it was asked to judge, which destroys the finding and the evidence
  for it at the same time, and it would route around the one-regeneration-pass
  rule that is the only bound on spec-rewriting spend.
  `test_no_review_step_can_write` asserts the shape of every one of those tool
  lists, so adding a writable tool breaks a test rather than a build. The turn
  cap (30) is part of the same bound: enough to open a neighbour and grep for a
  name the plan claims exists, not enough to wander.
- **PLAN_REVIEW requests at most one owner repair.** Model and deterministic
  findings share that bound; the required recheck blocks unresolved major
  findings. `steps.plan_review.repair_attempts` records repair admission; its
  revision-bound outcome records success. Revalidate both required audits after
  a repair, including any audit invalidated by a later review. `oversized-plan` proposes a split for the size
  gate and never silently restructures a plan.

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
│   ├── ticket.md                     — the ticket (fenced untrusted), when the input was one
│   ├── plan.md                       — a --plan-file, recorded
│   ├── interview.md                  — the interview, whichever mode ran it
│   ├── research.md                   — research content, or why none was needed
│   ├── split-proposal.md             — written when the size gate stops the build
│   ├── codebase-analysis.md          — what the repo already looks like
│   ├── proposal.md
│   ├── use-cases.md               — personas + the use cases they must observe
│   ├── design.md
│   ├── unknowns.md                   — explainer per new-to-this-project dependency
│   ├── tasks.md
│   ├── rubric.md
│   ├── prototype/                    — NOTES.md + the cheap shape proof
│   ├── implementation-notes.md       — agent SURPRISES/SPEC_IMPACT, appended live
│   ├── reviews/block-<NN>-round-<k>.json — one per review round of each block
│   ├── unfixed-findings.md           — accepted findings buildme left unfixed, with the reason
│   ├── respec.md                     — proposed spec delta (never auto-applied)
│   └── requirements/<capability>.md  — BDD scenarios (one per capability)
├── registry/<capability>.md          — main spec registry (merged from archives; a first baseline from spec-extract)
└── archive/<date>-<name>/            — archived changes
```

The hardcoded `ARTIFACT_DAG` constant defines the dependency graph. Templates and instructions are Python constants in `change_manager.py`.

`unknowns` sits between `design` and `tasks` (`tasks` requires
`["requirements", "design", "unknowns", "use-cases"]`), so the edge cases are on
disk while the task breakdown is written. `use-cases` hangs off `proposal` and
is required by `tasks` for the same reason: a use case is delivered by a block
or by nothing at all, and PLAN_REVIEW checks block coverage against it. It is
its own artifact rather than two more proposal sections because
`_extract_capabilities` reads backticked list items out of `proposal.md`'s
`## Capabilities` section, and persona names would land in that harvest. `codebase-analysis.md`,
`implementation-notes.md`, `respec.md`, `prototype/` and `.board.json` are
**not** DAG artifacts — they are written by their phases. All of them travel into `archive/<date>-<name>/`; only
`requirements/*.md` ever merge into `registry/`. The one other writer of `registry/` is
spec-extract's `--apply`, run by CODEBASE_ANALYSIS while the registry is empty (see Codebase Analysis below).

## Phases

`BuildPhase` (models.py) retains its legacy enum order for checkpoint
compatibility and progress display. Required planning decisions use outcomes
bound to their input revisions, not ordinal position. The execution order is:

```
INIT → BOOTSTRAP → INTERVIEW_DONE → RESEARCH → CODEBASE_ANALYSIS → EPIC_SPLIT
  → SPEC_GENERATION → PROTOTYPE → DESIGN_AUDIT → PLAN_REVIEW → REVIEW
  → TDD_BUILD → DOCS_UPDATE → RESPEC → PUBLISH → COMPLETE  (or FAILED)
```

**Exit code 3 and the size gate are not phases.** `interview: ticket` and a
`RESEARCH:NEEDED` decision stop at the phase that must rerun. The size gate
checks the final reviewed plan after prototype and audit repairs. It keeps
stopping while `split_required` is set until `--accept-size`; a changed plan
revision invalidates that acceptance.

`PLAN_REVIEW` validates the final tasks and rubric before implementation. It
maps to Planning and never moves the card. Old checkpoints load conservatively
(`tests/fixtures/build-state-pre-plan-review.json` covers the legacy shape):
missing revision evidence requires validation before implementation can begin.

`PROTOTYPE`, `RESPEC` and `PUBLISH` are the additions from the dark-factory
lifecycle plan. `DOCS_UPDATE` sits between the build and the respec proposal so
the documentation it writes is committed onto the same branch and lands in the
same PR as the code it describes. `PUBLISH` runs **after** the `--auto` archive
move, so the pushed branch carries the archived layout.

`CODEBASE_ANALYSIS` sits before `SPEC_GENERATION` because `design.md` and
`EPIC_SPLIT` read it. It is best-effort in three ways, all deliberate: skipped in
`only` mode (no specs are generated, so nothing would read it), skipped when
the project has no source files yet (explore agents reporting "(new
project)" is pure spend), and non-fatal on failure (the design falls back to
"(new project)", exactly the pre-phase behavior). Every skip logs its reason.

The same phase then runs spec-extract (`orchestrator._run_spec_extract_step`,
not a phase of its own, so the enum, the board and the resume fixtures are
unchanged): `uv run --directory ../../spec-extract/scripts python -m
spec_extract_pipeline run --repo <checkout top> --tree <sha> [--path <project
prefix>] --max-usd <spec_extract_max_usd> --trust-repo --apply --specs-dir
<absolute specs_dir>` through `git_ops._run_spec_extract_skill`. It is skipped,
with `SKIP phase=CODEBASE_ANALYSIS step=spec-extract reason=<r>`, in `only`
mode, in an epic's child build (`orchestrator._epic_child`: `--epic-child`,
which the coordinator's `new_argv` passes, or `epic.CHILD_ENV`, which its
`_spawn` sets: the parent ran spec-extract, a child with no dependency in the
epic's repository branches from the parent's registry commit, and siblings at
one commit would race for spec-extract's lock; a child in another repository
builds without a baseline), with
`spec_extract: false`, for an untrusted repo, for a project with no commit,
and, while no commit is saved, when `specs/registry/` already holds a `*.md`.
The commit is saved as `SpecGenState.spec_extract_sha` before the launch, and a
resume always uses it, so it reaches the same spec-extract run folder. A saved
commit with a saved report path means the analysis already ran, so
`_run_codebase_analysis_phase` skips it (`reason=analysed-before-spec-extract`):
a re-run of the step never pays for the analysis twice, and an analysis that
failed (no report) is asked again. A saved report path that is this change
dir's `codebase-analysis.md` and still exists skips it too
(`reason=report-already-written`): a FAILED build re-run in its own worktree
keeps the report and the design written against it. The not-analysed report
(`code_analysis.NOT_ANALYSED_HEADING`, no commit) is never reused: it asks for
a commit and a re-run, and that re-run analyses. The block checkpoints right after the
analysis, before the step, so a retried analysis's report path survives the
step's `return 1` or a stop. Exit 0 commits `specs/registry/` with
`commit_stage` and prints `SPEC_EXTRACT:BASELINE <n>`, with ` uncommitted`
after it when the registry still holds uncommitted files (no pipeline branch
under `--no-worktree`, or a failed commit); exit 5 (another run holds the
folder) prints `SPEC_EXTRACT:RUNNING` and the build returns 1 without
advancing; any other exit goes on without a baseline (`SPEC_EXTRACT:NOT_RUN`)
unless `stop_record.others_work(<registry>, None)` finds uncommitted changes
there, which also returns 1. The spend is outside `BuildBudget`, capped by
`--max-usd`; spec-extract prints `SPEND:<usd>:<tokens>` last, and
`SPEND_REPORTING` adds it to buildme's `SPEND:` line as it does a walkthrough
run's. It is not in an epic's `epic.max_usd`, which counts only the children's
records (`epic/spend.py`); only the parent runs it. `tests/conftest.py` refuses a real `spec_extract_pipeline` launch, and
clears an inherited `BUILDME_TRUST_REPO` and `BUILDME_EPIC_CHILD`. Its stop and
resume rows are S1-S7 in the reverse-specs plan, plus S8 (a re-run of the step
does not repeat the analysis); `tests/test_spec_extract_step.py` tests S1, S2,
S3, S5, S6 and S8. With a non-empty registry,
`artifact_context` lists its stems as `registry`, and the proposal prompt gets
`## Current behaviour (specs/registry)` with the registry's capability names
and the rule that a changed capability keeps its registry name;
`spec_steps._registry_section` appends the same heading to the requirements
prompt (the same-name file, and the MODIFIED/REMOVED/ADDED rule) and to the
design and tasks prompts (the files of the change's own capabilities), capped
at `REGISTRY_TEXT_LIMIT` (a file left out is named with its requirement names,
and names still over it are cut, `(cut at 60,000 characters)`, as is the
proposal's list);
an empty registry leaves every prompt unchanged.

`EPIC_SPLIT` sits after `CODEBASE_ANALYSIS`, whose report it reads, and before
`SPEC_GENERATION`, which a split build never reaches. It is the one phase
whose pointer counts as done: the orchestrator skips it when `state.phase` is
`EPIC_SPLIT` or later, when `state.epic_dir` is set, or when
`state.epic_single` is (saved with a one-change answer or a skip, so a FAILED build's
retry does not ask again), because re-entering would pay for the decision
again. With `state.epic_dir` set, every run goes to
`epic.phase.continue_epic` and returns. Old checkpoints load with it ahead
(`tests/fixtures/build-state-pre-epic-split.json`); FAILED or past it, with `state.epic_single` set (A.S14). Its stop and resume rows
are PR A's table in the plan; `tests/test_epic_stop_resume_a.py` tests the
ones where a resume could destroy work or pay twice.

`implementation-notes.md`, `respec.md`, `prototype/` and `.board.json` are not
DAG artifacts, and neither are the documents `DOCS_UPDATE` writes — README,
CHANGELOG and `docs/**` belong to the project, not to `specs/`, and are
committed by their phase with an explicit pathspec.
