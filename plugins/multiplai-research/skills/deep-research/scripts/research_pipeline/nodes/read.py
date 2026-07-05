"""READ node — fetch + extract findings from triaged sources.

Two paths based on config.prefer_claude_tools:

Strategy C (prefer_claude_tools=True, default):
  Uses ClaudeAgentFetcher — one SDK call per source combines WebFetch +
  finding extraction. PoC-validated: 33% fewer tokens, 16% faster than
  the two-call approach. Pre-extracted findings skip the separate LLM call.

Legacy (prefer_claude_tools=False):
  httpx fetch → trafilatura markdown → separate LLM extraction call.

Both paths:
- Process sources in batches with per-batch checkpointing
- Respect the preset's fetch budget
- Optionally follow links flagged during extraction
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from ..claude_agent_fetcher import ClaudeAgentFetcher, FetcherProtocol, HttpxFetcher
from ..config import ResearchConfig
from ..models import FetchResult, Finding, ReputationTier, Source
from ..prompts.extract import EXTRACT_PROMPT
from ..search_router import SearchRouter
from ..sdk import llm_call_structured
from ..state import ResearchState

log = logging.getLogger(__name__)


# Concurrency tuning
DEFAULT_BATCH_SIZE = 3


class FollowLink(BaseModel):
    url: str
    reason: str = ""


class ExtractedFindings(BaseModel):
    findings: list[dict] = Field(default_factory=list)
    follow_links: list[FollowLink] = Field(default_factory=list)


def _build_fetchers(
    config: ResearchConfig,
) -> tuple[FetcherProtocol, FetcherProtocol | None]:
    """Build primary + optional fallback fetcher.

    When using Claude Agent (Strategy C), HttpxFetcher is the automatic fallback.
    When using httpx as primary (--no-claude-tools), there's no fallback.
    """
    if config.prefer_claude_tools:
        primary = ClaudeAgentFetcher(model=config.models.get("extract"), effort=config.effort)
        fallback = HttpxFetcher()
        return primary, fallback
    return HttpxFetcher(), None


async def read(
    config: ResearchConfig,
    state: ResearchState,
    router: SearchRouter | None = None,
) -> None:
    """Fetch + extract findings for all pending sources. Updates state in place.

    Three-tier fallback chain:
    1. Primary fetcher (ClaudeAgentFetcher or HttpxFetcher based on config)
    2. HttpxFetcher fallback (when primary is ClaudeAgent)
    3. Tavily content search (AUTHORITATIVE sources only, max 5 per run)
    """
    pending = state.pending_sources()
    if not pending:
        log.info("READ: no pending sources")
        return

    primary, fallback = _build_fetchers(config)
    log.info(
        "READ: %d pending sources, batch size %d, primary=%s, fallback=%s",
        len(pending), DEFAULT_BATCH_SIZE,
        type(primary).__name__,
        type(fallback).__name__ if fallback else "none",
    )

    # Separate authority sources (guaranteed budget) from regular sources.
    # Authority sources are never blocked by fetch budget exhaustion.
    regular = [s for s in pending if not getattr(s, "_is_authority", False)]
    authority = [s for s in pending if getattr(s, "_is_authority", False)]
    if authority:
        log.info(
            "READ: %d authority sources with guaranteed budget, %d regular",
            len(authority), len(regular),
        )

    # Process regular sources first (budget-limited), then authority (guaranteed)
    ordered = regular + authority

    for batch_start in range(0, len(ordered), DEFAULT_BATCH_SIZE):
        batch = ordered[batch_start : batch_start + DEFAULT_BATCH_SIZE]
        batch_has_authority = any(getattr(s, "_is_authority", False) for s in batch)

        if (
            state.total_fetches >= config.preset.max_total_fetches
            and not batch_has_authority
        ):
            log.warning(
                "READ: fetch budget exhausted (%d/%d), stopping regular sources",
                state.total_fetches,
                config.preset.max_total_fetches,
            )
            break

        # If budget is exhausted but batch has authority sources, fetch only the
        # authority sources from this batch
        if state.total_fetches >= config.preset.max_total_fetches:
            batch = [s for s in batch if getattr(s, "_is_authority", False)]
            log.info(
                "READ: budget exhausted, fetching %d authority sources from batch",
                len(batch),
            )

        batch_urls = [s.url for s in batch]
        sub_questions = state.plan.sub_questions if state.plan else []

        # Fetch (+ extract for Strategy C)
        fetch_results = await primary.fetch_batch(
            batch_urls, query=config.query, sub_questions=sub_questions,
        )
        state.total_fetches += len(batch_urls)

        # Process results — Strategy C provides pre-extracted findings
        extraction_tasks = []
        failed_in_batch: list[tuple[Source, str]] = []
        for source, fetch_result in zip(batch, fetch_results):
            if not fetch_result.success or not fetch_result.content:
                err = fetch_result.error.message if fetch_result.error else "unknown"
                failed_in_batch.append((source, err))
                continue

            pre_extracted = getattr(fetch_result, "_pre_extracted_findings", None)
            if pre_extracted:
                _store_pre_extracted(config, source, fetch_result, pre_extracted, state)
            else:
                extraction_tasks.append(
                    _extract_findings(config, source, fetch_result.content, state)
                )

        if extraction_tasks:
            await asyncio.gather(*extraction_tasks, return_exceptions=True)

        # --- Fallback chain for failed sources ---
        if failed_in_batch and fallback:
            await _retry_with_fallback(
                config, state, fallback, router, failed_in_batch, sub_questions,
            )
        elif failed_in_batch:
            # No fallback available — mark all as failed
            for source, err in failed_in_batch:
                state.mark_source_failed(source.url, error=f"fetch: {err}")

        # Follow links if preset allows and budget remains
        if (
            config.preset.follow_links
            and state.total_fetches < config.preset.max_total_fetches
        ):
            await _follow_links(config, state, primary, batch)


async def _retry_with_fallback(
    config: ResearchConfig,
    state: ResearchState,
    fallback: FetcherProtocol,
    router: SearchRouter | None,
    failed_sources: list[tuple[Source, str]],
    sub_questions: list[str],
) -> None:
    """Retry failed sources with httpx fallback, then Tavily for AUTHORITATIVE."""
    for source, original_err in failed_sources:
        # Tier 2: httpx fallback
        log.info("READ fallback: retrying %s with httpx", source.url[:60])
        fallback_result = await fallback.fetch_url(
            source.url, query=config.query, sub_questions=sub_questions,
        )

        if fallback_result.success and fallback_result.content:
            log.info("READ fallback: httpx succeeded for %s", source.url[:60])
            # Extract findings from httpx content via LLM
            await _extract_findings(config, source, fallback_result.content, state)
            continue

        # Tier 3: Tavily content search (AUTHORITATIVE only)
        if (
            router is not None
            and source.reputation == ReputationTier.AUTHORITATIVE
        ):
            log.info(
                "READ fallback: trying Tavily for AUTHORITATIVE source %s",
                source.url[:60],
            )
            tavily_results = await router.tavily_content_fallback(
                source.url, source.title, config.query,
            )
            if tavily_results:
                # Combine Tavily search result snippets as synthetic content
                content = "\n\n".join(
                    f"## {r.title}\n\n{r.snippet}" for r in tavily_results if r.snippet
                )
                if content.strip():
                    log.info(
                        "READ fallback: Tavily provided %d results for %s",
                        len(tavily_results), source.url[:60],
                    )
                    state.tavily_fallback_count += 1
                    await _extract_findings(config, source, content, state)
                    continue

        # All fallbacks exhausted — mark as failed
        fb_err = fallback_result.error.message if fallback_result.error else "unknown"
        state.mark_source_failed(
            source.url,
            error=f"fetch: {original_err} | httpx fallback: {fb_err}",
        )


def _store_pre_extracted(
    config: ResearchConfig,  # noqa: ARG001 — kept for signature consistency
    source: Source,
    fetch_result: FetchResult,
    findings_raw: list[dict],
    state: ResearchState,
) -> None:
    """Convert Strategy C pre-extracted findings and store them."""
    findings: list[Finding] = []
    for item in findings_raw:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(
                Finding(
                    fact=item.get("fact", ""),
                    source_url=source.url,
                    source_title=source.title,
                    reputation=source.reputation,
                    confidence=item.get("confidence", "medium"),
                    quote=item.get("quote"),
                    date=item.get("date"),
                    relates_to_sub_question=item.get("relates_to_sub_question"),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Pre-extracted finding validation failed: %s", e)

    # Store follow links on the source for _follow_links
    extracted_links = fetch_result.extracted_links or []
    setattr(source, "_follow_links", extracted_links[:3])

    content = fetch_result.content or ""
    state.mark_source_extracted(source.url, content=content, findings=findings)
    log.info("READ: %s → %d findings", source.title[:50], len(findings))


async def _extract_findings(
    config: ResearchConfig,
    source: Source,
    content: str,
    state: ResearchState,
) -> None:
    """Call the LLM to extract findings from one source's markdown content."""
    sub_questions = state.plan.sub_questions if state.plan else []

    prompt = EXTRACT_PROMPT.format(
        query=config.query,
        sub_questions="\n".join(f"{i}. {q}" for i, q in enumerate(sub_questions)),
        title=source.title,
        url=source.url,
        reputation=source.reputation.value,
        content=content[:30000],  # cap content length to keep prompt manageable
    )

    try:
        extracted = await llm_call_structured(
            prompt,
            ExtractedFindings,
            model=config.models.get("extract"),
            effort=config.effort,
            label=f"extract:{source.url[:50]}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Extraction failed for %s: %s", source.url, e)
        state.mark_source_failed(source.url, error=f"extraction: {e}")
        return

    # Convert extracted dicts to Finding objects
    findings: list[Finding] = []
    for item in extracted.findings:
        try:
            findings.append(
                Finding(
                    fact=item.get("fact", ""),
                    source_url=source.url,
                    source_title=source.title,
                    reputation=source.reputation,
                    confidence=item.get("confidence", "medium"),
                    quote=item.get("quote"),
                    date=item.get("date"),
                    relates_to_sub_question=item.get("relates_to_sub_question"),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Finding validation failed: %s", e)

    # Store follow links on the source for _follow_links to pick up
    source.extracted_content = content
    # Use a private attr on the source via model_extra (Pydantic allows this)
    setattr(source, "_follow_links", [fl.url for fl in extracted.follow_links[:3]])

    state.mark_source_extracted(source.url, content=content, findings=findings)
    log.info("READ: %s → %d findings", source.title[:50], len(findings))


async def _follow_links(
    config: ResearchConfig,
    state: ResearchState,
    fetcher: FetcherProtocol,
    batch: list[Source],
) -> None:
    """Follow LLM-flagged links from a completed batch."""
    candidate_links: list[tuple[str, Source]] = []
    for source in batch:
        follow_urls = getattr(source, "_follow_links", []) or []
        for url in follow_urls:
            candidate_links.append((url, source))

    if not candidate_links:
        return

    # Respect fetch budget
    budget_remaining = config.preset.max_total_fetches - state.total_fetches
    links_to_fetch = candidate_links[: min(len(candidate_links), budget_remaining)]
    if not links_to_fetch:
        return

    log.info("READ: following %d level-1 links", len(links_to_fetch))

    urls = [u for u, _ in links_to_fetch]
    sub_questions = state.plan.sub_questions if state.plan else []
    fetch_results = await fetcher.fetch_batch(
        urls, query=config.query, sub_questions=sub_questions,
    )
    state.total_fetches += len(urls)

    # Process link results — same Strategy C / legacy split as main read
    tasks = []
    for (url, parent), result in zip(links_to_fetch, fetch_results):
        if not result.success or not result.content:
            continue
        link_source = Source(
            url=url,
            title=f"(link from {parent.title[:40]})",
            snippet="",
            reputation=parent.reputation,
        )
        pre_extracted = getattr(result, "_pre_extracted_findings", None)
        if pre_extracted:
            _store_pre_extracted(config, link_source, result, pre_extracted, state)
        else:
            tasks.append(_extract_findings(config, link_source, result.content, state))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
