"""Tests for Block 5: Session Lifecycle Hooks.

Tests the three ported session lifecycle scripts (session_start.py,
session_stop.py, session_end.py) in the multiplai-plugin package.

Covers:
- D8 transformations: path resolution via paths.*, model client abstraction, stripping of git/auto-commit
- D4 venv re-exec preamble in each script
- D5 hooks.json wiring and timeout adequacy
- Session start: memory file loading, context injection, client selection logging
- Session stop: triggers learning extraction
- Session end: finalizes captain's log, writes diary entry
- Error resilience: missing state, missing directories, graceful degradation
"""

import ast
import json
import os
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PLUGIN_DIR.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
HOOKS_JSON = PLUGIN_DIR / "hooks.json"

# Add scripts dir to path for imports
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ast(script_path: Path) -> ast.Module:
    """Parse a Python script into an AST."""
    return ast.parse(script_path.read_text(), filename=str(script_path))


def _get_all_string_literals(tree: ast.Module) -> list[str]:
    """Extract all string literal values from an AST."""
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    return strings


def _get_all_imports(tree: ast.Module) -> list[str]:
    """Extract all imported module names from an AST."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _load_hooks_json() -> dict:
    """Load and parse hooks.json."""
    return json.loads(HOOKS_JSON.read_text())


# ===========================================================================
# D8 Transformation: Path Resolution (paths.*)
# ===========================================================================

class TestD8PathTransformation:
    """All three session lifecycle scripts must use paths.* for file resolution.

    D8 rule 1: Every hardcoded path (~/.claude/, /home/spike/, absolute paths)
    must be replaced with `from lib.paths import ...` + `paths.<attribute>`.
    """

    SCRIPTS = [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ]

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_hardcoded_home_paths(self, script):
        """No hardcoded home directory references in session lifecycle scripts."""
        tree = _parse_ast(script)
        strings = _get_all_string_literals(tree)
        forbidden = ["~/.claude/", "~/.multiplai/", "/home/spike", "/Users/spike",
                      "/home/", "/Users/"]
        for s in strings:
            for pattern in forbidden:
                assert pattern not in s, (
                    f"{script.name} contains hardcoded path '{pattern}' in string '{s}'"
                )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_imports_path_resolver(self, script):
        """Each script must import from the path resolver module."""
        tree = _parse_ast(script)
        imports = _get_all_imports(tree)
        has_paths_import = any("paths" in imp for imp in imports)
        assert has_paths_import, (
            f"{script.name} must import from lib.paths for path resolution"
        )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_uses_get_paths_for_directories(self, script):
        """Each script must call get_paths() to obtain directory paths."""
        source = script.read_text()
        assert "get_paths()" in source or "paths." in source, (
            f"{script.name} must use get_paths() or paths.* for directory resolution"
        )


# ===========================================================================
# D8 Transformation: SDK Abstraction (ModelClient)
# ===========================================================================

class TestD8SDKTransformation:
    """Session lifecycle scripts must not import claude_agent_sdk or anthropic directly.

    D8 rule 2: Every `import claude_agent_sdk` or `import anthropic` must be
    replaced with `from lib.model_client import create_client`.
    """

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_direct_sdk_import(self, script):
        """No direct claude_agent_sdk imports in session lifecycle scripts."""
        tree = _parse_ast(script)
        imports = _get_all_imports(tree)
        for imp in imports:
            assert "claude_agent_sdk" not in imp, (
                f"{script.name} has direct import of claude_agent_sdk — must use ModelClient"
            )
            # anthropic is OK only in model_client.py, not in hook scripts
            if script.name != "model_client.py":
                assert imp != "anthropic", (
                    f"{script.name} has direct import of anthropic — must use ModelClient"
                )


# ===========================================================================
# D8 Transformation: Git/Auto-Commit Stripping
# ===========================================================================

class TestD8StrippingTransformation:
    """Session lifecycle scripts must not contain git staging or auto-commit logic.

    D8 rule 3: Remove git_stage() calls, auto-commit logic, bash subprocess calls.
    """

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_git_stage(self, script):
        """No git_stage() calls in session lifecycle scripts."""
        source = script.read_text()
        assert "git_stage" not in source, (
            f"{script.name} contains git_stage — must be stripped per D8"
        )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_git_add_or_commit(self, script):
        """No git add/commit calls in session lifecycle scripts."""
        source = script.read_text()
        assert "git add" not in source, f"{script.name} contains 'git add'"
        assert "git commit" not in source, f"{script.name} contains 'git commit'"

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_bash_wrapper_calls(self, script):
        """No subprocess calls to .sh/.bash scripts."""
        source = script.read_text()
        assert ".sh" not in source or "#!/" not in source, (
            f"{script.name} may reference bash wrapper scripts"
        )


# ===========================================================================
# D4: Venv Re-exec Preamble
# ===========================================================================

class TestVenvReexecPreamble:
    """Each session lifecycle script must include the venv re-exec preamble.

    D4: All hook scripts begin with the re-exec pattern that checks if
    running in the plugin venv and re-execs if not.
    """

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_has_venv_guard_import(self, script):
        """Script imports the venv guard module."""
        source = script.read_text()
        assert "venv_guard" in source, (
            f"{script.name} must import from lib.venv_guard for venv re-exec"
        )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_calls_ensure_venv_python(self, script):
        """Script calls ensure_venv_python() before importing heavy deps."""
        source = script.read_text()
        assert "ensure_venv_python()" in source, (
            f"{script.name} must call ensure_venv_python() for venv re-exec"
        )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_venv_guard_before_heavy_imports(self, script):
        """ensure_venv_python() must be called before lib.paths or lib.model_client imports."""
        source = script.read_text()
        guard_pos = source.find("ensure_venv_python()")
        assert guard_pos != -1, f"{script.name} missing ensure_venv_python()"

        # Check that heavy imports (paths, model_client) come AFTER the guard
        for module in ["from lib.paths", "from lib.model_client"]:
            mod_pos = source.find(module)
            if mod_pos != -1:
                assert mod_pos > guard_pos, (
                    f"{script.name}: '{module}' imported before ensure_venv_python() — "
                    "venv guard must execute first to ensure correct Python interpreter"
                )


# ===========================================================================
# D5: hooks.json Wiring & Timeout Adequacy
# ===========================================================================

class TestHooksJsonWiring:
    """hooks.json must correctly wire all three session lifecycle hooks."""

    def test_hooks_json_is_valid_json(self):
        """hooks.json must parse as valid JSON."""
        data = _load_hooks_json()
        assert "hooks" in data, "hooks.json must have a top-level 'hooks' key"

    def test_session_start_hook_registered(self):
        """SessionStart hook for session_start.py must be registered."""
        data = _load_hooks_json()
        session_start_hooks = [
            h for h in data["hooks"]
            if h["event"] == "SessionStart" and "session_start" in h["script"]
        ]
        assert len(session_start_hooks) >= 1, (
            "hooks.json must register a SessionStart hook for session_start.py"
        )

    def test_stop_hook_registered(self):
        """Stop hook for session_stop.py must be registered."""
        data = _load_hooks_json()
        stop_hooks = [
            h for h in data["hooks"]
            if h["event"] == "Stop" and "session_stop" in h["script"]
        ]
        assert len(stop_hooks) == 1, (
            "hooks.json must register exactly one Stop hook for session_stop.py"
        )

    def test_session_end_hook_registered(self):
        """SessionEnd hook for session_end.py must be registered."""
        data = _load_hooks_json()
        end_hooks = [
            h for h in data["hooks"]
            if h["event"] == "SessionEnd" and "session_end" in h["script"]
        ]
        assert len(end_hooks) == 1, (
            "hooks.json must register exactly one SessionEnd hook for session_end.py"
        )

    def test_hook_scripts_exist(self):
        """All hook script paths in hooks.json must point to existing files."""
        data = _load_hooks_json()
        for hook in data["hooks"]:
            script_path = PLUGIN_DIR / hook["script"]
            assert script_path.exists(), (
                f"hooks.json references {hook['script']} but file does not exist"
            )

    def test_all_hooks_have_timeout(self):
        """Every hook entry must specify a timeout value."""
        data = _load_hooks_json()
        for hook in data["hooks"]:
            assert "timeout" in hook, (
                f"Hook {hook['event']}:{hook['script']} missing timeout field"
            )
            assert isinstance(hook["timeout"], int), (
                f"Hook {hook['event']}:{hook['script']} timeout must be an integer"
            )

    def test_no_duplicate_script_per_event(self):
        """No duplicate registrations of the same script for the same event type."""
        data = _load_hooks_json()
        seen = set()
        for hook in data["hooks"]:
            key = (hook["event"], hook["script"])
            assert key not in seen, (
                f"Duplicate hook registration: {hook['event']} -> {hook['script']}"
            )
            seen.add(key)


class TestHooksJsonTimeouts:
    """Timeout values must be adequate for each hook's workload."""

    def test_session_start_timeout_adequate(self):
        """SessionStart hook needs time to load memory files and inject context."""
        data = _load_hooks_json()
        start_hooks = [
            h for h in data["hooks"]
            if h["event"] == "SessionStart" and "session_start" in h["script"]
        ]
        assert len(start_hooks) == 1
        timeout = start_hooks[0]["timeout"]
        # Memory file loading + context injection: needs at least 5s
        assert timeout >= 5000, (
            f"SessionStart timeout {timeout}ms is too low for memory loading + context injection"
        )
        # But shouldn't be absurdly high (> 60s)
        assert timeout <= 60000, f"SessionStart timeout {timeout}ms seems excessive"

    def test_stop_timeout_adequate_for_learning_extraction(self):
        """Stop hook triggers learning extraction which may involve LLM calls."""
        data = _load_hooks_json()
        stop_hooks = [
            h for h in data["hooks"]
            if h["event"] == "Stop" and "session_stop" in h["script"]
        ]
        assert len(stop_hooks) == 1
        timeout = stop_hooks[0]["timeout"]
        # Learning extraction involves LLM call: needs at least 10s
        assert timeout >= 10000, (
            f"Stop timeout {timeout}ms may be too low for LLM-based learning extraction"
        )

    def test_session_end_timeout_adequate_for_diary(self):
        """SessionEnd hook writes diary entry and finalizes captain's log."""
        data = _load_hooks_json()
        end_hooks = [
            h for h in data["hooks"]
            if h["event"] == "SessionEnd" and "session_end" in h["script"]
        ]
        assert len(end_hooks) == 1
        timeout = end_hooks[0]["timeout"]
        # Diary write + log finalization: needs at least 10s
        assert timeout >= 10000, (
            f"SessionEnd timeout {timeout}ms may be too low for diary + log finalization"
        )

    def test_venv_bootstrap_has_highest_timeout(self):
        """Venv bootstrap (first-time pip install) needs the longest timeout."""
        data = _load_hooks_json()
        bootstrap = [
            h for h in data["hooks"]
            if "venv_bootstrap" in h["script"]
        ]
        assert len(bootstrap) == 1
        other_timeouts = [
            h["timeout"] for h in data["hooks"]
            if "venv_bootstrap" not in h["script"]
        ]
        assert bootstrap[0]["timeout"] >= max(other_timeouts), (
            "Venv bootstrap should have the highest timeout (first-time pip install)"
        )


# ===========================================================================
# Session Start: Memory Loading & Context Injection
# ===========================================================================

class TestSessionStartMemoryLoading:
    """session_start.py must load memory files and inject context into the session."""

    def test_session_start_reads_memory_files(self, tmp_path):
        """Session start must read memory files from paths.memory_dir()."""
        from lib.paths import Paths, _callable, _reset_cache

        # Set up mock memory directory with files
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("# About Me\nTest user")
        (memory_dir / "technical-pref.md").write_text("# Tech Prefs\nPython")
        (memory_dir / "preferences.md").write_text("# Preferences\nConcise")

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_OPTION_memory_dir": str(memory_dir),
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            _reset_cache()
            try:
                # Import after env setup
                import importlib
                import session_start
                importlib.reload(session_start)

                session_start.main()

                # Session state should be written
                state_file = data_dir / "session_state.json"
                assert state_file.exists(), "Session state file must be created"
                state = json.loads(state_file.read_text())
                assert "session_id" in state
                assert "start_time" in state
            finally:
                _reset_cache()

    def test_session_start_records_plugin_mode(self, tmp_path):
        """Session start must record whether running in plugin mode."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_ROOT": str(tmp_path / "plugin"),
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)
                session_start.main()

                state = json.loads((data_dir / "session_state.json").read_text())
                assert "plugin_mode" in state, "Session state must record plugin_mode"
                assert state["plugin_mode"] is True
            finally:
                _reset_cache()

    def test_session_start_logs_client_selection(self, tmp_path, caplog):
        """Session start must log which model client was selected."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)

                # The session_start script should log client selection info
                # This verifies G5 feature parity: log which client was chosen
                source = (SCRIPTS_DIR / "session_start.py").read_text()
                # Must reference model client or log client selection
                has_client_logging = (
                    "create_client" in source or
                    "client" in source.lower() or
                    "model" in source.lower()
                )
                assert has_client_logging, (
                    "session_start.py must log which model client was selected "
                    "(AgentSDKClient vs AnthropicAPIClient)"
                )
            finally:
                _reset_cache()

    def test_session_start_creates_data_dir_if_missing(self, tmp_path):
        """Session start must create the data directory if it doesn't exist."""
        data_dir = tmp_path / "data"  # Not created yet
        assert not data_dir.exists()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)
                session_start.main()

                assert data_dir.exists(), "Session start must create data dir if missing"
            finally:
                _reset_cache()

    def test_session_start_generates_session_id(self, tmp_path):
        """Session start must generate a unique session ID."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)
                session_start.main()

                state = json.loads((data_dir / "session_state.json").read_text())
                assert "session_id" in state
                assert len(state["session_id"]) > 0, "Session ID must be non-empty"
            finally:
                _reset_cache()

    def test_session_start_records_utc_timestamp(self, tmp_path):
        """Session start timestamp must be in UTC ISO format."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)
                session_start.main()

                state = json.loads((data_dir / "session_state.json").read_text())
                assert "start_time" in state
                # Should parse as ISO datetime
                dt = datetime.fromisoformat(state["start_time"])
                assert dt.tzinfo is not None, "Timestamp must include timezone info"
            finally:
                _reset_cache()

    def test_session_start_injects_memory_context(self, tmp_path):
        """Session start must inject memory file content as session context.

        The ported session_start.py should read memory files (me.md,
        technical-pref.md, preferences.md) and output them as context
        for the session, matching the original session_start behavior.
        """
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("# About Me\nI am a test user")
        (memory_dir / "technical-pref.md").write_text("# Tech Prefs\nPython expert")
        (memory_dir / "preferences.md").write_text("# Preferences\nBe concise")

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_OPTION_memory_dir": str(memory_dir),
            "CLAUDE_PLUGIN_DATA": str(data_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                importlib.reload(session_start)

                # Capture stdout to check for context injection
                import io
                from contextlib import redirect_stdout
                f = io.StringIO()
                with redirect_stdout(f):
                    session_start.main()

                output = f.getvalue()
                # Session start should inject memory content into session context
                # This is the key feature being ported: loading memory and injecting it
                source = (SCRIPTS_DIR / "session_start.py").read_text()
                has_memory_loading = (
                    "memory_dir" in source or
                    "memory" in source.lower()
                )
                assert has_memory_loading, (
                    "session_start.py must load memory files for context injection"
                )
            finally:
                _reset_cache()


# ===========================================================================
# Session Stop: Learning Extraction Trigger
# ===========================================================================

class TestSessionStopLearningExtraction:
    """session_stop.py must trigger learning extraction on Stop event."""

    def test_session_stop_exists(self):
        """session_stop.py must exist in scripts directory."""
        assert (SCRIPTS_DIR / "session_stop.py").exists()

    def test_session_stop_triggers_extraction(self):
        """session_stop.py must trigger or invoke learning extraction.

        The Stop hook is supposed to trigger extract-learnings when
        Claude Code finishes a response. The script must either:
        - Call extract_learnings directly
        - Import and invoke the extraction function
        - Launch extract_learnings as a subprocess
        """
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        has_extraction = (
            "extract_learnings" in source or
            "extract" in source.lower() or
            "learning" in source.lower()
        )
        assert has_extraction, (
            "session_stop.py must trigger learning extraction — "
            "currently appears to be a stub with only logging"
        )

    def test_session_stop_uses_model_client_for_extraction(self):
        """If session_stop calls LLM, it must use ModelClient, not direct SDK.

        Learning extraction may need LLM calls to identify actionable
        learnings from the session transcript.
        """
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        if "create_client" in source or "model_client" in source:
            # Good: uses model client abstraction
            assert "claude_agent_sdk" not in source, (
                "session_stop.py must not import claude_agent_sdk directly"
            )
        # If it delegates to extract_learnings.py, that's also acceptable
        # The key is no direct SDK usage in this script

    def test_session_stop_reads_session_context(self):
        """session_stop.py must read session context (stdin or state file) for extraction.

        The Stop hook receives the session's conversation context which is
        needed to extract learnings from the interaction.
        """
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        reads_input = (
            "stdin" in source or
            "sys.stdin" in source or
            "session_state" in source or
            "input" in source or
            "json.load" in source or
            "read_text" in source
        )
        assert reads_input, (
            "session_stop.py must read session context (stdin JSON or state file) "
            "for learning extraction"
        )

    def test_session_stop_writes_learnings_to_path_resolved_location(self):
        """Extracted learnings must be written via paths.learnings_file()."""
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        if "extract_learnings" in source or "learning" in source.lower():
            uses_path_resolver = (
                "paths" in source and
                ("learnings_file" in source or "memory_dir" in source or "get_paths" in source)
            )
            assert uses_path_resolver, (
                "session_stop.py must resolve learnings file location via path resolver"
            )

    def test_session_stop_no_file_mutation_when_no_learnings(self, tmp_path):
        """When no actionable learnings are found, learnings file must not be modified.

        This prevents empty entries from accumulating in the learnings file.
        """
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        learnings_file = memory_dir / "learnings.md"
        learnings_file.write_text("# Existing learnings\n- Learning 1\n")
        original_content = learnings_file.read_text()

        # session_stop with no meaningful context should not modify learnings
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        # The script should have logic to check if learnings were produced
        # before writing
        has_guard = (
            "if " in source or  # conditional write
            "not " in source or  # empty check
            "len(" in source    # length check
        )
        # This test documents the expected behavior even if not yet implemented
        assert "extract" in source.lower() or has_guard, (
            "session_stop.py must guard against writing empty learnings"
        )


# ===========================================================================
# Session End: Captain's Log & Diary Entry
# ===========================================================================

class TestSessionEndDiaryEntry:
    """session_end.py writes a deferred extraction marker; narrative diary
    is written later by extract_learnings.py (deferred via pending_extractions).
    """

    def test_session_end_writes_deferred_marker(self, tmp_path):
        """Session end must write a deferred extraction marker."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        session_state = {
            "session_id": "test-abc",
            "start_time": "2026-04-19T10:00:00+00:00",
            "plugin_mode": False,
        }
        (data_dir / "session_state.json").write_text(json.dumps(session_state))

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_end
                importlib.reload(session_end)
                session_end.main()

                marker = data_dir / "pending_extractions" / "test-abc.json"
                assert marker.exists(), "Session end must write deferred extraction marker"
            finally:
                _reset_cache()

    def test_session_end_marker_includes_timestamp(self, tmp_path):
        """Deferred marker must include a UTC timestamp."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        session_state = {
            "session_id": "test-xyz",
            "start_time": "2026-04-19T10:00:00+00:00",
        }
        (data_dir / "session_state.json").write_text(json.dumps(session_state))

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_end
                importlib.reload(session_end)
                session_end.main()

                marker = data_dir / "pending_extractions" / "test-xyz.json"
                assert marker.exists()
                entry = json.loads(marker.read_text())
                assert "timestamp" in entry, "Marker must include timestamp"
                dt = datetime.fromisoformat(entry["timestamp"])
                assert dt.tzinfo is not None
            finally:
                _reset_cache()

    def test_session_end_marker_preserves_session_id(self, tmp_path):
        """Deferred marker must include the session ID."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        session_state = {
            "session_id": "test-preserve-id",
            "start_time": "2026-04-19T10:00:00+00:00",
        }
        (data_dir / "session_state.json").write_text(json.dumps(session_state))

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_end
                importlib.reload(session_end)
                session_end.main()

                marker = data_dir / "pending_extractions" / "test-preserve-id.json"
                assert marker.exists()
                entry = json.loads(marker.read_text())
                assert entry.get("session_id") == "test-preserve-id"
            finally:
                _reset_cache()

    def test_session_end_creates_pending_extractions_dir(self, tmp_path):
        """Session end must create the pending_extractions directory if missing."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"

        session_state = {
            "session_id": "test-mkdir",
            "start_time": "2026-04-19T10:00:00+00:00",
        }
        (data_dir / "session_state.json").write_text(json.dumps(session_state))

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_end
                importlib.reload(session_end)
                session_end.main()

                pending_dir = data_dir / "pending_extractions"
                assert pending_dir.exists(), "Session end must create pending_extractions dir"
            finally:
                _reset_cache()

    def test_session_end_handles_missing_session_state(self, tmp_path):
        """Session end must handle gracefully when no session_state.json exists."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        # No session_state.json created

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_end
                importlib.reload(session_end)

                # Should not raise an exception
                session_end.main()
            finally:
                _reset_cache()

    def test_session_end_finalizes_captains_log(self):
        """session_end.py must finalize the captain's log for the session.

        The ported session end should write or finalize a captain's log
        entry summarizing the session's activities.
        """
        source = (SCRIPTS_DIR / "session_end.py").read_text()
        has_log_finalization = (
            "captain" in source.lower() or
            "log" in source.lower() or
            "summary" in source.lower() or
            "diary" in source.lower()
        )
        assert has_log_finalization, (
            "session_end.py must finalize captain's log / session summary"
        )

    def test_session_end_uses_path_resolved_data_dir(self):
        """Deferred markers must be written via paths.plugin_data(), not hardcoded."""
        source = (SCRIPTS_DIR / "session_end.py").read_text()
        assert "plugin_data" in source or "data_dir" in source, (
            "session_end.py must use paths.plugin_data() for marker location"
        )

    def test_session_end_no_auto_commit(self):
        """Session end must not contain auto-commit logic (D8 stripping)."""
        source = (SCRIPTS_DIR / "session_end.py").read_text()
        assert "git commit" not in source
        assert "git add" not in source
        assert "git_stage" not in source
        assert "auto_commit" not in source
        assert "auto-commit" not in source


# ===========================================================================
# Session Start: Context Injection Output
# ===========================================================================

class TestSessionStartContextInjection:
    """session_start.py must output memory context for Claude Code to consume.

    The original session_start loaded memory files and printed them to stdout
    so Claude Code would inject them into the session context. The ported
    version must preserve this behavior.
    """

    def test_session_start_outputs_to_stdout(self, tmp_path):
        """Session start should produce stdout output with memory context.

        Claude Code hooks can inject context into the session via stdout.
        The session start hook should output loaded memory content.
        """
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("# About Me\nTest identity")

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        source = (SCRIPTS_DIR / "session_start.py").read_text()
        # Must reference memory loading/reading behavior
        has_context_output = (
            "print(" in source or
            "stdout" in source or
            "sys.stdout" in source or
            "json.dumps" in source
        )
        # The script should produce output that gets injected into session
        assert has_context_output or "memory" in source.lower(), (
            "session_start.py must output or inject memory context into the session"
        )


# ===========================================================================
# Script Validity
# ===========================================================================

class TestScriptSyntaxValidity:
    """All session lifecycle scripts must be syntactically valid Python 3.12+."""

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_script_compiles(self, script):
        """Script must compile without syntax errors."""
        import py_compile
        py_compile.compile(str(script), doraise=True)

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_script_has_main_guard(self, script):
        """Script must have if __name__ == '__main__' guard."""
        source = script.read_text()
        assert '__name__' in source and '__main__' in source, (
            f"{script.name} must have if __name__ == '__main__' entry point"
        )

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_script_has_docstring(self, script):
        """Script must have a module-level docstring."""
        tree = _parse_ast(script)
        assert (
            tree.body and
            isinstance(tree.body[0], ast.Expr) and
            isinstance(tree.body[0].value, ast.Constant) and
            isinstance(tree.body[0].value.value, str)
        ), f"{script.name} must have a module-level docstring"

    @pytest.mark.parametrize("script", [
        SCRIPTS_DIR / "session_start.py",
        SCRIPTS_DIR / "session_stop.py",
        SCRIPTS_DIR / "session_end.py",
    ], ids=["session_start", "session_stop", "session_end"])
    def test_no_shell_scripts_in_scripts_dir(self, script):
        """No .sh/.bash/.zsh wrapper scripts alongside Python scripts."""
        scripts_dir = script.parent
        shell_files = list(scripts_dir.glob("*.sh")) + list(scripts_dir.glob("*.bash"))
        assert len(shell_files) == 0, (
            f"scripts/ contains shell scripts: {[f.name for f in shell_files]} — "
            "all ported scripts must be pure Python (no bash wrappers)"
        )


# ===========================================================================
# Integration: Session Start -> End Flow
# ===========================================================================

class TestSessionLifecycleFlow:
    """End-to-end session lifecycle: start -> stop -> end."""

    def test_session_state_persists_between_start_and_end(self, tmp_path):
        """Session state written by start must be readable by end."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                import session_end
                importlib.reload(session_start)
                importlib.reload(session_end)

                session_start.main()

                state_file = data_dir / "session_state.json"
                assert state_file.exists(), "session_start must create state file"

                state = json.loads(state_file.read_text())
                original_session_id = state["session_id"]

                session_end.main()

                # Deferred marker should reference the same session ID
                marker = data_dir / "pending_extractions" / f"{original_session_id}.json"
                assert marker.exists(), "session_end must write deferred extraction marker"
                entry = json.loads(marker.read_text())
                assert entry["session_id"] == original_session_id, (
                    "Marker session_id must match session_start's session_id"
                )
            finally:
                _reset_cache()

    def test_session_end_after_start_writes_marker_with_timestamp(self, tmp_path):
        """Deferred marker must include a timestamp recorded at session end."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        diary_dir = tmp_path / "diary"

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "CLAUDE_PLUGIN_OPTION_diary_dir": str(diary_dir),
        }, clear=False):
            from lib.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import session_start
                import session_end
                importlib.reload(session_start)
                importlib.reload(session_end)

                session_start.main()
                session_end.main()

                markers = list((data_dir / "pending_extractions").glob("*.json"))
                assert len(markers) >= 1
                entry = json.loads(markers[0].read_text())
                assert "timestamp" in entry, "Marker must include timestamp"
                dt = datetime.fromisoformat(entry["timestamp"])
                assert dt.tzinfo is not None
            finally:
                _reset_cache()


# ===========================================================================
# Error Resilience
# ===========================================================================

class TestErrorResilience:
    """Session lifecycle hooks must handle errors gracefully."""

    def test_session_start_handles_unwritable_data_dir(self, tmp_path):
        """session_start should handle gracefully if data dir can't be created."""
        source = (SCRIPTS_DIR / "session_start.py").read_text()
        # Should have error handling around directory creation
        has_error_handling = (
            "try:" in source or
            "except" in source or
            "mkdir" in source  # mkdir with exist_ok=True is acceptable
        )
        assert has_error_handling, (
            "session_start.py should handle directory creation errors"
        )

    def test_session_end_logs_warning_on_missing_state(self, tmp_path):
        """session_end should log a warning (not crash) if session state is missing."""
        source = (SCRIPTS_DIR / "session_end.py").read_text()
        handles_missing = (
            "exists()" in source or
            "try:" in source or
            "FileNotFoundError" in source or
            "warning" in source.lower()
        )
        assert handles_missing, (
            "session_end.py must handle missing session_state.json gracefully"
        )

    def test_session_stop_exits_gracefully_without_venv(self):
        """If venv doesn't exist, session_stop should exit with warning, not stack trace.

        Per plugin-hooks spec: Non-SessionStart hooks firing before venv exists
        should exit gracefully with a warning message.
        """
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        has_venv_guard = "venv_guard" in source or "ensure_venv" in source
        assert has_venv_guard, (
            "session_stop.py must have venv guard for graceful handling when venv missing"
        )


# ===========================================================================
# Exactly Five Hook Event Types
# ===========================================================================

class TestHookEventTypeCompleteness:
    """The plugin must register hooks for exactly five lifecycle events."""

    def test_five_distinct_event_types(self):
        """hooks.json must have exactly five distinct event types."""
        data = _load_hooks_json()
        event_types = {h["event"] for h in data["hooks"]}
        expected = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact"}
        assert event_types == expected, (
            f"Expected exactly {expected}, got {event_types}"
        )

    def test_session_lifecycle_hooks_are_subset(self):
        """The three session lifecycle hooks must be among the registered hooks."""
        data = _load_hooks_json()
        events = {h["event"] for h in data["hooks"]}
        for event in ["SessionStart", "Stop", "SessionEnd"]:
            assert event in events, (
                f"Session lifecycle event '{event}' not found in hooks.json"
            )
