"""Tests for resources catalog generator.

Block 6: Skills and resources catalog generators.

Covers all scenarios from requirements/resources-catalog-generator.md:
- Resources catalog generator is gated on configuration (enable_resources + resources_dir)
- Resources catalog generator discovers resource files from configured directory
- Resources catalog entries contain LLM-generated summaries
- Resources catalog uses content hashing for state-aware regeneration
- Deleted resource files are pruned from the catalog
- Resources catalog output conforms to a versioned JSON schema
- Resources catalog generator extends the shared generator base
- Resources catalog generator is invocable by the catalog dispatcher
- Resources catalog generator handles large resource files gracefully
- discover_sources(), build_prompt(), parse_response()
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_RESOURCES_LLM_RESPONSE = json.dumps({
    "summary": "API integration documentation for OpenAI endpoints",
    "topics": ["api", "openai", "integration"],
})


def _make_mock_client(response_content=None):
    """Create an AsyncMock model client that returns the given content."""
    from lib.model_client import ModelResponse

    if response_content is None:
        response_content = _SAMPLE_RESOURCES_LLM_RESPONSE
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=response_content))
    return client


def _make_resources_generator(tmp_path, *, client=None, config=None):
    """Create a ResourcesGenerator instance with a temp catalogs dir.

    Returns (generator, catalogs_dir, resources_dir).
    """
    from generators.config import CatalogConfig
    from generators.resources import ResourcesGenerator

    catalogs_dir = tmp_path / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = CatalogConfig(enable_resources=True, resources_dir=str(resources_dir))
    if client is None:
        client = _make_mock_client()

    gen = ResourcesGenerator(config=config, model_client=client)

    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)

    return gen, catalogs_dir, resources_dir


def _write_resource_file(resources_dir, filename="openai-api.md", content="# OpenAI API\nIntegration docs."):
    """Write a resource file to the resources directory."""
    path = resources_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _read_catalog(catalogs_dir, filename="resources.json"):
    """Read and parse a catalog JSON file."""
    path = catalogs_dir / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _read_state(catalogs_dir):
    """Read and parse the generation state file."""
    path = catalogs_dir / ".generation-state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(catalogs_dir, state_data):
    """Write a generation state file."""
    path = catalogs_dir / ".generation-state.json"
    path.write_text(json.dumps(state_data), encoding="utf-8")


def _write_catalog(catalogs_dir, catalog_data, filename="resources.json"):
    """Write a catalog file."""
    path = catalogs_dir / filename
    path.write_text(json.dumps(catalog_data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Module Structure & Entry Point
# ---------------------------------------------------------------------------


class TestResourcesGeneratorModuleStructure:
    """Requirement: Resources catalog generator module structure.

    The resources catalog generator MUST be implemented at
    scripts/generators/resources.py and expose ResourcesGenerator class.
    """

    def test_module_file_exists(self):
        """scripts/generators/resources.py must exist."""
        module_file = SCRIPTS_DIR / "generators" / "resources.py"
        assert module_file.exists(), (
            f"Resources catalog generator must exist at {module_file}"
        )

    def test_module_importable(self):
        """resources module must be importable without error."""
        from generators import resources  # noqa: F401

    def test_resources_generator_class_exists(self):
        """ResourcesGenerator class must be exposed by the module."""
        from generators.resources import ResourcesGenerator

        assert ResourcesGenerator is not None

    def test_resources_generator_inherits_generator_base(self):
        """ResourcesGenerator must be a subclass of GeneratorBase."""
        from generators.base import GeneratorBase
        from generators.resources import ResourcesGenerator

        assert issubclass(ResourcesGenerator, GeneratorBase), (
            "ResourcesGenerator must inherit from GeneratorBase"
        )


# ---------------------------------------------------------------------------
# Generator Identity
# ---------------------------------------------------------------------------


class TestResourcesGeneratorIdentity:
    """ResourcesGenerator must have correct name and catalog_filename."""

    def test_generator_name_is_resources(self, tmp_path, monkeypatch):
        """Generator name must be 'resources' for state namespacing."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        from generators.resources import ResourcesGenerator
        from generators.config import CatalogConfig

        config = CatalogConfig(enable_resources=True, resources_dir=str(tmp_path / "r"))
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())
        assert gen.name == "resources"

    def test_catalog_filename_is_resources_json(self, tmp_path, monkeypatch):
        """Catalog output must be resources.json."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        from generators.resources import ResourcesGenerator
        from generators.config import CatalogConfig

        config = CatalogConfig(enable_resources=True, resources_dir=str(tmp_path / "r"))
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())
        assert gen.catalog_filename == "resources.json"


# ---------------------------------------------------------------------------
# Config Gating (enable_resources + resources_dir)
# ---------------------------------------------------------------------------


class TestResourcesGeneratorConfigGating:
    """Requirement: Resources catalog generator is gated on configuration.

    The generator MUST only run when both enable_resources is true AND
    resources_dir is set to a non-empty string.
    """

    def test_skip_when_enable_resources_false(self, tmp_path, monkeypatch):
        """Generator skips when enable_resources is false, even with valid resources_dir."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        _write_resource_file(resources_dir)

        config = CatalogConfig(enable_resources=False, resources_dir=str(resources_dir))
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        assert result.generated == 0
        client.query.assert_not_called()
        assert not (catalogs_dir / "resources.json").exists()

    def test_skip_when_resources_dir_empty(self, tmp_path, monkeypatch):
        """Generator skips when resources_dir is empty string."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        config = CatalogConfig(enable_resources=True, resources_dir="")
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        assert result.generated == 0
        client.query.assert_not_called()

    def test_skip_when_resources_dir_not_set(self, tmp_path, monkeypatch):
        """Generator skips when resources_dir is not configured."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        # Default CatalogConfig has resources_dir=""
        config = CatalogConfig(enable_resources=True)
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        client.query.assert_not_called()

    def test_skip_when_both_disabled(self, tmp_path, monkeypatch):
        """Generator skips when both enable_resources is false and resources_dir empty."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        config = CatalogConfig(enable_resources=False, resources_dir="")
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())

        result = asyncio.run(gen.run())
        assert result.total_sources == 0

    def test_runs_when_enabled_with_valid_dir(self, tmp_path, monkeypatch):
        """Generator runs when enable_resources is true and resources_dir is valid."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        result = asyncio.run(gen.run())

        assert result.total_sources == 1
        assert result.generated == 1
        assert (catalogs_dir / "resources.json").exists()

    def test_no_catalog_file_modified_when_disabled(self, tmp_path, monkeypatch):
        """When disabled, existing catalog files must not be touched."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()

        existing = {"schema_version": "1.0.0", "entries": [{"path": "old.md"}]}
        _write_catalog(catalogs_dir, existing)

        config = CatalogConfig(enable_resources=False, resources_dir=str(resources_dir))
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert catalog == existing

    def test_resources_dir_does_not_exist_on_disk(self, tmp_path, monkeypatch):
        """resources_dir pointing to nonexistent path produces no catalog, no crash."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        nonexistent = tmp_path / "nonexistent_resources"

        config = CatalogConfig(enable_resources=True, resources_dir=str(nonexistent))
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())

        # Should not raise
        result = asyncio.run(gen.run())
        assert result.total_sources == 0


# ---------------------------------------------------------------------------
# discover_sources()
# ---------------------------------------------------------------------------


class TestResourcesDiscoverSources:
    """Requirement: Resources catalog generator discovers resource files.

    The generator MUST recursively scan the directory specified by resources_dir.
    """

    def test_discovers_flat_directory_files(self, tmp_path, monkeypatch):
        """Discovers resource files at top level of resources_dir."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "openai.md", "# OpenAI")
        _write_resource_file(resources_dir, "setup.md", "# Setup")
        _write_resource_file(resources_dir, "deploy.md", "# Deploy")

        sources = gen.discover_sources()
        assert len(sources) == 3

    def test_discovers_nested_directory_files(self, tmp_path, monkeypatch):
        """Discovers resource files in subdirectories recursively."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "apis/openai.md", "# OpenAI API")
        _write_resource_file(resources_dir, "docs/setup.md", "# Setup Docs")
        _write_resource_file(resources_dir, "top.md", "# Top Level")

        sources = gen.discover_sources()
        assert len(sources) == 3

    def test_preserves_relative_paths(self, tmp_path, monkeypatch):
        """Source keys preserve paths relative to resources_dir."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "apis/openai.md", "# OpenAI API")
        _write_resource_file(resources_dir, "top.md", "# Top")

        sources = gen.discover_sources()

        # Keys should include relative path components
        keys = set(sources.keys())
        has_nested = any("apis" in k for k in keys)
        assert has_nested, f"Expected nested path in keys, got {keys}"

    def test_empty_resources_dir_returns_empty(self, tmp_path, monkeypatch):
        """Empty resources directory returns empty dict."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        sources = gen.discover_sources()
        assert sources == {}

    def test_missing_resources_dir_returns_empty(self, tmp_path, monkeypatch):
        """Missing resources directory returns empty dict without error."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        nonexistent = tmp_path / "nonexistent_resources"

        config = CatalogConfig(enable_resources=True, resources_dir=str(nonexistent))
        gen = ResourcesGenerator(config=config, model_client=_make_mock_client())

        sources = gen.discover_sources()
        assert sources == {}


# ---------------------------------------------------------------------------
# build_prompt()
# ---------------------------------------------------------------------------


class TestResourcesBuildPrompt:
    """Requirement: Resources catalog entries contain LLM-generated summaries.

    build_prompt must produce a prompt containing the resource content.
    """

    def test_prompt_contains_resource_content(self, tmp_path, monkeypatch):
        """Prompt must include the resource file's content."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir, "api.md", "# API Integration\nUse the REST endpoint.")

        prompt = gen.build_prompt(resource_path)
        assert "# API Integration" in prompt
        assert "REST endpoint" in prompt

    def test_prompt_requests_summary(self, tmp_path, monkeypatch):
        """Prompt must request a summary field."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir)
        prompt = gen.build_prompt(resource_path)
        assert "summary" in prompt.lower()

    def test_prompt_requests_json_output(self, tmp_path, monkeypatch):
        """Prompt must request JSON output."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir)
        prompt = gen.build_prompt(resource_path)
        assert "json" in prompt.lower()

    def test_prompt_requests_intent_domains(self, tmp_path, monkeypatch):
        """Prompt must request intent_domains for multi-corpus routing (1.2.0)."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir)
        prompt = gen.build_prompt(resource_path)
        assert "intent_domains" in prompt
        assert "anti_domains" in prompt


# ---------------------------------------------------------------------------
# parse_response()
# ---------------------------------------------------------------------------


class TestResourcesParseResponse:
    """Requirement: Resources catalog generator parses LLM responses."""

    def test_parses_valid_json(self, tmp_path, monkeypatch):
        """parse_response returns a dict from valid JSON."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        raw = json.dumps({"summary": "API docs", "topics": ["api"]})
        result = gen.parse_response(raw)

        assert isinstance(result, dict)
        assert result["summary"] == "API docs"

    def test_parses_json_in_code_fences(self, tmp_path, monkeypatch):
        """parse_response handles JSON wrapped in markdown code fences."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        raw = '```json\n{"summary": "API docs", "topics": []}\n```'
        result = gen.parse_response(raw)
        assert result["summary"] == "API docs"

    def test_raises_on_invalid_json(self, tmp_path, monkeypatch):
        """parse_response raises on malformed JSON."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        with pytest.raises((json.JSONDecodeError, ValueError)):
            gen.parse_response("not valid json")


# ---------------------------------------------------------------------------
# merge_entry() — Hand-Authored Field Preservation
# ---------------------------------------------------------------------------


class TestResourcesMergeEntry:
    """Requirement: Preserve hand-authored intent_domains/anti_domains across regeneration (1.2.0)."""

    def test_preserves_intent_domains_on_regeneration(self, tmp_path, monkeypatch):
        gen, _, _ = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        existing = {
            "source": "voice-ai/2026.md",
            "summary": "old",
            "intent_domains": ["researching voice AI", "comparing voice frameworks"],
        }
        new = {"source": "voice-ai/2026.md", "summary": "new", "topics": ["voice"]}

        merged = gen.merge_entry(existing, new)
        assert merged["intent_domains"] == [
            "researching voice AI",
            "comparing voice frameworks",
        ]
        assert merged["summary"] == "new"

    def test_preserves_anti_domains_on_regeneration(self, tmp_path, monkeypatch):
        gen, _, _ = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        existing = {"source": "x.md", "summary": "old", "anti_domains": ["debugging python"]}
        new = {"source": "x.md", "summary": "new"}

        merged = gen.merge_entry(existing, new)
        assert merged["anti_domains"] == ["debugging python"]

    def test_merge_with_none_existing_returns_new(self, tmp_path, monkeypatch):
        gen, _, _ = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        new = {"source": "x.md", "summary": "first run"}
        merged = gen.merge_entry(None, new)
        assert merged == new


# ---------------------------------------------------------------------------
# Catalog Output Structure
# ---------------------------------------------------------------------------


class TestResourcesCatalogOutput:
    """Requirement: Resources catalog output conforms to a versioned JSON schema.

    Catalog must have schema_version, generated_at, entries array.
    Each entry must have path, summary, and content_hash.
    """

    def test_catalog_has_schema_version(self, tmp_path, monkeypatch):
        """Catalog must include schema_version field."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "schema_version" in catalog
        assert isinstance(catalog["schema_version"], str)

    def test_catalog_has_generated_at(self, tmp_path, monkeypatch):
        """Catalog must include generated_at ISO-8601 timestamp."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "generated_at" in catalog
        from datetime import datetime
        datetime.fromisoformat(catalog["generated_at"])

    def test_catalog_has_entries_array(self, tmp_path, monkeypatch):
        """Catalog must include entries as an array."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "entries" in catalog
        assert isinstance(catalog["entries"], list)

    def test_one_entry_per_resource_file(self, tmp_path, monkeypatch):
        """Catalog must have one entry per resource file."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "a.md", "# A")
        _write_resource_file(resources_dir, "b.md", "# B")
        _write_resource_file(resources_dir, "c.md", "# C")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 3

    def test_entry_has_source_or_path_field(self, tmp_path, monkeypatch):
        """Each entry must reference the source resource file."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir, "api.md", "# API")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        # Entry should have either "source" or "path" field
        has_ref = "source" in entry or "path" in entry
        assert has_ref, f"Entry must have source or path field: {entry}"

    def test_entry_has_summary(self, tmp_path, monkeypatch):
        """Each entry must include a summary string."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir, "api.md", "# API")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "summary" in entry
        assert isinstance(entry["summary"], str)
        assert len(entry["summary"]) > 0

    def test_empty_resources_dir_produces_empty_catalog(self, tmp_path, monkeypatch):
        """Empty resources directory produces valid catalog with empty entries."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []
        assert "schema_version" in catalog

    def test_catalog_is_valid_json(self, tmp_path, monkeypatch):
        """Written catalog must be valid parseable JSON."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        content = (catalogs_dir / "resources.json").read_text(encoding="utf-8")
        catalog = json.loads(content)  # Must not raise
        assert isinstance(catalog, dict)


# ---------------------------------------------------------------------------
# State-Aware Regeneration
# ---------------------------------------------------------------------------


class TestResourcesStateAwareRegeneration:
    """Requirement: Resources catalog uses content hashing for state-aware regeneration.

    Unchanged files MUST be skipped. Modified or new files trigger LLM calls.
    """

    def test_skip_unchanged_resource_file(self, tmp_path, monkeypatch):
        """Unchanged resource files are skipped (no LLM call)."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "api.md", "# API\nContent.")

        asyncio.run(gen.run())
        client = gen._model_client
        first_call_count = client.query.call_count

        result = asyncio.run(gen.run())

        assert result.skipped == 1
        assert result.generated == 0
        assert client.query.call_count == first_call_count

    def test_regenerate_modified_resource_file(self, tmp_path, monkeypatch):
        """Modified resource files trigger re-summarization."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "api.md", "Version 1")
        asyncio.run(gen.run())

        _write_resource_file(resources_dir, "api.md", "Version 2 with changes")
        result = asyncio.run(gen.run())

        assert result.generated == 1
        assert result.skipped == 0

    def test_generate_new_resource_file(self, tmp_path, monkeypatch):
        """New resource files trigger LLM summarization."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "api.md", "# API")
        asyncio.run(gen.run())

        _write_resource_file(resources_dir, "deploy.md", "# Deploy")
        result = asyncio.run(gen.run())

        assert result.generated >= 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    def test_first_run_with_no_prior_state(self, tmp_path, monkeypatch):
        """First run processes all resource files and creates state."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_resource_file(resources_dir, "a.md", "# A")
        _write_resource_file(resources_dir, "b.md", "# B")

        result = asyncio.run(gen.run())

        assert result.generated == 2
        assert result.skipped == 0
        state = _read_state(catalogs_dir)
        assert "resources" in state["generators"]


# ---------------------------------------------------------------------------
# Deletion Pruning
# ---------------------------------------------------------------------------


class TestResourcesDeletionPruning:
    """Requirement: Deleted resource files are pruned from the catalog.

    When a resource file no longer exists, its entry MUST be removed.
    """

    def test_prune_deleted_resource(self, tmp_path, monkeypatch):
        """Deleted resource file's entry is removed from catalog."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        api_path = _write_resource_file(resources_dir, "api.md", "# API")
        _write_resource_file(resources_dir, "deploy.md", "# Deploy")

        asyncio.run(gen.run())
        assert len(_read_catalog(catalogs_dir)["entries"]) == 2

        api_path.unlink()
        result = asyncio.run(gen.run())

        assert result.pruned == 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1

    def test_prune_multiple_deleted_resources(self, tmp_path, monkeypatch):
        """Multiple deleted files are all pruned in one run."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        paths = [
            _write_resource_file(resources_dir, "a.md", "# A"),
            _write_resource_file(resources_dir, "b.md", "# B"),
            _write_resource_file(resources_dir, "c.md", "# C"),
        ]

        asyncio.run(gen.run())

        # Delete a and b
        paths[0].unlink()
        paths[1].unlink()

        result = asyncio.run(gen.run())

        assert result.pruned == 2
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1

    def test_prune_removed_from_state(self, tmp_path, monkeypatch):
        """Deleted resource's hash is removed from state file."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        api_path = _write_resource_file(resources_dir, "api.md", "# API")
        _write_resource_file(resources_dir, "deploy.md", "# Deploy")

        asyncio.run(gen.run())

        api_path.unlink()
        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        hashes = state["generators"]["resources"]["source_hashes"]
        assert "api.md" not in hashes


# ---------------------------------------------------------------------------
# LLM Failure Handling
# ---------------------------------------------------------------------------


class TestResourcesLLMFailureHandling:
    """Requirement: Resources catalog generator handles LLM failures gracefully.

    Failed entries are skipped; remaining resources continue processing.
    """

    def test_partial_failure_produces_partial_catalog(self, tmp_path, monkeypatch):
        """When LLM fails for 1 of 3 resources, catalog has 2 entries."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator
        from lib.model_client import ModelResponse

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        _write_resource_file(resources_dir, "a.md", "# A")
        _write_resource_file(resources_dir, "b.md", "# B")
        _write_resource_file(resources_dir, "c.md", "# C")

        call_count = 0

        async def mock_query(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("LLM API error")
            return ModelResponse(content=_SAMPLE_RESOURCES_LLM_RESPONSE)

        client = AsyncMock()
        client.query = mock_query

        config = CatalogConfig(enable_resources=True, resources_dir=str(resources_dir))
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.generated == 2
        assert len(result.errors) == 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    def test_all_failures_still_completes(self, tmp_path, monkeypatch):
        """Generator exits without raising even when all LLM calls fail."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        _write_resource_file(resources_dir, "a.md", "# A")

        client = AsyncMock()
        client.query = AsyncMock(side_effect=Exception("All fail"))

        config = CatalogConfig(enable_resources=True, resources_dir=str(resources_dir))
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())
        assert len(result.errors) == 1
        assert result.generated == 0


# ---------------------------------------------------------------------------
# Content Hashing
# ---------------------------------------------------------------------------


class TestResourcesContentHashing:
    """Content hashing for resource files."""

    def test_hash_deterministic(self, tmp_path, monkeypatch):
        """Same content produces same hash."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir, "api.md", "# API\nContent.")
        hash1 = gen.hash_source(resource_path)
        hash2 = gen.hash_source(resource_path)
        assert hash1 == hash2

    def test_hash_changes_on_modification(self, tmp_path, monkeypatch):
        """Modified content produces different hash."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        resource_path = _write_resource_file(resources_dir, "api.md", "Version 1")
        hash1 = gen.hash_source(resource_path)

        _write_resource_file(resources_dir, "api.md", "Version 2")
        hash2 = gen.hash_source(resource_path)
        assert hash1 != hash2


# ---------------------------------------------------------------------------
# LLM Routes Through model_client
# ---------------------------------------------------------------------------


class TestResourcesLLMRouting:
    """Requirement: All LLM calls route through model_client."""

    def test_llm_call_uses_model_client(self, tmp_path, monkeypatch):
        """LLM calls must go through the injected model_client."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        gen._model_client.query.assert_called()

    def test_custom_model_config_used(self, tmp_path, monkeypatch):
        """Custom catalog_model is used in LLM calls."""
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        _write_resource_file(resources_dir)

        config = CatalogConfig(
            model="claude-haiku-4-5",
            enable_resources=True,
            resources_dir=str(resources_dir),
        )
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        asyncio.run(gen.run())

        call_kwargs = client.query.call_args
        assert call_kwargs.kwargs.get("model", call_kwargs[1].get("model", "")) == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Force and Dry-Run Modes
# ---------------------------------------------------------------------------


class TestResourcesForceMode:
    """Force mode bypasses state-aware skipping."""

    def test_force_regenerates_unchanged_resource(self, tmp_path, monkeypatch):
        """Force mode regenerates even when content hash matches."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir, "api.md", "# API")

        asyncio.run(gen.run())

        result = asyncio.run(gen.run(force=True))

        assert result.generated == 1
        assert result.skipped == 0


class TestResourcesDryRunMode:
    """Dry run reports actions without side effects."""

    def test_dry_run_no_catalog_written(self, tmp_path, monkeypatch):
        """Dry run must not write catalog files."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir, "api.md", "# API")

        result = asyncio.run(gen.run(dry_run=True))

        assert result.dry_run is True
        assert result.generated == 1
        assert not (catalogs_dir / "resources.json").exists()

    def test_dry_run_no_llm_calls(self, tmp_path, monkeypatch):
        """Dry run must not make any LLM calls."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir, "api.md", "# API")

        asyncio.run(gen.run(dry_run=True))

        gen._model_client.query.assert_not_called()


# ---------------------------------------------------------------------------
# Base Class Integration
# ---------------------------------------------------------------------------


class TestResourcesBaseClassIntegration:
    """Requirement: Resources catalog generator extends the shared generator base."""

    def test_state_namespace_is_resources(self, tmp_path, monkeypatch):
        """State entries are stored under 'resources' namespace."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        assert "resources" in state["generators"]

    def test_does_not_interfere_with_other_namespaces(self, tmp_path, monkeypatch):
        """Resources state must not affect other generators' state."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        existing_state = {
            "schema_version": 1,
            "generators": {
                "memory": {
                    "last_run": "2026-04-19T10:00:00Z",
                    "source_hashes": {"memory.md": "abc123"},
                    "entry_count": 1,
                },
                "skills": {
                    "last_run": "2026-04-19T11:00:00Z",
                    "source_hashes": {"dream.md": "def456"},
                    "entry_count": 1,
                },
            },
        }
        _write_state(catalogs_dir, existing_state)
        _write_resource_file(resources_dir)

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        assert "memory" in state["generators"]
        assert state["generators"]["memory"]["source_hashes"]["memory.md"] == "abc123"
        assert "skills" in state["generators"]
        assert state["generators"]["skills"]["source_hashes"]["dream.md"] == "def456"
        assert "resources" in state["generators"]

    def test_uses_base_class_hash_source(self, tmp_path, monkeypatch):
        """hash_source produces SHA-256 consistent with base class."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        content = "# Test content"
        resource_path = _write_resource_file(resources_dir, "test.md", content)

        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        actual = gen.hash_source(resource_path)
        assert actual == expected


# ---------------------------------------------------------------------------
# Large / Binary File Handling
# ---------------------------------------------------------------------------


class TestResourcesLargeFileHandling:
    """Requirement: Resources catalog generator handles large resource files gracefully."""

    def test_binary_file_does_not_crash(self, tmp_path, monkeypatch):
        """Binary files should be skipped or handled without crashing."""
        gen, catalogs_dir, resources_dir = _make_resources_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        # Write a binary file
        binary_path = resources_dir / "image.png"
        binary_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        # Also write a normal text file
        _write_resource_file(resources_dir, "api.md", "# API")

        # Should not crash
        result = asyncio.run(gen.run())

        # The text file should be processed at minimum
        # Binary file may be skipped or produce a minimal entry
        assert result.total_sources >= 1


# ---------------------------------------------------------------------------
# force_enable — explicit --only bypasses the enable_resources gate
# ---------------------------------------------------------------------------


class TestResourcesForceEnable:
    """force_enable lets an explicit selection run despite enable_resources=false.

    The resources_dir requirement is NOT bypassed — there is nothing to
    scan without a directory.
    """

    def test_force_enable_runs_when_flag_disabled(self, tmp_path, monkeypatch):
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        _write_resource_file(resources_dir, "api.md", "# API")

        config = CatalogConfig(enable_resources=False, resources_dir=str(resources_dir))
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run(force_enable=True))

        assert result.total_sources == 1
        assert result.generated == 1
        assert (catalogs_dir / "resources.json").exists()

    def test_force_enable_still_requires_resources_dir(self, tmp_path, monkeypatch):
        from generators.config import CatalogConfig
        from generators.resources import ResourcesGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        config = CatalogConfig(enable_resources=False, resources_dir="")
        client = _make_mock_client()
        gen = ResourcesGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run(force_enable=True))

        assert result.total_sources == 0
        client.query.assert_not_called()


class TestResourcesDispatcherOverride:
    """`--only resources` regenerates even when enable_resources is false."""

    def test_only_filter_runs_disabled_resources(self, tmp_path, monkeypatch):
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        resources_dir = tmp_path / "resources"
        resources_dir.mkdir()
        (tmp_path / "catalogs").mkdir()
        _write_resource_file(resources_dir, "api.md", "# API")

        config = CatalogConfig(enable_resources=False, resources_dir=str(resources_dir))

        import generators.dispatcher as disp
        monkeypatch.setattr(disp, "_create_model_client",
                            lambda: _async_return(_make_mock_client()))

        results = asyncio.run(
            generate_catalogs(config=config, generators=["resources"])
        )
        res = [r for r in results if r.generator == "resources"][0]
        assert res.generated == 1


async def _async_return(value):
    return value
