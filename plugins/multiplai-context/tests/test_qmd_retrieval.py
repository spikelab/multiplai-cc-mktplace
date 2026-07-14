"""Unit tests for scripts/qmd_retrieval.py — pure functions and the
search() orchestration with an injected fake runner (no qmd needed).
"""

import json
import sqlite3
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

    def test_chunk_line_included_when_present(self):
        (entry,) = qr.results_to_entries([dict(_r("a", 0.9), line=42)], TARGET)
        assert entry["line"] == 42

    def test_chunk_line_omitted_when_missing_or_invalid(self):
        for item in (_r("a", 0.9), dict(_r("b", 0.9), line="5"),
                     dict(_r("c", 0.9), line=0), dict(_r("d", 0.9), line=True)):
            (entry,) = qr.results_to_entries([item], TARGET)
            assert "line" not in entry


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


class TestBuildArgv:
    def test_ssh_mode_shape(self):
        target = qr.QmdTarget(
            workspace="/ws", resources_dir="/ws/R", mode="ssh", ssh_host="hosty",
            collection="resources",
        )
        argv = qr.build_argv("vsearch", "hello; world", target)
        assert argv[0] == "ssh"
        assert "hosty" in argv
        remote = argv[-1]
        # workspace and collection are single-quoted alongside the query.
        assert remote.startswith("cd '/ws' && qmd vsearch 'hello world'")
        assert "-c 'resources'" in remote
        assert ";" not in remote.split("'")[1]

    def test_ssh_mode_empty_query_is_none(self):
        target = qr.QmdTarget(workspace="/ws", resources_dir="/ws/R", mode="ssh")
        assert qr.build_argv("search", ";;;", target) is None

    def test_ssh_workspace_with_spaces_is_quoted(self):
        """A legitimate path with a space survives via single-quoting."""
        target = qr.QmdTarget(
            workspace="/home/My Project", resources_dir="/r", mode="ssh",
        )
        argv = qr.build_argv("search", "hello", target)
        assert argv is not None
        assert argv[-1].startswith("cd '/home/My Project' && ")

    def test_ssh_unsafe_workspace_refused(self):
        """A workspace with shell metacharacters yields no command (fail-open)."""
        for bad_ws in ("/ws; rm -rf /", "/ws$(id)", "/ws`whoami`", "/ws'x", "/ws\nx"):
            target = qr.QmdTarget(workspace=bad_ws, resources_dir="/r", mode="ssh")
            assert qr.build_argv("search", "hello", target) is None, (
                f"unsafe workspace must be refused: {bad_ws!r}"
            )

    def test_ssh_unsafe_collection_refused(self):
        """A collection with shell metacharacters yields no command."""
        for bad_col in ("res;rm", "res$(x)", "res`x`", "res'x"):
            target = qr.QmdTarget(
                workspace="/ws", resources_dir="/r", mode="ssh", collection=bad_col,
            )
            assert qr.build_argv("search", "hello", target) is None, (
                f"unsafe collection must be refused: {bad_col!r}"
            )

    def test_gateway_safe_helper(self):
        assert qr._gateway_safe("/home/user/project")
        assert qr._gateway_safe("resources")
        assert qr._gateway_safe("with spaces ok")
        assert not qr._gateway_safe("has;semicolon")
        assert not qr._gateway_safe("has$dollar")
        assert not qr._gateway_safe("has'quote")
        assert not qr._gateway_safe("has\nnewline")

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
        assert cfg.qmd_http_url == "http://host.docker.internal:8181"
        assert cfg.qmd_candidate_limit == 10
        assert cfg.qmd_min_score == 0.30

    def test_http_is_a_valid_mode(self):
        assert CatalogConfig(qmd_mode="http").qmd_mode == "http"

    def test_invalid_values_fall_back(self):
        cfg = CatalogConfig(
            resources_retrieval="elasticsearch",
            qmd_mode="teleport",
            qmd_ssh_host="  ",
            qmd_collection="",
            qmd_strategy="psychic",
            qmd_http_url="   ",
            qmd_candidate_limit=0,
            qmd_min_score=1.7,
        )
        assert cfg.resources_retrieval == "catalog"
        assert cfg.qmd_mode == "local"
        assert cfg.qmd_ssh_host == "host.docker.internal"
        assert cfg.qmd_collection == "resources"
        assert cfg.qmd_strategy == "fused"
        assert cfg.qmd_http_url == "http://host.docker.internal:8181"
        assert cfg.qmd_candidate_limit == 10
        assert cfg.qmd_min_score == 0.30

    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_resources_retrieval", "qmd")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_mode", "http")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_ssh_host", "myhost")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_collection", "notes")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_strategy", "fts")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_http_url", "http://host.docker.internal:9000")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_candidate_limit", "20")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_qmd_min_score", "0.5")
        cfg = load_catalog_config()
        assert cfg.resources_retrieval == "qmd"
        assert cfg.qmd_mode == "http"
        assert cfg.qmd_ssh_host == "myhost"
        assert cfg.qmd_collection == "notes"
        assert cfg.qmd_strategy == "fts"
        assert cfg.qmd_http_url == "http://host.docker.internal:9000"
        assert cfg.qmd_candidate_limit == 20
        assert cfg.qmd_min_score == 0.5

    def test_target_from_config_uses_cwd_as_workspace(self):
        cfg = CatalogConfig(
            resources_dir="/ws/RESOURCES", resources_retrieval="qmd", qmd_mode="http",
            qmd_http_url="http://h:1", qmd_candidate_limit=7, qmd_min_score=0.4,
        )
        target = qr.target_from_config(cfg, "/some/project")
        assert target.workspace == "/some/project"
        assert target.resources_dir == "/ws/RESOURCES"
        assert target.mode == "http"
        assert target.http_url == "http://h:1"
        assert target.candidate_limit == 7
        assert target.min_score == 0.4


# ---------------------------------------------------------------------------
# http mode: query authoring (flatten_query / doc_frequencies /
# lexical_terms / build_searches) and http_search / search dispatch
# ---------------------------------------------------------------------------


def _make_index(workspace: Path, docs: list[str]) -> None:
    """Write a minimal qmd-shaped index at <workspace>/.qmd/index.sqlite.

    Only what doc_frequencies() touches: a `documents_fts` FTS5 table whose
    `body` column holds each doc's text (porter/unicode61, like real qmd).
    """
    qdir = workspace / ".qmd"
    qdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(qdir / "index.sqlite")
    conn.execute(
        "CREATE VIRTUAL TABLE documents_fts USING fts5("
        "filepath, title, body, tokenize='porter unicode61')"
    )
    conn.executemany(
        "INSERT INTO documents_fts (filepath, title, body) VALUES (?, ?, ?)",
        [(f"doc{i}.md", f"doc {i}", body) for i, body in enumerate(docs)],
    )
    conn.commit()
    conn.close()


HTTP_TARGET = qr.QmdTarget(
    workspace="/ws", resources_dir="/ws/RESOURCES", mode="http",
    http_url="http://host.docker.internal:8181", collection="resources",
)


class TestFlattenQuery:
    def test_collapses_whitespace_keeps_metacharacters(self):
        # Unlike sanitize_query, http mode has no shell — nothing is stripped.
        assert qr.flatten_query("what's  this?\n(a test)") == "what's this? (a test)"

    def test_truncates(self):
        assert len(qr.flatten_query("x " * 1000)) <= qr.MAX_QUERY_CHARS

    def test_empty(self):
        assert qr.flatten_query("") == ""
        assert qr.flatten_query(None) == ""


class TestDocFrequencies:
    def test_counts_documents_per_term(self, tmp_path):
        _make_index(tmp_path, ["coffee arabica gesha", "more learning here",
                               "learn more about things", "learning more today"])
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        dfs = qr.doc_frequencies(["coffee", "more", "learn"], target)
        assert dfs is not None
        assert dfs["coffee"] == 1
        assert dfs["more"] == 3
        # porter stemming folds learn/learning together
        assert dfs["learn"] == 3

    def test_missing_index_returns_none(self):
        target = qr.QmdTarget(workspace="/nonexistent", resources_dir="/r", mode="http")
        assert qr.doc_frequencies(["coffee"], target) is None

    def test_empty_terms_returns_empty(self):
        assert qr.doc_frequencies([], HTTP_TARGET) == {}

    def test_fts_operator_token_does_not_raise(self, tmp_path):
        _make_index(tmp_path, ["alpha and beta", "gamma"])
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        dfs = qr.doc_frequencies(["and", "alpha"], target)
        assert dfs is not None
        assert dfs["alpha"] == 1
        assert dfs["and"] == 1  # quoted → literal token, not a boolean operator


class TestLexicalTerms:
    def test_orders_by_rarity_and_drops_common(self, tmp_path):
        _make_index(tmp_path, ["gesha special"] + ["beans"] * 20 + ["flavor"] * 8)
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        terms = qr.lexical_terms("gesha beans flavor notes", target, max_terms=2)
        # gesha (df 1) and flavor (df 8) are rarest; "beans" (df 20) drops out
        assert terms == ["gesha", "flavor"]

    def test_drops_terms_absent_from_corpus(self, tmp_path):
        _make_index(tmp_path, ["coffee arabica beans"])
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        terms = qr.lexical_terms("coffee sourdough", target)
        assert terms == ["coffee"]  # sourdough (df 0) is excluded

    def test_all_absent_yields_empty(self, tmp_path):
        _make_index(tmp_path, ["coffee arabica"])
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        assert qr.lexical_terms("sourdough pizza dough", target) == []

    def test_degrades_to_word_order_without_index(self):
        target = qr.QmdTarget(workspace="/nonexistent", resources_dir="/r", mode="http")
        terms = qr.lexical_terms("kubernetes deployment canary rollout", target, max_terms=2)
        assert terms == ["kubernetes", "deployment"]  # stopword-filtered order


class TestBuildSearches:
    def test_lex_and_vec_arms(self, tmp_path):
        _make_index(tmp_path, ["arabica robusta special"]
                    + ["beans"] * 30 + ["compare"] * 30)
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        prompt = "compare arabica and robusta beans"
        searches = qr.build_searches(prompt, target)
        assert searches[-1] == {"type": "vec", "query": prompt}
        lex = searches[0]
        assert lex["type"] == "lex"
        assert "arabica" in lex["query"] and "robusta" in lex["query"]
        assert "beans" not in lex["query"].split()  # common term drops (max 3 terms)

    def test_vec_only_when_no_rare_terms(self, tmp_path):
        _make_index(tmp_path, ["coffee arabica"])
        target = qr.QmdTarget(workspace=str(tmp_path), resources_dir="/r", mode="http")
        searches = qr.build_searches("tell me about sourdough", target)  # all absent
        assert searches == [{"type": "vec", "query": "tell me about sourdough"}]


class TestHttpTimeout:
    def test_floor_applies_at_default_dial(self):
        # 3 + 0.7*10 = 10 → HTTP_TIMEOUT floor holds
        assert qr.http_timeout(10) == qr.HTTP_TIMEOUT == 10

    def test_scales_with_candidate_limit(self):
        # ~0.58s/doc measured (40 docs → 24.9s); 0.7s/doc + 3s covers it
        assert qr.http_timeout(40) == pytest.approx(31.0)
        assert qr.http_timeout(20) == pytest.approx(17.0)

    def test_tiny_dial_still_gets_floor(self):
        assert qr.http_timeout(1) == qr.HTTP_TIMEOUT


class TestHttpSearch:
    def test_posts_typed_payload_and_parses_results(self, monkeypatch):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(
                {"results": [{"file": "qmd://resources/x.md", "score": 0.9}]}
            ).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data)
            captured["ctype"] = req.headers.get("Content-type")
            captured["timeout"] = timeout
            return FakeResp()

        monkeypatch.setattr(qr.urllib.request, "urlopen", fake_urlopen)
        searches = [{"type": "lex", "query": "coffee"},
                    {"type": "vec", "query": "learn about coffee"}]
        results = qr.http_search(searches, "learn about coffee", HTTP_TARGET)

        assert captured["url"] == "http://host.docker.internal:8181/query"
        assert captured["method"] == "POST"
        assert captured["ctype"] == "application/json"
        body = captured["body"]
        assert body["searches"] == searches
        assert body["intent"] == "learn about coffee"
        assert body["collections"] == ["resources"]
        assert body["rerank"] is True
        assert body["minScore"] == 0.0            # cutoff stays in results_to_entries
        assert body["candidateLimit"] == HTTP_TARGET.candidate_limit
        # default dial (10): the HTTP_TIMEOUT floor applies
        assert captured["timeout"] == qr.HTTP_TIMEOUT
        assert results == [{"file": "qmd://resources/x.md", "score": 0.9}]

    def test_timeout_scales_with_candidate_limit(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            raise qr.urllib.error.URLError("refused")

        monkeypatch.setattr(qr.urllib.request, "urlopen", fake_urlopen)
        target = qr.QmdTarget(workspace="/ws", resources_dir="/r", mode="http",
                              candidate_limit=40)
        qr.http_search([{"type": "vec", "query": "x"}], "x", target)
        assert captured["timeout"] == pytest.approx(31.0)  # 3 + 0.7*40

    def test_transport_error_is_fail_open(self, monkeypatch):
        def boom(req, timeout=None):
            raise qr.urllib.error.URLError("connection refused")

        monkeypatch.setattr(qr.urllib.request, "urlopen", boom)
        assert qr.http_search([{"type": "vec", "query": "x"}], "x", HTTP_TARGET) is None


class TestSearchHttpDispatch:
    def test_http_mode_authors_query_and_filters(self, tmp_path):
        _make_index(tmp_path, ["coffee arabica"] + ["common"] * 40)
        target = qr.QmdTarget(
            workspace=str(tmp_path), resources_dir=str(tmp_path / "R"), mode="http",
            min_score=0.30,
        )
        seen = {}

        def fake_http(searches, intent, tgt):
            seen["searches"] = searches
            seen["intent"] = intent
            return [{"file": "qmd://resources/good.md", "score": 0.8},
                    {"file": "qmd://resources/weak.md", "score": 0.1}]

        entries = qr.search("I want to learn more about coffee", target,
                            http_runner=fake_http)
        # authored, not raw: a lex arm of rare terms + the vec arm
        assert seen["intent"] == "I want to learn more about coffee"
        assert any(s["type"] == "lex" and "coffee" in s["query"]
                   for s in seen["searches"])
        assert any(s["type"] == "vec" for s in seen["searches"])
        # min_score drops the weak hit
        assert [e["path"] for e in entries] == [str(tmp_path / "R" / "good.md")]

    def test_http_mode_never_calls_shell_runner(self, tmp_path):
        _make_index(tmp_path, ["coffee"])
        target = qr.QmdTarget(
            workspace=str(tmp_path), resources_dir=str(tmp_path / "R"), mode="http",
        )

        def exploding_runner(*a, **k):
            raise AssertionError("shell runner must not be used in http mode")

        qr.search("a prompt about coffee here", target,
                  runner=exploding_runner, http_runner=lambda *a: [])
