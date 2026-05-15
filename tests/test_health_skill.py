"""Tests for the /multiplai:health skill — memory audit.

Block 9: Dream & Health Skills
Covers: D6 skill definition, health.md prompt, health_check.py script,
ModelClient reporting (R1), Paths validation, --plugin-dir execution.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR


# ---------------------------------------------------------------------------
# Skill definition in plugin.json
# ---------------------------------------------------------------------------


def _parse_fm(skill_file):
    import re as _re
    text = (PLUGIN_ROOT / skill_file).read_text()
    m = _re.match(r'^---\n(.*?)\n---', text, _re.DOTALL)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                fm[k.strip()] = v.strip().strip('"')
    return fm


class TestHealthSkillFrontmatter:
    """Verify health skill frontmatter for CC auto-discovery."""

    def test_health_skill_file_exists(self):
        assert (PLUGIN_ROOT / "skills" / "health.md").is_file()

    def test_health_skill_has_frontmatter(self):
        assert _parse_fm("skills/health.md"), "skills/health.md missing YAML frontmatter"

    def test_health_skill_name(self):
        assert _parse_fm("skills/health.md").get("name") == "health"

    def test_health_skill_has_description(self):
        assert _parse_fm("skills/health.md").get("description", "").strip()


# ---------------------------------------------------------------------------
# Health skill markdown content (skills/health.md)
# ---------------------------------------------------------------------------


class TestHealthSkillPrompt:
    """Verify health.md prompt content meets spec requirements."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "health.md").read_text()

    def test_has_title_heading(self):
        """Health skill must have a top-level heading."""
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_audit_or_health(self):
        """Health skill prompt must describe audit/health purpose."""
        assert re.search(r"(?i)(audit|health|check)", self.text)

    def test_mentions_memory_files(self):
        """Health skill must list expected memory files to check."""
        assert re.search(r"(?i)me\.md", self.text)
        assert re.search(r"(?i)technical-pref\.md", self.text)
        assert re.search(r"(?i)preferences\.md", self.text)

    def test_mentions_existence_check(self):
        """Health skill must check whether memory files exist."""
        assert re.search(r"(?i)(exist|missing|found)", self.text)

    def test_mentions_size_check(self):
        """Health skill must report file sizes."""
        assert re.search(r"(?i)size", self.text)

    def test_mentions_last_modified(self):
        """Health skill must report last-modified timestamps."""
        assert re.search(r"(?i)(modif|timestamp|date|last)", self.text)

    def test_mentions_diary_status(self):
        """Health skill must check diary directory status."""
        assert re.search(r"(?i)diary", self.text)

    def test_mentions_learnings_status(self):
        """Health skill must check unprocessed learnings."""
        assert re.search(r"(?i)learning", self.text)

    def test_mentions_autodream_status(self):
        """Health skill must report last AutoDream consolidation date."""
        assert re.search(r"(?i)(autodream|consolidat|dream|never)", self.text)

    def test_mentions_recommendations(self):
        """Health skill must provide recommendations for issues found."""
        assert re.search(r"(?i)(recommend|suggest|action)", self.text)

    def test_recommends_setup_for_missing(self):
        """Health skill must recommend /multiplai:setup for missing config."""
        assert re.search(r"(?i)setup", self.text)

    def test_no_hardcoded_paths(self):
        """Health skill must not contain hardcoded filesystem paths."""
        assert "/home/" not in self.text
        assert "/Users/" not in self.text
        assert "~/.claude/" not in self.text
        assert "~/.multiplai/" not in self.text

    def test_no_direct_sdk_imports(self):
        """Health skill must not reference direct SDK imports."""
        assert "import claude_agent_sdk" not in self.text
        assert "from claude_agent_sdk" not in self.text
        assert "import anthropic" not in self.text

    def test_mentions_path_resolver(self):
        """Health skill must instruct using path resolver for file locations."""
        assert re.search(r"(?i)(path.?resolv|hardcod|custom.*director)", self.text)

    def test_works_with_custom_directories(self):
        """Health skill must mention custom directory support via userConfig."""
        assert re.search(r"(?i)(custom|config|userConfig)", self.text)

    def test_handles_fresh_install(self):
        """Health skill must handle fresh install (no memory dir) gracefully."""
        assert re.search(r"(?i)(not.*configured|setup|fresh|first)", self.text)


# ---------------------------------------------------------------------------
# Health skill reports ModelClient status (R1)
# ---------------------------------------------------------------------------


class TestHealthSkillReportsModelClient:
    """R1: Health skill must report which ModelClient implementation is active."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "health.md").read_text()

    def test_health_check_script_reports_client_type(self):
        """health_check.py must report the active ModelClient implementation.

        The health check script must include which client backend is active
        (AgentSDKClient or AnthropicAPIClient) so operators can diagnose
        configuration issues.
        """
        health_script = SCRIPTS_DIR / "health_check.py"
        assert health_script.is_file(), \
            "health_check.py must exist for health skill to report client type"
        source = health_script.read_text()
        # Must reference detect_client_type or ModelClient detection
        assert re.search(
            r"(?i)(detect_client_type|model.?client|agent.?sdk|anthropic.?api)",
            source,
        ), "health_check.py must detect and report the active ModelClient type"


class TestDetectClientTypeFunction:
    """Verify detect_client_type() function in model_client module."""

    def test_detect_client_type_exists(self):
        """model_client module must expose detect_client_type()."""
        from lib.model_client import detect_client_type
        assert callable(detect_client_type)

    def test_detect_client_type_returns_string(self):
        """detect_client_type() must return a human-readable string."""
        from lib.model_client import detect_client_type
        result = detect_client_type()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_detect_client_type_without_sdk(self):
        """When no SDK and no API key, reports 'none'."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove API key if present
            os.environ.pop("CLAUDE_PLUGIN_OPTION_anthropic_api_key", None)
            import importlib
            import lib.model_client as mc
            # Mock claude_agent_sdk import to fail
            with patch.dict("sys.modules", {"claude_agent_sdk": None}):
                with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (_ for _ in ()).throw(ImportError()) if name == "claude_agent_sdk" else importlib.__import__(name, *a, **kw)):
                    result = mc.detect_client_type()
            assert "none" in result.lower() or "unavailable" in result.lower() \
                or "no" in result.lower()

    def test_detect_client_type_with_api_key(self):
        """When API key is set, reports AnthropicAPIClient."""
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_anthropic_api_key": "sk-test"}):
            import importlib
            import lib.model_client as mc
            # Mock claude_agent_sdk import to fail so API key fallback is tested
            with patch.dict("sys.modules", {"claude_agent_sdk": None}):
                with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (_ for _ in ()).throw(ImportError()) if name == "claude_agent_sdk" else importlib.__import__(name, *a, **kw)):
                    result = mc.detect_client_type()
            assert "anthropic" in result.lower() or "api" in result.lower()


# ---------------------------------------------------------------------------
# Health check script existence and structure
# ---------------------------------------------------------------------------


class TestHealthCheckScriptExists:
    """Verify health_check.py exists and is valid Python."""

    def test_script_exists(self):
        """health_check.py must exist in scripts/ directory.

        The health.md skill references 'python scripts/health_check.py',
        so this script must exist for the skill to work.
        """
        assert (SCRIPTS_DIR / "health_check.py").is_file(), \
            "health_check.py must exist — health.md skill references it"

    def test_script_compiles(self):
        """health_check.py must be syntactically valid Python."""
        import py_compile
        py_compile.compile(
            str(SCRIPTS_DIR / "health_check.py"),
            doraise=True,
        )


class TestHealthCheckUsesPathResolver:
    """Verify health_check.py uses the path resolver for all file locations."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_imports_path_resolver(self):
        """health_check.py must import from the paths module."""
        assert re.search(r"from\s+lib\.paths\s+import", self.source)

    def test_no_hardcoded_paths(self):
        """health_check.py must not contain hardcoded directory paths."""
        assert "~/.multiplai/" not in self.source
        assert "~/.claude/" not in self.source
        lines = self.source.split("\n")
        for line in lines:
            if line.strip().startswith("#"):
                continue
            assert "/home/" not in line or "expanduser" in line, \
                f"Hardcoded /home/ path found: {line.strip()}"


class TestHealthCheckUsesModelClient:
    """Verify health_check.py uses model client for client detection."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_imports_model_client(self):
        """health_check.py must import from the model_client module."""
        assert re.search(
            r"from\s+lib\.model_client\s+import",
            self.source,
        )

    def test_no_direct_sdk_imports(self):
        """health_check.py must not directly import claude_agent_sdk."""
        lines = self.source.split("\n")
        for line in lines:
            if line.strip().startswith("#"):
                continue
            assert "import claude_agent_sdk" not in line
            assert "from claude_agent_sdk" not in line


# ---------------------------------------------------------------------------
# Health skill validates Paths fields resolve to existing directories
# ---------------------------------------------------------------------------


class TestHealthCheckValidatesPathsExist:
    """Health skill must validate that all Paths fields resolve to existing dirs."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_checks_memory_dir_exists(self):
        """health_check.py must verify memory_dir exists on disk."""
        assert re.search(r"(?i)memory.*dir|memory_dir", self.source)
        assert re.search(r"(?i)(exist|is_dir|isdir)", self.source)

    def test_checks_diary_dir_exists(self):
        """health_check.py must verify diary_dir exists on disk."""
        assert re.search(r"(?i)diary.*dir|diary_dir", self.source)

    def test_checks_data_dir_exists(self):
        """health_check.py must verify plugin data dir exists on disk."""
        assert re.search(r"(?i)data.*dir|data_dir|plugin_data", self.source)

    def test_checks_venv_dir_exists(self):
        """health_check.py must verify venv directory exists."""
        assert re.search(r"(?i)venv", self.source)

    def test_reports_directory_status(self):
        """health_check.py must report whether each directory exists."""
        # Should output structured status for each path
        assert re.search(r"(?i)(status|exists?|missing|found|not found)", self.source)


# ---------------------------------------------------------------------------
# Health skill memory file inventory
# ---------------------------------------------------------------------------


class TestHealthCheckMemoryInventory:
    """Health skill must list each expected memory file with metadata."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_checks_me_md(self):
        """health_check.py must check for me.md."""
        assert "me.md" in self.source

    def test_checks_technical_pref_md(self):
        """health_check.py must check for technical-pref.md."""
        assert "technical-pref.md" in self.source

    def test_checks_preferences_md(self):
        """health_check.py must check for preferences.md."""
        assert "preferences.md" in self.source

    def test_reports_file_size(self):
        """health_check.py must report file sizes."""
        assert re.search(r"(?i)(size|stat|st_size|bytes)", self.source)

    def test_reports_last_modified(self):
        """health_check.py must report last-modified timestamps."""
        assert re.search(r"(?i)(mtime|st_mtime|modified|timestamp)", self.source)


# ---------------------------------------------------------------------------
# Health skill staleness detection
# ---------------------------------------------------------------------------


class TestHealthCheckStalenessDetection:
    """Health skill must flag files not modified in >30 days as stale."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_checks_staleness(self):
        """health_check.py must check file age for staleness."""
        assert re.search(r"(?i)(stale|days|30|age|old)", self.source)

    def test_recommends_dream_for_stale(self):
        """health_check.py must recommend /multiplai:dream for stale files."""
        assert re.search(r"(?i)dream", self.source)


# ---------------------------------------------------------------------------
# Health skill diary and learnings status
# ---------------------------------------------------------------------------


class TestHealthCheckDiaryAndLearnings:
    """Health skill must report diary entries and unprocessed learnings."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_counts_diary_entries(self):
        """health_check.py must count diary entries."""
        assert re.search(r"(?i)diary", self.source)

    def test_counts_learnings(self):
        """health_check.py must count unprocessed learnings."""
        assert re.search(r"(?i)learning", self.source)

    def test_reports_last_dream_date(self):
        """health_check.py must report last AutoDream date or 'never'."""
        assert re.search(r"(?i)(dream.*state|last.*dream|never|consolidat)", self.source)


# ---------------------------------------------------------------------------
# Health skill handles fresh install
# ---------------------------------------------------------------------------


class TestHealthCheckFreshInstall:
    """Health skill must handle completely fresh install without errors."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_handles_missing_memory_dir(self):
        """health_check.py must handle non-existent memory directory gracefully."""
        # Should check existence before accessing files, not crash
        assert re.search(r"(?i)(not.*exist|mkdir|is_dir|exist|setup)", self.source)

    def test_recommends_setup(self):
        """health_check.py must recommend running /multiplai:setup."""
        assert re.search(r"(?i)setup", self.source)


# ---------------------------------------------------------------------------
# Health check output format
# ---------------------------------------------------------------------------


class TestHealthCheckOutputFormat:
    """Health skill must produce markdown-formatted audit report."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "health.md").read_text()

    def test_mentions_markdown_format(self):
        """Health skill output must be markdown-formatted."""
        assert re.search(r"(?i)markdown", self.text)

    def test_has_memory_files_section(self):
        """Health skill output must have a Memory Files section."""
        assert re.search(r"(?i)memory.?file", self.text)

    def test_has_diary_section(self):
        """Health skill output must have a Diary Status section."""
        assert re.search(r"(?i)diary", self.text)

    def test_has_recommendations_section(self):
        """Health skill output must have a Recommendations section."""
        assert re.search(r"(?i)recommend", self.text)


# ---------------------------------------------------------------------------
# No git operations in health scripts
# ---------------------------------------------------------------------------


class TestHealthNoGitOperations:
    """Verify health-related scripts contain no git staging/commit logic."""

    def test_health_check_no_git_stage(self):
        """health_check.py must not contain git_stage calls."""
        path = SCRIPTS_DIR / "health_check.py"
        if path.is_file():
            source = path.read_text()
            assert "git_stage" not in source

    def test_health_check_no_git_commit(self):
        """health_check.py must not contain git commit calls."""
        path = SCRIPTS_DIR / "health_check.py"
        if path.is_file():
            source = path.read_text()
            assert not re.search(r'git\s+commit\b', source)


# ---------------------------------------------------------------------------
# Both skills referenced scripts are entry points that can be invoked
# ---------------------------------------------------------------------------


class TestSkillScriptsAreEntryPoints:
    """Verify skill scripts can be invoked as standalone entry points."""

    def test_autodream_has_main_guard(self):
        """autodream.py must have if __name__ == '__main__' guard."""
        source = (SCRIPTS_DIR / "autodream.py").read_text()
        assert re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', source)

    def test_health_check_has_main_guard(self):
        """health_check.py must have if __name__ == '__main__' guard."""
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        source = path.read_text()
        assert re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', source)


# ---------------------------------------------------------------------------
# Skills execute under --plugin-dir (structural validation)
# ---------------------------------------------------------------------------


class TestSkillsPluginDirExecution:
    """Verify both skills can be found and loaded under --plugin-dir.

    These tests validate the structural requirements for plugin-dir execution:
    - skill files referenced in plugin.json exist
    - scripts referenced by skills exist
    - plugin.json is valid JSON with correct structure
    """

    def test_all_skill_files_exist(self):
        """All skill files in skills/ must exist with frontmatter."""
        for skill_file in ["skills/health.md", "skills/dream.md", "skills/setup.md", "skills/refresh-catalogs.md"]:
            path = PLUGIN_ROOT / skill_file
            assert path.is_file(), f"Skill file missing: {skill_file}"

    def test_dream_skill_autodream_script_exists(self):
        """autodream.py referenced by dream skill must exist."""
        assert (SCRIPTS_DIR / "autodream.py").is_file()

    def test_health_skill_health_check_script_exists(self):
        """health_check.py referenced by health skill must exist."""
        assert (SCRIPTS_DIR / "health_check.py").is_file(), \
            "health_check.py must exist for health skill to function under --plugin-dir"

    def test_synthesize_now_exists(self):
        """synthesize_now.py used by dream pipeline must exist."""
        assert (SCRIPTS_DIR / "synthesize_now.py").is_file()

    def test_scripts_use_sys_path_setup(self):
        """All scripts must set up sys.path for lib/ imports."""
        for script_name in ["autodream.py"]:
            source = (SCRIPTS_DIR / script_name).read_text()
            assert re.search(r"sys\.path\.insert", source), \
                f"{script_name} must set up sys.path for lib/ imports"

    def test_health_check_uses_sys_path_setup(self):
        """health_check.py must set up sys.path for lib/ imports."""
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        source = path.read_text()
        assert re.search(r"sys\.path\.insert", source), \
            "health_check.py must set up sys.path for lib/ imports"


# ---------------------------------------------------------------------------
# Health skill outputs structured JSON or formatted text
# ---------------------------------------------------------------------------


class TestHealthCheckStructuredOutput:
    """Health check script must produce parseable structured output."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = SCRIPTS_DIR / "health_check.py"
        assert path.is_file(), "health_check.py must exist"
        self.source = path.read_text()

    def test_produces_output(self):
        """health_check.py must write output to stdout (print/json.dump)."""
        assert re.search(r"(print|json\.dump|sys\.stdout)", self.source)

    def test_includes_all_path_fields(self):
        """health_check.py must report on all major path fields."""
        # Must reference the key paths that need validation
        assert re.search(r"(?i)memory", self.source)
        assert re.search(r"(?i)diary", self.source)
        assert re.search(r"(?i)venv", self.source)
