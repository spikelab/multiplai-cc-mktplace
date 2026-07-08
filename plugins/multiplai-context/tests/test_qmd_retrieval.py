"""Unit tests for scripts/qmd_retrieval.py — pure functions and the
search() orchestration with an injected fake runner (no qmd needed).
"""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import qmd_retrieval as qr
from generators.config import CatalogConfig, load_catalog_config


TARGET = qr.QmdTarget(
    workspace="/ws",
    resources_dir="/ws/RESOURCES",
    collection="resources",
    mode="local",
    strategy="fused",
)


# ---------------------------------------------------------------------------
# sanitize_query
# ---------------------------------------------------------------------------


class TestSanitizeQuery:
    def test_strips_gateway_metacharacters(self):
        q = qr.sanitize_query("what's up; rm -rf | cat & `id` $(x) > out < in \"q\"")
        for ch in ";|&<>`$()'\"\\":
            assert ch not in q

    def test_flattens_newlines_and_whitespace(self):
        assert qr.sanitize_query("a\nb\r\nc   d") == "a b c d"

    def test_truncates_long_queries(self):
        assert len(qr.sanitize_query("x" * 1000)) <= qr.MAX_QUERY_CHARS

    def test_all_metachar_query_becomes_empty(self):
        assert qr.sanitize_query(";;;$()") == ""


# ---------------------------------------------------------------------------
# content_words
# ---------------------------------------------------------------------------


class TestContentWords:
    def test_drops_stopwords_and_short_words(self):
        words = qr.content_words("what is the best water filter for me?")
        assert words == ["best", "water", "filter"]

    def test_deduplicates_preserving_order(self):
        assert qr.content_words("docker docker compose docker") == ["docker", "compose"]

    def test_strips_punctuation(self):
        assert qr.content_words("kubernetes, (docker) 'helm'!") == [
            "kubernetes", "docker", "helm",
        ]


# ---------------------------------------------------------------------------
# normalize_score / to_abs_path
# ---------------------------------------------------------------------------


class TestNormalizeScore:
    def test_fraction_passthrough(self):
        assert qr.normalize_score({"score": 0.42}) == 0.42

    def test_percentage_scaled(self):
        assert qr.normalize_score({"score": 62}) == 0.62

    def test_garbage_is_zero(self):
        assert qr.normalize_score({"score": "n/a"}) == 0.0
        assert qr.normalize_score({}) == 0.0


class TestToAbsPath:
    def test_qmd_uri_maps_into_resources_dir(self):
        item = {"file": "qmd://resources/deep/dive.md"}
        assert qr.to_abs_path(item, TARGET) == "/ws/RESOURCES/deep/dive.md"

    def test_absolute_path_passthrough(self):
        assert qr.to_abs_path({"file": "/abs/x.md"}, TARGET) == "/abs/x.md"

    def test_relative_path_joined(self):
        assert qr.to_abs_path({"path": "x.md"}, TARGET) == "/ws/RESOURCES/x.md"


# ---------------------------------------------------------------------------
# interleave_ladder_steps / rrf_fuse
# ---------------------------------------------------------------------------


def _r(name: str, score: float = 0.5) -> dict:
    return {"file": f"qmd://resources/{name}", "score": score}


class TestInterleaveLadderSteps:
    def test_round_robin_by_rank(self):
        steps = [[_r("a"), _r("b")], [_r("c"), _r("d")]]
        merged = qr.interleave_ladder_steps(steps, TARGET)
        names = [i["file"].rsplit("/", 1)[1] for i in merged]
        assert names == ["a", "c", "b", "d"]

    def test_dedupes_by_file(self):
        steps = [[_r("a")], [_r("a"), _r("b")]]
        merged = qr.interleave_ladder_steps(steps, TARGET)
        names = [i["file"].rsplit("/", 1)[1] for i in merged]
        assert names == ["a", "b"]

    def test_empty_steps(self):
        assert qr.interleave_ladder_steps([], TARGET) == []


class TestRrfFuse:
    def test_doc_in_both_sources_ranks_first(self):
        vec = [_r("both", 0.6), _r("vec-only", 0.9)]
        fts = [_r("both", 0.4), _r("fts-only", 0.5)]
        fused = qr.rrf_fuse(vec, fts, TARGET)
        assert fused[0]["file"].endswith("/both")

    def test_keeps_best_score_across_sources(self):
        vec = [_r("both", 0.6)]
        fts = [_r("both", 0.4)]
        fused = qr.rrf_fuse(vec, fts, TARGET)
        assert fused[0]["score"] == 0.6

    def test_caps_at_max_results(self):
        vec = [_r(f"v{i}") for i in range(10)]
        fused = qr.rrf_fuse(vec, [], TARGET)
        assert len(fused) == qr.MAX_RESULTS

    def test_ignores_non_dict_items(self):
        fused = qr.rrf_fuse(["garbage", _r("a")], [None], TARGET)
        assert len(fused) == 1


# ---------------------------------------------------------------------------
# results_to_entries
# ---------------------------------------------------------------------------


class TestResultsToEntries:
    def test_filters_below_min_score(self):
        entries = qr.results_to_entries([_r("weak", 0.1), _r("ok", 0.5)], TARGET)
        assert [e["path"] for e in entries] == ["/ws/RESOURCES/ok"]

    def test_dedupes_chunks_of_same_doc(self):
        entries = qr.results_to_entries([_r("a", 0.9), _r("a", 0.8)], TARGET)
        assert len(entries) == 1
        assert entries[0]["score"] == 0.9

    def test_snippet_flattened_and_truncated(self):
        item = dict(_r("a", 0.9), snippet="line1\nline2  " + "x" * 1000)
        (entry,) = qr.results_to_entries([item], TARGET)
        assert "\n" not in entry["snippet"]
        assert entry["snippet"].startswith("line1 line2")
        assert len(entry["snippet"]) <= qr.SNIPPET_CHARS

    def test_title_falls_back_to_basename(self):
        (entry,) = qr.results_to_entries([_r("dir/doc.md", 0.9)], TARGET)
        assert entry["title"] == "doc.md"


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


class TestBuildArgv:
    def test_ssh_mode_shape(self):
        target = qr.QmdTarget(
            workspace="/ws", resources_dir="/ws/R", mode="ssh", ssh_host="hosty",
        )
        argv = qr.build_argv("vsearch", "hello; world", target)
        assert argv[0] == "ssh"
        assert "hosty" in argv
        remote = argv[-1]
        assert remote.startswith("cd /ws && qmd vsearch 'hello world'")
        assert ";" not in remote.split("'")[1]

    def test_ssh_mode_empty_query_is_none(self):
        target = qr.QmdTarget(workspace="/ws", resources_dir="/ws/R", mode="ssh")
        assert qr.build_argv("search", ";;;", target) is None

    def test_local_mode_without_qmd_is_none(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", "/nonexistent")
        assert qr.build_argv("search", "hello", TARGET) is None


# ---------------------------------------------------------------------------
# search() orchestration with a fake runner
# ---------------------------------------------------------------------------


def _runner_returning(mapping):
    """Fake run_qmd: mapping of subcmd -> results list (or None)."""
    calls = []

    def runner(subcmd, query, timeout, target):
        calls.append((subcmd, query))
        return mapping.get(subcmd)

    runner.calls = calls
    return runner


class TestSearch:
    def test_skips_slash_commands_and_short_prompts(self):
        runner = _runner_returning({})
        assert qr.search("/compact", TARGET, runner) == []
        assert qr.search("hi", TARGET, runner) == []
        assert runner.calls == []

    def test_skips_when_no_resources_dir(self):
        runner = _runner_returning({"vsearch": [_r("a", 0.9)]})
        target = qr.QmdTarget(workspace="/ws", resources_dir="")
        assert qr.search("a long enough prompt here", target, runner) == []

    def test_fused_strategy_merges_vec_and_fts(self):
        runner = _runner_returning({
            "vsearch": [_r("vec", 0.7)],
            "search": [_r("fts", 0.6)],
        })
        entries = qr.search("compare water filter options", TARGET, runner)
        paths = {e["path"] for e in entries}
        assert paths == {"/ws/RESOURCES/vec", "/ws/RESOURCES/fts"}

    def test_falls_back_to_ladder_when_strategy_empty(self):
        runner = _runner_returning({"vsearch": None, "search": [_r("kw", 0.5)]})
        entries = qr.search("compare water filter options", TARGET, runner)
        assert [e["path"] for e in entries] == ["/ws/RESOURCES/kw"]

    def test_hybrid_strategy_uses_query_subcommand(self):
        target = qr.QmdTarget(
            workspace="/ws", resources_dir="/ws/RESOURCES", strategy="hybrid",
        )
        runner = _runner_returning({"query": [_r("deep", 0.8)]})
        entries = qr.search("compare water filter options", target, runner)
        assert runner.calls[0][0] == "query"
        assert [e["path"] for e in entries] == ["/ws/RESOURCES/deep"]

    def test_fail_open_on_runner_exception(self):
        def exploding(subcmd, query, timeout, target):
            raise RuntimeError("boom")

        assert qr.search("compare water filter options", TARGET, exploding) == []

    def test_ladder_narrows_terms(self):
        runner = _runner_returning({"vsearch": None, "search": [_r("kw", 0.5)]})
        qr.search(
            "kubernetes deployment strategies canary rollouts production", TARGET, runner,
        )
        searches = [q for c, q in runner.calls if c == "search"]
        assert len(searches) == 3
        assert len(searches[0].split()) == 4
        assert len(searches[1].split()) == 3
        assert len(searches[2].split()) == 2


# ---------------------------------------------------------------------------
# Config plumbing (generators/config.py)
# ---------------------------------------------------------------------------


class TestQmdConfig:
    def test_defaults(self):
        cfg = CatalogConfig()
        assert cfg.resources_retrieval == "catalog"
        assert cfg.qmd_mode == "local"
        assert cfg.qmd_ssh_host == "host.docker.internal"
        assert cfg.qmd_collection == "resources"
        assert cfg.qmd_strategy == "fused"

    def test_invalid_values_fall_back(self):
        cfg = CatalogConfig(
            resources_retrieval="elasticsearch",
            qmd_mode="teleport",
            qmd_ssh_host="  ",
            qmd_collection="",
            qmd_strategy="psychic",
        )
        assert cfg.resources_retrieval == "catalog"
        assert cfg.qmd_mode == "local"
        assert cfg.qmd_ssh_host == "host.docker.internal"
        assert cfg.qmd_collection == "resources"
        assert cfg.qmd_strategy == "fused"

    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_resources_retrieval", "qmd")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_mode", "ssh")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_ssh_host", "myhost")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_collection", "notes")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_strategy", "fts")
        cfg = load_catalog_config()
        assert cfg.resources_retrieval == "qmd"
        assert cfg.qmd_mode == "ssh"
        assert cfg.qmd_ssh_host == "myhost"
        assert cfg.qmd_collection == "notes"
        assert cfg.qmd_strategy == "fts"

    def test_target_from_config_uses_cwd_as_workspace(self):
        cfg = CatalogConfig(
            resources_dir="/ws/RESOURCES", resources_retrieval="qmd", qmd_mode="ssh",
        )
        target = qr.target_from_config(cfg, "/some/project")
        assert target.workspace == "/some/project"
        assert target.resources_dir == "/ws/RESOURCES"
        assert target.mode == "ssh"
