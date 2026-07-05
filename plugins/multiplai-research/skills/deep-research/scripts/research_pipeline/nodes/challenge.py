"""Adversarial review (challenge mode) — stress-tests a completed report."""

from __future__ import annotations

import logging

from ..config import ResearchConfig
from ..prompts.challenge import ADVERSARIAL_REVIEW_PROMPT
from ..sdk import llm_call

log = logging.getLogger(__name__)


async def adversarial_review(config: ResearchConfig, report: str) -> str:
    """Run the adversarial review LLM call and return the markdown review."""
    prompt = ADVERSARIAL_REVIEW_PROMPT.format(
        report=report[:50000],  # cap to keep prompt size manageable
        date=config.date,
    )
    review = await llm_call(prompt, model=config.models.get("adversarial"), effort=config.effort, label="adversarial")
    log.info("ADVERSARIAL: review generated (%d chars)", len(review))
    return review
