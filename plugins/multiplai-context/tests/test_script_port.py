"""Tests for ported scripts in scripts/ directory."""

import py_compile
import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

EXPECTED_SCRIPTS = [
    "session_start.py",
    "context_manager.py",
    "session_stop.py",
    "session_end.py",
    "pre_compact.py",
    "extract_learnings.py",
    "dream.py",
    "synthesize_now.py",
    "generate_catalog.py",
]


class TestScriptExistence:
    """Verify all expected scripts exist."""

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_script_exists(self, script):
        assert (SCRIPTS_DIR / script).is_file(), f"Script missing: {script}"

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_script_compiles(self, script):
        path = SCRIPTS_DIR / script
        if path.exists():
            py_compile.compile(str(path), doraise=True)


class TestNoBashWrappers:
    """Verify no shell scripts in scripts directory."""

    def test_no_sh_files(self):
        if SCRIPTS_DIR.exists():
            sh_files = list(SCRIPTS_DIR.glob("*.sh")) + list(SCRIPTS_DIR.glob("*.bash")) + list(SCRIPTS_DIR.glob("*.zsh"))
            assert len(sh_files) == 0, f"Shell scripts found: {[f.name for f in sh_files]}"

    def test_no_subprocess_calls_to_sh_scripts(self):
        for script in EXPECTED_SCRIPTS:
            path = SCRIPTS_DIR / script
            if path.exists():
                text = path.read_text()
                # Check for subprocess calls to .sh scripts
                assert not re.search(r'subprocess\.\w+\([^)]*\.sh', text), \
                    f"{script} calls a .sh script via subprocess"


class TestNoDirectSDKImports:
    """Verify no direct claude_agent_sdk imports in ported scripts."""

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_no_claude_agent_sdk_import(self, script):
        path = SCRIPTS_DIR / script
        if path.exists():
            text = path.read_text()
            assert "import claude_agent_sdk" not in text, \
                f"{script} has direct claude_agent_sdk import"
            assert "from claude_agent_sdk" not in text, \
                f"{script} has direct claude_agent_sdk import"

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_no_direct_anthropic_import(self, script):
        path = SCRIPTS_DIR / script
        if path.exists():
            text = path.read_text()
            # Direct anthropic import only allowed in model_client.py
            assert "import anthropic" not in text, \
                f"{script} has direct anthropic import"


class TestNoHardcodedPaths:
    """Verify no hardcoded paths in ported scripts."""

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_no_hardcoded_home(self, script):
        path = SCRIPTS_DIR / script
        if path.exists():
            text = path.read_text()
            assert "~/.multiplai" not in text, f"{script} has hardcoded ~/.multiplai"
            assert "~/.claude/" not in text, f"{script} has hardcoded ~/.claude/"

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_no_hardcoded_user_paths(self, script):
        path = SCRIPTS_DIR / script
        if path.exists():
            text = path.read_text()
            assert "/home/spike" not in text
            assert "/Users/spike" not in text


class TestContextManagerPort:
    """Verify context manager port specifics."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        path = SCRIPTS_DIR / "context_manager.py"
        self.text = path.read_text() if path.exists() else ""

    def test_no_catalog_routing(self):
        assert "generate-catalog" not in self.text
        assert "skill-catalog" not in self.text
        assert "resource-catalog" not in self.text

    def test_uses_path_resolver(self):
        assert "from multiplai_core.paths" in self.text or "multiplai_core.paths" in self.text


class TestSessionLifecyclePort:
    """Verify session lifecycle port specifics."""

    def test_session_start_uses_paths(self):
        text = (SCRIPTS_DIR / "session_start.py").read_text()
        assert "from multiplai_core.paths" in text or "multiplai_core.paths" in text

    def test_session_end_uses_paths(self):
        text = (SCRIPTS_DIR / "session_end.py").read_text()
        assert "from multiplai_core.paths" in text or "multiplai_core.paths" in text

    def test_no_auto_commit_in_session_start(self):
        text = (SCRIPTS_DIR / "session_start.py").read_text()
        assert "git commit" not in text
        assert "git add" not in text
        assert "git_stage" not in text

    def test_no_auto_commit_in_session_end(self):
        text = (SCRIPTS_DIR / "session_end.py").read_text()
        assert "git commit" not in text
        assert "git add" not in text
        assert "git_stage" not in text


class TestExtractLearningsPort:
    """Verify extract learnings port specifics."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        self.text = (SCRIPTS_DIR / "extract_learnings.py").read_text()

    def test_uses_path_resolver(self):
        assert "from multiplai_core.paths" in self.text or "multiplai_core.paths" in self.text

    def test_uses_model_client(self):
        assert "from multiplai_core.model_client" in self.text or "model_client" in self.text

    def test_no_git_stage(self):
        assert "git_stage" not in self.text
        assert "git add" not in self.text


class TestAutodreamPort:
    """Verify dream port specifics."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        self.text = (SCRIPTS_DIR / "dream.py").read_text()

    def test_uses_path_resolver(self):
        assert "from multiplai_core.paths" in self.text or "multiplai_core.paths" in self.text

    def test_uses_model_client(self):
        assert "from multiplai_core.model_client" in self.text or "model_client" in self.text


class TestSynthesizeNowPort:
    """Verify synthesize now port specifics."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        self.text = (SCRIPTS_DIR / "synthesize_now.py").read_text()

    def test_uses_path_resolver(self):
        assert "from multiplai_core.paths" in self.text or "multiplai_core.paths" in self.text

    def test_uses_model_client(self):
        assert "from multiplai_core.model_client" in self.text or "model_client" in self.text


class TestGenerateCatalogPort:
    """Verify generate catalog port specifics."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        self.text = (SCRIPTS_DIR / "generate_catalog.py").read_text()

    def test_uses_path_resolver(self):
        assert "from multiplai_core.paths" in self.text or "multiplai_core.paths" in self.text

    def test_no_skill_catalog(self):
        assert "skill-catalog" not in self.text
        assert "resource-catalog" not in self.text
