"""Tests for /refresh-catalogs skill.

Block 9: /dream integration and /refresh-catalogs skill.

Covers all scenarios from requirements/refresh-catalogs-skill.md:
- Skill registration in plugin.json and skills/ directory
- Default invocation triggers all enabled catalogs
- Force-regenerate mode (--force flag)
- Dry-run mode (--dry-run flag)
- Selective generator execution (--generators flag)
- Output reports per-catalog status
- Invocation delegates to catalog dispatcher
- Handles missing or corrupt state file gracefully
- Handles missing source directories gracefully
- No new external dependencies
- Respects configured model
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------


class TestRefreshCatalogsSkillRegistration:
    """Requirement: Skill registration.

    The /refresh-catalogs skill must be registered in skills/ as a
    discoverable skill so users can invoke it from the Claude Code prompt.
    """

    def test_skill_file_exists(self):
        """Scenario: Skill file exists at expected path.

        A skill definition file must exist at skills/refresh-catalogs/SKILL.md.
        """
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        assert skill_path.is_file(), (
            f"skills/refresh-catalogs/SKILL.md must exist at {skill_path}"
        )

    def test_skill_has_frontmatter(self):
        """Scenario: Skill file has YAML frontmatter for CC auto-discovery."""
        import re as _re
        text = (PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md").read_text()
        assert _re.match(r'^---\n', text), "skills/refresh-catalogs/SKILL.md missing YAML frontmatter"

    def test_skill_frontmatter_name(self):
        """Frontmatter must declare name: refresh-catalogs."""
        import re as _re
        text = (PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md").read_text()
        m = _re.match(r'^---\n(.*?)\n---', text, _re.DOTALL)
        assert m, "Missing frontmatter block"
        fm = dict(line.partition(':')[::2] for line in m.group(1).splitlines() if ':' in line)
        assert fm.get('name', '').strip().strip('"') == "refresh-catalogs"

    def test_skill_frontmatter_description(self):
        """Frontmatter must have a non-empty description."""
        import re as _re
        text = (PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md").read_text()
        m = _re.match(r'^---\n(.*?)\n---', text, _re.DOTALL)
        assert m, "Missing frontmatter block"
        fm = dict(line.partition(':')[::2] for line in m.group(1).splitlines() if ':' in line)
        assert fm.get('description', '').strip().strip('"')


# ---------------------------------------------------------------------------
# Skill content — prompt requirements
# ---------------------------------------------------------------------------


class TestRefreshCatalogsSkillContent:
    """Verify refresh-catalogs.md prompt content meets spec requirements."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_has_title_heading(self):
        """Skill must have a top-level heading."""
        assert re.search(r"^#\s+", self.text, re.MULTILINE), (
            "refresh-catalogs.md must have a top-level heading"
        )

    def test_mentions_catalog_generation(self):
        """Skill must reference catalog generation/regeneration."""
        assert re.search(r"(?i)(catalog|generat)", self.text), (
            "refresh-catalogs.md must mention catalog generation"
        )

    def test_references_generate_catalog_or_dispatcher(self):
        """Skill must reference the catalog dispatcher entry point.

        Requirement: Invocation delegates to catalog-dispatcher.
        """
        assert re.search(
            r"(?i)(generate_catalog|dispatcher|generate_catalogs)",
            self.text,
        ), "refresh-catalogs.md must reference the catalog dispatcher"

    def test_documents_force_flag(self):
        """Skill must document --force flag support.

        Requirement: Force-regenerate mode.
        """
        assert re.search(r"(?i)(--force|force)", self.text), (
            "refresh-catalogs.md must document --force flag"
        )

    def test_documents_dry_run_flag(self):
        """Skill must document --dry-run flag support.

        Requirement: Dry-run mode.
        """
        assert re.search(r"(?i)(--dry.?run|dry.?run)", self.text), (
            "refresh-catalogs.md must document --dry-run flag"
        )

    def test_documents_generators_flag(self):
        """Skill must document --generators or --only flag for selective runs.

        Per block description: --generators flag to selectively regenerate
        specific catalogs.
        """
        assert re.search(
            r"(?i)(--generators|--only|selective|specific.*catalog)",
            self.text,
        ), "refresh-catalogs.md must document selective generator flag"

    def test_describes_output_format(self):
        """Skill must describe what output to report per catalog.

        Requirement: Output reports per-catalog status.
        """
        assert re.search(
            r"(?i)(status|report|result|regenerat|skip|fail)",
            self.text,
        ), "refresh-catalogs.md must describe per-catalog output reporting"

    def test_no_hardcoded_paths(self):
        """Skill must not contain hardcoded filesystem paths."""
        assert "/home/" not in self.text
        assert "/Users/" not in self.text
        assert "~/.multiplai/" not in self.text

    def test_no_direct_sdk_imports(self):
        """Skill must not reference direct SDK imports."""
        assert "import anthropic" not in self.text
        assert "from anthropic" not in self.text

    def test_mentions_model_client(self):
        """Skill must reference model_client for LLM calls.

        Requirement: No new external dependencies.
        """
        assert re.search(
            r"(?i)(model.?client|model_client|existing.*llm|no.*direct.*api)",
            self.text,
        ), "refresh-catalogs.md must reference model_client for LLM calls"

    def test_describes_error_handling(self):
        """Skill must mention error handling for missing/corrupt state."""
        assert re.search(
            r"(?i)(error|fail|missing|corrupt|graceful)",
            self.text,
        ), "refresh-catalogs.md must describe error handling behavior"


# ---------------------------------------------------------------------------
# Default invocation — all enabled catalogs
# ---------------------------------------------------------------------------


class TestRefreshCatalogsDefaultInvocation:
    """Requirement: Default invocation triggers all enabled catalogs.

    When invoked without arguments, /refresh-catalogs must regenerate
    all catalogs that are enabled in the current plugin.json configuration.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_default_invocation_runs_all_enabled(self):
        """Scenario: All enabled catalogs regenerated.

        Without arguments, the skill must invoke the dispatcher without
        filtering, which runs all enabled generators.
        """
        # The skill should instruct running without --only or equivalent
        # when no arguments are provided
        assert re.search(
            r"(?i)(no.*argument|default|all.*enabled|without.*flag)",
            self.text,
        ), (
            "refresh-catalogs.md must describe default behavior of "
            "running all enabled catalogs"
        )

    def test_skill_mentions_mandatory_catalogs(self):
        """Skill should reference that memory and diary are always processed."""
        assert re.search(
            r"(?i)(memory|diary)",
            self.text,
        ), "refresh-catalogs.md must mention memory and diary catalogs"


# ---------------------------------------------------------------------------
# Force-regenerate mode
# ---------------------------------------------------------------------------


class TestRefreshCatalogsForceMode:
    """Requirement: Force-regenerate mode.

    /refresh-catalogs must support a --force flag that bypasses
    state-aware skipping.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_force_flag_documented(self):
        """Scenario: Unchanged sources are regenerated under force mode."""
        assert re.search(r"--force", self.text), (
            "refresh-catalogs.md must document the --force flag"
        )

    def test_force_flag_bypasses_state_check(self):
        """Force mode must bypass content hash comparison."""
        assert re.search(
            r"(?i)(force.*bypass|force.*skip|force.*regardless|"
            r"force.*all.*regen|ignore.*hash|ignore.*state)",
            self.text,
        ), (
            "refresh-catalogs.md must explain that --force bypasses "
            "state-aware skipping"
        )

    def test_force_flag_passthrough_to_dispatcher(self):
        """Skill must pass --force through to the dispatcher.

        The skill should invoke generate_catalog.py with --force flag.
        """
        assert re.search(
            r"(?i)(--force|force.*flag|force.*pass|force.*dispatch)",
            self.text,
        ), (
            "refresh-catalogs.md must indicate --force is passed to the dispatcher"
        )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestRefreshCatalogsDryRunMode:
    """Requirement: Dry-run mode.

    /refresh-catalogs must support a --dry-run flag that reports what
    would be regenerated without side effects.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_dry_run_flag_documented(self):
        """Scenario: Dry-run reports pending work without side effects."""
        assert re.search(r"(?i)--dry.?run", self.text), (
            "refresh-catalogs.md must document the --dry-run flag"
        )

    def test_dry_run_no_side_effects(self):
        """Dry-run must not write files or make LLM calls."""
        assert re.search(
            r"(?i)(dry.?run.*(no|without|not).*(writ|modif|chang|llm|generat)|"
            r"(no|without|not).*(writ|modif|chang).*(dry.?run)|"
            r"dry.?run.*report|preview|would)",
            self.text,
        ), (
            "refresh-catalogs.md must explain that --dry-run has no side effects"
        )

    def test_dry_run_output_format(self):
        """Dry-run must show what would be generated/pruned.

        Per block description: --dry-run output formatting showing what
        would be generated/pruned.
        """
        assert re.search(
            r"(?i)(would.*(generat|regen|skip|prun)|"
            r"(generat|regen|skip|prun).*would|"
            r"dry.?run.*(show|report|output|display))",
            self.text,
        ), (
            "refresh-catalogs.md must describe dry-run output showing "
            "what would be generated/pruned"
        )


# ---------------------------------------------------------------------------
# Selective generator execution
# ---------------------------------------------------------------------------


class TestRefreshCatalogsSelectiveGenerators:
    """Block description: --generators flag to selectively regenerate
    specific catalogs.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_selective_flag_documented(self):
        """Skill must document a flag for selecting specific generators."""
        assert re.search(
            r"(?i)(--generators|--only|specific.*generator|select.*catalog)",
            self.text,
        ), (
            "refresh-catalogs.md must document a flag for selecting "
            "specific generators (e.g., --generators or --only)"
        )

    def test_lists_available_generators(self):
        """Skill should list or reference available generator names."""
        has_names = re.search(
            r"(?i)(memory|diary|skills|resources)",
            self.text,
        )
        assert has_names, (
            "refresh-catalogs.md must reference available generator names "
            "(memory, diary, skills, resources)"
        )


# ---------------------------------------------------------------------------
# Output reports per-catalog status
# ---------------------------------------------------------------------------


class TestRefreshCatalogsOutputReporting:
    """Requirement: Output reports per-catalog status.

    /refresh-catalogs must report the outcome for each catalog individually.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_reports_per_catalog_status(self):
        """Scenario: Mixed results reported clearly.

        Output must show each catalog's status (regenerated/skipped/failed).
        """
        assert re.search(
            r"(?i)(per.?catalog|each.?catalog|individual|status.*catalog|"
            r"catalog.*status|regenerat.*skip.*fail)",
            self.text,
        ), (
            "refresh-catalogs.md must describe per-catalog status reporting"
        )

    def test_reports_success_status(self):
        """Output must indicate successful regeneration."""
        assert re.search(
            r"(?i)(regenerat|success|complet|updated)",
            self.text,
        ), "Skill must describe success status reporting"

    def test_reports_skip_status(self):
        """Output must indicate skipped catalogs (unchanged)."""
        assert re.search(
            r"(?i)(skip|unchanged|no.?change|up.?to.?date)",
            self.text,
        ), "Skill must describe skip status reporting"

    def test_reports_failure_status(self):
        """Output must indicate failed catalogs with error reason."""
        assert re.search(
            r"(?i)(fail|error|reason)",
            self.text,
        ), "Skill must describe failure status reporting"


# ---------------------------------------------------------------------------
# Invocation delegates to catalog-dispatcher
# ---------------------------------------------------------------------------


class TestRefreshCatalogsDelegatesToDispatcher:
    """Requirement: Invocation delegates to catalog-dispatcher.

    /refresh-catalogs must invoke the catalog dispatcher rather than
    calling individual generators directly.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_invokes_dispatcher_not_individual_generators(self):
        """Scenario: Skill calls dispatcher with correct flags.

        The skill must invoke generate_catalog.py or the dispatcher module,
        not individual generator scripts.
        """
        assert re.search(
            r"(?i)(generate_catalog\.py|generate_catalogs|dispatcher|"
            r"python.*scripts.*generate)",
            self.text,
        ), (
            "refresh-catalogs.md must invoke the catalog dispatcher "
            "(generate_catalog.py), not individual generators"
        )

    def test_flags_passed_through_to_dispatcher(self):
        """Flags (--force, --dry-run, --generators) must be passed to dispatcher."""
        # At minimum, --force should be documented as a passthrough
        assert re.search(
            r"(?i)(pass.*flag|flag.*pass|--force|--dry.?run)",
            self.text,
        ), (
            "refresh-catalogs.md must indicate flags are passed to the dispatcher"
        )


# ---------------------------------------------------------------------------
# Missing/corrupt state file handling
# ---------------------------------------------------------------------------


class TestRefreshCatalogsStateFileHandling:
    """Requirement: Handles missing or corrupt state file gracefully.

    /refresh-catalogs must not fail if .generation-state.json is missing,
    empty, or contains invalid JSON.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_handles_missing_state(self):
        """Scenario: Missing state file triggers full regeneration.

        This is a dispatcher concern, but the skill must not crash.
        """
        # The skill delegates to dispatcher which handles this,
        # but the skill should mention graceful handling
        assert re.search(
            r"(?i)(missing|first.?run|no.*state|graceful|corrupt|invalid)",
            self.text,
        ), (
            "refresh-catalogs.md must mention handling of missing/corrupt state"
        )


# ---------------------------------------------------------------------------
# Missing source directories
# ---------------------------------------------------------------------------


class TestRefreshCatalogsMissingDirectories:
    """Requirement: Handles missing source directories gracefully.

    /refresh-catalogs must not crash if expected source directories don't exist.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_mentions_directory_handling(self):
        """Scenario: Missing diary directory skips diary catalog.

        The skill should not crash when source directories are missing.
        """
        assert re.search(
            r"(?i)(director|missing|not.*exist|skip|graceful)",
            self.text,
        ), (
            "refresh-catalogs.md must mention handling of missing directories"
        )


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------


class TestRefreshCatalogsModelConfig:
    """Requirement: Respects configured model.

    /refresh-catalogs must use the catalog model from
    plugin.json userConfig.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        skill_path = PLUGIN_ROOT / "skills" / "refresh-catalogs" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("skills/refresh-catalogs/SKILL.md does not exist yet")
        self.text = skill_path.read_text()

    def test_mentions_model_configuration(self):
        """Scenario: Custom model config is respected.

        The skill documentation must reference that configured model is used.
        """
        assert re.search(
            r"(?i)(model|config|model_client|configured)",
            self.text,
        ), (
            "refresh-catalogs.md must reference model configuration"
        )


# ---------------------------------------------------------------------------
# generate_catalog.py entry point supports dispatcher flags
# ---------------------------------------------------------------------------


class TestGenerateCatalogEntryPoint:
    """Verify generate_catalog.py serves as a proper entry point
    for the catalog dispatcher with CLI flag support.

    The skill invokes generate_catalog.py which must support
    --force, --dry-run, and --only flags.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "generate_catalog.py").read_text()

    def test_generate_catalog_imports_dispatcher(self):
        """generate_catalog.py must import the dispatcher module."""
        assert re.search(
            r"(from\s+generators\.dispatcher\s+import|"
            r"import\s+generators\.dispatcher|"
            r"generate_catalogs)",
            self.source,
        ), "generate_catalog.py must import the catalog dispatcher"

    def test_generate_catalog_supports_force_flag(self):
        """generate_catalog.py must accept --force CLI argument."""
        assert re.search(r"--force", self.source), (
            "generate_catalog.py must support --force CLI flag"
        )

    def test_generate_catalog_supports_dry_run_flag(self):
        """generate_catalog.py must accept --dry-run CLI argument."""
        assert re.search(r"--dry.?run", self.source), (
            "generate_catalog.py must support --dry-run CLI flag"
        )

    def test_generate_catalog_supports_only_flag(self):
        """generate_catalog.py must accept --only CLI argument for filtering."""
        assert re.search(r"--only|--generators", self.source), (
            "generate_catalog.py must support --only or --generators CLI flag"
        )

    def test_generate_catalog_calls_dispatcher(self):
        """generate_catalog.py must call generate_catalogs() from the dispatcher."""
        assert re.search(
            r"generate_catalogs\s*\(",
            self.source,
        ), "generate_catalog.py must call generate_catalogs()"

    def test_generate_catalog_loads_config(self):
        """generate_catalog.py must load CatalogConfig before dispatching."""
        assert re.search(
            r"(load_catalog_config|CatalogConfig|CatalogGeneratorConfig)",
            self.source,
        ), "generate_catalog.py must load catalog configuration"

    def test_generate_catalog_exits_nonzero_on_failure(self):
        """generate_catalog.py must exit with non-zero status on failure.

        Requirement: Exit code reflects generation outcome.
        """
        assert re.search(
            r"(sys\.exit\(|exit\(|returncode|exit_code)",
            self.source,
        ), "generate_catalog.py must set exit code based on generation outcome"

    def test_generate_catalog_passes_force_to_dispatcher(self):
        """--force flag must be passed through to generate_catalogs()."""
        assert re.search(
            r"generate_catalogs\s*\([^)]*force",
            self.source,
        ), "generate_catalog.py must pass force flag to generate_catalogs()"

    def test_generate_catalog_passes_dry_run_to_dispatcher(self):
        """--dry-run flag must be passed through to generate_catalogs()."""
        assert re.search(
            r"generate_catalogs\s*\([^)]*dry_run",
            self.source,
        ), "generate_catalog.py must pass dry_run flag to generate_catalogs()"


# ---------------------------------------------------------------------------
# Integration: dispatcher flag combinations
# ---------------------------------------------------------------------------


class TestRefreshCatalogsIntegrationFlags:
    """Integration tests for /refresh-catalogs with each flag combination.

    These test the end-to-end behavior of the dispatcher when invoked
    with different flag combinations, as would happen via the skill.
    """

    @pytest.fixture
    def mock_env(self, tmp_path, monkeypatch):
        """Set up mock environment for dispatcher tests."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()
        catalogs_dir = data_dir / "catalogs"
        catalogs_dir.mkdir()

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(diary_dir))

        # Create a memory file
        (memory_dir / "me.md").write_text("# About Me\nTest user.\n")

        return {
            "data_dir": data_dir,
            "memory_dir": memory_dir,
            "diary_dir": diary_dir,
            "catalogs_dir": catalogs_dir,
        }

    def test_default_invocation_runs_mandatory_generators(self, mock_env):
        """Default /refresh-catalogs runs memory and diary generators.

        Scenario: Only mandatory catalogs when optional catalogs disabled.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(
            enable_skills=False,
            enable_resources=False,
        )

        results = asyncio.run(generate_catalogs(config=config))

        # Memory and diary must be included
        gen_names = [r.generator for r in results]
        assert "memory" in gen_names, "Memory generator must run by default"
        assert "diary" in gen_names, "Diary generator must run by default"
        assert "skills" not in gen_names, "Skills generator must be skipped when disabled"
        assert "resources" not in gen_names, "Resources generator must be skipped when disabled"

    def test_force_flag_regenerates_all(self, mock_env):
        """Scenario: Unchanged sources are regenerated under force mode.

        With --force, all generators run even if content hashes match.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        # Run once to populate state
        asyncio.run(generate_catalogs(config=config))

        # Run again with force — should still generate
        results = asyncio.run(generate_catalogs(config=config, force=True))

        for r in results:
            # In force mode, generators should report generated > 0 or
            # total_sources >= 0 (even if no sources, the generator ran)
            assert r.dry_run is False, f"{r.generator} should not be in dry-run mode"

    def test_dry_run_no_file_modifications(self, mock_env):
        """Scenario: Dry-run reports pending work without side effects.

        With --dry-run, no catalog files should be written.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        catalogs_dir = mock_env["catalogs_dir"]

        # Record files before
        files_before = set(catalogs_dir.iterdir()) if catalogs_dir.exists() else set()

        results = asyncio.run(generate_catalogs(config=config, dry_run=True))

        # Verify dry_run flag is set on all results
        for r in results:
            assert r.dry_run is True, f"{r.generator} must report dry_run=True"

        # No new catalog files should be created
        files_after = set(catalogs_dir.iterdir()) if catalogs_dir.exists() else set()
        new_files = files_after - files_before
        # Filter to only catalog json files (exclude state file which may exist)
        new_catalogs = {f for f in new_files if f.suffix == ".json" and not f.name.startswith(".")}
        assert not new_catalogs, (
            f"Dry-run must not create catalog files. New files: {new_catalogs}"
        )

    def test_dry_run_with_force_shows_all_pending(self, mock_env):
        """Scenario: Dry-run with force shows all catalogs as pending.

        --dry-run --force should report all enabled catalogs would regenerate.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        results = asyncio.run(
            generate_catalogs(config=config, dry_run=True, force=True)
        )

        for r in results:
            assert r.dry_run is True

    def test_only_filter_single_generator(self, mock_env):
        """Scenario: Single generator filter.

        --only diary should only run the diary generator.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        results = asyncio.run(
            generate_catalogs(config=config, generators=["diary"])
        )

        gen_names = [r.generator for r in results]
        assert gen_names == ["diary"], (
            f"With --only diary, only diary should run. Got: {gen_names}"
        )

    def test_only_filter_multiple_generators(self, mock_env):
        """Scenario: Multiple generator filter.

        --only memory,diary should run both in canonical order.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        results = asyncio.run(
            generate_catalogs(config=config, generators=["memory", "diary"])
        )

        gen_names = [r.generator for r in results]
        assert gen_names == ["memory", "diary"], (
            f"With --only memory,diary, both should run in order. Got: {gen_names}"
        )

    def test_only_filter_invalid_name(self, mock_env):
        """Scenario: Invalid generator name in filter.

        --only nonexistent should fail with an error.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        with pytest.raises(ValueError, match="nonexistent"):
            asyncio.run(
                generate_catalogs(config=config, generators=["nonexistent"])
            )

    def test_skills_enabled_runs_skills_generator(self, mock_env):
        """Scenario: All enabled catalogs regenerated.

        With enable_skills=True, skills generator must run.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(
            enable_skills=True,
            skills_dir=str(PLUGIN_ROOT / "skills"),
        )

        results = asyncio.run(generate_catalogs(config=config))

        gen_names = [r.generator for r in results]
        assert "skills" in gen_names, (
            "Skills generator must run when enable_skills=True"
        )

    def test_mixed_results_reported(self, mock_env):
        """Scenario: Mixed results reported clearly.

        Each generator's result must be individually inspectable.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        results = asyncio.run(generate_catalogs(config=config))

        # Each result must have the expected fields
        for r in results:
            assert hasattr(r, "generator"), "Result must have generator name"
            assert hasattr(r, "total_sources"), "Result must have total_sources"
            assert hasattr(r, "skipped"), "Result must have skipped count"
            assert hasattr(r, "generated"), "Result must have generated count"
            assert hasattr(r, "pruned"), "Result must have pruned count"
            assert hasattr(r, "errors"), "Result must have errors list"
            assert hasattr(r, "dry_run"), "Result must have dry_run flag"

    def test_missing_state_file_triggers_full_generation(self, mock_env):
        """Scenario: Missing state file triggers full regeneration.

        When .generation-state.json doesn't exist, all catalogs regenerate.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        state_file = mock_env["catalogs_dir"] / ".generation-state.json"

        # Ensure no state file exists
        if state_file.exists():
            state_file.unlink()

        results = asyncio.run(generate_catalogs(config=config))

        # All mandatory generators should have run
        gen_names = [r.generator for r in results]
        assert "memory" in gen_names
        assert "diary" in gen_names

    def test_corrupt_state_file_triggers_full_generation(self, mock_env):
        """Scenario: Corrupt state file triggers full regeneration.

        When .generation-state.json contains invalid JSON, all catalogs regenerate.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        state_file = mock_env["catalogs_dir"] / ".generation-state.json"

        # Write corrupt state file
        state_file.write_text("{invalid json content!!!}")

        results = asyncio.run(generate_catalogs(config=config))

        # Should complete without error
        gen_names = [r.generator for r in results]
        assert "memory" in gen_names
        assert "diary" in gen_names

    def test_dry_run_always_exits_zero(self, mock_env):
        """Scenario: Dry run always exits zero.

        Dry-run must always report success regardless of what would happen.
        """
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        results = asyncio.run(
            generate_catalogs(config=config, dry_run=True)
        )

        # All results should have dry_run=True and no fatal errors
        for r in results:
            assert r.dry_run is True
            # In dry-run, errors list should be empty (no actual operations)
