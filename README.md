# multiplai

A Claude Code **plugin marketplace** — a growing suite of plugins for
memory, context, and workflow.

## Add the marketplace

```
/plugin marketplace add spikelab/multiplai
```

Then install any plugin from it:

```
/plugin install multiplai@multiplai
```

## Plugins

| Plugin | Status | Description |
|--------|--------|-------------|
| [`multiplai`](plugins/multiplai-ctx-manager) (context manager) | **Available** | Context routing, continuous learning, session awareness, and memory management. |
| `multiplai-container` | Planned | Containerized execution environment. |
| `multiplai-deepresearch` | Planned | Multi-source deep-research pipeline. |

Each plugin is self-contained under `plugins/`. The marketplace manifest
is `.claude-plugin/marketplace.json`.

## Repository layout

```
.
├── .claude-plugin/
│   └── marketplace.json          # marketplace manifest (lists plugins)
├── plugins/
│   └── multiplai-ctx-manager/    # the context-manager plugin
│       ├── .claude-plugin/plugin.json
│       ├── hooks/  scripts/  skills/  templates/  tests/
│       ├── README.md             # plugin docs
│       └── CHANGELOG.md
├── LICENSE
└── README.md                     # this file
```

## Development

See [`plugins/multiplai-ctx-manager/README.md`](plugins/multiplai-ctx-manager/README.md)
for plugin-specific setup, configuration, and the test suite.

## License

MIT — see [LICENSE](LICENSE).
