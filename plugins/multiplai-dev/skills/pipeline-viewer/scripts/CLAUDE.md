# pipeline-viewer scripts — orientation

A stdlib HTTP server, a status reader and the decision record for pm-spec and
buildme changes. Nothing here calls a model.

```bash
uv run --directory plugins/multiplai-dev/skills/pipeline-viewer/scripts python -m pipeline_viewer --help
cd plugins/multiplai-dev/skills/pipeline-viewer/scripts && \
  uv run --project ../../../../.. --package pipeline-viewer --extra dev python -m pytest tests/ -q -n auto
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `serve`, `invite`, `reply`, `pending`, `notify`, `record decided`. The box is `--box`, else `<data_dir>/pipeline-viewer/<repo>-<hash>/`. No command prints a token. |
| `models.py` | `ChangeStatus` and its parts, the inbox and outbox rows. `API.md` restates them. |
| `config.py` | `pipeline_viewer:` in `specs/config.yaml`: reviewers per role, reviewers and timeouts per stop, the Slack channel. |
| `places.py` | A change's copies (repo, pm-spec's `pm-spec-<branch>` worktree, a build's worktree) and the write checkout. |
| `gitio.py` | Git argv runner and read helpers (worktrees, upstream, review commits and trailers, the docs revision, dirty files). |
| `status.py` | Pure: files and git → `ChangeStatus`. Which file each stop's state comes from is in its docstring. |
| `record.py` | The only module that writes the repo: comment, acknowledge, approve, rework, decided, push. Refusals S4-S6. |
| `deadlines.py` | Reminder and timeout row, once per `(change, stop, revision)` in `deadlines.json`. |
| `slack.py` | One post per stop revision (`notified.json`), thread replies; `SLACK_TOKEN` only handed to `slack_sdk`. |
| `buildme.py` | Imports `write_issues` and `epic_dir_for` from `../../buildme/scripts` (not a declared dependency: uv refuses a workspace member as a path source). |
| `server.py` | `ThreadingHTTPServer`, the routes of `API.md`, people.json auth (re-read on change), the deadline thread. |
| `static/` | The page (chunk C). Until then a placeholder `index.html`. |

## Decision commits

Subject `review(<change>): …`; trailers `Stop: <n>`, `Revision: <rev>` and one
of `Approved-by`, `Acknowledged-by`, `Commented-by`, `Rework-requested-by`,
`Decided-by: agent` (+ `Reason`). Stop 4's `Revision` is buildme's
`spec_gen.revision`, so buildme's plan-approval gate accepts the commit; stops
1-3's is a sha1 over the committed blobs of the stop's documents
(`gitio.tree_revision`). A person's commit has `--author` from people.json.

## Tests

`tests/fixture_repo.py` builds a bare origin, a main checkout, pm-spec
worktrees at stops 1-3 and build worktrees at stop 4, mid-TDD and stop 5
(`python tests/fixture_repo.py <dir>` builds it anywhere). All content is
invented. `test_resume.py` covers rows S1-S7, S11 and S13; `slackstub.py`
replaces `slack_sdk.WebClient`.
