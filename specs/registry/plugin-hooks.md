## ADDED Requirements

### Requirement: SessionStart hook registration
The plugin's `hooks.json` must declare a `SessionStart` hook that invokes the session-lifecycle Python script when a Claude Code session begins.

#### Scenario: SessionStart hook is present in hooks.json
- **WHEN** the plugin's `hooks.json` file is parsed
- **THEN** it contains a hook entry with event type `SessionStart` that points to a valid Python script path within the plugin directory

#### Scenario: SessionStart hook script exists and is executable
- **WHEN** the `SessionStart` hook entry references a script path
- **THEN** that script file exists in the plugin directory structure and is a valid Python file

### Requirement: SessionStart venv bootstrap
The `SessionStart` hook must auto-create and populate a Python virtual environment on first session if one does not already exist, installing `anthropic` and `pyyaml` dependencies.

#### Scenario: First session with no existing venv
- **WHEN** the `SessionStart` hook fires and no venv directory exists under `$CLAUDE_PLUGIN_DATA`
- **THEN** a Python 3.12+ venv is created at `$CLAUDE_PLUGIN_DATA/venv/`, `pip install` runs with `anthropic>=0.40.0` and `pyyaml>=6.0`, and the hook completes successfully

#### Scenario: Subsequent session with existing venv
- **WHEN** the `SessionStart` hook fires and a venv directory already exists under `$CLAUDE_PLUGIN_DATA` with all required packages installed
- **THEN** the venv creation and pip install steps are skipped, and the hook completes without reinstalling dependencies

#### Scenario: Venv creation fails due to missing Python
- **WHEN** the `SessionStart` hook fires and `python3` is not available on `$PATH`
- **THEN** the hook exits with a non-zero exit code and emits a diagnostic message indicating Python 3.12+ is required

#### Scenario: pip install fails (network error or package resolution)
- **WHEN** the `SessionStart` hook fires, the venv is created, but `pip install` fails
- **THEN** the hook exits with a non-zero exit code and emits a diagnostic message describing the installation failure, and the partially-created venv is not left in an inconsistent state (either cleaned up or marked incomplete)

### Requirement: UserPromptSubmit hook registration
The plugin's `hooks.json` must declare a `UserPromptSubmit` hook that invokes the context-router Python script before each user prompt is processed.

#### Scenario: UserPromptSubmit hook is present in hooks.json
- **WHEN** the plugin's `hooks.json` file is parsed
- **THEN** it contains a hook entry with event type `UserPromptSubmit` that points to the context-router Python script

#### Scenario: UserPromptSubmit hook receives user input
- **WHEN** the `UserPromptSubmit` hook fires
- **THEN** the hook script receives the user's prompt text via the standard Claude Code hook input mechanism (stdin JSON or environment variable, per Claude Code hook protocol)

### Requirement: Stop hook registration
The plugin's `hooks.json` must declare a `Stop` hook that invokes the extract-learnings Python script when Claude Code finishes a response.

#### Scenario: Stop hook is present in hooks.json
- **WHEN** the plugin's `hooks.json` file is parsed
- **THEN** it contains a hook entry with event type `Stop` that points to the extract-learnings Python script

#### Scenario: Stop hook does not include git_stage behavior
- **WHEN** the `Stop` hook script is inspected
- **THEN** it contains no calls to `git_stage()`, `git add`, `git commit`, or any git staging/commit logic

### Requirement: SessionEnd hook registration
The plugin's `hooks.json` must declare a `SessionEnd` hook that invokes session cleanup and learning consolidation when a Claude Code session ends.

#### Scenario: SessionEnd hook is present in hooks.json
- **WHEN** the plugin's `hooks.json` file is parsed
- **THEN** it contains a hook entry with event type `SessionEnd` that points to a valid Python script within the plugin directory

### Requirement: PreCompact hook registration
The plugin's `hooks.json` must declare a `PreCompact` hook that invokes a synthesis or context-preservation script before Claude Code compacts conversation context.

#### Scenario: PreCompact hook is present in hooks.json
- **WHEN** the plugin's `hooks.json` file is parsed
- **THEN** it contains a hook entry with event type `PreCompact` that points to a valid Python script within the plugin directory

### Requirement: hooks.json schema validity
The `hooks.json` file must conform to the Claude Code plugin hooks schema so that Claude Code can parse and register all hooks without errors.

#### Scenario: hooks.json is valid JSON
- **WHEN** the `hooks.json` file is loaded with a JSON parser
- **THEN** it parses without errors

#### Scenario: All hook entries have required fields
- **WHEN** each hook entry in `hooks.json` is inspected
- **THEN** every entry contains at minimum an event type and a command/script reference, with no missing required fields per the Claude Code hook specification

#### Scenario: No duplicate event registrations for the same script
- **WHEN** the `hooks.json` file is parsed
- **THEN** there are no duplicate entries registering the same script for the same event type

### Requirement: Hook scripts use plugin venv Python
All hook scripts must execute using the Python interpreter from the plugin's venv (bootstrapped during SessionStart), not the system Python.

#### Scenario: Hook script invocation uses venv Python path
- **WHEN** any hook command in `hooks.json` is examined
- **THEN** it either references `$CLAUDE_PLUGIN_DATA/venv/bin/python` directly, or uses a wrapper that activates the plugin venv before invoking Python

#### Scenario: Hook runs after venv bootstrap
- **WHEN** the `UserPromptSubmit`, `Stop`, `SessionEnd`, or `PreCompact` hook fires and the venv exists
- **THEN** the script runs with the venv's Python interpreter, which has `anthropic` and `pyyaml` importable

#### Scenario: Non-SessionStart hook fires before venv exists
- **WHEN** a `UserPromptSubmit`, `Stop`, `SessionEnd`, or `PreCompact` hook fires but no venv has been bootstrapped yet
- **THEN** the hook exits gracefully with a warning message (not a stack trace) indicating that the session must be restarted or `/multiplai:setup` must be run first

### Requirement: Hook scripts resolve paths via path-resolver
All hook scripts must use the `path-resolver` module (central `paths.*` API) for file location resolution rather than hardcoding paths.

#### Scenario: No hardcoded home directory paths in hook scripts
- **WHEN** all Python hook scripts are searched for string literals matching `~/.multiplai`, `/home/`, or absolute user-directory paths
- **THEN** zero matches are found — all path resolution goes through the `paths` module

#### Scenario: Hook scripts import path-resolver
- **WHEN** any hook Python script that accesses the file system is inspected
- **THEN** it imports from the path-resolver module (e.g., `from multiplai.paths import ...` or equivalent)

### Requirement: Hook scripts use model-client abstraction
All hook scripts that make LLM calls must use the `ModelClient` abstraction rather than importing `claude_agent_sdk` or `anthropic` directly.

#### Scenario: No direct SDK imports in hook scripts
- **WHEN** all Python hook scripts are searched for `import claude_agent_sdk`, `from claude_agent_sdk`, `import anthropic`, or `from anthropic`
- **THEN** zero matches are found in the hook scripts themselves (these imports exist only in the model-client module)

#### Scenario: Hook scripts use create_client factory
- **WHEN** a hook script needs to make an LLM call
- **THEN** it obtains a client via the `create_client()` factory function from the model-client module

### Requirement: Exactly five hook event types are registered
The plugin must register hooks for exactly the five lifecycle events specified: `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, and `PreCompact`.

#### Scenario: Count of distinct event types
- **WHEN** all unique event types in `hooks.json` are collected
- **THEN** there are exactly five distinct event types: `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, `PreCompact`

#### Scenario: No unexpected event types
- **WHEN** all event types in `hooks.json` are inspected
- **THEN** none fall outside the set `{SessionStart, UserPromptSubmit, Stop, SessionEnd, PreCompact}`