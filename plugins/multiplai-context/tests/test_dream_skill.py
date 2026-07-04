"""Tests for the /multiplai-context:dream skill — manual Dream trigger.

Block 9: Dream & Health Skills
Covers: D6 skill definition, dream.md prompt, dream pipeline invocation,
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


def _dream_fm():
    import re as _re
    text = (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").read_text()
    m = _re.match(r'^---\n(.*?)\n---', text, _re.DOTALL)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                fm[k.strip()] = v.strip().strip('"')
    return fm


class TestDreamSkillFrontmatter:
    """Verify dream skill frontmatter for CC auto-discovery."""

    def test_dream_skill_file_exists(self):
        assert (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").is_file()

    def test_dream_skill_has_frontmatter(self):
        assert _dream_fm(), "skills/dream.md missing YAML frontmatter"

    def test_dream_skill_name(self):
        assert _dream_fm().get("name") == "dream"

    def test_dream_skill_has_description(self):
        assert _dream_fm().get("description", "").strip()


# ---------------------------------------------------------------------------
# Dream skill markdown content (skills/dream.md)
# ---------------------------------------------------------------------------


class TestDreamSkillPrompt:
    """Verify dream.md prompt content meets spec requirements."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").read_text()

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

    def test_references_dream_script(self):
        """Dream skill must reference the dream.py script for execution."""
        assert "dream.py" in self.text.lower()

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
# Dream script — behavioral contracts
# ---------------------------------------------------------------------------


class TestAutodreamScriptExists:
    """Verify the dream.py script exists and is valid Python."""

    def test_script_exists(self):
        """dream.py must exist in scripts/."""
        assert (SCRIPTS_DIR / "dream.py").is_file()

    def test_script_compiles(self):
        """dream.py must be syntactically valid Python."""
        import py_compile
        py_compile.compile(
            str(SCRIPTS_DIR / "dream.py"),
            doraise=True,
        )


class TestAutodreamUsesPathResolver:
    """Verify dream.py uses the path resolver for all file locations."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_imports_path_resolver(self):
        """dream.py must import from the paths module."""
        assert re.search(r"from\s+multiplai_core\.paths\s+import", self.source)

    def test_no_hardcoded_paths(self):
        """dream.py must not contain hardcoded directory paths."""
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
    """Verify dream.py uses ModelClient abstraction for LLM calls."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_imports_model_client(self):
        """dream.py must import from the model_client module."""
        assert re.search(r"from\s+multiplai_core\.model_client\s+import", self.source)

    def test_no_direct_sdk_imports(self):
        """dream.py must not directly import claude_agent_sdk or anthropic."""
        lines = self.source.split("\n")
        for line in lines:
            if line.strip().startswith("#"):
                continue
            assert "import claude_agent_sdk" not in line, \
                f"Direct SDK import found: {line.strip()}"
            assert "from claude_agent_sdk" not in line, \
                f"Direct SDK import found: {line.strip()}"

    def test_uses_create_client(self):
        """dream.py must use create_client() factory."""
        assert "create_client" in self.source


class TestAutodreamDreamState:
    """Verify dream persists dream state to plugin data directory."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_reads_dream_state(self):
        """dream must read dream state from a path-resolved location."""
        assert re.search(r"dream_state", self.source)

    def test_writes_dream_state(self):
        """dream must save dream state after processing."""
        assert re.search(r"save.*dream.*state|dream_state.*write|_save_dream_state", self.source)

    def test_uses_yaml_for_state(self):
        """Dream state should be persisted as YAML."""
        assert "yaml" in self.source.lower()


class TestAutodreamLearnings:
    """Verify dream reads learnings from path-resolved location."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_reads_learnings_file(self):
        """dream must read learnings from path-resolved location."""
        assert re.search(r"learnings", self.source)

    def test_updates_memory_files(self):
        """dream must write updates to memory directory."""
        assert re.search(r"memory", self.source.lower())


class TestAutodreamCheckMode:
    """Verify dream supports a --check mode for dry-run inspection."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_supports_check_flag(self):
        """dream.py must accept --check flag for checking pending learnings."""
        assert re.search(r"--check", self.source)

    def test_supports_run_flag(self):
        """dream.py must accept --run flag for triggering consolidation."""
        assert re.search(r"--run", self.source)


# ---------------------------------------------------------------------------
# Dream skill output contract
# ---------------------------------------------------------------------------


class TestDreamSkillOutputContract:
    """Verify dream skill produces structured output per spec."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").read_text()

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
# No git operations in dream/dream
# ---------------------------------------------------------------------------


class TestDreamNoGitOperations:
    """Verify dream-related scripts contain no git staging/commit logic."""

    @pytest.fixture(autouse=True)
    def load_sources(self):
        self.dream_source = (SCRIPTS_DIR / "dream.py").read_text()
        self.dream_skill = (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").read_text()

    def test_dream_no_git_stage(self):
        """dream.py must not contain git_stage calls."""
        assert "git_stage" not in self.dream_source

    def test_dream_no_git_add(self):
        """dream.py must not contain git add calls."""
        # Check for subprocess git add, not variable names
        assert not re.search(r'git\s+add\b', self.dream_source)

    def test_dream_no_git_commit(self):
        """dream.py must not contain git commit calls."""
        assert not re.search(r'git\s+commit\b', self.dream_source)


# ---------------------------------------------------------------------------
# Proposal filename versioning — no silent same-day overwrite
# ---------------------------------------------------------------------------


def _load_dream_module():
    """Import dream.py as an isolated module (the venv guard no-ops in-venv)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dream_under_test", SCRIPTS_DIR / "dream.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestProposalOutputPath:
    """`_proposal_output_path` must never clobber an existing same-day proposal."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.dream = _load_dream_module()

    def test_uses_base_name_when_free(self, tmp_path):
        p = self.dream._proposal_output_path(tmp_path, "2026-06-26")
        assert p == tmp_path / "processed-learnings-2026-06-26.md"

    def test_versions_when_base_exists(self, tmp_path):
        (tmp_path / "processed-learnings-2026-06-26.md").write_text("first")
        p = self.dream._proposal_output_path(tmp_path, "2026-06-26")
        assert p == tmp_path / "processed-learnings-2026-06-26-2.md"

    def test_increments_past_existing_versions(self, tmp_path):
        (tmp_path / "processed-learnings-2026-06-26.md").write_text("first")
        (tmp_path / "processed-learnings-2026-06-26-2.md").write_text("second")
        p = self.dream._proposal_output_path(tmp_path, "2026-06-26")
        assert p == tmp_path / "processed-learnings-2026-06-26-3.md"

    def test_never_returns_an_existing_path(self, tmp_path):
        # Whatever the state, the returned path must not already exist.
        for name in ("processed-learnings-2026-06-26.md",
                     "processed-learnings-2026-06-26-2.md"):
            (tmp_path / name).write_text("x")
        p = self.dream._proposal_output_path(tmp_path, "2026-06-26")
        assert not p.exists()


class TestDreamTimeoutDefault:
    """dream.py raises the SDK call timeout for long batch runs before import."""

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_sets_1800s_default(self):
        assert re.search(
            r'os\.environ\.setdefault\(\s*["\']MULTIPLAI_SDK_CALL_TIMEOUT_S["\']\s*,\s*["\']1800["\']',
            self.source,
        )

    def test_set_before_model_client_import(self):
        setdefault_idx = self.source.index("MULTIPLAI_SDK_CALL_TIMEOUT_S")
        import_idx = self.source.index("from lib.model_client import")
        assert setdefault_idx < import_idx
