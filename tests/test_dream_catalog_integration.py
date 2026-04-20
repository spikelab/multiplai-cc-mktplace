"""Tests for /dream integration with catalog regeneration.

Block 9: /dream integration and /refresh-catalogs skill.

Covers all scenarios from requirements/catalog-dream-integration.md:
- Dream lifecycle triggers catalog regeneration
- Autodream call chain includes catalog regeneration
- Catalog regeneration runs after diary write
- Dream skill markdown references catalog regeneration
- Catalog regeneration uses state-aware skipping during dream
- Dream-triggered regeneration does not block on optional catalogs
- Catalog regeneration inherits configured model settings
- Deleted sources are pruned during dream-triggered regeneration
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR


# ---------------------------------------------------------------------------
# dream.md contains catalog regeneration instructions
# ---------------------------------------------------------------------------


class TestDreamSkillContainsCatalogRegeneration:
    """Requirement: Dream skill markdown references catalog regeneration.

    dream.md must contain explicit instructions to invoke catalog
    regeneration after diary writing, so the LLM executing the skill
    knows to perform this step.
    """

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream.md").read_text()

    def test_dream_md_references_catalog_regeneration(self):
        """dream.md must contain instruction to invoke catalog regeneration."""
        # Should mention catalog generation/regeneration somewhere
        assert re.search(
            r"(?i)(catalog.*(regen|generat)|generate.*catalog|refresh.*catalog)",
            self.text,
        ), "dream.md must contain catalog regeneration instructions"

    def test_dream_md_references_generate_catalog_script(self):
        """dream.md must reference generate_catalog.py or the dispatcher."""
        assert re.search(
            r"(?i)(generate_catalog|catalog.*dispatch|refresh.?catalog)",
            self.text,
        ), "dream.md must reference the catalog generation entry point"

    def test_dream_md_catalog_step_after_diary(self):
        """Catalog regeneration step must appear after diary/consolidation step.

        The ordering within the markdown must place catalog regeneration
        after the diary/learnings consolidation to ensure the new diary
        entry is available for the diary catalog generator.
        """
        # Find position of consolidation/diary reference
        diary_match = re.search(r"(?i)(consolidat|synthesi|diary|learnings)", self.text)
        # Find position of catalog regeneration reference
        catalog_match = re.search(
            r"(?i)(catalog.*(regen|generat)|generate.*catalog)", self.text
        )
        assert diary_match is not None, "dream.md must reference diary/consolidation"
        assert catalog_match is not None, "dream.md must reference catalog regeneration"
        assert catalog_match.start() > diary_match.start(), (
            "Catalog regeneration must appear after diary/consolidation in dream.md "
            "to ensure ordering guarantee"
        )

    def test_dream_md_catalog_regen_section_exists(self):
        """dream.md should contain a <!-- catalog-regen --> section or equivalent.

        Per the block description, dream.md gets a <!-- catalog-regen -->
        section that calls the dispatcher.
        """
        # Check for HTML comment section marker or a dedicated section
        has_comment = "<!-- catalog-regen -->" in self.text
        has_section = re.search(
            r"(?i)##?\s+.*catalog", self.text
        )
        assert has_comment or has_section, (
            "dream.md must contain a <!-- catalog-regen --> section "
            "or a dedicated catalog regeneration heading"
        )

    def test_dream_md_catalog_error_handling(self):
        """dream.md must indicate that catalog errors don't fail the dream cycle."""
        assert re.search(
            r"(?i)(catalog.*(fail|error).*(continu|complet|not.*block|still)|"
            r"(error|fail).*(catalog).*(continu|complet|not.*block|still))",
            self.text,
        ), (
            "dream.md must indicate that catalog generation errors "
            "do not prevent the dream cycle from completing"
        )


# ---------------------------------------------------------------------------
# autodream.py triggers catalog regeneration
# ---------------------------------------------------------------------------


class TestAutodreamTriggersCatalogRegeneration:
    """Requirement: Autodream call chain includes catalog regeneration.

    autodream.py must trigger catalog regeneration after the dream
    consolidation phase completes, using the catalog dispatcher.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_autodream_imports_or_references_catalog_generation(self):
        """autodream.py must import or reference catalog generation."""
        has_import = re.search(
            r"(from\s+generators\.dispatcher\s+import|"
            r"import\s+generators\.dispatcher|"
            r"from\s+generators\s+import|"
            r"generate_catalog)",
            self.source,
        )
        assert has_import, (
            "autodream.py must import or reference the catalog dispatcher "
            "or generate_catalog module"
        )

    def test_autodream_calls_generate_catalogs(self):
        """autodream.py must call generate_catalogs() or equivalent."""
        has_call = re.search(
            r"generate_catalogs?\s*\(|catalog.*dispatch|run_catalog",
            self.source,
        )
        assert has_call, (
            "autodream.py must call generate_catalogs() or equivalent "
            "dispatcher function"
        )

    def test_autodream_catalog_generation_after_dream(self):
        """Catalog generation must come after dream consolidation in autodream.py.

        The code must call catalog generation after the dream() function
        or consolidation step completes, so the diary entry is written first.
        """
        # Find the dream consolidation call
        dream_call = re.search(r"dream\(\)|_update_memory_file|consolidat", self.source)
        # Find the catalog generation call
        catalog_call = re.search(
            r"generate_catalogs?\s*\(|catalog.*dispatch|run_catalog",
            self.source,
        )
        assert dream_call is not None, "autodream.py must have a dream/consolidation call"
        assert catalog_call is not None, "autodream.py must have a catalog generation call"
        assert catalog_call.start() > dream_call.start(), (
            "Catalog generation must come after dream consolidation in source order"
        )

    def test_autodream_catalog_generation_handles_errors(self):
        """autodream.py must catch catalog generation errors gracefully.

        Catalog generation failure must not prevent autodream from completing.
        """
        # Look for try/except around catalog generation
        has_error_handling = re.search(
            r"try:.*(?:generate_catalogs?|catalog).*except|"
            r"except.*(?:catalog|generat)",
            self.source,
            re.DOTALL,
        )
        assert has_error_handling, (
            "autodream.py must handle catalog generation errors gracefully "
            "so dream cycle completes even on catalog failure"
        )


class TestAutodreamCatalogGenerationBehavior:
    """Behavioral tests for autodream catalog regeneration integration.

    These tests mock the catalog dispatcher to verify that autodream
    correctly invokes catalog generation after the dream phase.
    """

    @pytest.fixture
    def mock_env(self, tmp_path, monkeypatch):
        """Set up mock environment for autodream tests."""
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

        # Create a learnings file so dream has work to do
        learnings = data_dir / "learnings.md"
        learnings.write_text("- Learned about testing\n- Learned about catalogs\n")

        # Create a memory file for dream to update
        (memory_dir / "technical-pref.md").write_text("# Technical Preferences\n")

        return {
            "data_dir": data_dir,
            "memory_dir": memory_dir,
            "diary_dir": diary_dir,
            "catalogs_dir": catalogs_dir,
        }

    def test_dream_triggers_catalog_generation(self, mock_env):
        """When autodream completes dream, it must invoke catalog generation.

        Scenario: Catalogs regenerate after a normal dream cycle.
        """
        # This test verifies the call chain by importing and patching
        with patch("generators.dispatcher.generate_catalogs") as mock_gen:
            mock_gen.return_value = []

            # Mock the model client so we don't make real LLM calls
            with patch("lib.model_client.create_client") as mock_client:
                mock_client.return_value = AsyncMock()
                mock_client.return_value.query = AsyncMock(
                    return_value=MagicMock(content="Updated content")
                )

                from lib.paths import _reset_cache
                _reset_cache()

                # Import and run autodream
                import importlib
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "autodream_test", SCRIPTS_DIR / "autodream.py"
                )
                mod = importlib.util.module_from_spec(spec)

                # The test should verify generate_catalogs is called
                # This will fail until autodream.py integrates catalog generation
                try:
                    spec.loader.exec_module(mod)
                    asyncio.run(mod.dream())
                except Exception:
                    pass  # Allow import/runtime errors

                mock_gen.assert_called(), (
                    "autodream dream() must call generate_catalogs() "
                    "after completing the consolidation phase"
                )

    def test_dream_completes_when_catalog_generation_fails(self, mock_env):
        """Dream cycle must complete even if catalog generation fails.

        Scenario: Dream completes even if catalog generation fails.
        The dream function must explicitly call generate_catalogs() and
        handle its failure gracefully, not just skip it.
        """
        with patch("generators.dispatcher.generate_catalogs") as mock_gen:
            mock_gen.side_effect = RuntimeError("LLM call failed")

            with patch("lib.model_client.create_client") as mock_client:
                mock_client.return_value = AsyncMock()
                mock_client.return_value.query = AsyncMock(
                    return_value=MagicMock(content="Updated content")
                )

                from lib.paths import _reset_cache
                _reset_cache()

                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "autodream_fail_test", SCRIPTS_DIR / "autodream.py"
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                # dream() should call generate_catalogs and NOT raise
                # even when catalog gen fails
                asyncio.run(mod.dream())

                # Verify generate_catalogs was actually called
                mock_gen.assert_called(), (
                    "dream() must call generate_catalogs() even in error test — "
                    "the integration must exist before we can test error handling"
                )


# ---------------------------------------------------------------------------
# Catalog regeneration ordering guarantee
# ---------------------------------------------------------------------------


class TestCatalogRegenerationOrdering:
    """Requirement: Catalog regeneration runs after diary write.

    Catalog generation must occur after the dream's diary entry has been
    written to disk, so the diary catalog generator can index the new entry.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_diary_write_before_catalog_generation(self):
        """The diary/memory write must complete before catalog generation starts.

        Scenario: Ordering guarantee on diary write before catalog generation.
        """
        # Find memory file write (the "diary write" in autodream is writing
        # updated memory files and persisting dream state)
        write_match = re.search(
            r"(write|save|open.*\"w\"|\.write_text|save_yaml.*dream_state)",
            self.source,
        )
        catalog_match = re.search(
            r"generate_catalogs?\s*\(|catalog.*dispatch|run_catalog",
            self.source,
        )
        assert write_match is not None, "autodream must have a file write operation"
        assert catalog_match is not None, "autodream must have catalog generation"
        assert catalog_match.start() > write_match.start(), (
            "Catalog generation must occur after diary/memory file writes "
            "to ensure new entries are indexed"
        )


# ---------------------------------------------------------------------------
# Catalog regeneration respects config
# ---------------------------------------------------------------------------


class TestDreamCatalogConfigRespected:
    """Requirement: Catalog regeneration inherits configured model settings.

    When triggered from dream, catalog generation must use the model and
    reasoning effort specified in plugin.json config.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_autodream_loads_catalog_config(self):
        """autodream.py must load catalog config for generation.

        Scenario: Custom model config is used during dream-triggered generation.
        """
        has_config_load = re.search(
            r"(CatalogConfig|load_catalog_config|CatalogGeneratorConfig)",
            self.source,
        )
        assert has_config_load, (
            "autodream.py must load catalog configuration "
            "so that generators use the configured model and effort"
        )

    def test_autodream_passes_config_to_generate_catalogs(self):
        """autodream.py must pass config to the catalog dispatcher.

        The dispatcher needs config to determine which generators run
        and what model/effort to use.
        """
        has_config_param = re.search(
            r"generate_catalogs?\s*\(\s*(?:config\s*=|.*CatalogConfig)",
            self.source,
        )
        assert has_config_param, (
            "autodream.py must pass CatalogConfig to generate_catalogs()"
        )


# ---------------------------------------------------------------------------
# State-aware skipping during dream
# ---------------------------------------------------------------------------


class TestDreamStateAwareSkipping:
    """Requirement: Catalog regeneration uses state-aware skipping during dream.

    During dream-triggered regeneration, unchanged sources should be
    skipped to keep dream execution fast.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_autodream_does_not_force_regeneration(self):
        """Dream-triggered catalog generation should NOT use force mode by default.

        Scenario: Unchanged catalogs are skipped.
        State-aware skipping means force=False (the default).
        First, generate_catalogs must be called at all, then verify no force=True.
        """
        # Must first have the generate_catalogs call
        has_call = re.search(
            r"generate_catalogs?\s*\(",
            self.source,
        )
        assert has_call, (
            "autodream.py must call generate_catalogs() before we can verify "
            "it doesn't use force=True"
        )
        # Then check it's NOT called with force=True
        force_call = re.search(
            r"generate_catalogs?\s*\([^)]*force\s*=\s*True",
            self.source,
        )
        assert not force_call, (
            "autodream.py must not pass force=True to generate_catalogs() — "
            "dream-triggered regeneration should use state-aware skipping"
        )

    def test_only_changed_catalogs_regenerated(self):
        """When dream writes a new diary entry but memory is unchanged,
        only the diary catalog should regenerate.

        Scenario: Only changed catalogs are regenerated.
        This is a behavioral property of the dispatcher, but autodream
        must not override it with force=True.
        """
        # Already tested by test_autodream_does_not_force_regeneration
        # This test verifies the contract from the autodream side
        assert re.search(
            r"generate_catalogs?\s*\(",
            self.source,
        ), "autodream must call generate_catalogs()"


# ---------------------------------------------------------------------------
# Dream-triggered pruning
# ---------------------------------------------------------------------------


class TestDreamTriggeredPruning:
    """Requirement: Deleted sources are pruned during dream-triggered regeneration.

    When dream triggers catalog regeneration, the dispatcher must prune
    catalog entries whose source files no longer exist.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_autodream_uses_standard_dispatcher(self):
        """autodream must use the standard dispatcher (which handles pruning).

        Scenario: Removed diary day file is pruned from catalog.
        Pruning is a dispatcher concern, but autodream must delegate to it
        rather than implementing its own catalog generation logic.
        """
        has_dispatcher = re.search(
            r"(generate_catalogs|from\s+generators\.dispatcher|"
            r"import.*dispatcher)",
            self.source,
        )
        assert has_dispatcher, (
            "autodream must use the standard catalog dispatcher "
            "which handles deletion pruning automatically"
        )


# ---------------------------------------------------------------------------
# Dream does not block on optional catalogs
# ---------------------------------------------------------------------------


class TestDreamDoesNotBlockOnOptionalCatalogs:
    """Requirement: Dream-triggered regeneration does not block on optional catalogs.

    If optional catalog generators (skills, resources) are enabled but slow,
    they should not prevent the dream cycle from completing.
    """

    @pytest.fixture(autouse=True)
    def load_source(self):
        self.source = (SCRIPTS_DIR / "autodream.py").read_text()

    def test_catalog_generation_not_blocking_dream_completion(self):
        """Dream cycle must complete regardless of catalog generation time.

        Scenario: Dream completes with slow optional generator.
        The dream function must not hang if a generator is slow.
        This is partially ensured by error handling and the sequential
        dispatcher design.
        """
        # Verify catalog generation has error handling/timeout protection
        has_protection = re.search(
            r"(try:.*(?:generate_catalogs?|catalog).*except|"
            r"asyncio\.wait_for.*catalog|"
            r"timeout.*catalog)",
            self.source,
            re.DOTALL,
        )
        assert has_protection, (
            "autodream must protect dream completion from slow catalog generation "
            "via error handling or timeout"
        )
