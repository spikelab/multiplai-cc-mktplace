## Context

The multiplai system is a personal Claude Code toolkit (~3,500 lines) providing context routing, continuous learning, session awareness, and memory management. It currently lives as hooks and skills in `claude-code-multiplai`, hardcoded to one developer's dotfiles. Phase 1 modernized internals (Python ports, captain's log synthesis, AutoDream consolidation). Phase 2 — this design — repackages those internals into a standalone Claude Code plugin (`PROJECTS/multiplai-plugin/`) that any user can install.

The core logic is proven and stable. The work is structural: new directory layout, centralized path resolution, abstracted model client, plugin manifests, and hook declarations. No algorithmic changes to context routing, learning extraction, or dream synthesis.

The Claude Code plugin format provides: `plugin.json` manifests, hook lifecycle declarations (`hooks.json`), skill definitions, user-configurable options via `CLAUDE_PLUGIN_OPTION_*` environment variables, and a `$CLAUDE_PLUGIN_DATA` directory for runtime state. The plugin runtime injects `claude-agent-sdk` — plugins don't vendor it.

## Goals / Non-Goals

**Goals:**

- **G1**: Produce a self-contained plugin repository that passes `claude --plugin-dir ./multiplai-plugin` validation and loads all hooks/skills without errors.
- **G2**: Eliminate all hardcoded paths — every file reference resolves through a central `path_resolver` module that reads plugin environment variables with sensible fallbacks.
- **G3**: Decouple LLM calls from a specific SDK — abstract `ModelClient` supports Agent SDK (OAuth, zero-config) and Anthropic API key fallback, selected at runtime.
- **G4**: Ship a working onboarding flow — `/multiplai:setup` interviews the user and populates memory files from starter templates, producing a functional system from cold start.
- **G5**: Maintain feature parity with the Phase 1 `claude-code-multiplai` system for: context routing, session lifecycle (start/stop/end), learning extraction, AutoDream, and log synthesis.
- **G6**: Keep PyPI dependencies minimal — only `anthropic` and `pyyaml`. No transitive dependency trees.

**Non-Goals:**

- **NG1**: Modifying the source `claude-code-multiplai` repository. It continues as-is; this is a port, not a migration.
- **NG2**: Marketplace submission. The plugin will be tested with `--plugin-dir` only. Marketplace metadata (`marketplace.json`) is scaffolded but not submitted.
- **NG3**: Multi-user or team features. The plugin targets a single developer's workflow.
- **NG4**: Porting git-specific behaviors (`git_stage`, auto-commit, workspace scaffolding). These are kit-specific and out of scope.
- **NG5**: Resource/skill catalog routing from the context router. The plugin ships a simpler context router without dynamic catalog discovery.
- **NG6**: Supporting Python < 3.12.

## Decisions

### D1: Repository layout — flat `scripts/` with shared `lib/` package

**Decision**: All hook and skill entry-point scripts live in `scripts/`. Shared logic lives in `scripts/lib/` as an importable package (`paths`, `model_client`, `log_utils`, `config`). Plugin manifests and templates live at the repo root.

```
multiplai-plugin/
├── plugin.json
├── marketplace.json
├── hooks.json
├── LICENSE
├── README.md
├── CHANGELOG.md
├── requirements.txt
├── templates/
│   ├── me.md
│   ├── technical-pref.md
│   └── preferences.md
├── skills/
│   ├── setup.md
│   ├── dream.md
│   └── health.md
└── scripts/
    ├── lib/
    │   ├── __init__.py
    │   ├── paths.py          # path_resolver
    │   ├── model_client.py   # ModelClient abstraction
    │   ├── log_utils.py
    │   └── config.py
    ├── session_start.py
    ├── venv_bootstrap.py
    ├── context_router.py
    ├── session_stop.py
    ├── session_end.py
    ├── pre_compact.py
    ├── extract_learnings.py
    ├── autodream.py
    ├── synthesize_now.py
    └── generate_catalog.py
```

**Alternatives considered**:
- *Nested package structure* (`src/multiplai/`): More Pythonic but heavier for a plugin that's essentially a collection of scripts. Plugin hooks call individual scripts — a `src` layout adds import complexity with no benefit.
- *Single monolithic script per hook*: Eliminates import issues but duplicates shared logic (path resolution, model client) across every file. Unmaintainable.

**Why chosen**: Flat scripts are the natural unit for hook entry points. The `lib/` package centralizes shared code. The `scripts/` directory is a single `sys.path` entry, so `from lib.paths import resolve` works everywhere without install or `PYTHONPATH` manipulation.

### D2: Path resolution — environment variable cascade with hardcoded fallbacks

**Decision**: `scripts/lib/paths.py` exports a `Paths` dataclass populated once at import time. Resolution order for each path category:

| Path | Plugin mode | Standalone fallback |
|------|-------------|-------------------|
| Plugin root | `CLAUDE_PLUGIN_ROOT` | `Path(__file__).parents[2]` |
| Data dir | `CLAUDE_PLUGIN_DATA` | `~/.multiplai/data/` |
| Memory dir | `CLAUDE_PLUGIN_OPTION_memory_dir` | `~/.multiplai/memory/` |
| Diary dir | `CLAUDE_PLUGIN_OPTION_diary_dir` | `~/.multiplai/diary/` |
| Venv | `$data_dir/venv/` | same |
| Catalogs | `$data_dir/catalogs/` | same |

```python
@dataclasses.dataclass(frozen=True)
class Paths:
    plugin_root: Path
    data_dir: Path
    memory_dir: Path
    diary_dir: Path
    venv_dir: Path
    catalogs_dir: Path
    templates_dir: Path

    @classmethod
    def resolve(cls) -> "Paths":
        """Resolve all paths from environment, with fallbacks."""
        ...

# Module-level singleton
paths = Paths.resolve()
```

**Alternatives considered**:
- *Config file* (`multiplai.yaml`): Adds a file the user must find and edit. Plugin options already provide user configuration through `plugin.json` — a second config system creates confusion.
- *Constructor injection*: Pass paths into every function. Correct in theory, but every script entry point would need the same boilerplate. A module-level singleton is pragmatic for a plugin with a single process model.

**Why chosen**: Plugin environment variables are the canonical source in plugin mode. The fallback chain means `python scripts/session_start.py` works outside the plugin runtime for development and testing. Frozen dataclass prevents accidental mutation.

### D3: Model client — abstract interface with Agent SDK preferred, API key fallback

**Decision**: `scripts/lib/model_client.py` defines:

```python
class ModelClient(Protocol):
    async def query(self, prompt: str, *, system: str = "", model: str = "sonnet", max_tokens: int = 4096) -> str: ...

class AgentSDKClient:
    """Uses claude_agent_sdk.query() — available when running inside Claude Code."""
    ...

class AnthropicAPIClient:
    """Uses anthropic.AsyncAnthropic — requires API key."""
    ...

async def create_client() -> ModelClient:
    """Try Agent SDK first; fall back to API key from plugin option."""
    try:
        import claude_agent_sdk
        return AgentSDKClient(claude_agent_sdk)
    except ImportError:
        api_key = os.environ.get("CLAUDE_PLUGIN_OPTION_anthropic_api_key", "")
        if not api_key:
            raise RuntimeError("No Agent SDK and no API key configured")
        return AnthropicAPIClient(api_key)
```

**Alternatives considered**:
- *Agent SDK only*: Simpler, but the plugin can't be developed or tested outside Claude Code. The API key fallback is essential for local iteration.
- *LiteLLM / generic router*: Adds a heavy dependency and supports providers we don't need. The plugin only talks to Claude.
- *Synchronous client*: Several hooks (learning extraction, dream synthesis) benefit from concurrent LLM calls. Async from the start avoids a rewrite later.

**Why chosen**: The Protocol-based interface means scripts don't know or care which backend is active. `create_client()` encapsulates the detection logic. The two implementations are small (< 50 lines each) and share no state.

### D4: Venv bootstrap — SessionStart hook with idempotent setup

**Decision**: Register a `SessionStart` hook that runs `scripts/venv_bootstrap.py`. This script:

1. Checks if `$data_dir/venv/` exists and has a marker file (`.bootstrap-complete` with a hash of `requirements.txt`).
2. If missing or stale: creates venv, runs `pip install -r requirements.txt`, writes marker.
3. If present: no-op, exits in < 50ms.

All other hook scripts begin with:

```python
import subprocess, sys, os
venv_python = os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA", "~/.multiplai/data"), "venv", "bin", "python")
if sys.executable != venv_python:
    os.execv(venv_python, [venv_python] + sys.argv)
```

This re-execs into the venv Python if not already running there.

**Alternatives considered**:
- *Require user to pre-install dependencies*: Breaks the "install with one command" promise. Users shouldn't need to know about venvs.
- *Inline `pip install` in every script*: Redundant, slow, and races if multiple hooks fire concurrently.
- *Use `uv` for faster installs*: Can't assume `uv` is available on user machines. `venv` + `pip` are stdlib/bundled.

**Why chosen**: One-time cost on first session. Marker file with requirements hash means adding a dependency triggers re-bootstrap. The re-exec pattern is battle-tested in the existing kit.

### D5: Hook wiring — `hooks.json` with lifecycle mapping

**Decision**: `hooks.json` declares all hook registrations:

```json
{
  "hooks": [
    {
      "event": "SessionStart",
      "script": "scripts/venv_bootstrap.py",
      "timeout": 30000
    },
    {
      "event": "SessionStart",
      "script": "scripts/session_start.py",
      "timeout": 10000,
      "after": ["scripts/venv_bootstrap.py"]
    },
    {
      "event": "UserPromptSubmit",
      "script": "scripts/context_router.py",
      "timeout": 5000
    },
    {
      "event": "Stop",
      "script": "scripts/session_stop.py",
      "timeout": 15000
    },
    {
      "event": "SessionEnd",
      "script": "scripts/session_end.py",
      "timeout": 20000
    },
    {
      "event": "PreCompact",
      "script": "scripts/pre_compact.py",
      "timeout": 10000
    }
  ]
}
```

**Alternatives considered**:
- *Single entry-point script that dispatches by event*: Loses the per-hook timeout control and makes `hooks.json` less readable.
- *Inline hook commands (no separate scripts)*: Plugin format requires script references for anything beyond trivial commands.

**Why chosen**: One script per hook event is the natural mapping. The `after` field on `session_start.py` ensures venv is ready before any script that imports `anthropic` or `pyyaml`.

### D6: Skill definitions — markdown files with prompt + metadata

**Decision**: Each skill is a markdown file in `skills/` referenced from `plugin.json`:

```json
{
  "skills": [
    {"name": "setup", "description": "Onboarding interviewer", "file": "skills/setup.md"},
    {"name": "dream", "description": "Manual AutoDream trigger", "file": "skills/dream.md"},
    {"name": "health", "description": "Memory audit", "file": "skills/health.md"}
  ]
}
```

Skill markdown files contain the prompt text and instruct Claude to call the plugin's hook scripts (via Bash tool) for any operations that need Python (file I/O, LLM calls for synthesis).

**Alternatives considered**:
- *Skills as Python scripts*: The plugin skill format expects markdown prompt definitions, not executable scripts. Python logic goes in hook scripts that the skill invokes.
- *Single combined skill*: Three distinct use cases (onboarding, dreaming, auditing) with different UX flows. Combining them creates a confusing interface.

**Why chosen**: Matches the Claude Code plugin skill format. Each skill has a clear, single purpose. The markdown prompt can reference `scripts/` for heavy lifting.

### D7: Memory templates — shipped files copied during onboarding, never overwritten

**Decision**: `templates/` contains starter `.md` files. The `/multiplai:setup` skill flow copies them to the user's `memory_dir` only if the target file doesn't already exist. Template content is generic ("# About Me\n\n<!-- Describe yourself... -->") and designed to be populated by the onboarding interview.

**Alternatives considered**:
- *Generate memory files from scratch via LLM*: The interview already uses LLM to generate content — templates provide structure, not content. Without templates, the LLM might produce inconsistent file layouts across users.
- *No templates — let users create files manually*: Defeats the purpose of guided onboarding. Users shouldn't need to know the expected file structure.

**Why chosen**: Templates ensure consistent file structure across all installations while preserving existing content for users who re-run setup.

### D8: Porting strategy — systematic find-and-replace, not rewrite

**Decision**: Each script is ported from `claude-code-multiplai` with three mechanical transformations applied in order:

1. **Path replacement**: Every hardcoded path (`~/.claude/`, `/home/spike/`, absolute paths) → `from lib.paths import paths` + `paths.<attribute>`.
2. **SDK replacement**: Every `from claude_agent_sdk import query` or `import claude_agent_sdk` → `from lib.model_client import create_client` + `client = await create_client()`.
3. **Stripping**: Remove `git_stage()` calls, resource/skill catalog routing branches, bash subprocess calls that wrap shell scripts, and any auto-commit logic.

No algorithmic changes. If a function worked in `claude-code-multiplai`, it works identically in the plugin after transformation.

**Alternatives considered**:
- *Clean-room rewrite*: Higher risk of introducing bugs. The existing code is battle-tested; preserving it is safer.
- *AST-based automated refactoring*: Over-engineered for ~15 files. Manual find-and-replace with grep verification is faster and more reliable.

**Why chosen**: Minimizes risk. Each transformation is independently verifiable (grep for the old pattern, confirm zero hits after port). The three transformations are orthogonal — they don't interact.

## Risks / Trade-offs

### R1: Agent SDK import availability (Medium risk)

The `AgentSDKClient` assumes `claude_agent_sdk` is importable from the host runtime. If the plugin's venv Python doesn't inherit the host's packages, the import fails silently and falls back to API key — which the user may not have configured.

**Mitigation**: The `venv_bootstrap.py` creates the venv with `--system-site-packages` to inherit host packages. The `session_start.py` script logs which client was selected. The `/multiplai:health` skill checks and reports client status.

### R2: Hook timeout pressure (Medium risk)

`UserPromptSubmit` (context router) has a 5-second timeout. If the memory directory contains many large files, reading and ranking context may exceed this. The existing system runs on a fast local SSD; other users' machines may be slower.

**Mitigation**: Context router reads file metadata (size, mtime) first, only reads content for top candidates. Cache catalog in `$data_dir/catalogs/` and refresh asynchronously rather than on every prompt. Timeout is configurable — can be raised if users report issues.

### R3: `hooks.json` `after` field may not be supported (Low risk)

The design assumes `hooks.json` supports an `after` field for ordering hooks within the same event. If the plugin format doesn't support this, `session_start.py` could execute before venv bootstrap completes.

**Mitigation**: Fall back to the re-exec pattern (D4) in every script — if the venv isn't ready, the script bootstraps it inline before proceeding. This is slower but correct. Verify `after` support against plugin format docs before implementation.

### R4: Dual-mode path resolution adds testing surface (Low risk)

`Paths.resolve()` has two code paths (plugin env vars vs. fallbacks). A bug in the fallback path won't be caught when testing in plugin mode, and vice versa.

**Mitigation**: Unit tests for `Paths.resolve()` explicitly test both modes by manipulating `os.environ`. The frozen dataclass means paths are validated once at startup — no path can silently change mid-session.

### R5: No automated tests in initial delivery (Medium risk)

The port prioritizes structural correctness over test coverage. Scripts are verified manually via `claude --plugin-dir`.

**Mitigation**: Accept this for the initial port. The porting strategy (D8) preserves battle-tested logic, reducing the risk of novel bugs. Add a `tests/` directory and CI in a follow-up phase before marketplace submission.

### R6: Memory file format coupling (Low risk)

The plugin assumes memory files are markdown with specific heading conventions (inherited from `claude-code-multiplai`). If users edit memory files with unexpected structure, the context router or dream synthesis may produce degraded results.

**Mitigation**: Templates (D7) establish conventions. The context router and dream scripts already handle malformed input gracefully (they pass raw content to the LLM, which is robust to formatting variation). Document expected structure in template comments.

### R7: `requirements.txt` hash invalidation (Low risk)

The venv bootstrap uses a hash of `requirements.txt` to detect dependency changes. If the user manually installs packages into the venv, the marker won't reflect them — and a plugin update that changes `requirements.txt` will wipe and recreate the venv, losing those packages.

**Trade-off accepted**: The plugin venv is managed infrastructure, not a user workspace. Documenting "don't pip install into the plugin venv" is sufficient. The alternative (tracking installed packages independently) adds complexity for an edge case.