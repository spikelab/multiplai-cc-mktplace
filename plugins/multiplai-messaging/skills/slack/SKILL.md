---
name: slack
description: >-
  Read, search, and post to Slack as the user (via their own xoxp user token —
  no bot, sees exactly what they see). Use when the user pastes a Slack message
  link (…slack.com/archives/…) and wants it read, asks what someone said in a
  channel or DM, wants to search Slack for a topic, or wants to post/DM a
  message. Triggers on "read this Slack thread", "what did X say in Slack",
  "check #channel", "search Slack for …", "post to #channel", "DM X on Slack",
  "send a Slack message", "sync my Slack", or a pasted Slack permalink.
model: opus
effort: low
---

# Slack

A local-first Slack client the user owns. It authenticates as **the user** with
their `xoxp-…` user token (read from `$SLACK_TOKEN`), so it sees every channel
and DM they're in — no bot to invite. Messages are cached to SQLite so nothing is
re-fetched; attachments download to disk. It can post messages **as the user**.

Run everything through the bundled script (`uv` auto-installs `slack_sdk`):

```bash
SLACK="uv run ${CLAUDE_PLUGIN_ROOT}/skills/slack/scripts/slack_client.py"
```

Requires `SLACK_TOKEN` (an `xoxp-…` user token) in the environment. If it's unset
or a scope is missing, the script prints exactly what to fix — see
[references/setup.md](references/setup.md) for creating the app and its scopes.

## Verbs

| Verb | What it does |
|------|--------------|
| `thread <link>` | Read a pasted Slack message permalink + its full thread. **The default when the user pastes a link.** |
| `search <query>` | Search messages workspace-wide (server-side) or the local cache (`--local`). |
| `send --to <who> --text <msg>` | Post as the user to a channel, a person, or self. |
| `sync` | Fetch new messages for all member channels (incremental; the default command). |
| `channels` / `users` | List channels you're in / the id↔name directory. |
| `export --channel <c>` | Dump a channel's cached messages (md or json). |
| `files` / `status` | Re-download missing attachments / show cache stats. |

Run `$SLACK <verb> -h` for every flag.

## Reading a pasted link (most common)

When the user pastes any Slack message link, read it directly:

```bash
$SLACK thread 'https://<workspace>.slack.com/archives/C0B0T384MT8/p1783347819836239'
```

It fetches that message — and its whole thread if it has replies — printing author
names and timestamps, caching messages and downloading attachments. A
`?thread_ts=…` on the link (reply permalinks have it) is used as the thread parent.
Right-click any message in Slack → *Copy link* produces such a permalink.

## Searching

Default is **server-side** search across the whole workspace (needs the
`search:read` scope — see setup):

```bash
$SLACK search "location mismatch dataform"
$SLACK search "budget" --channel '#finance' --from '@emma' --limit 30
```

`--channel`/`--from` become Slack search modifiers (`in:#chan`, `from:@handle`);
you can also put raw Slack search syntax directly in the query. Each result prints
with a permalink you can then pass to `thread`.

Add `--local` to search only the **already-synced** cache (offline, no extra
scope; here `--channel`/`--from` resolve against the cached directory):

```bash
$SLACK search "permissions" --local --channel '#dolcetech'
```

If a server-side search returns `missing_scope`, either add `search:read` (setup)
or fall back to `--local` after a `sync`.

## Posting (as the user)

`send` posts **as the user** — confirm the exact text and destination before
sending; it's an outward-facing action.

```bash
$SLACK send --to me           --text "reminder to self"
$SLACK send --to '#dolcetech'  --text "deploy finished ✅"
$SLACK send --to 'Claudio'     --text "ping"                       # name → cached directory
$SLACK send --to U09H4TU1V6U   --text "in-thread reply" --thread-ts 1720123456.000100
```

`--to` accepts `me`/`self`, a `#channel`, a `C…/D…/U…` id, or a person's name
(matched against the cached directory; ambiguous names print the candidates).

## Notes

- **Cache location:** skill state (SQLite db + downloaded assets) lives in
  `$WORKSPACE/.multiplai/data/skills/slack` — the workspace's git-ignored
  kit-runtime bucket, so message content and attachments are never committed and
  never land in INBOX. Outside the kit it falls back to
  `~/.local/share/multiplai-messaging/slack`. Override with `--data-dir` or
  `SLACK_DATA_DIR`.
- **First run in a workspace:** `sync` populates history; later runs pull only
  what's new. Local search and `export` only see what's been synced.
- **Errors are actionable:** `missing_scope` names the exact scope to add;
  `not_in_channel` means the user isn't a member. Show the message verbatim and
  point at [references/setup.md](references/setup.md).
