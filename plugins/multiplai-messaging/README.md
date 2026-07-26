# multiplai-messaging

Messaging skill pack for Claude Code: **read/search/post Slack as yourself, and
search/read/draft Gmail (never sends)**. Part of the
[`multiplai`](../../README.md) marketplace.

## Installation

```
claude plugin marketplace add spikelab/multiplai-cc-mktplace
claude plugin install multiplai-messaging@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `slack` | Read, search, and post to Slack as the user via their own `xoxp` user token — no bot, sees exactly what the user sees. |
| `gmail` | Work with the user's Gmail inbox. Exactly three operations: search the inbox, read one inbox message, create a draft. It **never sends**, and cannot see anything outside the inbox (archive/sent/spam/all-mail are unreachable). |

## Compatibility

Both skills need credentials you provide (full standalone setup docs live in
each skill):

- `slack` — your Slack `xoxp` user token.
- `gmail` — Gmail OAuth credentials.

No kit or container required — runs on vanilla Claude Code.

Message bodies, subjects, and sender/channel names are externally-authored
text: both skills deliver them fenced as data, never instructions — see the
[untrusted-content contract](../../docs/untrusted-content.md).

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
