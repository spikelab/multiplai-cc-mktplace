"""Tests for memory catalog generator.

Block 4: Memory catalog generator.

Covers all scenarios from requirements/memory-catalog-generator.md:
- Generator module location and entry point
- Shared base infrastructure usage (GeneratorBase subclass)
- Output catalog format and location
- Preserve hand-authored fields across regeneration (sections, bundle, co_retrieve_for)
- State-aware skipping of unchanged sources
- Deletion pruning of removed sources
- Configurable model and reasoning effort
- Graceful handling of empty or missing memory directory
- LLM call failure handling with retry
- Atomic catalog write
- discover_sources(), hash_source(), build_prompt(), parse_response(), merge_entry()
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


def _make_mock_client(response_content='{"summary": "test summary", "topics": ["topic1"]}'):
    """Create an AsyncMock model client that returns the given content."""
    from multiplai_core.model_client import ModelResponse

    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=response_content))
    return client


def _make_memory_generator(tmp_path, *, client=None, config=None):
    """Create a MemoryGenerator instance with a temp catalogs dir.

    Returns (generator, catalogs_dir, memory_dir).
    """
    from generators.config import CatalogConfig
    from generators.memory import MemoryGenerator

    catalogs_dir = tmp_path / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = CatalogConfig()
    if client is None:
        client = _make_mock_client()

    gen = MemoryGenerator(config=config, model_client=client)

    # Point generator at our temp directories
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    os.environ["CLAUDE_PLUGIN_OPTION_memory_dir"] = str(memory_dir)

    return gen, catalogs_dir, memory_dir


def _write_memory_file(memory_dir, filename="memory.md", content="# My Memory\nSome content here."):
    """Write a memory file to the memory directory."""
    path = memory_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _read_catalog(catalogs_dir, filename="memory.json"):
    """Read and parse a catalog JSON file."""
    path = catalogs_dir / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _read_state(catalogs_dir):
    """Read and parse the generation state file."""
    path = catalogs_dir / ".generation-state.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Module Structure & Entry Point
# ---------------------------------------------------------------------------


class TestMemoryGeneratorModuleStructure:
    """Requirement: Generator module location and entry point.

    The memory catalog generator MUST be implemented as a module at
    scripts/generators/memory.py and expose a callable entry point.
    """

    def test_module_file_exists(self):
        """scripts/generators/memory.py must exist."""
        module_file = SCRIPTS_DIR / "generators" / "memory.py"
        assert module_file.exists(), (
            f"Memory catalog generator must exist at {module_file}"
        )

    def test_module_importable(self):
        """memory_catalog module must be importable without error."""
        from generators import memory  # noqa: F401

    def test_memory_generator_class_exists(self):
        """MemoryGenerator class must be exposed by the module."""
        from generators.memory import MemoryGenerator

        assert MemoryGenerator is not None

    def test_memory_generator_inherits_generator_base(self):
        """MemoryGenerator must be a subclass of GeneratorBase."""
        from generators.base import GeneratorBase
        from generators.memory import MemoryGenerator

        assert issubclass(MemoryGenerator, GeneratorBase), (
            "MemoryGenerator must inherit from GeneratorBase"
        )


# ---------------------------------------------------------------------------
# Generator Identity
# ---------------------------------------------------------------------------


class TestMemoryGeneratorIdentity:
    """MemoryGenerator must have correct name and catalog_filename."""

    def test_generator_name_is_memory(self, tmp_path, monkeypatch):
        """Generator name must be 'memory' for state namespacing."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(tmp_path / "mem"))
        from generators.memory import MemoryGenerator
        from generators.config import CatalogConfig

        gen = MemoryGenerator(config=CatalogConfig(), model_client=_make_mock_client())
        assert gen.name == "memory"

    def test_catalog_filename_is_memory_json(self, tmp_path, monkeypatch):
        """Catalog output must be named memory.json."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(tmp_path / "mem"))
        from generators.memory import MemoryGenerator
        from generators.config import CatalogConfig

        gen = MemoryGenerator(config=CatalogConfig(), model_client=_make_mock_client())
        assert gen.catalog_filename == "memory.json"


# ---------------------------------------------------------------------------
# discover_sources()
# ---------------------------------------------------------------------------


class TestDiscoverSources:
    """Requirement: MemoryGenerator.discover_sources() locates memory files.

    Must scan the configured memory directory and return a dict mapping
    source keys to Path objects for each memory file found.
    """

    def test_discovers_single_memory_file(self, tmp_path, monkeypatch):
        """Single memory file in directory is discovered."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "memory.md")

        sources = gen.discover_sources()
        assert len(sources) >= 1
        # At least one source must be a Path that exists
        for key, source in sources.items():
            assert Path(source).exists() if isinstance(source, (str, Path)) else True

    def test_discovers_multiple_memory_files(self, tmp_path, monkeypatch):
        """Multiple memory files are all discovered."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "preferences.md", "# Preferences\nContent")
        _write_memory_file(memory_dir, "projects.md", "# Projects\nContent")
        _write_memory_file(memory_dir, "tools.md", "# Tools\nContent")

        sources = gen.discover_sources()
        assert len(sources) == 3

    def test_empty_memory_dir_returns_empty(self, tmp_path, monkeypatch):
        """Empty memory directory returns empty dict."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        sources = gen.discover_sources()
        assert sources == {}

    def test_missing_memory_dir_returns_empty(self, tmp_path, monkeypatch):
        """Non-existent memory directory returns empty dict, no exception."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        nonexistent = tmp_path / "does_not_exist"
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(nonexistent))

        sources = gen.discover_sources()
        assert sources == {}


# ---------------------------------------------------------------------------
# hash_source()
# ---------------------------------------------------------------------------


class TestHashSource:
    """Requirement: hash_source() computes SHA-256 of file contents.

    Content-based hashing for change detection — identical content produces
    identical hashes regardless of file metadata.
    """

    def test_hash_uses_sha256(self, tmp_path, monkeypatch):
        """Hash must be a SHA-256 hex digest of file contents."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "test.md", "hello world")

        result = gen.hash_source(path)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    def test_identical_content_produces_identical_hash(self, tmp_path, monkeypatch):
        """Same content in two files produces the same hash."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        content = "identical content here"
        path_a = _write_memory_file(memory_dir, "a.md", content)
        path_b = _write_memory_file(memory_dir, "b.md", content)

        assert gen.hash_source(path_a) == gen.hash_source(path_b)

    def test_different_content_produces_different_hash(self, tmp_path, monkeypatch):
        """Modified content produces a different hash."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path_a = _write_memory_file(memory_dir, "a.md", "version 1")
        path_b = _write_memory_file(memory_dir, "b.md", "version 2")

        assert gen.hash_source(path_a) != gen.hash_source(path_b)

    def test_hash_based_on_content_not_metadata(self, tmp_path, monkeypatch):
        """Hash depends on content, not file modification time."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "test.md", "stable content")
        hash1 = gen.hash_source(path)

        # Touch the file (change mtime) without changing content
        import time
        time.sleep(0.01)
        os.utime(path, None)
        hash2 = gen.hash_source(path)

        assert hash1 == hash2


# ---------------------------------------------------------------------------
# build_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Requirement: build_prompt() produces a memory-specific LLM prompt.

    The prompt must include the memory file content and instruct the LLM
    to produce structured catalog output.
    """

    def test_prompt_contains_file_content(self, tmp_path, monkeypatch):
        """The LLM prompt must include the source memory file content."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        content = "# My Preferences\nI like Python and dark themes."
        path = _write_memory_file(memory_dir, "prefs.md", content)

        prompt = gen.build_prompt(path)
        assert "My Preferences" in prompt
        assert "Python" in prompt

    def test_prompt_requests_structured_output(self, tmp_path, monkeypatch):
        """The prompt must ask for structured/JSON output."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "test.md", "some content")

        prompt = gen.build_prompt(path)
        # Prompt should mention JSON or structured output
        prompt_lower = prompt.lower()
        assert "json" in prompt_lower or "structured" in prompt_lower

    def test_prompt_is_nonempty_string(self, tmp_path, monkeypatch):
        """build_prompt must return a non-empty string."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "test.md", "content")

        prompt = gen.build_prompt(path)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_keywords_instruction_demands_discriminative_not_glossary(
        self, tmp_path, monkeypatch
    ):
        """The keyword instruction must steer away from generic glossaries.

        Regression for the career-history.md flood: a bare "array of
        keyword strings" let the LLM dump every technology a file
        mentions, which then over-matched unrelated prompts.
        """
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "career.md", "career bio")

        prompt = gen.build_prompt(path).lower()
        assert "discriminative" in prompt
        assert "exclude" in prompt
        # It must explicitly warn off generic tech as keywords.
        assert "python" in prompt and "docker" in prompt


# ---------------------------------------------------------------------------
# parse_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Requirement: parse_response() extracts structured catalog entry from LLM output.

    Must parse LLM text into a dict with at minimum summary and topics/keywords.
    """

    def test_parses_valid_json_response(self, tmp_path, monkeypatch):
        """Valid JSON response is parsed into a dict."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        raw = '{"summary": "User preferences for tooling", "topics": ["python", "dark-theme"]}'
        result = gen.parse_response(raw)
        assert isinstance(result, dict)
        assert "summary" in result

    def test_parsed_entry_has_summary(self, tmp_path, monkeypatch):
        """Parsed entry must contain a summary field."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        raw = '{"summary": "Technical preferences", "topics": ["arch"]}'
        result = gen.parse_response(raw)
        assert result["summary"] == "Technical preferences"

    def test_parsed_entry_has_topics_or_keywords(self, tmp_path, monkeypatch):
        """Parsed entry should include topics or keywords for routing."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        raw = '{"summary": "test", "topics": ["python", "testing"]}'
        result = gen.parse_response(raw)
        has_routing_info = "topics" in result or "keywords" in result
        assert has_routing_info, "Parsed entry must have topics or keywords for routing"

    def test_handles_json_with_markdown_fences(self, tmp_path, monkeypatch):
        """LLM may wrap JSON in markdown code fences — parser should handle it."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        raw = '```json\n{"summary": "fenced output", "topics": ["a"]}\n```'
        result = gen.parse_response(raw)
        assert isinstance(result, dict)
        assert result["summary"] == "fenced output"


# ---------------------------------------------------------------------------
# merge_entry() — Hand-Authored Field Preservation
# ---------------------------------------------------------------------------


class TestMergeEntry:
    """Requirement: Preserve hand-authored fields across regeneration.

    merge_entry() MUST preserve sections, section_anchors, bundle, and
    co_retrieve_for from existing catalog entries when regenerating.
    """

    def test_preserves_sections_on_regeneration(self, tmp_path, monkeypatch):
        """Hand-authored 'sections' field must survive regeneration."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "prefs.md",
            "summary": "old summary",
            "sections": ["projects", "preferences"],
        }
        new = {
            "source": "prefs.md",
            "summary": "new summary",
            "topics": ["python"],
        }

        merged = gen.merge_entry(existing, new)
        assert merged["sections"] == ["projects", "preferences"]
        assert merged["summary"] == "new summary"

    def test_preserves_bundle_on_regeneration(self, tmp_path, monkeypatch):
        """Hand-authored 'bundle' field must survive regeneration."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "work.md",
            "summary": "old",
            "bundle": "work-context",
        }
        new = {
            "source": "work.md",
            "summary": "updated",
        }

        merged = gen.merge_entry(existing, new)
        assert merged["bundle"] == "work-context"
        assert merged["summary"] == "updated"

    def test_preserves_co_retrieve_for_on_regeneration(self, tmp_path, monkeypatch):
        """Hand-authored 'co_retrieve_for' field must survive regeneration."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "tools.md",
            "summary": "old",
            "co_retrieve_for": ["diary", "skills"],
        }
        new = {
            "source": "tools.md",
            "summary": "updated tools",
        }

        merged = gen.merge_entry(existing, new)
        assert merged["co_retrieve_for"] == ["diary", "skills"]

    def test_preserves_section_anchors_on_regeneration(self, tmp_path, monkeypatch):
        """Hand-authored 'section_anchors' field must survive regeneration (1.2.0)."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "big.md",
            "summary": "old",
            "section_anchors": ["Architecture", "Decisions", "Operations"],
        }
        new = {
            "source": "big.md",
            "summary": "updated",
        }

        merged = gen.merge_entry(existing, new)
        assert merged["section_anchors"] == ["Architecture", "Decisions", "Operations"]
        assert merged["summary"] == "updated"

    def test_preserves_all_hand_authored_fields(self, tmp_path, monkeypatch):
        """All hand-authored fields preserved simultaneously."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "all.md",
            "summary": "old",
            "sections": ["sec1"],
            "section_anchors": ["Anchor One"],
            "bundle": "my-bundle",
            "co_retrieve_for": ["diary"],
        }
        new = {
            "source": "all.md",
            "summary": "brand new",
            "topics": ["updated"],
        }

        merged = gen.merge_entry(existing, new)
        assert merged["sections"] == ["sec1"]
        assert merged["section_anchors"] == ["Anchor One"]
        assert merged["bundle"] == "my-bundle"
        assert merged["co_retrieve_for"] == ["diary"]
        assert merged["summary"] == "brand new"
        assert merged["topics"] == ["updated"]

    def test_llm_generated_fields_updated_on_regeneration(self, tmp_path, monkeypatch):
        """LLM-generated fields (summary, topics) are updated during merge."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        existing = {
            "source": "x.md",
            "summary": "outdated summary",
            "topics": ["old-topic"],
            "sections": ["kept"],
        }
        new = {
            "source": "x.md",
            "summary": "fresh summary",
            "topics": ["new-topic"],
        }

        merged = gen.merge_entry(existing, new)
        assert merged["summary"] == "fresh summary"
        assert merged["topics"] == ["new-topic"]

    def test_new_entry_has_no_hand_authored_fields(self, tmp_path, monkeypatch):
        """New entries (no existing) must NOT invent hand-authored fields."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        new = {
            "source": "brand-new.md",
            "summary": "new file",
            "topics": ["intro"],
        }

        merged = gen.merge_entry(None, new)
        # Hand-authored fields should not be invented
        sections_val = merged.get("sections")
        bundle_val = merged.get("bundle")
        co_retrieve_val = merged.get("co_retrieve_for")

        # They should either be absent or set to null/empty defaults
        if sections_val is not None:
            assert sections_val == [] or sections_val is None
        if bundle_val is not None:
            assert bundle_val == "" or bundle_val is None
        if co_retrieve_val is not None:
            assert co_retrieve_val == [] or co_retrieve_val is None

    def test_merge_with_none_existing_returns_new(self, tmp_path, monkeypatch):
        """When existing is None (first run), merge returns the new entry."""
        gen, _, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        new = {"source": "new.md", "summary": "first run"}
        merged = gen.merge_entry(None, new)
        assert merged["summary"] == "first run"


# ---------------------------------------------------------------------------
# Full Run Lifecycle (via run())
# ---------------------------------------------------------------------------


class TestFullRunLifecycle:
    """Requirement: run() orchestrates the full generation lifecycle.

    Tests the template method flow: discover → hash → skip/generate → merge → write.
    """

    @pytest.mark.asyncio
    async def test_first_run_generates_catalog(self, tmp_path, monkeypatch):
        """First run with no prior state generates catalog for all sources."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "prefs.md", "# Preferences\nDark theme")

        result = await gen.run()

        assert result.generator == "memory"
        assert result.generated >= 1
        assert result.dry_run is False
        # Catalog file must exist
        catalog_path = catalogs_dir / "memory.json"
        assert catalog_path.exists()
        catalog = json.loads(catalog_path.read_text())
        assert "schema_version" in catalog
        assert "entries" in catalog
        assert len(catalog["entries"]) >= 1

    @pytest.mark.asyncio
    async def test_first_run_creates_state_file(self, tmp_path, monkeypatch):
        """First run creates .generation-state.json with memory hashes."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        state_path = catalogs_dir / ".generation-state.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert "generators" in state
        assert "memory" in state["generators"]
        assert "source_hashes" in state["generators"]["memory"]

    @pytest.mark.asyncio
    async def test_catalog_has_schema_version(self, tmp_path, monkeypatch):
        """Generated catalog must include schema_version field."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "schema_version" in catalog
        assert isinstance(catalog["schema_version"], str)

    @pytest.mark.asyncio
    async def test_catalog_has_generated_at_timestamp(self, tmp_path, monkeypatch):
        """Generated catalog must include generated_at ISO-8601 timestamp."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "generated_at" in catalog

    @pytest.mark.asyncio
    async def test_catalog_entries_have_source_field(self, tmp_path, monkeypatch):
        """Each catalog entry must have a source identifier."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "prefs.md", "# Prefs")

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        for entry in catalog["entries"]:
            has_key = entry.get("source") or entry.get("path") or entry.get("file")
            assert has_key, "Each catalog entry must have a source identifier"


# ---------------------------------------------------------------------------
# State-Aware Skipping
# ---------------------------------------------------------------------------


class TestStateAwareSkipping:
    """Requirement: Skip regeneration for unchanged sources.

    When a memory file's content hash matches the stored hash, no LLM call
    is made and the existing catalog entry is retained.
    """

    @pytest.mark.asyncio
    async def test_unchanged_source_skipped(self, tmp_path, monkeypatch):
        """Unchanged source file is skipped on second run — no LLM call."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "stable.md", "unchanged content")

        # First run — generates
        result1 = await gen.run()
        assert result1.generated >= 1
        call_count_after_first = client.query.call_count

        # Second run — should skip (same content)
        result2 = await gen.run()
        assert result2.skipped >= 1
        assert result2.generated == 0
        assert client.query.call_count == call_count_after_first  # No new LLM calls

    @pytest.mark.asyncio
    async def test_changed_source_triggers_regeneration(self, tmp_path, monkeypatch):
        """Modified source file triggers LLM call on next run."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "changing.md", "version 1")

        await gen.run()
        calls_after_first = client.query.call_count

        # Modify the file
        _write_memory_file(memory_dir, "changing.md", "version 2 — updated")

        result2 = await gen.run()
        assert result2.generated >= 1
        assert client.query.call_count > calls_after_first

    @pytest.mark.asyncio
    async def test_new_source_triggers_generation(self, tmp_path, monkeypatch):
        """New memory file with no prior state triggers LLM call."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "first.md", "first file")

        await gen.run()
        calls_after_first = client.query.call_count

        # Add a new file
        _write_memory_file(memory_dir, "second.md", "brand new file")

        result2 = await gen.run()
        assert result2.generated >= 1
        assert client.query.call_count > calls_after_first

    @pytest.mark.asyncio
    async def test_state_file_updated_after_generation(self, tmp_path, monkeypatch):
        """State file contains current hashes for all memory files after run."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "a.md", "content a")
        _write_memory_file(memory_dir, "b.md", "content b")

        await gen.run()

        state = _read_state(catalogs_dir)
        memory_state = state["generators"]["memory"]
        assert len(memory_state["source_hashes"]) == 2


# ---------------------------------------------------------------------------
# Deletion Pruning
# ---------------------------------------------------------------------------


class TestDeletionPruning:
    """Requirement: Remove catalog entries for deleted memory files.

    When a previously tracked memory file no longer exists on disk, its
    entry must be removed from both catalog and state file.
    """

    @pytest.mark.asyncio
    async def test_deleted_file_pruned_from_catalog(self, tmp_path, monkeypatch):
        """Deleted memory file is removed from catalog on next run."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "temporary.md", "temp content")

        await gen.run()
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1

        # Delete the file
        path.unlink()

        result = await gen.run()
        assert result.pruned >= 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 0

    @pytest.mark.asyncio
    async def test_deleted_file_pruned_from_state(self, tmp_path, monkeypatch):
        """Deleted memory file's hash is removed from state file."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        path = _write_memory_file(memory_dir, "doomed.md", "will be deleted")

        await gen.run()

        path.unlink()
        await gen.run()

        state = _read_state(catalogs_dir)
        memory_hashes = state["generators"]["memory"]["source_hashes"]
        # The deleted file's key should no longer be in the state
        for key in memory_hashes:
            assert "doomed" not in key

    @pytest.mark.asyncio
    async def test_multiple_deletions_pruned(self, tmp_path, monkeypatch):
        """Multiple deleted files are all pruned in one run."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        p1 = _write_memory_file(memory_dir, "a.md", "a")
        p2 = _write_memory_file(memory_dir, "b.md", "b")
        p3 = _write_memory_file(memory_dir, "c.md", "c")

        await gen.run()

        p1.unlink()
        p2.unlink()
        p3.unlink()

        result = await gen.run()
        assert result.pruned == 3
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 0


# ---------------------------------------------------------------------------
# Empty/Missing Memory Directory
# ---------------------------------------------------------------------------


class TestEmptyOrMissingMemoryDir:
    """Requirement: Graceful handling of empty or missing memory directory.

    Must produce valid empty catalog without raising exceptions.
    """

    @pytest.mark.asyncio
    async def test_empty_dir_produces_empty_catalog(self, tmp_path, monkeypatch):
        """Empty memory dir produces valid catalog with empty entries."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))

        result = await gen.run()

        assert result.total_sources == 0
        assert result.generated == 0
        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []
        assert "schema_version" in catalog

    @pytest.mark.asyncio
    async def test_missing_dir_produces_empty_catalog(self, tmp_path, monkeypatch):
        """Non-existent memory dir produces valid catalog, no exception."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        nonexistent = tmp_path / "nowhere"
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(nonexistent))

        result = await gen.run()

        assert result.total_sources == 0
        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []


# ---------------------------------------------------------------------------
# Force Mode
# ---------------------------------------------------------------------------


class TestForceMode:
    """Requirement: force=True bypasses state-aware skipping."""

    @pytest.mark.asyncio
    async def test_force_regenerates_unchanged_sources(self, tmp_path, monkeypatch):
        """Force mode triggers LLM call even when content hash matches."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "stable.md", "same content")

        await gen.run()
        calls_after_first = client.query.call_count

        result = await gen.run(force=True)
        assert result.generated >= 1
        assert client.query.call_count > calls_after_first


# ---------------------------------------------------------------------------
# Dry-Run Mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Requirement: dry_run=True reports what would happen without side effects."""

    @pytest.mark.asyncio
    async def test_dry_run_no_catalog_written(self, tmp_path, monkeypatch):
        """Dry run does not write catalog file."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        result = await gen.run(dry_run=True)

        assert result.dry_run is True
        assert result.generated >= 1  # Would have generated
        assert not (catalogs_dir / "memory.json").exists()
        assert client.query.call_count == 0  # No LLM calls

    @pytest.mark.asyncio
    async def test_dry_run_no_state_written(self, tmp_path, monkeypatch):
        """Dry run does not write state file."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run(dry_run=True)

        assert not (catalogs_dir / ".generation-state.json").exists()


# ---------------------------------------------------------------------------
# LLM Call Failure Handling
# ---------------------------------------------------------------------------


class TestLLMFailureHandling:
    """Requirement: LLM failures are retried and gracefully handled.

    Failed entries are skipped; existing catalog data is preserved.
    """

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_abort(self, tmp_path, monkeypatch):
        """One failed LLM call does not prevent other entries from generating."""
        from multiplai_core.model_client import ModelResponse

        call_count = 0

        async def flaky_query(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call fails with non-retryable error
                error = Exception("LLM error")
                error.status_code = 400
                raise error
            return ModelResponse(content='{"summary": "ok", "topics": ["t"]}')

        client = AsyncMock()
        client.query = AsyncMock(side_effect=flaky_query)

        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "fail.md", "will fail")
        _write_memory_file(memory_dir, "pass.md", "will succeed")

        result = await gen.run()

        # At least one should succeed, at least one error
        assert len(result.errors) >= 1
        assert result.generated >= 1

    @pytest.mark.asyncio
    async def test_all_llm_failures_preserves_prior_entries(self, tmp_path, monkeypatch):
        """When all LLM calls fail, prior catalog entries are preserved."""
        from multiplai_core.model_client import ModelResponse

        # First run succeeds
        success_client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=success_client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "file.md", "initial content")

        await gen.run()
        catalog_before = _read_catalog(catalogs_dir)
        assert len(catalog_before["entries"]) == 1

        # Second run — modify file and make LLM fail
        _write_memory_file(memory_dir, "file.md", "modified content")
        fail_error = Exception("server error")
        fail_error.status_code = 400
        fail_client = AsyncMock()
        fail_client.query = AsyncMock(side_effect=fail_error)
        gen._model_client = fail_client

        result = await gen.run()

        assert len(result.errors) >= 1
        # Prior entry should still be in catalog
        catalog_after = _read_catalog(catalogs_dir)
        assert len(catalog_after["entries"]) == 1


# ---------------------------------------------------------------------------
# Configurable Model and Reasoning Effort
# ---------------------------------------------------------------------------


class TestConfigurableModelAndEffort:
    """Requirement: Respects plugin.json userConfig for model and effort.

    MemoryGenerator uses config.model for LLM calls, defaulting to
    claude-sonnet-4-6 at medium reasoning effort.
    """

    @pytest.mark.asyncio
    async def test_custom_model_used_in_llm_calls(self, tmp_path, monkeypatch):
        """Custom catalog_model from config is passed to model_client."""
        from generators.config import CatalogConfig

        config = CatalogConfig(model="claude-haiku-4-5")
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(
            tmp_path, client=client, config=config
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        # Verify model param in the LLM call
        assert client.query.call_count >= 1
        call_kwargs = client.query.call_args
        assert call_kwargs.kwargs.get("model") == "claude-haiku-4-5" or \
            (len(call_kwargs.args) > 2 and call_kwargs.args[2] == "claude-haiku-4-5")

    @pytest.mark.asyncio
    async def test_default_model_when_no_config(self, tmp_path, monkeypatch):
        """Default model claude-sonnet-4-6 is used when no override set."""
        from generators.config import CatalogConfig

        config = CatalogConfig()  # defaults
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(
            tmp_path, client=client, config=config
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        assert client.query.call_count >= 1
        call_kwargs = client.query.call_args
        model_used = call_kwargs.kwargs.get("model", "")
        assert "claude-sonnet-4-6" in model_used or "claude-sonnet" in model_used


# ---------------------------------------------------------------------------
# Catalog Content — Routing-Ready Entries
# ---------------------------------------------------------------------------


class TestCatalogContentForRouting:
    """Requirement: Summaries are useful for routing.

    Catalog entries must contain enough info (summary, topics, keywords)
    for context_manager to make routing decisions without reading raw files.
    """

    @pytest.mark.asyncio
    async def test_entry_has_summary_for_routing(self, tmp_path, monkeypatch):
        """Each catalog entry has a non-empty summary."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "prefs.md", "# Preferences\nI like Python")

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        for entry in catalog["entries"]:
            assert "summary" in entry
            assert len(entry["summary"]) > 0

    @pytest.mark.asyncio
    async def test_multiple_sources_produce_multiple_entries(self, tmp_path, monkeypatch):
        """Three memory files produce three catalog entries."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "a.md", "file a")
        _write_memory_file(memory_dir, "b.md", "file b")
        _write_memory_file(memory_dir, "c.md", "file c")

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 3


# ---------------------------------------------------------------------------
# LLM Calls Route Through model_client
# ---------------------------------------------------------------------------


class TestLLMCallsRouting:
    """Requirement: All LLM calls route through model_client.

    No generator should import or instantiate an LLM client directly.
    """

    @pytest.mark.asyncio
    async def test_llm_call_goes_through_model_client(self, tmp_path, monkeypatch):
        """LLM calls during generation use the injected model_client."""
        client = _make_mock_client()
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path, client=client)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        assert client.query.call_count >= 1

    def test_generator_does_not_import_anthropic_directly(self):
        """memory.py must not import anthropic SDK directly."""
        module_path = SCRIPTS_DIR / "generators" / "memory.py"
        if module_path.exists():
            source = module_path.read_text()
            assert "import anthropic" not in source, (
                "Generator must not import anthropic directly — use model_client"
            )
            assert "from anthropic" not in source, (
                "Generator must not import from anthropic — use model_client"
            )


# ---------------------------------------------------------------------------
# Atomic Catalog Write
# ---------------------------------------------------------------------------


class TestAtomicCatalogWrite:
    """Requirement: Catalog file written atomically.

    Write to temp file then rename — concurrent readers never see partial data.
    """

    @pytest.mark.asyncio
    async def test_catalog_is_valid_json_after_write(self, tmp_path, monkeypatch):
        """Written catalog must be valid, parseable JSON."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "test.md", "content")

        await gen.run()

        catalog_path = catalogs_dir / "memory.json"
        # Must parse without error
        data = json.loads(catalog_path.read_text())
        assert isinstance(data, dict)
        assert "entries" in data


# ---------------------------------------------------------------------------
# Merge Preservation During Full Run
# ---------------------------------------------------------------------------


class TestMergePreservationDuringFullRun:
    """Integration: hand-authored fields survive a full run cycle.

    Seed a catalog with hand-authored fields, modify the source,
    and verify the fields survive regeneration via run().
    """

    @pytest.mark.asyncio
    async def test_hand_authored_fields_survive_regeneration_cycle(self, tmp_path, monkeypatch):
        """Full cycle: generate → hand-edit catalog → modify source → re-run preserves edits."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "prefs.md", "# Preferences v1")

        # First run — generates initial catalog
        await gen.run()

        # Hand-edit the catalog to add sections, section_anchors, bundle, co_retrieve_for
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1
        catalog["entries"][0]["sections"] = ["workflow", "tools"]
        catalog["entries"][0]["section_anchors"] = ["Workflow", "Tools"]
        catalog["entries"][0]["bundle"] = "dev-context"
        catalog["entries"][0]["co_retrieve_for"] = ["diary"]
        (catalogs_dir / "memory.json").write_text(json.dumps(catalog), encoding="utf-8")

        # Modify the source to trigger regeneration
        _write_memory_file(memory_dir, "prefs.md", "# Preferences v2 — updated heavily")

        # Re-run — should regenerate but preserve hand-authored fields
        await gen.run()

        catalog_after = _read_catalog(catalogs_dir)
        entry = catalog_after["entries"][0]
        assert entry.get("sections") == ["workflow", "tools"]
        assert entry.get("section_anchors") == ["Workflow", "Tools"]
        assert entry.get("bundle") == "dev-context"
        assert entry.get("co_retrieve_for") == ["diary"]

    @pytest.mark.asyncio
    async def test_unchanged_source_preserves_all_fields(self, tmp_path, monkeypatch):
        """When source is unchanged, entire entry (including hand-authored) is kept."""
        gen, catalogs_dir, memory_dir = _make_memory_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(memory_dir))
        _write_memory_file(memory_dir, "stable.md", "stable content")

        await gen.run()

        # Hand-edit
        catalog = _read_catalog(catalogs_dir)
        catalog["entries"][0]["sections"] = ["custom"]
        catalog["entries"][0]["bundle"] = "my-bundle"
        (catalogs_dir / "memory.json").write_text(json.dumps(catalog), encoding="utf-8")

        # Re-run without changing source
        await gen.run()

        catalog_after = _read_catalog(catalogs_dir)
        entry = catalog_after["entries"][0]
        # Entry was skipped so all fields preserved
        assert entry.get("sections") == ["custom"]
        assert entry.get("bundle") == "my-bundle"
