## Why

The multiplai system — context routing, continuous learning, session awareness — exists today as a personal Claude Code kit (~3,500 lines across hooks and skills in `claude-code-multiplai`). It works, but it's welded to one developer's dotfiles. No one else can install it. No one else can benefit from it.

Claude Code now supports a plugin distribution model: structured repos with manifest files, hook declarations, skill definitions, and managed user configuration. This is the mechanism to turn a battle-tested personal toolkit into a product that any Claude Code user can install with a single command.

Phase 1 (assumed complete) modernized the internals — renamed files, ported bash to Python, added captain's log synthesis and AutoDream consolidation. Phase 2 takes those cleaned-up internals and repackages them into the plugin structure. The work is primarily structural: new directory layout, centralized path resolution, abstracted model client, plugin manifests, and hook declarations. The core logic doesn't change — it gets ported with systematic find-and-replace of hardcoded paths and direct SDK imports.

Why now: the plugin format is stable, Phase 1 outputs are ready to port, and the longer the kit stays as personal dotfiles, the more drift accumulates between "what works for Spike" and "what could work for anyone."

## What Changes

A new standalone repository (`PROJECTS/multiplai-plugin/`) is created with the Claude Code plugin structure. All hook scripts are ported from `claude-code-multiplai` with three systematic transformations: (1) hardcoded path constants replaced with a central `path_resolver` module that reads plugin environment variables, (2) direct `claude_agent_sdk` imports replaced with an abstract `ModelClient` that tries Agent SDK (OAuth) first and falls back to Anthropic API key, (3) git-specific and kit-specific logic stripped (no `git_stage`, no resource/skill catalog routing, no workspace scaffolding).

The plugin ships three skills (`setup`, `dream`, `health`), starter memory templates, and a venv-bootstrap hook that auto-installs Python dependencies on first session. The only PyPI dependencies are `anthropic` and `pyyaml` — the Agent SDK comes from the Claude Code host runtime.

## Capabilities

### New Capabilities

- `plugin-scaffold`: Directory structure, manifests (`plugin.json`, `marketplace.json`), `hooks.json`, LICENSE, README, CHANGELOG — everything needed for Claude Code to recognize and load the plugin.
- `path-resolver`: Central `paths.*` API that resolves all file locations from plugin environment variables (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PLUGIN_OPTION_*`) in plugin mode, with fallback to standalone conventions for local development.
- `model-client`: Abstract `ModelClient` interface with `AgentSDKClient` (OAuth, zero-config) and `AnthropicAPIClient` (API key fallback) implementations, plus `create_client()` factory. Decouples all LLM calls from a specific provider.
- `plugin-hooks`: Hook registrations for `SessionStart` (with venv bootstrap), `UserPromptSubmit`, `Stop`, `SessionEnd`, and `PreCompact` — wiring the plugin's Python scripts into Claude Code's lifecycle.
- `plugin-skills`: Three slash commands — `/multiplai:setup` (onboarding interviewer that populates memory files from templates), `/multiplai:dream` (manual AutoDream trigger), `/multiplai:health` (memory audit).
- `memory-templates`: Starter template files (`me.md`, `technical-pref.md`, `preferences.md`) shipped with the plugin and copied during onboarding.
- `script-port`: Ported versions of context-router, session-lifecycle, extract-learnings, autodream, synthesize-now, generate-catalog, model-resolver, log-utils, and supporting config files — adapted for plugin path resolution and model client abstraction.

### Modified Capabilities

_(None — this is a new repo. The source files in `claude-code-multiplai` are not modified.)_

## Impact

- **New repository**: `PROJECTS/multiplai-plugin/` with its own git history, independent of `claude-code-multiplai`.
- **Dependencies**: `anthropic>=0.40.0` and `pyyaml>=6.0` installed into a plugin-managed venv. `claude-agent-sdk` imported at runtime from the host — not vendored.
- **User configuration**: Three optional `userConfig` fields exposed via `plugin.json` — `memory_dir`, `diary_dir`, `anthropic_api_key` (sensitive, only needed without Agent SDK).
- **File system**: Plugin writes to `$CLAUDE_PLUGIN_DATA` (venv, catalogs, logs, dream state) and user-configured directories (`~/.multiplai/` by default) for memory files, diary, and learnings.
- **Removed from port**: `git_stage()` in extract-learnings, resource/skill catalog routing in context-router, all bash wrapper scripts, auto-commit logic. These are kit-specific behaviors that don't belong in a distributed plugin.
- **Distribution**: Once verified with `claude --plugin-dir`, publishable to GitHub (`spikelab/multiplai`) and submittable to the Anthropic plugin marketplace.