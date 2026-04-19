## ADDED Requirements

### Requirement: Ship starter memory templates
The plugin must include starter template files (`me.md`, `technical-pref.md`, `preferences.md`) in a `templates/memory/` directory within the plugin package. Each template must contain structured markdown with placeholder sections that guide the user through populating their personal context.

#### Scenario: Templates exist in plugin package
- **WHEN** the plugin repository is checked out
- **THEN** the directory `templates/memory/` contains exactly three files: `me.md`, `technical-pref.md`, `preferences.md`

#### Scenario: Templates are valid markdown
- **WHEN** each template file is read
- **THEN** it parses as valid markdown and contains at least one heading (`#` or `##`) and at least one placeholder or prompt indicating where the user should fill in information

### Requirement: me.md template structure
The `me.md` template must contain sections for personal identity, background, communication style, and any other context that helps the LLM understand who the user is.

#### Scenario: me.md contains expected sections
- **WHEN** `templates/memory/me.md` is read
- **THEN** it contains heading-level sections for at least: identity/about, background/experience, and communication style/preferences

#### Scenario: me.md does not contain developer-specific data
- **WHEN** `templates/memory/me.md` is read
- **THEN** it contains no references to "Spike", "spikelab", or any other data specific to the original developer

### Requirement: technical-pref.md template structure
The `technical-pref.md` template must contain sections for preferred languages, frameworks, tooling, coding style, and architectural preferences.

#### Scenario: technical-pref.md contains expected sections
- **WHEN** `templates/memory/technical-pref.md` is read
- **THEN** it contains heading-level sections for at least: languages, frameworks/libraries, coding style, and tooling

#### Scenario: technical-pref.md placeholders are actionable
- **WHEN** `templates/memory/technical-pref.md` is read
- **THEN** each section contains either example entries or instructional text (e.g., "List your preferred…") so the user knows what to fill in

### Requirement: preferences.md template structure
The `preferences.md` template must contain sections for general interaction preferences — verbosity, tone, workflow habits, and any behavioral directives for the LLM.

#### Scenario: preferences.md contains expected sections
- **WHEN** `templates/memory/preferences.md` is read
- **THEN** it contains heading-level sections for at least: verbosity/detail level, tone, and workflow preferences

### Requirement: Templates are copied to memory directory during onboarding
The `/multiplai:setup` skill (defined in `plugin-skills`) must copy template files from `templates/memory/` to the user's configured memory directory, preserving filenames.

#### Scenario: Fresh onboarding copies all templates
- **WHEN** the setup skill runs and the target memory directory is empty or does not exist
- **THEN** all three template files are copied to the memory directory with their original filenames

#### Scenario: Existing files are not overwritten
- **WHEN** the setup skill runs and a file with the same name (e.g., `me.md`) already exists in the target memory directory
- **THEN** the existing file is not overwritten and the user is informed that the file was skipped

#### Scenario: Partial existing files
- **WHEN** the setup skill runs and only `me.md` exists in the memory directory but `technical-pref.md` and `preferences.md` do not
- **THEN** only `technical-pref.md` and `preferences.md` are copied; `me.md` is skipped

### Requirement: Template source path resolved via path resolver
Template file locations must be resolved using the `path_resolver` module (from the `path-resolver` capability), not hardcoded absolute paths.

#### Scenario: Templates found in plugin mode
- **WHEN** the plugin is loaded by Claude Code and `CLAUDE_PLUGIN_ROOT` is set
- **THEN** the template path resolves to `$CLAUDE_PLUGIN_ROOT/templates/memory/` and all three files are found

#### Scenario: Templates found in standalone/dev mode
- **WHEN** the code runs outside of the plugin runtime (no `CLAUDE_PLUGIN_ROOT` set)
- **THEN** the template path resolves relative to the project root using the path resolver's fallback logic and all three files are found

### Requirement: Templates contain no sensitive or environment-specific content
Shipped templates must be generic and portable — no machine-specific paths, API keys, usernames, or references to the original developer's setup.

#### Scenario: No hardcoded paths in templates
- **WHEN** all template files are scanned
- **THEN** none contain absolute filesystem paths (e.g., `/Users/`, `/home/`, `C:\`)

#### Scenario: No secrets or credentials in templates
- **WHEN** all template files are scanned
- **THEN** none contain API keys, tokens, passwords, or other credential-like strings

### Requirement: Templates are UTF-8 encoded with no BOM
All template files must be UTF-8 encoded without a byte-order mark, and use LF line endings for cross-platform compatibility.

#### Scenario: Encoding validation
- **WHEN** each template file is read as raw bytes
- **THEN** the first three bytes are not `EF BB BF` (UTF-8 BOM) and the file decodes cleanly as UTF-8

#### Scenario: Line ending validation
- **WHEN** each template file is read as raw bytes
- **THEN** the file contains no `\r\n` (CRLF) sequences