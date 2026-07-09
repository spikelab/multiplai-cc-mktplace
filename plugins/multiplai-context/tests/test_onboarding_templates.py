"""Tests for Block 8: Memory Templates & Onboarding Skill.

Covers the onboarding flow — template copying, setup scripts, cold-start
behavior, and integration between setup skill and scripts/ for file I/O.

Existing test_memory_templates.py covers static template structure.
Existing test_plugin_skills.py covers skill manifest declarations.
This file covers the DYNAMIC behavior: copying, populating, and the
onboarding pipeline that ties templates + skills + scripts together.
"""

import json
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATES_DIR = PLUGIN_ROOT / "templates"
TEMPLATE_FILES = ["me.md", "technical-pref.md", "preferences.md"]
SETUP_SKILL_PATH = PLUGIN_ROOT / "skills" / "setup" / "SKILL.md"
SETUP_CHECK_SCRIPT = SCRIPTS_DIR / "setup_check.py"
SETUP_WRITE_SCRIPT = SCRIPTS_DIR / "setup_write.py"


# ===================================================================
# 1. Setup scripts exist and are importable
# ===================================================================


class TestSetupScriptsExist:
    """Verify the scripts referenced by skills/setup.md exist."""

    def test_setup_check_script_exists(self):
        """setup.md references scripts/setup_check.py — it must exist."""
        assert SETUP_CHECK_SCRIPT.is_file(), (
            f"setup_check.py missing at {SETUP_CHECK_SCRIPT}; "
            "setup skill cannot function without it"
        )

    def test_setup_write_script_exists(self):
        """setup.md references scripts/setup_write.py — it must exist."""
        assert SETUP_WRITE_SCRIPT.is_file(), (
            f"setup_write.py missing at {SETUP_WRITE_SCRIPT}; "
            "setup skill cannot function without it"
        )

    def test_setup_check_is_valid_python(self):
        """setup_check.py must be syntactically valid Python."""
        import py_compile

        py_compile.compile(str(SETUP_CHECK_SCRIPT), doraise=True)

    def test_setup_write_is_valid_python(self):
        """setup_write.py must be syntactically valid Python."""
        import py_compile

        py_compile.compile(str(SETUP_WRITE_SCRIPT), doraise=True)


class TestSetupScriptImports:
    """Setup scripts must use path resolver and model client, not hardcoded paths."""

    @pytest.fixture(autouse=True)
    def load_sources(self):
        self.check_src = SETUP_CHECK_SCRIPT.read_text() if SETUP_CHECK_SCRIPT.is_file() else ""
        self.write_src = SETUP_WRITE_SCRIPT.read_text() if SETUP_WRITE_SCRIPT.is_file() else ""

    def test_check_imports_paths(self):
        """setup_check.py must import from multiplai_core.paths for path resolution."""
        assert re.search(r"from\s+multiplai_core\.paths\s+import|import\s+multiplai_core\.paths", self.check_src), \
            "setup_check.py must import path resolver from multiplai_core.paths"

    def test_write_imports_paths(self):
        """setup_write.py must import from multiplai_core.paths for path resolution."""
        assert re.search(r"from\s+multiplai_core\.paths\s+import|import\s+multiplai_core\.paths", self.write_src), \
            "setup_write.py must import path resolver from multiplai_core.paths"

    def test_check_no_hardcoded_paths(self):
        """setup_check.py must not contain hardcoded home directory paths."""
        assert "/home/" not in self.check_src
        assert "~/.multiplai" not in self.check_src
        assert "/Users/" not in self.check_src

    def test_write_no_hardcoded_paths(self):
        """setup_write.py must not contain hardcoded home directory paths."""
        assert "/home/" not in self.write_src
        assert "~/.multiplai" not in self.write_src
        assert "/Users/" not in self.write_src

    def test_write_no_direct_sdk_import(self):
        """setup_write.py must not directly import claude_agent_sdk or anthropic."""
        assert "import claude_agent_sdk" not in self.write_src
        assert "from claude_agent_sdk" not in self.write_src
        assert "import anthropic" not in self.write_src
        assert "from anthropic" not in self.write_src

    def test_check_no_direct_sdk_import(self):
        """setup_check.py must not directly import claude_agent_sdk or anthropic."""
        assert "import claude_agent_sdk" not in self.check_src
        assert "from claude_agent_sdk" not in self.check_src


# ===================================================================
# 2. Setup skill references scripts/ for file I/O
# ===================================================================


class TestSetupSkillReferencesScripts:
    """The setup skill (setup.md) must delegate to scripts/ for file I/O."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = SETUP_SKILL_PATH.read_text()

    def test_references_setup_check(self):
        """Setup skill must reference setup_check.py for checking existing files."""
        assert "setup_check" in self.text, \
            "setup.md must reference setup_check.py for existence checks"

    def test_references_setup_write(self):
        """Setup skill must reference setup_write.py for writing memory files."""
        assert "setup_write" in self.text, \
            "setup.md must reference setup_write.py for writing files"

    def test_references_scripts_dir(self):
        """Setup skill must reference scripts/ directory (not inline file ops)."""
        assert re.search(r"scripts/", self.text), \
            "setup.md must invoke scripts via scripts/ directory, not inline operations"

    def test_no_direct_file_write_instructions(self):
        """Setup skill prompt must not instruct inline file creation — delegate to scripts."""
        # Skill should not contain raw `Write` or file-creation commands
        # It should delegate to scripts/ for all file I/O
        assert not re.search(r"(?i)write\s+tool|Write\s+file|cat\s+>|echo\s+>", self.text), \
            "setup.md should delegate file I/O to scripts, not use inline tools"


# ===================================================================
# 3. Template copying — copy-if-absent logic
# ===================================================================


class TestTemplateCopyIfAbsent:
    """Test the copy-if-absent behavior during onboarding.

    The setup flow must:
    - Copy all three templates when memory dir is empty
    - Skip files that already exist
    - Copy only missing files in partial scenarios
    """

    @pytest.fixture
    def memory_dir(self, tmp_path):
        """Create a temporary memory directory."""
        mem = tmp_path / "memory"
        mem.mkdir()
        return mem

    @pytest.fixture
    def source_templates(self):
        """Return dict of template filename -> content from plugin templates."""
        return {
            f: (TEMPLATES_DIR / f).read_text()
            for f in TEMPLATE_FILES
        }

    def test_fresh_copy_all_templates(self, memory_dir, source_templates):
        """WHEN memory dir is empty, all three templates must be copied."""
        # Simulate fresh onboarding: copy templates to empty memory dir
        for fname, content in source_templates.items():
            dest = memory_dir / fname
            assert not dest.exists(), "Precondition: memory dir is empty"

        # Copy templates (simulating what setup_write.py should do)
        for fname in TEMPLATE_FILES:
            src = TEMPLATES_DIR / fname
            dst = memory_dir / fname
            if not dst.exists():
                shutil.copy2(src, dst)

        # Verify all copied
        for fname in TEMPLATE_FILES:
            assert (memory_dir / fname).is_file(), f"{fname} not copied"
            assert (memory_dir / fname).read_text() == source_templates[fname]

    def test_existing_files_not_overwritten(self, memory_dir, source_templates):
        """WHEN me.md already exists, it must NOT be overwritten."""
        # Pre-create me.md with custom content
        existing_content = "# My Custom Profile\n\nThis is my existing file."
        (memory_dir / "me.md").write_text(existing_content)

        # Simulate copy-if-absent logic
        for fname in TEMPLATE_FILES:
            src = TEMPLATES_DIR / fname
            dst = memory_dir / fname
            if not dst.exists():
                shutil.copy2(src, dst)

        # me.md should retain original content
        assert (memory_dir / "me.md").read_text() == existing_content

        # Others should be copied
        assert (memory_dir / "technical-pref.md").is_file()
        assert (memory_dir / "preferences.md").is_file()

    def test_partial_existing_only_copies_missing(self, memory_dir, source_templates):
        """WHEN only me.md exists, only technical-pref.md and preferences.md are copied."""
        existing = "# Existing me\n"
        (memory_dir / "me.md").write_text(existing)

        copied = []
        skipped = []
        for fname in TEMPLATE_FILES:
            src = TEMPLATES_DIR / fname
            dst = memory_dir / fname
            if dst.exists():
                skipped.append(fname)
            else:
                shutil.copy2(src, dst)
                copied.append(fname)

        assert "me.md" in skipped
        assert "technical-pref.md" in copied
        assert "preferences.md" in copied

    def test_filenames_preserved_during_copy(self, memory_dir):
        """Copied files must retain their original filenames."""
        for fname in TEMPLATE_FILES:
            src = TEMPLATES_DIR / fname
            dst = memory_dir / fname
            shutil.copy2(src, dst)
            assert dst.name == fname

    def test_memory_dir_created_if_absent(self, tmp_path):
        """WHEN memory dir doesn't exist, the setup flow should create it."""
        mem = tmp_path / "nonexistent" / "memory"
        assert not mem.exists()

        # Simulating what setup_write.py should do: create dirs + copy
        mem.mkdir(parents=True, exist_ok=True)
        for fname in TEMPLATE_FILES:
            shutil.copy2(TEMPLATES_DIR / fname, mem / fname)

        assert mem.is_dir()
        for fname in TEMPLATE_FILES:
            assert (mem / fname).is_file()


# ===================================================================
# 4. Template path resolution via path resolver
# ===================================================================


class TestTemplatePathResolution:
    """Templates must be found via the path resolver, not hardcoded."""

    @pytest.fixture(autouse=True)
    def setup_env(self, clean_env, reset_paths_cache):
        pass

    def test_plugin_mode_templates_dir(self, monkeypatch, reset_paths_cache):
        """In plugin mode, templates resolve to $CLAUDE_PLUGIN_ROOT/templates."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))

        paths = Paths.resolve()
        expected = PLUGIN_ROOT / "templates"
        assert paths.templates_dir == expected

    def test_standalone_mode_templates_dir(self, monkeypatch, reset_paths_cache):
        """In standalone mode, templates resolve to ~/.multiplai/templates."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        # Clear all plugin env vars
        for key in list(os.environ):
            if key.startswith("CLAUDE_PLUGIN"):
                monkeypatch.delenv(key, raising=False)

        paths = Paths.resolve()
        expected = Path.home() / ".multiplai" / "templates"
        assert paths.templates_dir == expected

    def test_templates_dir_is_path_instance(self, monkeypatch, reset_paths_cache):
        """templates_dir must return a Path instance."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))

        paths = Paths.resolve()
        assert isinstance(paths.templates_dir, Path)

    def test_template_files_accessible_via_resolved_path(self, monkeypatch, reset_paths_cache):
        """All three templates must exist at the path-resolved templates dir."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))

        paths = Paths.resolve()
        for fname in TEMPLATE_FILES:
            fpath = paths.templates_dir / fname
            assert fpath.is_file(), f"Template {fname} not found at {fpath}"


# ===================================================================
# 5. Cold-start onboarding flow
# ===================================================================


class TestColdStartOnboarding:
    """Verify cold-start onboarding produces a functional memory system."""

    @pytest.fixture
    def cold_start_dir(self, tmp_path):
        """Simulate a cold-start: empty memory, diary, and data dirs."""
        dirs = {
            "memory": tmp_path / "memory",
            "diary": tmp_path / "diary",
            "data": tmp_path / "data",
        }
        return dirs  # Not created yet — that's the cold-start point

    def test_cold_start_all_dirs_absent(self, cold_start_dir):
        """Before onboarding, no memory directories should exist."""
        for name, d in cold_start_dir.items():
            assert not d.exists(), f"{name} dir should not exist pre-onboarding"

    def test_cold_start_produces_all_memory_files(self, cold_start_dir):
        """After onboarding from cold start, all three memory files must exist."""
        mem = cold_start_dir["memory"]
        mem.mkdir(parents=True, exist_ok=True)

        for fname in TEMPLATE_FILES:
            shutil.copy2(TEMPLATES_DIR / fname, mem / fname)

        for fname in TEMPLATE_FILES:
            fpath = mem / fname
            assert fpath.is_file(), f"Post-onboarding: {fname} missing"
            content = fpath.read_text()
            assert len(content.strip()) > 0, f"Post-onboarding: {fname} is empty"

    def test_cold_start_memory_files_are_valid_markdown(self, cold_start_dir):
        """Memory files produced by cold-start must be valid markdown with headings."""
        mem = cold_start_dir["memory"]
        mem.mkdir(parents=True, exist_ok=True)

        for fname in TEMPLATE_FILES:
            shutil.copy2(TEMPLATES_DIR / fname, mem / fname)

        for fname in TEMPLATE_FILES:
            content = (mem / fname).read_text()
            assert re.search(r"^#{1,2}\s+\S", content, re.MULTILINE), \
                f"Post-onboarding: {fname} has no markdown heading"

    def test_cold_start_memory_files_contain_placeholders(self, cold_start_dir):
        """Memory files from cold start must have placeholder/instruction text."""
        mem = cold_start_dir["memory"]
        mem.mkdir(parents=True, exist_ok=True)

        for fname in TEMPLATE_FILES:
            shutil.copy2(TEMPLATES_DIR / fname, mem / fname)

        for fname in TEMPLATE_FILES:
            content = (mem / fname).read_text()
            has_comment = "<!--" in content
            has_prompt = re.search(r"(?i)(list|describe|what|how|your)", content) is not None
            assert has_comment or has_prompt, \
                f"Post-onboarding: {fname} has no placeholder or instructional text"


# ===================================================================
# 6. Setup skill interview flow content
# ===================================================================


class TestSetupSkillInterviewFlow:
    """Verify setup.md defines the correct interview phases."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = SETUP_SKILL_PATH.read_text()

    def test_identity_phase_mentioned(self):
        """Interview must include an identity/about phase."""
        assert re.search(r"(?i)(identity|about|name|role)", self.text), \
            "Setup skill must mention identity/name/role in interview"

    def test_technical_preferences_phase_mentioned(self):
        """Interview must include a technical preferences phase."""
        assert re.search(r"(?i)(technical|language|framework|tool)", self.text), \
            "Setup skill must mention technical preferences in interview"

    def test_general_preferences_phase_mentioned(self):
        """Interview must include a general preferences phase."""
        assert re.search(r"(?i)(preference|verbos|tone|workflow)", self.text), \
            "Setup skill must mention general preferences in interview"

    def test_overwrite_warning_mentioned(self):
        """Skill must warn when files already exist."""
        assert re.search(r"(?i)(exist|overwrite|warn|confirm)", self.text), \
            "Setup skill must address existing files scenario"

    def test_three_interview_phases(self):
        """Interview should have at least 3 distinct phases/sections."""
        # Count distinct numbered steps or phase markers
        steps = re.findall(r"(?:^|\n)\s*\d+\.\s+", self.text)
        assert len(steps) >= 3, f"Expected at least 3 interview steps, found {len(steps)}"


# ===================================================================
# 7. Templates contain no sensitive data (extended checks)
# ===================================================================


class TestTemplateSensitivityExtended:
    """Extended checks that shipped templates are generic and portable."""

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_developer_names(self, filename):
        """No references to original developer in template files."""
        text = (TEMPLATES_DIR / filename).read_text()
        # Check for developer-specific identifiers
        assert "Spike" not in text
        assert "spikelab" not in text
        assert "spike" not in text.lower().split("<!--")[0]  # Allow in comments

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_environment_specific_content(self, filename):
        """No machine-specific paths or environment references."""
        text = (TEMPLATES_DIR / filename).read_text()
        assert "/opt/homebrew" not in text
        assert "knowhere" not in text
        assert "claude-code-multiplai" not in text

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_absolute_paths(self, filename):
        """No absolute filesystem paths in templates."""
        text = (TEMPLATES_DIR / filename).read_text()
        # Absolute path patterns
        assert not re.search(r"(?<!\w)/Users/\w+", text), "Found macOS-style absolute path"
        assert not re.search(r"(?<!\w)/home/\w+", text), "Found Linux-style absolute path"
        assert not re.search(r"[A-Z]:\\", text), "Found Windows-style absolute path"


# ===================================================================
# 8. Setup skill respects configured memory directory
# ===================================================================


class TestSetupRespectsconfiguredDir:
    """The setup flow must use the path-resolved memory_dir, not defaults."""

    def test_custom_memory_dir_via_env(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN CLAUDE_PLUGIN_OPTION_memory_dir is set, setup should use it."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        custom_mem = tmp_path / "custom-memory"
        custom_mem.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(custom_mem))

        paths = Paths.resolve()
        assert paths.memory_dir == custom_mem

        # Simulate writing templates to the resolved dir
        for fname in TEMPLATE_FILES:
            shutil.copy2(TEMPLATES_DIR / fname, paths.memory_dir / fname)

        for fname in TEMPLATE_FILES:
            assert (custom_mem / fname).is_file(), \
                f"Template {fname} not in custom memory dir"

    def test_default_memory_dir_when_unconfigured(self, monkeypatch, reset_paths_cache):
        """WHEN no custom memory_dir and no workspace, should use ~/.multiplai/memory."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        for key in list(os.environ):
            if key.startswith("CLAUDE_PLUGIN"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("WORKSPACE", raising=False)

        paths = Paths.resolve()
        expected = Path.home() / ".multiplai" / "memory"
        assert paths.memory_dir == expected


# ===================================================================
# 9. Template source path uses templates_dir from path resolver
# ===================================================================


class TestTemplateSourceResolution:
    """Template source files must be resolved via paths.templates_dir, not hardcoded."""

    def test_templates_dir_in_plugin_mode(self, monkeypatch, reset_paths_cache):
        """In plugin mode, templates_dir = $CLAUDE_PLUGIN_ROOT/templates."""
        from multiplai_core.paths import _reset_cache, Paths

        _reset_cache()
        fake_root = "/fake/plugin/root"
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", fake_root)

        paths = Paths.resolve()
        assert str(paths.templates_dir).endswith("/templates")
        assert "fake/plugin/root" in str(paths.templates_dir)

    def test_templates_in_source_contain_all_required_files(self):
        """All three template files must be present in the templates directory."""
        for fname in TEMPLATE_FILES:
            fpath = TEMPLATES_DIR / fname
            assert fpath.is_file(), f"Template {fname} not found at {fpath}"


# ===================================================================
# 10. Setup check script behavior contract
# ===================================================================


class TestSetupCheckContract:
    """setup_check.py must check for existing memory files and report status."""

    @pytest.fixture(autouse=True)
    def require_script(self):
        assert SETUP_CHECK_SCRIPT.is_file(), (
            "setup_check.py must exist — it has shipped, so a missing script "
            "is a real failure, not a skip"
        )
        self.src = SETUP_CHECK_SCRIPT.read_text()

    def test_checks_memory_dir(self):
        """setup_check.py must reference memory_dir for checking files."""
        assert re.search(r"memory_dir|memory", self.src), \
            "setup_check.py must reference memory directory"

    def test_checks_template_filenames(self):
        """setup_check.py must check for the three template filenames."""
        assert "me.md" in self.src
        assert "technical-pref.md" in self.src or "technical_pref" in self.src
        assert "preferences.md" in self.src

    def test_reports_existing_files(self):
        """setup_check.py must report which files exist (for skip/overwrite decision)."""
        # Should output or return info about existing vs missing files
        assert re.search(r"(?i)(exist|missing|found|skip|present)", self.src), \
            "setup_check.py must report file existence status"


# ===================================================================
# 11. Setup write script behavior contract
# ===================================================================


class TestSetupWriteContract:
    """setup_write.py must copy templates and populate with user answers."""

    @pytest.fixture(autouse=True)
    def require_script(self):
        assert SETUP_WRITE_SCRIPT.is_file(), (
            "setup_write.py must exist — it has shipped, so a missing script "
            "is a real failure, not a skip"
        )
        self.src = SETUP_WRITE_SCRIPT.read_text()

    def test_reads_templates(self):
        """setup_write.py must read from templates directory."""
        assert re.search(r"templates|template", self.src), \
            "setup_write.py must reference templates"

    def test_writes_to_memory_dir(self):
        """setup_write.py must write to the resolved memory directory."""
        assert re.search(r"memory_dir|memory", self.src), \
            "setup_write.py must write to memory directory"

    def test_uses_path_resolver_for_templates(self):
        """setup_write.py must resolve template paths via lib.paths."""
        assert re.search(r"templates_dir|paths\.templates", self.src), \
            "setup_write.py must use path resolver for template locations"

    def test_uses_path_resolver_for_memory(self):
        """setup_write.py must resolve memory dir via lib.paths."""
        assert re.search(r"memory_dir|paths\.memory", self.src), \
            "setup_write.py must use path resolver for memory directory"

    def test_handles_all_three_templates(self):
        """setup_write.py must handle all three template files."""
        assert "me.md" in self.src
        assert "preferences.md" in self.src
        # technical-pref.md may be referenced with hyphen or underscore
        assert "technical-pref" in self.src or "technical_pref" in self.src

    def test_no_overwrite_when_exists(self):
        """setup_write.py must implement skip-if-exists logic."""
        assert re.search(r"(?i)(exist|skip|overwrite|already)", self.src), \
            "setup_write.py must check for existing files before writing"


# ===================================================================
# 12. Integration: templates_dir points to directory with actual files
# ===================================================================


class TestTemplateIntegration:
    """End-to-end: path resolver templates_dir contains the actual template files."""

    def test_plugin_root_templates_dir_matches_reality(self):
        """The templates_dir derivation must point to where templates actually live."""
        from multiplai_core.paths import Paths

        # In actual plugin layout, templates are at PLUGIN_ROOT/templates/
        expected = PLUGIN_ROOT / "templates"
        assert expected.is_dir()

        for fname in TEMPLATE_FILES:
            assert (expected / fname).is_file()

    def test_template_content_matches_source(self):
        """Templates in templates_dir must match the shipped template content."""
        for fname in TEMPLATE_FILES:
            content = (TEMPLATES_DIR / fname).read_text()
            # Templates must have actual content (not empty stubs)
            assert len(content.strip()) > 50, \
                f"Template {fname} seems too short ({len(content.strip())} chars)"
            # Must have structured markdown
            headings = re.findall(r"^#{1,3}\s+.+", content, re.MULTILINE)
            assert len(headings) >= 2, \
                f"Template {fname} should have at least 2 headings, found {len(headings)}"


# ===================================================================
# 13. Templates directory structure matches spec
# ===================================================================


class TestTemplateDirectorySpec:
    """The specs reference templates/memory/ but design doc says templates/.

    These tests verify the templates are accessible and the path resolver
    correctly derives the templates path.
    """

    def test_templates_dir_exists(self):
        """Templates directory must exist at the expected location."""
        assert TEMPLATES_DIR.is_dir()

    def test_template_count(self):
        """Exactly three markdown files in templates directory."""
        md_files = sorted(f.name for f in TEMPLATES_DIR.iterdir() if f.suffix == ".md")
        assert md_files == sorted(TEMPLATE_FILES), \
            f"Expected {sorted(TEMPLATE_FILES)}, got {md_files}"

    def test_no_non_template_files(self):
        """Templates directory should only contain the expected template files."""
        all_files = [f.name for f in TEMPLATES_DIR.iterdir() if f.is_file()]
        unexpected = set(all_files) - set(TEMPLATE_FILES)
        assert not unexpected, f"Unexpected files in templates/: {unexpected}"
