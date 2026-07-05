"""PLAN, DIVERGE, CHALLENGE nodes — LLM calls that build the research plan."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..config import ResearchConfig
from ..models import PlanResult
from ..prompts.challenge import CHALLENGE_PROMPT
from ..prompts.diverge import DIVERGE_PROMPT
from ..prompts.plan import PLAN_PROMPT
from ..research_types import guidance_for
from ..sdk import llm_call_structured

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response schemas (what the LLM returns per node)
# ---------------------------------------------------------------------------


class PlanResponse(BaseModel):
    sub_questions: list[str] = Field(default_factory=list)
    target_domains: list[str] = Field(default_factory=list)
    authority_domains: list[str] = Field(default_factory=list)
    what_good_looks_like: str = ""


class DivergeResponse(BaseModel):
    primary_queries: list[str] = Field(default_factory=list)
    mechanism_queries: list[str] = Field(default_factory=list)
    directory_queries: list[str] = Field(default_factory=list)


class ChallengeResponse(BaseModel):
    contrarian_queries: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def plan(config: ResearchConfig) -> PlanResult:
    """PLAN: decompose the query into sub-questions and target domains."""
    prompt = PLAN_PROMPT.format(
        query=config.query,
        research_type=config.research_type,
        date=config.date,
        personal_context=config.personal_context or "none",
        prior_knowledge=config.prior_knowledge or "none",
        research_type_guidance=guidance_for(config.research_type, "plan"),
        max_sub_questions=config.preset.max_sub_questions,
    )
    response = await llm_call_structured(
        prompt, PlanResponse, model=config.models.get("plan"), effort=config.effort,
        label="plan",
    )

    log.info(
        "PLAN: %d sub-questions, %d authority domains",
        len(response.sub_questions),
        len(response.authority_domains),
    )
    return PlanResult(
        sub_questions=response.sub_questions,
        target_domains=response.target_domains,
        authority_domains=response.authority_domains,
        what_good_looks_like=response.what_good_looks_like,
    )


async def diverge(config: ResearchConfig, plan_result: PlanResult) -> PlanResult:
    """DIVERGE: expand queries with mechanism-level and directory angles."""
    prompt = DIVERGE_PROMPT.format(
        query=config.query,
        research_type=config.research_type,
        date=config.date,
        sub_questions="\n".join(f"{i+1}. {q}" for i, q in enumerate(plan_result.sub_questions)),
        research_type_guidance=guidance_for(config.research_type, "diverge"),
    )
    response = await llm_call_structured(
        prompt, DivergeResponse, model=config.models.get("diverge"), effort=config.effort,
        label="diverge",
    )

    plan_result.primary_queries = response.primary_queries
    plan_result.mechanism_queries = response.mechanism_queries
    plan_result.directory_queries = response.directory_queries

    log.info(
        "DIVERGE: %d primary, %d mechanism, %d directory queries",
        len(response.primary_queries),
        len(response.mechanism_queries),
        len(response.directory_queries),
    )
    return plan_result


async def challenge(config: ResearchConfig, plan_result: PlanResult) -> PlanResult:
    """CHALLENGE: generate contrarian queries."""
    prompt = CHALLENGE_PROMPT.format(
        query=config.query,
        research_type=config.research_type,
        date=config.date,
        sub_questions="\n".join(f"{i+1}. {q}" for i, q in enumerate(plan_result.sub_questions)),
        primary_queries="\n".join(f"- {q}" for q in plan_result.primary_queries),
        research_type_guidance=guidance_for(config.research_type, "challenge"),
    )
    response = await llm_call_structured(
        prompt, ChallengeResponse, model=config.models.get("challenge"), effort=config.effort,
        label="challenge",
    )

    plan_result.contrarian_queries = response.contrarian_queries

    log.info("CHALLENGE: %d contrarian queries", len(response.contrarian_queries))
    return plan_result
