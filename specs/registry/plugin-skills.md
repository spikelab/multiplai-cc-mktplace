## ADDED Requirements

### Requirement: Skill manifest declarations
The plugin's `plugin.json` manifest MUST declare exactly three skills — `setup`, `dream`, and `health` — each with a `name`, `description`, and `entry_point` referencing a Python script under the plugin's `skills/` directory. The skill names MUST be namespaced as `multiplai:setup`, `multiplai:dream`, and `multiplai:health` when registered with Claude Code.

#### Scenario: Plugin manifest contains all three skill declarations
- **WHEN** the `plugin.json` file is parsed
- **THEN** it contains a `skills` array with exactly three entries whose `name` fields are `"setup"`, `"dream"`, and `"health"`, each with a non-empty `description` string and an `entry_point` path that resolves to an existing Python file under `skills/`

#### Scenario: Skills are invocable with plugin namespace prefix
- **WHEN** a user types `/multiplai:setup`, `/multiplai:dream`, or `/multiplai:health` in Claude Code with the plugin loaded
- **THEN** Claude Code resolves each to the corresponding skill entry point declared in the manifest

---

### Requirement: Setup skill — onboarding interview flow
The `/multiplai:setup` skill MUST conduct an interactive onboarding interview that asks the user about their identity, technical preferences, and workflow preferences, then populates memory files from the shipped starter templates.

#### Scenario: Setup skill launches interview on first invocation
- **WHEN** a user runs `/multiplai:setup` and no memory files exist in the configured memory directory
- **THEN** the skill outputs a series of questions covering identity (name, role, context), technical preferences (languages, tools, style), and general preferences (communication style, verbosity)

#### Scenario: Setup skill populates memory files from templates
- **WHEN** the onboarding interview completes with user-provided answers
- **THEN** the skill writes `me.md`, `technical-pref.md`, and `preferences.md` to the configured memory directory, each populated with the user's answers merged into the corresponding starter template structure

#### Scenario: Setup skill warns when memory files already exist
- **WHEN** a user runs `/multiplai:setup` and one or more memory files (`me.md`, `technical-pref.md`, `preferences.md`) already exist in the memory directory
- **THEN** the skill warns the user that existing files will be overwritten and requires explicit confirmation before proceeding

#### Scenario: Setup skill respects configured memory directory
- **WHEN** the user has set a custom `memory_dir` via `plugin.json` `userConfig`
- **THEN** the skill reads templates from the plugin's bundled `templates/` directory and writes populated memory files to the custom `memory_dir` path, not the default `~/.multiplai/`

#### Scenario: Setup skill uses default memory directory when unconfigured
- **WHEN** no custom `memory_dir` is configured in `userConfig`
- **THEN** the skill writes memory files to `~/.multiplai/`

---

### Requirement: Dream skill — manual Dream trigger
The `/multiplai:dream` skill MUST trigger the Dream consolidation process on demand, synthesizing learnings from the current session and recent diary entries into updated memory files.

#### Scenario: Dream skill runs consolidation successfully
- **WHEN** a user runs `/multiplai:dream` and there are accumulated learnings or diary entries to process
- **THEN** the skill invokes the Dream pipeline (extract-learnings → synthesize-now) and reports a summary of what was consolidated and which memory files were updated

#### Scenario: Dream skill uses model client for LLM reasoning
- **WHEN** the dream skill needs to synthesize learnings into memory updates
- **THEN** it calls the `ModelClient` abstraction (not a hardcoded SDK import) for all LLM inference, allowing it to work with either Agent SDK or Anthropic API key

#### Scenario: Dream skill handles no pending learnings gracefully
- **WHEN** a user runs `/multiplai:dream` and there are no new learnings or diary entries since the last consolidation
- **THEN** the skill reports that there is nothing new to consolidate and exits without modifying any memory files

#### Scenario: Dream skill reports errors without crashing
- **WHEN** the dream skill encounters an LLM call failure or file system error during consolidation
- **THEN** it reports the error to the user with a human-readable message and does not leave memory files in a partially-written state

---

### Requirement: Health skill — memory audit
The `/multiplai:health` skill MUST audit the current state of the user's memory files and plugin data, reporting completeness, staleness, and potential issues.

#### Scenario: Health skill reports memory file inventory
- **WHEN** a user runs `/multiplai:health`
- **THEN** the skill lists each expected memory file (`me.md`, `technical-pref.md`, `preferences.md`), indicating whether it exists, its size, and its last-modified timestamp

#### Scenario: Health skill detects missing memory files
- **WHEN** one or more expected memory files are missing from the memory directory
- **THEN** the health report flags each missing file and recommends running `/multiplai:setup` to create them

#### Scenario: Health skill detects stale memory files
- **WHEN** a memory file has not been modified in more than 30 days
- **THEN** the health report flags that file as potentially stale and suggests running `/multiplai:dream` to refresh it

#### Scenario: Health skill checks diary and learnings directories
- **WHEN** a user runs `/multiplai:health`
- **THEN** the report includes the number of diary entries, the number of unprocessed learnings, and the date of the last Dream consolidation (or "never" if none has occurred)

#### Scenario: Health skill works with custom directories
- **WHEN** `memory_dir` and `diary_dir` are configured to non-default paths via `userConfig`
- **THEN** the health skill audits files at those custom paths, not the defaults

#### Scenario: Health skill handles completely fresh install
- **WHEN** a user runs `/multiplai:health` before ever running `/multiplai:setup` and no memory directory exists
- **THEN** the skill reports that the plugin is not yet configured and recommends running `/multiplai:setup`, rather than raising an unhandled error

---

### Requirement: Skill entry points are executable Python scripts
Each skill's entry point MUST be a standalone Python script that can be invoked by Claude Code's skill runner. Scripts MUST use the plugin's `path_resolver` for all file locations and the `ModelClient` for all LLM calls.

#### Scenario: Skill scripts import path resolver, not hardcoded paths
- **WHEN** any skill entry point script (`skills/setup.py`, `skills/dream.py`, `skills/health.py`) is statically analyzed
- **THEN** it imports from the plugin's `paths` module for all directory and file path resolution and contains zero hardcoded absolute paths (no `/home/`, no `~/.claude/`, no literal path strings)

#### Scenario: Skill scripts import model client, not direct SDK
- **WHEN** any skill entry point script is statically analyzed
- **THEN** it imports `ModelClient` or `create_client` from the plugin's model client module and does not directly import `anthropic` or `claude_agent_sdk` at the top level

#### Scenario: Skill scripts are syntactically valid Python 3.12+
- **WHEN** each skill entry point is compiled with `py_compile`
- **THEN** compilation succeeds with no syntax errors

---

### Requirement: Skills produce structured output
Each skill MUST produce output that Claude Code can present to the user — either as markdown-formatted text returned to the conversation or as structured status messages.

#### Scenario: Setup skill outputs progress during interview
- **WHEN** the setup skill is running the onboarding interview
- **THEN** each question and each file-write confirmation is output as readable text in the conversation

#### Scenario: Dream skill outputs consolidation summary
- **WHEN** the dream skill completes a consolidation run
- **THEN** it outputs a summary listing: number of learnings processed, memory files updated, and any items skipped — formatted as markdown

#### Scenario: Health skill outputs formatted audit report
- **WHEN** the health skill completes its audit
- **THEN** it outputs a markdown-formatted report with sections for memory files status, diary status, and recommendations