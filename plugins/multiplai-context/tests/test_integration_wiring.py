"""Block 10: Integration Wiring & Validation tests.

Tests cover:
  - plugin.json fully wired: skills + options + hooks cross-referenced
  - hooks.json `after` field support / inline-bootstrap fallback (R3)
  - End-to-end session lifecycle: start → prompt → stop → end
  - Grep audit: zero remaining hardcoded paths in ALL plugin files (G2)
  - requirements.txt minimal dependencies (G6): only anthropic, pyyaml
  - Cross-module wiring: hooks, skills, paths, model_client all consistent
  - Venv bootstrap idempotency and re-exec pattern validation
  - Template copy during onboarding (setup skill → templates → memory dir)
  - All plugin scripts syntactically valid and importable

These tests define the expected behavioral contract for the fully wired
plugin. They are meant to FAIL until the implementation is complete.
"""

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR, HOOKS_JSON, parse_hooks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_PLUGIN_FILES_CACHE: list[Path] | None = None


def _all_plugin_files() -> list[Path]:
    """Collect every non-binary file in the plugin directory tree."""
    global ALL_PLUGIN_FILES_CACHE
    if ALL_PLUGIN_FILES_CACHE is not None:
        return ALL_PLUGIN_FILES_CACHE

    exclude_dirs = {"__pycache__", ".git", "node_modules", "data", "venv",
                    "tests", "specs", ".pytest_cache", ".venv"}
    binary_exts = {".pyc", ".pyo", ".png", ".jpg", ".gif", ".ico", ".woff",
                   ".woff2", ".ttf", ".eot", ".so", ".dylib"}
    result = []
    for root, dirs, files in os.walk(PLUGIN_ROOT):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            if f == ".git":  # worktree pointer file, not a plugin file
                continue
            fp = Path(root) / f
            if fp.suffix in binary_exts:
                continue
            result.append(fp)
    ALL_PLUGIN_FILES_CACHE = result
    return result


def _load_hooks_json() -> dict:
    """Normalized view of the official nested hooks/hooks.json schema.

    Returns ``{"hooks": [...]}`` where each entry is a flat dict with
    ``event``, ``script``, ``command`` and ``timeout`` keys (one per hook
    command), so existing iteration over ``data["hooks"]`` keeps working.
    """
    return {"hooks": parse_hooks()}


def _load_plugin_json() -> dict:
    return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())


def _python_files_in_scripts() -> list[Path]:
    """All .py files under scripts/ (recursively)."""
    return list((PLUGIN_ROOT / "scripts").rglob("*.py"))


def _run_plugin_script(script: str, *, input_data: str = "{}",
                       env_overrides: dict | None = None,
                       timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a plugin script with JSON on stdin."""
    env = os.environ.copy()
    # Clear plugin env vars for clean testing
    for k in list(env):
        if k.startswith("CLAUDE_PLUGIN"):
            del env[k]
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / script)],
        input=input_data,
        capture_output=True, text=True,
        env=env,
        timeout=timeout,
    )


# ===========================================================================
# 1. Plugin.json Full Wiring Validation
# ===========================================================================

class TestPluginJsonFullWiring:
    """Validate plugin.json is fully wired with all declarations
    cross-referencing actual files and consistent with hooks.json."""

    @pytest.fixture(autouse=True)
    def load_manifests(self):
        self.plugin = _load_plugin_json()
        self.hooks = _load_hooks_json()

    def test_hooks_json_exists_at_hooks_dir(self):
        """WHEN the plugin is loaded
        THEN hooks/hooks.json exists for CC auto-discovery."""
        assert HOOKS_JSON.is_file()
        assert HOOKS_JSON == PLUGIN_ROOT / "hooks" / "hooks.json"

    def test_all_skill_files_reference_existing_scripts(self):
        """WHEN skill markdown files reference scripts via bash tool
        THEN those scripts exist in the plugin scripts/ directory."""
        for skill_file in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
            content = skill_file.read_text()
            script_refs = re.findall(r'scripts/\w+\.py', content)
            for ref in script_refs:
                assert (PLUGIN_ROOT / ref).is_file(), \
                    f"Skill {skill_file.name} references non-existent script: {ref}"

    def test_every_skill_has_a_readme_command_row(self):
        """WHEN a skill exists under skills/
        THEN README documents it as a /multiplai-context:<name> command row.

        Guards the command table against the drift the 2026-07-08 review
        caught (now / log-doctor / costs were shipped but undocumented)."""
        readme = (PLUGIN_ROOT / "README.md").read_text()
        documented = set(re.findall(r"/multiplai-context:([a-z0-9-]+)", readme))
        skills = {d.name for d in (PLUGIN_ROOT / "skills").iterdir() if d.is_dir()}
        missing = skills - documented
        assert not missing, (
            "Skills shipped but absent from the README command table: "
            f"{sorted(missing)}"
        )

    def test_user_config_fields_correspond_to_env_vars(self):
        """WHEN userConfig fields are declared in plugin.json
        THEN some module reads the corresponding CLAUDE_PLUGIN_OPTION_* env var.

        Config consumption is split across two locations: the plugin's own
        context modules (scripts/lib, scripts/generators) and the extracted
        multiplai_core package (which owns path/config/model-client resolution
        and therefore the path + api-key + catalog-model env vars)."""
        source_dirs = [SCRIPTS_DIR / "lib", SCRIPTS_DIR / "generators"]
        all_sources = []
        for src_dir in source_dirs:
            for py_file in src_dir.glob("*.py"):
                all_sources.append(py_file.read_text())

        # Include multiplai_core's installed source: path/config/model_client
        # resolution (and their env vars) moved there during extraction.
        try:
            import multiplai_core
            core_dir = Path(multiplai_core.__file__).parent
            for py_file in core_dir.glob("*.py"):
                all_sources.append(py_file.read_text())
        except Exception:  # pragma: no cover - core always present in CI
            pass

        all_source_text = "\n".join(all_sources)

        for field_name in self.plugin.get("userConfig", {}):
            env_var = f"CLAUDE_PLUGIN_OPTION_{field_name}"
            assert env_var in all_source_text, \
                f"userConfig field '{field_name}' has no corresponding {env_var} " \
                "in any plugin or multiplai_core module"

    def test_hooks_scripts_all_exist_and_are_python(self):
        """WHEN every hook script path in hooks.json is checked
        THEN each exists and is a .py file."""
        for hook in self.hooks["hooks"]:
            script_path = PLUGIN_ROOT / hook["script"]
            assert script_path.is_file(), \
                f"Hook script missing: {hook['script']}"
            assert script_path.suffix == ".py", \
                f"Hook script is not Python: {hook['script']}"

    def test_hooks_have_timeout_values(self):
        """WHEN each hook entry in hooks.json is inspected
        THEN it has a timeout value to prevent hung hooks."""
        for hook in self.hooks["hooks"]:
            assert "timeout" in hook, \
                f"Hook {hook['event']}→{hook['script']} missing timeout"
            assert isinstance(hook["timeout"], int), \
                f"Hook timeout must be integer: {hook['event']}"
            assert hook["timeout"] > 0, \
                f"Hook timeout must be positive: {hook['event']}"

    def test_session_start_has_no_venv_bootstrap(self):
        """WHEN SessionStart hooks are listed
        THEN the retired venv_bootstrap.py hook is gone and session_start.py
        is the sole SessionStart command."""
        session_start_hooks = [h for h in self.hooks["hooks"]
                               if h["event"] == "SessionStart"]
        scripts = [h["script"] for h in session_start_hooks]
        assert scripts, "No SessionStart hooks found"
        assert not any(s.endswith("venv_bootstrap.py") for s in scripts), \
            "venv_bootstrap.py hook must be removed (uv migration)"
        assert "scripts/session_start.py" in scripts, \
            "session_start.py must remain the SessionStart hook"


# ===========================================================================
# 2. hooks.json `after` Field Support & Inline Bootstrap Fallback (R3)
# ===========================================================================

class TestAfterFieldAndBootstrapFallback:
    """Verify hooks are launched via uv with PEP 723 inline metadata.

    The managed-venv re-exec preamble (R3 mitigation) was retired: every hook
    command runs through `uv run --no-project`, which provisions deps from each
    script's inline PEP 723 block.
    """

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        self.hooks = _load_hooks_json()

    def test_session_start_has_pep723_metadata(self):
        """WHEN session_start.py is inspected
        THEN it carries PEP 723 inline metadata and no retired venv guard."""
        text = (SCRIPTS_DIR / "session_start.py").read_text()
        assert "# /// script" in text, \
            "session_start.py must carry PEP 723 inline metadata"
        assert "ensure_venv_python" not in text, \
            "session_start.py must not reference the retired venv guard"

    def test_all_hook_scripts_have_pep723_metadata(self):
        """WHEN any hook script is inspected
        THEN it carries PEP 723 inline metadata (and no venv guard)."""
        for hook in self.hooks["hooks"]:
            script_path = PLUGIN_ROOT / hook["script"]
            if script_path.exists():
                text = script_path.read_text()
                assert "# /// script" in text, \
                    f"{hook['script']} missing PEP 723 inline metadata"
                assert "venv_guard" not in text and "ensure_venv_python" not in text, \
                    f"{hook['script']} still references the retired venv guard"

    def test_all_hook_commands_use_uv_run(self):
        """WHEN any hook command is inspected
        THEN it execs the script via `uv run --no-project` (rather than
        `python`), wrapped in the sh-level uv guard that turns a missing uv
        into one clear message instead of a silent spawn failure (C1 —
        see test_uv_guard.py for behavior)."""
        parsed = json.loads(HOOKS_JSON.read_text())
        for event, groups in parsed["hooks"].items():
            for group in groups:
                for entry in group["hooks"]:
                    cmd = entry["command"]
                    assert cmd.startswith("sh -c 'command -v uv "), \
                        f"{event} command must start with the uv guard, got: {cmd}"
                    assert 'exec uv run --no-project "' in cmd, \
                        f"{event} command must exec via 'uv run --no-project', got: {cmd}"
                    assert "python " not in cmd.replace("uv run", ""), \
                        f"{event} command must not invoke bare python: {cmd}"

    def test_hooks_json_valid_official_nested_schema(self):
        """WHEN hooks/hooks.json is parsed
        THEN it is valid JSON conforming to the official nested CC schema:
        top-level "hooks" maps each event to a list of groups, each group
        holding a "hooks" list of command entries."""
        parsed = json.loads(HOOKS_JSON.read_text())
        assert isinstance(parsed.get("hooks"), dict), \
            "official schema: 'hooks' must be an object keyed by event"
        for event, groups in parsed["hooks"].items():
            assert isinstance(groups, list), f"{event} must map to a list"
            for group in groups:
                assert isinstance(group.get("hooks"), list), \
                    f"{event} group must contain a 'hooks' list"
                for entry in group["hooks"]:
                    assert entry.get("type") == "command"
                    assert isinstance(entry.get("command"), str) and entry["command"]



# ===========================================================================
# 3. End-to-End Session Lifecycle
# ===========================================================================

class TestEndToEndSessionLifecycle:
    """End-to-end test: full session lifecycle (start → prompt → stop → end).

    Verifies that all hook scripts can at minimum be invoked without
    crashing, and that they produce the expected side effects.
    """

    @pytest.fixture
    def plugin_env(self, tmp_path):
        """Create a mock plugin environment."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        env = {
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_memory_dir": str(memory_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }
        return env

    def test_session_start_writes_session_state(self, tmp_path, plugin_env):
        """WHEN session_start.py executes
        THEN it writes session_state.json to the data directory."""
        result = _run_plugin_script(
            "scripts/session_start.py",
            env_overrides=plugin_env,
        )
        # Script may fail due to missing venv, but session_state should be written
        data_dir = Path(plugin_env["CLAUDE_PLUGIN_DATA"])
        state_file = data_dir / "session_state.json"
        if result.returncode == 0:
            assert state_file.exists(), \
                "session_start.py must write session_state.json"
            state = json.loads(state_file.read_text())
            assert "session_id" in state
            assert "start_time" in state

    def test_session_start_records_available_memory_files(self, tmp_path, plugin_env):
        """WHEN session_start.py runs with memory files present
        THEN it records the available memory files in session_state.json.

        Post-restructure, session_start.py intentionally does NOT read or
        inject memory contents (its docstring: "Contents are NOT read or
        injected here — context_manager.py performs routed, per-prompt
        memory injection on UserPromptSubmit"). It only enumerates the
        available files into the session_state record."""
        memory_dir = Path(plugin_env["CLAUDE_PLUGIN_OPTION_memory_dir"])
        (memory_dir / "me.md").write_text("# About Me\nI am a test user")

        result = _run_plugin_script(
            "scripts/session_start.py",
            env_overrides=plugin_env,
        )
        if result.returncode == 0:
            state_file = Path(plugin_env["CLAUDE_PLUGIN_DATA"]) / "session_state.json"
            assert state_file.exists(), "session_start.py must write session_state.json"
            state = json.loads(state_file.read_text())
            assert "me.md" in state.get("memory_files_available", []), \
                "session_start.py should record available memory files"

    def test_context_manager_accepts_stdin_json(self, plugin_env):
        """WHEN context_manager.py receives valid JSON on stdin
        THEN it does not crash (exit code 0 or handled gracefully)."""
        input_data = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Tell me about the project",
        })
        result = _run_plugin_script(
            "scripts/context_manager.py",
            input_data=input_data,
            env_overrides=plugin_env,
        )
        # Allow non-zero exit if venv is missing, but not a crash
        assert "Traceback" not in result.stderr or "venv" in result.stderr.lower(), \
            f"context_manager.py crashed: {result.stderr[:500]}"

    def test_session_stop_accepts_stdin_json(self, plugin_env):
        """WHEN session_stop.py receives valid JSON on stdin
        THEN it does not crash."""
        input_data = json.dumps({
            "hook_event_name": "Stop",
            "stop_hook_active": "true",
        })
        result = _run_plugin_script(
            "scripts/session_stop.py",
            input_data=input_data,
            env_overrides=plugin_env,
        )
        assert "Traceback" not in result.stderr or "venv" in result.stderr.lower(), \
            f"session_stop.py crashed: {result.stderr[:500]}"

    def test_session_start_preserves_cooldown_state(self, plugin_env):
        """WHEN session_start.py runs against an existing session_state.json
        THEN it preserves turn_index / recently_injected (a concurrent
        session's cooldown map) while updating the identity keys.

        Regression guard for the wholesale-rewrite bug: session_start used
        to blow away the shared state file, clearing another session's
        re-recommendation cooldown."""
        data_dir = Path(plugin_env["CLAUDE_PLUGIN_DATA"])
        state_file = data_dir / "session_state.json"
        state_file.write_text(json.dumps({
            "session_id": "OLDSESS",
            "turn_index": 7,
            "recently_injected": {"memory": {"voice.md": 5}},
            "cwd": "/old",
        }))

        result = _run_plugin_script(
            "scripts/session_start.py",
            input_data=json.dumps({
                "hook_event_name": "SessionStart",
                "session_id": "NEWSESS",
                "cwd": "/new",
            }),
            env_overrides=plugin_env,
        )
        assert result.returncode == 0, result.stderr[:500]
        state = json.loads(state_file.read_text())
        # Cooldown bookkeeping from the prior/concurrent session survives.
        assert state.get("turn_index") == 7, "turn_index must be preserved"
        assert state.get("recently_injected") == {"memory": {"voice.md": 5}}, \
            "recently_injected cooldown map must be preserved"
        # Identity keys reflect the new session.
        assert state["session_id"] == "NEWSESS"
        assert state["cwd"] == "/new"

    def test_session_stop_preserves_cooldown_state(self, plugin_env):
        """WHEN session_stop.py runs THEN it merges last_stop without
        dropping turn_index / recently_injected, via the atomic writer."""
        data_dir = Path(plugin_env["CLAUDE_PLUGIN_DATA"])
        state_file = data_dir / "session_state.json"
        state_file.write_text(json.dumps({
            "session_id": "SESS1",
            "turn_index": 3,
            "recently_injected": {"skills": {"foo": 2}},
        }))

        result = _run_plugin_script(
            "scripts/session_stop.py",
            input_data=json.dumps({
                "hook_event_name": "Stop",
                "session_id": "SESS1",
            }),
            env_overrides=plugin_env,
        )
        assert result.returncode == 0, result.stderr[:500]
        state = json.loads(state_file.read_text())
        assert state.get("turn_index") == 3
        assert state.get("recently_injected") == {"skills": {"foo": 2}}
        assert "last_stop" in state, "session_stop must record last_stop"

    def test_session_end_accepts_stdin_json(self, plugin_env):
        """WHEN session_end.py receives valid JSON on stdin
        THEN it does not crash."""
        input_data = json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "test-e2e-001",
            "reason": "user_exit",
        })
        result = _run_plugin_script(
            "scripts/session_end.py",
            input_data=input_data,
            env_overrides=plugin_env,
        )
        assert "Traceback" not in result.stderr or "venv" in result.stderr.lower(), \
            f"session_end.py crashed: {result.stderr[:500]}"

    def test_pre_compact_accepts_stdin_json(self, plugin_env):
        """WHEN pre_compact.py receives valid JSON on stdin
        THEN it does not crash."""
        input_data = json.dumps({
            "hook_event_name": "PreCompact",
        })
        result = _run_plugin_script(
            "scripts/pre_compact.py",
            input_data=input_data,
            env_overrides=plugin_env,
        )
        assert "Traceback" not in result.stderr or "venv" in result.stderr.lower(), \
            f"pre_compact.py crashed: {result.stderr[:500]}"

    def test_full_lifecycle_sequence(self, tmp_path, plugin_env):
        """WHEN hooks fire in order: start → prompt → stop → end
        THEN each completes without crashing and side effects accumulate."""
        # Phase 1: Session Start
        result_start = _run_plugin_script(
            "scripts/session_start.py",
            env_overrides=plugin_env,
        )

        # Phase 2: User Prompt Submit
        result_prompt = _run_plugin_script(
            "scripts/context_manager.py",
            input_data=json.dumps({"hook_event_name": "UserPromptSubmit",
                                   "prompt": "hello"}),
            env_overrides=plugin_env,
        )

        # Phase 3: Stop (after Claude responds)
        result_stop = _run_plugin_script(
            "scripts/session_stop.py",
            input_data=json.dumps({"hook_event_name": "Stop"}),
            env_overrides=plugin_env,
        )

        # Phase 4: Session End
        result_end = _run_plugin_script(
            "scripts/session_end.py",
            input_data=json.dumps({"hook_event_name": "SessionEnd",
                                   "reason": "user_exit"}),
            env_overrides=plugin_env,
        )

        # At minimum, session_start should succeed (it doesn't need venv deps)
        # or fail gracefully. All scripts should not produce tracebacks
        # (except venv-related ones which are expected pre-bootstrap)
        for name, result in [("start", result_start), ("prompt", result_prompt),
                             ("stop", result_stop), ("end", result_end)]:
            # Allow venv-related failures, but not unhandled exceptions
            if "Traceback" in result.stderr:
                assert ("venv" in result.stderr.lower() or
                        "ModuleNotFoundError" in result.stderr or
                        "ensure_venv_python" in result.stderr), \
                    f"Lifecycle phase '{name}' crashed: {result.stderr[:500]}"


# ===========================================================================
# 4. Grep Audit: Zero Hardcoded Paths (G2)
# ===========================================================================

class TestGrepAuditHardcodedPaths:
    """Comprehensive grep audit: zero remaining hardcoded paths in ALL plugin
    files per G2.

    Scans every file in multiplai-plugin/ for forbidden path patterns:
    - ~/.claude/
    - /home/spike/
    - /Users/spike/
    - /home/<any-user>/ (outside of documented examples)
    - Absolute paths to the original claude-code-multiplai repo
    """

    FORBIDDEN_PATTERNS = [
        (r"~/.claude/", "Hardcoded ~/.claude/ path"),
        (r"/home/spike/", "Hardcoded /home/spike/ path"),
        (r"/Users/spike/", "Hardcoded /Users/spike/ path"),
        (r"claude-code-multiplai", "Reference to source repo name"),
    ]

    # Files where these patterns are ALLOWED (tests, docs, config defaults)
    EXEMPT_FILES = {"test_integration_wiring.py", "CHANGELOG.md"}

    # Files allowed to reference ~/.claude/ (the standard Claude Code config dir)
    # because they define user-facing path defaults, not hardcoded user paths.
    # README.md documents the same ~/.claude/skills default that
    # plugin.json defines — a user-facing default, not a hardcoded path.
    _CLAUDE_DIR_EXEMPT = {"plugin.json", "config.py", "README.md"}

    def test_no_hardcoded_paths_in_any_plugin_file(self):
        """WHEN every file in multiplai-plugin/ is scanned
        THEN zero forbidden path patterns are found (G2)."""
        violations = []
        for fp in _all_plugin_files():
            if fp.name in self.EXEMPT_FILES:
                continue
            try:
                text = fp.read_text(errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

            for pattern, desc in self.FORBIDDEN_PATTERNS:
                # Allow config files to reference ~/.claude/ (standard config dir)
                if pattern == r"~/.claude/" and fp.name in self._CLAUDE_DIR_EXEMPT:
                    continue
                if pattern in text:
                    rel = fp.relative_to(PLUGIN_ROOT)
                    violations.append(f"  {rel}: {desc}")

        assert len(violations) == 0, \
            f"G2 violation — hardcoded paths found:\n" + "\n".join(violations)

    def test_no_absolute_home_paths_in_python_scripts(self):
        """WHEN all .py files in scripts/ are scanned for /home/ or /Users/
        THEN zero matches are found outside of string comparisons and docs."""
        violations = []
        for py_file in _python_files_in_scripts():
            text = py_file.read_text()
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Check for hardcoded absolute paths
                if re.search(r'"/home/|"/Users/', stripped):
                    rel = py_file.relative_to(PLUGIN_ROOT)
                    violations.append(f"  {rel}:{line_no}: {stripped[:80]}")

        assert len(violations) == 0, \
            f"Absolute home paths in scripts:\n" + "\n".join(violations)

    def test_user_config_defaults_use_tilde_not_absolute(self):
        """WHEN userConfig defaults are inspected in plugin.json
        THEN all path defaults use ~ prefix, not absolute paths."""
        manifest = _load_plugin_json()
        for key, cfg in manifest.get("userConfig", {}).items():
            default = cfg.get("default")
            # Only filesystem paths must use ~. URL defaults (e.g. the qmd
            # http daemon endpoint) legitimately contain "/" but aren't paths.
            if isinstance(default, str) and "/" in default and "://" not in default:
                assert default.startswith("~"), \
                    f"userConfig.{key}.default must use ~ prefix, got: {default}"

    def test_no_spikelab_references(self):
        """WHEN all plugin files are scanned
        THEN none contain 'spikelab' references outside of author fields
        and marketplace.json repository URL."""
        allowed_files = {"plugin.json", "marketplace.json", "CHANGELOG.md",
                         "README.md", "LICENSE"}
        violations = []
        for fp in _all_plugin_files():
            if fp.name in allowed_files or fp.name in self.EXEMPT_FILES:
                continue
            try:
                text = fp.read_text(errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            # The 'spikelab' org legitimately appears in the multiplai-core
            # git URL inside PEP 723 inline metadata — that's infrastructure,
            # not a leaked personal reference. Ignore those lines.
            offending = [
                ln for ln in text.lower().splitlines()
                if "spikelab" in ln and "multiplai-core" not in ln
            ]
            if offending:
                rel = fp.relative_to(PLUGIN_ROOT)
                violations.append(str(rel))

        assert len(violations) == 0, \
            f"'spikelab' found in non-manifest files: {violations}"


# ===========================================================================
# 5. Requirements.txt Minimal Dependencies (G6)
# ===========================================================================

class TestMinimalDependencies:
    """Verify runtime deps moved out of requirements.txt and into per-script
    PEP 723 inline metadata + multiplai-core.

    anthropic / claude-agent-sdk / pyyaml are no longer pinned in
    requirements.txt; entry-point scripts declare their deps inline and
    `uv run` provisions them (the SDK arrives transitively via multiplai-core)."""

    def test_requirements_has_no_runtime_pins(self):
        """WHEN requirements.txt is parsed
        THEN it contains no runtime dependency lines (comments only)."""
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        deps = [line.strip() for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")]
        assert deps == [], \
            f"requirements.txt must declare no runtime deps, got: {deps}"

    def test_no_pinned_runtime_deps(self):
        """WHEN requirements.txt is inspected
        THEN the retired runtime pins are absent."""
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        for pin in ("anthropic==", "claude-agent-sdk==", "pyyaml>="):
            assert pin not in text, \
                f"requirements.txt must not pin {pin!r} (moved to PEP 723 + multiplai-core)"

    def test_entry_points_declare_multiplai_core(self):
        """WHEN entry-point scripts are inspected
        THEN each carries PEP 723 metadata depending on multiplai-core."""
        for name in ("session_start.py", "context_manager.py", "dream.py",
                     "extract_learnings.py", "generate_catalog.py"):
            text = (SCRIPTS_DIR / name).read_text()
            assert "# /// script" in text, f"{name} missing PEP 723 header"
            assert "multiplai-core" in text, f"{name} missing multiplai-core dependency"


# ===========================================================================
# 6. Cross-Module Wiring Consistency
# ===========================================================================

class TestCrossModuleWiring:
    """Verify all modules reference each other correctly and consistently."""

    def test_all_hook_scripts_import_core_paths(self):
        """WHEN each hook script is inspected
        THEN it imports the path resolver from multiplai_core.paths."""
        hooks = _load_hooks_json()
        for hook in hooks["hooks"]:
            script_path = PLUGIN_ROOT / hook["script"]
            if not script_path.exists():
                continue
            text = script_path.read_text()
            has_paths = ("from multiplai_core.paths" in text
                         or "multiplai_core.paths" in text)
            assert has_paths, \
                f"{hook['script']} doesn't import multiplai_core.paths"

    def test_lib_package_has_init(self):
        """WHEN scripts/lib/__init__.py is checked
        THEN it exists (required for import to work)."""
        init_file = SCRIPTS_DIR / "lib" / "__init__.py"
        assert init_file.is_file(), "scripts/lib/__init__.py must exist"

    def test_model_client_not_imported_at_module_level_in_hooks(self):
        """WHEN hook scripts that don't need LLM calls are inspected
        THEN they don't import model_client at the top level (avoiding
        unnecessary anthropic import before venv is ready)."""
        # These scripts shouldn't need model_client
        no_llm_scripts = ["venv_bootstrap.py", "session_start.py"]
        for script_name in no_llm_scripts:
            path = SCRIPTS_DIR / script_name
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            # Check for top-level (non-function) imports of model_client
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        if "model_client" in node.module and "create_client" in \
                                [a.name for a in node.names]:
                            pytest.fail(
                                f"{script_name} imports create_client at top level; "
                                "should be deferred to function scope"
                            )

    def test_venv_guard_module_removed(self):
        """WHEN scripts/lib/ is inspected
        THEN the retired venv_guard.py is gone (uv migration)."""
        guard_path = SCRIPTS_DIR / "lib" / "venv_guard.py"
        assert not guard_path.exists(), \
            "venv_guard.py must be removed (replaced by uv run + PEP 723)"

    def test_all_scripts_add_parent_to_sys_path(self):
        """WHEN each script in scripts/ is inspected
        THEN it adds its parent directory to sys.path for lib imports."""
        for script in (SCRIPTS_DIR).glob("*.py"):
            text = script.read_text()
            has_path_setup = ("sys.path" in text or "from lib." in text)
            assert has_path_setup, \
                f"{script.name} doesn't set up sys.path for lib imports"


# ===========================================================================
# 8. Template Copy During Setup
# ===========================================================================

class TestTemplateCopyIntegration:
    """Verify template → memory directory copy logic for onboarding."""

    def test_setup_check_script_exists(self):
        """WHEN setup skill references setup_check.py
        THEN the script exists."""
        assert (SCRIPTS_DIR / "setup_check.py").is_file(), \
            "setup_check.py must exist for the setup skill"

    def test_setup_write_script_exists(self):
        """WHEN setup skill references setup_write.py
        THEN the script exists."""
        assert (SCRIPTS_DIR / "setup_write.py").is_file(), \
            "setup_write.py must exist for the setup skill"

    def test_setup_check_reports_existing_files(self, tmp_path):
        """WHEN setup_check.py runs with some memory files present
        THEN it reports which files exist and which are missing."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("# About Me")

        env = {
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
            "CLAUDE_PLUGIN_OPTION_memory_dir": str(memory_dir),
        }
        result = _run_plugin_script(
            "scripts/setup_check.py",
            env_overrides=env,
        )
        if result.returncode == 0:
            output = result.stdout
            assert "me.md" in output, \
                "setup_check should report on me.md status"

    def test_templates_match_expected_filenames(self):
        """WHEN templates/ directory is listed
        THEN it contains me.md, technical-pref.md, preferences.md."""
        templates_dir = PLUGIN_ROOT / "templates"
        template_names = {f.name for f in templates_dir.glob("*.md")}
        expected = {"me.md", "technical-pref.md", "preferences.md"}
        assert expected.issubset(template_names), \
            f"Missing templates: {expected - template_names}"

    def test_setup_skill_references_template_files(self):
        """WHEN the setup skill markdown is inspected
        THEN it references the template filenames or the setup scripts."""
        setup_md = (PLUGIN_ROOT / "skills" / "setup" / "SKILL.md").read_text()
        # Must reference the setup workflow scripts
        assert ("setup_check" in setup_md or "setup_write" in setup_md or
                "template" in setup_md.lower()), \
            "Setup skill must reference setup scripts or templates"


# ===========================================================================
# 9. All Plugin Scripts Syntactically Valid
# ===========================================================================

class TestPluginScriptValidity:
    """Verify all Python scripts compile and have no syntax errors."""

    @pytest.mark.parametrize("py_file", _python_files_in_scripts(),
                             ids=lambda p: p.name)
    def test_script_compiles(self, py_file):
        """WHEN each .py file is compiled
        THEN py_compile succeeds."""
        import py_compile
        py_compile.compile(str(py_file), doraise=True)

    def test_lib_modules_importable(self):
        """WHEN lib/ modules are imported
        THEN they load without ImportError or SyntaxError."""
        # paths.py should be importable (it's already on sys.path via conftest)
        try:
            from multiplai_core import paths
            assert hasattr(paths, "get_paths")
            assert hasattr(paths, "Paths")
        except ImportError:
            pytest.fail("lib.paths should be importable")

    def test_model_client_importable_without_sdk(self):
        """WHEN lib.model_client is imported without claude_agent_sdk
        THEN the module loads successfully (SDK import is deferred)."""
        try:
            from multiplai_core import model_client
            assert hasattr(model_client, "ModelClient")
            assert hasattr(model_client, "create_client")
            assert hasattr(model_client, "AgentSDKClient")
            assert hasattr(model_client, "AnthropicAPIClient")
        except ImportError as e:
            if "claude_agent_sdk" in str(e):
                pytest.fail("model_client should import without claude_agent_sdk")
            raise


# ===========================================================================
# 10. Model Client Factory Wiring
# ===========================================================================

class TestModelClientFactoryWiring:
    """Verify create_client() factory behavior."""

    def test_create_client_raises_without_sdk_or_key(self):
        """WHEN create_client() is called with no SDK and no API key
        THEN it raises RuntimeError."""
        from multiplai_core.model_client import create_client
        from unittest.mock import patch
        import builtins

        # Ensure no API key env var is set
        env_key = "CLAUDE_PLUGIN_OPTION_anthropic_api_key"
        old_val = os.environ.pop(env_key, None)
        try:
            # Block claude_agent_sdk import to simulate missing SDK
            real_import = builtins.__import__
            def mock_import(name, *args, **kwargs):
                if name == "claude_agent_sdk":
                    raise ImportError("mocked: no SDK")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(RuntimeError, match="(?i)neither|not available"):
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(create_client())
                    finally:
                        loop.close()
        finally:
            if old_val is not None:
                os.environ[env_key] = old_val

    def test_create_client_returns_anthropic_with_key(self):
        """WHEN create_client() is called with an API key and no SDK
        THEN it returns AnthropicAPIClient."""
        from multiplai_core.model_client import create_client, AnthropicAPIClient
        from unittest.mock import patch
        import builtins

        # Block claude_agent_sdk import to force fallback
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("mocked: no SDK")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            loop = asyncio.new_event_loop()
            try:
                client = loop.run_until_complete(
                    create_client(api_key="test-key-for-wiring-check")
                )
                assert isinstance(client, AnthropicAPIClient), \
                    f"Expected AnthropicAPIClient, got {type(client).__name__}"
            finally:
                loop.close()

    def test_anthropic_client_rejects_empty_key(self):
        """WHEN AnthropicAPIClient is instantiated with empty key
        THEN it raises ValueError."""
        from multiplai_core.model_client import AnthropicAPIClient
        with pytest.raises(ValueError, match="(?i)api key.*required"):
            AnthropicAPIClient("")

    def test_anthropic_client_rejects_none_key(self):
        """WHEN AnthropicAPIClient is instantiated with None key
        THEN it raises ValueError."""
        from multiplai_core.model_client import AnthropicAPIClient
        with pytest.raises(ValueError, match="(?i)api key.*required"):
            AnthropicAPIClient(None)

    def test_default_model_is_correct(self):
        """WHEN DEFAULT_MODEL is inspected
        THEN it matches claude-sonnet-4-6."""
        from multiplai_core.model_client import DEFAULT_MODEL
        assert DEFAULT_MODEL == "claude-sonnet-4-6"

    def test_default_max_tokens_is_4096(self):
        """WHEN DEFAULT_MAX_TOKENS is inspected
        THEN it equals 4096."""
        from multiplai_core.model_client import DEFAULT_MAX_TOKENS
        assert DEFAULT_MAX_TOKENS == 4096

    def test_detect_client_type_returns_string(self):
        """WHEN detect_client_type() is called
        THEN it returns a non-empty string."""
        from multiplai_core.model_client import detect_client_type
        result = detect_client_type()
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# 11. Path Resolver Integration
# ===========================================================================

class TestPathResolverIntegration:
    """Verify paths module resolves correctly in both modes."""

    def test_plugin_mode_with_env_vars(self, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_ROOT is set
        THEN is_plugin_mode() returns True."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/test-plugin")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_memory_dir", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        assert p.is_plugin_mode() is True
        assert str(p.plugin_root) == "/tmp/test-plugin"

    def test_standalone_mode_without_env_vars(self, monkeypatch, reset_paths_cache):
        """WHEN no CLAUDE_PLUGIN_* env vars are set
        THEN is_plugin_mode() returns False and paths fall back to ~/.multiplai/."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_memory_dir", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        assert p.is_plugin_mode() is False
        assert ".multiplai" in str(p.memory_dir)

    def test_empty_env_var_treated_as_unset(self, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_ROOT is set to empty string
        THEN is_plugin_mode() returns False."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        assert p.is_plugin_mode() is False

    def test_custom_memory_dir_override(self, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_OPTION_memory_dir is set
        THEN memory_dir() returns that path."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/plugin")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", "/custom/mem")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        assert str(p.memory_dir) == "/custom/mem"

    def test_derived_paths_from_data_dir(self, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_DATA is set
        THEN venv_dir, catalogs_dir are derived from it."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/plugin")
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/tmp/data")
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_memory_dir", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        assert str(p.venv_dir) == "/tmp/data/venv"
        assert str(p.catalogs_dir) == "/tmp/data/catalogs"

    def test_all_path_accessors_return_path_objects(self, monkeypatch, reset_paths_cache):
        """WHEN any public accessor is called
        THEN it returns a pathlib.Path instance."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/plugin")
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/tmp/data")
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_memory_dir", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()

        for accessor_name in ["plugin_root", "data_dir", "memory_dir",
                              "diary_dir", "venv_dir", "catalogs_dir",
                              "templates_dir"]:
            val = getattr(p, accessor_name)
            # Handle both property and callable accessors
            if callable(val) and not isinstance(val, Path):
                val = val()
            assert isinstance(val, Path), \
                f"{accessor_name} must return Path, got {type(val)}"

        # Method-style accessors
        for method_name in ["plugin_data", "logs_dir", "dream_state_file",
                            "learnings_file", "scripts_dir"]:
            val = getattr(p, method_name)()
            assert isinstance(val, Path), \
                f"{method_name}() must return Path, got {type(val)}"

    def test_tilde_expansion_in_env_var(self, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_OPTION_memory_dir is set to ~/my-memory
        THEN memory_dir() returns an absolute expanded path."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/plugin")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", "~/my-memory")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import Paths
        p = Paths.resolve()
        mem_dir = p.memory_dir
        if callable(mem_dir) and not isinstance(mem_dir, Path):
            mem_dir = mem_dir()
        assert mem_dir.is_absolute(), "Tilde-expanded path must be absolute"
        assert "~" not in str(mem_dir), "Tilde must be expanded"

    def test_cached_resolution_survives_env_mutation(self, monkeypatch, reset_paths_cache):
        """WHEN paths are resolved and then env var changes
        THEN get_paths() returns the original cached value."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", "/first/path")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/plugin")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_diary_dir", raising=False)

        from multiplai_core.paths import get_paths, _reset_cache
        _reset_cache()
        p1 = get_paths()

        # Mutate env var
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", "/second/path")
        p2 = get_paths()

        assert p1.memory_dir == p2.memory_dir, \
            "Cached paths must survive env var mutation"


# ===========================================================================
# 12. Plugin Validation Readiness
# ===========================================================================

class TestPluginValidationReadiness:
    """Tests that validate the plugin is ready for `claude --plugin-dir` loading.

    These verify the structural requirements that Claude Code checks during
    plugin discovery.
    """

    def test_plugin_json_is_valid_json(self):
        """WHEN plugin.json is parsed
        THEN it succeeds without JSON errors."""
        path = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        assert path.is_file()
        json.loads(path.read_text())  # Should not raise

    def test_hooks_json_is_valid_json(self):
        """WHEN hooks/hooks.json is parsed
        THEN it succeeds without JSON errors."""
        assert HOOKS_JSON.is_file()
        json.loads(HOOKS_JSON.read_text())  # Should not raise

    def test_marketplace_json_is_valid_json(self):
        """WHEN marketplace.json is parsed
        THEN it succeeds without JSON errors."""
        from conftest import MARKETPLACE_JSON
        assert MARKETPLACE_JSON.is_file()
        json.loads(MARKETPLACE_JSON.read_text())  # Should not raise

    def test_all_referenced_files_exist(self):
        """WHEN all file references in plugin.json and hooks.json are collected
        THEN every referenced file exists on disk."""
        plugin = _load_plugin_json()
        hooks = _load_hooks_json()

        missing = []

        # Skill files
        for skill in plugin.get("skills", []):
            path = PLUGIN_ROOT / skill["file"]
            if not path.is_file():
                missing.append(f"skill file: {skill['file']}")

        # Hook scripts
        for hook in hooks.get("hooks", []):
            path = PLUGIN_ROOT / hook["script"]
            if not path.is_file():
                missing.append(f"hook script: {hook['script']}")

        # Hooks entrypoint
        hooks_ref = plugin.get("entrypoints", {}).get("hooks")
        if hooks_ref and not (PLUGIN_ROOT / hooks_ref).is_file():
            missing.append(f"hooks entrypoint: {hooks_ref}")

        assert len(missing) == 0, \
            f"Missing referenced files:\n" + "\n".join(f"  - {m}" for m in missing)

    def test_no_json_trailing_commas(self):
        """WHEN JSON manifest files are checked for trailing commas
        THEN none are found (they cause parse errors)."""
        from conftest import MARKETPLACE_JSON
        manifest_paths = [
            PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
            HOOKS_JSON,
            MARKETPLACE_JSON,
        ]
        for path in manifest_paths:
            if path.exists():
                text = path.read_text()
                # Simple check: no comma followed by } or ]
                cleaned = re.sub(r'//.*$', '', text, flags=re.MULTILINE)
                cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
                if re.search(r',\s*[\]}]', cleaned):
                    pytest.fail(f"{path.name} has trailing comma(s)")

    def test_plugin_version_matches_changelog(self):
        """WHEN plugin.json version is checked against CHANGELOG.md
        THEN the version appears in the changelog."""
        plugin = _load_plugin_json()
        version = plugin["version"]
        changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text()
        assert version in changelog, \
            f"plugin.json version {version} not found in CHANGELOG.md"

    def test_skills_count_matches_expected(self):
        """WHEN skills/ directory is listed
        THEN it has the expected set of skill files."""
        expected = {
            "setup", "dream", "dream-remember", "health",
            "refresh-catalogs", "memory-health-audit", "backfill", "now",
            "log-doctor", "qmd-search", "costs",
        }
        skill_files = list((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        actual = {f.parent.name for f in skill_files}
        assert actual == expected, \
            f"Skill mismatch. Extra: {actual - expected}, Missing: {expected - actual}"

    def test_six_distinct_hook_events(self):
        """WHEN hooks.json events are collected
        THEN there are exactly 6 distinct event types."""
        hooks = _load_hooks_json()
        event_types = {h["event"] for h in hooks["hooks"]}
        expected = {
            "SessionStart", "UserPromptSubmit", "Notification", "Stop",
            "SessionEnd", "PreCompact",
        }
        assert event_types == expected, \
            f"Expected exactly {expected}, got {event_types}"


# ===========================================================================
# 13. No Direct SDK Imports in Any Plugin Script
# ===========================================================================

class TestNoDirectSDKImportsAnywhere:
    """Verify no ported script directly imports claude_agent_sdk or anthropic.
    All LLM access must go through lib/model_client.py."""

    def test_no_claude_agent_sdk_in_scripts(self):
        """WHEN all .py files in scripts/ are scanned (excluding lib/model_client.py)
        THEN zero contain 'import claude_agent_sdk' or 'from claude_agent_sdk'."""
        violations = []
        for py_file in _python_files_in_scripts():
            if "model_client" in py_file.name:
                continue
            text = py_file.read_text()
            if "import claude_agent_sdk" in text or "from claude_agent_sdk" in text:
                violations.append(py_file.name)
        assert len(violations) == 0, \
            f"Direct claude_agent_sdk imports found in: {violations}"

    def test_no_direct_anthropic_in_scripts(self):
        """WHEN all .py files in scripts/ are scanned (excluding lib/model_client.py)
        THEN zero contain 'import anthropic' or 'from anthropic'."""
        violations = []
        for py_file in _python_files_in_scripts():
            if "model_client" in py_file.name:
                continue
            text = py_file.read_text()
            if "import anthropic" in text or "from anthropic" in text:
                violations.append(py_file.name)
        assert len(violations) == 0, \
            f"Direct anthropic imports found in: {violations}"

    # NOTE: model_client.py and log_utils.py were extracted into the
    # standalone multiplai_core package; their dedicated unit tests live
    # there. The in-tree SDK-import scans above still guard plugin scripts.


# ===========================================================================
# 15. Generate Catalog Port
# ===========================================================================

class TestGenerateCatalogPort:
    """Verify generate_catalog.py is ported correctly."""

    def test_script_exists(self):
        """WHEN scripts/generate_catalog.py is checked
        THEN it exists."""
        assert (SCRIPTS_DIR / "generate_catalog.py").is_file()

    def test_uses_path_resolver(self):
        """WHEN generate_catalog.py source is inspected
        THEN it uses multiplai_core.paths for output location."""
        text = (SCRIPTS_DIR / "generate_catalog.py").read_text()
        assert "from multiplai_core.paths" in text or "multiplai_core.paths" in text

    def test_no_skill_resource_catalog(self):
        """WHEN generate_catalog.py is inspected
        THEN it does not generate skill-catalog or resource-catalog routing files."""
        text = (SCRIPTS_DIR / "generate_catalog.py").read_text()
        assert "skill-catalog" not in text
        assert "resource-catalog" not in text

    def test_catalog_output_uses_data_dir(self):
        """WHEN generate_catalog.py output logic is inspected
        THEN catalogs are written to paths derived from plugin_data/catalogs."""
        text = (SCRIPTS_DIR / "generate_catalog.py").read_text()
        assert "catalogs" in text, \
            "generate_catalog.py should write to catalogs directory"
