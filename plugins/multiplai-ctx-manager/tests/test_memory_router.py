"""Tests for scripts/lib/memory_router.py."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Strategy resolution
# ---------------------------------------------------------------------------


class TestResolveStrategy:
    def test_default_is_llm(self, monkeypatch):
        # llm is the configured default — it is the only strategy that
        # can abstain. create_router() may still degrade to
        # token_overlap when no client exists (see TestCreateRouter).
        from lib.memory_router import (
            DEFAULT_STRATEGY,
            ROUTER_ENV_VAR,
            STRATEGY_LLM,
            resolve_strategy,
        )
        monkeypatch.delenv(ROUTER_ENV_VAR, raising=False)
        assert DEFAULT_STRATEGY == STRATEGY_LLM
        assert resolve_strategy() == STRATEGY_LLM

    def test_env_override_to_llm(self, monkeypatch):
        from lib.memory_router import (
            ROUTER_ENV_VAR,
            STRATEGY_LLM,
            resolve_strategy,
        )
        monkeypatch.setenv(ROUTER_ENV_VAR, "llm")
        assert resolve_strategy() == STRATEGY_LLM

    def test_unknown_strategy_falls_back(self, monkeypatch):
        from lib.memory_router import (
            DEFAULT_STRATEGY,
            ROUTER_ENV_VAR,
            resolve_strategy,
        )
        monkeypatch.setenv(ROUTER_ENV_VAR, "nonsense-strategy")
        assert resolve_strategy() == DEFAULT_STRATEGY

    def test_explicit_arg_wins(self, monkeypatch):
        from lib.memory_router import resolve_strategy
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_router", "token_overlap")
        assert resolve_strategy("llm") == "llm"


class TestCreateRouter:
    def test_default_degrades_to_token_overlap_without_client(self, monkeypatch):
        # Default is llm, but with no model client create_router()
        # degrades to the offline router so sessions still get routing.
        import lib.memory_router as mr
        monkeypatch.delenv(mr.ROUTER_ENV_VAR, raising=False)
        monkeypatch.setattr(
            "lib.model_client.detect_client_type",
            lambda: "none (no SDK or API key)",
        )
        assert isinstance(mr.create_router(), mr.TokenOverlapRouter)

    def test_llm_returns_llm_router_when_client_available(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "lib.model_client.detect_client_type", lambda: "AgentSDKClient"
        )
        assert isinstance(mr.create_router("llm"), mr.LLMRouter)

    def test_llm_degrades_to_token_overlap_without_client(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "lib.model_client.detect_client_type",
            lambda: "none (no SDK or API key)",
        )
        assert isinstance(mr.create_router("llm"), mr.TokenOverlapRouter)

    def test_embeddings_not_implemented(self):
        from lib.memory_router import create_router
        with pytest.raises(NotImplementedError):
            create_router("embeddings")


# ---------------------------------------------------------------------------
# TokenOverlapRouter
# ---------------------------------------------------------------------------


class TestTokenOverlapRouter:
    def _catalog(self) -> list[dict]:
        return [
            {
                "source": "writing.md",
                "summary": "voice guide for blog posts",
                "intent_domains": ["writing a blog post", "long-form content"],
                "anti_domains": ["debugging python"],
            },
            {
                "source": "python.md",
                "summary": "python patterns",
                "intent_domains": ["debugging python code", "async patterns"],
                "anti_domains": [],
            },
            {
                "source": "unrelated.md",
                "summary": "cooking notes",
                "intent_domains": ["cooking dinner"],
                "anti_domains": [],
            },
        ]

    def test_empty_prompt_returns_empty(self):
        from lib.memory_router import TokenOverlapRouter
        assert TokenOverlapRouter().select("", self._catalog()) == []

    def test_empty_catalog_returns_empty(self):
        from lib.memory_router import TokenOverlapRouter
        assert TokenOverlapRouter().select("debug async code", []) == []

    def test_matches_by_intent_domain(self):
        from lib.memory_router import TokenOverlapRouter
        picks = TokenOverlapRouter().select(
            "I need help debugging python async code", self._catalog(),
        )
        assert "python.md" in picks
        assert "unrelated.md" not in picks

    def test_anti_domain_drops_match(self):
        """File with matching intent_domain is dropped if anti_domain also matches."""
        from lib.memory_router import TokenOverlapRouter
        # Prompt matches writing.md's intent ("blog") AND its anti ("debugging")
        picks = TokenOverlapRouter().select(
            "writing a blog post about debugging python", self._catalog(),
        )
        assert "writing.md" not in picks

    def test_sorts_by_overlap_count(self):
        from lib.memory_router import TokenOverlapRouter
        catalog = [
            {"source": "a.md", "intent_domains": ["debugging python code"]},
            {"source": "b.md", "intent_domains": ["debugging python async code patterns"]},
        ]
        picks = TokenOverlapRouter().select(
            "debugging python async code patterns", catalog,
        )
        # b.md matches more tokens, should be first
        assert picks == ["b.md", "a.md"]

    def test_respects_max_files(self):
        from lib.memory_router import TokenOverlapRouter
        catalog = [
            {"source": f"f{i}.md", "intent_domains": ["python code"]}
            for i in range(20)
        ]
        picks = TokenOverlapRouter().select("python code", catalog, max_files=3)
        assert len(picks) == 3


# ---------------------------------------------------------------------------
# LLMRouter
# ---------------------------------------------------------------------------


class TestLLMRouter:
    def _catalog(self) -> list[dict]:
        return [
            {"source": "writing.md", "summary": "voice guide",
             "intent_domains": ["writing"]},
            {"source": "python.md", "summary": "py patterns",
             "intent_domains": ["python code"]},
        ]

    def test_empty_prompt_returns_empty(self):
        from lib.memory_router import LLMRouter
        assert LLMRouter().select("", self._catalog()) == []

    def test_empty_catalog_returns_empty(self):
        from lib.memory_router import LLMRouter
        assert LLMRouter().select("prompt", []) == []

    def test_select_uses_model_client(self):
        """Successful LLM response returns the parsed filename list (filtered to known)."""
        from lib.memory_router import LLMRouter

        mock_response = MagicMock()
        mock_response.content = '["python.md"]'
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client():
            return mock_client

        with patch("lib.model_client.create_client", _fake_create_client):
            picks = LLMRouter().select("help me debug python", self._catalog())
        assert picks == ["python.md"]

    def test_filters_unknown_filenames(self):
        """LLM-hallucinated filenames not in the catalog are dropped."""
        from lib.memory_router import LLMRouter

        mock_response = MagicMock()
        mock_response.content = '["python.md", "hallucinated.md"]'
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client():
            return mock_client

        with patch("lib.model_client.create_client", _fake_create_client):
            picks = LLMRouter().select("prompt", self._catalog())
        assert picks == ["python.md"]

    def test_handles_fenced_json(self):
        from lib.memory_router import LLMRouter

        mock_response = MagicMock()
        mock_response.content = '```json\n["python.md"]\n```'
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client():
            return mock_client

        with patch("lib.model_client.create_client", _fake_create_client):
            picks = LLMRouter().select("prompt", self._catalog())
        assert picks == ["python.md"]

    def test_malformed_response_returns_empty(self):
        from lib.memory_router import LLMRouter

        mock_response = MagicMock()
        mock_response.content = "not even close to JSON"
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client():
            return mock_client

        with patch("lib.model_client.create_client", _fake_create_client):
            picks = LLMRouter().select("prompt", self._catalog())
        assert picks == []

    def test_client_exception_returns_empty(self):
        """Exceptions from create_client or query are swallowed into []."""
        from lib.memory_router import LLMRouter

        async def _failing_client():
            raise RuntimeError("no backend configured")

        with patch("lib.model_client.create_client", _failing_client):
            picks = LLMRouter().select("prompt", self._catalog())
        assert picks == []


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_token_overlap_router_is_memory_router(self):
        from lib.memory_router import MemoryRouter, TokenOverlapRouter
        assert isinstance(TokenOverlapRouter(), MemoryRouter)

    def test_llm_router_is_memory_router(self):
        from lib.memory_router import LLMRouter, MemoryRouter
        assert isinstance(LLMRouter(), MemoryRouter)

    def test_token_overlap_router_is_corpus_router(self):
        from lib.memory_router import CorpusRouter, TokenOverlapRouter
        assert isinstance(TokenOverlapRouter(), CorpusRouter)

    def test_llm_router_is_corpus_router(self):
        from lib.memory_router import CorpusRouter, LLMRouter
        assert isinstance(LLMRouter(), CorpusRouter)


# ---------------------------------------------------------------------------
# TokenOverlapRouter.select_multi (multi-corpus)
# ---------------------------------------------------------------------------


class TestTokenOverlapMultiCorpus:
    def _corpora(self) -> dict[str, list[dict]]:
        return {
            "memory": [
                {"source": "writing.md", "intent_domains": ["writing a blog post"]},
                {"source": "python.md", "intent_domains": ["debugging python code"]},
            ],
            "skills": [
                {"name": "writing", "intent_domains": ["writing a blog post"]},
                {"name": "code-review", "intent_domains": ["reviewing pull requests"]},
            ],
            "resources": [
                {"source": "voice-ai.md", "intent_domains": ["voice AI frameworks"]},
            ],
        }

    def test_returns_dict_with_three_corpora(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "writing a blog post", None, self._corpora()
        )
        assert set(result.keys()) == {"memory", "skills", "resources"}

    def test_empty_prompt_returns_all_empty(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi("", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_routes_to_each_corpus_independently(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "writing a blog post", None, self._corpora()
        )
        assert "writing.md" in result["memory"]
        assert "writing" in result["skills"]
        assert result["resources"] == []  # no match for "writing"

    def test_resources_corpus_matched_by_intent(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "researching voice AI frameworks", None, self._corpora()
        )
        assert "voice-ai.md" in result["resources"]

    def test_last_response_supplements_tokens(self):
        """Last-response tokens combine with prompt tokens for matching."""
        from lib.memory_router import TokenOverlapRouter
        # Prompt alone has zero overlap with python intent
        result = TokenOverlapRouter().select_multi(
            "what next?",
            "We were just debugging python async code patterns",
            self._corpora(),
        )
        assert "python.md" in result["memory"]

    def test_missing_corpus_treated_as_empty(self):
        from lib.memory_router import TokenOverlapRouter
        # Only memory corpus provided
        # intent_domains rich enough to clear the relevance floor —
        # the mechanism under test is "absent corpora → []", not the
        # (removed) "any single-token overlap → returned" behavior.
        result = TokenOverlapRouter().select_multi(
            "writing a blog post",
            None,
            {"memory": [{"source": "x.md",
                         "intent_domains": ["writing a blog post"]}]},
        )
        assert result["skills"] == []
        assert result["resources"] == []
        assert "x.md" in result["memory"]

    def test_max_files_per_corpus_caps_each_independently(self):
        from lib.memory_router import TokenOverlapRouter
        many = [
            {"source": f"m{i}.md", "intent_domains": ["python code"]}
            for i in range(20)
        ]
        corpora = {"memory": many, "skills": [], "resources": []}
        result = TokenOverlapRouter().select_multi(
            "python code", None, corpora, max_files_per_corpus=4
        )
        assert len(result["memory"]) == 4


# ---------------------------------------------------------------------------
# Routing policy (NONE floor, continuation guard, relative cutoff) —
# select_multi only; select() stays pure rank+cap.
# ---------------------------------------------------------------------------


class TestRoutingPolicy:
    def _rich(self) -> list[dict]:
        # One strongly-relevant entry + several weak ones.
        return [
            {"source": "finances.md",
             "intent_domains": ["italian taxes and FBAR filing"],
             "keywords": ["FBAR", "backdoor Roth", "Form 8606"]},
            {"source": "python.md", "intent_domains": ["debugging python"]},
            {"source": "life.md", "intent_domains": ["personal life logistics"]},
        ]

    def test_none_floor_returns_empty_when_no_real_match(self):
        from lib.memory_router import TokenOverlapRouter
        # Prompt shares at most a faint token with any entry → below
        # MIN_SIGNAL → nothing injected (the abstention the old
        # always-top-N behavior could never do).
        result = TokenOverlapRouter().select_multi(
            "fix the CSS bug on line 42", None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        assert result["memory"] == []

    @pytest.mark.parametrize("phrase", ["yes", "go ahead", "do it", "thanks", "continue"])
    def test_continuation_guard_returns_all_empty(self, phrase):
        from lib.memory_router import TokenOverlapRouter
        r = TokenOverlapRouter()
        result = r.select_multi(
            phrase,
            "We were deep in italian taxes and FBAR filing details",
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        assert result == {"memory": [], "skills": [], "resources": []}
        assert r.last_scores["memory"].get("continuation") is True

    def test_relative_cutoff_isolates_the_strong_match(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "help with my FBAR and backdoor Roth for italian taxes",
            None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        # The strong entry is picked; weak unrelated ones are cut off,
        # so the result does not saturate to the whole catalog.
        assert "finances.md" in result["memory"]
        assert len(result["memory"]) < 3

    def test_diagnostics_exposed_for_logging(self):
        from lib.memory_router import TokenOverlapRouter
        r = TokenOverlapRouter()
        r.select_multi(
            "FBAR and backdoor Roth", None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        mem = r.last_scores["memory"]
        assert set(mem) >= {"scored", "cap", "n_candidates", "n_picked", "capped"}
        assert mem["scored"] and mem["scored"][0][0] > 0


# ---------------------------------------------------------------------------
# LLMRouter.select_multi (multi-corpus, single LLM call)
# ---------------------------------------------------------------------------


class TestLLMRouterMultiCorpus:
    def _corpora(self) -> dict[str, list[dict]]:
        return {
            "memory": [
                {"source": "voice.md", "intent_domains": ["writing"]},
                {"source": "py.md", "intent_domains": ["python"]},
            ],
            "skills": [
                {"name": "writing", "intent_domains": ["writing"]},
                {"name": "code-review", "intent_domains": ["review"]},
            ],
            "resources": [
                {"source": "ai/voice-ai.md", "intent_domains": ["voice AI"]},
            ],
        }

    def _make_mock_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.content = response_text
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create():
            return mock_client

        return _fake_create, mock_client

    def test_single_call_returns_three_corpus_dict(self):
        """A successful LLM response with all three keys is parsed correctly."""
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client(
            '{"memory": ["voice.md"], "skills": ["writing"], "resources": ["ai/voice-ai.md"]}'
        )
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "help me write a blog post", None, self._corpora()
            )
        assert result == {
            "memory": ["voice.md"],
            "skills": ["writing"],
            "resources": ["ai/voice-ai.md"],
        }
        # Single LLM call regardless of corpus count
        mock_client.query.assert_called_once()

    def test_empty_prompt_returns_all_empty_no_call(self):
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client('{}')
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}
        mock_client.query.assert_not_called()

    def test_filters_hallucinated_per_corpus(self):
        """LLM-hallucinated names not in their corpus are dropped."""
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md", "fake.md"], "skills": ["writing", "no-such"], "resources": []}'
        )
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result["memory"] == ["voice.md"]
        assert result["skills"] == ["writing"]

    def test_section_refs_pass_validation(self):
        """Entries like 'voice.md#Section' validate by stripping the fragment."""
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md#Voice Tone"], "skills": [], "resources": []}'
        )
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result["memory"] == ["voice.md#Voice Tone"]

    def test_malformed_response_returns_all_empty(self):
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client("not json at all")
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_query_exception_returns_all_empty(self):
        from lib.memory_router import LLMRouter

        async def _failing_client():
            raise RuntimeError("no backend")

        with patch("lib.model_client.create_client", _failing_client):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_all_empty_corpora_no_call(self):
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client('{}')
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "prompt",
                None,
                {"memory": [], "skills": [], "resources": []},
            )
        assert result == {"memory": [], "skills": [], "resources": []}
        mock_client.query.assert_not_called()

    def test_last_response_included_in_user_message(self):
        """When last_response is provided, it appears in the LLM input."""
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client(
            '{"memory": [], "skills": [], "resources": []}'
        )
        with patch("lib.model_client.create_client", fake_create):
            LLMRouter().select_multi(
                "are these costs ok?",
                "I just showed you the API pricing breakdown.",
                self._corpora(),
            )
        call_args = mock_client.query.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "LAST ASSISTANT RESPONSE" in user_msg
        assert "API pricing" in user_msg

    def test_max_files_per_corpus_caps_picks(self):
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md", "py.md"], "skills": [], "resources": []}'
        )
        with patch("lib.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "prompt", None, self._corpora(), max_files_per_corpus=1
            )
        assert len(result["memory"]) == 1
