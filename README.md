# Multiplai

> Your agent's model of you should be something you edited, not something that accreted while you weren't looking.

Multiplai gives Claude Code **persistent, inspectable memory** — plus six
themed skill packs on top.

- **Memory that's yours.** Everything gets captured; nothing becomes memory
  without your approval — `/multiplai-context:dream-remember` is the only
  path that edits your memory files.
- **No black boxes.** Every routing decision, every unattended pass, every
  write is logged where you can read it.
- **Works on vanilla Claude Code.** One command, no Docker:

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-context@multiplai
```

*(2-minute demo lands here — recorded with the suite's own screen-demo skill.)*

**First recall.** Install, run `/multiplai-context:setup` (two questions),
restart once, then ask the new session *"What do you know about me?"* — and
get your own words back. That's the loop that everything else builds on;
the step-by-step walkthrough is in the
[plugin quickstart](plugins/multiplai-context/README.md#standalone-install-no-kit).

## Requirements

One hard prerequisite: the context-plugin hooks and the Python-backed skills
run via [`uv`](https://docs.astral.sh/uv/) — install it first
(`curl -LsSf https://astral.sh/uv/install.sh | sh`). Per-skill details in the
[compatibility matrix](#compatibility-matrix).

The `@multiplai` suffix in the install command is the **marketplace's** name
(`name` in `.claude-plugin/marketplace.json`), not the repository's and not
the [umbrella repo](https://github.com/spikelab/multiplai)'s — it is what
Claude Code shows in the `/plugin` menu, so it stays as it is. (The same
commands also work from a shell: `claude plugin marketplace add …` /
`claude plugin install …`.)

Want the sandboxed container, launcher, and workspace conventions too?
That's [`multiplai-kit`](https://github.com/spikelab/multiplai-kit) — later,
if you want it; nothing here needs it. The wider suite — what it is and how
the five repos fit together — is mapped in the
[umbrella repo](https://github.com/spikelab/multiplai).

**Two cross-cutting contracts** apply across plugins and are worth reading once:

- [`docs/untrusted-content.md`](docs/untrusted-content.md) — how skills that
  ingest text somebody else wrote (web pages, email, Slack, browser pages, log
  lines) fence and defang it, and why fenced text is **data, never
  instructions**.
- [`docs/degradation-contract.md`](docs/degradation-contract.md) — what a skill
  does when a prerequisite is missing.

## Plugins

Seven plugin packs, 40+ skills. Each plugin is versioned and released on its
own, and each carries its own release notes — see
[`CHANGELOG.md`](CHANGELOG.md) for the index.

| Plugin | Description |
|--------|-------------|
| [`multiplai-context`](plugins/multiplai-context) | Context routing, continuous learning, session awareness, and memory management. The heart of the system. |
| [`multiplai-pm`](plugins/multiplai-pm) | Product-management pack: JTBD synthesis, persona codification, PR/FAQ, strategy memos, job applications, landing pages. |
| [`multiplai-writing`](plugins/multiplai-writing) | Content creation toolkit: briefs, drafts, copy editing, LinkedIn posts, image prompts. |
| [`multiplai-research`](plugins/multiplai-research) | Code-driven deep-research pipeline, insight extraction, structured interviewing. |
| [`multiplai-dev`](plugins/multiplai-dev) | Developer pack: spec-driven builds (buildme), code/security review, refactoring, walkthroughs, e2e tests, cloud ops, skill authoring. |
| [`multiplai-media`](plugins/multiplai-media) | Transcription, YouTube transcripts, screen-recording demo videos, diagrams, host-browser automation. |
| [`multiplai-messaging`](plugins/multiplai-messaging) | Messaging pack: read/search/post Slack as yourself, and search/read/draft Gmail (never sends). |

## Compatibility matrix

| | |
|---|---|
| ✅ | vanilla Claude Code, any OS |
| 🍎 | vanilla Claude Code on macOS (no kit needed) |
| 🔑 | needs credentials/tokens you provide |
| 🌉 | needs the multiplai-kit container→host SSH bridge |

All ✅/🍎 skills also work inside the kit; the kit never *removes* a
capability. When a prerequisite is missing, skills fail with a message naming
it and the vanilla fix (see the [degradation
contract](docs/degradation-contract.md)).

| Plugin | Skill | Runs on | Notes |
|--------|-------|:-------:|-------|
| multiplai-context | *all hooks & skills* | ✅ | Needs `uv`. First session start resolves deps (allow ~1 min once). `qmd-search` additionally needs qmd installed. The hooks also write a session registry, which the fleet view reads to say which of your sessions needs you (see [Session accounting](plugins/multiplai-context/README.md#session-accounting)) — with multiplai-kit the launcher additionally marks entries whose container has exited; without it, uncleanly-killed sessions are listed as idle until they age out. Session start also launches a **detached memory maintainer** (24h gate, never writes to memory) — see [What runs unattended](#what-runs-unattended). |
| multiplai-dev | buildme | ✅ | Needs `uv` + network. `--skip-research` if multiplai-research absent. |
| | deepen, think, e2e-test | ✅ | e2e-test frontend mode needs `agent-browser` (npm); backend mode is plain HTTP. |
| | codebase-walkthrough, learn-stack, skill-creator, plan | ✅ | |
| | analyze-context-router, propose-skill | ✅ | Operate on multiplai-context — install it first. |
| | devops-gcp | 🔑 | Knowledge pack; real operations need your `gcloud` auth. |
| | swift-build | 🍎 | Swift/Xcode toolchain is macOS-only. From the kit container: 🌉. |
| multiplai-media | youtube-transcript | ✅ | Subtitle path works anywhere. Audio-transcription fallback: 🍎 (Apple-Silicon mlx-whisper) or 🌉. |
| | excalidraw | ✅ | |
| | transcribe | 🍎 | mlx-whisper needs Apple Silicon macOS. From the kit container: 🌉. Plain Linux: use whisper.cpp / faster-whisper instead. |
| | screen-demo | 🍎 | Needs ffmpeg + mlx-whisper on a Mac. From the kit container: 🌉. |
| | host-browser | 🌉 | Drives the host's real Chrome via the `ab` bridge; on a Mac a local CDP Chrome also works. |
| multiplai-messaging | slack | 🔑 | Your Slack `xoxp` user token. Full standalone setup docs in the skill. |
| | gmail | 🔑 | Gmail OAuth credentials. Full standalone setup docs in the skill. |
| | fireflies | 🔑 | Your Fireflies API key (`FIREFLIES_API_KEY`). Stdlib-only, no other setup. |
| multiplai-pm | job-application, landing-page, pm-jtbd-synthesis, pm-persona-codifier, pm-pr-faq, pm-strategy-memo | ✅ | Personal memory files are optional — skills ask for source material when absent. |
| multiplai-research | deep-research | ✅ | Zero-config via the Agent SDK; optional 🔑 search-provider keys widen coverage. |
| | extract-insights, interviewer | ✅ | |
| multiplai-writing | writing (all modes) | ✅ | Voice memory files optional — asks if missing. |

## What runs unattended

Installing `multiplai-context` starts a small amount of background work. Nothing
here needs a decision from you, but you should know it exists:

| What | When | Writes to |
|---|---|---|
| Deferred learning/diary extraction | Next `SessionStart` after a session ends or pre-compacts | `diary/`, `learnings/` |
| **Memory maintainer** — staleness lint, dream *proposal*, catalog refresh, `now/` rebuild | Detached at `SessionStart`, **at most once per 24h**, cheap tier | `dreams/`, catalogs, `now/` — **never `memory/`** |
| Cost collection (when `enable_costs`) | Detached at `SessionStart` | the cost ledger under `data/` |
| Nudges — dream due, config audit (**60-day** cadence), **prospective intentions** due | `SessionStart`, surfaced to you as text | nothing |

The invariant worth remembering: **`/multiplai-context:dream-remember` is the
only path that edits your memory files.** Unattended passes produce proposals and
derived files; you approve every memory write. What this background work costs
(real ledger-derived figures), whose quota it draws on, and how to switch it
off: [What it costs](plugins/multiplai-context/README.md#what-it-costs).
Details on the passes themselves in the
[plugin README](plugins/multiplai-context/README.md#proactive-maintenance).

## Repository layout

```
.
├── .claude-plugin/
│   └── marketplace.json          # marketplace manifest (lists plugins)
├── plugins/                      # each: .claude-plugin/plugin.json, skills/,
│   │                             #        README.md, CHANGELOG.md
│   ├── multiplai-context/        # hooks/ scripts/ skills/ templates/ tests/
│   ├── multiplai-pm/             # .claude-plugin/plugin.json + skills/
│   ├── multiplai-writing/
│   ├── multiplai-research/
│   ├── multiplai-dev/
│   ├── multiplai-media/
│   └── multiplai-messaging/
├── docs/                         # cross-cutting contracts (degradation, untrusted content)
├── scripts/                      # repo-level checks: lint_skills.py, scan_skills.py,
│                                 # check_changelog.py — plus tests/ for all three
├── CHANGELOG.md                  # index only; the notes live per plugin
├── CLAUDE.md                     # orientation for an agent working in this repo
├── CONTRIBUTING.md               # gates, release convention, how to contribute
├── SECURITY.md                   # reporting, and what the scanner does not cover
├── LICENSE
└── README.md                     # this file
```

Shared Python infrastructure (paths, config, logging, model client) lives in
[`multiplai-core`](https://github.com/spikelab/multiplai-core), consumed via
PEP 723 inline metadata pinned to immutable tags — its README carries an
explicit [availability guarantee](https://github.com/spikelab/multiplai-core#availability-guarantee)
(the repo stays public; release tags are never moved or deleted).

## Uninstall

`/plugin uninstall multiplai-context@multiplai`, then
`/plugin marketplace remove multiplai` (skip that second command if you're
keeping other multiplai packs — it removes the marketplace they all install
from). Your data (plain markdown under
`<workspace>/.multiplai/`) stays on your disk, yours to keep or delete —
the [plugin README](plugins/multiplai-context/README.md#uninstall) has the
full picture.

## Development

[`CLAUDE.md`](CLAUDE.md) is the orientation for anyone — human or agent —
changing this repo. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the pre-publish
gates, versioning/release convention, and test suites.

## Community

Questions, ideas, show-and-tell → [GitHub Discussions on the umbrella
repo](https://github.com/spikelab/multiplai/discussions). Bugs in a plugin
or skill → [issues here](https://github.com/spikelab/multiplai-cc-mktplace/issues).

## License

MIT — see [LICENSE](LICENSE).
