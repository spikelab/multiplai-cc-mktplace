"""Bounds on what one prompt can inject, and a warn-once that survives processes.

Four defects, all measured in the real logs (2026-10-10 → 10-20):

1. **No total byte cap on router picks.** 21 prompts got >100 KB of injected
   context, the largest 708,504 bytes. The only cap applied to the recency
   fallback. Now every memory injection is held to ``memory_budget_bytes``,
   dropping the lowest-ranked picks first.
2. **A section pick whose anchor matched no heading loaded the whole file**
   (22 times). Now the nearest heading is used when one is close enough, and
   otherwise the pick is dropped. Never the whole file.
3. **A bare pick of a very large file loaded all of it** (claude-code-tools.md
   133 KB, ai-agent-patterns.md 125 KB). Files over ``whole_file_max_bytes``
   now load only by section; a bare pick gets a pointer: path, preamble and
   section list.
4. **"Catalog X is stale" logged on every prompt** (~930 times in 11 days).
   The warn-once set lived in process memory and each prompt is a new
   process. It now persists in the data dir, keyed by the catalog's mtime.

No test here calls a live model.
"""

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import write_catalog

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _caplog_sees_context_manager(monkeypatch):
    """``setup_logging`` turns propagation off, which hides records from caplog.

    Other files' caplog tests pass only after something earlier in the run
    has turned it back on; this file must pass on its own.
    """
    import context_manager

    monkeypatch.setattr(context_manager.logger, "propagate", True)


_FILLER = "Body text that exists only to push this file over the size bar. " * 60


def _doc(*sections: str, preamble: str = "**Purpose:** test file.") -> str:
    body = ["# Memory File", "", preamble, ""]
    for name in sections:
        body += [f"## {name}", "", f"{name} content. {_FILLER}", ""]
    return "\n".join(body)


# ---------------------------------------------------------------------------
# 2a. Heading resolution (unit)
# ---------------------------------------------------------------------------


class TestResolveSectionName:
    """``resolve_section_name`` maps a router fragment to a real H2, or None."""

    HEADINGS = [
        "Distribution",
        "Kit Repo",
        "GCP — IAP, SSH & Contractor Access",
        "Cloud SQL IAM Auth Constraints",
        "Code Quality — Tooling & UI Preferences",
        "Development Workflow — Debugging & Gotchas",
    ]

    def _text(self):
        return _doc(*self.HEADINGS)

    def _resolve(self, fragment):
        from lib.section_loader import resolve_section_name

        return resolve_section_name(self._text(), fragment)

    def test_exact_match_ignores_case_and_whitespace(self):
        assert self._resolve("  kit repo ") == "Kit Repo"

    def test_punctuation_does_not_matter(self):
        assert (
            self._resolve("code quality - tooling & ui preferences")
            == "Code Quality — Tooling & UI Preferences"
        )

    def test_a_gloss_appended_after_a_dash_is_cut_off(self):
        """Seen in the logs: the router pasted the anchor's gloss into the pick."""
        assert (
            self._resolve("Distribution — Release chain, marketplace tags, SECURITY.md…")
            == "Distribution"
        )
        assert (
            self._resolve(
                "Cloud SQL IAM Auth Constraints — IAM auth token limits, hardening order"
            )
            == "Cloud SQL IAM Auth Constraints"
        )

    def test_a_dropped_qualifier_prefix_still_resolves(self):
        """Seen in the logs: ``Tooling & UI Preferences`` for the qualified H2."""
        assert (
            self._resolve("Tooling & UI Preferences")
            == "Code Quality — Tooling & UI Preferences"
        )

    def test_a_near_spelling_resolves_by_similarity(self):
        """Seen in the logs: ``IAM`` for ``IAP`` with the ``GCP —`` prefix lost."""
        assert (
            self._resolve("IAM, SSH & Contractor Access")
            == "GCP — IAP, SSH & Contractor Access"
        )

    def test_an_unrelated_name_resolves_to_nothing(self):
        assert self._resolve("Screen-Demo Pipeline") is None
        assert self._resolve("Host-Container Networking & OrbStack (canonical)") is None

    def test_an_ambiguous_suffix_resolves_to_nothing(self):
        from lib.section_loader import resolve_section_name

        text = _doc("Alpha — Release Notes", "Beta — Release Notes")
        assert resolve_section_name(text, "Release Notes") is None

    def test_a_single_word_is_not_matched_as_a_suffix(self):
        """One shared word is too weak a signal to pick a section on."""
        from lib.section_loader import resolve_section_name

        text = _doc("Team Notes", "Other")
        assert resolve_section_name(text, "Notes") is None

    def test_a_file_without_h2_resolves_to_nothing(self):
        from lib.section_loader import resolve_section_name

        assert resolve_section_name("# Title\n\nno sections\n", "Anything") is None


# ---------------------------------------------------------------------------
# 2b. Anchor misses never load the whole file
# ---------------------------------------------------------------------------


class TestAnchorMissIsNeverTheWholeFile:
    def test_an_unresolvable_anchor_is_dropped(self, tmp_path, caplog):
        from context_manager import _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        with caplog.at_level(logging.WARNING):
            loaded = _load_memory_content(tmp_path, ["big.md#Ghost Section"])

        assert loaded == {}
        assert "big.md#Ghost Section" in caplog.text
        assert "dropped" in caplog.text

    def test_a_near_anchor_loads_the_nearest_section_only(self, tmp_path, caplog):
        from context_manager import _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta Release", "Gamma"))
        with caplog.at_level(logging.INFO):
            loaded = _load_memory_content(
                tmp_path, ["big.md#beta release — what ships and when"]
            )

        assert list(loaded) == ["big.md#Beta Release"]
        content = loaded["big.md#Beta Release"]
        assert "Beta Release content." in content
        assert "Alpha content." not in content
        assert "**Purpose:** test file." in content  # preamble kept
        assert "Beta Release" in caplog.text

    def test_a_stale_anchor_does_not_absorb_a_good_one(self, tmp_path):
        from context_manager import _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        loaded = _load_memory_content(tmp_path, ["big.md#Beta", "big.md#Gone"])

        assert list(loaded) == ["big.md#Beta"]
        assert "Alpha content." not in loaded["big.md#Beta"]

    def test_two_picks_resolving_to_one_heading_load_it_once(self, tmp_path):
        from context_manager import _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta Release", "Gamma"))
        loaded = _load_memory_content(
            tmp_path, ["big.md#Beta Release", "big.md#beta release — gloss"]
        )
        assert list(loaded) == ["big.md#Beta Release"]

    def test_a_resolved_alias_is_stamped_for_cooldown(self, tmp_path):
        """The router will emit the same misspelt pick next turn; it must match."""
        from context_manager import MemoryLoadReport, _cooldown_keys, _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta Release", "Gamma"))
        picks = ["big.md#beta release — gloss"]
        report = MemoryLoadReport()
        loaded = _load_memory_content(tmp_path, picks, report=report)

        keys = set(_cooldown_keys(picks, loaded, report.aliases))
        assert keys == {"big.md#Beta Release", "big.md#beta release — gloss"}

    def test_dropped_anchors_are_reported(self, tmp_path):
        from context_manager import MemoryLoadReport, _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        report = MemoryLoadReport()
        _load_memory_content(tmp_path, ["big.md#Gone"], report=report)
        assert report.dropped_anchors == ["big.md#Gone"]


# ---------------------------------------------------------------------------
# 3. Large files load only by section
# ---------------------------------------------------------------------------


class TestLargeFilesLoadOnlyBySection:
    def test_a_bare_pick_of_a_large_file_injects_a_pointer(self, tmp_path):
        from context_manager import MemoryLoadReport, _load_memory_content

        body = _doc("Alpha", "Beta", "Gamma")
        (tmp_path / "big.md").write_text(body)
        report = MemoryLoadReport()

        loaded = _load_memory_content(
            tmp_path, ["big.md"], whole_file_max_bytes=1000, report=report
        )

        pointer = loaded["big.md"]
        assert str(tmp_path / "big.md") in pointer
        for name in ("Alpha", "Beta", "Gamma"):
            assert f"- {name}" in pointer
        assert "**Purpose:** test file." in pointer
        assert "Alpha content." not in pointer
        assert len(pointer) < 1000
        assert report.pointers == ["big.md"]

    def test_a_small_file_still_loads_whole(self, tmp_path):
        from context_manager import _load_memory_content

        body = _doc("Alpha", "Beta", "Gamma")
        (tmp_path / "big.md").write_text(body)

        loaded = _load_memory_content(
            tmp_path, ["big.md"], whole_file_max_bytes=len(body.encode()) + 1
        )
        assert loaded == {"big.md": body}

    def test_section_picks_of_a_large_file_replace_its_bare_pick(self, tmp_path):
        from context_manager import MemoryLoadReport, _load_memory_content

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        report = MemoryLoadReport()

        loaded = _load_memory_content(
            tmp_path, ["big.md", "big.md#Beta"],
            whole_file_max_bytes=1000, report=report,
        )
        assert list(loaded) == ["big.md#Beta"]
        assert report.pointers == []

    def test_a_large_file_with_no_sections_gets_a_pointer(self, tmp_path):
        from context_manager import _load_memory_content

        body = "# Flat\n\n" + _FILLER
        (tmp_path / "flat.md").write_text(body)

        loaded = _load_memory_content(
            tmp_path, ["flat.md#Anything"], whole_file_max_bytes=1000
        )
        assert str(tmp_path / "flat.md") in loaded["flat.md"]
        assert _FILLER not in loaded["flat.md"]

    def test_the_default_threshold_applies_without_an_argument(self, tmp_path):
        from context_manager import DEFAULT_WHOLE_FILE_MAX_BYTES, _load_memory_content

        sections = [f"Section {i}" for i in range(DEFAULT_WHOLE_FILE_MAX_BYTES // 3000)]
        body = _doc(*sections)
        assert len(body.encode()) > DEFAULT_WHOLE_FILE_MAX_BYTES
        (tmp_path / "huge.md").write_text(body)

        loaded = _load_memory_content(tmp_path, ["huge.md"])
        assert "Section 0 content." not in loaded["huge.md"]

    def test_a_pointer_is_not_attributed_as_a_whole_file_load(self, tmp_path):
        """Utilisation reads ``sections_by_file == []`` as "the whole file"."""
        from context_manager import (
            MemoryLoadReport, _load_memory_content, _section_attribution,
        )

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        (tmp_path / "small.md").write_text("# Small\n\nsmall body\n")
        report = MemoryLoadReport()
        loaded = _load_memory_content(
            tmp_path, ["big.md", "small.md"],
            whole_file_max_bytes=1000, report=report,
        )

        sections, sizes = _section_attribution(loaded, pointers=report.pointers)
        assert sections == {"small.md": []}
        assert set(sizes) == {"small.md"}


# ---------------------------------------------------------------------------
# 1. Total byte budget, lowest rank dropped first
# ---------------------------------------------------------------------------


class TestMemoryBudget:
    def test_keeps_the_rank_order_prefix_that_fits(self, tmp_path, caplog):
        from context_manager import _apply_memory_budget

        content = {
            "a.md": "a" * 10_000,
            "b.md": "b" * 10_000,
            "c.md": "c" * 30_000,
            "d.md": "d" * 1_000,   # fits, but ranks below c: dropped too
        }
        with caplog.at_level(logging.INFO):
            kept, dropped = _apply_memory_budget(tmp_path, content, 25_000)

        assert list(kept) == ["a.md", "b.md"]
        assert dropped == ["c.md", "d.md"]
        assert "c.md" in caplog.text and "d.md" in caplog.text

    def test_measures_utf8_bytes_not_characters(self, tmp_path):
        from context_manager import _apply_memory_budget

        # 700 characters fit in 1,000; 1,100 bytes do not.
        content = {"a.md": "é" * 400, "b.md": "x" * 300}
        kept, dropped = _apply_memory_budget(tmp_path, content, 1_000)
        assert dropped == ["b.md"]
        assert list(kept) == ["a.md"]

    def test_an_oversized_top_pick_becomes_a_pointer(self, tmp_path):
        """The best match must not vanish just because it is long."""
        from context_manager import _apply_memory_budget

        (tmp_path / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        content = {"big.md#Beta": "x" * 5_000, "small.md": "y" * 10}
        pointers: list[str] = []
        kept, dropped = _apply_memory_budget(
            tmp_path, content, 2_000, pointers=pointers
        )

        assert "x" * 100 not in kept["big.md#Beta"]
        assert str(tmp_path / "big.md") in kept["big.md#Beta"]
        assert "## Beta" in kept["big.md#Beta"]
        assert pointers == ["big.md#Beta"]
        # Nothing ranked above it was dropped, so what still fits stays.
        assert list(kept) == ["big.md#Beta", "small.md"]
        assert dropped == []

    def test_an_oversized_top_pick_with_no_file_is_dropped(self, tmp_path):
        from context_manager import _apply_memory_budget

        kept, dropped = _apply_memory_budget(
            tmp_path, {"gone.md": "x" * 5_000, "b.md": "y"}, 2_000
        )
        assert kept == {"b.md": "y"} and dropped == ["gone.md"]

    def test_everything_fits_nothing_changes(self, tmp_path):
        from context_manager import _apply_memory_budget

        content = {"a.md": "a" * 10, "b.md": "b" * 10}
        kept, dropped = _apply_memory_budget(tmp_path, content, 1_000)
        assert kept == content and dropped == []

    def test_zero_disables_the_budget(self, tmp_path):
        from context_manager import _apply_memory_budget

        content = {"a.md": "a" * 100_000}
        kept, dropped = _apply_memory_budget(tmp_path, content, 0)
        assert kept == content and dropped == []


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class TestOptions:
    @pytest.mark.parametrize(
        "name,default",
        [("memory_budget_bytes", 40_000), ("whole_file_max_bytes", 20_000)],
    )
    def test_declared_in_the_manifest(self, name, default):
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text()
        )
        opt = manifest["userConfig"][name]
        assert opt["type"] == "number"
        assert opt["default"] == default
        assert opt["description"]

    def test_loaded_with_defaults(self, monkeypatch):
        from generators.config import load_catalog_config

        cfg = load_catalog_config()
        assert cfg.memory_budget_bytes == 40_000
        assert cfg.whole_file_max_bytes == 20_000

    def test_loaded_from_the_environment(self, monkeypatch):
        from generators.config import load_catalog_config

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_BUDGET_BYTES", "12345")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_WHOLE_FILE_MAX_BYTES", "6789")
        cfg = load_catalog_config()
        assert cfg.memory_budget_bytes == 12345
        assert cfg.whole_file_max_bytes == 6789

    def test_negative_values_fall_back_to_defaults(self):
        from generators.config import CatalogConfig

        cfg = CatalogConfig(memory_budget_bytes=-1, whole_file_max_bytes=-5)
        assert cfg.memory_budget_bytes == 40_000
        assert cfg.whole_file_max_bytes == 20_000


# ---------------------------------------------------------------------------
# The hook body, end to end with a scripted router
# ---------------------------------------------------------------------------


class _StubRun:
    @contextmanager
    def stage(self, _name):
        yield

    def note(self, **kwargs):
        self.notes = {**getattr(self, "notes", {}), **kwargs}


class _ScriptedRouter:
    name = "scripted"
    last_scores = None

    def __init__(self, picks):
        self._picks = picks

    def select_multi(self, prompt, last_response, corpora):
        return {"memory": list(self._picks), "skills": [], "resources": []}


@pytest.fixture
def hook_env(env_setup, monkeypatch):
    from multiplai_core.paths import _reset_cache

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(env_setup["data_dir"]))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_DIR", str(env_setup["memory_dir"]))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_ENABLE_DEV_REFERENCES", "false")
    _reset_cache()
    yield env_setup
    _reset_cache()


def _run_body(monkeypatch, capsys, picks, catalog_sources):
    import context_manager

    env = context_manager.get_paths()
    write_catalog(
        env.catalogs_dir(), "memory.json",
        [{"source": s, "summary": s} for s in catalog_sources],
    )
    monkeypatch.setattr(
        context_manager, "create_router", lambda **_kw: _ScriptedRouter(picks)
    )
    run = _StubRun()
    context_manager._assemble_and_emit({"prompt": "anything at all"}, run)
    out = json.loads(capsys.readouterr().out)
    return out["hookSpecificOutput"]["additionalContext"], run.notes


class TestHookBody:
    def test_the_budget_holds_on_the_router_path(self, hook_env, monkeypatch, capsys):
        mem = hook_env["memory_dir"]
        names = [f"f{i}.md" for i in range(6)]
        for i, name in enumerate(names):
            # ~4 KB each, all under the whole-file limit
            (mem / name).write_text(f"# F{i}\n\nmarker-{i} " + "z" * 4000)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_BUDGET_BYTES", "10000")

        context, notes = _run_body(monkeypatch, capsys, names, names)

        assert "marker-0" in context and "marker-1" in context
        assert "marker-2" not in context and "marker-5" not in context
        assert notes["bytes"] < 12_000

    def test_dropped_anchors_do_not_trigger_the_recency_dump(
        self, hook_env, monkeypatch, capsys
    ):
        """A dropped pick is a decision, not a router failure."""
        mem = hook_env["memory_dir"]
        (mem / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        (mem / "recent.md").write_text("# Recent\n\nrecency-net content\n")

        context, notes = _run_body(
            monkeypatch, capsys, ["big.md#Ghost"], ["big.md", "recent.md"]
        )

        assert context == ""
        assert "recency-net content" not in context
        assert notes["outcome"] == "anchor_miss"

    def test_a_bare_pick_of_a_large_file_reaches_the_context_as_a_pointer(
        self, hook_env, monkeypatch, capsys
    ):
        mem = hook_env["memory_dir"]
        (mem / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_WHOLE_FILE_MAX_BYTES", "1000")

        context, _ = _run_body(monkeypatch, capsys, ["big.md"], ["big.md"])

        assert str(mem / "big.md") in context
        assert "Alpha content." not in context

        logs = sorted((hook_env["data_dir"] / "logs").glob("activity-*.jsonl"))
        events = [json.loads(l) for f in logs for l in f.read_text().splitlines()]
        inject = [e for e in events if e.get("event") == "inject"][-1]
        assert inject["pointers"] == ["big.md"]
        assert inject["sections_by_file"] == {}


# ---------------------------------------------------------------------------
# 4. Warn-once survives the process
# ---------------------------------------------------------------------------


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "plugin_data"
    (data_dir / "catalogs").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    _reset_cache()
    import context_manager

    # The autouse conftest fixture points the state file at a per-test temp
    # path; this test needs the real data-dir location.
    monkeypatch.setattr(
        context_manager, "_warn_state_path",
        lambda: data_dir / context_manager._WARN_ONCE_FILE,
    )
    yield data_dir
    _reset_cache()


def _stale_catalog(data_dir: Path, name: str = "skills.json") -> Path:
    from generators.base import CATALOG_SCHEMA_VERSION

    path = data_dir / "catalogs" / name
    path.write_text(json.dumps({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2020-01-01T00:00:00Z",
        "entries": [{"name": "x", "summary": "x"}],
    }))
    return path


def _new_process():
    """What the next prompt's hook process starts with: an empty set."""
    import context_manager

    context_manager._catalog_warnings_emitted.clear()


def _stale_warnings(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "stale" in r.getMessage()
    ]


class TestWarnOnceAcrossProcesses:
    def test_a_second_process_does_not_repeat_the_warning(self, data_env, caplog):
        import context_manager

        _stale_catalog(data_env)
        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)
            _new_process()
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)

        assert len(_stale_warnings(caplog)) == 1
        assert (data_env / context_manager._WARN_ONCE_FILE).exists()

    def test_a_regenerated_catalog_warns_again(self, data_env, caplog):
        import context_manager

        path = _stale_catalog(data_env)
        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)
            _new_process()
            # Rewritten, still stale: a different file, so a new warning.
            _stale_catalog(data_env)
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)

        assert len(_stale_warnings(caplog)) == 2

    def test_the_warning_repeats_after_the_window(self, data_env, caplog, monkeypatch):
        import context_manager

        _stale_catalog(data_env)
        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)
            _new_process()
            later = time.time() + context_manager._WARN_ONCE_WINDOW_S + 60
            monkeypatch.setattr(context_manager.time, "time", lambda: later)
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)

        assert len(_stale_warnings(caplog)) == 2

    def test_a_corrupt_state_file_still_warns(self, data_env, caplog):
        import context_manager

        _stale_catalog(data_env)
        (data_env / context_manager._WARN_ONCE_FILE).write_text("{not json")
        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)

        assert len(_stale_warnings(caplog)) == 1

    def test_the_remedy_names_the_qualified_skill_and_the_catalog(
        self, data_env, caplog
    ):
        """``--only skills`` runs even when the shell lacks enable_skills."""
        import context_manager

        _stale_catalog(data_env)
        with caplog.at_level(logging.WARNING):
            context_manager._read_catalog_or_scan("skills", ttl_hours=168)

        (message,) = _stale_warnings(caplog)
        assert "/multiplai-context:refresh-catalogs --only skills" in message

    def test_anchor_warnings_persist_too(self, data_env, tmp_path, caplog):
        import context_manager

        mem = tmp_path / "mem"
        mem.mkdir()
        (mem / "big.md").write_text(_doc("Alpha", "Beta", "Gamma"))
        with caplog.at_level(logging.WARNING):
            context_manager._load_memory_content(mem, ["big.md#Ghost"])
            _new_process()
            context_manager._load_memory_content(mem, ["big.md#Ghost"])

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
