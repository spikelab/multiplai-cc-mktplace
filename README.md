# multiplai

A Claude Code **plugin marketplace** — memory, context, and a themed skill
library for a full working environment. Designed to pair with
[`multiplai-kit`](https://github.com/spikelab/multiplai-kit) (launcher +
sandboxed container + workspace conventions).

**Requirements & compatibility.** The context-plugin hooks and the
Python-backed skills (buildme, deep-research, render_report) run via
[`uv`](https://docs.astral.sh/uv/) — install it first. Many skills assume the
kit's workspace layout (`INBOX/`, `RESOURCES/`, `PROJECTS/plans/`, the memory
files); those work best inside a kit workspace. Skills that bridge to a macOS
host over SSH (transcribe, screen-demo, swift-build, host-browser) need the
`multiplai-kit` container environment. The rest work in a plain Claude Code
install.

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

Skill-by-skill index: see each plugin's `skills/` directory. Some
`multiplai-media`/`multiplai-dev` skills (transcribe, screen-demo,
swift-build, host-browser) bridge to a macOS host over SSH and are designed
for the `multiplai-kit` container environment.

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
│   └── multiplai-media/
├── LICENSE
└── README.md                     # this file
```

## Development

See [`plugins/multiplai-context/README.md`](plugins/multiplai-context/README.md)
for plugin-specific setup, configuration, and the test suite. Shared Python
infrastructure (paths, config, logging, model client) lives in
[`multiplai-core`](https://github.com/spikelab/multiplai-core), consumed via
PEP 723 inline metadata (`uv run --no-project`).

## License

MIT — see [LICENSE](LICENSE).
