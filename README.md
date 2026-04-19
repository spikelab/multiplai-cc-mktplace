# Multiplai

Personal Claude Code plugin for context routing, continuous learning, session awareness, and memory management.

## Installation

```bash
claude --plugin-dir ./multiplai-plugin
```

Or clone and point to the local directory:

```bash
git clone https://github.com/spikelab/multiplai.git
claude --plugin-dir ./multiplai
```

## Configuration

The plugin supports three user configuration options via `plugin.json` `userConfig`:

### `memory_dir`
- **Type:** string
- **Default:** `~/.multiplai/memory`
- **Description:** Directory where memory files (me.md, technical-pref.md, preferences.md) are stored. Set via `CLAUDE_PLUGIN_OPTION_memory_dir` environment variable.

### `diary_dir`
- **Type:** string
- **Default:** `~/.multiplai/diary`
- **Description:** Directory for diary entries and session logs. Set via `CLAUDE_PLUGIN_OPTION_diary_dir` environment variable.

### `anthropic_api_key`
- **Type:** string (sensitive)
- **Default:** none
- **Description:** Anthropic API key for fallback when Agent SDK is unavailable. Set via `CLAUDE_PLUGIN_OPTION_anthropic_api_key` environment variable. This value is marked sensitive and will be redacted from logs and UI.

## Skills

### `/multiplai:setup`
Onboarding interviewer. Conducts an interactive interview to populate your memory files from starter templates. Asks about your identity, technical preferences, and workflow preferences, then writes `me.md`, `technical-pref.md`, and `preferences.md` to your memory directory.

### `/multiplai:dream`
Manual AutoDream trigger. Runs the consolidation pipeline (extract learnings → synthesize) on demand. Processes accumulated learnings and diary entries, updating memory files with new insights.

### `/multiplai:health`
Memory audit. Reports the completeness and staleness of your memory files, diary entries, and plugin data. Flags missing or stale files and recommends actions.

## Architecture

The plugin uses:
- **Path resolver** (`scripts/lib/paths.py`): Centralized path resolution from plugin environment variables with standalone fallbacks.
- **Model client** (`scripts/lib/model_client.py`): Abstract LLM interface supporting Agent SDK (zero-config) and Anthropic API key fallback.
- **Venv bootstrap** (`scripts/venv_bootstrap.py`): Automatic Python virtual environment setup on first session.

## License

MIT
