# Security

Installing a plugin from here copies files onto your machine, where they run
with **your** credentials. The reasoning behind the checks that guard that, and
what each one catches, is in the README —
[Pre-publish checks](README.md#pre-publish-checks). This file does not restate
it; it covers reporting, the limits, and what happens after a report.

## Reporting a vulnerability

Email **security@spikelab.org**. Please include:

- which plugin and version (`.claude-plugin/marketplace.json`, or the
  `<plugin>@<version>` tag you installed);
- the file and, if you have it, the shortest reproduction;
- what an attacker gets — a credential read, an unexpected network egress, a
  command executed on the user's behalf.

Do **not** open a public issue for something exploitable. Anything else — a
skill that fails confusingly, a missing prerequisite, a wrong doc — is a normal
issue and is welcome as one.

Expect a first reply within a few days. This is a small project maintained by
one person; there is no bounty and no SLA, and saying so is more useful than
implying otherwise.

## A malicious or compromised skill

If you believe a shipped skill is doing something it does not declare, or that
a released version was tampered with:

1. **Stop using it now.** Remove it from Claude Code's `/plugin` menu.
   Uninstalling stops future runs; it does not undo anything an earlier run
   already did.
2. **Rotate whatever it could reach.** Assume the blast radius is every
   credential available in the sessions where it ran — the tokens in that
   environment, not only the ones the skill's SKILL.md mentions. The
   credential-scoped skills here are the obvious first look (`slack` and
   `gmail` in `multiplai-messaging`, `devops-gcp` in `multiplai-dev`), but a
   skill that is misbehaving is by definition not bound by its own
   documentation.
3. **Report it** as above, with the plugin version and the tag if you have it.
4. **Check what it left behind.** Skills write into your workspace. `git status`
   and `git log` on the affected repository, plus the plugin's own log files,
   are the record.

## Which versions get fixes

**The latest version of the affected plugin, and only that one.** A fix ships
as a new plugin version — bumped in `.claude-plugin/marketplace.json`, with a
`CHANGELOG.md` entry and a new `<plugin>@<version>` tag. Existing tags are
never moved and older versions are never patched in place, so a tag you have
pinned or installed keeps meaning exactly what it meant.

Practically: update through the `/plugin` menu, and read the plugin's
[changelog](CHANGELOG.md) to see what you got.

Every plugin here is versioned independently, so a security fix in one does not
move any other.

## The honest limit

`scripts/scan_skills.py` is a **static scan, not a sandbox**. It compares what a
skill's code does against what its SKILL.md says it does, and it fails on
patterns with no legitimate use in a shipped skill (`curl | bash`,
base64-decode-and-execute). It **raises the cost of hiding behaviour; it does
not make hiding impossible.** Nothing here executes a skill in isolation and
watches what it touches.

Two more limits worth stating:

- Skills run with the permissions of the Claude Code session that invoked them.
  This repository does not sandbox them. The
  [`multiplai-container`](https://github.com/spikelab/multiplai-container)
  sandbox is where containment lives, and it is optional.
- Several skills deliberately read text that somebody else wrote — web pages,
  email, Slack messages, browser pages. The contract that keeps that text as
  *data* rather than instructions is
  [`docs/untrusted-content.md`](docs/untrusted-content.md). It is a mitigation
  for prompt injection, not a solution to it: prompt injection is role
  confusion, and no filter resolves it in general.

## Scope

In scope: the plugins, hooks, scripts and workflows in this repository, and the
release process that publishes them. Out of scope: Claude Code itself (report
to Anthropic), `uv`, and third-party tools a skill shells out to. Issues in
[`multiplai-core`](https://github.com/spikelab/multiplai-core),
[`multiplai-kit`](https://github.com/spikelab/multiplai-kit) or
[`multiplai-container`](https://github.com/spikelab/multiplai-container) reach
the same address — say which repo.
