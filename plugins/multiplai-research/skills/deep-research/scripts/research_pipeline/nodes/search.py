"""SEARCH node — pure code, batches all queries via SearchRouter."""

from __future__ import annotations

import logging

from ..config import ResearchConfig
from ..models import PlanResult, SearchResult
from ..search_router import SearchRouter

log = logging.getLogger(__name__)


async def search(
    config: ResearchConfig,
    plan_result: PlanResult,
    router: SearchRouter,
) -> list[SearchResult]:
    """Execute all queries concurrently via the search router."""
    queries = plan_result.all_queries
    log.info("SEARCH: executing %d queries", len(queries))

    results = await router.batch_search(
        queries,
        max_results=max(5, config.preset.sources // len(queries) if queries else 5),
        strategy="keyword",
    )

    log.info("SEARCH: %d unique results collected", len(results))
    return results
