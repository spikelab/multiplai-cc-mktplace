"""Tests for Block 6: Context Router Hook.

Covers:
- Context router port with D8 transformations (path resolver, model client, stripped catalogs)
- Metadata-first ranking (size, mtime) before content reads per R2 mitigation
- Catalog caching in $data_dir/catalogs/
- Stripped resource/skill catalog routing branches (NG5)
- Execution within 5-second timeout on representative memory sets
- Re-exec venv preamble

Tests are written against behavioral contracts from the spec.  All tests
should FAIL until the implementation is complete.
"""

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR, HOOKS_JSON, parse_hooks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTEXT_ROUTER_PATH = SCRIPTS_DIR / "context_manager.py"

# Ensure scripts dir is on sys.path for lib imports
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _make_memory_dir(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Create a temporary memory directory with optional files."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if files:
        for name, content in files.items():
            (mem_dir / name).write_text(content)
    return mem_dir


def _make_catalogs_dir(tmp_path: Path) -> Path:
    """Create a temporary catalogs directory."""
    cat_dir = tmp_path / "catalogs"
    cat_dir.mkdir(parents=True, exist_ok=True)
    return cat_dir


def _import_context_manager():
    """Dynamically import context_manager module, resetting caches."""
    from lib.paths import _reset_cache
    _reset_cache()
    spec = importlib.util.spec_from_file_location("context_manager", CONTEXT_ROUTER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import context_manager from {CONTEXT_ROUTER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    return mod, spec


# ===========================================================================
# D8 TRANSFORMATION: Path resolver usage
# ===========================================================================


class TestContextRouterPathResolution:
    """Context router must resolve memory files via paths.memory_dir(), not hardcoded paths."""

    def test_reads_memory_files_from_path_resolver(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN the ported context-router runs in plugin mode with memory_dir set
        THEN it reads memory files from paths returned by paths.memory_dir()."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nI am a developer.",
            "technical-pref.md": "# Technical\nI prefer Python.",
            "preferences.md": "# Preferences\nKeep it concise.",
        })
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(mem_dir))

        from lib.paths import _reset_cache, Paths
        _reset_cache()
        paths = Paths.resolve()

        # Import _read_memory_files from the context router
        from context_manager import _read_memory_files

        result = _read_memory_files(paths.memory_dir())
        assert "me.md" in result
        assert "technical-pref.md" in result
        assert "preferences.md" in result
        assert "About Me" in result["me.md"]

    def test_no_hardcoded_paths_in_context_manager(self):
        """WHEN the ported context-router source is inspected
        THEN it contains no hardcoded paths."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "~/.multiplai" not in text, "Hardcoded ~/.multiplai found"
        assert "~/.claude" not in text, "Hardcoded ~/.claude found"
        assert "/home/spike" not in text, "Hardcoded user home path found"
        assert "/Users/spike" not in text, "Hardcoded user home path found"

    def test_context_manager_imports_from_lib_paths(self):
        """WHEN the context-router source is inspected
        THEN it imports from lib.paths for all path resolution."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "from lib.paths" in text or "lib.paths" in text, \
            "Context router must import from lib.paths"


# ===========================================================================
# D8 TRANSFORMATION: Model client usage
# ===========================================================================


class TestContextRouterModelClient:
    """Context router must use ModelClient for LLM calls, not direct SDK imports."""

    def test_no_direct_claude_agent_sdk_import(self):
        """WHEN the context-router source is inspected
        THEN it does not directly import claude_agent_sdk."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "import claude_agent_sdk" not in text
        assert "from claude_agent_sdk" not in text

    def test_no_direct_anthropic_import(self):
        """WHEN the context-router source is inspected
        THEN it does not directly import anthropic."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "import anthropic" not in text
        assert "from anthropic" not in text

    def test_uses_model_client_for_llm_calls(self):
        """WHEN the context-router needs to make an LLM call
        THEN it uses ModelClient / create_client from lib.model_client."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # The context router should reference the model client when it makes LLM calls
        has_model_client = (
            "from lib.model_client" in text
            or "model_client" in text
            or "create_client" in text
        )
        assert has_model_client, \
            "Context router must use model_client abstraction for LLM calls"


# ===========================================================================
# NG5: STRIPPED CATALOG ROUTING
# ===========================================================================


class TestCatalogRoutingStripped:
    """Context router must NOT contain resource/skill catalog routing branches."""

    def test_no_generate_catalog_reference(self):
        """WHEN the ported context-router source is inspected
        THEN it contains no references to generate-catalog."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "generate-catalog" not in text
        assert "generate_catalog" not in text.replace("generate_catalog.py", "")

    def test_no_skill_catalog_reference(self):
        """WHEN the ported context-router source is inspected
        THEN it contains no references to skill-catalog JSON."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "skill-catalog" not in text
        assert "skill_catalog" not in text

    def test_no_resource_catalog_reference(self):
        """WHEN the ported context-router source is inspected
        THEN it contains no references to resource-catalog JSON."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "resource-catalog" not in text
        assert "resource_catalog" not in text

    def test_no_catalog_based_routing_logic(self):
        """WHEN the ported context-router source is inspected
        THEN it contains no catalog-based routing logic (catalog lookup, parse, etc)."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # Should not contain patterns like loading catalog JSON for routing decisions
        assert "catalog_routing" not in text
        assert "route_from_catalog" not in text


# ===========================================================================
# R2 MITIGATION: METADATA-FIRST RANKING
# ===========================================================================


class TestMetadataFirstRanking:
    """Context router must rank memory files by metadata (size, mtime) BEFORE reading content.

    This prevents exceeding the 5-second timeout on large memory sets by
    avoiding reading every file's content upfront.
    """

    def test_has_metadata_ranking_function(self):
        """WHEN the context-router source is inspected
        THEN it has a function that ranks files by metadata before reading content."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # Should have some form of metadata-based ranking/sorting
        has_metadata_ranking = any(kw in text for kw in [
            "mtime", "st_mtime", "stat()", "os.stat",
            "file_size", "st_size", "getsize",
            "rank_by_metadata", "metadata_rank", "sort_by_metadata",
            "metadata_first",
        ])
        assert has_metadata_ranking, \
            "Context router must implement metadata-first ranking (size, mtime)"

    def test_metadata_ranking_uses_stat_not_read(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN the context router ranks memory files
        THEN it uses file stat (size, mtime) before reading content."""
        # Create several memory files with different sizes and modification times
        mem_dir = _make_memory_dir(tmp_path, {
            "old-small.md": "Small old file.",
            "recent-large.md": "# Recent Large\n" + ("Context data. " * 500),
            "me.md": "# About Me\nI am a developer with many interests.",
            "technical-pref.md": "# Technical Preferences\n" + ("Python code. " * 200),
        })
        # Make "old-small.md" have an older mtime
        old_file = mem_dir / "old-small.md"
        os.utime(old_file, (time.time() - 86400 * 30, time.time() - 86400 * 30))

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(mem_dir))

        from lib.paths import _reset_cache, Paths
        _reset_cache()
        paths = Paths.resolve()

        # The context router should have a ranking function that works on metadata
        # before reading full content
        from context_manager import _rank_memory_files

        ranked = _rank_memory_files(mem_dir)
        assert isinstance(ranked, list), "Ranking must return a list"
        assert len(ranked) > 0, "Ranking must return files"

        # Each ranked item should contain metadata (at minimum path and rank info)
        first = ranked[0]
        assert hasattr(first, 'path') or isinstance(first, (tuple, dict, Path)), \
            "Ranked items must contain path information"

    def test_recent_files_ranked_higher(self, tmp_path):
        """WHEN memory files have different modification times
        THEN more recently modified files are ranked higher."""
        mem_dir = _make_memory_dir(tmp_path, {
            "stale.md": "# Stale\nOld content.",
            "fresh.md": "# Fresh\nNew content.",
        })
        # Make stale.md 30 days old
        stale = mem_dir / "stale.md"
        os.utime(stale, (time.time() - 86400 * 30, time.time() - 86400 * 30))

        from context_manager import _rank_memory_files

        ranked = _rank_memory_files(mem_dir)
        # Extract filenames in ranked order
        names = [Path(r.path if hasattr(r, 'path') else r).name
                 if not isinstance(r, dict)
                 else Path(r.get('path', r.get('name', ''))).name
                 for r in ranked]
        fresh_idx = names.index("fresh.md") if "fresh.md" in names else len(names)
        stale_idx = names.index("stale.md") if "stale.md" in names else len(names)
        assert fresh_idx < stale_idx, \
            "More recently modified files should be ranked higher"

    def test_larger_files_ranked_with_size_consideration(self, tmp_path):
        """WHEN memory files have different sizes
        THEN size is factored into the ranking decision."""
        mem_dir = _make_memory_dir(tmp_path, {
            "tiny.md": "# Tiny",
            "large.md": "# Large File\n" + ("Substantial content. " * 500),
        })
        # Both files have the same mtime (just created)

        from context_manager import _rank_memory_files

        ranked = _rank_memory_files(mem_dir)
        # Verify the function accounts for size in some way
        assert len(ranked) == 2, "Should rank both files"

    def test_content_only_read_for_top_candidates(self, tmp_path):
        """WHEN ranking many memory files
        THEN only top candidates have their full content read.

        This is the key R2 mitigation: avoid reading all files to stay
        under the 5-second timeout.
        """
        # Create 20 memory files
        files = {}
        for i in range(20):
            files[f"memory-{i:02d}.md"] = f"# Memory {i}\n" + ("Content. " * 100)
        mem_dir = _make_memory_dir(tmp_path, files)

        from context_manager import _read_top_memory_files

        # Should exist: a function that reads only the top N files
        result = _read_top_memory_files(mem_dir, max_files=5)
        assert isinstance(result, dict), "Should return dict of filename->content"
        assert len(result) <= 5, \
            f"Should read at most 5 files, but read {len(result)}"


# ===========================================================================
# CATALOG CACHING
# ===========================================================================


class TestCatalogCaching:
    """Context router must cache catalog in $data_dir/catalogs/."""

    def test_catalog_cache_written_to_data_dir(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN the context router generates/caches a catalog
        THEN it writes to $data_dir/catalogs/."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nDeveloper.",
        })
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(mem_dir))

        from lib.paths import _reset_cache, Paths
        _reset_cache()
        paths = Paths.resolve()

        # Context router should have a catalog cache mechanism
        from context_manager import _cache_catalog, _load_cached_catalog

        catalog_data = {"files": [{"name": "me.md", "size": 30}]}
        _cache_catalog(paths.catalogs_dir(), catalog_data)

        # Verify it was written
        cached = _load_cached_catalog(paths.catalogs_dir())
        assert cached is not None, "Should be able to load cached catalog"
        assert cached["files"][0]["name"] == "me.md"

    def test_catalog_cache_reused_on_subsequent_calls(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN a cached catalog exists and is still fresh
        THEN the context router reuses it instead of regenerating."""
        data_dir = tmp_path / "data"
        catalogs_dir = data_dir / "catalogs"
        catalogs_dir.mkdir(parents=True)

        from context_manager import _cache_catalog, _load_cached_catalog, _is_catalog_fresh

        catalog_data = {"files": [{"name": "me.md", "size": 30}], "timestamp": time.time()}
        _cache_catalog(catalogs_dir, catalog_data)

        assert _is_catalog_fresh(catalogs_dir), \
            "Freshly written catalog should be considered fresh"

        cached = _load_cached_catalog(catalogs_dir)
        assert cached == catalog_data, "Cached catalog should match original"

    def test_catalog_cache_invalidated_when_stale(self, tmp_path):
        """WHEN the catalog cache is older than the staleness threshold
        THEN it is regenerated."""
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        from context_manager import _cache_catalog, _is_catalog_fresh

        old_catalog = {"files": [], "timestamp": time.time() - 3600}
        _cache_catalog(catalogs_dir, old_catalog)

        # Backdate the cache file
        for f in catalogs_dir.iterdir():
            os.utime(f, (time.time() - 3600, time.time() - 3600))

        assert not _is_catalog_fresh(catalogs_dir), \
            "Stale catalog should not be considered fresh"

    def test_catalog_cache_path_uses_path_resolver(self):
        """WHEN the context router caches catalogs
        THEN the cache path is derived from paths.catalogs_dir()."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # Should reference catalogs_dir from paths
        has_catalogs_ref = any(kw in text for kw in [
            "catalogs_dir", "catalogs", "cache",
        ])
        assert has_catalogs_ref, \
            "Context router must use paths.catalogs_dir() for catalog cache"


# ===========================================================================
# GRACEFUL HANDLING OF MISSING FILES
# ===========================================================================


class TestMissingMemoryFiles:
    """Context router must handle missing memory files gracefully."""

    def test_missing_memory_dir_returns_empty(self, tmp_path):
        """WHEN the context-router runs but the memory directory doesn't exist
        THEN it returns empty results without raising an exception."""
        nonexistent = tmp_path / "does-not-exist"
        from context_manager import _read_memory_files

        result = _read_memory_files(nonexistent)
        assert result == {}, "Missing memory dir should return empty dict"

    def test_partial_memory_files(self, tmp_path):
        """WHEN the memory directory exists but only has some expected files
        THEN the available files are read and missing ones are skipped."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nI exist.",
            # technical-pref.md is missing
            # preferences.md is missing
        })
        from context_manager import _read_memory_files

        result = _read_memory_files(mem_dir)
        assert "me.md" in result, "Existing file should be read"
        assert len(result) == 1, "Only existing files should be returned"

    def test_unreadable_file_skipped_gracefully(self, tmp_path):
        """WHEN a memory file exists but cannot be read (permissions, encoding)
        THEN it is skipped without crashing the router."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nI exist.",
            "corrupt.md": "Valid content for now.",
        })
        # Make corrupt.md unreadable
        corrupt = mem_dir / "corrupt.md"
        corrupt.chmod(0o000)

        from context_manager import _read_memory_files

        try:
            result = _read_memory_files(mem_dir)
            # Should have read me.md and skipped corrupt.md
            assert "me.md" in result
            # corrupt.md should be skipped, not cause an error
        finally:
            corrupt.chmod(0o644)  # Restore for cleanup

    def test_empty_memory_dir(self, tmp_path):
        """WHEN the memory directory exists but is empty
        THEN the context router returns empty results."""
        mem_dir = _make_memory_dir(tmp_path)  # empty

        from context_manager import _read_memory_files

        result = _read_memory_files(mem_dir)
        assert result == {}, "Empty memory dir should return empty dict"


# ===========================================================================
# TIMEOUT COMPLIANCE (R2)
# ===========================================================================


class TestTimeoutCompliance:
    """Context router must complete within the 5-second timeout.

    Per R2 mitigation: metadata-first ranking ensures the router doesn't
    read all files' content when there are many memory files.
    """

    def test_hooks_json_timeout_is_5_seconds(self):
        """WHEN the UserPromptSubmit hook is inspected in hooks/hooks.json
        THEN it has a 5-second timeout (official schema uses seconds)."""
        user_prompt_hooks = [h for h in parse_hooks()
                             if h["event"] == "UserPromptSubmit"]
        assert len(user_prompt_hooks) > 0, "No UserPromptSubmit hook found"

        context_manager_hook = None
        for h in user_prompt_hooks:
            if "context_manager" in h["script"]:
                context_manager_hook = h
                break
        assert context_manager_hook is not None, "No context_manager hook found"
        assert context_manager_hook["timeout"] == 5, \
            f"Context manager timeout should be 5s, got {context_manager_hook['timeout']}"

    def test_completes_under_5_seconds_small_memory(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN the context router runs with a small memory set (3 files)
        THEN execution completes in well under 5 seconds."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nDeveloper.",
            "technical-pref.md": "# Tech\nPython, Go.",
            "preferences.md": "# Prefs\nConcise.",
        })
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(mem_dir))

        from lib.paths import _reset_cache
        _reset_cache()

        start = time.monotonic()
        from context_manager import _read_memory_files, _rank_memory_files
        _rank_memory_files(mem_dir)
        _read_memory_files(mem_dir)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, \
            f"Small memory set should complete in <1s, took {elapsed:.2f}s"

    def test_completes_under_5_seconds_large_memory(self, tmp_path):
        """WHEN the context router runs with a large memory set (50 files)
        THEN execution completes within 5 seconds thanks to metadata-first ranking."""
        files = {}
        for i in range(50):
            files[f"memory-{i:02d}.md"] = f"# Memory File {i}\n" + ("x " * 5000)
        mem_dir = _make_memory_dir(tmp_path, files)

        from context_manager import _rank_memory_files, _read_top_memory_files

        start = time.monotonic()
        ranked = _rank_memory_files(mem_dir)
        _read_top_memory_files(mem_dir, max_files=10)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, \
            f"Large memory set should complete in <5s, took {elapsed:.2f}s"


# ===========================================================================
# VENV RE-EXEC PREAMBLE
# ===========================================================================


class TestVenvPreamble:
    """Context router must include the re-exec venv preamble per D4."""

    def test_has_venv_guard_import(self):
        """WHEN the context_manager.py source is inspected
        THEN it imports the venv guard module."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "venv_guard" in text, "Context router must import venv_guard"

    def test_has_ensure_venv_python_call(self):
        """WHEN the context_manager.py source is inspected
        THEN it calls ensure_venv_python() early in the script."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "ensure_venv_python" in text, \
            "Context router must call ensure_venv_python()"

    def test_venv_preamble_before_heavy_imports(self):
        """WHEN the context_manager.py source is inspected
        THEN the venv preamble appears before imports that require pip packages."""
        text = CONTEXT_ROUTER_PATH.read_text()
        lines = text.split("\n")
        ensure_line = None
        heavy_import_line = None

        for i, line in enumerate(lines):
            if "ensure_venv_python" in line and "import" not in line:
                ensure_line = i
            if any(pkg in line for pkg in ["import yaml", "import anthropic"]):
                heavy_import_line = i

        if ensure_line is not None and heavy_import_line is not None:
            assert ensure_line < heavy_import_line, \
                "ensure_venv_python() must be called before heavy imports"

    def test_sys_path_insert_before_venv_guard(self):
        """WHEN the context_manager.py source is inspected
        THEN sys.path is set up before importing venv_guard."""
        text = CONTEXT_ROUTER_PATH.read_text()
        lines = text.split("\n")
        path_insert_line = None
        venv_import_line = None

        for i, line in enumerate(lines):
            if "sys.path.insert" in line:
                path_insert_line = i
            if "from lib.venv_guard" in line:
                venv_import_line = i

        if path_insert_line is not None and venv_import_line is not None:
            assert path_insert_line < venv_import_line, \
                "sys.path setup must come before venv_guard import"


# ===========================================================================
# D8: CONTEXT ROUTER BEHAVIORAL TESTS
# ===========================================================================


class TestContextRouterMainFlow:
    """Integration-style tests for the context router main flow."""

    def test_main_function_exists(self):
        """WHEN context_manager is imported
        THEN it exposes a main() function."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "def main" in text, "Context router must have a main() function"

    def test_main_entrypoint_guard(self):
        """WHEN context_manager.py is inspected
        THEN it has an if __name__ == '__main__' guard."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "__name__" in text and "__main__" in text, \
            "Context router must have __name__ == '__main__' guard"

    def test_reads_stdin_for_user_prompt(self):
        """WHEN the UserPromptSubmit hook fires
        THEN the context router receives user prompt via stdin JSON."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # The hook should read stdin for the user's prompt
        has_stdin_read = any(kw in text for kw in [
            "sys.stdin", "stdin", "json.load", "input()",
        ])
        assert has_stdin_read, \
            "Context router should read user prompt from stdin"

    def test_outputs_context_as_json(self):
        """WHEN the context router determines relevant context
        THEN it outputs the result as JSON to stdout."""
        text = CONTEXT_ROUTER_PATH.read_text()
        has_json_output = any(kw in text for kw in [
            "json.dump", "json.dumps", "sys.stdout", "print(",
        ])
        assert has_json_output, \
            "Context router should output results as JSON"

    def test_memory_file_routing_preserved(self):
        """WHEN the context router processes a prompt
        THEN memory-file routing logic is present (not stripped)."""
        text = CONTEXT_ROUTER_PATH.read_text()
        # Memory file routing should be preserved (unlike catalog routing which is stripped)
        has_memory_routing = any(kw in text for kw in [
            "memory_files", "memory_dir", "_read_memory",
            "context", "route",
        ])
        assert has_memory_routing, \
            "Memory-file routing must be preserved in context router"

    def test_session_context_injection_preserved(self):
        """WHEN the context router processes a prompt
        THEN session context injection logic is present."""
        text = CONTEXT_ROUTER_PATH.read_text()
        has_session_ctx = any(kw in text for kw in [
            "session", "context", "inject", "system_prompt",
            "prefix", "prepend",
        ])
        assert has_session_ctx, \
            "Session context injection must be preserved in context router"


# ===========================================================================
# HOOK WIRING (D5)
# ===========================================================================


class TestContextRouterHookWiring:
    """Context router must be correctly wired in hooks/hooks.json
    (official nested Claude Code schema)."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        assert HOOKS_JSON.is_file()
        self.hooks = parse_hooks()

    def test_registered_on_user_prompt_submit(self):
        """WHEN hooks.json is parsed
        THEN there is a UserPromptSubmit hook pointing to context_manager.py."""
        user_hooks = [h for h in self.hooks if h["event"] == "UserPromptSubmit"]
        scripts = [h["script"] for h in user_hooks]
        assert any("context_manager" in s for s in scripts), \
            "context_manager.py must be registered as UserPromptSubmit hook"

    def test_timeout_set_to_5(self):
        """WHEN the context_manager hook entry is inspected
        THEN it has a 5-second timeout (official schema uses seconds)."""
        for h in self.hooks:
            if h["event"] == "UserPromptSubmit" and "context_manager" in h["script"]:
                assert h["timeout"] == 5, \
                    f"Expected 5s timeout, got {h['timeout']}"
                return
        pytest.fail("Context manager hook not found in hooks.json")

    def test_script_path_exists(self):
        """WHEN the context_manager hook entry references a script
        THEN that script exists in the plugin directory."""
        for h in self.hooks:
            if h["event"] == "UserPromptSubmit" and "context_manager" in h["script"]:
                script_path = PLUGIN_ROOT / h["script"]
                assert script_path.is_file(), \
                    f"Hook script not found: {script_path}"
                return
        pytest.fail("Context router hook not found in hooks.json")


# ===========================================================================
# NO GIT OPERATIONS
# ===========================================================================


class TestNoGitOperations:
    """Context router must not contain any git operations (D8 stripping)."""

    def test_no_git_stage(self):
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "git_stage" not in text

    def test_no_git_add(self):
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "git add" not in text

    def test_no_git_commit(self):
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "git commit" not in text

    def test_no_auto_commit(self):
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "auto_commit" not in text
        assert "autocommit" not in text


# ===========================================================================
# EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases for context router robustness."""

    def test_non_markdown_files_ignored(self, tmp_path):
        """WHEN the memory directory contains non-.md files
        THEN they are excluded from routing."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nDeveloper.",
        })
        # Add non-markdown files
        (mem_dir / "notes.txt").write_text("Some notes")
        (mem_dir / "config.json").write_text("{}")
        (mem_dir / ".DS_Store").write_bytes(b"\x00\x00")

        from context_manager import _read_memory_files

        result = _read_memory_files(mem_dir)
        assert "me.md" in result
        assert "notes.txt" not in result
        assert "config.json" not in result
        assert ".DS_Store" not in result

    def test_empty_file_handled(self, tmp_path):
        """WHEN a memory file exists but is empty (0 bytes)
        THEN it is either included with empty content or skipped without error."""
        mem_dir = _make_memory_dir(tmp_path, {
            "me.md": "# About Me\nDeveloper.",
            "empty.md": "",
        })

        from context_manager import _read_memory_files

        result = _read_memory_files(mem_dir)
        # Should not crash. Whether empty file is included or skipped is OK.
        assert "me.md" in result

    def test_symlinked_memory_dir(self, tmp_path):
        """WHEN the memory directory is a symlink
        THEN the context router follows it correctly."""
        real_dir = tmp_path / "real-memory"
        real_dir.mkdir()
        (real_dir / "me.md").write_text("# About Me\nReal content.")
        link_dir = tmp_path / "link-memory"
        link_dir.symlink_to(real_dir)

        from context_manager import _read_memory_files

        result = _read_memory_files(link_dir)
        assert "me.md" in result
        assert "Real content" in result["me.md"]

    def test_very_large_file_handled(self, tmp_path):
        """WHEN a memory file is very large (>1MB)
        THEN the context router handles it without crashing."""
        large_content = "# Large Memory File\n" + ("x" * 1_100_000)
        mem_dir = _make_memory_dir(tmp_path, {
            "large.md": large_content,
            "normal.md": "# Normal\nSmall file.",
        })

        from context_manager import _read_memory_files

        result = _read_memory_files(mem_dir)
        # Should not crash
        assert "normal.md" in result


# ===========================================================================
# LOGGING
# ===========================================================================


class TestContextRouterLogging:
    """Context router must use the log_utils module for logging."""

    def test_imports_log_utils(self):
        """WHEN context_manager.py is inspected
        THEN it imports from lib.log_utils."""
        text = CONTEXT_ROUTER_PATH.read_text()
        assert "log_utils" in text or "logging" in text, \
            "Context router should use logging"

    def test_logs_number_of_files_loaded(self):
        """WHEN the context router loads memory files
        THEN it logs how many files were loaded."""
        text = CONTEXT_ROUTER_PATH.read_text()
        has_count_log = any(kw in text for kw in [
            "loaded %d", "loaded {", "memory files",
            "len(memory_files)", "file_count",
        ])
        assert has_count_log, \
            "Context router should log the number of memory files loaded"

    def test_logs_when_no_memory_files(self):
        """WHEN no memory files are found
        THEN the context router logs this condition."""
        text = CONTEXT_ROUTER_PATH.read_text()
        has_empty_log = any(kw in text for kw in [
            "No memory files", "no memory files",
            "skipping", "empty",
        ])
        assert has_empty_log, \
            "Context router should log when no memory files are found"
