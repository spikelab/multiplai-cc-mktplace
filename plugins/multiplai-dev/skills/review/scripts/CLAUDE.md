# review scripts — orientation

A pipeline of separate agents with gates written in Python between them. The
agents find, verify and merge duplicates; the gates decide what survives, by
re-reading the reviewed commit with git. No gate calls a model. The pipeline
proposes no fixes: the verifier writes each finding's `expected_behaviour`, one
sentence on what correct behaviour looks like, without code.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m review_pipeline --help
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m pytest tests/ -q
# CI form
cd plugins/multiplai-dev/skills/review/scripts && \
  uv run --project ../../../../.. --package review-pipeline --extra dev python -m pytest tests/ -q
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `review` (also `--tree`, `--dir`, `--trim` and `--paths` (with either), `--extra-finders [N]` (accepted, ignored, one notice), `--plan-only`, `--restart`, `--exclude <glob>` (change reviews only: `TargetSpec.exclude`, passed to `target.diff_text` and `build_target` as `exclude_pathspec`, recorded in `TargetInfo.excluded` / `excluded_files` and listed in the review's header), `--no-deps` and `--model` (every stage's model for the run, over `review.yaml` and `multiplai.conf`), which `resume` also takes), `batch`, `rollup`, `resume`, `post`, `assess-only`, `migrate`, `round-metrics` (dispatched before `setup_logging`, so it writes nothing). `default_out()` is `ledger.reviews_root()`. Calls `setup_logging` once for every other command. Owns the stdout contract and the exit codes (2 includes `RunInProgress`: another process holds the target's run lock; 4: a budget stop, or `InterruptedRun` from the ledger; 5: `VerifyIncomplete`, some findings have no verdict after three tries; `resume` asks only for those; 6: `sdk.UsageLimitError`, the Claude usage limit was hit, with the reset time and the target's `resume` command; a batch and `assess-only` start nothing after it). `--mode full|critical` on `review` and `batch` (`TargetSpec.mode`, a batch entry's `mode` over the flag). |
| `models.py` | Pipeline models. `Piece` (a piece's files, never its diff; a half cut at the turn limit has `split_from`, the dimensions it was cut for in `split_for`, and for a one-file half `hunks`, its [first, end) hunk indices; the cut piece lists its halves in `split_into`), `FinderReport` (a dimension or path finder's report and what convert made of it, findings and `needs`, in `ReviewState.finder_reports`), `ConvertedItem` and the convert call's `ConvertOutput` (items with `pre_existing`, and `needs`). `ReviewState.dimensions` is the dimensions a change review runs on every piece, stored by `prepare` and kept by `resume` (None in a checkpoint from before 0.106: the default set). `Finding.citations` has `min_length=1`; `Finding.id` is computed with the v1 rule, never taken from a model. `Finding.finders` lists every finder that reported it. `Finding.defect_class` and `Finding.other_locations` (0.95+, buildme's `ReviewFinding` names; a loose answer still parses through `locations_list`, which never raises: a value that is neither a string nor a list is empty, and only a list's strings are kept, and a checkpoint without them loads with empty values) are the kind of defect and every other `path:line` or `path:start-end` with the same defect, as the finder named them; `ConvertItem` carries them from a report. `STAGES` is the run order (target, explain, paths, find, verify, recheck, merge, repeats, assess, write, export, render, done; explain runs only with `explain: true`, paths on a tree review only with `--paths`, recheck only on a change review with an earlier round's open findings); `CoverageRecord` (stage, subject, level, reason, detail) goes in `ReviewState.coverage`, and `paths_enabled`, `paths_status`, `paths_failure` and `path_ids` hold the paths stage's choice and outcome; `Repeat` and `Assessment` hold the repeats and assess stages' results; `EarlierStatus` (an earlier finding's `earlier_id`, `lineage`, `round`, status, reason, citations at head, `error`) is the recheck agent's answer (`RecheckOutput`, where an unknown status word reads as `cannot_verify`) once gated, kept by earlier id in `ReviewState.recheck_answers`, and the final list is `ReviewState.earlier_status`; `ReviewState.lineage` maps a finding id to its first appearance's; `EARLIER_STATUSES` is the status words worst first and `worst_status` picks by it (buildme's `PRIOR_STATUSES` holds them best first, pinned by `test_recheck.py`); `ReviewState` maps the removed `prescribe`/`check_fix` stages of an old checkpoint to `verify`. `AgentCheck` (one agent call: stage, subject, given, calls, outcome, turns, cost, times, error, plus `findings` for a finder and `verdict` for a verifier) and `GateCheck` (one gate result) go in `ReviewState.checks` / `gate_checks`, empty in older checkpoints. `Need` (what the review could not get: `what`, `blocks` a finding id or `"review"`, `cause`, `command`, `source`) is kept in `state.needs`; `NeedAsk` is the shape an agent answers in (`Verdict.needs`, `FinderOutput.needs`), with an unknown `cause` read as `no-access`. `Verdict.impact` is one of `IMPACTS` (unknown → "", as `topic`); the verifier answers in `VerifierAnswer`, which fails the parse when a non-refuted answer has no impact or no topic, so the SDK re-asks — old checkpoints without impact still load as `Verdict`. `ReviewState.runs` gets one `RunStart` (`producer`, `session_id`, `started_at`) per run of the round, review or resume, appended by `orchestrator.run_state` before its first save and never rewritten; review-viewer's export reads it to find the sessions' transcripts. `ReviewState.mode` is `full` or `critical`, set by `prepare` and kept by `resume`; `ReviewState.trim` (a tree review's `--trim`) the same, and `ReviewState.extra_finders` (true only when the option was given and the change has no earlier finished round, `rounds.finished_rounds` 0). `section_of(topic, finders)` puts a finding only the trim finder reported (`is_trim`) in `trim`, `tests` and `docs` in their own sections, and everything else, no topic included, in `code`. |
| `target.py` | Resolves `--branch` / `--pr` / `--range` / `--tree` to shas; a slug starts with `slug_repo_name()`, `<owner>-<repo>` from the `origin` URL (`origin_name()`), or the directory name (logged) without one, so every checkout and worktree of a repo shares one directory (review-viewer's `gitdata` restates the rule); writes `diff.patch`, `commits.txt`, `files.txt` (and `skipped.txt` for a tree); a tree's base is `EMPTY_TREE`, its repository is the top of the working tree (`tree_root()`: a subdirectory `--repo` becomes the reviewed path, `--path` joined below it) and `tree_files()` lists its files less the skipped ones (submodules included); `import_dir()` copies a `--dir` source into its target directory's `source/` and commits it there; `target_gate`; `snapshot_head()` (`git archive` of head into the target directory's `tree/`); `snapshot_base()` (the files the explain stage reads at base, into its `base-tree/`). The target directory is `<out>/<repo>/<object>/`: `object_name()` (`pr-<n>`, `branch-<ref>`, `range-<a>..<b>`, `tree`, `tree-<path>`), `checkout_repo_part()` (the repo name from `origin_owner_repo()`, without the owner; `repo_part()` renames `legacy` and `.locks` to `<name>@<owner>`), `dir_object()` for `--dir` (`<basename>`, `dir-<basename>-<6 hex>`). `is_review_folder()`, `review_children()` and `object_folders()` say where reviews are under `<out>` by position, not by name alone (a repo may be called `tree`; an `<out>` that is itself a review folder has no review in its own `rounds/`, `viewer/` or snapshots); review-viewer's `gitdata` restates them. The slug does not change. For `--pr`, also the PR title and body and `branch_rules()` (the base branch's GitHub rules; `[]` when GitHub reports none, `None` with a warning when `gh` or the API fails). A failed rules lookup, and a `gh pr view` that returns an empty title or base branch, also become a `lookup-failed` `Need` with the command a person would run (`build_target(needs=)`). Fixed argv, no shell, no checkout. |
| `deps.py` | The git dependencies' source in the snapshot. `locked_git_deps` reads every `uv.lock` at head (`git show`, `tomllib`) and returns a `Dep(name, url, commit)` per git-sourced package, once per (url, commit). `materialize` keeps a bare partial clone per URL under `cache_root()` (`skill_state_dir("review")/deps/<host>/<path>.git`; only `ALLOWED_SCHEMES`, `https` and `ssh`, tests add `file`), holds `<entry>.lock` while fetching (up to `LOCK_WAIT_S`, 300 s, never broken), fetches a missing commit once and re-clones a broken entry once per run, `git archive`s the commit and extracts it with the data filter (a link leaving the folder is left out) into a temporary folder renamed to `.review-deps/<name>@<commit12>/`, skips one over `deps_max_mb`, and writes `.manifest.json` last. `ensure` (called by the orchestrator after each `snapshot_head`) reuses a manifest on the same head and dependency list whose `ok` folders all exist, else removes `.review-deps/` and materializes again; with `deps` off it does nothing. `needs` turns each dependency not extracted into a `Need`. The orchestrator puts the `ok` entries on `TargetInfo.deps` for the gates and the prompts. Its stop-and-resume rows are tested one per case in `tests/test_deps_resume.py`. |
| `gates.py` | `citation_gate`, `finding_gate`, `verdict_gate`, `earlier_status_gate`, `locations_gate` (with `gated_locations`, which drops each failing location and keeps the finding), `need_gate`, and `reason_kind` (which rule a reason names; the only gate text that reaches logs and `checks.json`). `citation_gate` calls `multiplai_core.citations.check_quote` and renames its "path not at commit" to "path not at head"; `file_at_head`/`lines_at_head` wrap core `file_at`/`lines_at`. The first four take `TargetInfo` and return `GateResult`; `gated_need()` applies `need_gate` and blanks a failing command. A web citation (`Citation.is_web`) is skipped, not checked: it may not be first, and it does not confirm. A dependency citation (`Citation.is_dependency`, a path under `.review-deps/`) is checked with `check_quote` against its `TargetInfo.deps` entry's clone and commit (`dependency not in manifest` when none matches), and may not be first either (`first citation is a dependency`). |
| `sdk.py` | `recording()`: a ContextVar block in which `_run` adds each attempt's tool calls (`multiplai_core.ToolCall`), turns and cost to a `CallRecord`, the re-ask included; the stages open one around each call. `agent_call_text` (the dimension and path finders' report call): the same trust gate, budget check and recording, the answer returned as text with no parsing and no re-ask. Every call waits in `window.admit` after the budget check and leaves it when it ends; `WindowFullError` (a `UsageLimitError`) is `admit`'s stop. A run that ends at the turn limit raises `MaxTurnsError`, matched by `MAX_TURNS_TEXT` in core's `reason` or `stderr_tail` (core keeps no result subtype). `agent_call_structured`: trust gate, an allow-list with its complement denied (`Read`/`Grep`/`Glob` for every stage, plus `WebFetch`/`WebSearch` for finders and verifiers), one re-ask on a bad answer: a no-tools, one-turn reformat of the text returned, or a re-run of the prompt when the run itself failed. A hit Claude usage limit (multiplai-core's `is_usage_limit`) becomes `UsageLimitError`, an `AgentCallError` that is never re-asked; every stage lets it escape, as it does `RepoTrustError`, and `orchestrator._run_stages` saves the checkpoint as for a budget stop. `parse_answer` and `reformat_prompt` come from `multiplai_core.structured` (`parse_answer` says so when an answer holds no JSON at all); the call loop and its retry policy are this module's. The only caller of `run_agent`. |
| `window.py` | The usage-window gate. `read()` asks `USAGE_URL` (`/api/oauth/usage`, internal) with the OAuth token of `$CLAUDE_CONFIG_DIR/.credentials.json` for the `five_hour` and `seven_day` utilization (0-100), reset time and `remaining_dollars`, at most once per `READ_EVERY_S` (30), timeout `READ_TIMEOUT_S` (5), None on any failure (`last_error` says why). The token goes into the one request only, never a log line. `Gate` (one per event loop) holds the calls in flight and their estimates (`estimate`: this run's ledger average per label, else `DEFAULT_ESTIMATES` from DolceBot PR #4's ledger), and `usd_per_point` per window, measured once utilization moved `MIN_POINTS` (2) in one window (`same_reset`: reset times within `SAME_RESET_S`, 60 s, since the endpoint's moves by under a second between reads) and preferred over `remaining_dollars`; `WINDOW` lines go to `progress.log` (`progress_line`). `admit` waits while the calls in flight plus this one pass `HEADROOM` (0.9) of the tightest window, raises `sdk.WindowFullError` when none is in flight, and admits every call (one NOTICE) when the endpoint cannot be read. |
| `checks.py` | `add_coverage` (appends a `CoverageRecord` to `state.coverage` and, for an `error` one, its `ERROR_LINES` line to `state.errors`). Pure functions for the record of what was checked: `summarise_call` (a tool call as `{tool, target, detail}`, repo-relative, inputs only), `diff_hunks`, `seen` (`read`/`searched`/`diff`/`prompt`/`fetched`/`not-seen` for a citation, from the agent's calls and the lines its prompt held: the diff, or a verifier's finding citations), `marked_citation` (adds `gate` and `seen`), `prompt_labels` (the `given` list). |
| `budget.py` | `ReviewBudget`, a `multiplai_core.budget.SpendLedger` with review's $150 default ceiling; one ledger per target in core's `ContextVar` (a batch runs targets concurrently) and the circuit breaker. `by_label` is USD per stage label; `by_stage` holds calls, the four token counts, cost and `no_usage_calls` per label. A ledger saved without `by_stage` still loads. |
| `timings.py` | `state.timings`: intervals per key (`run`, each stage, `find:<dimension>`). A resume adds an interval; the `run` interval's end moves forward at each checkpoint. `wall_seconds` sums the closed ones. |
| `config.py` | `review.yaml` > `multiplai.conf` > session model, for per-stage models, effort, concurrency (`DEFAULT_CONCURRENCY`, 16), and the `explain` and `find_with_facts` switches (both default false; `find_with_facts` needs `explain`), `trim` (default true), `dimensions` (`_dimensions`: a list of names from `DIMENSIONS` and `trim`; a list without `trim` turns trim off, one naming it turns it on; without the key a change review leaves out `CHANGE_DEFAULT_OFF`, `tests` and `trim`, and `trim` defaults false; an unknown name raises `ConfigError`, which `__main__.load_config` turns into exit 2). `extra_finders` is accepted with any value and ignored; `ReviewConfig.extra_finders` says only that the key is present, for `orchestrator.extra_finders_notice`. `TRIM_DIMENSION` is not in `DIMENSIONS`: `stages/find.finder_dimensions` adds it, last, to a change review unless `trim` is off and to a tree review only with `ReviewState.trim`. |
| `chunks.py` | `split_pieces` cuts a change review's diff into `models.Piece`s for the dimension finders: whole files only, grouped by the nearest directory holding a `GROUP_MARKERS` file (looked up in `head_tree`, or `base_tree` for a deleted file), a module with its tests (`tested_module`), at most `PIECE_MAX_CHARS` (50 000, inclusive) characters of diff unless one file alone is larger, release metadata (`is_meta`) in meta pieces last, lockfile text left out (listed in `omitted`), binary files counted 0, no cap; `prepare` stores them in `state.pieces` from one `target.tree_paths` per side, and `piece_diff_text` rebuilds a piece's diff (a one-file half's: the file's header and its `hunks`, read by `file_hunks`). `split_piece` cuts a piece in two at the turn limit: between files near half its characters, keeping a module with its tests where it can (`_file_cut`), or for one file at the hunk boundary nearest its middle; None for a one-hunk file or an empty piece. `head_changed_lines` gives the paths stage each file's added lines at head. `header_path` reads a path from a `---`, `+++` or `rename` header (git's tab after a name holding a space dropped, C-quoting undone, a space at either end kept) for every diff reader here, `checks.diff_hunks` and `facts.diff_ranges` included. `diff_files` splits on `\n` only and reads each file's base and head paths and status. |
| `facts.py` | The explain stage's scope (`scope_for`: changed hunks at head widened by `CONTEXT_LINES` (20) and clipped to the file, removed lines at base, binary files left out; a tree review's whole files), its `resolver` for `code_facts`, and `facts.json` (`FactsFile`, `schema_text()`, `write_facts_file`). The contract is `../schema/facts.v1.schema.json`, generated from `FactsFile`; a test fails when the committed file differs. |
| `rounds.py` | Earlier rounds: `keep_round` copies the whole last round (every file and directory but `rounds/`, `viewer/`, the snapshots `tree/`, `base-tree/`, `assess-tree/`, and a `--dir` review's imported checkout `source/`, listed in `NOT_KEPT`) to `rounds/<NN>-<head[:12]>/` when a run starts, also on the same head (a rerun). The copy is made as `rounds/.partial-<name>/` and renamed when complete. `<NN>` is `round_number()`: one more than the round directories there, so it is also the current run's number, the ledger's `round`. A round already kept (same `generated_at` and `run.started_at`, `_round_key`) is not copied twice; nothing is overwritten, and `resume` keeps nothing. A run that stopped before export (its `review-state.json` has another run start than `findings.json`, or there is no `findings.json`) is kept too, without the earlier round's exported files (`EXPORTED`), once (`_state_kept_already` compares the state byte for byte). Rounds kept before multiplai-dev 0.46 are named `<sha12>/` and `<sha12>-2/`; they are still read and counted. A fresh `review` (not `--plan-only`) then deletes the top-level `facts.json`, so a run with explain off never leaves the last round's facts beside its findings. `round_name` gives the round id `post` puts in its markers: the head sha cut to 12 with `-k` for a head's k-th round (the v1 marker form, not the directory name). `load_rounds` reads them with the person's decisions from `viewer/decisions.json`, giving a round's copy of a finding a decision only when its `ts` is not after the next round's `generated_at` (for the last kept round, the top-level `findings.json` when it is the current head's and not itself a kept round), because decisions are keyed by id alone. A copy with no decision of its own takes the rejection its `repeat` assessment recorded, so a reworded repeat is matched by id in the next round. `keep_round` also skips every other top-level directory (a review an older version nested in this one) and the four rollup files (`ROLLUP_FILES`). `folder_target` reads whose a directory is (`review-state.json`, else `findings.json`); `out_root` gives the `<out>` a directory was written under (its parent when it is named by its own slug, as 0.46 and earlier named them, else its grandparent), which `resume`, `assess-only` and the history walk use. `history_elsewhere` finds the same target's rounds kept before 0.46 under a checkout-named slug (`<dirname>--pr-<n>`, under `<out>`, in `legacy/` or in the old `INBOX/reviews`) by the slug's part after the repository and the file's own `origin`, walking `<out>` by position (`_walked`) and the target's own directory for a 0.42 review nested in it, and always reads the target's 0.46 directory, named by its slug; the repeats stage passes them to `load_rounds`, which reads each round once and the decisions beside them, the target's own decisions winning. `_read_rounds` is that reading and the decision rule, shared with `open_findings`: the recheck stage's open set, from the latest kept round with a `findings.json` only, its `render.listed` findings in its own mode plus its `earlier` entries not `resolved` (each `OpenFinding` with the row of its latest copy and the head of the round it was listed in), less the rejected ones. `kept_findings_files` gives every kept round's `findings.json` for `render`'s Recurring section. |
| `round_metrics.py` | `round-metrics`, read-only: per `target.object_folders()` folder, its rounds (`rounds._round_dirs` with a `findings.json`, plus the top-level one unless `rounds._kept_already`; kept runs with none are counted as left out), sorted by `generated_at`; per round `render.listed` in its own mode and the `repeat`, `still-open` (among listed) and `low-value` labels; for each listed finding from round 2 on, `line_origin` of its first repo citation (not web, not `.review-deps/`): `git blame -L <line>,<line> --porcelain <round head>` then `git merge-base --is-ancestor <blamed> <round-1 head>`, exit 0 `present at first review`, 1 `written since`, anything else or a missing repository `unknown`, no repo citation `no line`. The rule is copied from buildme's `review_metrics.py`, not imported. A round whose `repo_path` is gone uses the first `--repo` checkout whose `origin` names the same owner/repo (`target.origin_owner_repo`). `render` prints a table per target and the totals line. No model, no network, no file written. |
| `ledger.py` | `data_dir()` / `reviews_root()` (`get_paths().data_dir / "reviews"`, the default output directory) and the append-only `<data_dir>/reviews/ledger.jsonl`: `started` and `finished` lines written by `orchestrator.run_state` around every run (review, resume, each batch target; `stopped` is `null`, `budget`, `verify-incomplete`, `usage-limit` or `failed: <error>`), `interrupted()` (the last line for a `target_dir` and `head_sha` is `started`), and `append()`, which opens with `O_APPEND` and never raises. Tests point `data_dir` at `tmp_path` (conftest). |
| `runlock.py` | `hold(target_dir)`: an exclusive non-blocking `flock` on `<out>/<repo>/.locks/<object>.lock` (`lock_path`: beside the target directory), held by `review`, `resume`, each batch target and `--plan-only` from before the ledger check (before the `--dir` copy) until the run ends, so a running review is never read as interrupted or written over by a second run; the kernel drops it when the process dies. `Locked` names the holder's pid, host and start time, which the file holds; the orchestrator raises it as `RunInProgress` (exit 2). A filesystem without `flock` logs a warning and runs unlocked. |
| `migrate.py` | `migrate --from <dir>`: each directory under `<dir>` copied whole to `<out>/legacy/<name>/` (as `.partial-<name>` first), one `migrated` ledger line per `findings.json` at any depth with `migrated_from`; each source gets a `copying` line in `legacy/sources.jsonl` before its copy and a `done` line after its ledger lines; a `done` source is skipped, and one left at `copying` is finished under the name it was given (copied if missing, only the ledger lines not already in the ledger appended); a name another source already took gets `~<6 hex of the source path>`; the source is only read. |
| `incremental.py` | The scope of a round (`decide`, called once by `orchestrator.prepare` right after `keep_round`; `ReviewState.scope`, a `models.Scope`, reused by `resume`). `incremental` needs a PR or branch target, a kept round with a `findings.json` (`latest_finished_round`, by `generated_at`), its `diff.patch` and loadable `review-state.json`, the same mode, a different head, no `--full` (`TargetSpec.full`) and `ReviewConfig.incremental` (`review.yaml` `incremental`); else `full` with `reason`. `normalised_patches` drops `index` lines and the numbers of `@@` headers; `changed_files` lists the paths whose patch differs or is new. Of `rounds.open_findings`, each finding of the kept round's state whose file is still in the change and none of whose repo citations is a re-reviewed file is carried (`Carried`, `apply`): copied into `state.findings` with `carried_from`, its verdict, write answer, original severity and lineage. `prepare` builds the pieces from `limit_diff` and `_run_stages` gives the agents the limited diff (`diff.patch` keeps the whole change); `run_find` keeps the carried findings ahead of what it found; `recheck.open_set` leaves them out, and `left_the_change` resolves an earlier finding whose file left the change with no call; `assess.reconcile_earlier` keeps a carried finding's lineage. With nothing to re-review (`nothing_changed`), `orchestrator.NOTHING_CHANGED_SKIPS` are skipped, recheck runs, and the reconcile runs before export. `export` writes `carried_from` and the top-level `scope`; `render.scope_line` the header line. Tests: `tests/test_incremental.py`; `tests/test_resume_rows.py` stops a run at each stop row of the review-cost plan (S1–S7, S11), resumes it, and compares the state, `findings.json` and the agent call count with a run that never stopped. |
| `costs.py` | Arithmetic only: `estimate` (each piece by `Piece.chars` once per dimension that runs on it, its halves for a dimension it was cut for, paths at the cap of 21 until mapped; `Estimate.calls` is a report and a convert per key), `remaining` (resume: unstored keys in full, unconverted ones at `CONVERT_FRACTION`), `cost_lines` (the stderr estimate, the WARNING at `OVERHEAD_FACTOR` x estimate >= budget left, the default-budget NOTICE), `kept_line` (the budget STOPPED message's last line). `orchestrator.print_cost_lines` prints them in `plan_only`, `review` (after `prepare`) and `resume` (WARNING only); `config.max_cost_source` says whether `--max-cost-usd` was given. |
| `linked_docs.py` | `linked_docs`: the Markdown docs the conventions finder's `CLAUDE.md` / `coding-standards.md` files link (one level, at head then base-only links, read with git at head or else base, binding first, whole within what is left of `CONVENTIONS_MAX_CHARS`, each `fenced`); skips are `conventions` coverage records, and a doc skipped for length is named in the prompt by `not_included_line`. `stages/find.conventions_blocks` calls it and adds `binding_instruction` before the first doc. |
| `paths_io.py` | The only reader of `paths.v1`: `assign_paths` (after merge, in `stages/merge.py`: each finding's `paths`, the ids of the paths one of its citations contains an `anchors` line of, `.review-deps/` citations ignored, in `paths.json` order, None for none; `export._finding` writes the key only when non-empty), `views` (each path as a `PathView` for its finder's prompt, a repeated id kept once), `load_paths` (the run's `paths.json`, or None unless `state.paths_status` is `ok` or `empty`), `classify_drop` / `classify_broken` (a `PathDrop` or a cut `TracedPath` as a `CoverageRecord`: scoping drops `info`, a `trace` drop or a cut trace `error`). |
| `stages/` | `paths` (every change review, a tree review only with `--paths`, before find: `code_facts.map_paths` over `chunks.head_changed_lines` or a tree's every line, cap 21, trace limit 30, answers under `paths-cache/`, result in `paths.json` and `state.path_ids`; a failure is recorded with `checks.add_coverage` and find still runs), `explain` (optional, before find: `code_facts` over the scope, one re-ask for files with no fact, answers in `explain_answers`, facts in `state.facts` and `facts.json`), `find` (the path finders, then every dimension on every piece: see "The dimension and path finders" below), `verify`, `recheck` (see "The recheck stage" below), `merge`, `repeats`, `assess`. Each `run_<stage>(state, ctx)` returns at once when the state is past it and skips items it already has. Each stores an agent's answer in the state as it returns (`finder_results`, `verdicts`, `recheck_answers`, `merge_answers`, `repeats`, `assess_answer`), with its `AgentCheck` beside it, and `explain`, `find`, `verify`, `recheck` and `merge` then save the checkpoint (`stages.save`, which also writes the spend and moves the `run` interval's end), so a budget stop or a kill keeps both and a resume records no call twice. `find` makes each finding's `other_locations` repo-relative, puts every finding not deduped through `gated_locations` (one `locations_gate dropped …` line in `state.errors` per dropped location), sets each returned finding's fate and records a `GateCheck` per gated finding; `verify` asks no verifier about a non-trim finding the finder rated LOW unless `verify_low` is on (`ReviewConfig.verify_low`, `review.yaml` `verify_low`, `--verify-low` on `review`, `batch` and `resume`): it stores `unverified_verdict` (status `unverified`, empty reason, no impact, topic `tests` for a `tests` finding) and saves before the first call, so a resume asks only for the rest; `VerifierAnswer` refuses `unverified`. It records a `GateCheck` per verdict, tries each call up to `VERIFY_TRIES` (3) times inside one `AgentCheck`, stores no stand-in verdict for a finding whose every try failed and raises `VerifyIncomplete` once the others are done (the orchestrator saves the checkpoint like a budget stop), and lowers a `breaks-*` impact on a `tests` finding to `correctness-only` with a line in `state.errors`; `merge` and `assess` mark merged findings in their finder's entry. `find.conventions_blocks` reads every `coding-standards.md` on the path from the root to each changed file's directory, then every `CLAUDE.md` on the same path, so the `CONVENTIONS_MAX_CHARS` cap skips `CLAUDE.md` text first; it returns each path with whether it fit, and `conventions_chain` joins the text. |
| `prompts/` | One module per stage; `find.build_report` (one dimension on one piece: its files and whole diff, never cut, ownership not reading, `REVIEW_BRIEF` for `diff-bugs` and `DIMENSION_TASKS` for the others, every severity and advice, `Pre-existing: yes` and `Could not check:`, the plain-text report format and `NO FINDINGS`; `find.build` is a tree review's JSON prompt only) and `convert.build_convert` (one item per defect or advice, none judged, exact quotes, `pre_existing` and `needs`) for the dimension finders, `path.build_path` (the whole path inside one fence, the end-to-end and changed-line-first instructions) for the path finders, all fencing their text with `untrusted.fenced` (`multiplai_core.untrusted.defang` with markdown fences kept, so quotes stay exact); `find.facts_block` lists the explain stage's facts with their ids and citation ids, cut at `FACTS_MAX_CHARS`, only with `find_with_facts` (a tree group gets the facts citing its files, `stages/find.group_facts`). Shared blocks in `__init__.py`: `workspace_block(web=)`, `description_block` (PR title and body inside an `<untrusted-content>` fence, as claims to check), `settings_block` (the base branch's rules, and the sentence "a red run can be merged" when no `required_status_checks` rule exists). |
| `export.py` | `ReviewState` → `findings.json` v1, key by key (the contract rejects unknown keys), with the optional `verifier_citations`. Merged-away findings and `Finding.finders` are not exported; the survivor's optional `absorbed` (0.94+, from `state.merged`, following a chain of merges to its end) lists each merged-away finding's id and claim. The optional `defect_class` and `other_locations` (0.95+) are written only when not empty. The optional `lineage` (0.94+, from `state.lineage`) is written only when it differs from the id, and the top-level `earlier` (0.94+, `state.earlier_status`) only when there are some. A shown finding's `Assessment` is exported as the optional `assessment` object. `write_checks_file` writes `checks.json` (`../../review-viewer/schema/checks.v1.schema.json`): agents ordered by `started_at`, then gate results. `run_record` builds the optional `run` object from the final state only: the ledger, `state.timings`, `state.run_config` and the counts (with `repeats` and `low_value` from `state.assessments`). `needs` is written only when there are some: top level, and on each finding it blocks; `exported_needs()` moves a need from a merged-away finding to the survivor. `need_commands()` groups the needs by normalised command (`N1`, … first-seen; `why` the shortest `what`; `blocks` the finding ids, `["review"]` when none), written as top-level `need_commands`; a need with no command is `unchecked` on its finding, or, blocking the review, a `no-command` coverage record (`need_coverage()`, called before each export). |
| `render.py` | `not_reviewed` (per dimension, the files no converted key of that dimension covers, with why, `checks.failure_kind`; then failed path ids) and `not_reviewed_section` (`## Not reviewed`, after the header lines of the review and the summary; nothing when every key converted); `not_reviewed_count` ends the `DONE` line. `review-<slug>.md` (with `render_paths_section`: the `## Paths` section before the appendix, from `paths_io.load_paths` and the findings rows only, every `paths.json` value through `inline_code`, then `gap_line`), the short `summary-<slug>.md` the session pastes into chat, `checks-<slug>.md` (`render_checks`: the checklist, then one section per agent), and the `<SEV>-only.md` rollups, each written to a `.tmp` file and renamed into place (`_write`), all from the exported dicts (by default each target's latest round once: a 0.46 directory named by the slug alone is left out when a `<out>/<repo>/<object>/` holds its slug, `_latest_rounds`). `class_line` (`class: <defect_class>; also at: <locations>`, the parts present: buildme's `class_line` wording) goes under each finding's heading, and `post` puts the same line under the claim of each inline comment and review-body item. `listed(findings, mode)` is the one rule for what is listed and counted (confirmed, unverifiable or unverified, not `repeat`, `low-value` or `important`, and in critical mode `impact` in `CRITICAL_IMPACTS`); `not_listed` goes to the review's appendix; `post.select` without decisions uses it too. `unverified` findings are listed and counted in their section's count, but rendered after the appendix, in the review's last section `## Not verified (LOW)` (`UNVERIFIED_TITLE`). Findings are listed in Code, Tests, Docs and Trim sections (`section`, `SECTION_TITLES`; Trim carries `SECTION_NOTES`, its advisory note, in the review, the summary and the rollups), counted per section by `count_line`, which the header, the summary and the orchestrator's `review finished:` line share. Both cost lines read `run` (`cost_line`). `write_runs` writes `runs.jsonl`: every target's latest round and every round under `rounds/`, one line per run (deduplicated by slug, head, `generated_at` and `run.started_at`), sorted by `generated_at`. `need_lines()` writes the **Needs you** lines (one per `need_commands` entry) in the review and the summary, `answers_lines()` the same list at the end of the run's terminal output with the `answers` command; each finding shows its `unchecked` entries as **Not checked:** and the ids of the commands bearing on it, the review lists the `no-command` coverage records under **Not checked**, and the summary counts the commands it leaves out. `render_earlier_sections` writes `## Findings from earlier rounds` (each `earlier` status with its citations, the claim looked up in the kept rounds) and `## Recurring` (`recurring`: a lineage open, by `open_lineages`, in `RECURRING_ROUNDS` (3) or more of the kept rounds and this one, computed in code, no fix proposed) after the findings sections; `render_review` takes the kept rounds as `earlier_rounds` (`write_review` reads them). `earlier_line` (`<new> new, <still> still open, <resolved> earlier resolved, <unknown> earlier cannot verify`, from `earlier_counts`) goes in the header and the summary on a round with earlier statuses or a `still-open` finding; `count_line` is unchanged. |
| `post.py` | One PR review (`gh api -X POST .../pulls/<n>/reviews`, event `COMMENT`). `select`: with `--decisions`, every shown finding whose decision is `accept`; without it, what `render.listed` keeps for the review's mode, at the severities `POSTED_SEVERITIES_BY_SECTION` gives its section. `commentable_lines` parses the base..head diff into head lines per path. It reads `pr_diff`, not `target.diff_text`: the same base as GitHub's PR diff and GitHub's hunks (`-U3`, `--inter-hunk-context=0`, myers, renames found, `a/` `b/` prefixes) whatever the user's `diff.*` settings, because a wider local hunk sends a comment GitHub refuses with 422 and the one POST then posts nothing; a finding with every line there is an inline comment (`side: RIGHT`), any other goes in the review body with its link. `posted_ids` reads the markers already on the PR (review comments and review bodies, `gh api --paginate`); a finding already marked, or a `still-open` one whose `earlier_id` is, is skipped. A failed read or POST raises `PostError` and posts nothing. |
| `pr_marker.py` | `format_marker`, `MARKER_RE`, `parse_markers`: the hidden `<!-- multiplai-review:finding id=… round=… v=1 -->` line on every posted finding. The format is pinned by a test and documented in `../SKILL.md` ("Marker"), where buildme-findings copies the regex from. |
| `orchestrator.py` | target → [explain] → paths → find → verify → recheck → merge → repeats → assess → write → export (`findings.json`, `checks.json`) → render, saving `review-state.json` after each; `prepare` chooses the target directory (`_choose_folder`: `<repo>/<object>`, then `<repo>@<owner>/<object>` when the directory names another remote, then `<object>-<6 hex>` when a branch or range directory names another ref; each locked before it is read, except that a directory another run holds is read without its lock and passed over when it names another remote or ref), checks it and, while it has no state, the 0.46 directory for an interrupted run, and keeps the earlier round first; `resume` (a checkpoint saved after assess and before export runs `assess.reconcile_earlier` before export, since the assess stage will not run again); `batch`; `assess_only` and `assess_report` for the `assess-only` command. |
| `answers.py` | `answers <target_dir>`: applies the outputs a person saved as `answers/N<k>.txt` for the review's `need_commands` to a finished review, under its run lock. A1 `read_outputs` (whole, at most `MAX_CHARS`; an id not in `need_commands`, a file over the limit, or one older than `state.runs[0]` refused), A2 `match` (a command naming findings keeps them; one no-tools call, `prompts/answers.py`, for `["review"]` commands, invented ids dropped; `AnswerMatch` with `before` statuses saved in `state.answer_matches`, the matched verdicts dropped in the same save), A3 `stages/verify.verify_one` with the fenced outputs as `evidence` and `keep_verdict`, then `settle_severity`, A4 `stages/write.write_findings` per changed shown finding (`state.answers_rewritten`), A5 export and `state.answers_applied`. Never runs find, merge, repeats or assess, so ids and decisions stay. Stop rows S1–S11 in `tests/test_answers.py`. |
| `state.py`, `progress.py` | Atomic checkpoint (`multiplai_core.checkpoint`, with review's file name and model); the tailable `progress.log` (`STARTED`, `STAGE`, `DONE`, `FAILED`), a `multiplai_core.progress.ProgressWriter` that adds review's `STARTED` line, which names `export.producer()` (skill and version) first. |

## Tree reviews

`--tree [COMMIT] [--path DIR]` and `--dir PATH` review code that is not a
change. `TargetInfo.kind` is `"tree"` and `base_sha` is git's empty tree
(`target.EMPTY_TREE`), which `findings.json` v1 accepts unchanged; review-viewer
knows a tree review by that base.

- `import_dir()` copies the directory (less `IMPORT_EXCLUDES` and any `.git`)
  into `<out>/<name>/dir-<name>-<hash>/source/`, not its `tree/`: `tree/` is the agents' snapshot,
  deleted at the end of a run, and the copy is the repository the review and
  the viewer read afterwards. The slug's name is `dir_name()`:
  `<basename>-<6 hex of the absolute path>`, so two directories with one
  basename never share a `source/`. A later run keeps the earlier commits: it
  replaces the copy's files, reuses HEAD when nothing changed, and otherwise
  commits on top, so the head an earlier `findings.json` names stays readable.
  `check_out_dir()` refuses an `--out` inside the source before `__main__`
  creates it. `review` (not `prepare`, which `--plan-only` also calls) deletes
  the last `progress.log` when a kept round in `rounds/` holds a copy, and
  otherwise moves it to `progress-<UTC time>.log`, which `keep_round` never copies.
- `stages/find.file_groups()` splits the files at `GROUP_MAX_CHARS` (100 000)
  and at each top-level directory below the reviewed path; a change review is
  one group, so its path is unchanged. Each dimension runs once per group and
  its result is stored in `finder_results` under `"<dimension>@<group index>"`.
  A change review's dimension finders are reports keyed `<dimension>@<piece id>`
  instead; a checkpoint's bare dimension keys from before 0.106 still load, and
  their findings are kept.
  Each call's `AgentCheck` has that key as its `subject`. Its timing goes
  under `find:<dimension>`, the budget label, and each call ends the
  interval it opened (groups of one dimension run at once), so that row's
  time is the sum over its groups.
  `history` does not run on a tree.
- `target.tree_root()` reads a tree from the top of the working tree: `git
  ls-tree` run in a subdirectory lists names relative to it, while `git show
  <sha>:<path>` and `git archive` read from the top, so a subdirectory
  `--repo` would make every citation fail. The subdirectory becomes the
  reviewed path, `--path` is joined below it, and a path that leaves the
  repository is refused. review-viewer's `gitdata.tree_path()` restates the
  rule, so `serve --tree --repo <subdir>` finds the same slug.
- `prompts/find.build(..., files=)` gives a tree finder the group's file list
  instead of the commits, description, settings, files and diff blocks, with
  the task worded from `DIMENSION_TASKS_TREE`. `FILE_RULE_TREE` says `file`
  is one of the listed files and any other file (a skipped one included) is
  `pre-existing`, which is what `finding_gate` lets through.
- `orchestrator.plan_only()` (`review --plan-only`) writes `plan.txt` and makes
  no agent call and no ledger.

## Why the agents read a snapshot

The gates check quotes against `git show <head_sha>:<path>`. The agents' tools
see a directory, and the repository's working tree is whatever is checked out.
So `snapshot_head()` extracts the head tree with `git archive` (which writes
nothing to the repo) and every agent runs with `cwd` there. Paths the agents
return are made repo-relative in `stages/__init__.py`. The snapshot is deleted
when the run finishes.

## The gates

| Gate | Passes when | On failure |
|---|---|---|
| `citation_gate` | the whitespace-normalised quote is inside lines `line_start..line_end` at head (for a `.review-deps/` path: at the dependency's pinned commit) | building block |
| `finding_gate` | every repo and dependency citation passes; the first citation is a repo citation; the file changed (unless `dimension == "pre-existing"`); severity is known | finding → `rejected` |
| `verdict_gate` | a `confirmed` verdict has at least one repo citation that passes (web citations do not count) | verdict → `unverifiable`, severity lowered one step |
| `earlier_status_gate` | an earlier finding's status other than `cannot_verify` has at least one repo citation that passes at head (web and dependency citations do not count) | status → `cannot_verify`, the rule in its reason and in a `GateCheck` |
| `locations_gate` | an `other_locations` entry is `path:line` or `path:start-end`, the path is at head, and every line is inside the file (split on `\n`, the empty string after a final newline not counted) | location dropped, a line in `state.errors`; the finding is kept |
| `write_gate` | a write answer's title is at most 12 words; no `path:line` in it or in `what_happens`; no sentence of `what_happens` over 50 words; none of `WRITE_BANNED`; every backticked name occurs in the finding's claim, scenario, expected behaviour, verifier reason or cited quotes | asked again once with the reasons, then dropped (`state.write_failed`, a line in `state.errors`); the claim is shown |
| `need_gate` | a need's command is one line under 300 characters, holds none of `;` `&` `\|` `>` `<` backtick `$(`, and is a read-only form of a CLI in `NEED_READ_VERBS` (its verb listed there; `gh api` and `curl` with no write method or body, `aws` describe/list/get, no SQL that writes) | command blanked; the need is kept |

`need_gate` is not a security boundary: neither the pipeline nor the session
runs a need's command (the session gives it to the user as `! <command>`). It
keeps each ask readable and safe to copy.

An `unverifiable` verdict lowers severity one step (`stages/verify.py`,
`unverifiable_severity`), but not below MEDIUM when a need blocks the
finding.

Gate function names must not start with `test` (pytest would collect them).

## The dimension and path finders: report, then convert

A change review's find stage is `stages/find._find_change`. It builds one
job per path in `state.path_ids` (read back with `paths_io.load_paths` and
`paths_io.views`, prompt `prompts/path.build_path`), keyed `path@<id>`, then,
through `PieceFinders`, one job per (piece, dimension) for each stored piece
with files (`top_pieces`) and each of `change_dimensions(state)`, keyed
`<dimension>@<piece id>`: largest piece first, all of a piece's dimensions
together. The shared concurrency bound starts them in that order, and a
budget stop launches nothing later. A dimension's prompt is
`prompts/find.build_report` with the piece's diff (`chunks.piece_diff_text`),
`conventions` alone getting the rules of that piece's files
(`PieceFinders.rules`, computed once per piece). Each job is `run_reported`:
an `sdk.agent_call_text` call (`sdk.FINDER_TOOLS` for a dimension,
`READ_ONLY` for a path, cwd the snapshot, label `find:<dimension>` or
`find:path`) whose text is stored as a `FinderReport` in
`state.finder_reports`, saved, and written atomically as
`report-<dimension>-<id>.md` or `report-path-<id>.md` (`write_report_file`)
before the convert call (`prompts/convert.build_convert`, `ConvertOutput`,
`READ_ONLY`, label `find:<kind>-convert`). `map_converted` turns its items
into `ConvertedItem`s (a null severity is LOW) and a `Finding` for each item
with a location, whose finder and dimension are the key's kind (dimension
`pre-existing` when the item says so); its `needs` go to the report. The two
calls are two `AgentCheck`s, `<key>` and `<key> convert`; the items, with
their kind, are on the convert one only (`AgentCheck.items`, exported in
`checks.json`), and `render.finder_rows` folds the convert into the key's
row. `run_find` then reads each key's findings with `_reported_result` and
puts them through one dedupe and `finding_gate`. A report of `NO FINDINGS`
(or nothing) is converted with no call. A failed report or convert is
recorded with `checks.add_coverage` (`finder-failed`, `convert-failed`) and
the key stays pending, so `resume` asks only for the missing call;
`checks.drop_coverage` removes the record once a later call succeeds. Only a
trust stop, a hit usage limit (the usage window's `WindowFullError`
included) or a budget stop leaves the stage. A fresh round starts with an
empty `finder_reports`, and `rounds.clear_stale_outputs` removes the last
round's report files after `keep_round` has copied them.

**The turn limit.** A report call that raises `sdk.MaxTurnsError` on a piece
that is not itself a half calls `PieceFinders.split`: `chunks.split_piece`
cuts the piece once (`split_into`, halves appended to `state.pieces` with the
next ids and `split_from`), the dimension is added to each half's
`split_for`, the state is saved, and the dimension is asked on the halves one
after the other. `covering(state, piece, dimension)` then gives the halves
in place of the piece for that dimension, which `keys`, `jobs`, `costs` and
`render.not_reviewed` all read. A half is never cut, and a piece
`split_piece` cannot cut (one hunk) stays failed.

**Old checkpoints** (before 0.106): `adopt_legacy_reports` renames a
converted `piece@<id>` report to `diff-bugs@<id>` (also writing it as
`report-diff-bugs-<id>.md`), bare dimension keys in `finder_results` keep
their findings, `orchestrator.kept_dimensions` sets the default dimensions,
and every `<dimension>@<id>` key still missing is asked. The resume rows are
tested one per case in `tests/test_resume_finders.py`.

## The merge stage

`find` drops a finding only when file, first line and the first 160 characters
of the normalised claim match (`dedupe_key`), and records the dropped copy's
finder in `finders`. Reworded duplicates get through, so `stages/merge.py`
runs after verify:

- `overlap_groups` takes the `confirmed`, `unverifiable` and `unverified` findings and
  chains every pair that `linked` accepts: their anchors are in one file and
  overlap or are at most `MERGE_LINE_GAP` (2) lines apart, or any repository
  citation of one overlaps (within the same gap) any repository citation of
  the other, in any file. Web citations never link. Groups of two or more go
  to one agent each.
- The agent returns `duplicate_sets` of ids. `usable_sets` keeps only ids in
  the group, each id once, sets of two or more.
- `merge_set` keeps one member (confirmed, then unverified, then unverifiable, then the most
  severe, then the first), so its id and verdict stand; it takes the highest severity in the
  set, the union of citations, of finders and of `other_locations` (the
  survivor's first), and the survivor's `defect_class`. For an
  all-unverifiable set, `original_severity` becomes the highest of the members'.
- Each finding merged away goes to `state.merged` with `into` and a reason.
  `render.py` lists them in the appendix.
- Answers are stored in `state.merge_answers` by `group_key` as they arrive,
  so a resume asks no group twice. A failed agent call stores an empty answer
  (the group stays unmerged) and adds a line to `state.errors`.

## The assess stage

Two stages after merge judge the findings as a set. Neither deletes a
finding or changes a verdict or a severity; both run on the merger's model.

- **Rounds.** A PR is reviewed into the same `<out>/<repo>/<object>/`, so
  `orchestrator.prepare` calls `rounds.keep_round` before a new run writes
  over the last round (after the ledger check for an interrupted run). `viewer/decisions.json` (review-viewer's, keyed by
  finding id) already persists across rounds and stays where it is.
- **`repeats`** (`stages/repeats.py`) builds the rejected list (findings in
  `rounds/*/findings.json` whose decision is `reject`), skips when it is
  empty, matches equal ids in Python, and sends the rest to one agent.
  `gate_matches` keeps a match only when the id is a shown finding not yet
  matched, `rejected_id` is on the list and `reason` is not empty; dropped
  ones go to `state.errors`. Matches go to `state.repeats`, and the ids asked
  about to `state.repeats_checked`, as the answer arrives. The finders never
  see the rejected list: a repeat is shown, labelled, not hidden.
- **`assess`** (`stages/assess.py`, rules in `prompts/assess.py`) labels the
  shown findings that are not repeats: `useful`, `still-open` or
  `low-value` (rules `context`, `covered`, `speculative`). Skipped with fewer
  than two such findings and no earlier rounds. `gate_assessments` turns any
  label that fails (unknown id, unknown label, an `earlier_id` not in
  `rounds/`, `still-open` without an accepted, deferred or undecided earlier finding,
  `low-value` naming no rule, a finding left out) into `useful` with a line
  in `state.errors`. Its `duplicate_sets` are first split by
  `anchored_sets` into the runs of members whose anchors lie within
  `MERGE_LINE_GAP` lines of each other in one file (`merge.anchors_near`,
  the anchor half of `linked`); a member in no run is dropped, with one log
  line per set. The agent sees every finding at once and set root bugs with
  weaker findings in other files, whose claim then hid them. The rest go
  through the merge stage's `apply_duplicate_sets` (`usable_sets`,
  `merge_set`); each finding merged away keeps its label on its `Merged`
  entry (`assessment`), and `carry_still_open` labels a `useful` or
  `low-value` survivor `still-open` against the earlier finding of the first
  absorbed `still-open` finding in `state.findings` order. The raw answer is
  stored in `state.assess_answer`, so a resume asks nothing twice; a failed
  call stores an empty answer and leaves every finding `useful`.
- Both labels end up in `state.assessments` (repeats as `repeat`), exported
  per finding as `assessment`.
- **Reconcile.** Once the labels are final, also when the stage is skipped,
  `reconcile_earlier` sets in code `state.lineage` (for a `still-open` or
  `repeat` finding, the `lineage` its earlier finding exported, else that
  finding's id; otherwise its own id) and `state.earlier_status`: for each
  finding of the recheck stage's open set, its stored answer, or
  `cannot_verify` with reason `not rechecked` when it has none (a checkpoint
  past recheck written before 0.94), made `still_present` (`worst_status`)
  with the finding's citations added when a finding of this round, or one
  merged into it (`absorbed_still_open`), is `still-open` against it. It
  reads only the state and the rounds, so running
  it again gives the same result.
- Neither stage's answers have been tuned against recorded decisions. Run
  `assess-only` over the saved reviews first (see `review/SKILL.md`), and
  only then change the prompt.

## The write stage

`stages/write.py` (prompt `prompts/write.py`) runs after assess and before
export. It restates each shown finding (`confirmed`, `unverifiable` or
`unverified`, not `repeat`) in plain words, for the reader of the review and
the viewer; nothing else in the pipeline reads its text.

- **Calls.** Findings sorted by file, at most `prompts.write.BATCH` (10) of
  one file per call, `allowed_tools=[]`, `cfg.merger_model`, budget label
  `write`, one `AgentCheck` (`stage="write"`) per call, bounded by
  `concurrency`. The prompt fences each finding's claim, failure scenario,
  expected behaviour, verifier reason and cited quotes with `untrusted.fenced`,
  and carries `WRITING_RULES`, a copy of the Clear Writing style's rules 2-8
  (the walkthrough's `EXPLAINER_RULES` less its citation, snippet and diagram
  lines), copied, not read at run time.
- **Gate.** `gates.write_gate` checks each answer. The failing ids are asked
  again once in one call with the reasons; an id that fails again goes to
  `state.write_failed` with a line in `state.errors`, and keeps no answer.
- **Output.** `state.write_answers` (id → `WriteAnswer`); `export` writes
  `title` and `what_happens` on a shown finding that has one. `claim` is never
  changed: repeats, recheck and the person's decisions read it.
- **Resume.** The state is saved after each call. A resume asks only for ids
  in neither `write_answers` nor `write_failed`. Tests: `test_stages.py`
  (`-k write`). Every other test gets its write calls answered in code by
  `conftest.canned_write`; a test marked `write_calls` uses its own fake.

## The recheck stage

`stages/recheck.py` (prompt `prompts/recheck.py`) runs after verify and before
merge, and gives each finding an earlier round left open a status at head.

- **The open set** is `rounds.open_findings` (see `rounds.py` above),
  computed again from disk by the stage and by the reconcile; it is never
  saved. Empty on a tree review and without a target directory, and the
  stage then makes no call.
- **One fresh agent per open finding** (`cfg.verifier_model`,
  `sdk.VERIFIER_TOOLS`, `cwd` the snapshot, up to `VERIFY_TRIES` tries inside
  one `sdk.recording()`, label `recheck`, one `AgentCheck` with
  `stage="recheck"` and the earlier id as `subject`). Its prompt holds the
  earlier finding (claim, failure scenario, citations, the round's head) and
  `git diff <that head>..<head> -- <the paths it cites>` (`changed_since`,
  run in the reviewed repository with fixed argv; a note in its place when
  git cannot), both through `untrusted.fenced`. It never holds this round's
  findings, so the finders and the recheck stay independent; the finders get
  nothing from earlier rounds (`test_find_prompt_ignores_rounds`).
- **Gate.** `earlier_status_gate` turns a status without a repo citation that
  reproduces at head into `cannot_verify`, recording the rule
  (`reason_kind`) in the reason and a `GateCheck` with
  `gate="earlier_status_gate"`.
- **Failures.** A finding whose every try failed is stored as
  `cannot_verify` with `error` set and a line in `state.errors`; the stage
  does not raise, because this round's findings still need to reach the
  review. A finding no kept round holds any more is `cannot_verify` with no
  call. A trust stop, a usage limit and a budget stop leave the stage with
  the answers already returned.
- **Resume.** Answers are stored by earlier id as they arrive, the first per
  id standing; a resume asks only for the open ids without one. Rows S1-S4
  and S9-S12 of the plan's stop-and-resume table are tested one per case in
  `tests/test_recheck.py` (`-k earlier_status_resume`).

## The trim finder and the `section` field

The `trim` finder (`config.TRIM_DIMENSION`) judges the tests and comments a
change adds or edits; its findings go to an advisory Trim section that never
blocks a merge. Its findings are kept apart from the others: `merge.linked`
never links one to a non-trim finding, `assess` never shows one to its agent,
and `verify` skips the `tests` impact cap for one (its impact rates what
removing the test risks), sets its severity to LOW once the stage ends, and
labels `important` (`important_assessments`, hidden like `low-value`) each
shown (confirmed or unverifiable) one about a test rated `breaks-*` or citing
a path under a `## Critical paths` glob in a `coding-standards.md`
(`stages/find.critical_paths`): a `docs` topic is a comment and never counts,
under a topic other than `tests` only a citation outside the finding's own
file is matched, and the findings merged into a trim finding (`state.merged`)
count as part of it. Both are recomputed from the verdicts whenever the stage
ends; `assess` recomputes the label too, since it rewrites `state.assessments`.
`find` still folds a word-for-word copy into another finder's finding (both
would have one id), which then is not trim. Its "every finder failed" check
counts only the other finders: a review whose every defect finder failed
raises even when `trim` answered.

**Invariant: four copies of the section rule must agree.** `models.section_of`,
`render.section`, buildme's `findings/decisions.py` `_section` and
review-viewer's `static/logic.js` `findingSection` each decide a finding's
section. `export.py` writes the answer on every finding as `section`, and every
reader takes that field when present; the topic rule (no topic is `code`) is
only the fallback for a `findings.json` written before 0.71. A trim finding
must never be read as `code`: buildme's epic merge blocks on Code HIGH/MEDIUM.

## Logging

`setup_logging("review-pipeline", packages=("review_pipeline", "code_facts"))`
writes `review-pipeline-YYYY-MM-DD.log` (one file per UTC day, never deleted
or truncated), holding every INFO+ record from those packages and
`multiplai_core`. `log_event("review", …)` fires `start`, `stage`,
`gate_reject`, `budget_stop`, `verify_incomplete`, `usage_limit`, `done`, `post`. No field
holds finding or prompt text: `gate_reject` records which rule fired
(`gates.reason_kind`), not the reason string. The record of what was checked goes to `checks.json` and
`checks-<slug>.md` only, never to a log line.

## Tests

`tests/fixture_repo.py` builds the DB2038 regression repository from
`tests/fixtures/settings_consumer_repo/{base,head}` with fixed dates, so its
shas are stable. Every agent call is monkeypatched; nothing here calls a model.
`test_export.py` validates against
`../../review-viewer/schema/findings.v1.schema.json`, `test_checks.py` validates
`checks.json` against `checks.v1.schema.json` beside it (its stage tests feed
the recorder the way `_run` does), and `test_models.py`
checks this package's `finding_id` against the ids in the viewer's own
fixture. The package never imports `review_viewer`.
