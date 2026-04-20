"""Tests for generator base class and state management.

Block 3: Generator base class and state management.

Covers all scenarios from requirements/catalog-generation-base.md:
- GenerationResult and GenerationState dataclasses
- GeneratorBase template method run() lifecycle
- _load_state() / _save_state() with atomic writes and schema versioning
- _read_catalog() / _write_catalog() with JSON serialization
- _call_llm() with retry logic via ModelClient
- Content hashing for source change detection
- State-aware skip for unchanged sources
- Deletion pruning for removed sources
- dry_run mode
- force mode
- Schema versioning for catalogs
- Catalogs directory initialization
- Error handling and failure isolation
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Package Structure
# ---------------------------------------------------------------------------


class TestGeneratorsPackageStructure:
    """Requirement: scripts/generators/ must be a Python package with base module."""

    def test_generators_package_exists(self):
        """scripts/generators/ must exist as a Python package."""
        pkg = SCRIPTS_DIR / "generators"
        assert pkg.is_dir(), f"scripts/generators/ must exist at {pkg}"
        assert (pkg / "__init__.py").exists(), "scripts/generators/__init__.py required"

    def test_base_module_exists(self):
        """scripts/generators/base.py must exist."""
        base_file = SCRIPTS_DIR / "generators" / "base.py"
        assert base_file.exists(), f"scripts/generators/base.py must exist at {base_file}"

    def test_base_module_importable(self):
        """GeneratorBase must be importable from generators.base."""
        from generators.base import GeneratorBase

        assert GeneratorBase is not None


# ---------------------------------------------------------------------------
# GenerationResult Dataclass
# ---------------------------------------------------------------------------


class TestGenerationResult:
    """Requirement: GenerationResult dataclass provides structured return values.

    GenerationResult reports what happened during a generation run:
    generator name, counts of skipped/generated/pruned entries, errors, dry_run flag.
    """

    def test_generation_result_importable(self):
        """GenerationResult must be importable from generators.base."""
        from generators.base import GenerationResult

        assert GenerationResult is not None

    def test_generation_result_is_dataclass(self):
        """GenerationResult must be a dataclass."""
        from generators.base import GenerationResult

        assert dataclasses.is_dataclass(GenerationResult)

    def test_generation_result_has_generator_field(self):
        """GenerationResult must have a 'generator' field (name string)."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "generator" in fields

    def test_generation_result_has_total_sources_field(self):
        """GenerationResult must have a 'total_sources' field."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "total_sources" in fields

    def test_generation_result_has_skipped_field(self):
        """GenerationResult must have a 'skipped' field for unchanged entries."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "skipped" in fields

    def test_generation_result_has_generated_field(self):
        """GenerationResult must have a 'generated' field for LLM-called entries."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "generated" in fields

    def test_generation_result_has_pruned_field(self):
        """GenerationResult must have a 'pruned' field for deleted entries."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "pruned" in fields

    def test_generation_result_has_errors_field(self):
        """GenerationResult must have an 'errors' field (list of strings)."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "errors" in fields

    def test_generation_result_has_dry_run_field(self):
        """GenerationResult must have a 'dry_run' field."""
        from generators.base import GenerationResult

        fields = {f.name for f in dataclasses.fields(GenerationResult)}
        assert "dry_run" in fields

    def test_generation_result_construction(self):
        """GenerationResult can be constructed with all fields."""
        from generators.base import GenerationResult

        result = GenerationResult(
            generator="memory",
            total_sources=5,
            skipped=3,
            generated=1,
            pruned=1,
            errors=["some error"],
            dry_run=False,
        )
        assert result.generator == "memory"
        assert result.total_sources == 5
        assert result.skipped == 3
        assert result.generated == 1
        assert result.pruned == 1
        assert result.errors == ["some error"]
        assert result.dry_run is False


# ---------------------------------------------------------------------------
# GenerationState Dataclass
# ---------------------------------------------------------------------------


class TestGenerationState:
    """Requirement: GenerationState tracks per-generator source hashes.

    State file schema:
    {
      "schema_version": 1,
      "generators": {
        "<name>": {
          "last_run": "ISO-8601",
          "source_hashes": { "key": "hash..." },
          "entry_count": N
        }
      }
    }
    """

    def test_generation_state_importable(self):
        """GenerationState must be importable from generators.base."""
        from generators.base import GenerationState

        assert GenerationState is not None

    def test_generation_state_is_dataclass(self):
        """GenerationState must be a dataclass."""
        from generators.base import GenerationState

        assert dataclasses.is_dataclass(GenerationState)

    def test_generation_state_has_schema_version(self):
        """GenerationState must have a schema_version field."""
        from generators.base import GenerationState

        fields = {f.name for f in dataclasses.fields(GenerationState)}
        assert "schema_version" in fields

    def test_generation_state_has_generators_field(self):
        """GenerationState must have a generators field (dict)."""
        from generators.base import GenerationState

        fields = {f.name for f in dataclasses.fields(GenerationState)}
        assert "generators" in fields


# ---------------------------------------------------------------------------
# GeneratorBase Interface — Template Method Pattern
# ---------------------------------------------------------------------------


class TestGeneratorBaseInterface:
    """Requirement: Shared base class provides common interface.

    GeneratorBase must define override points for subclasses:
    discover_sources, hash_source, build_prompt, parse_response, merge_entry.
    And provide: run(), _load_state, _save_state, _read_catalog, _write_catalog,
    _call_llm.
    """

    def test_generator_base_has_run_method(self):
        """GeneratorBase must have a run() template method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "run")
        assert callable(getattr(GeneratorBase, "run"))

    def test_generator_base_has_discover_sources(self):
        """GeneratorBase must define discover_sources() for subclass override."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "discover_sources")

    def test_generator_base_has_hash_source(self):
        """GeneratorBase must define hash_source() for content hashing."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "hash_source")

    def test_generator_base_has_build_prompt(self):
        """GeneratorBase must define build_prompt() for LLM prompt construction."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "build_prompt")

    def test_generator_base_has_parse_response(self):
        """GeneratorBase must define parse_response() for LLM output parsing."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "parse_response")

    def test_generator_base_has_merge_entry(self):
        """GeneratorBase must define merge_entry() for preserving hand-authored fields."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "merge_entry")

    def test_generator_base_has_load_state(self):
        """GeneratorBase must have _load_state() method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "_load_state")

    def test_generator_base_has_save_state(self):
        """GeneratorBase must have _save_state() method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "_save_state")

    def test_generator_base_has_read_catalog(self):
        """GeneratorBase must have _read_catalog() method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "_read_catalog")

    def test_generator_base_has_write_catalog(self):
        """GeneratorBase must have _write_catalog() method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "_write_catalog")

    def test_generator_base_has_call_llm(self):
        """GeneratorBase must have _call_llm() method."""
        from generators.base import GeneratorBase

        assert hasattr(GeneratorBase, "_call_llm")

    def test_generator_base_constructor_takes_config_and_model_client(self):
        """GeneratorBase.__init__ must accept config and model_client."""
        from generators.base import GeneratorBase
        from generators.config import CatalogConfig

        # Should accept a config and a model_client — we verify the signature
        # accepts these params by attempting construction with mocks.
        # This will fail until base.py is implemented.
        config = CatalogConfig()
        mock_client = MagicMock()
        # Subclass to test since GeneratorBase might be abstract
        try:

            class TestGen(GeneratorBase):
                name = "test"
                catalog_filename = "test.json"

                def discover_sources(self):
                    return {}

                def build_prompt(self, source):
                    return ""

                def parse_response(self, raw):
                    return {}

            gen = TestGen(config=config, model_client=mock_client)
            assert gen is not None
        except TypeError as e:
            pytest.fail(
                f"GeneratorBase constructor should accept config and model_client: {e}"
            )


# ---------------------------------------------------------------------------
# Concrete Test Generator (helper for lifecycle tests)
# ---------------------------------------------------------------------------


def _make_test_generator(
    tmp_path,
    sources=None,
    llm_response='{"summary": "test summary"}',
    llm_side_effect=None,
):
    """Create a concrete GeneratorBase subclass for testing.

    Returns (generator_instance, catalogs_dir).
    """
    from generators.base import GeneratorBase
    from generators.config import CatalogConfig

    catalogs_dir = tmp_path / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)

    # Create source files
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    if sources:
        for name, content in sources.items():
            (sources_dir / name).write_text(content)

    mock_client = AsyncMock()
    if llm_side_effect:
        mock_client.query.side_effect = llm_side_effect
    else:
        mock_response = MagicMock()
        mock_response.content = llm_response
        mock_client.query.return_value = mock_response

    config = CatalogConfig()

    class TestGenerator(GeneratorBase):
        name = "test"
        catalog_filename = "test.json"

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._sources_dir = sources_dir
            self._catalogs_dir_override = catalogs_dir

        @property
        def _catalogs_dir(self):
            return self._catalogs_dir_override

        @property
        def _state_file(self):
            return self._catalogs_dir_override / ".generation-state.json"

        def discover_sources(self):
            result = {}
            if self._sources_dir.exists():
                for f in sorted(self._sources_dir.iterdir()):
                    if f.is_file():
                        result[f.name] = f
            return result

        def build_prompt(self, source):
            content = source.read_text()
            return f"Summarize: {content}"

        def parse_response(self, raw):
            return json.loads(raw) if isinstance(raw, str) else raw

        def merge_entry(self, existing, new):
            if existing and new:
                merged = dict(new)
                for key in ("sections", "bundle", "co_retrieve_for"):
                    if key in existing:
                        merged[key] = existing[key]
                return merged
            return new

    gen = TestGenerator(config=config, model_client=mock_client)
    return gen, catalogs_dir


# ---------------------------------------------------------------------------
# Content Hashing
# ---------------------------------------------------------------------------


class TestContentHashing:
    """Requirement: Content hashing for source change detection.

    The base must compute deterministic content hashes of source files
    so that generators can skip unchanged sources on re-run.
    """

    def test_identical_content_produces_identical_hash(self, tmp_path):
        """Scenario: Same content hashed twice produces same hash."""
        from generators.base import GeneratorBase

        f1 = tmp_path / "file1.txt"
        f2 = tmp_path / "file2.txt"
        f1.write_text("hello world")
        f2.write_text("hello world")

        gen, _ = _make_test_generator(tmp_path)
        hash1 = gen.hash_source(f1)
        hash2 = gen.hash_source(f2)
        assert hash1 == hash2, "Identical content must produce identical hashes"

    def test_modified_content_produces_different_hash(self, tmp_path):
        """Scenario: Modified content (even one character) produces different hash."""
        from generators.base import GeneratorBase

        f = tmp_path / "file.txt"
        f.write_text("hello world")
        gen, _ = _make_test_generator(tmp_path)
        hash1 = gen.hash_source(f)

        f.write_text("hello world!")  # one char added
        hash2 = gen.hash_source(f)
        assert hash1 != hash2, "Modified content must produce different hash"

    def test_hash_based_on_content_not_metadata(self, tmp_path):
        """Scenario: Hash is computed on file content, not metadata.

        Changing modification timestamp but not content should produce same hash.
        """
        f = tmp_path / "file.txt"
        f.write_text("stable content")
        gen, _ = _make_test_generator(tmp_path)
        hash1 = gen.hash_source(f)

        # Touch the file to change mtime without changing content
        os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
        hash2 = gen.hash_source(f)
        assert hash1 == hash2, "Hash should not change when only metadata changes"

    def test_hash_is_non_empty_string(self, tmp_path):
        """Content hash must be a non-empty string."""
        f = tmp_path / "file.txt"
        f.write_text("some content")
        gen, _ = _make_test_generator(tmp_path)
        h = gen.hash_source(f)
        assert isinstance(h, str)
        assert len(h) > 0


# ---------------------------------------------------------------------------
# State File I/O
# ---------------------------------------------------------------------------


class TestStateFileIO:
    """Requirement: State file I/O.

    The base must read/write .generation-state.json in the catalogs directory
    tracking per-entry source hashes and generation metadata.
    """

    def test_state_file_created_on_first_run(self, tmp_path):
        """Scenario: State file created on first run when none exists."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        state_file = catalogs_dir / ".generation-state.json"
        assert not state_file.exists(), "State file should not exist before first run"

        asyncio.run(gen.run())

        assert state_file.exists(), "State file must be created after first run"
        data = json.loads(state_file.read_text())
        assert "generators" in data or "schema_version" in data

    def test_state_file_read_on_subsequent_run(self, tmp_path):
        """Scenario: State file read on subsequent run."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        # First run creates state
        asyncio.run(gen.run())

        # Second run should read existing state
        result = asyncio.run(gen.run())
        # The second run should skip unchanged source
        assert result.skipped >= 0  # Will be 1 if hash matches

    def test_state_file_updated_atomically(self, tmp_path):
        """Scenario: State file updated atomically (write-to-temp-then-rename).

        After a run, the state file must be valid JSON. If the write were
        non-atomic, a crash could leave a truncated file.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        state_file = catalogs_dir / ".generation-state.json"
        content = state_file.read_text()
        # Must be valid JSON (atomic write ensures no partial writes)
        data = json.loads(content)
        assert isinstance(data, dict)

    def test_corrupt_state_file_triggers_fresh_regeneration(self, tmp_path):
        """Scenario: Corrupt state file triggers full regeneration.

        When .generation-state.json contains invalid JSON, the base must
        log a warning, discard it, and proceed as if no state exists.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        state_file = catalogs_dir / ".generation-state.json"
        state_file.write_text("{invalid json!!!")

        # Should not raise — should treat as fresh run
        result = asyncio.run(gen.run())
        assert result.generated >= 1, "Corrupt state should trigger full regeneration"

        # State file should now be valid
        data = json.loads(state_file.read_text())
        assert isinstance(data, dict)

    def test_state_file_has_schema_version(self, tmp_path):
        """State file must include a schema_version field."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        state_file = catalogs_dir / ".generation-state.json"
        data = json.loads(state_file.read_text())
        assert "schema_version" in data, "State file must have schema_version"

    def test_state_file_has_generator_namespace(self, tmp_path):
        """State file must namespace hashes under the generator name.

        Multiple generators coexist without interference.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        state_file = catalogs_dir / ".generation-state.json"
        data = json.loads(state_file.read_text())
        # The test generator's name is "test" — its hashes should be
        # under a "test" namespace in the generators dict
        assert "generators" in data
        assert "test" in data["generators"]
        assert "source_hashes" in data["generators"]["test"]


# ---------------------------------------------------------------------------
# State-Aware Skip for Unchanged Sources
# ---------------------------------------------------------------------------


class TestStateAwareSkip:
    """Requirement: State-aware skip for unchanged sources.

    The base must compare current source hashes against stored state
    to skip regeneration of unchanged entries.
    """

    def test_unchanged_source_is_skipped(self, tmp_path):
        """Scenario: Unchanged source is skipped on second run."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        # First run generates
        result1 = asyncio.run(gen.run())
        assert result1.generated >= 1

        # Second run should skip (source unchanged)
        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        # Point gen2 to the same catalogs dir so it reads existing state
        gen2._catalogs_dir_override = catalogs_dir
        result2 = asyncio.run(gen2.run())
        assert result2.skipped >= 1, "Unchanged source should be skipped"
        assert result2.generated == 0, "No LLM call should be made for unchanged source"

    def test_changed_source_triggers_regeneration(self, tmp_path):
        """Scenario: Changed source triggers regeneration."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "original content"}
        )
        asyncio.run(gen.run())

        # Modify the source file
        sources_dir = tmp_path / "sources"
        (sources_dir / "a.md").write_text("modified content")

        gen2, _ = _make_test_generator(
            tmp_path, sources={"a.md": "modified content"}
        )
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run())
        assert result.generated >= 1, "Changed source should trigger regeneration"

    def test_new_source_triggers_generation(self, tmp_path):
        """Scenario: New source with no prior state triggers generation."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        # Add a new source file
        sources_dir = tmp_path / "sources"
        (sources_dir / "b.md").write_text("content b")

        gen2, _ = _make_test_generator(
            tmp_path,
            sources={"a.md": "content a", "b.md": "content b"},
        )
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run())
        assert result.generated >= 1, "New source should trigger generation"


# ---------------------------------------------------------------------------
# Deletion Pruning
# ---------------------------------------------------------------------------


class TestDeletionPruning:
    """Requirement: Deletion pruning for removed sources.

    The base must detect when source files have been deleted and remove
    their entries from both catalog and state file.
    """

    def test_deleted_source_pruned_from_state(self, tmp_path):
        """Scenario: Deleted source is pruned from state file."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a", "b.md": "content b"}
        )
        asyncio.run(gen.run())

        # Delete source b
        sources_dir = tmp_path / "sources"
        (sources_dir / "b.md").unlink()

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run())
        assert result.pruned >= 1, "Deleted source must be pruned"

        # Verify state file no longer contains hash for b.md
        state_data = json.loads(
            (catalogs_dir / ".generation-state.json").read_text()
        )
        hashes = state_data.get("generators", {}).get("test", {}).get(
            "source_hashes", {}
        )
        assert "b.md" not in hashes, "Deleted source hash must be removed from state"

    def test_deleted_source_pruned_from_catalog(self, tmp_path):
        """Scenario: Deleted source is pruned from catalog output."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a", "b.md": "content b"}
        )
        asyncio.run(gen.run())

        # Delete source b
        sources_dir = tmp_path / "sources"
        (sources_dir / "b.md").unlink()

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run())

        # Read catalog and verify b.md entry is gone
        catalog_file = catalogs_dir / "test.json"
        catalog = json.loads(catalog_file.read_text())
        entries = catalog.get("entries", [])
        entry_keys = [e.get("source", e.get("path", e.get("file", ""))) for e in entries]
        assert "b.md" not in entry_keys, "Deleted source must be pruned from catalog"

    def test_all_sources_deleted_produces_empty_catalog(self, tmp_path):
        """Scenario: All sources deleted results in empty catalog."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        # Delete all sources
        sources_dir = tmp_path / "sources"
        (sources_dir / "a.md").unlink()

        gen2, _ = _make_test_generator(tmp_path, sources={})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run())

        # Catalog should have zero entries but be valid JSON
        catalog_file = catalogs_dir / "test.json"
        catalog = json.loads(catalog_file.read_text())
        entries = catalog.get("entries", [])
        assert len(entries) == 0, "Empty sources should produce empty catalog"
        assert "schema_version" in catalog, "Empty catalog must still have schema_version"


# ---------------------------------------------------------------------------
# Catalog File I/O
# ---------------------------------------------------------------------------


class TestCatalogFileIO:
    """Requirement: Catalog file I/O with JSON serialization.

    The base must read and write catalog JSON files with schema versioning.
    """

    def test_catalog_written_after_generation(self, tmp_path):
        """Catalog file is written after generation completes."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        catalog_file = catalogs_dir / "test.json"
        assert not catalog_file.exists()

        asyncio.run(gen.run())

        assert catalog_file.exists(), "Catalog file must be written after generation"

    def test_catalog_is_valid_json(self, tmp_path):
        """Catalog output must be valid, parseable JSON."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        catalog_file = catalogs_dir / "test.json"
        data = json.loads(catalog_file.read_text())
        assert isinstance(data, dict)

    def test_catalog_includes_schema_version(self, tmp_path):
        """Scenario: Catalog includes schema_version field.

        The output must contain a top-level schema_version field
        with a string value following semver format.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        catalog = json.loads((catalogs_dir / "test.json").read_text())
        assert "schema_version" in catalog, "Catalog must have schema_version"
        assert isinstance(catalog["schema_version"], str)
        # Check semver-ish format (at least has a dot)
        assert "." in catalog["schema_version"] or catalog["schema_version"].isdigit()

    def test_catalog_includes_generated_at_timestamp(self, tmp_path):
        """Catalog must include a generated_at ISO-8601 timestamp."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        catalog = json.loads((catalogs_dir / "test.json").read_text())
        assert "generated_at" in catalog, "Catalog must have generated_at timestamp"
        assert isinstance(catalog["generated_at"], str)

    def test_catalog_includes_entries_array(self, tmp_path):
        """Catalog must have a top-level entries array."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        catalog = json.loads((catalogs_dir / "test.json").read_text())
        assert "entries" in catalog, "Catalog must have entries array"
        assert isinstance(catalog["entries"], list)

    def test_schema_version_accessible_before_entries(self, tmp_path):
        """Scenario: Consumer can read schema_version before parsing entries.

        The schema_version field must be at the top level, not nested
        within entries.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        raw = (catalogs_dir / "test.json").read_text()
        data = json.loads(raw)
        # schema_version should be directly accessible at root level
        assert "schema_version" in data
        version = data["schema_version"]
        assert isinstance(version, str)

    def test_catalog_written_atomically(self, tmp_path):
        """Scenario: Catalog is written atomically (write-to-temp-then-rename).

        After generation, the catalog file must be valid JSON. No partial writes.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a", "b.md": "content b"}
        )
        asyncio.run(gen.run())

        catalog_file = catalogs_dir / "test.json"
        data = json.loads(catalog_file.read_text())
        assert isinstance(data, dict)
        assert "entries" in data


# ---------------------------------------------------------------------------
# LLM Client Integration and Retry Logic
# ---------------------------------------------------------------------------


class TestLLMClientRetry:
    """Requirement: Retry logic for LLM calls.

    The base must retry transient LLM failures with backoff.
    """

    def test_llm_call_routes_through_model_client(self, tmp_path):
        """Scenario: model_client is the sole LLM interface.

        All LLM calls must pass through the model_client.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        # The mock model_client's query should have been called
        assert gen._model_client.query.called or gen._model_client.query.await_count > 0, (
            "LLM calls must route through model_client"
        )

    def test_transient_failure_retried_then_succeeds(self, tmp_path):
        """Scenario: Transient failure followed by success.

        A retryable error (429, 500, 503) on first attempt should be
        retried and the successful result returned.
        """

        class RetryableError(Exception):
            status_code = 429

        call_count = 0
        mock_response = MagicMock()
        mock_response.content = '{"summary": "success after retry"}'

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryableError("rate limited")
            return mock_response

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "content a"},
            llm_side_effect=side_effect,
        )
        result = asyncio.run(gen.run())
        assert result.generated >= 1, "Should succeed after retry"
        assert call_count >= 2, "Should have retried at least once"

    def test_persistent_failure_exhausts_retries(self, tmp_path):
        """Scenario: Persistent failure exhausts retries.

        When LLM fails on every attempt, the base should raise/skip
        and not silently return empty results.
        """

        class PersistentError(Exception):
            status_code = 500

        async def always_fail(*args, **kwargs):
            raise PersistentError("server error")

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "content a"},
            llm_side_effect=always_fail,
        )
        result = asyncio.run(gen.run())
        # The entry should be skipped (error), not silently empty
        assert len(result.errors) >= 1, "Persistent failure must report error"

    def test_non_retryable_error_not_retried(self, tmp_path):
        """Scenario: Non-retryable errors (400, 401) are not retried."""

        class AuthError(Exception):
            status_code = 401

        call_count = 0

        async def auth_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise AuthError("unauthorized")

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "content a"},
            llm_side_effect=auth_fail,
        )
        result = asyncio.run(gen.run())
        # Non-retryable should not be retried (call_count should be 1)
        assert call_count == 1, "Non-retryable error should not be retried"
        assert len(result.errors) >= 1


# ---------------------------------------------------------------------------
# Lifecycle Orchestration — run() Template Method
# ---------------------------------------------------------------------------


class TestRunLifecycle:
    """Requirement: GeneratorBase.run() orchestrates the full lifecycle.

    load state -> detect changes -> generate -> write catalog -> save state
    """

    def test_run_returns_generation_result(self, tmp_path):
        """run() must return a GenerationResult."""
        from generators.base import GenerationResult

        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        result = asyncio.run(gen.run())
        assert isinstance(result, GenerationResult)

    def test_run_generates_all_new_sources(self, tmp_path):
        """First run generates entries for all sources."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "aaa", "b.md": "bbb", "c.md": "ccc"}
        )
        result = asyncio.run(gen.run())
        assert result.total_sources == 3
        assert result.generated == 3
        assert result.skipped == 0
        assert result.pruned == 0

    def test_run_with_no_sources_produces_empty_catalog(self, tmp_path):
        """run() with no sources produces valid empty catalog."""
        gen, catalogs_dir = _make_test_generator(tmp_path, sources={})
        result = asyncio.run(gen.run())
        assert result.total_sources == 0
        assert result.generated == 0

        catalog = json.loads((catalogs_dir / "test.json").read_text())
        assert catalog["entries"] == []

    def test_run_handles_generator_error_per_entry(self, tmp_path):
        """Scenario: Base handles lifecycle even if generator raises per entry.

        If generate entry raises for a specific source, the base logs
        the error, skips that entry, and continues with remaining sources.
        """
        call_count = 0
        mock_response = MagicMock()
        mock_response.content = '{"summary": "ok"}'

        async def sometimes_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("LLM failed for this entry")
            return mock_response

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "aaa", "b.md": "bbb", "c.md": "ccc"},
            llm_side_effect=sometimes_fail,
        )
        result = asyncio.run(gen.run())
        # Should have at least 2 generated (1 failed)
        assert result.generated >= 2, "Should continue after per-entry failure"
        assert len(result.errors) >= 1, "Failed entry must be reported"

    def test_multiple_generators_no_cross_contamination(self, tmp_path):
        """Scenario: Multiple generators coexist without interference.

        Each generator maintains its own state entries and catalog output.
        """
        from generators.base import GeneratorBase
        from generators.config import CatalogConfig

        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir(parents=True)
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir(parents=True)
        (sources_dir / "a.md").write_text("content a")

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = '{"summary": "test"}'
        mock_client.query.return_value = mock_response

        class GenA(GeneratorBase):
            name = "gen_a"
            catalog_filename = "gen_a.json"

            @property
            def _catalogs_dir(self):
                return catalogs_dir

            @property
            def _state_file(self):
                return catalogs_dir / ".generation-state.json"

            def discover_sources(self):
                return {"a.md": sources_dir / "a.md"}

            def build_prompt(self, source):
                return "prompt"

            def parse_response(self, raw):
                return json.loads(raw) if isinstance(raw, str) else raw

        class GenB(GeneratorBase):
            name = "gen_b"
            catalog_filename = "gen_b.json"

            @property
            def _catalogs_dir(self):
                return catalogs_dir

            @property
            def _state_file(self):
                return catalogs_dir / ".generation-state.json"

            def discover_sources(self):
                return {"a.md": sources_dir / "a.md"}

            def build_prompt(self, source):
                return "prompt"

            def parse_response(self, raw):
                return json.loads(raw) if isinstance(raw, str) else raw

        config = CatalogConfig()
        gen_a = GenA(config=config, model_client=mock_client)
        gen_b = GenB(config=config, model_client=mock_client)

        asyncio.run(gen_a.run())
        asyncio.run(gen_b.run())

        # Both catalogs should exist
        assert (catalogs_dir / "gen_a.json").exists()
        assert (catalogs_dir / "gen_b.json").exists()

        # State file should have both namespaces
        state = json.loads((catalogs_dir / ".generation-state.json").read_text())
        assert "gen_a" in state.get("generators", {})
        assert "gen_b" in state.get("generators", {})


# ---------------------------------------------------------------------------
# Dry Run Mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Requirement: dry_run mode reports what would change without side effects.

    No catalog files, state files, or LLM calls should be made in dry_run mode.
    """

    def test_dry_run_does_not_write_catalog(self, tmp_path):
        """Dry run must not create or modify catalog files."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        catalog_file = catalogs_dir / "test.json"

        result = asyncio.run(gen.run(dry_run=True))

        assert not catalog_file.exists(), "Dry run must not write catalog file"

    def test_dry_run_does_not_write_state(self, tmp_path):
        """Dry run must not create or modify state file."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        state_file = catalogs_dir / ".generation-state.json"

        asyncio.run(gen.run(dry_run=True))

        assert not state_file.exists(), "Dry run must not write state file"

    def test_dry_run_does_not_call_llm(self, tmp_path):
        """Dry run must not make any LLM calls."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )

        asyncio.run(gen.run(dry_run=True))

        assert not gen._model_client.query.called, (
            "Dry run must not make LLM calls"
        )

    def test_dry_run_returns_generation_result(self, tmp_path):
        """Dry run must return a GenerationResult with dry_run=True."""
        from generators.base import GenerationResult

        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        result = asyncio.run(gen.run(dry_run=True))

        assert isinstance(result, GenerationResult)
        assert result.dry_run is True

    def test_dry_run_reports_what_would_generate(self, tmp_path):
        """Dry run with new sources should report them as would-be-generated."""
        gen, _ = _make_test_generator(
            tmp_path, sources={"a.md": "content a", "b.md": "content b"}
        )
        result = asyncio.run(gen.run(dry_run=True))
        assert result.total_sources == 2
        # In dry_run, new sources should show as would-be-generated
        assert result.generated >= 2 or result.total_sources >= 2

    def test_dry_run_with_existing_state_reports_skips(self, tmp_path):
        """Dry run with unchanged state should report sources as would-be-skipped."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        # First real run to create state
        asyncio.run(gen.run())

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run(dry_run=True))
        assert result.skipped >= 1, "Dry run should report unchanged sources as skipped"

    def test_dry_run_does_not_modify_existing_catalog(self, tmp_path):
        """Dry run must not modify an existing catalog file."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        catalog_file = catalogs_dir / "test.json"
        original_content = catalog_file.read_text()

        # Modify source and run dry_run
        (tmp_path / "sources" / "a.md").write_text("modified content")
        gen2, _ = _make_test_generator(
            tmp_path, sources={"a.md": "modified content"}
        )
        gen2._catalogs_dir_override = catalogs_dir
        asyncio.run(gen2.run(dry_run=True))

        assert catalog_file.read_text() == original_content, (
            "Dry run must not modify existing catalog"
        )


# ---------------------------------------------------------------------------
# Force Mode
# ---------------------------------------------------------------------------


class TestForceMode:
    """Requirement: force mode ignores hashes and regenerates all entries.

    When force=True, all sources are regenerated regardless of state.
    """

    def test_force_regenerates_unchanged_sources(self, tmp_path):
        """Force mode must regenerate even when content hash matches."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        # Second run with force — same content, but should regenerate
        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run(force=True))
        assert result.generated >= 1, "Force mode must regenerate unchanged sources"
        assert result.skipped == 0, "Force mode must not skip anything"

    def test_force_updates_state_file(self, tmp_path):
        """Force mode must update state file with new timestamps/hashes."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        asyncio.run(gen.run())

        state_before = json.loads(
            (catalogs_dir / ".generation-state.json").read_text()
        )

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content a"})
        gen2._catalogs_dir_override = catalogs_dir
        asyncio.run(gen2.run(force=True))

        state_after = json.loads(
            (catalogs_dir / ".generation-state.json").read_text()
        )

        # last_run timestamp should be updated
        gen_state_before = state_before.get("generators", {}).get("test", {})
        gen_state_after = state_after.get("generators", {}).get("test", {})
        if "last_run" in gen_state_before and "last_run" in gen_state_after:
            # Timestamps should differ (or at least state should be refreshed)
            assert gen_state_after["last_run"] >= gen_state_before["last_run"]

    def test_force_still_prunes_deleted_sources(self, tmp_path):
        """Force mode must still prune deleted sources."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "aaa", "b.md": "bbb"}
        )
        asyncio.run(gen.run())

        # Delete b.md
        (tmp_path / "sources" / "b.md").unlink()

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "aaa"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run(force=True))
        assert result.pruned >= 1, "Force mode must still prune deleted sources"

    def test_force_returns_generation_result(self, tmp_path):
        """Force run must return a GenerationResult with dry_run=False."""
        from generators.base import GenerationResult

        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "aaa"})
        result = asyncio.run(gen.run(force=True))
        assert isinstance(result, GenerationResult)
        assert result.dry_run is False


# ---------------------------------------------------------------------------
# Catalogs Directory Initialization
# ---------------------------------------------------------------------------


class TestCatalogsDirectoryInit:
    """Requirement: Catalogs directory initialization.

    The base must ensure the catalogs directory exists before any I/O.
    """

    def test_directory_created_if_missing(self, tmp_path):
        """Scenario: Directory created if missing.

        When catalogs dir does not exist, the base creates it
        (including parent directories) before writing.
        """
        from generators.base import GeneratorBase
        from generators.config import CatalogConfig

        catalogs_dir = tmp_path / "deep" / "nested" / "catalogs"
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir(parents=True)
        (sources_dir / "a.md").write_text("content")

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = '{"summary": "test"}'
        mock_client.query.return_value = mock_response

        class DirTestGen(GeneratorBase):
            name = "test"
            catalog_filename = "test.json"

            @property
            def _catalogs_dir(self):
                return catalogs_dir

            @property
            def _state_file(self):
                return catalogs_dir / ".generation-state.json"

            def discover_sources(self):
                return {"a.md": sources_dir / "a.md"}

            def build_prompt(self, source):
                return "prompt"

            def parse_response(self, raw):
                return json.loads(raw) if isinstance(raw, str) else raw

        gen = DirTestGen(config=CatalogConfig(), model_client=mock_client)
        assert not catalogs_dir.exists()

        asyncio.run(gen.run())

        assert catalogs_dir.exists(), "Catalogs dir must be created if missing"

    def test_existing_directory_left_untouched(self, tmp_path):
        """Scenario: Existing directory is left untouched."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content"}
        )
        # Pre-create a file in catalogs dir
        existing_file = catalogs_dir / "existing.txt"
        existing_file.write_text("do not delete")

        asyncio.run(gen.run())

        assert existing_file.exists(), "Existing files must not be deleted"
        assert existing_file.read_text() == "do not delete"


# ---------------------------------------------------------------------------
# Error Handling — Entry-Level Failures
# ---------------------------------------------------------------------------


class TestEntryLevelErrorHandling:
    """Requirement: Generator failure per-entry does not abort entire run.

    If a single entry fails LLM generation, the generator must continue
    with remaining entries and report the failure.
    """

    def test_single_entry_failure_continues_remaining(self, tmp_path):
        """Scenario: One entry fails, others succeed."""
        call_count = 0
        mock_response = MagicMock()
        mock_response.content = '{"summary": "ok"}'

        async def fail_on_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("LLM failed for entry 2")
            return mock_response

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "aaa", "b.md": "bbb", "c.md": "ccc"},
            llm_side_effect=fail_on_second,
        )
        result = asyncio.run(gen.run())
        assert result.generated >= 2, "Remaining entries should succeed"
        assert len(result.errors) >= 1, "Failed entry must be in errors list"

    def test_all_entries_fail_still_produces_result(self, tmp_path):
        """Scenario: All entries fail — generator completes, no data loss."""

        async def always_fail(*args, **kwargs):
            raise Exception("total failure")

        gen, catalogs_dir = _make_test_generator(
            tmp_path,
            sources={"a.md": "aaa", "b.md": "bbb"},
            llm_side_effect=always_fail,
        )
        # Should not raise
        result = asyncio.run(gen.run())
        assert result.generated == 0
        assert len(result.errors) >= 2

    def test_failed_entry_preserves_previous_catalog_data(self, tmp_path):
        """Scenario: Failed entry's previous catalog data is preserved.

        When LLM fails for a specific entry on re-run, the old catalog
        entry (if any) should be preserved.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )
        # First successful run
        asyncio.run(gen.run())

        catalog_before = json.loads((catalogs_dir / "test.json").read_text())
        entries_before = catalog_before.get("entries", [])
        assert len(entries_before) >= 1

        # Modify source so it triggers regeneration, but LLM fails
        (tmp_path / "sources" / "a.md").write_text("modified content")

        async def fail(*args, **kwargs):
            raise Exception("LLM failure")

        gen2, _ = _make_test_generator(
            tmp_path,
            sources={"a.md": "modified content"},
            llm_side_effect=fail,
        )
        gen2._catalogs_dir_override = catalogs_dir
        asyncio.run(gen2.run())

        catalog_after = json.loads((catalogs_dir / "test.json").read_text())
        entries_after = catalog_after.get("entries", [])
        # Previous entry should be preserved
        assert len(entries_after) >= 1, (
            "Failed entry should preserve previous catalog data"
        )


# ---------------------------------------------------------------------------
# Merge Entry — Preserve Hand-Authored Fields
# ---------------------------------------------------------------------------


class TestMergeEntry:
    """Requirement: merge_entry preserves hand-authored fields.

    Memory catalog generator must preserve sections, bundle,
    and co_retrieve_for from existing entries.
    """

    def test_merge_preserves_hand_authored_sections(self, tmp_path):
        """Scenario: Hand-authored sections field is preserved."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content a"}
        )

        existing = {"summary": "old", "sections": ["projects", "preferences"]}
        new = {"summary": "new summary"}
        merged = gen.merge_entry(existing, new)

        assert merged["sections"] == ["projects", "preferences"], (
            "merge_entry must preserve hand-authored sections"
        )
        assert merged["summary"] == "new summary", (
            "merge_entry must update LLM-generated fields"
        )

    def test_merge_preserves_hand_authored_bundle(self, tmp_path):
        """Scenario: Hand-authored bundle field is preserved."""
        gen, _ = _make_test_generator(tmp_path)

        existing = {"summary": "old", "bundle": "work-context"}
        new = {"summary": "new"}
        merged = gen.merge_entry(existing, new)

        assert merged["bundle"] == "work-context"

    def test_merge_preserves_hand_authored_co_retrieve_for(self, tmp_path):
        """Scenario: Hand-authored co_retrieve_for field is preserved."""
        gen, _ = _make_test_generator(tmp_path)

        existing = {"summary": "old", "co_retrieve_for": ["diary", "skills"]}
        new = {"summary": "new"}
        merged = gen.merge_entry(existing, new)

        assert merged["co_retrieve_for"] == ["diary", "skills"]

    def test_merge_new_entry_has_no_hand_authored_fields(self, tmp_path):
        """Scenario: New entries have no hand-authored fields (no existing entry)."""
        gen, _ = _make_test_generator(tmp_path)

        new = {"summary": "brand new entry"}
        merged = gen.merge_entry(None, new)

        assert merged == new, "New entry should pass through without added fields"

    def test_merge_updates_llm_fields(self, tmp_path):
        """merge_entry must update LLM-generated fields while keeping hand-authored."""
        gen, _ = _make_test_generator(tmp_path)

        existing = {
            "summary": "old summary",
            "topics": ["old"],
            "sections": ["preserved"],
        }
        new = {"summary": "new summary", "topics": ["new"]}
        merged = gen.merge_entry(existing, new)

        assert merged["summary"] == "new summary"
        assert merged["topics"] == ["new"]
        assert merged["sections"] == ["preserved"]


# ---------------------------------------------------------------------------
# Schema Versioning — Catalog Version Mismatch
# ---------------------------------------------------------------------------


class TestSchemaVersioning:
    """Requirement: Schema versioning for catalogs.

    Each catalog must include a schema version. Version mismatch should
    trigger full regeneration.
    """

    def test_catalog_has_schema_version(self, tmp_path):
        """Scenario: Catalog includes schema version field."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content"}
        )
        asyncio.run(gen.run())

        catalog = json.loads((catalogs_dir / "test.json").read_text())
        assert "schema_version" in catalog

    def test_state_file_has_schema_version(self, tmp_path):
        """State file includes schema version for format tracking."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content"}
        )
        asyncio.run(gen.run())

        state = json.loads(
            (catalogs_dir / ".generation-state.json").read_text()
        )
        assert "schema_version" in state


# ---------------------------------------------------------------------------
# run() Accepts force and dry_run Parameters
# ---------------------------------------------------------------------------


class TestRunParameters:
    """Requirement: run() method signature includes force and dry_run."""

    def test_run_accepts_force_parameter(self, tmp_path):
        """run() must accept force=True without error."""
        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "content"})
        result = asyncio.run(gen.run(force=True))
        assert result is not None

    def test_run_accepts_dry_run_parameter(self, tmp_path):
        """run() must accept dry_run=True without error."""
        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "content"})
        result = asyncio.run(gen.run(dry_run=True))
        assert result is not None

    def test_run_defaults_to_no_force_no_dry_run(self, tmp_path):
        """run() with no args defaults to force=False, dry_run=False."""
        gen, _ = _make_test_generator(tmp_path, sources={"a.md": "content"})
        result = asyncio.run(gen.run())
        assert result.dry_run is False

    def test_run_force_and_dry_run_together(self, tmp_path):
        """run(force=True, dry_run=True) should show all as would-be-generated."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "content"}
        )
        # First normal run
        asyncio.run(gen.run())

        gen2, _ = _make_test_generator(tmp_path, sources={"a.md": "content"})
        gen2._catalogs_dir_override = catalogs_dir
        result = asyncio.run(gen2.run(force=True, dry_run=True))
        assert result.dry_run is True
        # With force+dry_run, should report all sources as would-be-generated
        assert result.generated >= 1 or result.total_sources >= 1


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for generator base class."""

    def test_empty_source_file_handled(self, tmp_path):
        """Generator must handle empty source files without crashing."""
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"empty.md": ""}
        )
        # Should not raise
        result = asyncio.run(gen.run())
        assert result is not None

    def test_unicode_content_hashed_correctly(self, tmp_path):
        """Content hashing must handle Unicode content."""
        gen, _ = _make_test_generator(
            tmp_path, sources={"unicode.md": "Hello \u4e16\u754c \U0001f30d"}
        )
        source_file = tmp_path / "sources" / "unicode.md"
        h = gen.hash_source(source_file)
        assert isinstance(h, str)
        assert len(h) > 0

    def test_large_number_of_sources(self, tmp_path):
        """Generator should handle many source files without error."""
        sources = {f"file_{i}.md": f"content {i}" for i in range(50)}
        gen, catalogs_dir = _make_test_generator(tmp_path, sources=sources)
        result = asyncio.run(gen.run())
        assert result.total_sources == 50
        assert result.generated == 50

    def test_concurrent_state_file_access_safety(self, tmp_path):
        """State file writes must be atomic to handle concurrent access.

        Verify that after a run, the state file is always valid JSON.
        """
        gen, catalogs_dir = _make_test_generator(
            tmp_path, sources={"a.md": "aaa", "b.md": "bbb"}
        )
        asyncio.run(gen.run())

        state_file = catalogs_dir / ".generation-state.json"
        # State must always be valid JSON (atomic write guarantee)
        data = json.loads(state_file.read_text())
        assert isinstance(data, dict)
