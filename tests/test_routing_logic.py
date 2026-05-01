"""Tests for scripts/lib/routing_logic.py — bundle and co_retrieve_for expansion."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# expand_bundles
# ---------------------------------------------------------------------------


class TestExpandBundles:
    def test_no_bundle_field_returns_picks_unchanged(self):
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "a.md"},
            {"source": "b.md"},
        ]
        assert expand_bundles(["a.md"], catalog) == ["a.md"]

    def test_picking_bundle_member_pulls_in_siblings(self):
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "voice.md", "bundle": "writing"},
            {"source": "style.md", "bundle": "writing"},
            {"source": "workflow.md", "bundle": "writing"},
            {"source": "python.md", "bundle": "code"},
        ]
        result = expand_bundles(["voice.md"], catalog)
        assert "voice.md" in result
        assert "style.md" in result
        assert "workflow.md" in result
        assert "python.md" not in result

    def test_picks_preserved_first(self):
        """Original picks come first; bundle siblings appended in catalog order."""
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "style.md", "bundle": "writing"},
            {"source": "voice.md", "bundle": "writing"},
        ]
        result = expand_bundles(["voice.md"], catalog)
        assert result[0] == "voice.md"
        assert "style.md" in result

    def test_no_duplicate_when_pick_already_in_bundle(self):
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "voice.md", "bundle": "writing"},
            {"source": "style.md", "bundle": "writing"},
        ]
        result = expand_bundles(["voice.md", "style.md"], catalog)
        assert sorted(result) == ["style.md", "voice.md"]

    def test_unknown_pick_silently_skipped(self):
        from lib.routing_logic import expand_bundles
        catalog = [{"source": "a.md"}]
        result = expand_bundles(["ghost.md"], catalog)
        # Caller is responsible for filtering unknown picks pre-expand;
        # expansion just no-ops on missing entries.
        assert result == ["ghost.md"]

    def test_excluded_entries_not_introduced(self):
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "voice.md", "bundle": "writing"},
            {"source": "style.md", "bundle": "writing"},
        ]
        result = expand_bundles(["voice.md"], catalog, excluded={"style.md"})
        assert "style.md" not in result
        assert "voice.md" in result

    def test_empty_picks_returns_empty(self):
        from lib.routing_logic import expand_bundles
        catalog = [{"source": "voice.md", "bundle": "writing"}]
        assert expand_bundles([], catalog) == []

    def test_multiple_bundles_all_expand(self):
        from lib.routing_logic import expand_bundles
        catalog = [
            {"source": "voice.md", "bundle": "writing"},
            {"source": "style.md", "bundle": "writing"},
            {"source": "py.md", "bundle": "code"},
            {"source": "go.md", "bundle": "code"},
        ]
        result = expand_bundles(["voice.md", "py.md"], catalog)
        for f in ["voice.md", "style.md", "py.md", "go.md"]:
            assert f in result


# ---------------------------------------------------------------------------
# expand_co_retrieve
# ---------------------------------------------------------------------------


class TestExpandCoRetrieve:
    def test_no_co_retrieve_returns_picks_unchanged(self):
        from lib.routing_logic import expand_co_retrieve
        catalog = [{"source": "a.md"}]
        assert expand_co_retrieve(["a.md"], catalog) == ["a.md"]

    def test_pulls_in_listed_companions(self):
        from lib.routing_logic import expand_co_retrieve
        catalog = [
            {"source": "main.md", "co_retrieve_for": ["companion.md"]},
            {"source": "companion.md"},
        ]
        result = expand_co_retrieve(["main.md"], catalog)
        assert "main.md" in result
        assert "companion.md" in result

    def test_companions_not_in_catalog_silently_skipped(self):
        """Filenames not present in the corpus are dropped (e.g. legacy 'diary' refs)."""
        from lib.routing_logic import expand_co_retrieve
        catalog = [
            {"source": "main.md", "co_retrieve_for": ["diary", "skills"]},
        ]
        result = expand_co_retrieve(["main.md"], catalog)
        assert result == ["main.md"]

    def test_excluded_companions_not_introduced(self):
        from lib.routing_logic import expand_co_retrieve
        catalog = [
            {"source": "main.md", "co_retrieve_for": ["companion.md"]},
            {"source": "companion.md"},
        ]
        result = expand_co_retrieve(["main.md"], catalog, excluded={"companion.md"})
        assert "companion.md" not in result

    def test_non_string_companions_ignored(self):
        from lib.routing_logic import expand_co_retrieve
        catalog = [
            {"source": "main.md", "co_retrieve_for": [42, None, "companion.md"]},
            {"source": "companion.md"},
        ]
        result = expand_co_retrieve(["main.md"], catalog)
        assert "companion.md" in result

    def test_non_list_co_retrieve_ignored(self):
        from lib.routing_logic import expand_co_retrieve
        catalog = [
            {"source": "main.md", "co_retrieve_for": "not-a-list"},
        ]
        result = expand_co_retrieve(["main.md"], catalog)
        assert result == ["main.md"]


# ---------------------------------------------------------------------------
# expand_picks (combined)
# ---------------------------------------------------------------------------


class TestExpandPicks:
    def test_bundle_then_co_retrieve(self):
        from lib.routing_logic import expand_picks
        catalog = [
            {"source": "voice.md", "bundle": "writing", "co_retrieve_for": ["dictionary.md"]},
            {"source": "style.md", "bundle": "writing"},
            {"source": "dictionary.md"},
        ]
        result = expand_picks(["voice.md"], catalog)
        for f in ["voice.md", "style.md", "dictionary.md"]:
            assert f in result

    def test_no_metadata_passes_picks_through(self):
        from lib.routing_logic import expand_picks
        catalog = [{"source": "a.md"}, {"source": "b.md"}]
        assert expand_picks(["a.md"], catalog) == ["a.md"]
