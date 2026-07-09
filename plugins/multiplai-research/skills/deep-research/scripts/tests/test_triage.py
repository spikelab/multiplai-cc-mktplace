"""Tests for the TRIAGE node — authority-source preservation (RES-3).

Focused on the slot-cap behavior: authority sources (is_authority=True) must
survive an over-full triage so READ's guaranteed fetch budget can see them,
even when there are more obvious sources than slots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.config import PRESETS, ResearchConfig
from research_pipeline.models import SearchResult
from research_pipeline.nodes.triage import triage


def _result(url: str) -> SearchResult:
    return SearchResult(url=url, title=url, snippet="", source_api="stub")


@pytest.mark.asyncio
async def test_authority_sources_survive_over_full_triage() -> None:
    """With len(obvious) > slots, authority sources are never truncated away."""
    config = ResearchConfig(
        query="test",
        output_dir=Path("/tmp"),
        preset=PRESETS["micro"],  # slots = 3
    )
    assert config.preset.sources == 3

    # 3 non-authority AUTHORITATIVE-tier sources (auto-include, is_authority=False)
    # followed by 2 authority-domain sources (is_authority=True). Listing the
    # authority ones LAST means the old obvious[:slots] would drop them.
    results = [
        _result("https://a.gov/1"),
        _result("https://b.gov/2"),
        _result("https://c.gov/3"),
        _result("https://auth.example/x"),
        _result("https://auth.example/y"),
    ]

    sources = await triage(
        config,
        results,
        sub_questions=["q1"],
        target_domains=[],
        authority_domains=["auth.example"],
    )

    # Exactly `slots` sources returned, and BOTH authority sources are among them.
    assert len(sources) == config.preset.sources
    authority_urls = {s.url for s in sources if s.is_authority}
    assert authority_urls == {"https://auth.example/x", "https://auth.example/y"}
    assert sum(1 for s in sources if s.is_authority) == 2


@pytest.mark.asyncio
async def test_no_authority_sources_still_truncates_to_slots() -> None:
    """Without authority sources, the slot cap still bounds the result count."""
    config = ResearchConfig(
        query="test",
        output_dir=Path("/tmp"),
        preset=PRESETS["micro"],  # slots = 3
    )
    results = [_result(f"https://{c}.gov/1") for c in "abcde"]  # 5 AUTHORITATIVE

    sources = await triage(
        config,
        results,
        sub_questions=["q1"],
        target_domains=[],
        authority_domains=[],
    )

    assert len(sources) == config.preset.sources
    assert all(not s.is_authority for s in sources)
