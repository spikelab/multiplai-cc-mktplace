---
name: Bug report
about: Something broke or behaved differently than documented
title: ''
labels: bug
assignees: ''
---

**Plugin / skill**
Which plugin (and skill, if applicable)? e.g. `multiplai-context` / `setup`.

**What happened**
What you did, what you expected, what happened instead.

**Environment**
- OS (macOS / Linux / WSL):
- Claude Code version (`claude --version`):
- Vanilla Claude Code or multiplai-kit container?
- `uv` installed? (`uv --version`)

**Logs**
For multiplai-context issues: the relevant lines from
`<workspace>/.multiplai/data/logs/activity.log` and/or `hook-errors.log`
(strip anything personal), and the output of `/multiplai-context:health`.

**Anything else**
Screenshots, settings snippets (redact keys), reproduction steps.
