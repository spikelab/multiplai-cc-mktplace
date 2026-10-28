# pipeline-viewer

A web page for following and answering pm-spec and buildme, shared with the
PM and engineers from the laptop that runs them.

- **Every change and where it is**: stops 1-5 (proposal, use cases and
  components, specs, implementation plan, pull request), who each waits on,
  for how long, whether someone is on it, and when the agent will decide
  alone.
- **Review a stop step by step**: each document in full, one section at a
  time; comment, ask Claude, then approve or ask for rework.
- **A record in git**: every comment, acknowledgement, approval, rework
  request and agent decision is a commit on the change's branch, in the
  person's name.
- **The build**: phase, blocks with review rounds and timeouts, RED/GREEN
  evidence, per-block diffs, spend against budget.
- **Slack**: a message in the configured channel when a stop is reached, and
  every later event in its thread.

Run it from a Claude Code session with `/multiplai-dev:pipeline-viewer`; see
[SKILL.md](SKILL.md) for what it does on the machine, the credentials it
reads (`SLACK_TOKEN`, optional) and the config in `specs/config.yaml`.

Needs `uv`, `git`, and ngrok to share the page beyond the machine.
