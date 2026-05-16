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
Manual AutoDream trigger. By default runs in **report mode** — generates a proposal file in `.multiplai/inbox/` for you to review without touching your memory files. Pass `--auto` to apply changes directly instead.

### `/multiplai:process-learnings`
Human-in-the-loop learning review. Checks `.multiplai/inbox/` for a pending AutoDream proposal (generating one if none exists), then walks through proposed memory updates grouped by target file. You approve, reject, or modify each change before anything is written. Cleans up processed learning files on completion.

### `/multiplai:health`
Memory audit. Reports the completeness and staleness of your memory files, diary entries, and plugin data. Flags missing or stale files and recommends actions.

### `/multiplai:refresh-catalogs`
Regenerate catalog indexes for memory, diary, skills, and resources. Supports `--force`, `--dry-run`, and `--only` flags. Run after adding or renaming memory files.

## Where your memory lives

By default, `memory_dir` is `~/.multiplai/memory` — a hidden directory with
no version control. Over time your memory files accumulate — learnings,
preference updates, corrections — and a single bad write can erase state
that took months to build.

**Recommended: point `memory_dir` at a git repository.** `/multiplai:setup`
detects whether your chosen `memory_dir` is inside a git repo and offers
to `git init` it (with a minimal `.gitignore` and initial commit). Once
tracked, `/multiplai:dream` auto-commits memory changes after each
consolidation pass so you always have a recoverable history.

Two common layouts:

- **Dedicated repo:** Keep memory in its own repo (`~/memory/` or
  similar) and sync it across machines via a personal git remote. Keeps
  memory portable and independent of any workspace.
- **Workspace subdirectory:** Point `memory_dir` at
  `<your-workspace>/MEMORY/` and let the workspace repo track it. Handy
  when memory evolves alongside your projects.

Either works. The plugin doesn't care — it only needs `memory_dir` to be
inside *some* git working tree for auto-commit to kick in.

If `memory_dir` isn't a git repo, auto-commit is skipped with a log
warning and everything else keeps working. No forced lifecycle.

## Architecture

### Plugin layout

```
multiplai-plugin/
├── .claude-plugin/
│   └── plugin.json          # CC plugin manifest
├── hooks/
│   └── hooks.json           # CC-native hook registrations
├── scripts/
│   ├── lib/                 # Shared libraries (paths, model client, venv guard)
│   ├── venv_bootstrap.py    # First-run venv setup (uv preferred, pip fallback)
│   ├── session_start.py     # SessionStart hook
│   ├── context_manager.py   # UserPromptSubmit — context routing
│   ├── session_stop.py      # Stop hook — diary + learnings capture
│   ├── session_end.py       # SessionEnd hook
│   ├── pre_compact.py       # PreCompact hook
│   ├── autodream.py         # Learnings consolidation (report or --auto)
│   └── generate_catalog.py  # Catalog index builder
└── skills/
    ├── setup/SKILL.md
    ├── dream/SKILL.md
    ├── process-learnings/SKILL.md
    ├── health/SKILL.md
    └── refresh-catalogs/SKILL.md
```

### Key libraries

- **Path resolver** (`scripts/lib/paths.py`): Centralized path resolution from plugin environment variables with standalone fallbacks. All runtime state (`diary/`, `learnings/`, `inbox/`, `memory/`) resolves through here.
- **Model client** (`scripts/lib/model_client.py`): Abstract LLM interface supporting Agent SDK (zero-config) and Anthropic API key fallback.
- **Venv bootstrap** (`scripts/venv_bootstrap.py`): Automatic Python virtual environment setup on first session. Prefers `uv` if available, falls back to `python -m venv` + `pip`.

### Learning lifecycle

1. **Capture** — `session_stop.py` writes raw learnings to `.multiplai/learnings/` after each session.
2. **Consolidate** — `autodream.py` (via `/multiplai:dream` or nightly) reads learnings + diary, generates a proposal in `.multiplai/inbox/`.
3. **Review & apply** — `/multiplai:process-learnings` walks through the proposal with you and applies approved edits to memory files.

## License

MIT
