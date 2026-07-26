# multiplai-cc-mktplace

**The Multiplai Claude Code plugin marketplace** — seven installable plugin
packs, one `/plugin marketplace add` away.

> Part of the **[Multiplai suite](https://github.com/spikelab/multiplai)** — what the suite is, how the five repos fit together, and which part you need.

A Claude Code **plugin marketplace** — memory, context, and a themed skill
library for a full working environment. Designed to pair with
[`multiplai-kit`](https://github.com/spikelab/multiplai-kit) (launcher +
sandboxed container + workspace conventions).

**Requirements.** One hard prerequisite: the context-plugin hooks and the
Python-backed skills run via [`uv`](https://docs.astral.sh/uv/) — install it
first. The kit (launcher, container, workspace layout) is **optional**:
everything below tells you exactly what runs where. Per-skill details in the
[compatibility matrix](#compatibility-matrix); the rules skills follow when a
capability is missing are in
[`docs/degradation-contract.md`](docs/degradation-contract.md).

**Two cross-cutting contracts** apply across plugins and are worth reading once:

- [`docs/untrusted-content.md`](docs/untrusted-content.md) — how skills that
  ingest text somebody else wrote (web pages, email, Slack, browser pages, log
  lines) fence and defang it, and why fenced text is **data, never
  instructions**.
- [`docs/degradation-contract.md`](docs/degradation-contract.md) — what a skill
  does when a prerequisite is missing.

## Add the marketplace

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
```

Then install the plugins you want:

```
/plugin install multiplai-context@multiplai
/plugin install multiplai-dev@multiplai
```

The `@multiplai` suffix is the **marketplace's** name (`name` in
`.claude-plugin/marketplace.json`), not the repository's and not the
[umbrella repo](https://github.com/spikelab/multiplai)'s — it is what Claude
Code shows in the `/plugin` menu, so it stays as it is.

## Plugins

**Seven plugins, 43 skills** (context 12, dev 14, pm 6, media 5, research 3,
messaging 2, writing 1). This repo is the authoritative source for that number;
derive it rather than quoting it from memory:

```bash
ls -d plugins/*/skills/*/ | wc -l     # 43 on 2026-07-26
```

Each plugin is versioned and released on its own, and each carries its own
release notes — see [`CHANGELOG.md`](CHANGELOG.md) for the index.

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
| multiplai-context | *all hooks & skills* | ✅ | Needs `uv`. First session start resolves deps (allow ~1 min once). `qmd-search` additionally needs qmd installed. The hub session registry the hooks write works identically with or without docker/kit; with no multiplai hub installed the files are simply never read. Session start also launches a **detached memory maintainer** (24h gate, never writes to memory) — see [What runs unattended](#what-runs-unattended). |
| multiplai-dev | buildme | ✅ | Needs `uv` + network. `--skip-research` if multiplai-research absent. |
| | code-review, security-review, deepen, think, e2e-test | ✅ | e2e-test frontend mode needs `agent-browser` (npm); backend mode is plain HTTP. |
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
derived files; you approve every memory write. Details in the
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
├── SECURITY.md                   # reporting, and what the scanner does not cover
├── LICENSE
└── README.md                     # this file
```

## Development

[`CLAUDE.md`](CLAUDE.md) is the orientation for anyone — human or agent —
changing this repo: where things live, which gates must pass, and the release
convention.

See [`plugins/multiplai-context/README.md`](plugins/multiplai-context/README.md)
for plugin-specific setup, configuration, and the test suite. Shared Python
infrastructure (paths, config, logging, model client) lives in
[`multiplai-core`](https://github.com/spikelab/multiplai-core), consumed via
PEP 723 inline metadata (`uv run --no-project`).

### Pre-publish checks

Installing a plugin copies these files onto someone else's machine, where they
run with that person's credentials — and nobody reads every line first. Two
repo-level checks stand in for that reading; both are deterministic and offline:

```bash
uv run --no-project scripts/lint_skills.py     # structure: frontmatter, script refs, absolute paths
uv run --no-project scripts/scan_skills.py     # security: what a skill does vs what its SKILL.md says
```

- `lint_skills.py` catches malformed frontmatter, unknown `model`/`effort`
  values (silently ignored at runtime, so the skill quietly runs on the wrong
  tier), SKILL.md references to renamed scripts, and machine-specific absolute
  paths baked into shipped files. Exit 1 on any error.
- `scan_skills.py` reports declared-vs-actual behaviour. **FAIL** (blocks CI) for
  patterns with no legitimate use in a shipped skill — `curl | bash`,
  base64-decode-and-execute; **WARN** for behaviour that is fine when declared
  and suspicious when not — network calls or credential reads absent from the
  SKILL.md. It is a static scan, not a sandbox: it raises the cost of hiding
  behaviour, it does not make hiding impossible.

Individual skills can additionally ship a `CONTRACT.md` (assertions on interface
*shape*, not on values) run by
`plugins/multiplai-dev/skills/skill-creator/scripts/promote_skill.py --contract`.

To report something you find, or to understand what the scanner does *not*
cover, see [`SECURITY.md`](SECURITY.md).

**Versioning.** Every version bump in `.claude-plugin/marketplace.json` gets a
matching annotated git tag `<plugin>@<version>` (e.g. `multiplai-context@0.6.4`)
pointing at the commit where that version lands on `main`, **and** an entry in
`plugins/<plugin>/CHANGELOG.md`. The `changelog-gate` CI job enforces the bump
and the entry on every pull request
([`scripts/check_changelog.py`](scripts/check_changelog.py), runnable locally);
the tag is cut by hand at release. Contributor-facing detail, including the
gate's escape hatch, is in [`CLAUDE.md`](CLAUDE.md#release-convention).

## License

MIT — see [LICENSE](LICENSE).
