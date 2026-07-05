"""TRIAGE node — hybrid: code dedup/diversity + LLM relevance scoring.

Most of the work is pure code: URL normalization, domain cap enforcement,
reputation tier assignment, diversity filtering. The LLM is only used to score
borderline cases when we have more candidates than slots.
"""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..config import ResearchConfig
from ..models import ReputationTier, SearchResult, Source
from ..prompts.triage_relevance import TRIAGE_RELEVANCE_PROMPT
from ..sdk import llm_call_structured

log = logging.getLogger(__name__)


# Heuristic reputation tiers by domain pattern
AUTHORITATIVE_PATTERNS = [
    ".gov", ".edu", "arxiv.org", "nature.com", "science.org", "who.int",
    "ieee.org", "acm.org",
]
ESTABLISHED_PATTERNS = [
    "wikipedia.org", "reuters.com", "apnews.com", "bloomberg.com", "nytimes.com",
    "wsj.com", "theguardian.com", "bbc.com", "bbc.co.uk", "economist.com",
    "crunchbase.com", "linkedin.com", "glassdoor.com", "github.com",
    "stackoverflow.com", "ycombinator.com",
]


class ScoredSource(BaseModel):
    url: str
    score: int
    reason: str = ""


class RelevanceScores(BaseModel):
    scores: list[ScoredSource] = Field(default_factory=list)


def _normalize_url(url: str) -> str:
    """Strip tracking params and trailing slash for dedup."""
    try:
        parsed = urlparse(url)
        # Strip UTM and other tracking params
        from urllib.parse import parse_qsl, urlencode, urlunparse

        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if not k.startswith(("utm_", "fbclid", "gclid", "ref", "source"))
        ]
        clean_query = urlencode(query)
        path = parsed.path.rstrip("/")
        return urlunparse(
            (parsed.scheme, parsed.netloc, path, parsed.params, clean_query, "")
        )
    except Exception:  # noqa: BLE001
        return url


def _reputation_for_domain(url: str) -> ReputationTier:
    """Heuristic reputation tier from URL domain."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ReputationTier.EMERGING

    for pattern in AUTHORITATIVE_PATTERNS:
        if pattern in host:
            return ReputationTier.AUTHORITATIVE
    for pattern in ESTABLISHED_PATTERNS:
        if pattern in host:
            return ReputationTier.ESTABLISHED
    return ReputationTier.EMERGING


def deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    """Deduplicate search results by normalized URL."""
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for r in results:
        key = _normalize_url(r.url)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    log.info("TRIAGE dedup: %d → %d", len(results), len(unique))
    return unique


def apply_diversity_filter(
    results: list[SearchResult],
    max_per_domain: int = 3,
) -> list[SearchResult]:
    """Enforce per-domain caps."""
    domain_counts: Counter[str] = Counter()
    kept: list[SearchResult] = []
    for r in results:
        try:
            domain = urlparse(r.url).netloc.lower()
        except Exception:  # noqa: BLE001
            domain = ""
        if domain_counts[domain] < max_per_domain:
            kept.append(r)
            domain_counts[domain] += 1
    log.info("TRIAGE diversity: %d → %d (max %d per domain)",
             len(results), len(kept), max_per_domain)
    return kept


def _host_matches_domain(url: str, domain: str) -> bool:
    """Check if a URL's host matches a domain (e.g., 'zendesk.com' matches 'www.zendesk.com')."""
    try:
        host = urlparse(url).netloc.lower()
        domain = domain.lower().strip()
        return host == domain or host.endswith("." + domain)
    except Exception:  # noqa: BLE001
        return False


def select_obvious_sources(
    results: list[SearchResult],
    target_domains: list[str],
    authority_domains: list[str] | None = None,
) -> tuple[list[SearchResult], list[SearchResult]]:
    """Split results into (auto-include, needs-scoring).

    Sources matching authority_domains are tagged with _is_authority=True
    so the READ node can give them guaranteed fetch budget.
    """
    obvious: list[SearchResult] = []
    borderline: list[SearchResult] = []
    _authority_domains = authority_domains or []
    for r in results:
        reputation = _reputation_for_domain(r.url)
        is_authority = any(
            _host_matches_domain(r.url, ad) for ad in _authority_domains
        )
        # Tag for downstream budget reservation
        setattr(r, "_is_authority", is_authority)

        # Authoritative sources auto-include
        if reputation == ReputationTier.AUTHORITATIVE:
            obvious.append(r)
            continue
        # Authority domain matches auto-include
        if is_authority:
            obvious.append(r)
            continue
        # Target domain matches auto-include (legacy descriptive matching)
        if any(td.lower() in r.url.lower() for td in target_domains):
            obvious.append(r)
            continue
        borderline.append(r)
    return obvious, borderline


async def triage(
    config: ResearchConfig,
    results: list[SearchResult],
    sub_questions: list[str],
    target_domains: list[str],
    authority_domains: list[str] | None = None,
) -> list[Source]:
    """Deduplicate, filter, and score sources — return the top N as Source objects."""
    # 1. Dedup
    unique = deduplicate(results)

    # 2. Diversity filter (domain cap)
    filtered = apply_diversity_filter(unique)

    # 3. Split into auto-include and borderline
    obvious, borderline = select_obvious_sources(
        filtered, target_domains, authority_domains,
    )

    # 4. Score borderline sources via LLM (only if we have capacity and borderline items)
    slots = config.preset.sources
    remaining_slots = slots - len(obvious)

    scored_borderline: list[tuple[SearchResult, int]] = []
    if borderline and remaining_slots > 0:
        # Only score if we have more borderline sources than slots (otherwise keep all)
        if len(borderline) > remaining_slots:
            sources_text = "\n".join(
                f"{i+1}. {r.title} — {r.url}\n   {r.snippet[:200]}"
                for i, r in enumerate(borderline[:30])  # cap LLM input
            )
            try:
                response = await llm_call_structured(
                    TRIAGE_RELEVANCE_PROMPT.format(
                        query=config.query,
                        sub_questions="\n".join(
                            f"- {q}" for q in sub_questions
                        ),
                        sources=sources_text,
                    ),
                    RelevanceScores,
                    model=config.models.get("triage_relevance"),
                    effort=config.effort,
                    label="triage:relevance",
                )
                score_map = {s.url: s.score for s in response.scores}
                scored_borderline = [(r, score_map.get(r.url, 2)) for r in borderline]
                scored_borderline.sort(key=lambda x: x[1], reverse=True)
            except Exception as e:  # noqa: BLE001
                log.warning("Relevance scoring failed, using heuristic order: %s", e)
                scored_borderline = [(r, 3) for r in borderline]
        else:
            scored_borderline = [(r, 3) for r in borderline]

    # 5. Pick top scored borderline to fill remaining slots
    selected_borderline = [r for r, _ in scored_borderline[:remaining_slots]]

    # 6. Build Source objects
    final_results = obvious[:slots] + selected_borderline[: slots - len(obvious)]
    sources = []
    for r in final_results:
        s = Source(
            url=r.url,
            title=r.title,
            snippet=r.snippet,
            reputation=_reputation_for_domain(r.url),
            source_api=r.source_api,
            published_date=r.published_date,
        )
        # Propagate authority flag for READ budget reservation
        if getattr(r, "_is_authority", False):
            setattr(s, "_is_authority", True)
        sources.append(s)

    authority_count = sum(1 for s in sources if getattr(s, "_is_authority", False))
    log.info("TRIAGE: %d sources selected (obvious=%d, borderline=%d, authority=%d)",
             len(sources), len(obvious), len(selected_borderline), authority_count)
    return sources
