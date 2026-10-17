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
| `__main__.py` | CLI: `review` (also `--tree`, `--dir`, `--trim` and `--paths` (with either), `--extra-finders [N]` (accepted, ignored, one notice), `--plan-only`, `--restart`, `--no-deps`, which `resume` also takes), `batch`, `rollup`, `resume`, `post`, `assess-only`, `migrate`. `default_out()` is `ledger.reviews_root()`. Calls `setup_logging` once. Owns the stdout contract and the exit codes (2 includes `RunInProgress`: another process holds the target's run lock; 4: a budget stop, or `InterruptedRun` from the ledger; 5: `VerifyIncomplete`, some findings have no verdict after three tries; `resume` asks only for those). `--mode full|critical` on `review` and `batch` (`TargetSpec.mode`, a batch entry's `mode` over the flag). |
| `models.py` | Pipeline models. `Piece` (a piece's files, never its diff), `FinderReport` (a piece finder's report and what convert made of it, in `ReviewState.finder_reports`), `ConvertedItem` and the convert call's `ConvertOutput`. `Finding.citations` has `min_length=1`; `Finding.id` is computed with the v1 rule, never taken from a model. `Finding.finders` lists every finder that reported it. `STAGES` is the run order (target, explain, paths, find, verify, merge, repeats, assess, export, render, done; explain runs only with `explain: true`, paths on a tree review only with `--paths`); `CoverageRecord` (stage, subject, level, reason, detail) goes in `ReviewState.coverage`, and `paths_enabled`, `paths_status`, `paths_failure` and `path_ids` hold the paths stage's choice and outcome; `Repeat` and `Assessment` hold the repeats and assess stages' results; `ReviewState` maps the removed `prescribe`/`check_fix` stages of an old checkpoint to `verify`. `AgentCheck` (one agent call: stage, subject, given, calls, outcome, turns, cost, times, error, plus `findings` for a finder and `verdict` for a verifier) and `GateCheck` (one gate result) go in `ReviewState.checks` / `gate_checks`, empty in older checkpoints. `Need` (what the review could not get: `what`, `blocks` a finding id or `"review"`, `cause`, `command`, `source`) is kept in `state.needs`; `NeedAsk` is the shape an agent answers in (`Verdict.needs`, `FinderOutput.needs`), with an unknown `cause` read as `no-access`. `Verdict.impact` is one of `IMPACTS` (unknown → "", as `topic`); the verifier answers in `VerifierAnswer`, which fails the parse when a non-refuted answer has no impact or no topic, so the SDK re-asks — old checkpoints without impact still load as `Verdict`. `ReviewState.mode` is `full` or `critical`, set by `prepare` and kept by `resume`; `ReviewState.trim` (a tree review's `--trim`) the same, and `ReviewState.extra_finders` (true only when the option was given and the change has no earlier finished round, `rounds.finished_rounds` 0). `section_of(topic, finders)` puts a finding only the trim finder reported (`is_trim`) in `trim`, `tests` and `docs` in their own sections, and everything else, no topic included, in `code`. |
| `target.py` | Resolves `--branch` / `--pr` / `--range` / `--tree` to shas; a slug starts with `slug_repo_name()`, `<owner>-<repo>` from the `origin` URL (`origin_name()`), or the directory name (logged) without one, so every checkout and worktree of a repo shares one directory (review-viewer's `gitdata` restates the rule); writes `diff.patch`, `commits.txt`, `files.txt` (and `skipped.txt` for a tree); a tree's base is `EMPTY_TREE`, its repository is the top of the working tree (`tree_root()`: a subdirectory `--repo` becomes the reviewed path, `--path` joined below it) and `tree_files()` lists its files less the skipped ones (submodules included); `import_dir()` copies a `--dir` source into its target directory's `source/` and commits it there; `target_gate`; `snapshot_head()` (`git archive` of head into the target directory's `tree/`); `snapshot_base()` (the files the explain stage reads at base, into its `base-tree/`). The target directory is `<out>/<repo>/<object>/`: `object_name()` (`pr-<n>`, `branch-<ref>`, `range-<a>..<b>`, `tree`, `tree-<path>`), `checkout_repo_part()` (the repo name from `origin_owner_repo()`, without the owner; `repo_part()` renames `legacy` and `.locks` to `<name>@<owner>`), `dir_object()` for `--dir` (`<basename>`, `dir-<basename>-<6 hex>`). `is_review_folder()`, `review_children()` and `object_folders()` say where reviews are under `<out>` by position, not by name alone (a repo may be called `tree`; an `<out>` that is itself a review folder has no review in its own `rounds/`, `viewer/` or snapshots); review-viewer's `gitdata` restates them. The slug does not change. For `--pr`, also the PR title and body and `branch_rules()` (the base branch's GitHub rules; `[]` when GitHub reports none, `None` with a warning when `gh` or the API fails). A failed rules lookup, and a `gh pr view` that returns an empty title or base branch, also become a `lookup-failed` `Need` with the command a person would run (`build_target(needs=)`). Fixed argv, no shell, no checkout. |
| `deps.py` | The git dependencies' source in the snapshot. `locked_git_deps` reads every `uv.lock` at head (`git show`, `tomllib`) and returns a `Dep(name, url, commit)` per git-sourced package, once per (url, commit). `materialize` keeps a bare partial clone per URL under `cache_root()` (`skill_state_dir("review")/deps/<host>/<path>.git`; only `ALLOWED_SCHEMES`, `https` and `ssh`, tests add `file`), holds `<entry>.lock` while fetching (up to `LOCK_WAIT_S`, 300 s, never broken), fetches a missing commit once and re-clones a broken entry once per run, `git archive`s the commit and extracts it with the data filter (a link leaving the folder is left out) into a temporary folder renamed to `.review-deps/<name>@<commit12>/`, skips one over `deps_max_mb`, and writes `.manifest.json` last. `ensure` (called by the orchestrator after each `snapshot_head`) reuses a manifest on the same head and dependency list whose `ok` folders all exist, else removes `.review-deps/` and materializes again; with `deps` off it does nothing. `needs` turns each dependency not extracted into a `Need`. The orchestrator puts the `ok` entries on `TargetInfo.deps` for the gates and the prompts. Its stop-and-resume rows are tested one per case in `tests/test_deps_resume.py`. |
| `gates.py` | `citation_gate`, `finding_gate`, `verdict_gate`, `need_gate`, and `reason_kind` (which rule a reason names; the only gate text that reaches logs and `checks.json`). `citation_gate` calls `multiplai_core.citations.check_quote` and renames its "path not at commit" to "path not at head"; `file_at_head`/`lines_at_head` wrap core `file_at`/`lines_at`. The first three take `TargetInfo` and return `GateResult`; `gated_need()` applies `need_gate` and blanks a failing command. A web citation (`Citation.is_web`) is skipped, not checked: it may not be first, and it does not confirm. A dependency citation (`Citation.is_dependency`, a path under `.review-deps/`) is checked with `check_quote` against its `TargetInfo.deps` entry's clone and commit (`dependency not in manifest` when none matches), and may not be first either (`first citation is a dependency`). |
| `sdk.py` | `recording()`: a ContextVar block in which `_run` adds each attempt's tool calls (`multiplai_core.ToolCall`), turns and cost to a `CallRecord`, the re-ask included; the stages open one around each call. `agent_call_text` (the piece finders' report call): the same trust gate, budget check and recording, the answer returned as text with no parsing and no re-ask. `agent_call_structured`: trust gate, an allow-list with its complement denied (`Read`/`Grep`/`Glob` for every stage, plus `WebFetch`/`WebSearch` for finders and verifiers), one re-ask on a bad answer: a no-tools, one-turn reformat of the text returned, or a re-run of the prompt when the run itself failed. `parse_answer` and `reformat_prompt` come from `multiplai_core.structured` (`parse_answer` says so when an answer holds no JSON at all); the call loop and its retry policy are this module's. The only caller of `run_agent`. |
| `checks.py` | `add_coverage` (appends a `CoverageRecord` to `state.coverage` and, for an `error` one, its `ERROR_LINES` line to `state.errors`). Pure functions for the record of what was checked: `summarise_call` (a tool call as `{tool, target, detail}`, repo-relative, inputs only), `diff_hunks`, `seen` (`read`/`searched`/`diff`/`prompt`/`fetched`/`not-seen` for a citation, from the agent's calls and the lines its prompt held: the diff, or a verifier's finding citations), `marked_citation` (adds `gate` and `seen`), `prompt_labels` (the `given` list). |
| `budget.py` | `ReviewBudget`, a `multiplai_core.budget.SpendLedger` with review's $50 default ceiling; one ledger per target in core's `ContextVar` (a batch runs targets concurrently) and the circuit breaker. `by_label` is USD per stage label; `by_stage` holds calls, the four token counts, cost and `no_usage_calls` per label. A ledger saved without `by_stage` still loads. |
| `timings.py` | `state.timings`: intervals per key (`run`, each stage, `find:<dimension>`). A resume adds an interval; the `run` interval's end moves forward at each checkpoint. `wall_seconds` sums the closed ones. |
| `config.py` | `review.yaml` > `multiplai.conf` > session model, for per-stage models, effort, concurrency, and the `explain` and `find_with_facts` switches (both default false; `find_with_facts` needs `explain`), `trim` (default true). `extra_finders` is accepted with any value and ignored; `ReviewConfig.extra_finders` says only that the key is present, for `orchestrator.extra_finders_notice`. `TRIM_DIMENSION` is not in `DIMENSIONS`: `stages/find.finder_dimensions` adds it, last, to a change review unless `trim` is off and to a tree review only with `ReviewState.trim`. |
| `chunks.py` | `split_pieces` cuts a change review's diff into `models.Piece`s for the piece finders: whole files only, grouped by the nearest directory holding a `GROUP_MARKERS` file (looked up in `head_tree`, or `base_tree` for a deleted file), a module with its tests (`tested_module`), at most `PIECE_MAX_CHARS` (50 000, inclusive) characters of diff unless one file alone is larger, release metadata (`is_meta`) in meta pieces last, lockfile text left out (listed in `omitted`), binary files counted 0, no cap; `prepare` stores them in `state.pieces` from one `target.tree_paths` per side, and `piece_diff_text` rebuilds a piece's diff. `head_changed_lines` gives the paths stage each file's added lines at head. `diff_files` splits on `\n` only and reads each file's base and head paths and status. |
| `facts.py` | The explain stage's scope (`scope_for`: changed hunks at head widened by `CONTEXT_LINES` (20) and clipped to the file, removed lines at base, binary files left out; a tree review's whole files), its `resolver` for `code_facts`, and `facts.json` (`FactsFile`, `schema_text()`, `write_facts_file`). The contract is `../schema/facts.v1.schema.json`, generated from `FactsFile`; a test fails when the committed file differs. |
| `rounds.py` | Earlier rounds: `keep_round` copies the whole last round (every file and directory but `rounds/`, `viewer/`, the snapshots `tree/`, `base-tree/`, `assess-tree/`, and a `--dir` review's imported checkout `source/`, listed in `NOT_KEPT`) to `rounds/<NN>-<head[:12]>/` when a run starts, also on the same head (a rerun). The copy is made as `rounds/.partial-<name>/` and renamed when complete. `<NN>` is `round_number()`: one more than the round directories there, so it is also the current run's number, the ledger's `round`. A round already kept (same `generated_at` and `run.started_at`, `_round_key`) is not copied twice; nothing is overwritten, and `resume` keeps nothing. A run that stopped before export (its `review-state.json` has another run start than `findings.json`, or there is no `findings.json`) is kept too, without the earlier round's exported files (`EXPORTED`), once (`_state_kept_already` compares the state byte for byte). Rounds kept before multiplai-dev 0.46 are named `<sha12>/` and `<sha12>-2/`; they are still read and counted. A fresh `review` (not `--plan-only`) then deletes the top-level `facts.json`, so a run with explain off never leaves the last round's facts beside its findings. `round_name` gives the round id `post` puts in its markers: the head sha cut to 12 with `-k` for a head's k-th round (the v1 marker form, not the directory name). `load_rounds` reads them with the person's decisions from `viewer/decisions.json`, giving a round's copy of a finding a decision only when its `ts` is not after the next round's `generated_at` (for the last kept round, the top-level `findings.json` when it is the current head's and not itself a kept round), because decisions are keyed by id alone. A copy with no decision of its own takes the rejection its `repeat` assessment recorded, so a reworded repeat is matched by id in the next round. `keep_round` also skips every other top-level directory (a review an older version nested in this one) and the four rollup files (`ROLLUP_FILES`). `folder_target` reads whose a directory is (`review-state.json`, else `findings.json`); `out_root` gives the `<out>` a directory was written under (its parent when it is named by its own slug, as 0.46 and earlier named them, else its grandparent), which `resume`, `assess-only` and the history walk use. `history_elsewhere` finds the same target's rounds kept before 0.46 under a checkout-named slug (`<dirname>--pr-<n>`, under `<out>`, in `legacy/` or in the old `INBOX/reviews`) by the slug's part after the repository and the file's own `origin`, walking `<out>` by position (`_walked`) and the target's own directory for a 0.42 review nested in it, and always reads the target's 0.46 directory, named by its slug; the repeats stage passes them to `load_rounds`, which reads each round once and the decisions beside them, the target's own decisions winning. |
| `ledger.py` | `data_dir()` / `reviews_root()` (`get_paths().data_dir / "reviews"`, the default output directory) and the append-only `<data_dir>/reviews/ledger.jsonl`: `started` and `finished` lines written by `orchestrator.run_state` around every run (review, resume, each batch target; `stopped` is `null`, `budget`, `verify-incomplete` or `failed: <error>`), `interrupted()` (the last line for a `target_dir` and `head_sha` is `started`), and `append()`, which opens with `O_APPEND` and never raises. Tests point `data_dir` at `tmp_path` (conftest). |
| `runlock.py` | `hold(target_dir)`: an exclusive non-blocking `flock` on `<out>/<repo>/.locks/<object>.lock` (`lock_path`: beside the target directory), held by `review`, `resume`, each batch target and `--plan-only` from before the ledger check (before the `--dir` copy) until the run ends, so a running review is never read as interrupted or written over by a second run; the kernel drops it when the process dies. `Locked` names the holder's pid, host and start time, which the file holds; the orchestrator raises it as `RunInProgress` (exit 2). A filesystem without `flock` logs a warning and runs unlocked. |
| `migrate.py` | `migrate --from <dir>`: each directory under `<dir>` copied whole to `<out>/legacy/<name>/` (as `.partial-<name>` first), one `migrated` ledger line per `findings.json` at any depth with `migrated_from`; each source gets a `copying` line in `legacy/sources.jsonl` before its copy and a `done` line after its ledger lines; a `done` source is skipped, and one left at `copying` is finished under the name it was given (copied if missing, only the ledger lines not already in the ledger appended); a name another source already took gets `~<6 hex of the source path>`; the source is only read. |
| `costs.py` | Arithmetic only: `estimate` (pieces by `Piece.chars`, paths at the cap of 21 until mapped), `remaining` (resume: unstored keys in full, unconverted ones at `CONVERT_FRACTION`), `cost_lines` (the stderr estimate, the WARNING at `OVERHEAD_FACTOR` x estimate >= budget left, the default-budget NOTICE), `kept_line` (the budget STOPPED message's last line). `orchestrator.print_cost_lines` prints them in `plan_only`, `review` (after `prepare`) and `resume` (WARNING only); `config.max_cost_source` says whether `--max-cost-usd` was given. |
| `linked_docs.py` | `linked_docs`: the Markdown docs the conventions finder's `CLAUDE.md` / `coding-standards.md` files link (one level, at head then base-only links, read with git at head or else base, binding first, whole within what is left of `CONVENTIONS_MAX_CHARS`, each `fenced`); skips are `conventions` coverage records. `stages/find.conventions_blocks` calls it and adds `binding_instruction` before the first doc. |
| `paths_io.py` | The only reader of `paths.v1`: `assign_paths` (after merge, in `stages/merge.py`: each finding's `paths`, the ids of the paths one of its citations contains an `anchors` line of, `.review-deps/` citations ignored, in `paths.json` order, None for none; `export._finding` writes the key only when non-empty), `views` (each path as a `PathView` for its finder's prompt, a repeated id kept once), `load_paths` (the run's `paths.json`, or None unless `state.paths_status` is `ok` or `empty`), `classify_drop` / `classify_broken` (a `PathDrop` or a cut `TracedPath` as a `CoverageRecord`: scoping drops `info`, a `trace` drop or a cut trace `error`). |
| `stages/` | `paths` (every change review, a tree review only with `--paths`, before find: `code_facts.map_paths` over `chunks.head_changed_lines` or a tree's every line, cap 21, trace limit 30, answers under `paths-cache/`, result in `paths.json` and `state.path_ids`; a failure is recorded with `checks.add_coverage` and find still runs), `explain` (optional, before find: `code_facts` over the scope, one re-ask for files with no fact, answers in `explain_answers`, facts in `state.facts` and `facts.json`), `find` (the dimension finders, then the piece finders: see "The piece finders" below), `verify`, `merge`, `repeats`, `assess`. Each `run_<stage>(state, ctx)` returns at once when the state is past it and skips items it already has. Each stores an agent's answer in the state as it returns (`finder_results`, `verdicts`, `merge_answers`, `repeats`, `assess_answer`), with its `AgentCheck` beside it, so the checkpoint saved at a budget stop keeps both and a resume records no call twice. `find` sets each returned finding's fate and records a `GateCheck` per gated finding; `verify` one per verdict, tries each call up to `VERIFY_TRIES` (3) times inside one `AgentCheck`, stores no stand-in verdict for a finding whose every try failed and raises `VerifyIncomplete` once the others are done (the orchestrator saves the checkpoint like a budget stop), and lowers a `breaks-*` impact on a `tests` finding to `correctness-only` with a line in `state.errors`; `merge` and `assess` mark merged findings in their finder's entry. `find.conventions_blocks` reads every `coding-standards.md` on the path from the root to each changed file's directory, then every `CLAUDE.md` on the same path, so the `CONVENTIONS_MAX_CHARS` cap skips `CLAUDE.md` text first; it returns each path with whether it fit, and `conventions_chain` joins the text. |
| `prompts/` | One module per stage; `piece.build_piece` (a piece's files and whole diff, ownership not reading, every severity and advice, the plain-text report format and `NO FINDINGS`) and `convert.build_convert` (one item per defect or advice, none judged, exact quotes) for the piece finders, `path.build_path` (the whole path inside one fence, the end-to-end and changed-line-first instructions) for the path finders, all fencing their text with `untrusted.fenced` (`multiplai_core.untrusted.defang` with markdown fences kept, so quotes stay exact); `find.facts_block` lists the explain stage's facts with their ids and citation ids, cut at `FACTS_MAX_CHARS`, only with `find_with_facts` (a tree group gets the facts citing its files, `stages/find.group_facts`). Shared blocks in `__init__.py`: `workspace_block(web=)`, `description_block` (PR title and body inside an `<untrusted-content>` fence, as claims to check), `settings_block` (the base branch's rules, and the sentence "a red run can be merged" when no `required_status_checks` rule exists). |
| `export.py` | `ReviewState` → `findings.json` v1, key by key (the contract rejects unknown keys), with the optional `verifier_citations`. Merged-away findings and `Finding.finders` are not exported; the v1 shape did not change for them. A shown finding's `Assessment` is exported as the optional `assessment` object. `write_checks_file` writes `checks.json` (`../../review-viewer/schema/checks.v1.schema.json`): agents ordered by `started_at`, then gate results. `run_record` builds the optional `run` object from the final state only: the ledger, `state.timings`, `state.run_config` and the counts (with `repeats` and `low_value` from `state.assessments`). `needs` is written only when there are some: top level, and on each finding it blocks; `exported_needs()` moves a need from a merged-away finding to the survivor. |
| `render.py` | `review-<slug>.md` (with `render_paths_section`: the `## Paths` section before the appendix, from `paths_io.load_paths` and the findings rows only, every `paths.json` value through `inline_code`, then `gap_line`), the short `summary-<slug>.md` the session pastes into chat, `checks-<slug>.md` (`render_checks`: the checklist, then one section per agent), and the `<SEV>-only.md` rollups, all from the exported dicts (by default each target's latest round once: a 0.46 directory named by the slug alone is left out when a `<out>/<repo>/<object>/` holds its slug, `_latest_rounds`). `listed(findings, mode)` is the one rule for what is listed and counted (confirmed or unverifiable, not `repeat`, `low-value` or `important`, and in critical mode `impact` in `CRITICAL_IMPACTS`); `not_listed` goes to the review's appendix; `post.select` without decisions uses it too. Findings are listed in Code, Tests, Docs and Trim sections (`section`, `SECTION_TITLES`; Trim carries `SECTION_NOTES`, its advisory note, in the review, the summary and the rollups), counted per section by `count_line`, which the header, the summary and the orchestrator's `review finished:` line share. Both cost lines read `run` (`cost_line`). `write_runs` writes `runs.jsonl`: every target's latest round and every round under `rounds/`, one line per run (deduplicated by slug, head, `generated_at` and `run.started_at`), sorted by `generated_at`. `need_lines()` writes the **Needs you** lines in the review and the summary; a need that blocks a finding not listed is printed under that finding in the review's appendix, and the summary counts it. |
| `post.py` | One PR review (`gh api -X POST .../pulls/<n>/reviews`, event `COMMENT`). `select`: with `--decisions`, every shown finding whose decision is `accept`; without it, what `render.listed` keeps for the review's mode, at the severities `POSTED_SEVERITIES_BY_SECTION` gives its section. `commentable_lines` parses the base..head diff into head lines per path. It reads `pr_diff`, not `target.diff_text`: the same base as GitHub's PR diff and GitHub's hunks (`-U3`, `--inter-hunk-context=0`, myers, renames found, `a/` `b/` prefixes) whatever the user's `diff.*` settings, because a wider local hunk sends a comment GitHub refuses with 422 and the one POST then posts nothing; a finding with every line there is an inline comment (`side: RIGHT`), any other goes in the review body with its link. `posted_ids` reads the markers already on the PR (review comments and review bodies, `gh api --paginate`); a finding already marked, or a `still-open` one whose `earlier_id` is, is skipped. A failed read or POST raises `PostError` and posts nothing. |
| `pr_marker.py` | `format_marker`, `MARKER_RE`, `parse_markers`: the hidden `<!-- multiplai-review:finding id=… round=… v=1 -->` line on every posted finding. The format is pinned by a test and documented in `../SKILL.md` ("Marker"), where buildme-findings copies the regex from. |
| `orchestrator.py` | target → [explain] → paths → find → verify → merge → repeats → assess → export (`findings.json`, `checks.json`) → render, saving `review-state.json` after each; `prepare` chooses the target directory (`_choose_folder`: `<repo>/<object>`, then `<repo>@<owner>/<object>` when the directory names another remote, then `<object>-<6 hex>` when a branch or range directory names another ref; each locked before it is read, except that a directory another run holds is read without its lock and passed over when it names another remote or ref), checks it and, while it has no state, the 0.46 directory for an interrupted run, and keeps the earlier round first; `resume`; `batch`; `assess_only` and `assess_report` for the `assess-only` command. |
| `state.py`, `progress.py` | Atomic checkpoint (`multiplai_core.checkpoint`, with review's file name and model); the tailable `progress.log` (`STARTED`, `STAGE`, `DONE`, `FAILED`), a `multiplai_core.progress.ProgressWriter` that adds review's `STARTED` line. |

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
  its result is stored in `finder_results` under `"<dimension>@<group index>"`;
  change reviews keep plain dimension keys, so old checkpoints still load.
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
| `need_gate` | a need's command is one line under 300 characters, holds none of `;` `&` `\|` `>` `<` backtick `$(`, and is a read-only form of a CLI in `NEED_READ_VERBS` (its verb listed there; `gh api` and `curl` with no write method or body, `aws` describe/list/get, no SQL that writes) | command blanked; the need is kept |

`need_gate` is not a security boundary: neither the pipeline nor the session
runs a need's command (the session gives it to the user as `! <command>`). It
keeps each ask readable and safe to copy.

An `unverifiable` verdict lowers severity one step (`stages/verify.py`,
`unverifiable_severity`), but not below MEDIUM when a need blocks the
finding.

Gate function names must not start with `test` (pytest would collect them).

## The piece and path finders: report, then convert

`stages/find.reported_finders` builds one job per stored piece with files
(`state.pieces`), keyed `piece@<id>`, then one per path in `state.path_ids`
(read back with `paths_io.load_paths` and `paths_io.views`, prompt
`prompts/path.build_path`), keyed `path@<id>`, after the dimension pairs, so
the shared concurrency bound starts them in that order and a budget stop
launches nothing later. A path finder works exactly as a piece finder below,
with labels `find:path` and `find:path-convert` and reports
`report-path-<id>.md`. Each job is `run_reported`: an
`sdk.agent_call_text` call (`READ_ONLY` tools, cwd the snapshot, label
`find:piece`) whose text is stored as a `FinderReport` in
`state.finder_reports`, saved, and written atomically as
`report-piece-<id>.md` (`write_report_file`) before the convert call
(`prompts/convert.build_convert`, `ConvertOutput`, label `find:piece-convert`).
`map_converted` turns its items into `ConvertedItem`s (a null severity is
LOW) and a `Finding` for each item with a location, dimension and finder
`piece`. The two calls are two `AgentCheck`s, `<key>` and `<key> convert`;
the items, with their kind, are on the convert one only (`AgentCheck.items`,
exported in `checks.json`), and `render.finder_rows` folds the convert into
the key's row. `run_find` then reads each key's findings with
`_reported_result` and puts them through the same dedupe and `finding_gate`
as the dimensions'. A report of `NO FINDINGS` (or nothing) is converted with
no call. A failed report or convert is recorded with `checks.add_coverage`
(`finder-failed`, `convert-failed`) and the key stays pending, so `resume`
asks only for the missing call; `checks.drop_coverage` removes the record
once a later call succeeds. Only a trust or budget stop leaves the stage. A
fresh round starts with an empty `finder_reports`, and
`rounds.clear_stale_outputs` removes the last round's report files after
`keep_round` has copied them.

## The merge stage

`find` drops a finding only when file, first line and the first 160 characters
of the normalised claim match (`dedupe_key`), and records the dropped copy's
finder in `finders`. Reworded duplicates get through, so `stages/merge.py`
runs after verify:

- `overlap_groups` takes the `confirmed` and `unverifiable` findings and
  chains every pair that `linked` accepts: their anchors are in one file and
  overlap or are at most `MERGE_LINE_GAP` (2) lines apart, or any repository
  citation of one overlaps (within the same gap) any repository citation of
  the other, in any file. Web citations never link. Groups of two or more go
  to one agent each.
- The agent returns `duplicate_sets` of ids. `usable_sets` keeps only ids in
  the group, each id once, sets of two or more.
- `merge_set` keeps one member (confirmed before unverifiable, then the most
  severe, then the first), so its id and verdict stand; it takes the highest severity in the
  set, the union of citations and of finders. For an all-unverifiable set,
  `original_severity` becomes the highest of the members'.
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
  in `state.errors`. Its `duplicate_sets` go through the merge stage's
  `apply_duplicate_sets` (`usable_sets`, `merge_set`). The raw answer is
  stored in `state.assess_answer`, so a resume asks nothing twice; a failed
  call stores an empty answer and leaves every finding `useful`.
- Both labels end up in `state.assessments` (repeats as `repeat`), exported
  per finding as `assessment`.
- Neither stage's answers have been tuned against recorded decisions. Run
  `assess-only` over the saved reviews first (see `review/SKILL.md`), and
  only then change the prompt.

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
`gate_reject`, `budget_stop`, `verify_incomplete`, `done`, `post`. No field
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
