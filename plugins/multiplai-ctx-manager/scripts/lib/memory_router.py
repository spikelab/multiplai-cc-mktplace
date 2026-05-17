"""Multi-corpus router strategies for context assembly.

Context routing picks which catalog entries (across memory, skills,
and resources) to inject into each user prompt. Two strategies are
supported, selected via the ``CLAUDE_PLUGIN_OPTION_memory_router``
environment variable:

    token_overlap  (default)   Cheap, offline. Tokenizes the prompt
                               (plus the last assistant response if
                               available) and scores each catalog
                               entry by word overlap against
                               intent_domains. Zero LLM calls,
                               instant, but misses synonym matches.

    llm                        Semantic. Sends ALL catalogs in a
                               SINGLE LLM call along with the prompt
                               and last response, asking for a
                               three-key JSON object selecting from
                               each corpus. Higher precision; one
                               LLM hop per prompt.

A third strategy, ``embeddings``, is reserved for a future port —
zero-cost per prompt after an initial embed pass, but requires model
setup out of scope here.

Both routers expose two methods:

    select(prompt, entries, *, max_files=10) -> list[str]
        Single-corpus selection. Used by tests and by the legacy
        single-corpus context_manager path. Last-response unaware.

    select_multi(prompt, last_response, corpora, *, max_files_per_corpus=10)
        -> dict[str, list[str]]
        Multi-corpus selection. The canonical entry point for the
        new context_manager flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Protocol, runtime_checkable

from lib.router_prompt import SYSTEM_PROMPT, FEW_SHOT_EXAMPLES, build_user_message

logger = logging.getLogger(__name__)

# Env var name — matches the plugin's CLAUDE_PLUGIN_OPTION_* convention.
ROUTER_ENV_VAR = "CLAUDE_PLUGIN_OPTION_memory_router"

STRATEGY_TOKEN_OVERLAP = "token_overlap"
STRATEGY_LLM = "llm"
STRATEGY_EMBEDDINGS = "embeddings"

DEFAULT_STRATEGY = STRATEGY_TOKEN_OVERLAP
KNOWN_STRATEGIES = frozenset({STRATEGY_TOKEN_OVERLAP, STRATEGY_LLM, STRATEGY_EMBEDDINGS})

CORPUS_TYPES = ("memory", "skills", "resources")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CorpusRouter(Protocol):
    """Selects relevant entries across one or more catalog corpora."""

    name: str

    def select(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int = 10,
    ) -> list[str]:
        """Single-corpus pick — returns ordered list of entry names."""
        ...

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus pick — returns ``{corpus_name: [name, ...]}``."""
        ...


# Legacy alias so existing single-corpus callers keep type-checking.
MemoryRouter = CorpusRouter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase word set for cheap overlap scoring."""
    return {
        w.strip(".,;:!?\"'()[]{}").lower()
        for w in text.split()
        if len(w.strip(".,;:!?\"'()[]{}")) >= 3
    }


def _entry_filename(entry: dict) -> str:
    """Resolve the catalog-entry key.

    Skills entries use ``name``; memory and resources use ``source``
    (with ``path`` / ``file`` legacy fallbacks).
    """
    return (
        entry.get("source")
        or entry.get("path")
        or entry.get("name")
        or entry.get("file", "")
    )


# ---------------------------------------------------------------------------
# Token-overlap router (cheap, offline)
# ---------------------------------------------------------------------------


class TokenOverlapRouter:
    """Pure token-overlap router — no network, no LLM calls."""

    name = STRATEGY_TOKEN_OVERLAP

    def __init__(self) -> None:
        # Populated by select_multi each call: per-corpus full
        # pre-truncation ranking + cap diagnostics, for the context
        # manager to log. This is the routing-quality signal /health
        # reports. Empty until the first select_multi call.
        self.last_scores: dict[str, dict] = {}

    def select(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int = 10,
    ) -> list[str]:
        """Single-corpus selection (no last-response awareness)."""
        return self._score_corpus(prompt, catalog_entries, max_files=max_files)

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus selection.

        Token scoring combines prompt and last-response tokens — the
        last response disambiguates short prompts where the same
        token (e.g., "costs") could match different domains. Each
        corpus is scored independently using the same tokens.

        Per-corpus scoring diagnostics (full pre-truncation ranking,
        the cap, candidate count, whether the cap was binding) are
        stashed on ``self.last_scores`` for the context manager to
        log — this is the routing-quality signal /health reports.
        """
        self.last_scores = {}
        if not prompt:
            return {ct: [] for ct in CORPUS_TYPES}
        combined = prompt
        if last_response:
            combined = f"{prompt}\n{last_response}"
        result: dict[str, list[str]] = {}
        for corpus_type in CORPUS_TYPES:
            entries = corpora.get(corpus_type) or []
            scored = self._scored_pairs(combined, entries)
            result[corpus_type] = [
                fn for _, fn in scored[:max_files_per_corpus]
            ]
            self.last_scores[corpus_type] = {
                "scored": scored,
                "cap": max_files_per_corpus,
                "n_candidates": len(scored),
                "capped": len(scored) > max_files_per_corpus,
            }
        return result

    def _score_corpus(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int,
    ) -> list[str]:
        scored = self._scored_pairs(prompt, catalog_entries)
        return [filename for _, filename in scored[:max_files]]

    def _scored_pairs(
        self,
        prompt: str,
        catalog_entries: list[dict],
    ) -> list[tuple[float, str]]:
        """Score every entry; return ``(score, filename)`` sorted desc.

        Full, un-truncated ranking — callers truncate to their own
        cap. Scoring logic here is unchanged from the original
        ``_score_corpus`` (raw intent/summary/topics token overlap);
        T1 only exposes the ranking so the cutoff can be measured.
        T3 reworks the scoring itself.
        """
        if not catalog_entries or not prompt:
            return []
        prompt_tokens = _tokenize(prompt)
        if not prompt_tokens:
            return []

        scored: list[tuple[float, str]] = []
        for entry in catalog_entries:
            filename = _entry_filename(entry)
            if not filename:
                continue

            intent_tokens: set[str] = set()
            for phrase in entry.get("intent_domains", []) or []:
                if isinstance(phrase, str):
                    intent_tokens |= _tokenize(phrase)
            # Summary + topics supplement intent_domains so files that
            # are still catalog-tagged but not yet hand-curated with
            # intent_domains still score.
            intent_tokens |= _tokenize(
                (entry.get("summary") or "")
                + " "
                + " ".join(entry.get("topics") or [])
            )

            anti_tokens: set[str] = set()
            for phrase in entry.get("anti_domains", []) or []:
                if isinstance(phrase, str):
                    anti_tokens |= _tokenize(phrase)

            if anti_tokens & prompt_tokens:
                continue  # Respect anti_domains — skip this entry
            match = len(intent_tokens & prompt_tokens)
            if match == 0:
                continue
            scored.append((float(match), filename))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored


# ---------------------------------------------------------------------------
# LLM router (semantic, ONE call covering all corpora)
# ---------------------------------------------------------------------------


def _parse_llm_multi_selection(
    raw: str,
    known_per_corpus: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Extract a ``{corpus: [name, ...]}`` selection from the LLM response.

    Tolerates markdown-fenced JSON. Filters each corpus's selections
    to entries actually present in that corpus's known-name set.
    Section refs (``"file#Section"``) are validated by stripping the
    fragment before checking presence.
    """
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("LLM router returned non-JSON; ignoring")
        return {ct: [] for ct in CORPUS_TYPES}

    if not isinstance(parsed, dict):
        logger.warning("LLM router JSON is not an object; ignoring")
        return {ct: [] for ct in CORPUS_TYPES}

    result: dict[str, list[str]] = {}
    for corpus_type in CORPUS_TYPES:
        raw_list = parsed.get(corpus_type, [])
        if not isinstance(raw_list, list):
            result[corpus_type] = []
            continue
        known = known_per_corpus.get(corpus_type, set())
        validated: list[str] = []
        for item in raw_list:
            if not isinstance(item, str):
                continue
            base = item.split("#", 1)[0]
            if base in known:
                validated.append(item)
        result[corpus_type] = validated
    return result


def _parse_llm_single_selection(raw: str, known_filenames: set[str]) -> list[str]:
    """Parse a single-corpus LLM response (legacy ``select`` path).

    The LLM is asked for a JSON array; we accept either an array or
    an object with the corpus key. Returns only filenames that exist
    in ``known_filenames``.
    """
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("LLM router returned non-JSON; ignoring")
        return []
    if isinstance(parsed, dict):
        # Tolerate the multi-corpus response shape under the "memory" key.
        parsed = parsed.get("memory", [])
    if not isinstance(parsed, list):
        logger.warning("LLM router JSON is not a list; ignoring")
        return []
    return [
        f for f in parsed
        if isinstance(f, str) and f.split("#", 1)[0] in known_filenames
    ]


class LLMRouter:
    """Semantic router — one LLM call per prompt covering all corpora.

    Failures (no client, query exception, malformed response, timeout)
    log at WARNING and return empty picks so the context manager
    falls back to the metadata ranking path rather than blocking the
    hook.
    """

    name = STRATEGY_LLM

    def __init__(self, *, timeout_seconds: float = 4.0) -> None:
        self._timeout_seconds = timeout_seconds

    def select(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int = 10,
    ) -> list[str]:
        """Single-corpus selection — convenience for legacy callers and tests.

        Wraps the multi-corpus path with a single ``memory`` corpus.
        """
        if not catalog_entries or not prompt:
            return []

        known_filenames = {
            _entry_filename(e) for e in catalog_entries if _entry_filename(e)
        }
        if not known_filenames:
            return []

        try:
            picks = asyncio.run(
                self._select_async_single(prompt, catalog_entries, known_filenames)
            )
        except RuntimeError as e:
            logger.warning("LLMRouter could not run event loop: %s", e)
            return []
        except Exception:
            logger.exception("LLMRouter call failed; falling back to no picks")
            return []
        return picks[:max_files]

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus selection via a single LLM call covering all 3 corpora."""
        empty = {ct: [] for ct in CORPUS_TYPES}
        if not prompt:
            return empty

        known_per_corpus: dict[str, set[str]] = {}
        any_entries = False
        for corpus_type in CORPUS_TYPES:
            entries = corpora.get(corpus_type) or []
            known_per_corpus[corpus_type] = {
                _entry_filename(e) for e in entries if _entry_filename(e)
            }
            if known_per_corpus[corpus_type]:
                any_entries = True
        if not any_entries:
            return empty

        try:
            picks = asyncio.run(
                self._select_async_multi(prompt, last_response, corpora, known_per_corpus)
            )
        except RuntimeError as e:
            logger.warning("LLMRouter could not run event loop: %s", e)
            return empty
        except Exception:
            logger.exception("LLMRouter call failed; falling back to no picks")
            return empty

        return {
            ct: picks.get(ct, [])[:max_files_per_corpus] for ct in CORPUS_TYPES
        }

    async def _select_async_single(
        self,
        prompt: str,
        catalog_entries: list[dict],
        known_filenames: set[str],
    ) -> list[str]:
        from lib.model_client import create_client

        client = await create_client()
        user_msg = build_user_message(
            prompt, None, {"memory": catalog_entries, "skills": [], "resources": []}
        )
        try:
            response = await asyncio.wait_for(
                client.query(
                    system=SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("LLMRouter timed out after %.1fs", self._timeout_seconds)
            return []
        return _parse_llm_single_selection(response.content, known_filenames)

    async def _select_async_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        known_per_corpus: dict[str, set[str]],
    ) -> dict[str, list[str]]:
        from lib.model_client import create_client

        client = await create_client()
        user_msg = build_user_message(prompt, last_response, corpora)
        try:
            response = await asyncio.wait_for(
                client.query(
                    system=SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("LLMRouter timed out after %.1fs", self._timeout_seconds)
            return {ct: [] for ct in CORPUS_TYPES}
        return _parse_llm_multi_selection(response.content, known_per_corpus)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_strategy(raw: str | None = None) -> str:
    """Return the effective strategy name, defaulting to token_overlap.

    Unknown values are logged and fall back to the default rather than
    raising — a typo in a plugin option shouldn't break the hook.
    """
    value = (raw if raw is not None else os.environ.get(ROUTER_ENV_VAR, "")).strip().lower()
    if not value:
        return DEFAULT_STRATEGY
    if value not in KNOWN_STRATEGIES:
        logger.warning(
            "Unknown memory router strategy %r; falling back to %s",
            value, DEFAULT_STRATEGY,
        )
        return DEFAULT_STRATEGY
    return value


def create_router(strategy: str | None = None) -> CorpusRouter:
    """Build a router for *strategy* (or the env default).

    ``embeddings`` is accepted by name but not yet implemented — it
    raises :class:`NotImplementedError` so a misconfiguration is loud
    at session start rather than silently producing bad routing.
    """
    effective = resolve_strategy(strategy)
    if effective == STRATEGY_TOKEN_OVERLAP:
        return TokenOverlapRouter()
    if effective == STRATEGY_LLM:
        return LLMRouter()
    if effective == STRATEGY_EMBEDDINGS:
        raise NotImplementedError(
            "Embeddings router is reserved for a future port — set "
            f"{ROUTER_ENV_VAR}={STRATEGY_TOKEN_OVERLAP} or "
            f"{STRATEGY_LLM} to pick an available strategy."
        )
    raise ValueError(f"Unhandled memory router strategy: {effective}")
