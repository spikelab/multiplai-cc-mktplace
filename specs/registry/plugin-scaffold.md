## ADDED Requirements

### Requirement: Plugin directory structure follows Claude Code plugin layout
The plugin repository at `PROJECTS/multiplai-plugin/` MUST contain the required directory structure that Claude Code expects for plugin discovery and loading.

#### Scenario: Top-level directories exist
- **WHEN** the plugin repository is created
- **THEN** the following directories exist: `src/`, `skills/`, `hooks/`, `templates/`, and `tests/`

#### Scenario: Python package structure under src
- **WHEN** inspecting `src/multiplai/`
- **THEN** it contains an `__init__.py` file making it an importable Python package

---

### Requirement: plugin.json manifest is valid and complete
The file `plugin.json` at the repository root MUST conform to the Claude Code plugin manifest schema, declaring the plugin's identity, version, dependencies, user configuration fields, and entry points.

#### Scenario: Required top-level fields are present
- **WHEN** `plugin.json` is parsed as JSON
- **THEN** it contains the fields `name`, `version`, `description`, `author`, `license`, `engines`, and `entrypoints`

#### Scenario: Plugin name follows naming convention
- **WHEN** reading the `name` field from `plugin.json`
- **THEN** the value is `"multiplai"`

#### Scenario: Version follows semver
- **WHEN** reading the `version` field from `plugin.json`
- **THEN** the value matches the pattern `^\d+\.\d+\.\d+$` (e.g., `"0.1.0"`)

#### Scenario: User configuration fields are declared
- **WHEN** reading the `userConfig` section of `plugin.json`
- **THEN** it declares three optional fields: `memory_dir` (string, default `"~/.multiplai/memory"`), `diary_dir` (string, default `"~/.multiplai/diary"`), and `anthropic_api_key` (string, marked `sensitive: true`, no default)

#### Scenario: anthropic_api_key is marked sensitive
- **WHEN** reading the `anthropic_api_key` entry in `userConfig`
- **THEN** the field has `"sensitive": true` so that Claude Code redacts it from logs and UI

#### Scenario: Engines field specifies minimum Claude Code version
- **WHEN** reading the `engines` field from `plugin.json`
- **THEN** it specifies a `claude-code` version constraint (e.g., `">=1.0.0"`)

#### Scenario: Malformed plugin.json is caught
- **WHEN** `plugin.json` contains invalid JSON (e.g., trailing comma)
- **THEN** `claude --plugin-dir PROJECTS/multiplai-plugin/` reports a parse error and does not load the plugin

---

### Requirement: marketplace.json provides distribution metadata
The file `marketplace.json` at the repository root MUST contain metadata required for Anthropic plugin marketplace submission.

#### Scenario: Required marketplace fields are present
- **WHEN** `marketplace.json` is parsed as JSON
- **THEN** it contains the fields `name`, `displayName`, `description`, `author`, `repository`, `categories`, and `keywords`

#### Scenario: Repository URL points to GitHub
- **WHEN** reading the `repository` field from `marketplace.json`
- **THEN** the value is `"https://github.com/<owner>/<repo>"` or follows the `https://github.com/<owner>/<repo>` pattern

#### Scenario: Categories is a non-empty array
- **WHEN** reading the `categories` field from `marketplace.json`
- **THEN** it is an array with at least one string entry

---

### Requirement: hooks.json declares all lifecycle hooks
The file `hooks.json` at the repository root MUST register hook scripts for all five lifecycle events the plugin supports.

#### Scenario: All required hook events are registered
- **WHEN** `hooks.json` is parsed as JSON
- **THEN** it declares hooks for `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, and `PreCompact`

#### Scenario: Each hook entry specifies a script path
- **WHEN** inspecting any hook entry in `hooks.json`
- **THEN** it contains a `script` field whose value is a relative path to a file that exists in the repository (e.g., `"hooks/session_start.py"`)

#### Scenario: SessionStart hook includes venv bootstrap
- **WHEN** reading the `SessionStart` hook entry
- **THEN** the entry references a script that handles venv bootstrapping (the script path exists and the referenced file contains venv creation or dependency installation logic)

#### Scenario: Hook script paths resolve to existing files
- **WHEN** iterating over all `script` values in `hooks.json`
- **THEN** every referenced file exists at the specified relative path within the repository

---

### Requirement: LICENSE file is present
The repository MUST include a LICENSE file at the root.

#### Scenario: LICENSE file exists and is non-empty
- **WHEN** checking the repository root
- **THEN** a file named `LICENSE` exists and contains at least 10 lines of text

#### Scenario: License type matches plugin.json
- **WHEN** reading the `license` field from `plugin.json`
- **THEN** the LICENSE file content corresponds to the declared license type (e.g., if `"MIT"` is declared, the LICENSE file contains the MIT license text)

---

### Requirement: README.md provides installation and usage documentation
The repository MUST include a README.md at the root with sections covering installation, configuration, and available skills.

#### Scenario: README contains installation instructions
- **WHEN** reading `README.md`
- **THEN** it includes a section with the heading "Install" or "Installation" that contains the command `claude --plugin-dir` or equivalent plugin installation instruction

#### Scenario: README documents user configuration options
- **WHEN** reading `README.md`
- **THEN** it describes the three `userConfig` fields (`memory_dir`, `diary_dir`, `anthropic_api_key`) with their defaults and purpose

#### Scenario: README lists available skills
- **WHEN** reading `README.md`
- **THEN** it documents the three slash commands: `/multiplai:setup`, `/multiplai:dream`, and `/multiplai:health`

---

### Requirement: CHANGELOG.md exists with initial entry
The repository MUST include a CHANGELOG.md at the root tracking release history.

#### Scenario: CHANGELOG has an initial version entry
- **WHEN** reading `CHANGELOG.md`
- **THEN** it contains at least one version heading matching the version in `plugin.json` (e.g., `## 0.1.0`) with a date and summary of initial capabilities

---

### Requirement: Plugin loads successfully via claude --plugin-dir
The complete scaffold MUST be loadable by Claude Code's plugin discovery mechanism without errors.

#### Scenario: Plugin discovery succeeds with valid scaffold
- **WHEN** running `claude --plugin-dir PROJECTS/multiplai-plugin/` against the completed scaffold
- **THEN** Claude Code loads the plugin without reporting any manifest, hook, or skill registration errors

#### Scenario: Plugin discovery reports missing manifest gracefully
- **WHEN** `plugin.json` is temporarily removed and `claude --plugin-dir PROJECTS/multiplai-plugin/` is run
- **THEN** Claude Code reports that no valid plugin was found at the specified path (does not crash or silently ignore)

---

### Requirement: Scaffold files contain no hardcoded user-specific paths
All scaffold files (manifests, hook declarations, README) MUST be portable — free of paths specific to any single developer's machine.

#### Scenario: No references to home directory literals
- **WHEN** searching all files in the repository for patterns like `/home/spike`, `/Users/spike`, or absolute paths outside of documented defaults
- **THEN** zero matches are found

#### Scenario: Default paths use tilde or environment variable notation
- **WHEN** inspecting default values in `plugin.json` `userConfig`
- **THEN** paths use `~` prefix (e.g., `~/.multiplai/memory`) rather than absolute paths