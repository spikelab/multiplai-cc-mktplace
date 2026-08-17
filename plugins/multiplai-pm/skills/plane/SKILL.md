---
name: plane
description: Work with Plane tickets and projects — list, read, search, create, update, comment, and move issues through workflow states, restricted to an explicit project allowlist so shared team projects cannot be touched. Triggers on "my tickets", "what's on my board", "create a ticket", "move X to in progress", "comment on SPK-12", "what's in progress", "add this to Plane", or /multiplai-pm:plane.
model: opus
effort: medium
disable-model-invocation: false
---

# multiplai-pm: `plane`

Read and write Plane issues from Claude Code. Every request is checked against a
**project allowlist**: the tool physically cannot write to a project you did not list —
the usual case being a shared team project you must not disturb while working on your
own. Project-scoped reads are refused the same way; the few workspace-scoped reads
(search, members, assets) do reach the server, and out-of-scope hits are dropped
client-side (rule 8).

## Setup

```bash
PLANE="python3 ${CLAUDE_PLUGIN_ROOT}/skills/plane/scripts/plane.py"
```

Configuration comes from the environment. All three are **required**; the tool refuses
to run without them rather than falling back to "everything the token can reach".

| Variable | Meaning |
|----------|---------|
| `PLANE_API_TOKEN` | Personal access token. Plane: *Profile settings → Personal access tokens*. |
| `PLANE_WORKSPACE` | Workspace slug — the segment after the host in the app URL. |
| `PLANE_ALLOWED_PROJECTS` | Comma-separated project UUIDs. Optional labels: `<uuid>:My Project`. |
| `PLANE_BASE_URL` | Optional. Defaults to `https://api.plane.so`. Self-hosted: your own host. |
| `PLANE_ENV_FILE` | Optional. A `KEY=VALUE` file to read the above from when they are absent from the environment. Only `PLANE_*` keys are read, and the real environment always wins. |

**Finding a project UUID:** open the project and copy it out of the URL —
`https://app.plane.so/<workspace>/projects/<uuid>/issues`.

**First run, always:** `$PLANE check`. It prints the resolved config, lists every project
the token can see marked `ALLOWED` / `BLOCKED`, and self-tests the guardrail with both
negative and positive cases. If a project you expected is `BLOCKED`, or an allowlisted
one shows `MISSING`, fix that before doing anything else.

## Referring to issues

Use the human ref you see in Plane: `SPK-12`. A bare number (`12`) works when the
project is unambiguous or `-p` is given, and a UUID always works.

Resolving `SPK-12` pages through the project's issues to match the sequence number,
because Plane's search does not match identifiers. That is fine for normal projects and
gets slow on very large ones. `-p`/`--project` is only needed for a bare number: the
scan is already per-project, so it does not make resolution faster.

## Commands

| Command | Purpose |
|---------|---------|
| `check` | Resolved config, allow/block status per project, guardrail self-test. |
| `projects [--all]` | Allowed projects; `--all` also lists blocked ones, marked. |
| `issues [-p P] [--state S] [--limit N] [--full]` | List issues. Slim by default. |
| `get <ref>` | One issue with its description rendered as text. |
| `create --title T [--body M \| --body-file F] [--priority] [--state] [--target-date]` | New issue. |
| `update <ref> [--title] [--body] [--priority] [--state] [--target-date]` | Partial update. |
| `comment <ref> "text"` | Add a comment. |
| `comments <ref>` | Read the comment thread. |
| `states [-p P]` / `labels [-p P]` / `members` / `cycles [-p P]` / `estimates [-p P]` | Reference data. |
| `attachments <ref> [--download DIR]` | List an issue's attachments; download them. |
| `search <query> [--limit N]` | Workspace search, filtered to allowlisted projects. |

`create` and `update` also take `--assignee`, `--label` (`--create-labels` to add a
missing one), `--estimate <value>` and `--cycle <name|uuid|active>`. The first three
replace the whole field; `--cycle` is additive and does nothing if the issue is already
there.

Global flags, given **before** the subcommand (`$PLANE --dry-run update ...`, not
`$PLANE update ... --dry-run`): `--json` for machine-readable output, `--dry-run` to
print a write instead of sending it.

Priorities: `urgent`, `high`, `medium`, `low`, `none`. States are given **by name**
(`--state "In Progress"`) and resolved per project; run `states` to see what exists.

## Working rules

1. **Run `check` first in a new environment.** Confirm the allowlist is what you expect
   before any write. Do not assume a project is in scope because the user mentioned it.

2. **`issues` is deliberately slim.** It omits issue bodies, which dominate the payload.
   Use `get <ref>` for one body; reach for `--full` only when you genuinely need every
   field of every issue.

3. **Dry-run anything you are unsure about.** `--dry-run` prints the exact request. Use
   it when the user's intent is ambiguous, or before a bulk change.

4. **Bodies are markdown.** Headings, lists, fenced code, images, bold/italic/inline code
   and links convert to Plane's HTML. Tables, blockquotes and **nested** list items do
   **not** — they degrade to plain paragraphs, so avoid them. For anything long, use
   `--body-file` rather than fighting shell quoting.

5. **`update --body` replaces the whole description.** It is not an append. To add to an
   existing issue, either `comment`, or `get` the body first and send it back extended.

6. **Report the ref you changed.** After a write, say `DFT-1`, not "the ticket" — the
   user needs something they can click.

7. **A `BLOCKED:` error is a scope decision, not a bug.** It means the request targeted
   a project outside the allowlist. Do not try to route around it; tell the user which
   project was refused and let them decide whether to widen `PLANE_ALLOWED_PROJECTS`.

8. **Search is workspace-wide and then filtered.** Hits from non-allowlisted projects are
   withheld and counted on stderr. So "no results" can mean "matches existed but were all
   out of scope" — pass on that distinction rather than reporting a flat zero. If stderr
   says `TRUNCATED`, the list is **not** the whole answer: raise `--limit` before telling
   anyone a ticket does not exist.

9. **An empty list is not always an absence.** Two cases proved against Plane Cloud: the
   cycle list comes back empty when the token's user is not a member of that project, and
   `issue-attachments/` is empty for images pasted into a description (they are inline
   assets, which `attachments` also lists). Report what was checked, not what was assumed.

## Examples

```bash
$PLANE check                                        # verify config + guardrail
$PLANE projects --all                               # what is in and out of scope
$PLANE issues -p SPK --state "In Progress"
$PLANE get SPK-12
$PLANE create -p SPK --title "Fix login redirect" \
    --body-file /tmp/body.md --priority high --state Todo
$PLANE --dry-run update SPK-12 --state Done       # print the PATCH, send nothing
$PLANE update SPK-12 --state Done
$PLANE comment SPK-12 "Deployed in **v2026.07.28**."
$PLANE search "migrations"
$PLANE --json issues -p SPK | jq '[.[] | select(.priority == "urgent")]'
```

## Limits

- No attachment **upload**; download works, and `![alt](url)` in a body references an
  image that must already be hosted somewhere.
- No module assignment on write.
- No delete, by design: the tool can create and modify issues but not remove them, and it
  refuses to touch project objects at all — so it cannot enable a project's cycles module
  or create its estimate set either. Those are set up in Plane.
- Sub-issues, relations and worklogs are not exposed.
