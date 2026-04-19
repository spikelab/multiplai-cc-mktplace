## ADDED Requirements

### Requirement: Context Router Port
The context-router script must be ported from `claude-code-multiplai` to the plugin repo, using `paths.*` for all file resolution and `ModelClient` for LLM calls. Git-specific routing (resource/skill catalog lookups) must be removed; memory-file routing and session-context injection must be preserved.

#### Scenario: Context router resolves memory files via path resolver
- **WHEN** the ported context-router runs in plugin mode with `CLAUDE_PLUGIN_DATA` and user-configured `memory_dir` set
- **THEN** it reads memory files (e.g., `me.md`, `technical-pref.md`, `preferences.md`) from paths returned by `paths.memory_dir()`, not from any hardcoded path

#### Scenario: Context router does not reference resource/skill catalog routing
- **WHEN** the ported context-router source is inspected
- **THEN** it contains no references to `generate-catalog`, skill-catalog JSON, resource-catalog JSON, or any catalog-based routing logic

#### Scenario: Context router uses ModelClient for LLM calls
- **WHEN** the context-router needs to make an LLM call (e.g., to classify or summarize context)
- **THEN** it calls `ModelClient` methods (obtained via `create_client()`) instead of importing `claude_agent_sdk` directly

#### Scenario: Context router handles missing memory files gracefully
- **WHEN** the context-router runs but one or more expected memory files do not exist on disk
- **THEN** it skips the missing files without raising an exception and continues processing available files

---

### Requirement: Session Lifecycle Port
The session-lifecycle scripts (session start, session end logic) must be ported with plugin path resolution. Auto-commit logic must be removed.

#### Scenario: Session start writes state to plugin data directory
- **WHEN** the session-lifecycle start logic executes in plugin mode
- **THEN** session state files (timestamps, session ID) are written under the path returned by `paths.plugin_data()`, not to hardcoded locations

#### Scenario: Session end persists session summary
- **WHEN** a session ends and the session-end lifecycle script runs
- **THEN** it writes a session summary to the appropriate diary or log directory resolved via `paths.*`

#### Scenario: No auto-commit logic present
- **WHEN** the ported session-lifecycle source is inspected
- **THEN** it contains no calls to `git commit`, `git add`, `git_stage`, or any auto-commit function

---

### Requirement: Extract Learnings Port
The extract-learnings script must be ported with `git_stage()` removed. Learnings must be appended to files resolved via the path resolver.

#### Scenario: Learnings written to path-resolved location
- **WHEN** extract-learnings runs and produces new learnings
- **THEN** learnings are appended to the file at the path returned by `paths.learnings_file()` or equivalent path-resolver call

#### Scenario: git_stage removed
- **WHEN** the ported extract-learnings source is inspected
- **THEN** it contains no reference to `git_stage`, `git add`, or any git staging function

#### Scenario: Extract learnings uses ModelClient for summarization
- **WHEN** extract-learnings calls the LLM to identify or summarize learnings from a session
- **THEN** it uses a `ModelClient` instance, not a direct `claude_agent_sdk` import

#### Scenario: No learnings produced yields no file mutation
- **WHEN** extract-learnings runs but the LLM returns no actionable learnings
- **THEN** the learnings file is not modified and no empty entries are appended

---

### Requirement: AutoDream Port
The autodream script must be ported to use plugin paths for dream state and memory files, and `ModelClient` for LLM synthesis.

#### Scenario: Dream state persisted in plugin data directory
- **WHEN** autodream runs and updates dream state (last run timestamp, pending learnings count)
- **THEN** dream state is read from and written to `paths.plugin_data() / "dream_state"` or equivalent path-resolver location

#### Scenario: AutoDream reads learnings from path-resolved location
- **WHEN** autodream triggers a consolidation cycle
- **THEN** it reads accumulated learnings from `paths.learnings_file()`, not from a hardcoded path

#### Scenario: AutoDream synthesizes via ModelClient
- **WHEN** autodream invokes the LLM to consolidate learnings into memory updates
- **THEN** it uses `ModelClient` methods, not direct `claude_agent_sdk` imports

#### Scenario: AutoDream updates memory files in user-configured directory
- **WHEN** autodream produces memory-file updates (e.g., updating `me.md`)
- **THEN** it writes those updates to the directory returned by `paths.memory_dir()`

---

### Requirement: Synthesize Now Port
The synthesize-now script must be ported with path resolution and model client abstraction.

#### Scenario: Synthesize now reads from correct input paths
- **WHEN** synthesize-now runs
- **THEN** it reads diary entries, learnings, and memory files from paths resolved via `paths.*` functions

#### Scenario: Synthesize now writes output to path-resolved location
- **WHEN** synthesize-now produces a synthesis artifact
- **THEN** the artifact is written to a path resolved via `paths.*`, not to a hardcoded directory

#### Scenario: Synthesize now uses ModelClient
- **WHEN** synthesize-now calls the LLM for synthesis
- **THEN** it uses a `ModelClient` instance obtained from `create_client()`

---

### Requirement: Generate Catalog Port
The generate-catalog script must be ported but scoped only to plugin-relevant catalog generation. Resource/skill catalog routing logic stays removed; only catalog data needed by the plugin itself (if any) is retained.

#### Scenario: Catalog output written to plugin data directory
- **WHEN** generate-catalog runs
- **THEN** any generated catalog files are written under `paths.plugin_data()`, not hardcoded paths

#### Scenario: No skill/resource routing catalog generation
- **WHEN** the ported generate-catalog source is inspected
- **THEN** it does not generate skill-catalog or resource-catalog JSON files used by the removed routing logic

---

### Requirement: Model Resolver Port
The model-resolver script must be ported to defer to `ModelClient` for model selection rather than resolving model names against a hardcoded SDK.

#### Scenario: Model resolver returns valid model identifier
- **WHEN** model-resolver is called with a task type (e.g., "fast", "strong", "default")
- **THEN** it returns a model identifier string that `ModelClient` can use, without importing `claude_agent_sdk` directly

#### Scenario: Model resolver falls back to default on unknown task type
- **WHEN** model-resolver is called with an unrecognized task type
- **THEN** it returns the default model identifier without raising an exception

---

### Requirement: Log Utils Port
The log-utils module must be ported with all file paths resolved through the path resolver.

#### Scenario: Log file written to plugin data directory
- **WHEN** log-utils writes a log entry
- **THEN** the log file is located under `paths.plugin_data()` or a logs subdirectory thereof

#### Scenario: Log utils creates log directory if missing
- **WHEN** log-utils attempts to write and the log directory does not exist
- **THEN** it creates the directory (including parents) before writing

#### Scenario: Log utils does not use hardcoded paths
- **WHEN** the ported log-utils source is inspected
- **THEN** every file path is derived from `paths.*` calls — no string literals like `~/.multiplai/logs` or `/home/*/` appear

---

### Requirement: No Bash Wrapper Scripts
All ported scripts must be pure Python. No bash wrapper scripts from the source repo are carried over.

#### Scenario: No shell scripts in ported code
- **WHEN** the plugin's `scripts/` (or equivalent) directory is listed
- **THEN** it contains only `.py` files — no `.sh`, `.bash`, or `.zsh` files

#### Scenario: No subprocess calls to bash wrappers
- **WHEN** all ported Python source files are searched for `subprocess.run`, `subprocess.Popen`, `os.system`, or `os.popen`
- **THEN** none of those calls invoke a `.sh` or `.bash` script that was part of the original `claude-code-multiplai` repo

---

### Requirement: No Direct claude_agent_sdk Imports in Ported Scripts
Every ported script must use `ModelClient` abstraction — no script may `import claude_agent_sdk` or `from claude_agent_sdk import`.

#### Scenario: Source inspection finds zero direct SDK imports
- **WHEN** all ported `.py` files are searched with pattern `import claude_agent_sdk` or `from claude_agent_sdk`
- **THEN** zero matches are found

#### Scenario: LLM calls go through ModelClient interface
- **WHEN** any ported script makes an LLM call
- **THEN** the call is made on an object implementing the `ModelClient` interface, obtained via `create_client()`

---

### Requirement: No Hardcoded Path Constants in Ported Scripts
Every ported script must resolve file paths via `paths.*` — no hardcoded home-directory paths, no `~/.claude/`, no absolute paths to the original repo.

#### Scenario: No hardcoded home directory references
- **WHEN** all ported `.py` files are searched for patterns matching `~/`, `/home/`, `/Users/`, or `expanduser` with a hardcoded subdirectory
- **THEN** zero matches reference kit-specific paths like `~/.claude/`, `~/.multiplai/`, or paths under `claude-code-multiplai`

#### Scenario: Path resolver is the sole source of directory locations
- **WHEN** a ported script needs a file path (memory dir, diary dir, log dir, data dir)
- **THEN** it calls the corresponding `paths.*` function and uses the returned value

---

### Requirement: Supporting Config Files Ported
Any YAML/JSON config files consumed by ported scripts (e.g., model tiers, prompt templates) must be included in the plugin repo and loaded via path resolver.

#### Scenario: Config files present in plugin repo
- **WHEN** a ported script references a config file at runtime
- **THEN** that config file exists in the plugin repo under a path discoverable via `paths.*`

#### Scenario: Config files loaded via path resolver
- **WHEN** a ported script opens a config file
- **THEN** the file path is obtained from `paths.*`, not from a relative path assumption or hardcoded string

#### Scenario: Missing config file raises clear error
- **WHEN** a required config file is absent from disk at the path-resolved location
- **THEN** the ported script raises a descriptive error (including the expected path) rather than an opaque `FileNotFoundError`