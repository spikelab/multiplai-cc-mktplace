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
| `__main__.py` | CLI: `run`. `main(argv, now=…)` takes the one clock. Order: resolve, choose and lock the folder, `--restart`'s rename, load state (unreadable → exit 1), `--dir` import, first state save, snapshot, `resolve` into `steps_done`, `SPEC_EXTRACT:RESOLVED <run>`. Exit codes 0 / 1 / 2 / 5. |
| `target.py` | `Target`; `resolve` (`--tree [--path]`, `--pr` through `_pr_view`, the `gh` seam, with `base_sha` = merge-base of the base tip and the head; `--dir`); the naming rules (`sanitize_slug`, `origin_owner_repo`, `repo_part`, `checkout_repo_part`, `object_name`, `dir_object`); `folder_target` (a folder's saved `target`, read without validating, for the walk); `run_folder` (this target's own folder first, then the walk); `import_dir` (one commit with a fixed author and the run's clock; a `source/` with a commit is reused); `snapshot` (`git archive` into `snapshot/`, `.spec-extract-head` written last); `restart` (one rename to `<object>.old-<ts>`). |
| `runlock.py` | `hold(run)`: an exclusive `flock` on `<root>/<repo>/.locks/<object>.lock`, which the kernel drops when the process dies. `Locked` names the holder. |
| `state.py` | `State` (`target`, `steps_done`), saved with `multiplai_core.checkpoint.save_model` (temp file `.state-<random>.tmp`, never read or deleted here). `load` returns None for no file and raises `Unreadable` for a file it cannot read. The saved `target` is never rewritten on a resume. |

## Tests

No test calls `gh` or a model: `--pr` tests replace `target._pr_view`. Git runs
for real on temporary repositories. `tests/test_resume_matrix.py` holds one
`def test_R<n>` per row of the plan's table that needs a test; each stops the
step at its row's point, resumes, and compares with a run that never stopped.
