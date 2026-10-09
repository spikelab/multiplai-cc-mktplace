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
| `__main__.py` | CLI: `review`, `batch`, `rollup`, `resume`, `post`, `assess-only`. Calls `setup_logging` once. Owns the stdout contract and the exit codes. |
| `models.py` | Pipeline models. `Finding.citations` has `min_length=1`; `Finding.id` is computed with the v1 rule, never taken from a model. `Finding.finders` lists every finder that reported it. `STAGES` is the run order (target, find, verify, merge, repeats, assess, export, render, done); `Repeat` and `Assessment` hold the repeats and assess stages' results; `ReviewState` maps the removed `prescribe`/`check_fix` stages of an old checkpoint to `verify`. `AgentCheck` (one agent call: stage, subject, given, calls, outcome, turns, cost, times, error, plus `findings` for a finder and `verdict` for a verifier) and `GateCheck` (one gate result) go in `ReviewState.checks` / `gate_checks`, empty in older checkpoints. `Need` (what the review could not get: `what`, `blocks` a finding id or `"review"`, `cause`, `command`, `source`) is kept in `state.needs`; `NeedAsk` is the shape an agent answers in (`Verdict.needs`, `FinderOutput.needs`), with an unknown `cause` read as `no-access`. |
| `target.py` | Resolves `--branch` / `--pr` / `--range` to shas, writes `diff.patch`, `commits.txt`, `files.txt`; `target_gate`; `snapshot_head()` (`git archive` of head into `<slug>/tree/`). For `--pr`, also the PR title and body and `branch_rules()` (the base branch's GitHub rules; `[]` when GitHub reports none, `None` with a warning when `gh` or the API fails). A failed rules lookup, and a `gh pr view` that returns an empty title or base branch, also become a `lookup-failed` `Need` with the command a person would run (`build_target(needs=)`). Fixed argv, no shell, no checkout. |
| `gates.py` | `citation_gate`, `finding_gate`, `verdict_gate`, `need_gate`, and `reason_kind` (which rule a reason names; the only gate text that reaches logs and `checks.json`). The first three take `TargetInfo` and return `GateResult`; `gated_need()` applies `need_gate` and blanks a failing command. A web citation (`Citation.is_web`) is skipped, not checked: it may not be first, and it does not confirm. |
| `sdk.py` | `recording()`: a ContextVar block in which `_run` adds each attempt's tool calls (`multiplai_core.ToolCall`), turns and cost to a `CallRecord`, the re-ask included; the stages open one around each call. `agent_call_structured`: trust gate, an allow-list with its complement denied (`Read`/`Grep`/`Glob` for every stage, plus `WebFetch`/`WebSearch` for finders and verifiers), one re-ask on a bad answer: a no-tools, one-turn reformat of the text returned, or a re-run of the prompt when the run itself failed. `parse_answer` says so when an answer holds no JSON at all. The only caller of `run_agent`. |
| `checks.py` | Pure functions for the record of what was checked: `summarise_call` (a tool call as `{tool, target, detail}`, repo-relative, inputs only), `diff_hunks`, `seen` (`read`/`searched`/`diff`/`prompt`/`fetched`/`not-seen` for a citation, from the agent's calls and the lines its prompt held: the diff, or a verifier's finding citations), `marked_citation` (adds `gate` and `seen`), `prompt_labels` (the `given` list). |
| `budget.py` | Per-target ledger in a `ContextVar` (a batch runs targets concurrently) and the circuit breaker. `by_label` is USD per stage label; `by_stage` holds calls, the four token counts, cost and `no_usage_calls` per label. A ledger saved without `by_stage` still loads. |
| `timings.py` | `state.timings`: intervals per key (`run`, each stage, `find:<dimension>`). A resume adds an interval; the `run` interval's end moves forward at each checkpoint. `wall_seconds` sums the closed ones. |
| `config.py` | `review.yaml` > `multiplai.conf` > session model, for per-stage models, effort, concurrency. |
| `rounds.py` | Earlier rounds: `keep_round` copies the last round's `findings.json`, review markdown and `checks.json` to `rounds/<head[:12]>/` when a run starts on a new head (never on the same head, never overwriting); `load_rounds` reads them with the person's decisions from `viewer/decisions.json`, giving a round's copy of a finding a decision only when its `ts` is not after the next round's `generated_at` (for the last kept round, the top-level `findings.json` when it is the current head's), because decisions are keyed by id alone. |
| `stages/` | `find`, `verify`, `merge`, `repeats`, `assess`. Each `run_<stage>(state, ctx)` returns at once when the state is past it and skips items it already has. Each stores an agent's answer in the state as it returns (`finder_results`, `verdicts`, `merge_answers`, `repeats`, `assess_answer`), with its `AgentCheck` beside it, so the checkpoint saved at a budget stop keeps both and a resume records no call twice. `find` sets each returned finding's fate and records a `GateCheck` per gated finding; `verify` one per verdict; `merge` and `assess` mark merged findings in their finder's entry. `find.conventions_blocks` reads every `coding-standards.md` on the path from the root to each changed file's directory, then every `CLAUDE.md` on the same path, so the `CONVENTIONS_MAX_CHARS` cap skips `CLAUDE.md` text first; it returns each path with whether it fit, and `conventions_chain` joins the text. |
| `prompts/` | One module per stage; shared blocks in `__init__.py`: `workspace_block(web=)`, `description_block` (PR title and body inside an `<untrusted-content>` fence, as claims to check), `settings_block` (the base branch's rules, and the sentence "a red run can be merged" when no `required_status_checks` rule exists). |
| `export.py` | `ReviewState` → `findings.json` v1, key by key (the contract rejects unknown keys), with the optional `verifier_citations`. Merged-away findings and `Finding.finders` are not exported; the v1 shape did not change for them. A shown finding's `Assessment` is exported as the optional `assessment` object. `write_checks_file` writes `checks.json` (`../../review-viewer/schema/checks.v1.schema.json`): agents ordered by `started_at`, then gate results. `run_record` builds the optional `run` object from the final state only: the ledger, `state.timings`, `state.run_config` and the counts (with `repeats` and `low_value` from `state.assessments`). `needs` is written only when there are some: top level, and on each finding it blocks; `exported_needs()` moves a need from a merged-away finding to the survivor. |
| `render.py` | `review-<slug>.md`, the short `summary-<slug>.md` the session pastes into chat, `checks-<slug>.md` (`render_checks`: the checklist, then one section per agent), and the `<SEV>-only.md` rollups, all from the exported dicts. `repeat` and `low-value` findings get their own section after the others, and only a count in the summary. Both cost lines read `run` (`cost_line`). `write_runs` writes `runs.jsonl`. `need_lines()` writes the **Needs you** lines in the review and the summary. |
| `post.py` | One `gh pr comment`; with `--decisions`, only findings whose decision is `accept`; without it, never a `repeat`. |
| `orchestrator.py` | target → find → verify → merge → repeats → assess → export (`findings.json`, `checks.json`) → render, saving `review-state.json` after each; `prepare` keeps the earlier round first; `resume`; `batch`; `assess_only` and `assess_report` for the `assess-only` command. |
| `state.py`, `progress.py` | Atomic checkpoint; the tailable `progress.log` (`STARTED`, `STAGE`, `DONE`, `FAILED`). |

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
| `citation_gate` | the whitespace-normalised quote is inside lines `line_start..line_end` at head | building block |
| `finding_gate` | every repo citation passes; the first citation is a repo citation; the file changed (unless `dimension == "pre-existing"`); severity is known | finding → `rejected` |
| `verdict_gate` | a `confirmed` verdict has at least one repo citation that passes (web citations do not count) | verdict → `unverifiable`, severity lowered one step |
| `need_gate` | a need's command is one line under 300 characters, holds none of `;` `&` `\|` `>` `<` backtick `$(`, and is a read-only form of a CLI in `NEED_READ_VERBS` (its verb listed there; `gh api` and `curl` with no write method or body, `aws` describe/list/get, no SQL that writes) | command blanked; the need is kept |

`need_gate` is not a security boundary: neither the pipeline nor the session
runs a need's command (the session gives it to the user as `! <command>`). It
keeps each ask readable and safe to copy.

An `unverifiable` verdict lowers severity one step (`stages/verify.py`,
`unverifiable_severity`), but not below MEDIUM when a need blocks the
finding.

Gate function names must not start with `test` (pytest would collect them).

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

- **Rounds.** A PR is reviewed into the same `<out>/<slug>/`, so
  `orchestrator.prepare` calls `rounds.keep_round` before a new head writes
  over the last round. `viewer/decisions.json` (review-viewer's, keyed by
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

## Logging

`setup_logging("review-pipeline", propagate_loggers=("review_pipeline", "multiplai_core"))`
writes `review-pipeline.log`. `log_event("review", …)` fires `start`, `stage`,
`gate_reject`, `budget_stop`, `done`, `post`. No field holds finding or prompt
text: `gate_reject` records which rule fired (`gates.reason_kind`), not
the reason string. The record of what was checked goes to `checks.json` and
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
