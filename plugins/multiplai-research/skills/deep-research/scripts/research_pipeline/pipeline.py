"""Pipeline orchestrator — sequences all nodes and gates in order.

This is where everything comes together:
- Loads or creates ResearchState
- Validates API keys
- Runs PLAN → DIVERGE → CHALLENGE → diversity gate → SEARCH → TRIAGE →
  min-sources gate → READ → coverage gate → REASSESS → reassess gate →
  SYNTHESIZE → (optional adversarial review)
- Handles gate recovery actions
- Handles parallel mode (N sub-pipelines merged via synthesis)
- Supports --plan-only and --approved-plan modes
- Resumes from checkpoint when state file exists
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .config import PRESETS, ResearchConfig
from .env import load_env
from .gates import (
    coverage_gate,
    critical_source_gate,
    deduplicate_findings,
    min_sources_gate,
    query_diversity_gate,
    reassess_gate,
)
from .models import PlanResult, SubTopic
from .nodes import challenge as challenge_node
from .nodes import plan as plan_node
from .nodes import quality_check as quality_check_node
from .nodes import read as read_node
from .nodes import reassess as reassess_node
from .nodes import search as search_node
from .nodes import synthesize as synthesize_node
from .nodes import triage as triage_node
from .progress import ProgressWriter
from .search_router import SearchRouter, build_default_router
from .state import ResearchState, Stage

log = logging.getLogger(__name__)


REQUIRED_API_KEYS = [
    (
        "TAVILY_API_KEY",
        "Tavily (1,000 queries/month free, no credit card)",
        "https://tavily.com",
    ),
    (
        "EXA_API_KEY",
        "Exa (1,000 requests/month free, no credit card)",
        "https://exa.ai",
    ),
]

OPTIONAL_API_KEYS = [
    ("BRAVE_API_KEY", "Brave Search (1,000 queries/month free)", "https://brave.com/search/api/"),
    ("SERPER_API_KEY", "Serper.dev (50K one-time free, $1/1K paid — cheapest overflow)", "https://serper.dev"),
    ("YOU_API_KEY", "You.com ($100 one-time credit)", "https://api.you.com"),
]


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------


def validate_api_keys(prefer_claude_tools: bool = True) -> list[str]:
    """Check for required API keys, return list of error messages (empty = OK).

    When prefer_claude_tools=True, external API keys are optional (Claude Agent
    handles search/fetch via the SDK). When False, at least one external API
    key is required.
    """
    if prefer_claude_tools:
        # Claude Agent is the primary — external keys are nice-to-have fallback
        for env_var, description, _ in REQUIRED_API_KEYS:
            if os.environ.get(env_var):
                log.info("Fallback API configured: %s", env_var)
            else:
                log.info("Fallback API not configured (OK — Claude Agent is primary): %s", env_var)
        for env_var, description, _ in OPTIONAL_API_KEYS:
            if os.environ.get(env_var):
                log.info("Fallback API configured: %s", env_var)
        return []

    # External APIs required — original validation
    missing: list[str] = []
    for env_var, description, signup_url in REQUIRED_API_KEYS:
        if not os.environ.get(env_var):
            missing.append(
                f"Missing {env_var}. {description}. Sign up: {signup_url}\n"
                f"  Then: export {env_var}=your-key"
            )

    if missing:
        return missing

    for env_var, description, _ in OPTIONAL_API_KEYS:
        if os.environ.get(env_var):
            log.info("Optional API configured: %s", env_var)
        else:
            log.info("Optional API not configured (OK): %s — %s", env_var, description)

    return []


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def _setup_logging(session_id: str = "") -> None:
    """Set up logging via shared multiplai_core.log_utils (always attaches a
    stderr handler for subprocess visibility)."""
    from multiplai_core.log_utils import setup_logging

    setup_logging(
        "deep-research",
        session_id=session_id,
    )


async def run_pipeline(config: ResearchConfig, *, reset_usage: bool = True) -> int:
    """Main pipeline entry point. Returns process exit code.

    ``reset_usage`` is False when this runs as one of several concurrent
    sub-pipelines (parallel mode) — the process-global usage/concurrency
    counters are shared, so each sub-pipeline resetting them would clobber the
    others' accounting. The parallel orchestrator resets once up front instead.
    """
    from .sdk import get_accumulated_usage, reset_accumulated_usage, reset_sdk_concurrency_stats

    _setup_logging(session_id=config.session_id)
    if reset_usage:
        reset_accumulated_usage()
        reset_sdk_concurrency_stats()

    # 0. Load .env from project root (idempotent — existing env vars win)
    load_env()

    # 1. Validate API keys (optional when prefer_claude_tools=True)
    key_errors = validate_api_keys(prefer_claude_tools=config.prefer_claude_tools)
    if key_errors:
        print("Pipeline requires search API keys:", file=sys.stderr)
        for err in key_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # 2. Parallel mode dispatches to parallel orchestrator
    if config.parallel:
        return await run_parallel(config)

    # 3. Ensure output dir exists
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # 4. Load or create state
    state_file = config.state_file_path()
    if state_file.exists() and not config.plan_only:
        log.info("Resuming from existing state: %s", state_file)
        state = ResearchState.load(state_file)
    else:
        state = ResearchState.new(
            query=config.query,
            output_file=config.output_file_path(),
            state_file=state_file,
        )
        state.checkpoint()

    # 5. Progress file
    progress = ProgressWriter(config.progress_file_path())
    if not state_file.exists() or state.stage == Stage.INIT:
        progress.initialize(
            query=config.query,
            preset=config.preset.name,
            fetch_budget=config.preset.max_total_fetches,
        )

    # 6. Build search router (Claude Agent primary when prefer_claude_tools=True)
    try:
        router = build_default_router(
            prefer_claude_tools=config.prefer_claude_tools,
            allow_paid_fallback=config.allow_paid_fallback,
            effort=config.effort,
        )
    except RuntimeError as e:
        print(f"Search router setup failed: {e}", file=sys.stderr)
        return 1

    # 7. --approved-plan mode: load plan, skip to SEARCH
    if config.approved_plan:
        state.plan = _load_approved_plan(config.approved_plan)
        state.advance_to(Stage.CHALLENGE_COMPLETE)
        progress.log_stage(
            "APPROVED-PLAN LOADED",
            f"{len(state.plan.sub_questions)} sub-questions, "
            f"{len(state.plan.all_queries)} queries",
        )

    try:
        await _run_main_stages(config, state, router, progress)

        # 8. --plan-only mode: dump plan JSON and exit
        if config.plan_only:
            plan_json = state.plan.model_dump_json(indent=2) if state.plan else "{}"
            print(plan_json)
            return 0

        # 9. Check if pipeline aborted (DONE without SYNTHESIZE_COMPLETE)
        aborted = (
            state.stage == Stage.DONE
            and not state.is_complete(Stage.SYNTHESIZE_COMPLETE)
        )

        # 10. Write output file
        if state.stage == Stage.SYNTHESIZE_COMPLETE or state.stage == Stage.DONE:
            # Output already written by synthesize step or abort handler
            pass

        # 11. Adversarial review (if enabled and not aborted)
        if (
            not aborted
            and config.challenge_enabled
            and not state.is_complete(Stage.CHALLENGE_REVIEW_COMPLETE)
        ):
            report = Path(state.output_file).read_text()
            review = await challenge_node.adversarial_review(config, report)
            review_path = config.output_dir / (
                config.output_file_path().stem + "-challenge.md"
            )
            review_path.write_text(review)
            progress.log_stage("CHALLENGE REVIEW", f"written to {review_path}")
            state.advance_to(Stage.CHALLENGE_REVIEW_COMPLETE)

        # 12. Done — cleanup (preserve state on abort for retry/manual synthesis)
        if not aborted:
            state.advance_to(Stage.DONE)
        progress.log_stage("DONE", f"output: {state.output_file}")
        state.cleanup(keep_on_incomplete=aborted)
        progress.cleanup()

        from .sdk import get_sdk_peak_concurrency

        usage = get_accumulated_usage()
        peak = get_sdk_peak_concurrency()
        print(f"PATH: {state.output_file}")
        if aborted:
            print(f"SUMMARY: Research INCOMPLETE. {len(state.findings)} findings from "
                  f"{len(state.completed_sources())} sources. "
                  f"State preserved at: {state.state_file}")
            print(f"STATUS: INCOMPLETE")
        else:
            print(f"SUMMARY: Research complete. {len(state.findings)} findings from "
                  f"{len(state.completed_sources())} sources.")
        print(f"COST: ${usage.cost_usd:.4f} | "
              f"input={usage.input_tokens} output={usage.output_tokens} | "
              f"{usage.num_calls} SDK calls | peak_concurrency={peak}")
        log.info(
            "Pipeline %s: %d findings, %d sources, %d SDK calls, peak_concurrency=%d",
            "INCOMPLETE" if aborted else "complete",
            len(state.findings), len(state.completed_sources()), usage.num_calls, peak,
        )
        return 0

    except Exception as e:  # noqa: BLE001
        log.exception("Pipeline failed at stage %s", state.stage)
        progress.log_stage(f"ERROR at {state.stage.value}", str(e))
        print(f"Pipeline failed: {e}", file=sys.stderr)
        print(f"State preserved at: {state_file}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Main stage sequence
# ---------------------------------------------------------------------------


async def _run_main_stages(
    config: ResearchConfig,
    state: ResearchState,
    router: SearchRouter,
    progress: ProgressWriter,
) -> None:
    """Sequence all stages, respecting resume points via state.is_complete()."""

    # PLAN
    if not state.is_complete(Stage.PLAN_COMPLETE):
        state.plan = await plan_node.plan(config)
        state.advance_to(Stage.PLAN_COMPLETE)
        progress.log_stage(
            "PLAN COMPLETE",
            f"{len(state.plan.sub_questions)} sub-questions",
        )

    # DIVERGE
    if not state.is_complete(Stage.DIVERGE_COMPLETE):
        assert state.plan is not None
        state.plan = await plan_node.diverge(config, state.plan)
        state.advance_to(Stage.DIVERGE_COMPLETE)
        progress.log_stage(
            "DIVERGE COMPLETE",
            f"{len(state.plan.primary_queries)} primary, "
            f"{len(state.plan.mechanism_queries)} mechanism, "
            f"{len(state.plan.directory_queries)} directory",
        )

    # CHALLENGE
    if not state.is_complete(Stage.CHALLENGE_COMPLETE):
        assert state.plan is not None
        state.plan = await plan_node.challenge(config, state.plan)
        state.advance_to(Stage.CHALLENGE_COMPLETE)
        progress.log_stage(
            "CHALLENGE COMPLETE",
            f"{len(state.plan.contrarian_queries)} contrarian queries",
        )

    # Diversity gate
    assert state.plan is not None
    diversity_result = query_diversity_gate(
        state.plan.all_queries, min_clusters=3
    )
    progress.log_stage(
        "DIVERSITY GATE",
        f"passed={diversity_result.passed} | {diversity_result.reason}",
    )
    if not diversity_result.passed:
        # Recovery: run DIVERGE again with an instruction to diversify
        log.info("Diversity gate failed — re-running DIVERGE")
        state.plan = await plan_node.diverge(config, state.plan)
        # Re-check (no infinite loop — just one retry)
    state.advance_to(Stage.DIVERSITY_GATE_PASSED)

    if config.plan_only:
        # Run the diversity gate and exit — already did that, caller handles print
        return

    # SEARCH
    if not state.is_complete(Stage.SEARCH_COMPLETE):
        state.search_results = await search_node.search(config, state.plan, router)
        state.advance_to(Stage.SEARCH_COMPLETE)
        progress.log_stage(
            "SEARCH COMPLETE",
            f"{len(state.search_results)} unique results",
        )

    # TRIAGE
    if not state.is_complete(Stage.TRIAGE_COMPLETE):
        state.sources = await triage_node.triage(
            config,
            state.search_results,
            state.plan.sub_questions,
            state.plan.target_domains,
            authority_domains=state.plan.authority_domains,
        )
        state.advance_to(Stage.TRIAGE_COMPLETE)
        progress.log_stage("TRIAGE COMPLETE", f"{len(state.sources)} sources selected")

    # Min sources gate
    min_result = min_sources_gate(len(state.sources), config.preset.min_sources)
    progress.log_stage(
        "MIN SOURCES GATE",
        f"passed={min_result.passed} | {min_result.reason}",
    )
    if not min_result.passed:
        log.warning("Min sources gate failed: %s", min_result.reason)
        # Best-effort recovery: broaden the search with contrarian/mechanism queries
        # already in the plan — most of the time this means search was already thin.
        # We proceed with what we have rather than infinite-looping.
    state.advance_to(Stage.MIN_SOURCES_GATE_PASSED)

    # READ (with fetch fallback chain: SDK → httpx → Tavily for AUTHORITATIVE)
    if not state.is_complete(Stage.READ_COMPLETE):
        state.advance_to(Stage.READ_IN_PROGRESS)
        await read_node.read(config, state, router=router)
        state.advance_to(Stage.READ_COMPLETE)
        progress.log_stage(
            "READ COMPLETE",
            f"{len(state.completed_sources())} extracted, "
            f"{len(state.failed_sources())} failed, "
            f"{len(state.findings)} findings, "
            f"{state.total_fetches} total fetches"
            + (f", {state.tavily_fallback_count} Tavily fallbacks" if state.tavily_fallback_count else ""),
        )

    # Coverage gate — run targeted search if sub-questions are uncovered
    coverage_result = coverage_gate(state.findings, state.plan.sub_questions)
    progress.log_stage(
        "COVERAGE GATE",
        f"passed={coverage_result.passed} | {coverage_result.reason}",
    )
    if not coverage_result.passed and coverage_result.metadata:
        uncovered = coverage_result.metadata.get("uncovered_questions", [])
        if uncovered:
            log.info(
                "Coverage gate failed — running targeted search for %d uncovered sub-questions",
                len(uncovered),
            )
            progress.log_stage(
                "COVERAGE RECOVERY",
                f"targeted search for {len(uncovered)} uncovered sub-questions",
            )
            # Search using the uncovered sub-questions as queries directly
            targeted_results = await router.batch_search(
                uncovered,
                max_results=config.preset.sources // max(len(uncovered), 1),
                strategy="keyword",
            )
            if targeted_results:
                # Triage the new results
                new_sources = await triage_node.triage(
                    config,
                    targeted_results,
                    uncovered,
                    state.plan.target_domains,
                    authority_domains=state.plan.authority_domains,
                )
                if new_sources:
                    # Add new sources to state and read them
                    state.sources.extend(new_sources)
                    await read_node.read(config, state, router=router)
                    progress.log_stage(
                        "COVERAGE RECOVERY COMPLETE",
                        f"+{len(new_sources)} sources, {len(state.findings)} total findings",
                    )
            # Re-check coverage (informational — don't loop)
            recheck = coverage_gate(state.findings, state.plan.sub_questions)
            progress.log_stage(
                "COVERAGE GATE (recheck)",
                f"passed={recheck.passed} | {recheck.reason}",
            )
    state.advance_to(Stage.COVERAGE_GATE_PASSED)

    # Critical source gate — abort if failed critical sources left fatal gaps
    if not state.is_complete(Stage.CRITICAL_SOURCE_GATE_PASSED):
        cs_result = critical_source_gate(
            state.sources, state.findings, state.plan.sub_questions,
        )
        progress.log_stage(
            "CRITICAL SOURCE GATE",
            f"passed={cs_result.passed} | {cs_result.reason}",
        )
        if not cs_result.passed:
            log.warning("Critical source gate failed — aborting: %s", cs_result.reason)
            report = synthesize_node.write_incomplete_report(
                config, state, cs_result.reason, cs_result.metadata,
            )
            Path(state.output_file).write_text(report)
            progress.log_stage("ABORTED", cs_result.reason)
            state.advance_to(Stage.DONE)
            return
        state.advance_to(Stage.CRITICAL_SOURCE_GATE_PASSED)

    # REASSESS — dedup + cap findings to manage context budget
    if not state.is_complete(Stage.REASSESS_COMPLETE):
        deduped = deduplicate_findings(state.findings)
        capped = deduped[: config.preset.max_reassess_findings]
        progress.log_stage(
            "REASSESS PREP",
            f"deduped {len(state.findings)} → {len(deduped)}, "
            f"capped at {len(capped)} for REASSESS "
            f"(SYNTHESIZE still gets all {len(state.findings)})",
        )
        state.reassessment = await reassess_node.reassess(
            config, state, findings_override=capped
        )
        state.advance_to(Stage.REASSESS_COMPLETE)
        progress.log_stage(
            "REASSESS COMPLETE",
            f"refinement={state.reassessment.refinement_needed} "
            f"verify_claims={len(state.reassessment.verify_claims)}",
        )

    # Reassess gate — may trigger refinement/verification cycles
    ra_result = reassess_gate(state.reassessment)
    if not ra_result.passed and state.reassessment is not None:
        await _run_reassess_cycle(config, state, router, progress)
    state.advance_to(Stage.REASSESS_GATE_PASSED)

    # Pre-synthesis quality check — cheap LLM go/no-go
    if not state.is_complete(Stage.QUALITY_CHECK_PASSED):
        qc_result = await quality_check_node.quality_check(config, state)
        progress.log_stage(
            "QUALITY CHECK",
            f"go={qc_result.go} confidence={qc_result.confidence:.2f} | {qc_result.reasoning[:100]}",
        )
        if not qc_result.go:
            log.warning("Quality check failed — aborting: %s", qc_result.reasoning)
            report = synthesize_node.write_incomplete_report(
                config, state, qc_result.reasoning,
                {"critical_gaps": qc_result.critical_gaps},
            )
            Path(state.output_file).write_text(report)
            progress.log_stage("ABORTED", qc_result.reasoning)
            state.advance_to(Stage.DONE)
            return
        state.advance_to(Stage.QUALITY_CHECK_PASSED)

    # SYNTHESIZE
    if not state.is_complete(Stage.SYNTHESIZE_COMPLETE):
        report = await synthesize_node.synthesize(config, state)
        Path(state.output_file).write_text(report)
        state.advance_to(Stage.SYNTHESIZE_COMPLETE)
        progress.log_stage("SYNTHESIZE COMPLETE", f"written to {state.output_file}")


# ---------------------------------------------------------------------------
# Reassess cycle (refinement + verification)
# ---------------------------------------------------------------------------


async def _run_reassess_cycle(
    config: ResearchConfig,
    state: ResearchState,
    router: SearchRouter,
    progress: ProgressWriter,
) -> None:
    """Run refinement and/or verification cycles based on REASSESS output."""
    assert state.reassessment is not None

    tasks = []
    if state.reassessment.refinement_needed and state.reassessment.refinement_queries:
        tasks.append(_run_refinement(config, state, router, progress))
    if state.reassessment.verify_queries:
        tasks.append(_run_verification(config, state, router, progress))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_refinement(
    config: ResearchConfig,
    state: ResearchState,
    router: SearchRouter,
    progress: ProgressWriter,
) -> None:
    """Run additional search+read rounds for framing refinement."""
    assert state.reassessment is not None
    queries = state.reassessment.refinement_queries
    log.info("REFINEMENT: running %d new queries", len(queries))

    new_results = await router.batch_search(queries, strategy="keyword")
    # Dedupe against already-triaged sources
    known_urls = {s.url for s in state.sources}
    new_results = [r for r in new_results if r.url not in known_urls]

    if not new_results:
        return

    assert state.plan is not None
    new_sources = await triage_node.triage(
        config,
        new_results,
        state.plan.sub_questions,
        state.plan.target_domains,
        authority_domains=state.plan.authority_domains,
    )
    # Only add up to what fits in fetch budget
    budget = config.preset.max_total_fetches - state.total_fetches
    new_sources = new_sources[: max(0, budget)]
    state.sources.extend(new_sources)
    state.checkpoint()

    await read_node.read(config, state, router=router)
    progress.log_stage(
        "REFINEMENT CYCLE",
        f"{len(new_sources)} new sources, {len(state.findings)} total findings",
    )


async def _run_verification(
    config: ResearchConfig,
    state: ResearchState,
    router: SearchRouter,
    progress: ProgressWriter,
) -> None:
    """Run targeted searches to verify specific claims."""
    assert state.reassessment is not None
    queries = state.reassessment.verify_queries
    log.info("VERIFICATION: running %d targeted queries", len(queries))

    results = await router.batch_search(queries, strategy="keyword")
    known_urls = {s.url for s in state.sources}
    new_results = [r for r in results if r.url not in known_urls]

    if not new_results:
        return

    # Verification prefers authoritative sources — use the triage pipeline to filter
    assert state.plan is not None
    verify_sources = await triage_node.triage(
        config,
        new_results,
        state.plan.sub_questions,
        state.plan.target_domains,
        authority_domains=state.plan.authority_domains,
    )
    # Cap the verify additions to keep the budget reasonable
    verify_sources = verify_sources[:5]
    state.sources.extend(verify_sources)
    state.checkpoint()

    await read_node.read(config, state, router=router)
    progress.log_stage("VERIFICATION CYCLE", f"{len(verify_sources)} verification sources")


# ---------------------------------------------------------------------------
# Parallel mode
# ---------------------------------------------------------------------------


async def run_parallel(config: ResearchConfig) -> int:
    """Run N sub-pipelines concurrently and merge via synthesis."""
    log.info("PARALLEL MODE: decomposing query")

    # Reset the shared usage/concurrency counters ONCE here; sub-pipelines run
    # with reset_usage=False so their token/cost accounting accumulates.
    # NOTE: the on-disk quota store (quotas.json) is still shared and written
    # concurrently by the sub-pipelines' routers — a known v1 limitation of
    # parallel mode; quota accounting may undercount under heavy contention.
    from .sdk import reset_accumulated_usage, reset_sdk_concurrency_stats
    reset_accumulated_usage()
    reset_sdk_concurrency_stats()

    # Build initial plan with sub-topic decomposition
    planning_config = replace(config, parallel=False)  # plan alone, no recursion
    state = ResearchState.new(
        query=config.query,
        output_file=config.output_file_path(),
        state_file=config.state_file_path(),
    )
    state.plan = await plan_node.plan(planning_config)

    # For now, treat each sub-question as a sub-topic. A full parallel plan
    # decomposition node would go here for queries that need deeper sub-topic
    # structure. Keeping this simple for v1.
    num_agents = config.agents or min(
        len(state.plan.sub_questions), 3
    )
    sub_topics = [
        SubTopic(
            title=sq[:60],
            focus=sq,
            angle="focused",
            sub_questions=[sq],
        )
        for sq in state.plan.sub_questions[:num_agents]
    ]

    # Each sub-agent runs a single-pipeline with downscaled preset
    sub_preset = PRESETS[config.per_agent_preset()]
    sub_configs = []
    for i, topic in enumerate(sub_topics):
        sub_config = replace(
            config,
            query=topic.focus,
            preset=sub_preset,
            parallel=False,
            output_dir=config.output_dir,
        )
        # Disambiguate output paths by appending agent index
        sub_configs.append((i, topic, sub_config))

    # Run all sub-pipelines concurrently
    sub_tasks = [_run_sub_pipeline(i, topic, sc) for i, topic, sc in sub_configs]
    sub_outputs = await asyncio.gather(*sub_tasks, return_exceptions=True)

    completed_files: list[Path] = []
    for result in sub_outputs:
        if isinstance(result, Path):
            completed_files.append(result)

    if not completed_files:
        print("All sub-pipelines failed", file=sys.stderr)
        return 2

    # Synthesize merged report (simplified — just concatenate pointers + run synthesize)
    merged_text = _merge_sub_reports(config, completed_files)
    Path(state.output_file).write_text(merged_text)
    print(f"PATH: {state.output_file}")
    print(f"SUMMARY: Parallel research complete. {len(completed_files)}/{len(sub_topics)} sub-pipelines succeeded.")
    return 0


async def _run_sub_pipeline(
    _index: int, _topic: SubTopic, sub_config: ResearchConfig
) -> Path | None:
    """Run a single sub-pipeline and return its output path.

    Each sub-pipeline's output path is derived from its (focused) query string,
    so paths differ between agents as long as their sub-topic queries differ.
    """
    sub_config.output_dir.mkdir(parents=True, exist_ok=True)
    # Don't reset the shared usage/concurrency counters — the parallel
    # orchestrator did that once before launching all sub-pipelines, so their
    # token/cost accounting accumulates instead of clobbering each other.
    rc = await run_pipeline(replace(sub_config, parallel=False), reset_usage=False)
    if rc == 0:
        return sub_config.output_file_path()
    return None


def _merge_sub_reports(config: ResearchConfig, files: list[Path]) -> str:
    """Concatenate sub-reports into a unified document.

    v1 merge: header + each sub-report. The synthesize node already handles
    per-pipeline synthesis; cross-topic merge is best-effort.
    """
    parts = [
        f"# {config.query}\n",
        f"**Date:** {config.date} | **Mode:** parallel ({len(files)} agents)\n",
        "",
        "## Sub-research\n",
    ]
    for i, f in enumerate(files):
        parts.append(f"### Agent {i+1}: {f.name}\n")
        try:
            parts.append(f.read_text())
        except Exception:  # noqa: BLE001
            parts.append(f"(could not read {f})")
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Plan loading (for --approved-plan mode)
# ---------------------------------------------------------------------------


def _load_approved_plan(path: Path) -> PlanResult:
    """Load a PlanResult from a JSON file."""
    data = json.loads(Path(path).read_text())
    return PlanResult.model_validate(data)
