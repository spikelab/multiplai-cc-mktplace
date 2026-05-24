"""Tests for skills catalog generator.

Block 6: Skills and resources catalog generators.

Covers all scenarios from requirements/skills-catalog-generator.md:
- Skills catalog generator produces a valid catalog from skill files
- Skills catalog generation is gated on enable_skills config
- Skills catalog generator uses shared base infrastructure (GeneratorBase subclass)
- Skills catalog generator performs state-aware regeneration
- Skills catalog generator prunes deleted skill files
- Skills catalog generator handles empty skills directory
- Skills catalog generator uses configured model and reasoning effort
- Skills catalog generator handles LLM call failures gracefully
- Skills catalog generator writes atomic output
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

_SAMPLE_SKILLS_LLM_RESPONSE = json.dumps({
    "name": "dream",
    "summary": "Runs a reflection and diary-writing cycle",
    "intent_domains": ["reflect", "dream", "consolidate learnings"],
})


def _make_mock_client(response_content=None):
    """Create an AsyncMock model client that returns the given content."""
    from lib.model_client import ModelResponse

    if response_content is None:
        response_content = _SAMPLE_SKILLS_LLM_RESPONSE
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=response_content))
    return client


def _make_skills_generator(tmp_path, *, client=None, config=None):
    """Create a SkillsGenerator instance with a temp catalogs dir.

    Returns (generator, catalogs_dir, skills_dir).
    """
    from generators.config import CatalogConfig
    from generators.skills import SkillsGenerator

    catalogs_dir = tmp_path / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = CatalogConfig(enable_skills=True, skills_dir=str(skills_dir))
    if client is None:
        client = _make_mock_client()

    gen = SkillsGenerator(config=config, model_client=client)

    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)

    return gen, catalogs_dir, skills_dir


def _write_skill_file(skills_dir, filename="dream.md", content="# Dream Skill\nTrigger a reflection cycle."):
    """Write a skill file under the Claude Code skill layout: <skills_dir>/<name>/SKILL.md.

    Accepts ``filename`` for backwards-compatible call sites: a value like
    ``"dream.md"`` is mapped to ``<skills_dir>/dream/SKILL.md`` (the ``.md``
    suffix is stripped to derive the skill directory name). The returned
    path points at the SKILL.md, and the source key used by
    ``discover_sources`` is the parent directory name (``"dream"``).
    """
    skill_name = filename[:-3] if filename.endswith(".md") else filename
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


def _read_catalog(catalogs_dir, filename="skills.json"):
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


def _write_catalog(catalogs_dir, catalog_data, filename="skills.json"):
    """Write a catalog file."""
    path = catalogs_dir / filename
    path.write_text(json.dumps(catalog_data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Module Structure & Entry Point
# ---------------------------------------------------------------------------


class TestSkillsGeneratorModuleStructure:
    """Requirement: Skills catalog generator module structure.

    The skills catalog generator MUST be implemented at
    scripts/generators/skills.py and expose SkillsGenerator class.
    """

    def test_module_file_exists(self):
        """scripts/generators/skills.py must exist."""
        module_file = SCRIPTS_DIR / "generators" / "skills.py"
        assert module_file.exists(), (
            f"Skills catalog generator must exist at {module_file}"
        )

    def test_module_importable(self):
        """skills module must be importable without error."""
        from generators import skills  # noqa: F401

    def test_skills_generator_class_exists(self):
        """SkillsGenerator class must be exposed by the module."""
        from generators.skills import SkillsGenerator

        assert SkillsGenerator is not None

    def test_skills_generator_inherits_generator_base(self):
        """SkillsGenerator must be a subclass of GeneratorBase."""
        from generators.base import GeneratorBase
        from generators.skills import SkillsGenerator

        assert issubclass(SkillsGenerator, GeneratorBase), (
            "SkillsGenerator must inherit from GeneratorBase"
        )


# ---------------------------------------------------------------------------
# Generator Identity
# ---------------------------------------------------------------------------


class TestSkillsGeneratorIdentity:
    """SkillsGenerator must have correct name and catalog_filename."""

    def test_generator_name_is_skills(self, tmp_path, monkeypatch):
        """Generator name must be 'skills' for state namespacing."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        from generators.skills import SkillsGenerator
        from generators.config import CatalogConfig

        config = CatalogConfig(enable_skills=True, skills_dir=str(tmp_path / "s"))
        gen = SkillsGenerator(config=config, model_client=_make_mock_client())
        assert gen.name == "skills"

    def test_catalog_filename_is_skills_json(self, tmp_path, monkeypatch):
        """Catalog output must be skills.json."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        from generators.skills import SkillsGenerator
        from generators.config import CatalogConfig

        config = CatalogConfig(enable_skills=True, skills_dir=str(tmp_path / "s"))
        gen = SkillsGenerator(config=config, model_client=_make_mock_client())
        assert gen.catalog_filename == "skills.json"


# ---------------------------------------------------------------------------
# Config Gating (enable_skills)
# ---------------------------------------------------------------------------


class TestSkillsGeneratorConfigGating:
    """Requirement: Skills catalog generation is gated on enable_skills config.

    The generator MUST only run when enable_skills is true.
    When disabled, it MUST skip execution without error.
    """

    def test_skip_when_enable_skills_false(self, tmp_path, monkeypatch):
        """Generator returns early with zero work when enable_skills is false."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir)

        config = CatalogConfig(enable_skills=False, skills_dir=str(skills_dir))
        client = _make_mock_client()
        gen = SkillsGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        assert result.generated == 0
        assert result.skipped == 0
        # No LLM calls should have been made
        client.query.assert_not_called()
        # No catalog file should be written
        assert not (tmp_path / "catalogs" / "skills.json").exists()

    def test_skip_when_enable_skills_not_set(self, tmp_path, monkeypatch):
        """Generator treats missing enable_skills as false and skips."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir)

        # Default CatalogConfig has enable_skills=False
        config = CatalogConfig(skills_dir=str(skills_dir))
        client = _make_mock_client()
        gen = SkillsGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        assert result.generated == 0
        client.query.assert_not_called()

    def test_runs_when_enable_skills_true(self, tmp_path, monkeypatch):
        """Generator proceeds to scan and generate when enable_skills is true."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        result = asyncio.run(gen.run())

        assert result.total_sources == 1
        assert result.generated == 1
        assert (catalogs_dir / "skills.json").exists()

    def test_no_catalog_file_modified_when_disabled(self, tmp_path, monkeypatch):
        """When disabled, existing catalog files must not be touched."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir(parents=True)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Pre-existing catalog
        existing = {"schema_version": "1.0.0", "entries": [{"name": "old"}]}
        _write_catalog(catalogs_dir, existing)

        config = CatalogConfig(enable_skills=False, skills_dir=str(skills_dir))
        gen = SkillsGenerator(config=config, model_client=_make_mock_client())

        asyncio.run(gen.run())

        # Catalog should be untouched
        catalog = _read_catalog(catalogs_dir)
        assert catalog == existing


# ---------------------------------------------------------------------------
# discover_sources()
# ---------------------------------------------------------------------------


class TestSkillsDiscoverSources:
    """Requirement: Skills catalog generator discovers skill files.

    The generator MUST scan ``<skills_dir>/<name>/SKILL.md`` files matching
    the Claude Code skill layout. Source keys are the skill directory names.
    """

    def test_discovers_skill_md_files(self, tmp_path, monkeypatch):
        """Discovers SKILL.md under each skill subdirectory."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream\nReflect.")
        _write_skill_file(skills_dir, "health.md", "# Health\nAudit memory.")
        _write_skill_file(skills_dir, "setup.md", "# Setup\nOnboarding.")

        sources = gen.discover_sources()
        assert len(sources) == 3
        assert "dream" in sources
        assert "health" in sources
        assert "setup" in sources

    def test_ignores_non_skill_files(self, tmp_path, monkeypatch):
        """Files other than ``<name>/SKILL.md`` are ignored."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream")
        # Loose files at the top level of skills_dir — must be ignored
        (skills_dir / "notes.txt").write_text("notes", encoding="utf-8")
        (skills_dir / "config.json").write_text("{}", encoding="utf-8")
        (skills_dir / "stray.md").write_text("not a skill", encoding="utf-8")
        # An assets file inside a skill dir — must not be discovered as a skill
        (skills_dir / "dream" / "assets.md").write_text("# Assets", encoding="utf-8")

        sources = gen.discover_sources()
        assert len(sources) == 1
        assert "dream" in sources

    def test_empty_skills_dir_returns_empty(self, tmp_path, monkeypatch):
        """Empty skills directory returns empty dict."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        sources = gen.discover_sources()
        assert sources == {}

    def test_missing_skills_dir_returns_empty(self, tmp_path, monkeypatch):
        """Missing skills directory returns empty dict without error."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        nonexistent = tmp_path / "nonexistent_skills"

        config = CatalogConfig(enable_skills=True, skills_dir=str(nonexistent))
        gen = SkillsGenerator(config=config, model_client=_make_mock_client())

        sources = gen.discover_sources()
        assert sources == {}

    def test_sources_are_path_objects(self, tmp_path, monkeypatch):
        """Source values should be Path objects pointing to SKILL.md files."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream")

        sources = gen.discover_sources()
        path = sources["dream"]
        assert isinstance(path, Path)
        assert path.exists()
        assert path.name == "SKILL.md"
        assert path.parent.name == "dream"


# ---------------------------------------------------------------------------
# build_prompt()
# ---------------------------------------------------------------------------


class TestSkillsBuildPrompt:
    """Requirement: Skills catalog generator produces prompts for LLM summarization."""

    def test_prompt_contains_skill_content(self, tmp_path, monkeypatch):
        """Prompt must include the skill file's content."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir, "dream.md", "# Dream\nReflect on the day.")

        prompt = gen.build_prompt(skill_path)
        assert "# Dream" in prompt
        assert "Reflect on the day." in prompt

    def test_prompt_requests_name(self, tmp_path, monkeypatch):
        """Prompt must request the skill name field."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir)
        prompt = gen.build_prompt(skill_path)
        assert "name" in prompt.lower()

    def test_prompt_requests_summary(self, tmp_path, monkeypatch):
        """Prompt must request a summary field."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir)
        prompt = gen.build_prompt(skill_path)
        assert "summary" in prompt.lower()

    def test_prompt_requests_intent_domains(self, tmp_path, monkeypatch):
        """Prompt must request intent_domains (renamed from "triggers" in 1.2.0)."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir)
        prompt = gen.build_prompt(skill_path)
        assert "intent_domains" in prompt

    def test_prompt_requests_json_output(self, tmp_path, monkeypatch):
        """Prompt must request JSON response."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir)
        prompt = gen.build_prompt(skill_path)
        assert "json" in prompt.lower()


# ---------------------------------------------------------------------------
# parse_response()
# ---------------------------------------------------------------------------


class TestSkillsParseResponse:
    """Requirement: Skills catalog generator parses LLM responses."""

    def test_parses_valid_json(self, tmp_path, monkeypatch):
        """parse_response returns a dict from valid JSON."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        raw = json.dumps({"name": "dream", "summary": "Reflect", "intent_domains": ["dream"]})
        result = gen.parse_response(raw)

        assert isinstance(result, dict)
        assert result["name"] == "dream"
        assert result["summary"] == "Reflect"
        assert result["intent_domains"] == ["dream"]

    def test_parses_json_in_code_fences(self, tmp_path, monkeypatch):
        """parse_response handles JSON wrapped in markdown code fences."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        raw = '```json\n{"name": "dream", "summary": "Reflect", "intent_domains": []}\n```'
        result = gen.parse_response(raw)

        assert result["name"] == "dream"

    def test_raises_on_invalid_json(self, tmp_path, monkeypatch):
        """parse_response raises on malformed JSON."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        with pytest.raises((json.JSONDecodeError, ValueError)):
            gen.parse_response("not valid json at all")


# ---------------------------------------------------------------------------
# merge_entry() — Hand-Authored Field Preservation
# ---------------------------------------------------------------------------


class TestSkillsMergeEntry:
    """Requirement: Preserve hand-authored intent_domains/anti_domains across regeneration (1.2.0)."""

    def test_preserves_intent_domains_on_regeneration(self, tmp_path, monkeypatch):
        gen, _, _ = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        existing = {
            "source": "writing.md",
            "name": "writing",
            "summary": "old",
            "intent_domains": ["writing a blog post", "drafting an essay"],
        }
        new = {"source": "writing.md", "name": "writing", "summary": "new"}

        merged = gen.merge_entry(existing, new)
        assert merged["intent_domains"] == [
            "writing a blog post",
            "drafting an essay",
        ]
        assert merged["summary"] == "new"

    def test_preserves_anti_domains_on_regeneration(self, tmp_path, monkeypatch):
        gen, _, _ = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        existing = {
            "source": "writing.md",
            "summary": "old",
            "anti_domains": ["debugging code"],
        }
        new = {"source": "writing.md", "summary": "new"}

        merged = gen.merge_entry(existing, new)
        assert merged["anti_domains"] == ["debugging code"]

    def test_merge_with_none_existing_returns_new(self, tmp_path, monkeypatch):
        gen, _, _ = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        new = {"source": "x.md", "name": "x", "summary": "first run"}
        merged = gen.merge_entry(None, new)
        assert merged == new


# ---------------------------------------------------------------------------
# Catalog Output Structure
# ---------------------------------------------------------------------------


class TestSkillsCatalogOutput:
    """Requirement: Skills catalog generator produces valid catalog from skill files.

    Catalog must include schema_version, generated_at, and entries array
    with name, file, summary, intent_domains, content_hash per entry.
    """

    def test_catalog_has_schema_version(self, tmp_path, monkeypatch):
        """Catalog must include schema_version field."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "schema_version" in catalog
        assert isinstance(catalog["schema_version"], str)

    def test_catalog_has_generated_at(self, tmp_path, monkeypatch):
        """Catalog must include generated_at ISO-8601 timestamp."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "generated_at" in catalog
        # Should be parseable as ISO timestamp
        from datetime import datetime
        datetime.fromisoformat(catalog["generated_at"])

    def test_catalog_has_entries_array(self, tmp_path, monkeypatch):
        """Catalog must include entries as an array."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert "entries" in catalog
        assert isinstance(catalog["entries"], list)

    def test_one_entry_per_skill_file(self, tmp_path, monkeypatch):
        """Catalog must have one entry per skill file."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream")
        _write_skill_file(skills_dir, "health.md", "# Health")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    def test_entry_has_source_field(self, tmp_path, monkeypatch):
        """Each entry must reference the source skill file."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert entry.get("source") == "dream"

    def test_entry_has_summary(self, tmp_path, monkeypatch):
        """Each entry must include an LLM-generated summary."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "summary" in entry
        assert isinstance(entry["summary"], str)
        assert len(entry["summary"]) > 0

    def test_entry_has_intent_domains(self, tmp_path, monkeypatch):
        """Each entry must include intent_domains (renamed from "triggers" in 1.2.0)."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "intent_domains" in entry
        assert isinstance(entry["intent_domains"], list)

    def test_empty_skills_dir_produces_empty_catalog(self, tmp_path, monkeypatch):
        """Empty skills directory produces valid catalog with empty entries."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        asyncio.run(gen.run())

        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []
        assert "schema_version" in catalog

    def test_missing_skills_dir_produces_empty_catalog(self, tmp_path, monkeypatch):
        """Missing skills directory produces valid catalog without error."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir(parents=True)
        nonexistent = tmp_path / "nonexistent_skills"

        config = CatalogConfig(enable_skills=True, skills_dir=str(nonexistent))
        gen = SkillsGenerator(config=config, model_client=_make_mock_client())

        result = asyncio.run(gen.run())

        assert result.total_sources == 0
        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []


# ---------------------------------------------------------------------------
# State-Aware Regeneration
# ---------------------------------------------------------------------------


class TestSkillsStateAwareRegeneration:
    """Requirement: Skills catalog generator performs state-aware regeneration.

    Unchanged skill files MUST be skipped. Only modified or new files trigger LLM calls.
    """

    def test_skip_unchanged_skill_file(self, tmp_path, monkeypatch):
        """Unchanged skill files are skipped (no LLM call)."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream\nReflect.")

        # First run: generates
        asyncio.run(gen.run())
        client = gen._model_client
        first_call_count = client.query.call_count

        # Second run: should skip (no change)
        result = asyncio.run(gen.run())

        assert result.skipped == 1
        assert result.generated == 0
        assert client.query.call_count == first_call_count  # No new LLM calls

    def test_regenerate_modified_skill_file(self, tmp_path, monkeypatch):
        """Modified skill files trigger re-summarization."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream\nVersion 1.")

        # First run
        asyncio.run(gen.run())

        # Modify the file
        _write_skill_file(skills_dir, "dream.md", "# Dream\nVersion 2 with changes.")

        # Second run
        result = asyncio.run(gen.run())

        assert result.generated == 1
        assert result.skipped == 0

    def test_generate_new_skill_file(self, tmp_path, monkeypatch):
        """New skill files trigger LLM summarization."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream")

        # First run
        asyncio.run(gen.run())

        # Add a new skill file
        _write_skill_file(skills_dir, "review.md", "# Review\nReview PRs.")

        result = asyncio.run(gen.run())

        assert result.generated >= 1  # At least the new file
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    def test_state_file_records_per_skill_hashes(self, tmp_path, monkeypatch):
        """State file must track content hash per skill file."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        _write_skill_file(skills_dir, "dream.md", "# Dream")
        _write_skill_file(skills_dir, "health.md", "# Health")

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        skills_state = state["generators"]["skills"]
        assert "dream" in skills_state["source_hashes"]
        assert "health" in skills_state["source_hashes"]


# ---------------------------------------------------------------------------
# Deletion Pruning
# ---------------------------------------------------------------------------


class TestSkillsDeletionPruning:
    """Requirement: Skills catalog generator prunes deleted skill files.

    When a skill file no longer exists, its entry MUST be removed from
    both the catalog and .generation-state.json.
    """

    def test_prune_deleted_skill_from_catalog(self, tmp_path, monkeypatch):
        """Deleted skill file's entry is removed from catalog."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        dream_path = _write_skill_file(skills_dir, "dream.md", "# Dream")
        _write_skill_file(skills_dir, "health.md", "# Health")

        # First run: both entries generated
        asyncio.run(gen.run())
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

        # Delete one skill file
        dream_path.unlink()

        # Second run: should prune
        result = asyncio.run(gen.run())

        assert result.pruned == 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1
        sources = [e.get("source") for e in catalog["entries"]]
        assert "dream" not in sources

    def test_prune_deleted_skill_from_state(self, tmp_path, monkeypatch):
        """Deleted skill file's hash is removed from state."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        dream_path = _write_skill_file(skills_dir, "dream.md", "# Dream")
        _write_skill_file(skills_dir, "health.md", "# Health")

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        assert "dream" in state["generators"]["skills"]["source_hashes"]

        # Delete and re-run
        dream_path.unlink()
        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        assert "dream" not in state["generators"]["skills"]["source_hashes"]
        assert "health" in state["generators"]["skills"]["source_hashes"]

    def test_pruning_does_not_trigger_llm_calls(self, tmp_path, monkeypatch):
        """Pruning deleted entries must not make any LLM calls."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        dream_path = _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run())
        client = gen._model_client
        call_count_after_first = client.query.call_count

        # Delete file
        dream_path.unlink()

        asyncio.run(gen.run())

        # No new LLM calls for pruning
        assert client.query.call_count == call_count_after_first


# ---------------------------------------------------------------------------
# LLM Calls Use Configured Model and Effort
# ---------------------------------------------------------------------------


class TestSkillsModelConfig:
    """Requirement: Skills catalog generator uses configured model and reasoning effort.

    LLM calls must use the model from config, defaulting to claude-sonnet-4-6
    at medium reasoning effort.
    """

    def test_default_model_and_effort(self, tmp_path, monkeypatch):
        """Default model is claude-sonnet-4-6 with medium reasoning effort."""
        from generators.config import CatalogConfig

        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        client = gen._model_client
        call_kwargs = client.query.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs.get("model", call_kwargs[1].get("model", "")) == "claude-sonnet-4-6"

    def test_custom_model_override(self, tmp_path, monkeypatch):
        """Custom catalog_model is used when configured."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()
        _write_skill_file(skills_dir)

        config = CatalogConfig(
            model="claude-haiku-4-5",
            enable_skills=True,
            skills_dir=str(skills_dir),
        )
        client = _make_mock_client()
        gen = SkillsGenerator(config=config, model_client=client)

        asyncio.run(gen.run())

        call_kwargs = client.query.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs.get("model", call_kwargs[1].get("model", "")) == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# LLM Failure Handling
# ---------------------------------------------------------------------------


class TestSkillsLLMFailureHandling:
    """Requirement: Skills catalog generator handles LLM call failures gracefully.

    Failed entries are skipped; remaining files continue processing.
    """

    def test_partial_failure_produces_partial_catalog(self, tmp_path, monkeypatch):
        """When 1 of 3 skill files fails LLM, catalog has 2 entries."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator
        from lib.model_client import ModelResponse

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        _write_skill_file(skills_dir, "a.md", "# A")
        _write_skill_file(skills_dir, "b.md", "# B")
        _write_skill_file(skills_dir, "c.md", "# C")

        call_count = 0

        async def mock_query(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("LLM API error")
            return ModelResponse(content=_SAMPLE_SKILLS_LLM_RESPONSE)

        client = AsyncMock()
        client.query = mock_query

        config = CatalogConfig(enable_skills=True, skills_dir=str(skills_dir))
        gen = SkillsGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.generated == 2
        assert len(result.errors) == 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    def test_skip_entry_after_exhausting_retries(self, tmp_path, monkeypatch):
        """Entry is omitted when LLM call fails on all retry attempts."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        _write_skill_file(skills_dir, "dream.md", "# Dream")

        client = AsyncMock()
        client.query = AsyncMock(side_effect=Exception("Persistent failure"))

        config = CatalogConfig(enable_skills=True, skills_dir=str(skills_dir))
        gen = SkillsGenerator(config=config, model_client=client)

        result = asyncio.run(gen.run())

        assert result.generated == 0
        assert len(result.errors) == 1
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 0

    def test_generator_does_not_raise_on_failure(self, tmp_path, monkeypatch):
        """Generator exits without raising an exception even if all fail."""
        from generators.config import CatalogConfig
        from generators.skills import SkillsGenerator

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir()

        _write_skill_file(skills_dir, "a.md", "# A")
        _write_skill_file(skills_dir, "b.md", "# B")

        client = AsyncMock()
        client.query = AsyncMock(side_effect=Exception("All fail"))

        config = CatalogConfig(enable_skills=True, skills_dir=str(skills_dir))
        gen = SkillsGenerator(config=config, model_client=client)

        # Should not raise
        result = asyncio.run(gen.run())
        assert len(result.errors) == 2


# ---------------------------------------------------------------------------
# Content Hashing
# ---------------------------------------------------------------------------


class TestSkillsContentHashing:
    """Requirement: Skills catalog generator uses base class content hashing.

    Hashes are computed via SHA-256 of file content.
    """

    def test_hash_deterministic(self, tmp_path, monkeypatch):
        """Same content produces same hash across calls."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir, "dream.md", "# Dream\nContent.")

        hash1 = gen.hash_source(skill_path)
        hash2 = gen.hash_source(skill_path)
        assert hash1 == hash2

    def test_hash_changes_on_modification(self, tmp_path, monkeypatch):
        """Modified content produces different hash."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        skill_path = _write_skill_file(skills_dir, "dream.md", "Version 1")
        hash1 = gen.hash_source(skill_path)

        _write_skill_file(skills_dir, "dream.md", "Version 2")
        hash2 = gen.hash_source(skill_path)

        assert hash1 != hash2

    def test_hash_uses_sha256(self, tmp_path, monkeypatch):
        """Hash should match SHA-256 of file content."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        content = "# Dream\nSome content."
        skill_path = _write_skill_file(skills_dir, "dream.md", content)

        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        actual = gen.hash_source(skill_path)
        assert actual == expected


# ---------------------------------------------------------------------------
# Atomic Writes
# ---------------------------------------------------------------------------


class TestSkillsAtomicWrite:
    """Requirement: Skills catalog generator writes atomic output.

    Catalog is written to temp file then renamed.
    """

    def test_catalog_file_exists_after_run(self, tmp_path, monkeypatch):
        """Catalog file must exist at expected path after successful run."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        assert (catalogs_dir / "skills.json").exists()

    def test_catalog_is_valid_json(self, tmp_path, monkeypatch):
        """Written catalog must be valid parseable JSON."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        content = (catalogs_dir / "skills.json").read_text(encoding="utf-8")
        catalog = json.loads(content)  # Must not raise
        assert isinstance(catalog, dict)


# ---------------------------------------------------------------------------
# Force and Dry-Run Modes
# ---------------------------------------------------------------------------


class TestSkillsForceMode:
    """Force mode bypasses state-aware skipping."""

    def test_force_regenerates_unchanged_skill(self, tmp_path, monkeypatch):
        """Force mode regenerates even when content hash matches."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        # First run
        asyncio.run(gen.run())

        # Second run with force
        result = asyncio.run(gen.run(force=True))

        assert result.generated == 1
        assert result.skipped == 0


class TestSkillsDryRunMode:
    """Dry run reports actions without side effects."""

    def test_dry_run_no_catalog_written(self, tmp_path, monkeypatch):
        """Dry run must not write any catalog files."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        result = asyncio.run(gen.run(dry_run=True))

        assert result.dry_run is True
        assert result.generated == 1  # Reports what *would* happen
        assert not (catalogs_dir / "skills.json").exists()

    def test_dry_run_no_llm_calls(self, tmp_path, monkeypatch):
        """Dry run must not make any LLM calls."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run(dry_run=True))

        gen._model_client.query.assert_not_called()

    def test_dry_run_no_state_modified(self, tmp_path, monkeypatch):
        """Dry run must not modify the state file."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run(dry_run=True))

        assert not (catalogs_dir / ".generation-state.json").exists()


# ---------------------------------------------------------------------------
# LLM Routes Through model_client
# ---------------------------------------------------------------------------


class TestSkillsLLMRouting:
    """Requirement: All LLM calls route through model_client.

    No direct API calls — all calls go through the shared base _call_llm.
    """

    def test_llm_call_uses_model_client(self, tmp_path, monkeypatch):
        """LLM calls must go through the injected model_client."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir, "dream.md", "# Dream")

        asyncio.run(gen.run())

        gen._model_client.query.assert_called()


# ---------------------------------------------------------------------------
# Base Class Integration
# ---------------------------------------------------------------------------


class TestSkillsBaseClassIntegration:
    """Requirement: Skills catalog generator uses shared base infrastructure.

    Uses GeneratorBase for hashing, state I/O, retry, and schema versioning.
    """

    def test_uses_base_class_hash_source(self, tmp_path, monkeypatch):
        """hash_source should use the base class default (SHA-256 of content)."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.base import GeneratorBase

        # Skills generator should not override hash_source (uses base default)
        # If it does override, it should still produce the same result
        content = "# Test content"
        skill_path = _write_skill_file(skills_dir, "test.md", content)

        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        actual = gen.hash_source(skill_path)
        assert actual == expected

    def test_state_namespace_is_skills(self, tmp_path, monkeypatch):
        """State entries are stored under 'skills' namespace."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        assert "skills" in state["generators"]

    def test_does_not_interfere_with_other_namespaces(self, tmp_path, monkeypatch):
        """Skills state must not affect other generators' state."""
        gen, catalogs_dir, skills_dir = _make_skills_generator(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        # Pre-existing state from memory generator
        existing_state = {
            "schema_version": 1,
            "generators": {
                "memory": {
                    "last_run": "2026-04-19T10:00:00Z",
                    "source_hashes": {"memory.md": "abc123"},
                    "entry_count": 1,
                }
            },
        }
        _write_state(catalogs_dir, existing_state)
        _write_skill_file(skills_dir)

        asyncio.run(gen.run())

        state = _read_state(catalogs_dir)
        # Memory namespace must be preserved
        assert "memory" in state["generators"]
        assert state["generators"]["memory"]["source_hashes"]["memory.md"] == "abc123"
        # Skills namespace added
        assert "skills" in state["generators"]
