# multiplai

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

## Add the marketplace

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
```

Then install the plugins you want:

```
/plugin install multiplai-context@multiplai
/plugin install multiplai-dev@multiplai
```

## Plugins

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
| multiplai-context | *all hooks & skills* | ✅ | Needs `uv`. First session start resolves deps (allow ~1 min once). `qmd-search` additionally needs qmd installed. The hub session registry the hooks write works identically with or without docker/kit; with no multiplai hub installed the files are simply never read. |
| multiplai-dev | buildme | ✅ | Needs `uv` + network. `--skip-research` if multiplai-research absent. |
| | code-review, security-review, deepen, think, e2e-test | ✅ | e2e-test frontend mode needs `agent-browser` (npm); backend mode is plain HTTP. |
| | codebase-walkthrough, learn-stack, skill-creator | ✅ | |
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

## Repository layout

```
.
├── .claude-plugin/
│   └── marketplace.json          # marketplace manifest (lists plugins)
├── plugins/
│   ├── multiplai-context/        # hooks/ scripts/ skills/ templates/ tests/
│   ├── multiplai-pm/             # .claude-plugin/plugin.json + skills/
│   ├── multiplai-writing/
│   ├── multiplai-research/
│   ├── multiplai-dev/
│   ├── multiplai-media/
│   └── multiplai-messaging/
├── LICENSE
└── README.md                     # this file
```

## Development

See [`plugins/multiplai-context/README.md`](plugins/multiplai-context/README.md)
for plugin-specific setup, configuration, and the test suite. Shared Python
infrastructure (paths, config, logging, model client) lives in
[`multiplai-core`](https://github.com/spikelab/multiplai-core), consumed via
PEP 723 inline metadata (`uv run --no-project`).

**Versioning.** Every version bump in `.claude-plugin/marketplace.json` gets a
matching annotated git tag `<plugin>@<version>` (e.g. `multiplai-context@0.6.4`)
pointing at the commit where that version lands on `main`.

## License

MIT — see [LICENSE](LICENSE).
