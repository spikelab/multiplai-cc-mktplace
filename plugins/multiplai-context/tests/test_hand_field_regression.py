"""A hand-authored catalog field must never disappear on regeneration.

The failure this guards against already happened, silently, and cost weeks.
``bundle`` and ``co_retrieve_for`` are documented in ``memory/CLAUDE.md``,
consumed by ``lib/routing_logic.py`` (``expand_bundles``,
``expand_co_retrieve``) and rendered by ``lib/router_prompt.py`` rules 4/5 —
and they were absent from **all 29** memory catalog entries. Nothing errored.
``merge_entry`` preserves only what a previous entry already had, so once a
value is gone it is gone: no LLM emits these fields and no source file
contains them.

The 2026-08-05 audit
(``ARTIFACTS/memory-work-2026/00-origin/plan-memory-system-fixes-2026-08-05.md``,
item 2) asked for two tests. The first half — merge keeps an existing hand
field when the new LLM result lacks it — already lives in ``test_memory.py``.
The second half is here, and is the one that would have caught this: a
**catalog-level** assertion comparing the previous catalog against the one
about to be written.

``section_anchors`` is deliberately *not* covered. It is derived from the
file's own H2 headers, and a file that loses sections or shrinks below
``MIN_FILE_BYTES`` is supposed to lose its anchors. Guarding it would make
correct behaviour fail.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Comfortably over MIN_FILE_BYTES with three H2 sections.
_FILLER = "Body text that exists only to push this file over the size bar. " * 60


def _doc(*sections: str) -> str:
    body = ["# A Memory File", ""]
    for name in sections:
        body += [f"## {name}", "", f"{name} content. {_FILLER}", ""]
    return "\n".join(body)


def _make_generator(tmp_path, monkeypatch, response: dict):
    """MemoryGenerator wired to a temp memory dir and a canned LLM reply."""
    from generators.config import CatalogConfig
    from generators.memory import MemoryGenerator
    from multiplai_core.model_client import ModelResponse

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_DIR", str(memory_dir))

    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=json.dumps(response)))
    gen = MemoryGenerator(config=CatalogConfig(), model_client=client)
    return gen, memory_dir, tmp_path / "catalogs"


def _seed_catalog(catalogs_dir, entries):
    from generators.base import CATALOG_SCHEMA_VERSION

    catalogs_dir.mkdir(parents=True, exist_ok=True)
    (catalogs_dir / "memory.json").write_text(
        json.dumps(
            {"schema_version": CATALOG_SCHEMA_VERSION, "entries": entries}, indent=2
        )
    )


def _entries(catalogs_dir):
    return {
        e["source"]: e
        for e in json.loads((catalogs_dir / "memory.json").read_text())["entries"]
    }


# ---------------------------------------------------------------------------
# 1. The comparison itself
# ---------------------------------------------------------------------------


class TestHandFieldLossDetection:
    """``hand_field_losses`` compares two catalogs, not two entries."""

    @pytest.fixture
    def gen(self, tmp_path, monkeypatch):
        gen, _, _ = _make_generator(tmp_path, monkeypatch, {"summary": "s"})
        return gen

    def test_a_dropped_field_is_reported(self, gen):
        before = {"a.md": {"source": "a.md", "bundle": "writing"}}
        after = {"a.md": {"source": "a.md"}}

        losses = gen.hand_field_losses(before, after)

        assert len(losses) == 1
        assert "a.md" in losses[0] and "bundle" in losses[0]

    def test_a_field_emptied_to_a_bare_list_is_also_a_loss(self, gen):
        """``[]`` loses the value as completely as dropping the key does."""
        before = {"a.md": {"source": "a.md", "co_retrieve_for": ["life.md"]}}
        after = {"a.md": {"source": "a.md", "co_retrieve_for": []}}

        assert gen.hand_field_losses(before, after)

    def test_a_field_emptied_to_whitespace_is_also_a_loss(self, gen):
        before = {"a.md": {"source": "a.md", "bundle": "writing"}}
        after = {"a.md": {"source": "a.md", "bundle": "   "}}

        assert gen.hand_field_losses(before, after)

    def test_an_unchanged_catalog_reports_nothing(self, gen):
        entry = {"source": "a.md", "bundle": "writing", "co_retrieve_for": ["b.md"]}
        assert gen.hand_field_losses({"a.md": entry}, {"a.md": dict(entry)}) == []

    def test_a_field_that_was_never_populated_is_not_a_loss(self, gen):
        """Absent-then-absent is the normal state of an unauthored field."""
        before = {"a.md": {"source": "a.md"}}
        after = {"a.md": {"source": "a.md"}}

        assert gen.hand_field_losses(before, after) == []

    def test_a_pruned_entry_is_not_a_loss(self, gen):
        """Deleting the source file is supposed to remove the entry."""
        before = {"gone.md": {"source": "gone.md", "bundle": "writing"}}

        assert gen.hand_field_losses(before, {}) == []

    def test_section_anchors_are_not_guarded(self, gen):
        """Anchors are derived, so losing them can be correct behaviour."""
        from generators.memory import _HAND_AUTHORED_FIELDS

        assert "section_anchors" not in _HAND_AUTHORED_FIELDS
        assert "section_anchors" not in gen.preserved_fields

        before = {"a.md": {"source": "a.md", "section_anchors": [{"name": "X"}]}}
        after = {"a.md": {"source": "a.md"}}

        assert gen.hand_field_losses(before, after) == []

    def test_a_generator_with_no_preserved_fields_reports_nothing(self):
        """The default is opt-in: a generator pays only for what it declares."""
        from generators.base import GeneratorBase

        base = GeneratorBase(config=None, model_client=None)
        assert base.preserved_fields == ()
        assert base.hand_field_losses(
            {"a.md": {"bundle": "writing"}}, {"a.md": {}}
        ) == []

    def test_the_memory_generator_guards_every_field_it_promises(self, gen):
        """The preserve list and the guard list must not drift apart."""
        from generators.memory import _HAND_AUTHORED_FIELDS

        assert tuple(gen.preserved_fields) == tuple(_HAND_AUTHORED_FIELDS)


# ---------------------------------------------------------------------------
# 2. The guard fires on a real regeneration
# ---------------------------------------------------------------------------


class TestGuardFiresOnRegeneration:
    """End-to-end: a run that drops a hand field must not exit quietly."""

    @pytest.mark.asyncio
    async def test_a_normal_regeneration_keeps_the_fields_and_stays_silent(
        self, tmp_path, monkeypatch
    ):
        """Positive control: without this, every assertion below is vacuous."""
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch, {"summary": "fresh", "topics": ["t"]}
        )
        (memory_dir / "core-voice.md").write_text(_doc("Tone", "Boundaries", "Checks"))
        _seed_catalog(
            catalogs_dir,
            [
                {
                    "source": "core-voice.md",
                    "summary": "stale",
                    "bundle": "writing",
                    "co_retrieve_for": ["blog-style-guide.md"],
                }
            ],
        )

        result = await gen.run(force=True)

        assert result.errors == []
        entry = _entries(catalogs_dir)["core-voice.md"]
        assert entry["bundle"] == "writing"
        assert entry["co_retrieve_for"] == ["blog-style-guide.md"]
        assert entry["summary"] == "fresh"  # LLM fields still refresh

    @pytest.mark.asyncio
    async def test_a_regeneration_that_drops_a_hand_field_reports_an_error(
        self, tmp_path, monkeypatch, caplog
    ):
        """Break preservation and the run must say so, loudly and by name.

        This is the assertion the 2026-08-05 audit asked for. Preservation is
        disabled here to stand in for whatever actually emptied the live
        catalog — a from-scratch rebuild, a schema migration, a corrupt-file
        read that returned ``{"entries": []}``. The guard is deliberately
        indifferent to the cause: it compares the catalog on disk with the one
        about to replace it.
        """
        import logging

        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch, {"summary": "fresh"}
        )
        (memory_dir / "core-voice.md").write_text(_doc("Tone", "Boundaries", "Checks"))
        _seed_catalog(
            catalogs_dir,
            [
                {
                    "source": "core-voice.md",
                    "summary": "stale",
                    "bundle": "writing",
                    "co_retrieve_for": ["blog-style-guide.md"],
                }
            ],
        )

        # The defect, reproduced: merge stops carrying hand-authored fields.
        monkeypatch.setattr(
            type(gen),
            "merge_entry",
            lambda self, existing, new: dict(new),
        )

        with caplog.at_level(logging.ERROR):
            result = await gen.run(force=True)

        assert result.errors, "a dropped hand field produced no error"
        joined = " | ".join(result.errors)
        assert "bundle" in joined
        assert "co_retrieve_for" in joined
        assert "core-voice.md" in joined
        assert any("HAND_FIELD_LOST" in r.message for r in caplog.records), (
            "a dropped hand field produced no log line"
        )

    @pytest.mark.asyncio
    async def test_an_entry_pruned_for_a_deleted_source_is_not_flagged(
        self, tmp_path, monkeypatch
    ):
        """Removing a memory file is a normal thing to do."""
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch, {"summary": "fresh"}
        )
        _seed_catalog(
            catalogs_dir,
            [{"source": "deleted.md", "summary": "s", "bundle": "writing"}],
        )
        # Record a hash for the now-missing source so run() prunes it.
        (catalogs_dir / ".generation-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generators": {
                        "memory": {"source_hashes": {"deleted.md": "abc"}}
                    },
                }
            )
        )

        result = await gen.run(force=True)

        assert result.pruned == 1
        assert result.errors == []


# ---------------------------------------------------------------------------
# 3. The other generator that makes the same promise
# ---------------------------------------------------------------------------


class TestSkillsGeneratorIsGuardedToo:
    """``SkillsGenerator`` preserves fields as well, so it must be covered.

    Any generator that overrides ``merge_entry`` to keep editorial fields owes
    the guard a ``preserved_fields`` declaration — otherwise it inherits the
    exact silence this test file exists to end.
    """

    def test_skills_declares_what_it_preserves(self):
        from generators import skills as skills_mod

        assert (
            tuple(skills_mod.SkillsGenerator.preserved_fields)
            == tuple(skills_mod._HAND_AUTHORED_FIELDS)
        )

    def test_every_generator_that_overrides_merge_entry_declares_its_fields(self):
        """A source-level check, so a new generator cannot forget quietly."""
        from generators.base import GeneratorBase
        from generators.memory import MemoryGenerator
        from generators.skills import SkillsGenerator

        for cls in (MemoryGenerator, SkillsGenerator):
            overrides = cls.merge_entry is not GeneratorBase.merge_entry
            assert overrides, f"{cls.__name__} no longer overrides merge_entry"
            assert cls.preserved_fields, (
                f"{cls.__name__} preserves hand fields but declares none, so "
                "hand_field_losses cannot guard it"
            )
