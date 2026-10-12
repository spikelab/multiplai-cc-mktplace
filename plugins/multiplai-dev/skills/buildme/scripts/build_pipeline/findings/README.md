# build_pipeline.findings

`python -m build_pipeline findings --pr <n>` reads a PR's open review comments
from allowed authors, sorts each one, fixes the real defects through buildme's
spec and TDD machinery, replies on the PR, and runs `/multiplai-dev:review`
again until a round finds nothing new or `findings.max_rounds` is reached.

| Module | What it does |
|---|---|
| `models.py` | `PRComment`, `Verdict`, `Triage`, `TriageRecord`, `SpecChange` |
| `fetch.py` | lists unresolved review threads, top-level comments and the findings in review bodies; decides whose comments are acted on; fences every body |
| `triage.py` | one read-only agent per allowed comment; writes `findings-r<N>.md` |
| `spec.py` | writes `respec-findings-r<N>.md`, applies it to the change's requirements (and `specs/registry/` when the change is archived), commits `spec(findings-r<N>): …` |
| `code.py` | writes `findings-r<N>-tasks.md` and runs the TDD engine on it against `.findings-r<N>-state.json`, through the `BuildConfig` overrides `change_dir`, `tasks_file` and `state_file` |
| `publish.py` | pushes, refreshes the PR body through the pr skill's `edit`, replies in each thread |
| `loop.py` | the round loop, its checkpoint (`.findings-state.json`), the wip refs and the `FINDINGS:*` lines |
| `cli.py` | resolves the change, branch and worktree for `--pr`, then runs the loop |

Review threads are never resolved. The pipeline only replies.

## GitHub calls, checked on `spikelab/github-tests` (2026-10-12)

The write calls were checked on the scratch repo before any code relied on
them (work item 1 of `plan-buildme-findings-2026-10-11.md`). The repo was empty,
so a `main` with one README commit was pushed first, then a branch
`probe/findings-replies` adding `calc.py`, then PR #1
(<https://github.com/spikelab/github-tests/pull/1>), head
`c193ad444ceead81532d953db46379331b74f2a4`.

### 1. One inline review comment

```
gh api repos/spikelab/github-tests/pulls/1/comments -f commit_id=$SHA -f path=calc.py \
  -F line=2 -f side=RIGHT \
  -f body=$'**HIGH** — add subtracts.\n\n<!-- multiplai-review:finding id=0123456789 round=abcdef012345 v=1 -->' \
  --jq '{id, path, line, side, in_reply_to_id, user: .user.login}'
{"id":4246766510,"in_reply_to_id":null,"line":2,"path":"calc.py","side":"RIGHT","user":"spikelab"}
```

### 2. One top-level PR comment

```
gh api repos/spikelab/github-tests/issues/1/comments -f body="Top-level probe comment." \
  --jq '{id, user: .user.login, author_association}'
{"author_association":"OWNER","id":6122005484,"user":"spikelab"}
```

### 3. A reply in the review comment's thread

```
gh api repos/spikelab/github-tests/pulls/1/comments/4246766510/replies \
  -f body="Fixed in c193ad4: add now adds." --jq '{id, in_reply_to_id, path, line, user: .user.login}'
{"id":4246767462,"in_reply_to_id":4246766510,"line":2,"path":"calc.py","user":"spikelab"}
```

A reply addressed to a reply's id lands in the same thread, attached to the
thread's first comment:

```
gh api repos/spikelab/github-tests/pulls/1/comments/4246767462/replies \
  -f body="Second reply, addressed to the reply's id." --jq '{id, in_reply_to_id}'
{"id":4246769647,"in_reply_to_id":4246766510}
```

The pipeline still always replies to the thread's first comment.

### 4. The reply sits in the same thread (GraphQL)

```
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:50){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated path line comments(first:50){nodes{databaseId author{login} authorAssociation body createdAt url}}}}}}}' \
  -f o=spikelab -f r=github-tests -F n=1
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":"Y3Vyc29yOnYyOpK0MjAyNi0xMC0xMlQxMDowMTo0OVrOq2jcZg=="},"nodes":[{"id":"PRRT_kwDOVExsjs6raNxm","isResolved":false,"isOutdated":false,"path":"calc.py","line":2,"comments":{"nodes":[{"databaseId":4246766510,"author":{"login":"spikelab"},"authorAssociation":"OWNER","body":"**HIGH** — add subtracts.\n\n<!-- multiplai-review:finding id=0123456789 round=abcdef012345 v=1 -->","createdAt":"2026-10-12T10:01:49Z","url":"https://github.com/spikelab/github-tests/pull/1#discussion_r4246766510"},{"databaseId":4246767462,"author":{"login":"spikelab"},"authorAssociation":"OWNER","body":"Fixed in c193ad4: add now adds.","createdAt":"2026-10-12T10:01:56Z","url":"https://github.com/spikelab/github-tests/pull/1#discussion_r4246767462"}]}}]}}}}}
```

`author { login __typename }` is accepted too and returns `"User"` for a person
(a GitHub App's comment has `"Bot"`).

### 5. The top-level comment is listed

```
gh api repos/spikelab/github-tests/issues/1/comments \
  --jq '[.[] | {id, user: .user.login, author_association, created_at}]'
[{"author_association":"OWNER","created_at":"2026-10-12T10:01:51Z","id":6122005484,"user":"spikelab"}]
```

`user.type` is `"User"` here; it is `"Bot"` for an app.

### 6. Collaborator permission

```
gh api repos/spikelab/github-tests/collaborators/spikelab/permission --jq '{permission, role_name}'
{"permission":"admin","role_name":"admin"}
gh api repos/spikelab/github-tests/collaborators/octocat/permission --jq '{permission, role_name}'
{"permission":"none","role_name":""}
```

No call that resolves a review thread was made.

### What this means for the code

- The pipeline's replies are written by the same login as the person who runs
  it, so they come back on the next fetch as comments from an allowed author.
  Every reply the pipeline writes ends with
  `<!-- multiplai-buildme:reply v=1 -->`, and `fetch.py` skips any comment
  carrying it.
- A thread is replied to through its first comment's `databaseId`.
