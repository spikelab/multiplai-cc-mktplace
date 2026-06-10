"""Tests for Block 7: Learning Extraction & Dream (plugin port).

Covers the four plugin scripts ported from claude-code-multiplai-context:
  - scripts/extract_learnings.py — Extract learnings with path/SDK/stripping transforms
  - scripts/dream.py — Dream consolidation with async model client
  - scripts/synthesize_now.py — Manual dream trigger
  - scripts/pre_compact.py — PreCompact hook event handler

Every WHEN/THEN scenario validates:
  1. Path resolution via paths.* (no hardcoded paths)
  2. LLM calls via ModelClient (no direct SDK imports)
  3. git_stage / auto-commit logic stripped
  4. Memory file writes to paths.memory_dir / paths.diary_dir
  5. Re-exec venv preamble present
  6. Correct async patterns for concurrent LLM calls

These tests MUST FAIL against the current stub implementations.
"""

import ast
import asyncio
import inspect
import os
import re
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PLUGIN_ROOT.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

# Script paths
EXTRACT_LEARNINGS = SCRIPTS_DIR / "extract_learnings.py"
DREAM = SCRIPTS_DIR / "dream.py"
SYNTHESIZE_NOW = SCRIPTS_DIR / "synthesize_now.py"
PRE_COMPACT = SCRIPTS_DIR / "pre_compact.py"

ALL_SCRIPTS = [EXTRACT_LEARNINGS, DREAM, SYNTHESIZE_NOW, PRE_COMPACT]
ALL_SCRIPT_NAMES = [
    "extract_learnings.py",
    "dream.py",
    "synthesize_now.py",
    "pre_compact.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_source(path: Path) -> str:
    """Read a script's source code."""
    return path.read_text(encoding="utf-8")


def _parse_ast(path: Path) -> ast.Module:
    """Parse a script to AST."""
    return ast.parse(_read_source(path), filename=str(path))


def _find_imports(tree: ast.Module) -> list[str]:
    """Collect all imported module names from AST."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


# ===================================================================
# Requirement: Re-exec venv preamble present in all four scripts
# ===================================================================

class TestVenvPreamble:
    """All four ported scripts must include the venv re-exec preamble
    so they run inside the plugin's virtual environment."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_imports_venv_guard(self, script_path):
        """WHEN any of the four scripts is inspected
        THEN it imports from lib.venv_guard."""
        source = _read_source(script_path)
        assert "venv_guard" in source, (
            f"{script_path.name} must import from lib.venv_guard "
            "for venv re-exec preamble"
        )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_calls_ensure_venv_python(self, script_path):
        """WHEN any of the four scripts is inspected
        THEN it calls ensure_venv_python() at module level, before other lib imports."""
        source = _read_source(script_path)
        assert "ensure_venv_python()" in source, (
            f"{script_path.name} must call ensure_venv_python() "
            "to re-exec into the plugin venv"
        )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_venv_guard_before_lib_imports(self, script_path):
        """WHEN the script source is read
        THEN ensure_venv_python() appears BEFORE imports of lib.paths / lib.model_client."""
        source = _read_source(script_path)
        venv_pos = source.find("ensure_venv_python()")
        paths_pos = source.find("from lib.paths")
        model_pos = source.find("from lib.model_client")

        if paths_pos != -1:
            assert venv_pos < paths_pos, (
                f"{script_path.name}: ensure_venv_python() must appear "
                "before 'from lib.paths' import"
            )
        if model_pos != -1:
            assert venv_pos < model_pos, (
                f"{script_path.name}: ensure_venv_python() must appear "
                "before 'from lib.model_client' import"
            )


# ===================================================================
# Requirement: No direct claude_agent_sdk imports in ported scripts
# ===================================================================

class TestNoDirectSDKImports:
    """Every ported script must use ModelClient abstraction.
    No script may import claude_agent_sdk or anthropic directly."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_claude_agent_sdk_import(self, script_path):
        """WHEN the script source is searched for claude_agent_sdk imports
        THEN zero matches are found."""
        source = _read_source(script_path)
        assert "import claude_agent_sdk" not in source, (
            f"{script_path.name} must not import claude_agent_sdk directly — "
            "use ModelClient abstraction"
        )
        assert "from claude_agent_sdk" not in source, (
            f"{script_path.name} must not import from claude_agent_sdk — "
            "use ModelClient abstraction"
        )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_direct_anthropic_import(self, script_path):
        """WHEN the script source is searched for anthropic imports
        THEN zero matches are found (those belong only in model_client.py)."""
        source = _read_source(script_path)
        assert "import anthropic" not in source, (
            f"{script_path.name} must not import anthropic directly — "
            "use ModelClient abstraction"
        )
        assert "from anthropic" not in source, (
            f"{script_path.name} must not import from anthropic — "
            "use ModelClient abstraction"
        )


# ===================================================================
# Requirement: No hardcoded path constants in ported scripts
# ===================================================================

class TestNoHardcodedPaths:
    """Every ported script must resolve file paths via paths.* —
    no hardcoded home-directory paths."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_hardcoded_home_paths(self, script_path):
        """WHEN scripts are searched for hardcoded home directory references
        THEN zero matches reference kit-specific paths."""
        source = _read_source(script_path)
        forbidden = [
            "~/.claude/",
            "~/.multiplai/",
            "/home/spike",
            "/Users/spike",
            "claude-code-multiplai",
            "CLAUDE_MULTIPLAI_HOME",
        ]
        for pattern in forbidden:
            assert pattern not in source, (
                f"{script_path.name} contains hardcoded path '{pattern}' — "
                "must use paths.* from lib.paths instead"
            )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_uses_path_resolver(self, script_path):
        """WHEN the script needs file paths
        THEN it imports from lib.paths."""
        source = _read_source(script_path)
        has_paths_import = (
            "from lib.paths" in source or
            "import lib.paths" in source
        )
        assert has_paths_import, (
            f"{script_path.name} must import from lib.paths for path resolution"
        )


# ===================================================================
# Requirement: No git_stage / auto-commit logic (D8 stripping)
# ===================================================================

class TestGitLogicStripped:
    """All ported scripts must have git_stage(), git add, git commit
    logic stripped per D8 porting strategy."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_git_stage(self, script_path):
        """WHEN the ported script source is inspected
        THEN it contains no reference to git_stage."""
        source = _read_source(script_path)
        assert "git_stage" not in source, (
            f"{script_path.name} must not contain git_stage — "
            "this was stripped during porting (D8)"
        )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_git_add_commit(self, script_path):
        """WHEN the ported script source is inspected
        THEN it contains no git add or git commit calls."""
        source = _read_source(script_path)
        assert "git add" not in source, (
            f"{script_path.name} must not contain 'git add' — stripped per D8"
        )
        assert "git commit" not in source, (
            f"{script_path.name} must not contain 'git commit' — stripped per D8"
        )


# ===================================================================
# Requirement: All scripts are syntactically valid Python
# ===================================================================

class TestSyntaxValidity:
    """All four scripts must parse as valid Python 3.12+."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_script_parses(self, script_path):
        """WHEN each script is compiled
        THEN compilation succeeds with no syntax errors."""
        source = _read_source(script_path)
        try:
            compile(source, str(script_path), "exec")
        except SyntaxError as e:
            pytest.fail(f"{script_path.name} has syntax error: {e}")


# ===================================================================
# Requirement: Scripts use sys.path setup for lib imports
# ===================================================================

class TestSysPathSetup:
    """Scripts must set up sys.path so lib.* imports work."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_sys_path_insert(self, script_path):
        """WHEN each script is read
        THEN it inserts the scripts directory into sys.path."""
        source = _read_source(script_path)
        assert "sys.path" in source, (
            f"{script_path.name} must configure sys.path for lib.* imports"
        )


# ===================================================================
# Requirement: Extract Learnings Port — specific behaviors
# ===================================================================

class TestExtractLearningsPort:
    """extract_learnings.py must be ported with path/SDK/stripping transforms."""

    def test_uses_model_client_for_llm(self):
        """WHEN extract-learnings calls the LLM to identify learnings
        THEN it uses a ModelClient instance via create_client()."""
        source = _read_source(EXTRACT_LEARNINGS)
        assert "create_client" in source, (
            "extract_learnings.py must use create_client() from lib.model_client "
            "for LLM summarization"
        )

    def test_uses_paths_for_learnings_file(self):
        """WHEN extract-learnings writes learnings
        THEN it resolves the path via paths.learnings_file() or equivalent."""
        source = _read_source(EXTRACT_LEARNINGS)
        assert "learnings_file" in source, (
            "extract_learnings.py must use paths.learnings_file() to resolve "
            "the learnings output path"
        )

    def test_reads_session_input(self):
        """WHEN extract-learnings runs as a Stop hook
        THEN it reads session transcript or conversation data from stdin/input."""
        source = _read_source(EXTRACT_LEARNINGS)
        reads_input = (
            "stdin" in source or
            "sys.stdin" in source or
            "json.load" in source or
            "input()" in source or
            "hook_input" in source or
            "read()" in source
        )
        assert reads_input, (
            "extract_learnings.py must read session data (stdin JSON or hook input) "
            "to extract learnings from the conversation"
        )

    def test_async_main_function(self):
        """WHEN extract-learnings is inspected
        THEN it has an async entry point for LLM calls."""
        source = _read_source(EXTRACT_LEARNINGS)
        assert "async def" in source, (
            "extract_learnings.py must have async function(s) for LLM calls"
        )
        assert "asyncio.run" in source, (
            "extract_learnings.py must use asyncio.run() to execute async code"
        )

    def test_no_learnings_produces_no_file_mutation(self):
        """WHEN extract-learnings runs but the LLM returns no actionable learnings
        THEN the learnings file is not modified and no empty entries are appended.

        The script must have a conditional check before writing."""
        source = _read_source(EXTRACT_LEARNINGS)
        # Must have a guard that checks if learnings were produced before writing
        has_guard = (
            "if not learnings" in source or
            "if learnings" in source or
            "if len(learnings)" in source or
            "if result" in source or
            "no learnings" in source.lower() or
            "nothing to" in source.lower()
        )
        assert has_guard, (
            "extract_learnings.py must guard against writing empty learnings — "
            "no file mutation when LLM returns no actionable learnings"
        )

    def test_appends_not_overwrites(self):
        """WHEN extract-learnings produces new learnings
        THEN they are appended to the existing file, not overwriting it."""
        source = _read_source(EXTRACT_LEARNINGS)
        has_append = (
            '"a"' in source or
            "'a'" in source or
            "append" in source or
            "mode='a'" in source or
            'mode="a"' in source
        )
        assert has_append, (
            "extract_learnings.py must append learnings to the file, not overwrite. "
            "Expected file open mode 'a' or explicit append logic."
        )

    def test_writes_to_memory_dir(self):
        """WHEN extract-learnings produces learnings
        THEN they are written under paths.memory_dir() (via learnings_file)."""
        source = _read_source(EXTRACT_LEARNINGS)
        has_memory_path = (
            "memory_dir" in source or
            "learnings_file" in source
        )
        assert has_memory_path, (
            "extract_learnings.py must write learnings to a path derived from "
            "paths.memory_dir() or paths.learnings_file()"
        )


# ===================================================================
# Requirement: Dream Port — specific behaviors
# ===================================================================

class TestAutodreamPort:
    """dream.py must be ported with concurrent LLM calls via async model client."""

    def test_uses_model_client(self):
        """WHEN dream invokes the LLM to consolidate learnings
        THEN it uses ModelClient methods via create_client()."""
        source = _read_source(DREAM)
        assert "create_client" in source, (
            "dream.py must use create_client() from lib.model_client "
            "for LLM synthesis"
        )

    def test_reads_learnings_from_path_resolver(self):
        """WHEN dream triggers a consolidation cycle
        THEN it reads learnings from a path-resolved location (learnings_dir or learnings_file)."""
        source = _read_source(DREAM)
        assert "learnings_dir" in source or "learnings_file" in source, (
            "dream.py must read learnings from a path-resolved location "
            "(paths.learnings_dir or paths.learnings_file()), not from a hardcoded path"
        )

    def test_reads_dream_state_from_path_resolver(self):
        """WHEN dream reads/writes dream state
        THEN it uses paths.dream_state_file()."""
        source = _read_source(DREAM)
        assert "dream_state_file" in source or "dream_state" in source, (
            "dream.py must use paths.dream_state_file() for dream state tracking"
        )

    def test_writes_to_memory_dir(self):
        """WHEN dream produces memory-file updates
        THEN it writes to the directory returned by paths.memory_dir()."""
        source = _read_source(DREAM)
        assert "memory_dir" in source, (
            "dream.py must write memory updates to paths.memory_dir()"
        )

    def test_async_entry_point(self):
        """WHEN dream is inspected
        THEN it has an async entry point (for concurrent LLM calls)."""
        source = _read_source(DREAM)
        assert "async def" in source, (
            "dream.py must have async functions for concurrent LLM calls"
        )
        assert "asyncio.run" in source, (
            "dream.py must use asyncio.run() for the async entry point"
        )

    def test_concurrent_llm_calls(self):
        """WHEN dream processes multiple memory files for consolidation
        THEN it uses concurrent LLM calls (asyncio.gather or TaskGroup)."""
        source = _read_source(DREAM)
        has_concurrency = (
            "asyncio.gather" in source or
            "TaskGroup" in source or
            "create_task" in source or
            "gather(" in source
        )
        assert has_concurrency, (
            "dream.py must use concurrent LLM calls (asyncio.gather, TaskGroup, "
            "or create_task) for processing multiple memory files in parallel"
        )

    def test_dream_state_persisted_in_plugin_data(self):
        """WHEN dream updates dream state
        THEN dream state is written to paths.plugin_data() / dream_state."""
        source = _read_source(DREAM)
        has_state_write = (
            "dream_state" in source and
            ("write" in source or "dump" in source or "save" in source)
        )
        assert has_state_write, (
            "dream.py must persist dream state (last run timestamp, etc.) "
            "to the dream_state file in plugin data directory"
        )

    def test_handles_empty_learnings(self):
        """WHEN dream runs but there are no learnings
        THEN it exits early without modifying memory files."""
        source = _read_source(DREAM)
        has_empty_check = (
            "not learnings" in source or
            "is empty" in source.lower() or
            "nothing to consolidate" in source.lower() or
            "no learnings" in source.lower() or
            'learnings_file.exists()' in source or
            "if not learnings" in source
        )
        assert has_empty_check, (
            "dream.py must check for empty/missing learnings and exit early"
        )

    def test_handles_missing_learnings_file(self):
        """WHEN dream runs but the learnings file does not exist
        THEN it exits gracefully without error."""
        source = _read_source(DREAM)
        has_existence_check = (
            ".exists()" in source or
            "FileNotFoundError" in source or
            "not learnings_file" in source
        )
        assert has_existence_check, (
            "dream.py must handle missing learnings file gracefully"
        )


# ===================================================================
# Requirement: Dream — functional behavior tests
# ===================================================================

class TestAutodreamFunctional:
    """Functional tests for dream.py using mocked dependencies."""

    @pytest.fixture
    def mock_env(self, tmp_path, monkeypatch, reset_paths_cache):
        """Set up a mock plugin environment with directories."""
        plugin_data = tmp_path / "data"
        plugin_data.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(diary_dir))

        return {
            "root": tmp_path,
            "data": plugin_data,
            "memory": memory_dir,
            "diary": diary_dir,
        }

    def test_dream_reads_learnings_from_learnings_dir(self, mock_env):
        """WHEN dream runs in plugin mode
        THEN per-day learnings live under paths.learnings_dir()."""
        from lib.paths import get_paths
        paths = get_paths()

        learnings_today = paths.learnings_file("2026-01-01")
        learnings_today.parent.mkdir(parents=True, exist_ok=True)
        learnings_today.write_text(
            "## Session Learnings — 2026-01-01\n"
            "- **[trust: medium]** OBSERVATION test → Target: x.md — note\n"
        )

        # learnings_dir is per-workspace and per-day files live inside it
        assert paths.learnings_file("2026-01-01") == learnings_today
        assert learnings_today.parent == paths.learnings_dir()

    def test_dream_dream_state_in_data_dir(self, mock_env):
        """WHEN dream updates dream state
        THEN it writes to plugin data directory."""
        from lib.paths import get_paths
        paths = get_paths()
        dream_state = paths.dream_state_file()
        assert str(mock_env["data"]) in str(dream_state), (
            "Dream state file should be under plugin data directory"
        )

    def test_dream_memory_updates_go_to_memory_dir(self, mock_env):
        """WHEN dream produces memory file updates
        THEN they are written to the configured memory directory."""
        from lib.paths import get_paths
        paths = get_paths()
        assert paths.memory_dir() == mock_env["memory"], (
            "Memory dir should resolve to the configured CLAUDE_PLUGIN_OPTION_memory_dir"
        )


# ===================================================================
# Requirement: Synthesize Now Port — specific behaviors
# ===================================================================

class TestSynthesizeNowPort:
    """synthesize_now.py must be ported with path resolution and model client."""

    def test_uses_model_client(self):
        """WHEN synthesize-now calls the LLM for synthesis
        THEN it uses a ModelClient instance obtained from create_client()."""
        source = _read_source(SYNTHESIZE_NOW)
        assert "create_client" in source, (
            "synthesize_now.py must use create_client() for LLM calls"
        )

    def test_reads_diary_entries(self):
        """WHEN synthesize-now runs
        THEN it reads diary entries from paths.diary_dir()."""
        source = _read_source(SYNTHESIZE_NOW)
        assert "diary_dir" in source, (
            "synthesize_now.py must read diary entries from paths.diary_dir()"
        )

    def test_writes_per_project_now_files(self):
        """WHEN synthesize-now runs
        THEN it writes per-project status summaries to paths.now_dir()."""
        source = _read_source(SYNTHESIZE_NOW)
        assert "now_dir" in source, (
            "synthesize_now.py must write per-project summaries to paths.now_dir()"
        )

    def test_groups_by_project(self):
        """WHEN synthesize-now processes diary entries
        THEN it groups them by project (derived from working-directory path)."""
        source = _read_source(SYNTHESIZE_NOW)
        has_grouping = (
            "_derive_project_name" in source or
            "project_name" in source or
            "entries_by_project" in source or
            "Path(working_dir).name" in source
        )
        assert has_grouping, (
            "synthesize_now.py must group diary entries by project name"
        )

    def test_writes_output_to_path_resolved_location(self):
        """WHEN synthesize-now produces a synthesis artifact
        THEN the artifact is written to a path resolved via paths.*."""
        source = _read_source(SYNTHESIZE_NOW)
        has_write = (
            "write" in source or
            "open(" in source or
            "save" in source.lower()
        )
        assert has_write, (
            "synthesize_now.py must write synthesis output to a path-resolved location"
        )

    def test_async_entry_point(self):
        """WHEN synthesize-now is inspected
        THEN it has an async entry point."""
        source = _read_source(SYNTHESIZE_NOW)
        assert "async def" in source, (
            "synthesize_now.py must have async function(s) for LLM calls"
        )
        assert "asyncio.run" in source, (
            "synthesize_now.py must use asyncio.run() for the async entry point"
        )

    def test_no_hardcoded_output_dir(self):
        """WHEN synthesize-now writes output
        THEN it does not use hardcoded paths."""
        source = _read_source(SYNTHESIZE_NOW)
        forbidden_patterns = [
            "~/.multiplai/",
            "/home/",
            "/Users/",
            "CLAUDE_MULTIPLAI_HOME",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, (
                f"synthesize_now.py contains hardcoded path '{pattern}' — "
                "must use paths.* for all output locations"
            )


# ===================================================================
# Requirement: PreCompact Port — specific behaviors
# ===================================================================

class TestPreCompactPort:
    """pre_compact.py must handle PreCompact hook event correctly."""

    def test_uses_path_resolver(self):
        """WHEN pre_compact runs
        THEN it uses the path resolver for all file access."""
        source = _read_source(PRE_COMPACT)
        assert "get_paths" in source or "from lib.paths" in source, (
            "pre_compact.py must use path resolver for file access"
        )

    def test_has_main_entry(self):
        """WHEN pre_compact.py is inspected
        THEN it has a __main__ guard and main() function."""
        source = _read_source(PRE_COMPACT)
        assert 'if __name__ == "__main__"' in source, (
            "pre_compact.py must have a __main__ guard"
        )
        assert "def main" in source, (
            "pre_compact.py must have a main() function"
        )

    def test_reads_context_for_preservation(self):
        """WHEN the PreCompact hook fires
        THEN the script reads current context (learnings, session state)
        to preserve important information before compaction."""
        source = _read_source(PRE_COMPACT)
        preserves_context = (
            "learnings" in source.lower() or
            "memory" in source.lower() or
            "diary" in source.lower() or
            "context" in source.lower() or
            "session" in source.lower() or
            "synthesize" in source.lower() or
            "extract" in source.lower()
        )
        assert preserves_context, (
            "pre_compact.py must preserve context (learnings, session state) "
            "before compaction — it should reference context/learnings/memory concepts"
        )

    def test_writes_preservation_to_path_resolved_location(self):
        """WHEN pre_compact preserves context
        THEN it writes to a path resolved via paths.*."""
        source = _read_source(PRE_COMPACT)
        has_output = (
            "write" in source or
            "dump" in source or
            "save" in source.lower() or
            "append" in source.lower() or
            "learnings_file" in source or
            "diary_dir" in source or
            "memory_dir" in source
        )
        assert has_output, (
            "pre_compact.py must write preserved context to a path-resolved location"
        )

    def test_uses_model_client_if_llm_needed(self):
        """WHEN pre_compact needs LLM calls (e.g., to summarize context)
        THEN it uses create_client() from the model client module."""
        source = _read_source(PRE_COMPACT)
        # PreCompact may or may not need LLM; if it imports model_client, it should use create_client
        if "model_client" in source or "create_client" in source:
            assert "create_client" in source, (
                "pre_compact.py must use create_client() if it makes LLM calls"
            )
        else:
            # If no LLM calls, it should at least trigger extract_learnings or similar
            has_trigger = (
                "extract" in source.lower() or
                "subprocess" in source or
                "learnings" in source.lower()
            )
            assert has_trigger, (
                "pre_compact.py must either use LLM (via create_client) or "
                "trigger learning extraction before compaction"
            )


# ===================================================================
# Requirement: LLM calls go through ModelClient interface
# ===================================================================

class TestModelClientUsage:
    """Scripts that make LLM calls must use the ModelClient interface."""

    @pytest.mark.parametrize("script_path,script_name", [
        (EXTRACT_LEARNINGS, "extract_learnings.py"),
        (DREAM, "dream.py"),
        (SYNTHESIZE_NOW, "synthesize_now.py"),
    ])
    def test_llm_scripts_use_create_client(self, script_path, script_name):
        """WHEN any LLM-calling script is inspected
        THEN it imports create_client from lib.model_client."""
        source = _read_source(script_path)
        has_model_import = (
            "from lib.model_client import" in source or
            "from lib.model_client import create_client" in source
        )
        assert has_model_import, (
            f"{script_name} must import create_client from lib.model_client"
        )

    @pytest.mark.parametrize("script_path,script_name", [
        (EXTRACT_LEARNINGS, "extract_learnings.py"),
        (DREAM, "dream.py"),
        (SYNTHESIZE_NOW, "synthesize_now.py"),
    ])
    def test_llm_scripts_await_create_client(self, script_path, script_name):
        """WHEN the script creates a client
        THEN it awaits create_client() inside an async function."""
        source = _read_source(script_path)
        assert "await create_client()" in source, (
            f"{script_name} must 'await create_client()' inside an async function"
        )

    @pytest.mark.parametrize("script_path,script_name", [
        (EXTRACT_LEARNINGS, "extract_learnings.py"),
        (DREAM, "dream.py"),
        (SYNTHESIZE_NOW, "synthesize_now.py"),
    ])
    def test_llm_scripts_call_query(self, script_path, script_name):
        """WHEN the script uses the model client
        THEN it or its shared lib calls client.query() to make LLM requests."""
        source = _read_source(script_path)
        # extract_learnings delegates to lib/extraction.py; check both
        lib_extraction = SCRIPTS_DIR / "lib" / "extraction.py"
        combined = source + (lib_extraction.read_text() if lib_extraction.exists() else "")
        has_query_call = (
            ".query(" in combined or
            "client.query" in combined or
            "await client.query" in combined
        )
        assert has_query_call, (
            f"{script_name} must call client.query() for LLM requests"
        )


# ===================================================================
# Requirement: Memory file writes go to paths.memory_dir
# ===================================================================

class TestMemoryFileWriteLocations:
    """Verify that memory file writes go to paths.memory_dir
    and diary writes go to paths.diary_dir."""

    def test_extract_learnings_writes_to_memory_dir(self):
        """WHEN extract_learnings writes learnings
        THEN it uses paths.learnings_file() (under memory_dir)."""
        source = _read_source(EXTRACT_LEARNINGS)
        assert "learnings_file" in source, (
            "extract_learnings.py must write to paths.learnings_file() (under memory_dir)"
        )

    def test_dream_reads_from_memory_dir(self):
        """WHEN dream reads current memory files
        THEN it uses paths.memory_dir()."""
        source = _read_source(DREAM)
        assert "memory_dir" in source, (
            "dream.py must read/write memory files via paths.memory_dir()"
        )

    def test_dream_updates_memory_files(self):
        """WHEN dream produces memory-file updates (e.g., updating me.md)
        THEN it writes those updates to the directory returned by paths.memory_dir()."""
        source = _read_source(DREAM)
        # Must have logic to write updated memory files
        has_memory_write = (
            "memory_dir" in source and
            ("write" in source or "open" in source)
        )
        assert has_memory_write, (
            "dream.py must write updated memory files to paths.memory_dir()"
        )

    def test_synthesize_reads_diary_dir(self):
        """WHEN synthesize_now reads diary entries
        THEN it uses paths.diary_dir()."""
        source = _read_source(SYNTHESIZE_NOW)
        assert "diary_dir" in source, (
            "synthesize_now.py must read diary entries from paths.diary_dir()"
        )


# ===================================================================
# Requirement: No bash wrapper scripts
# ===================================================================

class TestNoBashWrappers:
    """All ported scripts must be pure Python.
    No bash wrapper scripts from the source repo carried over."""

    def test_no_shell_scripts_in_scripts_dir(self):
        """WHEN the plugin's scripts/ directory is listed
        THEN it contains no .sh/.bash/.zsh/.fish shell wrappers.

        Non-code data files (e.g., catalog YAML/JSON) are allowed
        — the point is to ban shell wrappers, not all non-Python files.
        """
        forbidden_suffixes = {".sh", ".bash", ".zsh", ".fish"}
        for f in SCRIPTS_DIR.iterdir():
            if f.is_file():
                assert f.suffix not in forbidden_suffixes, (
                    f"Found shell wrapper {f.name} in scripts/ — "
                    f"ban list: {sorted(forbidden_suffixes)}"
                )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_no_subprocess_calls_to_bash(self, script_path):
        """WHEN ported scripts are searched for subprocess calls
        THEN none invoke .sh or .bash scripts."""
        source = _read_source(script_path)
        if "subprocess" in source:
            # Check for shell script invocations
            assert ".sh" not in source or "ssh" in source.lower(), (
                f"{script_path.name} must not call .sh bash wrapper scripts"
            )


# ===================================================================
# Requirement: Extract learnings — query with system prompt
# ===================================================================

class TestExtractLearningsQueryPattern:
    """Extract learnings must send a system prompt and messages to the LLM
    for learning identification."""

    def test_has_system_prompt(self):
        """WHEN extract_learnings calls the LLM
        THEN it provides a system prompt for learning extraction."""
        source = _read_source(EXTRACT_LEARNINGS)
        # May delegate to lib/extraction.py
        lib_extraction = SCRIPTS_DIR / "lib" / "extraction.py"
        combined = source + (lib_extraction.read_text() if lib_extraction.exists() else "")
        has_system = (
            "system" in combined.lower() and
            ("prompt" in combined.lower() or "='" in combined or '="' in combined)
        )
        assert has_system, (
            "extract_learnings.py must provide a system prompt for the LLM call "
            "to guide learning extraction from session transcript"
        )

    def test_builds_messages_from_transcript(self):
        """WHEN extract_learnings prepares the LLM call
        THEN it builds messages from the session transcript."""
        source = _read_source(EXTRACT_LEARNINGS)
        has_messages = (
            "messages" in source or
            "transcript" in source.lower() or
            "conversation" in source.lower()
        )
        assert has_messages, (
            "extract_learnings.py must build messages from session transcript "
            "for the LLM learning extraction call"
        )


# ===================================================================
# Requirement: Dream concurrent processing
# ===================================================================

class TestAutodreamConcurrency:
    """Dream must use concurrent LLM calls for processing multiple
    memory files in parallel."""

    def test_processes_multiple_memory_files(self):
        """WHEN dream consolidates learnings
        THEN it can process updates for multiple memory files."""
        source = _read_source(DREAM)
        has_multi_file = (
            "for " in source and "memory" in source.lower() or
            "files" in source.lower() or
            "glob" in source or
            "iterdir" in source
        )
        assert has_multi_file, (
            "dream.py must handle processing updates for multiple memory files"
        )

    def test_uses_asyncio_for_concurrency(self):
        """WHEN dream makes multiple LLM calls
        THEN it uses asyncio concurrency primitives."""
        source = _read_source(DREAM)
        assert "asyncio" in source, (
            "dream.py must use asyncio for concurrent LLM call processing"
        )


# ===================================================================
# Requirement: Dream dream state management
# ===================================================================

class TestAutodreamDreamState:
    """Dream must read/write dream state to track consolidation runs."""

    def test_reads_dream_state(self):
        """WHEN dream starts
        THEN it reads existing dream state to check last run time."""
        source = _read_source(DREAM)
        has_state_read = (
            "dream_state" in source and
            ("read" in source or "load" in source or "open" in source or
             "exists" in source)
        )
        assert has_state_read, (
            "dream.py must read dream state to check last run timing"
        )

    def test_updates_dream_state_after_run(self):
        """WHEN dream completes successfully
        THEN it updates the dream state with the current timestamp."""
        source = _read_source(DREAM)
        has_state_update = (
            "dream_state" in source and
            ("write" in source or "dump" in source or "save" in source or
             "timestamp" in source.lower())
        )
        assert has_state_update, (
            "dream.py must update dream state after successful consolidation"
        )

    def test_uses_yaml_for_state(self):
        """WHEN dream persists dream state
        THEN it uses YAML format (as specified by dream_state_file path .yaml)."""
        source = _read_source(DREAM)
        has_yaml = (
            "yaml" in source.lower() or
            "pyyaml" in source.lower()
        )
        assert has_yaml, (
            "dream.py must use YAML for dream state persistence "
            "(dream_state_file is .yaml)"
        )


# ===================================================================
# Requirement: Synthesize Now — reads all input sources
# ===================================================================

class TestSynthesizeNowInputs:
    """Synthesize-now must read from diary, learnings, and memory files."""

    def test_reads_diary_and_writes_now_dir(self):
        """WHEN synthesize-now runs
        THEN it reads diary entries and writes per-project summaries to now_dir.

        synthesize_now's purpose is per-project status synthesis, not
        memory consolidation — learnings and memory files are handled
        by dream.py.
        """
        source = _read_source(SYNTHESIZE_NOW)
        has_diary = "diary" in source.lower()
        has_now = "now_dir" in source

        assert has_diary, "synthesize_now.py must read diary entries"
        assert has_now, "synthesize_now.py must write to paths.now_dir()"

    def test_handles_missing_diary_dir(self):
        """WHEN synthesize-now runs but diary directory does not exist
        THEN it handles the missing directory gracefully."""
        source = _read_source(SYNTHESIZE_NOW)
        has_existence_check = (
            ".exists()" in source or
            "FileNotFoundError" in source or
            "OSError" in source or
            "if not diary" in source.lower() or
            "mkdir" in source
        )
        assert has_existence_check, (
            "synthesize_now.py must handle missing diary directory gracefully"
        )


# ===================================================================
# Requirement: PreCompact triggers context preservation
# ===================================================================

class TestPreCompactBehavior:
    """PreCompact hook must preserve context before compaction."""

    def test_fires_without_error(self):
        """WHEN pre_compact.py is loaded
        THEN it has a valid main() that can be called."""
        source = _read_source(PRE_COMPACT)
        assert "def main" in source, (
            "pre_compact.py must have a main() function"
        )

    def test_extracts_or_preserves_learnings(self):
        """WHEN PreCompact fires
        THEN the script extracts learnings or preserves context so they
        survive the compaction."""
        source = _read_source(PRE_COMPACT)
        has_preservation = (
            "extract" in source.lower() or
            "learnings" in source.lower() or
            "preserve" in source.lower() or
            "save" in source.lower() or
            "write" in source.lower() or
            "synthesize" in source.lower() or
            "flush" in source.lower()
        )
        assert has_preservation, (
            "pre_compact.py must extract/preserve learnings before compaction. "
            "Currently only logs a message without preserving any context."
        )

    def test_does_not_block_compaction(self):
        """WHEN pre_compact runs
        THEN it should not import heavy modules that would slow compaction."""
        source = _read_source(PRE_COMPACT)
        # PreCompact should be fast — should not do heavy LLM calls inline
        # unless it's truly necessary for context preservation
        if "asyncio.run" not in source:
            # If it's synchronous, it should be lightweight
            assert "time.sleep" not in source, (
                "pre_compact.py must not block with sleep calls"
            )


# ===================================================================
# Integration-style tests: verify the scripts work together
# ===================================================================

class TestScriptIntegrationPatterns:
    """Verify the ported scripts follow consistent patterns."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_consistent_logging_setup(self, script_path):
        """WHEN any of the four scripts is inspected
        THEN it uses setup_logging() from lib.log_utils."""
        source = _read_source(script_path)
        assert "setup_logging" in source, (
            f"{script_path.name} must use setup_logging() from lib.log_utils "
            "for consistent logging"
        )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=ALL_SCRIPT_NAMES)
    def test_has_main_guard(self, script_path):
        """WHEN each script is read
        THEN it has an if __name__ == '__main__' guard."""
        source = _read_source(script_path)
        assert '__name__' in source and '__main__' in source, (
            f"{script_path.name} must have an if __name__ == '__main__' guard"
        )

    @pytest.mark.parametrize("script_path,script_name", [
        (EXTRACT_LEARNINGS, "extract_learnings.py"),
        (DREAM, "dream.py"),
        (SYNTHESIZE_NOW, "synthesize_now.py"),
    ], ids=["extract_learnings", "dream", "synthesize_now"])
    def test_llm_scripts_have_error_handling(self, script_path, script_name):
        """WHEN an LLM-calling script is inspected
        THEN it has error handling for LLM call failures."""
        source = _read_source(script_path)
        has_error_handling = (
            "try:" in source or
            "except" in source or
            "raise" in source
        )
        assert has_error_handling, (
            f"{script_name} must have error handling for LLM call failures "
            "to avoid leaving files in a partially-written state"
        )

    def test_extract_learnings_is_stop_hook(self):
        """The Stop hook is session_stop.py (it gates the deferred
        extract_learnings path via session_end → session_start)."""
        from conftest import parse_hooks
        stop_scripts = [h["script"] for h in parse_hooks() if h["event"] == "Stop"]
        has_extract = any(
            "extract_learnings" in s or "session_stop" in s
            for s in stop_scripts
        )
        assert has_extract, (
            "hooks/hooks.json must register session_stop.py (or "
            "extract_learnings.py) as the Stop hook"
        )

    def test_pre_compact_is_precompact_hook(self):
        """pre_compact.py must be registered as the PreCompact hook."""
        from conftest import parse_hooks
        precompact_scripts = [
            h["script"] for h in parse_hooks() if h["event"] == "PreCompact"
        ]
        has_precompact = any("pre_compact" in s for s in precompact_scripts)
        assert has_precompact, (
            "hooks/hooks.json must register pre_compact.py as a PreCompact hook"
        )
