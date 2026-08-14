# Changelog — index

This repository has **no release line of its own**. Every plugin here is
versioned and released independently, so the notes live next to the plugin.
Eight independent release lines interleaved in one file would tell you less
than eight files each telling one story.

Find the plugin you are installing or updating and read its changelog.

| Plugin | Version | Notes |
|--------|---------|-------|
| [`multiplai-apple`](plugins/multiplai-apple) | 0.1.0 | [CHANGELOG](plugins/multiplai-apple/CHANGELOG.md) |
| [`multiplai-context`](plugins/multiplai-context) | 0.8.1 | [CHANGELOG](plugins/multiplai-context/CHANGELOG.md) |
| [`multiplai-dev`](plugins/multiplai-dev) | 0.5.1 | [CHANGELOG](plugins/multiplai-dev/CHANGELOG.md) |
| [`multiplai-media`](plugins/multiplai-media) | 0.1.7 | [CHANGELOG](plugins/multiplai-media/CHANGELOG.md) |
| [`multiplai-messaging`](plugins/multiplai-messaging) | 0.1.2 | [CHANGELOG](plugins/multiplai-messaging/CHANGELOG.md) |
| [`multiplai-pm`](plugins/multiplai-pm) | 0.1.0 | [CHANGELOG](plugins/multiplai-pm/CHANGELOG.md) |
| [`multiplai-research`](plugins/multiplai-research) | 0.5.0 | [CHANGELOG](plugins/multiplai-research/CHANGELOG.md) |
| [`multiplai-writing`](plugins/multiplai-writing) | 0.1.0 | [CHANGELOG](plugins/multiplai-writing/CHANGELOG.md) |

The versions above are a snapshot for reading convenience. The authoritative
values are in `.claude-plugin/marketplace.json`, which is what Claude Code's
`/plugin` menu reads:

```bash
python3 -c "import json; [print(p['name'], p['version']) for p in
            json.load(open('.claude-plugin/marketplace.json'))['plugins']]"
```

Each plugin's changelog follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
is hand-written, and is enforced: the `changelog-gate` CI job fails a pull
request that changes a plugin without both a version bump and a changelog
entry. See [`CLAUDE.md`](CLAUDE.md#release-convention) for the convention and
its escape hatch.

## Repository-level changes

Changes to shared machinery — the CI workflow, `scripts/`, the cross-cutting
contracts under `docs/`, this file — are not versioned and are not listed here.
`git log` is the record for those.
