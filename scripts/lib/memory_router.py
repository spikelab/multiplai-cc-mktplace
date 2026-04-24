"""Memory router strategies for context assembly.

Context routing picks which memory files to inject into each user
prompt. Two strategies are supported today, selected via the
``CLAUDE_PLUGIN_OPTION_memory_router`` environment variable:

    token_overlap  (default)   Cheap, offline. Tokenizes the prompt
                               and scores catalog entries by word
                               overlap against intent_domains. Zero
                               LLM calls, instant, but misses
                               synonym matches.

    llm                        Semantic. Sends the catalog (with
                               intent_domains + anti_domains) plus
                               the prompt to Sonnet and asks it to
                               return a JSON array of filenames.
                               Higher precision on synonyms and
                               task-intent matching; adds one LLM
                               hop per prompt.

A third strategy, ``embeddings``, is reserved for a future port that
scores prompt/domain similarity via a small local embedding model —
zero-cost per prompt after an initial embed pass, but requires model
setup out of scope here.

All routers implement the :class:`MemoryRouter` protocol so the
context manager can swap them at runtime without caring which is in
use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Env var name — matches the plugin's CLAUDE_PLUGIN_OPTION_* convention.
ROUTER_ENV_VAR = "CLAUDE_PLUGIN_OPTION_memory_router"

STRATEGY_TOKEN_OVERLAP = "token_overlap"
STRATEGY_LLM = "llm"
STRATEGY_EMBEDDINGS = "embeddings"

DEFAULT_STRATEGY = STRATEGY_TOKEN_OVERLAP
KNOWN_STRATEGIES = frozenset({STRATEGY_TOKEN_OVERLAP, STRATEGY_LLM, STRATEGY_EMBEDDINGS})


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryRouter(Protocol):
    """Selects relevant memory filenames for a prompt given a catalog."""

    name: str

    def select(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int = 10,
    ) -> list[str]:
        """Return an ordered list of memory filenames to load for *prompt*.

        An empty list means "no intent-based picks; fall back to the
        metadata ranking path." Raising is reserved for configuration
        errors; transient runtime failures should return ``[]``.
        """
        ...


# ---------------------------------------------------------------------------
# Token-overlap router (cheap, offline)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase word set for cheap overlap scoring."""
    return {
        w.strip(".,;:!?\"'()[]{}").lower()
        for w in text.split()
        if len(w.strip(".,;:!?\"'()[]{}")) >= 3
    }


def _entry_filename(entry: dict) -> str:
    return entry.get("source") or entry.get("path") or entry.get("file", "")


class TokenOverlapRouter:
    """Pure token-overlap router — no network, no LLM calls."""

    name = STRATEGY_TOKEN_OVERLAP

    def select(
        self,
        prompt: str,
        catalog_entries: list[dict],
        *,
        max_files: int = 10,
    ) -> list[str]:
        if not catalog_entries or not prompt:
            return []
        prompt_tokens = _tokenize(prompt)
        if not prompt_tokens:
            return []

        scored: list[tuple[int, str]] = []
        for entry in catalog_entries:
            filename = _entry_filename(entry)
            if not filename:
                continue

            intent_tokens = set()
            for phrase in entry.get("intent_domains", []) or []:
                if isinstance(phrase, str):
                    intent_tokens |= _tokenize(phrase)
            # Summary + topics supplement intent_domains so files that
            # are still catalog-tagged but not yet hand-curated with
            # intent_domains still score.
            intent_tokens |= _tokenize(
                entry.get("summary", "")
                + " "
                + " ".join(entry.get("topics", []) or [])
            )

            anti_tokens: set[str] = set()
            for phrase in entry.get("anti_domains", []) or []:
                if isinstance(phrase, str):
                    anti_tokens |= _tokenize(phrase)

            if anti_tokens & prompt_tokens:
                continue  # Respect anti_domains — skip this file
            match = len(intent_tokens & prompt_tokens)
            if match == 0:
                continue
            scored.append((match, filename))

        if not scored:
            return []
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [filename for _, filename in scored[:max_files]]


# ---------------------------------------------------------------------------
# LLM router (semantic, one Sonnet hop per prompt)
# ---------------------------------------------------------------------------


_LLM_SYSTEM_PROMPT = (
    "You are a memory-routing assistant. Given a user prompt and a catalog of "
    "memory files (each with intent_domains, anti_domains, and a summary), "
    "select the files whose contents would measurably help answer the prompt. "
    "Respond with ONLY a JSON array of filenames from the catalog — no prose, "
    "no markdown fences. If nothing is relevant, respond with []. "
    "Respect anti_domains: never select a file whose anti_domains match the prompt's intent."
)


def _format_catalog_for_llm(entries: Iterable[dict]) -> str:
    lines = []
    for entry in entries:
        filename = _entry_filename(entry)
        if not filename:
            continue
        summary = entry.get("summary", "").strip()
        intent = ", ".join(entry.get("intent_domains", []) or [])
        anti = ", ".join(entry.get("anti_domains", []) or [])
        block = [f"FILE: {filename}"]
        if summary:
            block.append(f"  Purpose: {summary}")
        if intent:
            block.append(f"  Relevant for: {intent}")
        if anti:
            block.append(f"  NOT relevant for: {anti}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)


def _parse_llm_selection(raw: str, known_filenames: set[str]) -> list[str]:
    """Extract a filename list from the LLM response; tolerate fenced JSON."""
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("LLM router returned non-JSON; ignoring")
        return []
    if not isinstance(parsed, list):
        logger.warning("LLM router JSON is not a list; ignoring")
        return []
    return [f for f in parsed if isinstance(f, str) and f in known_filenames]


class LLMRouter:
    """Semantic router — one LLM call per prompt via the model client.

    Failures (no client, query exception, malformed response) log at
    WARNING and return ``[]`` so the context manager falls back to
    the metadata ranking path rather than blocking the hook.
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
        if not catalog_entries or not prompt:
            return []

        known_filenames = {
            _entry_filename(e) for e in catalog_entries if _entry_filename(e)
        }
        if not known_filenames:
            return []

        try:
            selection = asyncio.run(
                self._select_async(prompt, catalog_entries, known_filenames)
            )
        except RuntimeError as e:
            # Running inside an already-active event loop (e.g., from
            # a test harness). Hooks normally invoke from a plain
            # sync entry point, so this path is rare.
            logger.warning("LLMRouter could not run event loop: %s", e)
            return []
        except Exception:
            logger.exception("LLMRouter call failed; falling back to no picks")
            return []

        return selection[:max_files]

    async def _select_async(
        self,
        prompt: str,
        catalog_entries: list[dict],
        known_filenames: set[str],
    ) -> list[str]:
        from lib.model_client import create_client

        client = await create_client()
        user_msg = (
            f"CATALOG:\n{_format_catalog_for_llm(catalog_entries)}\n\n"
            f"USER PROMPT:\n{prompt}"
        )
        try:
            response = await asyncio.wait_for(
                client.query(
                    system=_LLM_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "LLMRouter timed out after %.1fs", self._timeout_seconds,
            )
            return []
        return _parse_llm_selection(response.content, known_filenames)


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


def create_router(strategy: str | None = None) -> MemoryRouter:
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
    # resolve_strategy already filters unknowns; reaching here means
    # the caller passed something resolve_strategy let through that we
    # haven't branched on.
    raise ValueError(f"Unhandled memory router strategy: {effective}")
