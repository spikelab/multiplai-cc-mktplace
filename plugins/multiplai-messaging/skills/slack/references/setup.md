# Slack token & scope setup

The script authenticates as the user with a Slack **user token** (`xoxp-…`),
read from `$SLACK_TOKEN` (or `$SLACK_USER_TOKEN`). A user token sees exactly the
channels and DMs the user is in — no bot invites. The token is read from the
environment only; it is never written to disk.

## Creating the app and token

Use a **custom internal** Slack app. (Internal matters: since May 2025,
commercially-distributed apps are throttled to 1 request/min on message history —
internal apps keep normal limits.)

1. <https://api.slack.com/apps> → **Create New App** → *From scratch* → pick the
   workspace. The app is just a permission container; it hosts no code.
2. **OAuth & Permissions → Scopes → User Token Scopes.** Add:

   | Purpose | Scopes |
   |---|---|
   | Read channel/DM history | `channels:history`, `groups:history`, `im:history`, `mpim:history` |
   | List channels & resolve names | `channels:read`, `groups:read`, `im:read`, `mpim:read`, `users:read` |
   | Download attachments | `files:read` |
   | Post messages (`send`) | `chat:write` |
   | Workspace search (`search`, server-side) | `search:read` |

3. **Install to Workspace** and authorize. Copy the **User OAuth Token**
   (`xoxp-…`) and export it as `SLACK_TOKEN`.

> **Adding a scope later?** Slack only grants scopes at install time. After
> editing scopes, click **Reinstall to Workspace**, then re-copy/re-export the
> token. A missing scope surfaces as a clear `missing_scope` error naming exactly
> what to add.

In the multiplai runtime the token is forwarded into the container via the
launcher (`claude.sh`), so `SLACK_TOKEN` is already present — no manual export.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `missing_scope` (shows `needed`/`provided`) | Add the named scope under User Token Scopes, **reinstall**, re-export the token. |
| `search` → `missing_scope` for `search:read` | Add `search:read` and reinstall, or use `search --local` over the synced cache. |
| `not_in_channel` on a public channel | The user isn't a member — join it in Slack. |
| DM shows a raw `U…` id instead of a name | `users --refresh` (user may be deactivated or newly added). |
| `token does not start with xoxp-` warning | A bot (`xoxb`) token was exported; use the **User** OAuth Token. |
| Ambiguous `send --to <name>` | Pick from the printed candidates or pass the exact `U…` id. |

## How it works (short version)

- **Reading:** `users.conversations` lists what the user is in;
  `conversations.history` (cursor-paginated, `oldest=last_ts`) pulls new messages;
  `conversations.replies` pulls thread replies for parents seen in the run.
- **Search:** server-side uses `search.messages` (whole workspace); `--local`
  runs a substring query over the cached `messages` table.
- **Attachments:** each message's `files[]` download with the Bearer token and are
  recorded; already-downloaded and external (non-Slack) links are skipped.
- **Retry:** all three `slack_sdk` retry handlers are wired — rate-limit (honors
  `Retry-After`), connection, and server-error — plus a 3× backoff on downloads.

## Known limitation

New replies on **old** threads aren't always caught. `conversations.history` only
re-surfaces a thread when a reply is also broadcast to the channel. The tool
fetches replies for parents it sees in the current run; long-idle threads that get
a late reply won't be re-scanned.
