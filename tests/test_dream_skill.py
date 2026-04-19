"""Tests for the /multiplai:dream skill — manual AutoDream trigger.

Block 9: Dream & Health Skills
Covers: D6 skill definition, dream.md prompt, autodream pipeline invocation,
model client usage, error handling, and --plugin-dir execution.
"""

import json
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


class TestDreamSkillManifest:
    """Verify dream skill declaration in plugin.json."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        self.manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        self.dream_skill = next(
            (s for s in self.manifest.get("skills", []) if s["name"] == "dream"),
            None,
        )

    def test_dream_skill_exists_in_manifest(self):
        """Dream skill must be declared in plugin.json skills array."""
        assert self.dream_skill is not None, "dream skill not found in plugin.json"

    def test_dream_skill_has_description(self):
        """Dream skill must have a non-empty description."""
        assert self.dream_skill["description"].strip()

    def test_dream_skill_file_path(self):
        """Dream skill must reference skills/dream.md."""
        assert self.dream_skill["file"] == "skills/dream.md"

    def test_dream_skill_file_exists(self):
        """The referenced skill file must exist on disk."""
        assert (PLUGIN_ROOT / self.dream_skill["file"]).is_file()


# ---------------------------------------------------------------------------
# Dream skill markdown content (skills/dream.md)
# ---------------------------------------------------------------------------


class TestDreamSkillPrompt:
    """Verify dream.md prompt content meets spec requirements."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream.md").read_text()

    def test_has_title_heading(self):
        """Dream skill must have a top-level heading."""
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_consolidation_or_synthesis(self):
        """Dream skill prompt must describe consolidation/synthesis purpose."""
        assert re.search(r"(?i)(consolidat|synthesi|dream)", self.text)

    def test_mentions_learnings(self):
        """Dream skill must reference learnings as input to consolidation."""
        assert re.search(r"(?i)learning", self.text)

    def test_mentions_memory_files(self):
        """Dream skill must mention memory file updates as output."""
        assert re.search(r"(?i)memory", self.text)

    def test_instructs_to_report_results(self):
        """Dream skill must instruct reporting what was consolidated."""
        assert re.search(r"(?i)(report|summary|result)", self.text)

    def test_instructs_check_before_run(self):
        """Dream skill must check for pending learnings before running."""
        assert re.search(r"(?i)(check|pending|nothing)", self.text)

    def test_references_autodream_script(self):
        """Dream skill must reference the autodream.py script for execution."""
        assert "autodream" in self.text.lower()

    def test_no_hardcoded_paths(self):
        """Dream skill must not contain hardcoded filesystem paths."""
        assert "/home/" not in self.text
        assert "/Users/" not in self.text
        assert "~/.claude/" not in self.text
        assert "~/.multiplai/" not in self.text

    def test_no_direct_sdk_imports(self):
        """Dream skill must not reference direct SDK imports."""
        assert "import claude_agent_sdk" not in self.text
        assert "from claude_agent_sdk" not in self.text
        assert "import anthropic" not in self.text

    def test_mentions_model_client_abstraction(self):
        """Dream skill must instruct using model client, not direct SDK."""
        assert re.search(r"(?i)(model.?client|sdk.?direct)", self.text)

    def test_mentions_path_resolver(self):
        """Dream skill must instruct using path resolver for file locations."""
        assert re.search(r"(?i)(path.?resolv|hardcod)", self.text)

    def test_mentions_error_handling(self):
        """Dream skill must mention error handling behavior."""
        assert re.search(r"(?i)(error|fail|partial)", self.text)

    def test_handles_nothing_to_consolidate(self):
        """Dream skill must describe behavior when nothing to consolidate."""
        assert re.search(r"(?i)(nothing|no.+new|empty)", self.text)


# ---------------------------------------------------------------------------
# AutoDream script — behavioral contracts
# ---------------------------------------------------------------------------


class TestAutodreamScriptExists:
    """Verify the autodream.py script exists and is valid Python."""

    def test_script_exists(self):
        """autodream.py must exist in scripts/."""
        assert (SCRIPTS_DIR / "autodream.py").is_file()

    def test_script_compiles(self):
        """autodream.py must be syntactically valid Python."""
        import py_compile
        py_compile.compile(
            str(SCRIPTS_DIR / "autodream.py"),
            doraise=True,
        )


class TestAutodreamUsesPathResolver:
    """Verify autodream.py uses the path resolver for all file locations."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_imports_path_resolver(self):
        """autodream.py must import from the paths module."""
        assert re.search(r"from\s+lib\.paths\s+import", self.source)

    def test_no_hardcoded_paths(self):
        """autodream.py must not contain hardcoded directory paths."""
        # Should not have literal home-dir references
        assert "~/.multiplai/" not in self.source
        assert "~/.claude/" not in self.source
        # No absolute home directory paths
        lines = self.source.split("\n")
        for line in lines:
            if line.strip().startswith("#"):
                continue  # skip comments
            assert "/home/" not in line or "expanduser" in line, \
                f"Hardcoded /home/ path found: {line.strip()}"


class TestAutodreamUsesModelClient:
    """Verify autodream.py uses ModelClient abstraction for LLM calls."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_imports_model_client(self):
        """autodream.py must import from the model_client module."""
        assert re.search(r"from\s+lib\.model_client\s+import", self.source)

    def test_no_direct_sdk_imports(self):
        """autodream.py must not directly import claude_agent_sdk or anthropic."""
        lines = self.source.split("\n")
        for line in lines:
            if line.strip().startswith("#"):
                continue
            assert "import claude_agent_sdk" not in line, \
                f"Direct SDK import found: {line.strip()}"
            assert "from claude_agent_sdk" not in line, \
                f"Direct SDK import found: {line.strip()}"

    def test_uses_create_client(self):
        """autodream.py must use create_client() factory."""
        assert "create_client" in self.source


class TestAutodreamDreamState:
    """Verify autodream persists dream state to plugin data directory."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_reads_dream_state(self):
        """autodream must read dream state from a path-resolved location."""
        assert re.search(r"dream_state", self.source)

    def test_writes_dream_state(self):
        """autodream must save dream state after processing."""
        assert re.search(r"save.*dream.*state|dream_state.*write|_save_dream_state", self.source)

    def test_uses_yaml_for_state(self):
        """Dream state should be persisted as YAML."""
        assert "yaml" in self.source.lower()


class TestAutodreamLearnings:
    """Verify autodream reads learnings from path-resolved location."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_reads_learnings_file(self):
        """autodream must read learnings from path-resolved location."""
        assert re.search(r"learnings", self.source)

    def test_updates_memory_files(self):
        """autodream must write updates to memory directory."""
        assert re.search(r"memory", self.source.lower())


class TestAutodreamCheckMode:
    """Verify autodream supports a --check mode for dry-run inspection."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_supports_check_flag(self):
        """autodream.py must accept --check flag for checking pending learnings."""
        assert re.search(r"--check", self.source)

    def test_supports_run_flag(self):
        """autodream.py must accept --run flag for triggering consolidation."""
        assert re.search(r"--run", self.source)


# ---------------------------------------------------------------------------
# Dream skill output contract
# ---------------------------------------------------------------------------


class TestDreamSkillOutputContract:
    """Verify dream skill produces structured output per spec."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream.md").read_text()

    def test_specifies_learnings_count(self):
        """Dream skill output must include count of learnings processed."""
        assert re.search(r"(?i)(number|count).*(learning|processed)", self.text)

    def test_specifies_memory_files_updated(self):
        """Dream skill output must include which memory files were updated."""
        assert re.search(r"(?i)memory.*(file|update)", self.text)

    def test_specifies_skipped_items(self):
        """Dream skill output must mention skipped items if any."""
        assert re.search(r"(?i)skip", self.text)


# ---------------------------------------------------------------------------
# No git operations in dream/autodream
# ---------------------------------------------------------------------------


class TestDreamNoGitOperations:
    """Verify dream-related scripts contain no git staging/commit logic."""

    @pytest.fixture(autouse=True)
    def load_sources(self):
        self.autodream_source = (SCRIPTS_DIR / "autodream.py").read_text()
        self.dream_skill = (PLUGIN_ROOT / "skills" / "dream.md").read_text()

    def test_autodream_no_git_stage(self):
        """autodream.py must not contain git_stage calls."""
        assert "git_stage" not in self.autodream_source

    def test_autodream_no_git_add(self):
        """autodream.py must not contain git add calls."""
        # Check for subprocess git add, not variable names
        assert not re.search(r'git\s+add\b', self.autodream_source)

    def test_autodream_no_git_commit(self):
        """autodream.py must not contain git commit calls."""
        assert not re.search(r'git\s+commit\b', self.autodream_source)
