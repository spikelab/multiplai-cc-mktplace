"""Section-level retrieval: anchor generation, rendering, logging, lint.

The three pieces that make ``file.md#Section`` work already existed
(``lib/section_loader``, the ``load_picked_content`` calls in
``context_manager``, the router-prompt rule). What was missing was the
data — ``section_anchors`` was hand-authored and populated nowhere — so
every picked memory file was injected whole.

These tests cover the seven behaviours that keep the feature honest
rather than merely present:

1. anchors are regenerated, never preserved (a stale one must not freeze)
2. an anchor naming no real H2 degrades to the FULL file, end to end
3. a ``file.md#Section`` pick injects only that section, and the inject
   event records it
4. a bare ``file.md`` pick injects the whole file, recorded as ``[]``
5. ``router_prompt`` renders both the object and the legacy-string shape
6. ``memory_lint`` flags a duplicate H2 across files, and only then
7. a file below the anchoring thresholds gets no anchors, without error

No test here calls a live model.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conftest import run_context_hook as _run_hook, write_catalog as _write_catalog

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Big enough to clear MIN_FILE_BYTES (8 KB) with three H2 sections.
_FILLER = "Body text that exists only to push this file over the size bar. " * 60


def _big_doc(*sections: str) -> str:
    body = ["# Big Memory File", ""]
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


def _entry(catalogs_dir, source):
    catalog = json.loads((catalogs_dir / "memory.json").read_text())
    for entry in catalog["entries"]:
        if entry.get("source") == source:
            return entry
    raise AssertionError(f"no catalog entry for {source}")


def _inject_events(env_setup):
    """Every ``inject`` record the hook wrote to the activity JSONL mirror."""
    jsonl = env_setup["data_dir"] / "logs" / "activity.jsonl"
    assert jsonl.exists(), "context_manager wrote no activity log"
    records = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("event") == "inject"]


# ---------------------------------------------------------------------------
# 1. Anchors are generated, not preserved
# ---------------------------------------------------------------------------


class TestAnchorsAreGenerated:
    def test_section_anchors_not_in_hand_authored_fields(self):
        """The preserve list is the difference between working and rotting."""
        from generators.memory import _HAND_AUTHORED_FIELDS

        assert "section_anchors" not in _HAND_AUTHORED_FIELDS
        # The genuinely editorial fields stay put — this change is scoped
        # to section_anchors alone.
        for field in ("sections", "bundle", "co_retrieve_for"):
            assert field in _HAND_AUTHORED_FIELDS

    @pytest.mark.asyncio
    async def test_regeneration_overwrites_a_stale_anchor_list(self, tmp_path, monkeypatch):
        """A renamed H2 must move the anchors, not leave the old name advertised."""
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch,
            {"summary": "s", "section_anchors": [
                {"name": "Renamed Alpha", "gloss": "a"},
                {"name": "Beta", "gloss": "b"},
                {"name": "Gamma", "gloss": "c"},
            ]},
        )
        (memory_dir / "big.md").write_text(_big_doc("Renamed Alpha", "Beta", "Gamma"))

        # Seed a catalog holding an anchor list naming the OLD header.
        catalogs_dir.mkdir(parents=True, exist_ok=True)
        (catalogs_dir / "memory.json").write_text(json.dumps({
            "schema_version": "1.2.0",
            "entries": [{
                "source": "big.md",
                "summary": "old",
                "section_anchors": [{"name": "Old Alpha", "gloss": "stale"}],
            }],
        }))

        await gen.run(force=True)

        names = [a["name"] for a in _entry(catalogs_dir, "big.md")["section_anchors"]]
        assert names == ["Renamed Alpha", "Beta", "Gamma"]
        assert "Old Alpha" not in names

    @pytest.mark.asyncio
    async def test_hallucinated_anchor_name_is_dropped(self, tmp_path, monkeypatch):
        """Only names matching a real H2 survive; the rest never reach the catalog."""
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch,
            {"summary": "s", "section_anchors": [
                {"name": "Alpha", "gloss": "real"},
                {"name": "A Section That Does Not Exist", "gloss": "invented"},
                {"name": "beta", "gloss": "case-insensitive match"},
            ]},
        )
        (memory_dir / "big.md").write_text(_big_doc("Alpha", "Beta", "Gamma"))

        await gen.run(force=True)

        anchors = _entry(catalogs_dir, "big.md")["section_anchors"]
        # "beta" is canonicalised to the file's own spelling, so the emitted
        # name is byte-identical to the header the router will ask for.
        assert [a["name"] for a in anchors] == ["Alpha", "Beta"]
        assert anchors[0]["gloss"] == "real"

    def test_prompt_hands_the_model_a_closed_set_of_real_headers(self, tmp_path, monkeypatch):
        """The names come from code, so the model cannot paraphrase a header."""
        gen, memory_dir, _ = _make_generator(tmp_path, monkeypatch, {"summary": "s"})
        path = memory_dir / "big.md"
        path.write_text(_big_doc("Release Flow", "Container Runtime", "Gotchas"))

        prompt = gen.build_prompt(path)
        assert "section_anchors" in prompt
        assert "1. Release Flow" in prompt
        assert "2. Container Runtime" in prompt
        assert "3. Gotchas" in prompt


# ---------------------------------------------------------------------------
# 2. A wrong anchor falls back to the full file, end to end
# ---------------------------------------------------------------------------


class TestWrongAnchorFallsBackToFullFile:
    def test_stale_catalog_anchor_loads_the_whole_file(self, tmp_path):
        """The fallback is why this change can never make a prompt worse.

        Driven from a catalog entry, through the same
        ``_load_memory_content`` the hook calls: an anchor naming a header
        the file no longer has costs a whole-file load — i.e. exactly
        today's behaviour — never an empty injection.
        """
        from context_manager import _load_memory_content

        body = _big_doc("Alpha", "Beta", "Gamma")
        (tmp_path / "big.md").write_text(body)
        entry = {
            "source": "big.md",
            "section_anchors": [{"name": "Ghost Section", "gloss": "gone"}],
        }
        pick = f"{entry['source']}#{entry['section_anchors'][0]['name']}"

        loaded = _load_memory_content(tmp_path, [pick])
        assert loaded[pick] == body

    def test_section_loader_contract_holds_for_a_missing_anchor(self):
        """Unit-level statement of the same contract."""
        from lib.section_loader import load_picked_content

        text = _big_doc("Alpha", "Beta", "Gamma")
        name, content = load_picked_content("big.md#Ghost Section", text)
        assert name == "big.md"
        assert content == text


# ---------------------------------------------------------------------------
# 3 & 4. Injection logging records sections and per-file bytes
# ---------------------------------------------------------------------------


class TestInjectLogging:
    def test_section_pick_loads_one_section_and_is_attributed_to_its_file(self, tmp_path):
        """A ``file.md#Section`` pick loads only that section, logged per file.

        Exercises the exact chain ``main()`` runs — ``_load_memory_content``
        then ``_section_attribution`` — because the offline (token-overlap)
        router only ever emits bare filenames; emitting a fragment is the
        LLM router's job and cannot be driven without a live model. The
        companion E2E below proves both fields actually reach the log.
        """
        from context_manager import _load_memory_content, _section_attribution

        body = _big_doc("Alpha", "Beta", "Gamma")
        (tmp_path / "big.md").write_text(body)

        loaded = _load_memory_content(tmp_path, ["big.md#Beta"])
        assert "Beta content." in loaded["big.md#Beta"]
        assert "Alpha content." not in loaded["big.md#Beta"]

        sections, sizes = _section_attribution(loaded)
        # Attribution is per FILE, so the key is the bare filename even
        # though the pick carried a fragment.
        assert sections == {"big.md": ["Beta"]}
        assert 0 < sizes["big.md"] < len(body)

    def test_bare_file_pick_logs_an_empty_section_list(self, env_setup):
        body = _big_doc("Alpha", "Beta", "Gamma")
        (env_setup["memory_dir"] / "big.md").write_text(body)
        _write_catalog(
            env_setup["catalogs_dir"], "memory.json",
            [{"source": "big.md", "summary": "big",
              "intent_domains": ["software architecture decisions"]}],
        )

        out = _run_hook(env_setup, prompt="software architecture decisions")
        assert "Alpha content." in out["context"]
        assert "Gamma content." in out["context"]

        event = _inject_events(env_setup)[-1]
        # Empty list == whole file. A file with nothing loaded has no key.
        assert event["sections_by_file"] == {"big.md": []}
        assert event["bytes_by_file"]["big.md"] == len(body)
        # The pre-existing fields keep their exact shape.
        assert event["files"] == ["big.md"]
        assert event["files_by_corpus"]["memory"] == ["big.md"]

    def test_attribution_sums_two_sections_of_one_file(self):
        from context_manager import _section_attribution

        sections, sizes = _section_attribution({
            "multiplai.md#Release Flow": "a" * 100,
            "multiplai.md#Container Runtime": "b" * 50,
            "dev.md": "c" * 10,
        })
        assert sections == {
            "multiplai.md": ["Release Flow", "Container Runtime"],
            "dev.md": [],
        }
        assert sizes == {"multiplai.md": 150, "dev.md": 10}


# ---------------------------------------------------------------------------
# 5. router_prompt renders both anchor shapes
# ---------------------------------------------------------------------------


class TestRouterPromptRendering:
    def test_object_shape_renders_name_and_gloss(self):
        from lib.router_prompt import format_catalog_for_llm

        text = format_catalog_for_llm("memory", [{
            "source": "multiplai.md",
            "summary": "the stack",
            "section_anchors": [
                {"name": "Release Flow", "gloss": "dev vs runtime, release.sh"},
                {"name": "Container Runtime", "gloss": "OrbStack, no-docker rule"},
            ],
        }])
        assert "Release Flow — dev vs runtime, release.sh" in text
        assert "Container Runtime — OrbStack, no-docker rule" in text
        assert "multiplai.md#<section>" in text

    def test_legacy_string_shape_still_renders(self):
        """A catalog written by an older generator must not break the router."""
        from lib.router_prompt import format_catalog_for_llm

        text = format_catalog_for_llm("memory", [{
            "source": "legacy.md",
            "section_anchors": ["Architecture", "Decisions"],
        }])
        assert "- Architecture" in text
        assert "- Decisions" in text
        assert "—" not in text.split("Sections")[1].split("\n\n")[0]

    def test_malformed_anchor_is_skipped_not_stringified(self):
        from lib.router_prompt import format_catalog_for_llm

        text = format_catalog_for_llm("memory", [{
            "source": "odd.md",
            "section_anchors": [{"name": "Good", "gloss": "g"}, 42, {"gloss": "no name"}],
        }])
        assert "Good — g" in text
        assert "42" not in text

    def test_no_anchors_renders_no_sections_line(self):
        from lib.router_prompt import format_catalog_for_llm

        text = format_catalog_for_llm("memory", [{"source": "plain.md", "summary": "x"}])
        assert "Sections" not in text


# ---------------------------------------------------------------------------
# 6. memory_lint flags duplicate H2 names corpus-wide
# ---------------------------------------------------------------------------


class TestDuplicateH2Lint:
    def test_flags_a_duplicate_across_two_files(self, tmp_path):
        from lib.memory_lint import lint_dir

        (tmp_path / "a.md").write_text("# A\n\n## Overview\n\ntext\n\n## Alpha\n\ntext\n")
        (tmp_path / "b.md").write_text("# B\n\n## Overview\n\ntext\n\n## Beta\n\ntext\n")

        dupes = [f for f in lint_dir(tmp_path) if f.kind == "duplicate-h2"]
        assert len(dupes) == 2
        assert {f.path.name for f in dupes} == {"a.md", "b.md"}
        assert all("Overview" in f.detail for f in dupes)

    def test_silent_when_every_h2_is_unique(self, tmp_path):
        from lib.memory_lint import lint_dir

        (tmp_path / "a.md").write_text("# A\n\n## Alpha\n\ntext\n")
        (tmp_path / "b.md").write_text("# B\n\n## Beta\n\ntext\n")

        assert [f for f in lint_dir(tmp_path) if f.kind == "duplicate-h2"] == []

    def test_h2_inside_a_code_fence_is_not_a_section(self, tmp_path):
        from lib.memory_lint import lint_dir

        (tmp_path / "a.md").write_text("# A\n\n## Alpha\n\n```sh\n## Overview\n```\n")
        (tmp_path / "b.md").write_text("# B\n\n## Overview\n\ntext\n")

        assert [f for f in lint_dir(tmp_path) if f.kind == "duplicate-h2"] == []

    def test_repeat_within_one_file_is_not_a_cross_file_collision(self, tmp_path):
        from lib.memory_lint import lint_dir

        (tmp_path / "a.md").write_text("# A\n\n## Notes\n\nx\n\n## Notes\n\ny\n")

        assert [f for f in lint_dir(tmp_path) if f.kind == "duplicate-h2"] == []

    def test_findings_still_exit_nonzero(self, tmp_path, capsys):
        from lib.memory_lint import main

        (tmp_path / "a.md").write_text("# A\n\n## Overview\n\ntext\n")
        (tmp_path / "b.md").write_text("# B\n\n## Overview\n\ntext\n")

        assert main([str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "Duplicate H2 section names (2)" in out
        assert "2 duplicate-h2" in out


# ---------------------------------------------------------------------------
# 7. Below-threshold files get no anchors, and that is not an error
# ---------------------------------------------------------------------------


class TestAnchoringThresholds:
    @pytest.mark.asyncio
    async def test_two_section_file_gets_no_anchors_and_no_error(self, tmp_path, monkeypatch):
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch,
            {"summary": "s", "section_anchors": [{"name": "Alpha", "gloss": "a"}]},
        )
        (memory_dir / "small.md").write_text(_big_doc("Alpha", "Beta"))

        result = await gen.run(force=True)

        assert result.errors == []
        assert result.generated == 1
        assert "section_anchors" not in _entry(catalogs_dir, "small.md")

    @pytest.mark.asyncio
    async def test_tiny_file_with_many_sections_gets_no_anchors(self, tmp_path, monkeypatch):
        """Under ~8 KB the whole file is already one pick's worth of context."""
        gen, memory_dir, catalogs_dir = _make_generator(
            tmp_path, monkeypatch,
            {"summary": "s", "section_anchors": [{"name": "One", "gloss": "1"}]},
        )
        (memory_dir / "tiny.md").write_text(
            "# Tiny\n\n## One\n\nx\n\n## Two\n\ny\n\n## Three\n\nz\n"
        )

        result = await gen.run(force=True)

        assert result.errors == []
        assert "section_anchors" not in _entry(catalogs_dir, "tiny.md")

    def test_below_threshold_prompt_omits_the_anchor_instruction(self, tmp_path, monkeypatch):
        """No anchor block means no tokens spent asking for anchors we'd drop."""
        gen, memory_dir, _ = _make_generator(tmp_path, monkeypatch, {"summary": "s"})
        path = memory_dir / "tiny.md"
        path.write_text("# Tiny\n\n## One\n\nx\n\n## Two\n\ny\n\n## Three\n\nz\n")

        assert "section_anchors" not in gen.build_prompt(path)

    def test_anchorable_sections_uses_the_shared_h2_parser(self, tmp_path):
        from generators.memory import MemoryGenerator
        from lib.section_loader import h2_names

        text = _big_doc("Alpha", "Beta", "Gamma")
        assert MemoryGenerator.anchorable_sections(text) == h2_names(text)
        assert h2_names(text) == ["Alpha", "Beta", "Gamma"]


# ---------------------------------------------------------------------------
# Real-corpus assertion (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MULTIPLAI_ANCHOR_CORPUS"),
    reason="set MULTIPLAI_ANCHOR_CORPUS=<copy-of-memory-dir> "
           "and MULTIPLAI_ANCHOR_CATALOG=<memory.json> to check a real corpus",
)
def test_every_generated_anchor_matches_a_real_h2():
    """Every ``name`` in a generated catalog resolves to an H2 in its file.

    Opt-in because it needs a real (copied) memory corpus, which is not in
    this repo and must never be the live one. This is the assertion run
    against the regenerated catalog during release verification.
    """
    from lib.section_loader import h2_names

    corpus = Path(os.environ["MULTIPLAI_ANCHOR_CORPUS"])
    catalog = json.loads(Path(os.environ["MULTIPLAI_ANCHOR_CATALOG"]).read_text())
    checked = 0
    for entry in catalog["entries"]:
        for anchor in entry.get("section_anchors") or []:
            name = anchor["name"] if isinstance(anchor, dict) else anchor
            headers = h2_names((corpus / entry["source"]).read_text(encoding="utf-8"))
            assert name in headers, f"{entry['source']}: '{name}' is not an H2"
            checked += 1
    assert checked > 0, "catalog carries no section_anchors at all"
