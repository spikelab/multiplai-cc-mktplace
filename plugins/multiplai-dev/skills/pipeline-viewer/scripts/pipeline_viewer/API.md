# pipeline-viewer HTTP API

The contract between the server (`server.py`) and the page (`static/`). The
page is built against this file; a change to either side changes this file
first. The models behind every response are in `models.py`.

## Requests

- **Base URL.** The page is served at `/` from the same origin as the API:
  `http://<host>:<port>/` locally, or the ngrok URL given to
  `serve --public-url`.
- **Identity.** Each person has a personal link, `<base>/?t=<token>`, written
  by `invite`. The page takes the token out of the address bar on load (so it
  is not left in history or sent as a Referer) and keeps it for the origin
  (the same personal link opened once makes later links without `?t=`, such
  as the ones in Slack, work in that browser).
- **Auth header.** Every `/api/*` request carries the token:
  `X-Review-Token: <token>`. The server looks it up in `people.json`; the
  person it names is who the request is from. No other auth exists.
- **Origin.** A request with an `Origin` header must come from the page: its
  origin equals the request's `Host`, or the `--public-url` origin. Else 403.
- **POST bodies** are JSON objects with `Content-Type: application/json`
  (else 415), at most 64 KiB (else 413).
- **Static files.** `GET /` and `GET /static/<name>` need no token and hold no
  change data. `<name>` is one file name in `static/` (no `/`, no leading `.`).
- **Page links** the server makes (Slack): `<base>/#change=<name>&stop=<n>`.

## Errors

Every error is `{"error": "<message>"}` with one of these statuses. The
message is meant to be shown to the person as it is.

| Status | When |
|---|---|
| 400 | A missing or invalid parameter or body field (the message names it). |
| 401 | No token, or a token not in `people.json`: `unauthorized`. |
| 403 | A foreign `Origin` (`forbidden`); a doc path outside the change dir or a diff of a commit not on the change's branch since `base_sha` (`outside the change`); ack/approve/rework by a person who is not a reviewer of that stop (`<name> is not a reviewer of stop <n>`). |
| 404 | Unknown change, missing doc, unknown route, a diff for a change with no build (`no build for <name>`). |
| 405 | A method the route does not take. |
| 409 | A write the repo refuses (below), or a stop in the wrong state for it. |
| 413 | Body over 64 KiB; a doc over 1 MiB. |
| 415 | A POST that is not `application/json`. |
| 500 | `internal`: a bug; the server log has the trace. |

The 409 messages, exactly:

- `pm-spec is running on <name>; try again when it stops` (its `run.lock` is held)
- `buildme is building <name>; try again when it stops, or comment on the pull request`
  (the change's checkout is a build's worktree and the build is neither
  complete, nor stopped on a failure, nor waiting at stop 4; inferred from
  buildme's files, row S18)
- `specs/changes/<name> is a symlink; nothing is written through it`, or
  `<path> goes through a symlink; nothing is written through it`
- `<git's error>; nothing was recorded, try again` (the commit failed; the
  file the write had changed is put back as it was)
- `<files> have uncommitted edits; commit or discard them first` (approve only;
  `<files>` is a comma-separated list relative to the repo)
- `branch <b> has diverged from origin; resolve it, then retry`
- `no checkout holds branch <b>; check it out or start pm-spec/buildme on it`
- `stop <n> of <name> is not waiting` (ack/approve/rework/comment on a stop
  that is `not_reached`)
- `stop <n> is approved on the pull request` (approve or rework at stop 5)
- `stop <n> already approved by <name>` / `stop <n> already decided by the agent`
  (approve or rework after the stop was approved or decided at this revision)

## Shapes

### Person

```json
{"name": "Ada Example", "email": "ada@example.org", "role": "pm", "slack_id": "U0123" }
```

`role` is `"pm"` or `"engineer"`; `slack_id` may be `null`.

### ChangeStatus

```json
{
  "name": "add-export",
  "branch": "TCK-1-add-export",
  "current_stop": 2,
  "waiting_on": ["Ada Example"],
  "stops": [StopStatus, StopStatus, StopStatus, StopStatus, StopStatus],
  "pm_spec": PmSpecStatus | null,
  "build": BuildStatus | null,
  "epic": EpicStatus | null,
  "unpushed": false,
  "writable": true
}
```

- `stops` always has five entries, `n` 1 to 5, in order.
- `current_stop`: the first reached stop that is not `approved` or
  `decided_by_agent`, else the last reached stop; `null` before stop 1.
- `waiting_on`: the current stop's `waiting_on`.
- `unpushed`: the branch's checkout has commits its upstream lacks (a push
  failed). The page offers "retry push" (`POST /api/push`).
- `writable`: a checkout holds the branch, so comments and approvals can be
  committed. False: every write answers 409.

### StopStatus

```json
{
  "n": 1,
  "label": "proposal",
  "state": "waiting",
  "reviewers": [{"name": "Ada Example", "role": "pm", "invited": true}],
  "waiting_on": ["Ada Example"],
  "waiting_since": "2026-01-02T10:00:00Z",
  "ack_by": "2026-01-02T12:00:00Z",
  "decide_at": "2026-01-02T18:00:00Z",
  "acknowledged_by": [],
  "approved_by": [],
  "decided_by": null,
  "rework_by": [],
  "revision": "3f2a…40 hex",
  "docs": [{"path": "proposal.md", "approve": true, "exists": true}],
  "records": [Record],
  "pr_url": null
}
```

- `label`: 1 `proposal`, 2 `use cases and components`, 3 `specs`,
  4 `implementation plan`, 5 `pull request`.
- `state`: `not_reached | waiting | acknowledged | approved |
  decided_by_agent | rework_requested`. All of it is about the current
  `revision`: a commit for an older revision does not count, so a stop
  reached again after a rework is `waiting` again.
  - Stops 1-3 and 4 are `approved` once every role in the stop's reviewers
    has an `Approved-by` from a person of that role; at stop 3 (pm and
    engineer by default) one approval leaves it `waiting`/`acknowledged`
    with that name in `approved_by`. A stop pm-spec or buildme already went
    past with no approval commit is `approved` with `approved_by: []`.
  - Stop 5 is `waiting` once the build has a PR (`pr_url`); it is approved on
    GitHub, which this server never asks, so it stays `waiting`.
- `waiting_on`: reviewers still expected to act, while `waiting` or
  `acknowledged`; `[]` otherwise.
- `waiting_since`, `ack_by`, `decide_at`: ISO 8601 UTC, or `null`.
  `ack_by` is when a Slack reminder goes out if nobody acknowledged.
  `decide_at` is when the session is told to decide alone; it is `null` once
  someone acknowledged (an acknowledgement holds the wait) and always `null`
  for stop 5.
- `revision`: what an approval names. Stops 1-3: a hash of the stop's
  documents as committed on the branch. Stop 4: buildme's plan revision.
  `null` while not reached.
- `docs`: paths relative to the change dir, in reading order. `approve: false`
  marks a document shown for reading only (`tasks.md` at stop 4). Read each
  with `GET /api/doc`. Stop 4 lists `implementation-plan.md`, or `design.md`
  for a build from before the rename.
- `records`: the decision commits of this stop, oldest first.

### Record

```json
{"sha": "40 hex", "kind": "approve", "stop": 1, "by": "Ada Example",
 "at": "2026-01-02T11:00:00Z", "subject": "review(add-export): Ada Example approved stop 1",
 "revision": "3f2a…", "reason": null}
```

`kind`: `comment | ack | approve | rework | decided | answer`. `reason` is set on
`decided` (`no reply in <m> minutes`). `answer` is the session's answer to a
question asked on the page, written into `review/stop-<n>.md`; its `by` is
the person who asked. An answer decides nothing: it never changes `state`.

### PmSpecStatus

```json
{"steps": {"proposal": "passed", "components": "passed"},
 "interview_waiting": false, "spec_status": "draft",
 "handoff_revision": null, "running": false,
 "progress_tail": ["last lines of .pm-spec/progress.md"]}
```

### BuildStatus

```json
{
  "phase": "tdd", "complete": false,
  "started_at": "…", "updated_at": "…",
  "branch": "TCK-1-add-export", "base_sha": "40 hex", "pr_url": null,
  "failed_stop": false,
  "spent_usd": 3.2, "max_usd": 20.0,
  "blocks": [BlockRow],
  "blocks_done": 1, "blocks_total": 3,
  "elapsed_s": 5400.0,
  "board": {} | null,
  "progress_tail": ["last lines of build-progress.md"]
}
```

`elapsed_s` is `updated_at - started_at` (the last checkpoint, not now).
The page estimates the remaining time itself (elapsed / blocks_done ×
blocks left) and labels it an estimate. `board` is `.board.json` as buildme
wrote it.

### BlockRow

```json
{"number": 1, "name": "Parse the input", "status": "done",
 "review_iterations": 2, "timed_out": false, "fix_pass": false,
 "test_commit": "sha|null", "impl_commit": "sha|null", "refactor_commit": "sha|null",
 "red_evidence": "trimmed output", "green_evidence": "trimmed output",
 "review_rounds": ["reviews/block-01-round-1.json"]}
```

`status`: `pending | testing | implementing | reviewing | done | failed`.
Each commit opens with `GET /api/diff`; each review round with `GET /api/doc`.

### EpicStatus

```json
{"run": "03",
 "children": [{"id": "api", "status": "waiting", "pr": 12, "rounds": 1, "reason": "PLAN:WAITING:…"}],
 "totals": {"waiting": 1, "merged": 1}, "spent_usd": 12.5,
 "agents": [{"n": 7, "kind": "build", "child": "api", "started_at": "…", "exit_code": 3, "finished": true}]}
```

From the latest `runs/NN/summary.json` of the change's epic and its `agents/`
records. A child that is itself a change in this repo is in `/api/changes`
under its own name.

### OutboxRow (answers from the session)

```json
{"v": 1, "reply_to": "q-20260102T100000-ab12", "ts": "…", "text": "markdown",
 "done": true, "commit": "sha|null"}
```

A long answer comes as several rows; only the last has `done: true`. `commit`
is set when the answer edited a document (the page links it with
`GET /api/diff` when it is on the build branch, else shows the sha).

### WriteResult (every write)

```json
{"id": "q-20260102T100000-ab12", "commit": "40 hex", "already": false,
 "pushed": true, "unpushed": false, "push_error": null}
```

- `id`: the inbox row written for the session (every write writes one).
- `already`: true when the same record (same stop, revision and person) was
  already on the branch; nothing new was committed and `commit` is that
  earlier one. Sending an ack twice is safe.
- `pushed`: the push after the commit succeeded. `false` with
  `push_error: null` means the checkout has no upstream to push to (a
  build's branch before buildme publishes it).
- `unpushed`: commits are waiting for a push; `push_error` says why it failed.

## Endpoints

### `GET /api/whoami`

```json
{"person": Person, "agent": "Claude", "repo": "<repo directory name>",
 "started": "…", "public_url": "https://x.ngrok.app" | null}
```

### `GET /api/changes`

`{"changes": [ChangeStatus, …], "generated_at": "…"}`, sorted by name. Each
call re-reads the files, so the page polls it (every 15 s is enough).

### `GET /api/change?name=<name>`

One `ChangeStatus`. 404 for an unknown name.

### `GET /api/doc?name=<name>&path=<path>`

```json
{"name": "add-export", "path": "proposal.md", "text": "…", "bytes": 1234}
```

`path` is relative to the change dir (`components/api/plan.md`,
`reviews/block-01-round-1.json`, `review/stop-1.md`). It is read from the
checkout that holds the branch (the build's worktree first), else the repo.
Anything that resolves outside the change dir, symlinks included, is 403;
so is every path of a change whose dir (or `specs/` or `changes/` above it)
is a symlink. A symlinked change dir is not listed as a change at all.
Missing: 404. Over 1 MiB: 413. Text is UTF-8 with invalid bytes replaced.

### `GET /api/diff?name=<name>&commit=<sha>`

```json
{"commit": "40 hex", "subject": "…", "author": "…", "date": "…",
 "files": [{"path": "src/a.py", "status": "M"}],
 "diff": "unified diff text", "truncated": false}
```

`commit` is 7-40 hex (else 400). Only commits on the build branch since its
`base_sha` (else 403); a change with no build is 404. The diff is cut at
1 MiB (`truncated: true`).

### `GET /api/poll?since=<n>`

`{"answers": [OutboxRow, …], "n": <total rows>}`: the session's answers after
the first `since` rows. The page keeps `n` and sends it next time; after a
server restart it starts again from `since=0`.

### `GET /api/questions?name=<name>[&stop=<n>]`

Every question asked on the page about the change (at stop `n`, when
given), oldest first, with the session's answer so far. Everyone invited
sees every question.

```json
{"questions": [{"id": "q-…", "change": "add-export", "stop": 1,
  "doc": "proposal.md", "section": "Why",
  "author": {"name": "Ada Example", "role": "pm"},
  "asked_at": "…", "text": "Why now?",
  "answer": "markdown" | null, "answered_at": "…" | null,
  "done": true, "commit": "sha|null"}]}
```

`answer` joins the outbox rows that reply to the question; `done` is the
last row's flag. `stop` other than 1-5: 400. Unknown change: 404.

When the session gives a final answer (`reply` without `--more`) to a
question that names a stop, the question and the answer are appended to
`review/stop-<n>.md` (heading `## Question: <doc> — <section>`, then
`<asker>, <time>, asked:` and `<agent>, <time>, answered:`, each text quoted
with `> `) and committed with trailers `Stop`, `Answered-for: <asker>`,
`Question: <id>` and `Revision`. The commit names no author: the committer
is whoever runs the session. It is refused as a comment is (pm-spec running,
a diverged branch, no checkout); the answer is still in the outbox and on
the page, and the next write on that change commits it (row S17).

### `POST /api/ask`

Body: `{"name": str, "stop": int|null, "doc": str|null, "section": str|null, "text": str}`.
`text` 1-8000 characters. Writes an `ask` row for the session, nothing in
git. Answer: `{"id": "q-…"}`. The answer arrives through `/api/poll` with
`reply_to` = that id.

### `POST /api/comment`

Body: `{"name": str, "stop": 1-5, "doc": str, "section": str, "text": str}`.
`text` 1-8000 characters, `section` at most 200. Appends the comment to
`review/stop-<n>.md` in the change dir, quoted with `> ` on every line, and
commits only that file. Refused
409 only for `run.lock`, divergence, no checkout or a stop not reached
(uncommitted edits to the stop's docs do not block a comment).
Answer: WriteResult.

### `POST /api/ack`

Body: `{"name": str, "stop": 1-5}`. Records that this person is reviewing the
stop (an empty commit). The page sends it once when the person opens review
mode on a stop that waits on them; a repeat answers `already: true`.
Answer: WriteResult.

### `POST /api/approve`

Body: `{"name": str, "stop": 1-4}`. An empty commit with trailers `Stop`,
`Approved-by` and `Revision` (the stop's current `revision`). Refused 409
while the stop's documents have uncommitted edits. Answer: WriteResult.

### `POST /api/rework`

Body: `{"name": str, "stop": 1-4, "text": str, "title": str|null}`. `text`
1-8000 characters; `title` at most 120, default `Rework requested at stop <n>
by <name>`. Stops 1-3: the request is added to the change's `spec-issues.md`
(read by `pm-spec revise`). Stop 4: it is added to `review/stop-4.md`
(the session edits the implementation plan). Then a commit with trailers
`Stop`, `Rework-requested-by` and `Revision`. One rework request per person
per stop revision: a second one answers `already: true` and records nothing
(more detail goes in as comments). Answer: WriteResult.

### `POST /api/push`

Body: `{"name": str}`. Pushes the branch's checkout and makes no commit
(the "retry push" button). Answer: `{"pushed": bool, "unpushed": bool,
"push_error": str|null}`.

## Inbox rows (for the session, not the page)

Every `ask` and write appends one row to `<box>/inbox.jsonl`:

```json
{"v": 1, "id": "q-…", "ts": "…", "kind": "ask|comment|ack|approve|rework|timeout",
 "change": "add-export", "stop": 1, "doc": "proposal.md", "section": "Why",
 "text": "…", "revision": "…", "commit": "sha",
 "author": Person}
```

`timeout` rows come from the deadline check, have no `author`, and mean the
stop passed `decide_timeout_minutes` with nobody acknowledging or approving.
