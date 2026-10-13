# spec-extract scripts — orientation

Writes BDD capability specs from the Python code of a commit, a directory or a
PR. The user-facing contract (command, folders, exit codes) is `../SKILL.md`.
The plan is `plan-buildme-reverse-specs-2026-10-13.md`; its "Stop and resume
states" table says what every stop leaves on disk and what a re-run does.
Build no resume behaviour that table does not name.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/spec-extract/scripts python -m spec_extract_pipeline --help
# CI form
cd plugins/multiplai-dev/skills/spec-extract/scripts && \
  uv run --project ../../../../.. --package spec-extract-pipeline --extra dev python -m pytest tests/
```

This package never imports `review_pipeline` or `build_pipeline`: skills do not
import each other in an installed plugin. Review's folder rule is restated in
`target.py`, not imported.

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `run`. `main(argv, now=…)` takes the one clock. Order: resolve, choose and lock the folder, `--restart`'s rename, load state (unreadable → exit 1), `--dir` import (or, on a resume, the check that `source/` is at the saved import), first state save, snapshot, `resolve` into `steps_done`, `SPEC_EXTRACT:RESOLVED <run>`; then scope (computed once from the saved target read through this run's checkout, `git_target`, then read from state), `plan.txt`, `--plan-only`'s stop, the ledger restored from `state.budget`, explore, `SPEC_EXTRACT:EXPLORED`; then `_group_write_render`: group (no answer → `SPEC_EXTRACT:NOT_GROUPED`, exit 1), write, `write` into `steps_done`, gate, render, `SPEC_EXTRACT:RENDERED`. Once `render` is in `steps_done` a re-run extracts no snapshot, rewrites no output, finishes deleting `snapshot/` and exits 0 (R10). Exit codes 0 / 1 (also `BackendUnavailable`) / 2 / 3 budget / 4 not trusted / 5. |
| `target.py` | `Target`; `resolve` (`--tree [--path]`, `--pr` through `_pr_view`, the `gh` seam, which asks about origin's repository, with `base_sha` = merge-base of the base tip and the head; `--dir`); the naming rules (`sanitize_slug`, `origin_owner_repo`, `repo_part`, `checkout_repo_part`, `object_name`, `dir_object`); `folder_target` (a folder's saved `target`, read without validating, for the walk); `run_folder` (this target's own folder first, then the walk); `import_dir` (one commit, `.gitignore` overridden, with a fixed author and the run's clock; a `source/` with a commit is reused; on a resume, a `source/` not at the saved import raises `ImportLost`, exit 1, R27); `snapshot` (the commit's blobs from `git ls-tree`/`git cat-file` into `snapshot/`, as git stores them, links that leave the tree left out; `.spec-extract-head` written last); `restart` (one rename to `<object>.old-<ts>`). |
| `runlock.py` | `hold(run)`: an exclusive `flock` on `<root>/<repo>/.locks/<object>.lock`, which the kernel drops when the process dies. `Locked` names the holder. |
| `scope.py` | `compute(target)`: the `.py` files at the head (under `--path`; for `--pr`, those `git diff --no-renames` names between the saved `base_sha` and the head), less review's `tree_files` skips plus symbolic links; `ast` public symbols (test files: evidence, no symbols); files `ast` cannot parse in `unparsed`; units from `code_facts.partition` at 6,000 lines. `check` refuses no units (`SPEC_EXTRACT:NO_PYTHON`) or more than `--max-units`. `plan_text` for `plan.txt`. |
| `explore.py` | Item 7's dispatch rule over `code_facts.bounded` and `code_facts.ask`: a unit with no answer or an error answer and fewer than 2 attempts is asked; budget check, delete the error answer, count, save, call, save answer and ledger together. A budget stop takes the attempt back and exits 3 after the calls in flight; `BackendUnavailable` or `RepoTrustError` from a call takes it back too, then exits 1 or 4. `ordered_answers` puts answers in unit order for every reader. `finish` saves `facts_generated_at` the first time, then writes `facts.json` (`facts.v1`, kind `tree`, citations checked in `git_target`'s checkout) and `report.md` from state, then adds `explore`. |
| `calls.py` | Item 7's rule for group and write: `ask_one` (budget check, delete the error answer, count, save, call, one save of answer and ledger; `CallFailed` stored as an error; a budget, backend or trust stop takes the attempt back) and `dispatch` (two rounds over `code_facts.bounded`, then `BudgetStopped`, exit 3, or the backend/trust error). |
| `group.py` | One call with no tools over the gated facts (`explore.gated_facts`, never `facts.json`) and the saved symbols, key `group`. `gate` is pure: kebab-case unique names, each symbol in one capability (`unassigned` last), at most 40 per capability (split by module). `finish` writes `capabilities.json` from the gate and adds `group`; None when not grouped. |
| `write.py` | One agent per gated capability (`Read`, `Glob`, `Grep` on `snapshot/`), key = capability name, label `write:<name>`; the prompt carries `UNTRUSTED_RULES`, the symbols, the facts citing their files and the test files. |
| `gate.py` | `check`: every scenario citation through `check_citation` at the commit; a scenario with none passing, a requirement with no scenario left (or a repeated name), a capability with no requirement are dropped with the reason; "pinned by test" when a passing citation is in a test file; a symbol whose `ast` span no kept citation overlaps is "Not specified". |
| `render.py` | `registry/<capability>.md` (Decided 13's format, model text on one line so it cannot start a heading), `report.md` (scope sections, then counts, capabilities, dropped, not specified, not grouped, not written), `summary.json`; adds `render`, then deletes `snapshot/`, marker first. |
| `report.py` | `write_output` (every output file, through `<file>.tmp` and `os.replace`); `report.md` from sections passed as data; no timestamp, no spend. |
| `sdk.py` | The model call (`agent_call_structured`, the `CallFn`), copied in form from review's: trust gate (`--trust-repo` / `SPEC_EXTRACT_TRUST_REPO`); an answer that does not parse is reformatted once, and a failed run is not re-run (the dispatch rule asks again), then `CallFailed`; a reformat the budget or the backend stops after a paid run is `CallFailed` too (R31), and so is any exception from `run_agent` other than `AgentRunError`; a backend no call can run with (the Claude backend's or the pi backend's setup failures, `_SETUP_REASONS`) raises `BackendUnavailable`; spend recorded into the run folder's ledger (`start_budget`, default cap $80); `model_for(step)` from `multiplai.conf`. |
| `state.py` | `State` (`target`, `steps_done`, `scope`, `explore_answers`, `explore_attempts`, `budget`, `facts_generated_at`, `group_answer`, `group_attempts`, `write_answers`, `write_attempts`), saved with `multiplai_core.checkpoint.save_model` (temp file `.state-<random>.tmp`, never read or deleted here). `load` returns None for no file and raises `Unreadable` for a file it cannot read. The saved `target` is never rewritten on a resume. |

## Tests

No test calls the real `gh` or a model: `conftest.FakeCall` replaces `sdk.agent_call_structured` for every test (autouse), answering explore from the snapshot (and group and write by budget label, or from `answers[key]`), with a per-unit or per-label script of `ok`/`fail`/`kill`/`over`; the tests of resolve, scope and explore use `stops_after_explore`, which ends the run there; `--pr` tests replace `target._pr_view`, or put a
fake `gh` script in place to check its argv and failure paths. Git runs
for real on temporary repositories. `tests/test_resume_matrix.py` holds one
`def test_R<n>` per row of the plan's table that needs a test; each stops the
step at its row's point, resumes, and compares with a run that never stopped.
