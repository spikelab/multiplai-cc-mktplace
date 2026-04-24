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
    def test_default_is_token_overlap(self, monkeypatch):
        from lib.memory_router import (
            DEFAULT_STRATEGY,
            ROUTER_ENV_VAR,
            STRATEGY_TOKEN_OVERLAP,
            resolve_strategy,
        )
        monkeypatch.delenv(ROUTER_ENV_VAR, raising=False)
        assert DEFAULT_STRATEGY == STRATEGY_TOKEN_OVERLAP
        assert resolve_strategy() == STRATEGY_TOKEN_OVERLAP

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
    def test_default_returns_token_overlap(self, monkeypatch):
        from lib.memory_router import (
            ROUTER_ENV_VAR,
            TokenOverlapRouter,
            create_router,
        )
        monkeypatch.delenv(ROUTER_ENV_VAR, raising=False)
        assert isinstance(create_router(), TokenOverlapRouter)

    def test_llm_returns_llm_router(self):
        from lib.memory_router import LLMRouter, create_router
        assert isinstance(create_router("llm"), LLMRouter)

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
