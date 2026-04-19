## ADDED Requirements

### Requirement: Plugin environment variable resolution
The `paths` module MUST resolve file locations from plugin environment variables (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PLUGIN_OPTION_memory_dir`, `CLAUDE_PLUGIN_OPTION_diary_dir`) when running inside a Claude Code plugin context.

#### Scenario: Resolve plugin root from environment
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set to `/home/user/.claude/plugins/multiplai`
- **THEN** `paths.plugin_root()` returns `Path("/home/user/.claude/plugins/multiplai")`

#### Scenario: Resolve plugin data directory from environment
- **WHEN** `CLAUDE_PLUGIN_DATA` is set to `/home/user/.claude/plugins/multiplai/data`
- **THEN** `paths.plugin_data()` returns `Path("/home/user/.claude/plugins/multiplai/data")`

#### Scenario: Resolve user-configured memory directory
- **WHEN** `CLAUDE_PLUGIN_OPTION_memory_dir` is set to `/home/user/custom-memory`
- **THEN** `paths.memory_dir()` returns `Path("/home/user/custom-memory")`

#### Scenario: Resolve user-configured diary directory
- **WHEN** `CLAUDE_PLUGIN_OPTION_diary_dir` is set to `/home/user/custom-diary`
- **THEN** `paths.diary_dir()` returns `Path("/home/user/custom-diary")`

---

### Requirement: Standalone fallback resolution
When plugin environment variables are absent, the `paths` module MUST fall back to standalone conventions rooted at `~/.multiplai/` for local development and non-plugin usage.

#### Scenario: Fallback memory directory when no plugin env vars set
- **WHEN** `CLAUDE_PLUGIN_OPTION_memory_dir` is not set AND `CLAUDE_PLUGIN_ROOT` is not set
- **THEN** `paths.memory_dir()` returns `Path.home() / ".multiplai" / "memory"`

#### Scenario: Fallback diary directory when no plugin env vars set
- **WHEN** `CLAUDE_PLUGIN_OPTION_diary_dir` is not set AND `CLAUDE_PLUGIN_ROOT` is not set
- **THEN** `paths.diary_dir()` returns `Path.home() / ".multiplai" / "diary"`

#### Scenario: Fallback plugin data directory
- **WHEN** `CLAUDE_PLUGIN_DATA` is not set
- **THEN** `paths.plugin_data()` returns `Path.home() / ".multiplai" / "data"`

#### Scenario: Fallback plugin root directory
- **WHEN** `CLAUDE_PLUGIN_ROOT` is not set
- **THEN** `paths.plugin_root()` returns `Path.home() / ".multiplai"`

---

### Requirement: Plugin mode detection
The `paths` module MUST expose an `is_plugin_mode()` function that reports whether the module is resolving paths from plugin environment variables or standalone fallbacks.

#### Scenario: Plugin mode detected when CLAUDE_PLUGIN_ROOT is set
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set to any non-empty value
- **THEN** `paths.is_plugin_mode()` returns `True`

#### Scenario: Standalone mode detected when CLAUDE_PLUGIN_ROOT is absent
- **WHEN** `CLAUDE_PLUGIN_ROOT` is not set or is empty
- **THEN** `paths.is_plugin_mode()` returns `False`

---

### Requirement: Derived path accessors for known file locations
The `paths` module MUST provide accessors for all known file locations used by the plugin — venv, catalogs, logs, dream state, learnings, templates, and scripts — derived from the base directories.

#### Scenario: Venv path derived from plugin data
- **WHEN** `paths.plugin_data()` returns `/data`
- **THEN** `paths.venv_dir()` returns `Path("/data/venv")`

#### Scenario: Catalogs path derived from plugin data
- **WHEN** `paths.plugin_data()` returns `/data`
- **THEN** `paths.catalogs_dir()` returns `Path("/data/catalogs")`

#### Scenario: Logs path derived from plugin data
- **WHEN** `paths.plugin_data()` returns `/data`
- **THEN** `paths.logs_dir()` returns `Path("/data/logs")`

#### Scenario: Dream state path derived from plugin data
- **WHEN** `paths.plugin_data()` returns `/data`
- **THEN** `paths.dream_state_file()` returns `Path("/data/dream_state.yaml")`

#### Scenario: Learnings path derived from memory directory
- **WHEN** `paths.memory_dir()` returns `/mem`
- **THEN** `paths.learnings_file()` returns `Path("/mem/learnings.md")`

#### Scenario: Templates path derived from plugin root
- **WHEN** `paths.plugin_root()` returns `/plugin`
- **THEN** `paths.templates_dir()` returns `Path("/plugin/templates")`

#### Scenario: Scripts path derived from plugin root
- **WHEN** `paths.plugin_root()` returns `/plugin`
- **THEN** `paths.scripts_dir()` returns `Path("/plugin/scripts")`

---

### Requirement: All path accessors return Path objects
Every public function in the `paths` module MUST return a `pathlib.Path` instance, never a raw string.

#### Scenario: Return types are Path instances
- **WHEN** any public accessor (`plugin_root`, `plugin_data`, `memory_dir`, `diary_dir`, `venv_dir`, `catalogs_dir`, `logs_dir`, `templates_dir`, `scripts_dir`, `dream_state_file`, `learnings_file`) is called
- **THEN** the return value is an instance of `pathlib.Path`

---

### Requirement: Environment variable override takes precedence over defaults
When both a plugin environment variable and standalone defaults could apply, the environment variable MUST win.

#### Scenario: CLAUDE_PLUGIN_OPTION overrides default memory path
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set AND `CLAUDE_PLUGIN_OPTION_memory_dir` is set to `/custom/mem`
- **THEN** `paths.memory_dir()` returns `Path("/custom/mem")`, not a path derived from `CLAUDE_PLUGIN_ROOT` or `~/.multiplai`

#### Scenario: Plugin data env var overrides default data path
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set AND `CLAUDE_PLUGIN_DATA` is set to `/custom/data`
- **THEN** `paths.plugin_data()` returns `Path("/custom/data")`, not a path derived from `CLAUDE_PLUGIN_ROOT`

---

### Requirement: Partial plugin environment configuration
When `CLAUDE_PLUGIN_ROOT` is set but optional `CLAUDE_PLUGIN_OPTION_*` variables are absent, the module MUST derive sensible defaults from `CLAUDE_PLUGIN_ROOT` or `CLAUDE_PLUGIN_DATA` rather than falling back to `~/.multiplai/`.

#### Scenario: Memory dir defaults to plugin-relative path when option is unset
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set to `/plugin` AND `CLAUDE_PLUGIN_OPTION_memory_dir` is not set
- **THEN** `paths.memory_dir()` returns `Path.home() / ".multiplai" / "memory"` (user home default, since memory lives outside plugin root)

#### Scenario: Plugin data defaults when CLAUDE_PLUGIN_DATA unset but CLAUDE_PLUGIN_ROOT set
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set to `/plugin` AND `CLAUDE_PLUGIN_DATA` is not set
- **THEN** `paths.plugin_data()` returns `Path("/plugin/data")`

---

### Requirement: Path expansion and normalization
The `paths` module MUST expand `~` in environment variable values and resolve paths to absolute form.

#### Scenario: Tilde expansion in environment variable
- **WHEN** `CLAUDE_PLUGIN_OPTION_memory_dir` is set to `~/my-memory`
- **THEN** `paths.memory_dir()` returns an absolute path with `~` expanded to the user's home directory (e.g., `Path("/home/user/my-memory")`)

#### Scenario: Relative path in environment variable is resolved to absolute
- **WHEN** `CLAUDE_PLUGIN_OPTION_diary_dir` is set to `relative/diary`
- **THEN** `paths.diary_dir()` returns an absolute `Path` (resolved against `cwd` or another deterministic base)

---

### Requirement: Empty environment variable treated as unset
If a plugin environment variable is set to an empty string, the `paths` module MUST treat it as unset and use the fallback.

#### Scenario: Empty CLAUDE_PLUGIN_ROOT treated as standalone mode
- **WHEN** `CLAUDE_PLUGIN_ROOT` is set to `""`
- **THEN** `paths.is_plugin_mode()` returns `False` AND `paths.plugin_root()` returns the standalone fallback

#### Scenario: Empty CLAUDE_PLUGIN_OPTION_memory_dir uses default
- **WHEN** `CLAUDE_PLUGIN_OPTION_memory_dir` is set to `""`
- **THEN** `paths.memory_dir()` returns the same value as when the variable is unset

---

### Requirement: Thread safety and immutability per process lifetime
Path resolution MUST read environment variables once (at import time or on first access) and cache the results. Subsequent calls MUST return the same values even if environment variables change mid-process.

#### Scenario: Cached resolution survives env var mutation
- **WHEN** `paths.memory_dir()` is called, then `CLAUDE_PLUGIN_OPTION_memory_dir` is changed in `os.environ`, then `paths.memory_dir()` is called again
- **THEN** both calls return the same `Path` value

#### Scenario: Concurrent access returns consistent values
- **WHEN** `paths.plugin_data()` is called from two different asyncio tasks concurrently
- **THEN** both calls return the same `Path` value without raising exceptions