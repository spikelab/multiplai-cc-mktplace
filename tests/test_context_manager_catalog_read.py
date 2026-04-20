"""Tests for context_manager.py catalog-first read paths (Block 8).

Covers all scenarios from requirements/context-manager-catalog-read.md:
- _read_catalog_or_scan() with try/catalog-first, catch/fallback-to-scan logic
- Catalog read paths for memory, diary, skills, and resources
- Fail-open fallback for missing mandatory catalogs (memory, diary)
- Fail-open fallback for corrupt mandatory catalogs (invalid JSON)
- Schema version checking — reject catalogs with unknown schema versions
- Once-per-session warning logging for catalog failures
- Empty catalog treated as authoritative (no fallback)
- Optional catalogs (skills, resources) skip silently when missing
- Catalog read errors isolated per catalog type
- Catalog read does not block on stale data (TTL is read-side irrelevant)
- Catalog read path transparent to callers (same return shape)

Design Decision 8: Fail-open strategy — missing or corrupt catalogs
degrade to live file scanning rather than breaking context assembly.
"""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generators.base import CATALOG_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogs_dir(tmp_path):
    """Create a temporary catalogs directory."""
    d = tmp_path / "catalogs"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def plugin_data_env(tmp_path, monkeypatch):
    """Set CLAUDE_PLUGIN_DATA to a temp directory and return its catalogs dir."""
    data_dir = tmp_path / "plugin_data"
    data_dir.mkdir(parents=True)
    catalogs_dir = data_dir / "catalogs"
    catalogs_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    # Reset the paths cache so it picks up the new env
    from lib.paths import _reset_cache
    _reset_cache()
    yield catalogs_dir
    _reset_cache()


def _write_catalog(catalogs_dir, filename, data):
    """Write a catalog JSON file to the catalogs directory."""
    (catalogs_dir / filename).write_text(json.dumps(data, indent=2))


def _valid_memory_catalog(entries=None):
    """Create a valid memory catalog dict with correct schema version."""
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-04-19T10:30:00Z",
        "entries": entries or [
            {
                "source": "me.md",
                "summary": "Identity and background info",
                "topics": ["identity", "background"],
                "sections": ["identity"],
                "bundle": "core",
                "co_retrieve_for": ["diary"],
            },
            {
                "source": "technical-pref.md",
                "summary": "Technical preferences and patterns",
                "topics": ["architecture", "testing"],
            },
        ],
    }


def _valid_diary_catalog(entries=None):
    """Create a valid diary catalog dict with correct schema version."""
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-04-19T22:00:00Z",
        "entries": entries or [
            {
                "date": "2026-04-19",
                "sessions": [{"id": "session-abc", "project": "multiplai", "summary": "Catalog work"}],
                "topics": ["catalog-generation", "context-routing"],
                "projects": ["multiplai"],
                "word_count": 2340,
            },
            {
                "date": "2026-04-18",
                "sessions": [{"id": "session-def", "project": "dotfiles", "summary": "Hook fixes"}],
                "topics": ["hooks", "debugging"],
                "projects": ["dotfiles"],
                "word_count": 1200,
            },
        ],
    }


def _valid_skills_catalog(entries=None):
    """Create a valid skills catalog dict with correct schema version."""
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-04-19T10:30:00Z",
        "entries": entries or [
            {
                "name": "dream",
                "file": "dream.md",
                "summary": "Trigger reflection and diary writing",
                "triggers": ["dream", "reflect", "diary"],
                "content_hash": "abc123",
            },
        ],
    }


def _valid_resources_catalog(entries=None):
    """Create a valid resources catalog dict with correct schema version."""
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-04-19T10:30:00Z",
        "entries": entries or [
            {
                "path": "apis/openai.md",
                "summary": "OpenAI API integration docs",
                "content_hash": "def456",
            },
        ],
    }


# ---------------------------------------------------------------------------
# _read_catalog_or_scan() — Interface and Existence
# ---------------------------------------------------------------------------


class TestReadCatalogOrScanInterface:
    """Requirement: _read_catalog_or_scan() exists and has the correct signature.

    The context manager MUST have a _read_catalog_or_scan() method that
    accepts a catalog_type string and returns a list of context entries.
    """

    def test_function_exists_in_context_manager(self):
        """_read_catalog_or_scan must exist in context_manager module."""
        import context_manager

        assert hasattr(context_manager, "_read_catalog_or_scan"), (
            "context_manager must expose _read_catalog_or_scan"
        )

    def test_function_accepts_catalog_type_string(self):
        """_read_catalog_or_scan must accept a catalog_type string argument."""
        import context_manager

        # Should not raise TypeError for a string argument
        result = context_manager._read_catalog_or_scan("memory")
        assert isinstance(result, list), "_read_catalog_or_scan must return a list"

    def test_function_returns_list(self):
        """_read_catalog_or_scan must return a list."""
        import context_manager

        result = context_manager._read_catalog_or_scan("diary")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Catalog Hit — Memory
# ---------------------------------------------------------------------------


class TestMemoryCatalogHit:
    """Requirement: Catalog-first read path for memory context.

    When a valid memory catalog exists, the context manager reads context
    from the catalog file and does NOT scan individual memory source files.
    """

    def test_reads_from_valid_memory_catalog(self, plugin_data_env):
        """Scenario: Memory catalog exists and is valid.

        WHEN memory-catalog.json exists with valid schema version
        THEN context manager reads from catalog, not source files.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert len(result) > 0, (
            "Valid memory catalog should return non-empty entries"
        )

    def test_memory_catalog_entries_contain_expected_fields(self, plugin_data_env):
        """Scenario: Memory catalog is used for routing decisions.

        WHEN context manager reads a valid memory catalog containing
        section metadata, bundle info, and co_retrieve_for fields
        THEN those fields are available in the returned entries.
        """
        import context_manager

        entries = [
            {
                "source": "me.md",
                "summary": "Identity info",
                "sections": ["identity"],
                "bundle": "core",
                "co_retrieve_for": ["diary"],
            }
        ]
        catalog = _valid_memory_catalog(entries=entries)
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert len(result) >= 1
        # The entry should preserve catalog metadata fields
        entry = result[0] if isinstance(result[0], dict) else result[0]
        if isinstance(entry, dict):
            assert "summary" in entry or "source" in entry, (
                "Memory catalog entry must contain routing-relevant fields"
            )

    def test_memory_catalog_does_not_scan_source_files(self, plugin_data_env, tmp_path, monkeypatch):
        """When valid memory catalog exists, source files must NOT be scanned.

        This verifies the catalog-first optimization: no raw file I/O for
        memory source files when the catalog is available.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        # Create a memory dir that would be scanned in fallback mode
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("should not be read")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        # Track if source files were read
        original_read_text = Path.read_text
        source_files_read = []

        def tracking_read_text(self, *args, **kwargs):
            if str(memory_dir) in str(self):
                source_files_read.append(str(self))
            return original_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", tracking_read_text):
            context_manager._read_catalog_or_scan("memory")

        # Source files should NOT have been read (catalog was used instead)
        # Note: This test will fail until catalog-first logic is implemented
        assert len(source_files_read) == 0, (
            "Source memory files should not be read when valid catalog exists"
        )


# ---------------------------------------------------------------------------
# Catalog Hit — Diary
# ---------------------------------------------------------------------------


class TestDiaryCatalogHit:
    """Requirement: Catalog-first read path for diary context.

    When a valid diary catalog exists, the context manager uses per-day
    summaries to select relevant days without reading raw day files.
    """

    def test_reads_from_valid_diary_catalog(self, plugin_data_env):
        """Scenario: Diary catalog exists and is valid.

        WHEN diary-catalog.json exists with valid schema version
        THEN context manager reads per-day summaries from the catalog.
        """
        import context_manager

        catalog = _valid_diary_catalog()
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        assert len(result) > 0, (
            "Valid diary catalog should return non-empty entries"
        )

    def test_diary_catalog_entries_have_date_field(self, plugin_data_env):
        """Diary catalog entries must have date fields for day selection."""
        import context_manager

        catalog = _valid_diary_catalog()
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        assert len(result) >= 1
        if isinstance(result[0], dict):
            assert "date" in result[0], (
                "Diary catalog entry must contain 'date' field"
            )

    def test_diary_catalog_enables_selective_day_loading(self, plugin_data_env):
        """Scenario: Diary catalog enables selective day loading.

        WHEN diary catalog has 30 entries and only 3 match criteria
        THEN only those 3 matching days would be loaded from raw files.
        """
        import context_manager

        # Create a catalog with many days
        entries = [
            {
                "date": f"2026-03-{i:02d}",
                "sessions": [{"id": f"s-{i}", "project": "proj-a", "summary": f"Day {i}"}],
                "topics": ["topic-a"],
                "projects": ["proj-a"],
                "word_count": 500,
            }
            for i in range(1, 31)
        ]
        catalog = _valid_diary_catalog(entries=entries)
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        # The catalog should provide all 30 entries for the caller to filter
        assert len(result) == 30, (
            "Diary catalog should return all entries for caller to filter"
        )


# ---------------------------------------------------------------------------
# Catalog Hit — Skills
# ---------------------------------------------------------------------------


class TestSkillsCatalogHit:
    """Requirement: Catalog-first read path for skills context.

    When enable_skills is true and skills catalog exists, use catalog
    for routing instead of scanning skill files directly.
    """

    def test_reads_from_valid_skills_catalog(self, plugin_data_env, monkeypatch):
        """Scenario: Skills catalog exists and skills are enabled.

        WHEN enable_skills is true and skills-catalog.json exists
        THEN context manager reads skill metadata from catalog.
        """
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")

        catalog = _valid_skills_catalog()
        _write_catalog(plugin_data_env, "skills-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("skills")

        assert len(result) > 0, (
            "Valid skills catalog with skills enabled should return entries"
        )

    def test_skills_catalog_fallback_when_missing(self, plugin_data_env, monkeypatch):
        """Scenario: Skills catalog does not exist but skills are enabled.

        WHEN enable_skills is true and the skills catalog file does not exist
        THEN falls back to live scanning of skill files (fail-open behavior).
        """
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")
        # No skills-catalog.json created

        # Should not raise — should fall back gracefully
        result = context_manager._read_catalog_or_scan("skills")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Catalog Hit — Resources
# ---------------------------------------------------------------------------


class TestResourcesCatalogHit:
    """Requirement: Catalog-first read path for resources context.

    When both enable_resources and resources_dir are set and catalog exists,
    use catalog for routing.
    """

    def test_reads_from_valid_resources_catalog(self, plugin_data_env, monkeypatch, tmp_path):
        """Scenario: Resources catalog exists and resources are enabled.

        WHEN enable_resources is true, resources_dir is configured,
        and resources-catalog.json exists with valid schema version
        THEN context manager reads resource metadata from catalog.
        """
        import context_manager

        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_resources", "true")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_resources_dir", str(resources_dir))

        catalog = _valid_resources_catalog()
        _write_catalog(plugin_data_env, "resources-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("resources")

        assert len(result) > 0, (
            "Valid resources catalog with resources enabled should return entries"
        )


# ---------------------------------------------------------------------------
# Fail-Open Fallback — Missing Mandatory Catalog
# ---------------------------------------------------------------------------


class TestFailOpenMissingMandatoryCatalog:
    """Requirement: Fail-open fallback when mandatory catalog is missing.

    When memory or diary catalog is missing entirely, context manager MUST
    fall back to live file scanning rather than raising an error.
    """

    def test_memory_catalog_missing_falls_back(self, plugin_data_env):
        """Scenario: Memory catalog file does not exist.

        WHEN memory-catalog.json does not exist
        THEN falls back to scanning individual memory source files.
        """
        import context_manager

        # No memory-catalog.json created in catalogs_dir
        result = context_manager._read_catalog_or_scan("memory")

        # Should return a list (possibly empty from fallback scan)
        assert isinstance(result, list), (
            "Missing memory catalog must fall back, not raise"
        )

    def test_diary_catalog_missing_falls_back(self, plugin_data_env):
        """Scenario: Diary catalog file does not exist.

        WHEN diary-catalog.json does not exist
        THEN falls back to scanning individual diary day files.
        """
        import context_manager

        result = context_manager._read_catalog_or_scan("diary")

        assert isinstance(result, list), (
            "Missing diary catalog must fall back, not raise"
        )

    def test_missing_catalog_does_not_raise(self, plugin_data_env):
        """Missing mandatory catalog must never raise an unhandled exception."""
        import context_manager

        # Neither catalog exists
        try:
            context_manager._read_catalog_or_scan("memory")
            context_manager._read_catalog_or_scan("diary")
        except Exception as e:
            pytest.fail(
                f"Missing mandatory catalog must not raise: {e}"
            )


# ---------------------------------------------------------------------------
# Fail-Open Fallback — Corrupt Mandatory Catalog
# ---------------------------------------------------------------------------


class TestFailOpenCorruptMandatoryCatalog:
    """Requirement: Fail-open fallback when mandatory catalog is corrupt.

    When a mandatory catalog file exists but contains invalid JSON or fails
    schema validation, log a warning and fall back to live scanning.
    """

    def test_memory_catalog_invalid_json_falls_back(self, plugin_data_env):
        """Scenario: Memory catalog contains invalid JSON.

        WHEN memory-catalog.json contains malformed JSON
        THEN logs a warning, falls back to live scanning, does NOT raise.
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("{invalid json!!!")

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Corrupt memory catalog must fall back, not raise"
        )

    def test_memory_catalog_truncated_json_falls_back(self, plugin_data_env):
        """Scenario: Memory catalog contains truncated JSON.

        WHEN memory-catalog.json is truncated mid-write
        THEN logs a warning and falls back.
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text('{"schema_version": "1.0.0", "entries": [')

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Truncated memory catalog must fall back, not raise"
        )

    def test_diary_catalog_invalid_json_falls_back(self, plugin_data_env):
        """Scenario: Diary catalog contains invalid JSON.

        WHEN diary-catalog.json contains malformed JSON
        THEN logs a warning, falls back, does NOT raise.
        """
        import context_manager

        (plugin_data_env / "diary-catalog.json").write_text("not json at all")

        result = context_manager._read_catalog_or_scan("diary")

        assert isinstance(result, list), (
            "Corrupt diary catalog must fall back, not raise"
        )

    def test_corrupt_catalog_logs_warning(self, plugin_data_env, caplog):
        """Corrupt mandatory catalog must log a warning message.

        WHEN memory-catalog.json contains malformed JSON
        THEN a warning is logged indicating the catalog is corrupt.
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("{broken}")

        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("memory")

        # Should have logged a warning about corrupt catalog
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_messages) > 0, (
            "Corrupt catalog must produce a warning log message"
        )

    def test_corrupt_catalog_does_not_raise_unhandled(self, plugin_data_env):
        """Corrupt mandatory catalog must NOT raise an unhandled exception."""
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("{{{{")

        try:
            context_manager._read_catalog_or_scan("memory")
        except json.JSONDecodeError:
            pytest.fail(
                "Corrupt catalog must not raise JSONDecodeError to caller"
            )
        except Exception as e:
            pytest.fail(
                f"Corrupt catalog must not raise any exception: {type(e).__name__}: {e}"
            )


# ---------------------------------------------------------------------------
# Schema Version Checking
# ---------------------------------------------------------------------------


class TestSchemaVersionChecking:
    """Requirement: Schema version checking — reject unknown versions.

    When a catalog has a schema_version that doesn't match the expected
    version, the context manager must fall back to live scanning.
    """

    def test_matching_schema_version_uses_catalog(self, plugin_data_env):
        """Scenario: Memory catalog with matching schema version is used.

        WHEN schema_version matches expected version
        THEN catalog entries are returned.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        assert catalog["schema_version"] == CATALOG_SCHEMA_VERSION
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert len(result) > 0, (
            "Catalog with matching schema version should be used"
        )

    def test_wrong_schema_version_falls_back(self, plugin_data_env):
        """Scenario: Memory catalog has wrong schema version.

        WHEN schema_version does not match expected version
        THEN logs a warning about schema mismatch and falls back.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        catalog["schema_version"] = "99.0.0"  # Unknown future version
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        # With wrong schema, should fall back (return empty or scan result)
        # The catalog entries should NOT be returned as-is
        assert isinstance(result, list), "Schema mismatch must not raise"

    def test_wrong_schema_version_logs_warning(self, plugin_data_env, caplog):
        """Schema version mismatch must log a warning.

        WHEN schema_version doesn't match
        THEN a warning about schema mismatch is logged.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        catalog["schema_version"] = "99.0.0"
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("memory")

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("schema" in msg.lower() or "version" in msg.lower() for msg in warning_messages), (
            "Schema mismatch must produce a warning mentioning schema/version"
        )

    def test_missing_schema_version_field_falls_back(self, plugin_data_env):
        """Catalog missing schema_version field entirely should fall back.

        WHEN catalog JSON has no schema_version field
        THEN treat as invalid and fall back.
        """
        import context_manager

        catalog = {"entries": [{"source": "me.md", "summary": "test"}]}
        # No schema_version field
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Missing schema_version must not raise"
        )

    def test_diary_schema_version_mismatch_falls_back(self, plugin_data_env):
        """Diary catalog with wrong schema version also falls back.

        WHEN diary-catalog.json has schema_version mismatch
        THEN falls back to live scanning.
        """
        import context_manager

        catalog = _valid_diary_catalog()
        catalog["schema_version"] = "0.0.1"  # Old/unknown version
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        assert isinstance(result, list), (
            "Diary schema mismatch must not raise"
        )

    def test_schema_version_accessible_before_entries(self, plugin_data_env):
        """Consumer can read schema_version before parsing entries.

        The schema_version field must be at the top level, enabling
        version-gate logic before full deserialization.
        """
        catalog = _valid_memory_catalog()
        catalog_path = plugin_data_env / "memory-catalog.json"
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        # Read raw JSON and verify schema_version is accessible at top level
        raw = json.loads(catalog_path.read_text())
        assert "schema_version" in raw, (
            "schema_version must be a top-level field"
        )
        # Should be accessible without parsing entries
        version = raw["schema_version"]
        assert isinstance(version, str)


# ---------------------------------------------------------------------------
# Empty Catalog (Valid But Empty)
# ---------------------------------------------------------------------------


class TestEmptyCatalogAuthoritative:
    """Requirement: Fail-open fallback when mandatory catalog is empty.

    When a mandatory catalog has a valid schema but empty entries,
    treat it as authoritative (no fallback to live scanning).
    """

    def test_memory_catalog_empty_entries_no_fallback(self, plugin_data_env):
        """Scenario: Memory catalog exists with zero entries.

        WHEN memory-catalog.json has valid schema but empty entries array
        THEN treats as "no memory context" and does NOT fall back to scanning.
        """
        import context_manager

        catalog = _valid_memory_catalog(entries=[])
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        # Empty entries array with valid schema = authoritative empty result
        assert result == [], (
            "Empty catalog with valid schema should return empty list (authoritative)"
        )

    def test_diary_catalog_empty_entries_no_fallback(self, plugin_data_env):
        """Diary catalog with empty entries is authoritative empty."""
        import context_manager

        catalog = _valid_diary_catalog(entries=[])
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        assert result == [], (
            "Empty diary catalog with valid schema should return empty list"
        )


# ---------------------------------------------------------------------------
# Optional Catalogs — Missing/Disabled
# ---------------------------------------------------------------------------


class TestOptionalCatalogsMissing:
    """Requirement: Optional catalogs do not trigger fallback when missing.

    When skills or resources catalogs are missing, the context manager
    silently skips that context source.
    """

    def test_skills_catalog_missing_with_skills_enabled(self, plugin_data_env, monkeypatch):
        """Scenario: Skills catalog missing with skills enabled.

        WHEN enable_skills is true and skills catalog does not exist
        THEN falls back to live scanning without logging an error.
        """
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")
        # No skills-catalog.json

        result = context_manager._read_catalog_or_scan("skills")

        assert isinstance(result, list), (
            "Missing skills catalog must not raise"
        )

    def test_resources_catalog_missing_with_resources_disabled(self, plugin_data_env, monkeypatch):
        """Scenario: Resources catalog missing with resources disabled.

        WHEN enable_resources is false and resources catalog does not exist
        THEN does not attempt to read or scan resource files at all.
        """
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_resources", "false")

        result = context_manager._read_catalog_or_scan("resources")

        assert isinstance(result, list)
        # With resources disabled, should return empty without attempting reads
        assert result == [], (
            "Disabled resources should return empty list without any I/O"
        )

    def test_skills_missing_does_not_log_error(self, plugin_data_env, monkeypatch, caplog):
        """Missing optional catalog should not produce error-level log.

        WHEN enable_skills is true but skills catalog is missing
        THEN no error-level log (warning at most).
        """
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")

        with caplog.at_level(logging.DEBUG):
            context_manager._read_catalog_or_scan("skills")

        error_messages = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_messages) == 0, (
            "Missing optional catalog should not produce error-level log"
        )


# ---------------------------------------------------------------------------
# Once-Per-Session Warning Logging
# ---------------------------------------------------------------------------


class TestOncePerSessionWarning:
    """Requirement: Once-per-session warning logging for catalog failures.

    Design Decision 8: Warning is emitted once per session, not per call,
    to avoid log spam.
    """

    def test_corrupt_catalog_warns_on_first_call(self, plugin_data_env, caplog):
        """First call with corrupt catalog should log a warning."""
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("not json")

        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("memory")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1, (
            "First call with corrupt catalog must log a warning"
        )

    def test_repeated_calls_warn_only_once(self, plugin_data_env, caplog):
        """Repeated calls with same corrupt catalog should warn only once per session.

        WHEN _read_catalog_or_scan is called multiple times with the same
        corrupt catalog
        THEN the warning is emitted only on the first call (once-per-session
        deduplication).
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("not json")

        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("memory")
            caplog.clear()
            context_manager._read_catalog_or_scan("memory")

        # Second call should NOT produce additional warnings
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                     and "memory" in r.message.lower()]
        assert len(warnings) == 0, (
            "Repeated calls should not re-warn (once-per-session deduplication)"
        )

    def test_different_catalog_types_warn_independently(self, plugin_data_env, caplog):
        """Different catalog types should have independent warning state.

        WHEN memory catalog is corrupt AND diary catalog is corrupt
        THEN each gets its own warning (independent per catalog type).
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("bad")
        (plugin_data_env / "diary-catalog.json").write_text("bad")

        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("memory")
            context_manager._read_catalog_or_scan("diary")

        # Both catalog types should get their own warning
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 2, (
            "Different catalog types should warn independently"
        )


# ---------------------------------------------------------------------------
# Catalog Read Does Not Block on Stale Data
# ---------------------------------------------------------------------------


class TestCatalogStalenessIgnored:
    """Requirement: Catalog read does not block on stale data.

    TTL enforcement is the responsibility of the generation side,
    not the read side. Context manager uses whatever data is available.
    """

    def test_old_catalog_still_used(self, plugin_data_env):
        """Scenario: Catalog older than configured TTL.

        WHEN generated_at is older than catalog_ttl
        THEN still uses catalog data (TTL is advisory for regeneration).
        """
        import context_manager

        catalog = _valid_memory_catalog()
        catalog["generated_at"] = "2020-01-01T00:00:00Z"  # Very old
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert len(result) > 0, (
            "Old catalog should still be used (TTL is read-side irrelevant)"
        )


# ---------------------------------------------------------------------------
# Catalog Read Path Transparent to Callers
# ---------------------------------------------------------------------------


class TestCatalogReadTransparency:
    """Requirement: Catalog read path is transparent to callers.

    The context manager's public interface must remain unchanged.
    Callers should not need to know whether context was assembled
    from catalogs or from live scanning.
    """

    def test_return_type_is_list_from_catalog(self, plugin_data_env):
        """Scenario: Same return shape regardless of source.

        WHEN assembling from catalog path
        THEN returned context is a list.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list)

    def test_return_type_is_list_from_fallback(self, plugin_data_env):
        """Return shape from fallback path is also a list."""
        import context_manager

        # No catalog file — will fall back
        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list)

    def test_diary_return_type_consistent(self, plugin_data_env):
        """Scenario: Same return shape for diary context."""
        import context_manager

        catalog = _valid_diary_catalog()
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")

        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Catalog Read Errors Isolated Per Catalog Type
# ---------------------------------------------------------------------------


class TestCatalogErrorIsolation:
    """Requirement: Catalog read errors are isolated per catalog type.

    A failure reading one catalog MUST NOT prevent reading other catalogs.
    """

    def test_corrupt_diary_does_not_block_memory(self, plugin_data_env):
        """Scenario: Corrupt diary catalog does not block memory catalog.

        WHEN diary catalog contains invalid JSON but memory catalog is valid
        THEN memory catalog is successfully read despite diary failure.
        """
        import context_manager

        # Valid memory catalog
        memory_catalog = _valid_memory_catalog()
        _write_catalog(plugin_data_env, "memory-catalog.json", memory_catalog)

        # Corrupt diary catalog
        (plugin_data_env / "diary-catalog.json").write_text("invalid json")

        # Memory should work fine despite diary being corrupt
        memory_result = context_manager._read_catalog_or_scan("memory")
        assert len(memory_result) > 0, (
            "Memory catalog should work despite corrupt diary catalog"
        )

        # Diary should fall back without crashing
        diary_result = context_manager._read_catalog_or_scan("diary")
        assert isinstance(diary_result, list), (
            "Corrupt diary should fall back, not crash"
        )

    def test_missing_memory_does_not_block_diary(self, plugin_data_env):
        """Scenario: Missing memory catalog does not block diary catalog.

        WHEN memory catalog does not exist but diary catalog is valid
        THEN diary catalog is successfully read despite memory absence.
        """
        import context_manager

        # No memory catalog
        # Valid diary catalog
        diary_catalog = _valid_diary_catalog()
        _write_catalog(plugin_data_env, "diary-catalog.json", diary_catalog)

        # Memory should fall back
        memory_result = context_manager._read_catalog_or_scan("memory")
        assert isinstance(memory_result, list)

        # Diary should work fine
        diary_result = context_manager._read_catalog_or_scan("diary")
        assert len(diary_result) > 0, (
            "Diary catalog should work despite missing memory catalog"
        )

    def test_all_catalogs_independent(self, plugin_data_env, monkeypatch):
        """Each catalog type is read independently — one failure doesn't cascade."""
        import context_manager

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")

        # Memory: valid
        _write_catalog(plugin_data_env, "memory-catalog.json", _valid_memory_catalog())
        # Diary: corrupt
        (plugin_data_env / "diary-catalog.json").write_text("corrupt")
        # Skills: missing (will fall back)
        # Resources: not enabled

        memory_result = context_manager._read_catalog_or_scan("memory")
        diary_result = context_manager._read_catalog_or_scan("diary")
        skills_result = context_manager._read_catalog_or_scan("skills")
        resources_result = context_manager._read_catalog_or_scan("resources")

        assert len(memory_result) > 0, "Memory should succeed"
        assert isinstance(diary_result, list), "Diary should fall back"
        assert isinstance(skills_result, list), "Skills should fall back"
        assert isinstance(resources_result, list), "Resources should return empty"


# ---------------------------------------------------------------------------
# Catalog File Location
# ---------------------------------------------------------------------------


class TestCatalogFileLocation:
    """Verify catalogs are read from the correct path.

    Catalogs must be read from $CLAUDE_PLUGIN_DATA/catalogs/<type>-catalog.json.
    """

    def test_memory_catalog_path(self, plugin_data_env):
        """Memory catalog must be read from catalogs/memory-catalog.json."""
        import context_manager

        catalog = _valid_memory_catalog()
        expected_path = plugin_data_env / "memory-catalog.json"
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        assert expected_path.exists(), "Test setup: catalog file should exist"

        result = context_manager._read_catalog_or_scan("memory")
        assert len(result) > 0, (
            "Should read from $CLAUDE_PLUGIN_DATA/catalogs/memory-catalog.json"
        )

    def test_diary_catalog_path(self, plugin_data_env):
        """Diary catalog must be read from catalogs/diary-catalog.json."""
        import context_manager

        catalog = _valid_diary_catalog()
        _write_catalog(plugin_data_env, "diary-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("diary")
        assert len(result) > 0, (
            "Should read from $CLAUDE_PLUGIN_DATA/catalogs/diary-catalog.json"
        )


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for catalog-first read paths."""

    def test_catalog_is_not_a_dict(self, plugin_data_env):
        """Catalog file that contains a JSON array (not dict) should fall back.

        WHEN catalog file contains valid JSON but not a dict (e.g., a list)
        THEN treat as invalid and fall back.
        """
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text('[1, 2, 3]')

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Non-dict catalog JSON should fall back, not crash"
        )

    def test_catalog_entries_not_a_list(self, plugin_data_env):
        """Catalog with entries field that is not a list should fall back.

        WHEN entries is a dict instead of a list
        THEN treat as invalid and fall back.
        """
        import context_manager

        catalog = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "entries": {"not": "a list"},
        }
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Invalid entries type should fall back, not crash"
        )

    def test_empty_catalog_file(self, plugin_data_env):
        """Empty catalog file (0 bytes) should fall back."""
        import context_manager

        (plugin_data_env / "memory-catalog.json").write_text("")

        result = context_manager._read_catalog_or_scan("memory")

        assert isinstance(result, list), (
            "Empty file should fall back, not crash"
        )

    def test_unknown_catalog_type(self, plugin_data_env):
        """Unknown catalog type should return empty list without crashing."""
        import context_manager

        result = context_manager._read_catalog_or_scan("nonexistent")

        assert isinstance(result, list)
        assert result == [], (
            "Unknown catalog type should return empty list"
        )

    def test_catalog_with_extra_fields(self, plugin_data_env):
        """Catalog with extra unexpected fields should still work.

        Forward compatibility: extra fields in the catalog should be
        ignored, not cause an error.
        """
        import context_manager

        catalog = _valid_memory_catalog()
        catalog["extra_field"] = "should be ignored"
        catalog["entries"][0]["unknown_field"] = "also ignored"
        _write_catalog(plugin_data_env, "memory-catalog.json", catalog)

        result = context_manager._read_catalog_or_scan("memory")

        assert len(result) > 0, (
            "Extra fields should be ignored, not cause failure"
        )

    def test_catalogs_dir_does_not_exist(self, tmp_path, monkeypatch):
        """When catalogs directory itself doesn't exist, should fall back.

        WHEN $CLAUDE_PLUGIN_DATA/catalogs/ doesn't exist
        THEN should fall back gracefully, not crash with FileNotFoundError.
        """
        import context_manager
        from lib.paths import _reset_cache

        # Point to a data dir with no catalogs subdirectory
        data_dir = tmp_path / "no_catalogs_here"
        data_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        _reset_cache()

        try:
            result = context_manager._read_catalog_or_scan("memory")
            assert isinstance(result, list)
        finally:
            _reset_cache()

    def test_permission_error_on_catalog_file(self, plugin_data_env):
        """Permission error reading catalog should fall back, not crash.

        WHEN catalog file exists but is not readable
        THEN should fall back gracefully.
        """
        import context_manager

        catalog_path = plugin_data_env / "memory-catalog.json"
        _write_catalog(plugin_data_env, "memory-catalog.json", _valid_memory_catalog())

        # Make file unreadable
        original_mode = catalog_path.stat().st_mode
        try:
            catalog_path.chmod(0o000)

            result = context_manager._read_catalog_or_scan("memory")

            assert isinstance(result, list), (
                "Permission error should fall back, not crash"
            )
        finally:
            # Restore permissions for cleanup
            catalog_path.chmod(original_mode)


# ---------------------------------------------------------------------------
# Schema Version Constant Consistency
# ---------------------------------------------------------------------------


class TestSchemaVersionConstant:
    """Verify that context_manager uses the same schema version as generators."""

    def test_expected_schema_version_is_defined(self):
        """Context manager must have or reference the expected schema version."""
        from generators.base import CATALOG_SCHEMA_VERSION

        assert isinstance(CATALOG_SCHEMA_VERSION, str)
        assert len(CATALOG_SCHEMA_VERSION) > 0, (
            "CATALOG_SCHEMA_VERSION must be a non-empty string"
        )

    def test_schema_version_follows_semver(self):
        """CATALOG_SCHEMA_VERSION must follow semver-like format."""
        from generators.base import CATALOG_SCHEMA_VERSION

        parts = CATALOG_SCHEMA_VERSION.split(".")
        assert len(parts) >= 2, (
            f"Schema version '{CATALOG_SCHEMA_VERSION}' must follow semver (at least major.minor)"
        )
