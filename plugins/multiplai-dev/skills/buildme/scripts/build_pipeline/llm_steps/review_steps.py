"""LLM step functions for code and security review.

Each function calls llm_call_structured() or llm_call() to produce
ReviewResult objects for gate evaluation.
"""

from __future__ import annotations

import asyncio
import logging

from ..models import AgentResult, ReviewFinding, ReviewResult, ReviewScore
from ..prompts.review import CODE_REVIEW_PROMPT, SECURITY_REVIEW_PROMPT
from ..sdk import llm_call, llm_call_structured, agent_call, LLMCallError

log = logging.getLogger(__name__)


def _panel_models(config) -> list[str]:
    """The reviewer models for one block review, in panel order.

    Empty/absent `code_review.panel` → a single reviewer on the existing
    review_model-or-model choice, i.e. exactly today's behavior. A panel is
    opt-in because every extra member is another full-diff call.
    """
    default = getattr(config, "review_model", None) or config.model
    panel = getattr(config, "review_panel", None) or []
    return [m for m in panel if m] or [default]


def _merge_scores(results: list[ReviewResult]) -> list[ReviewScore]:
    """Merge per-dimension scores across panel members.

    Two reviewers agreeing on a dimension is a stronger signal than one
    asserting it, and two *disagreeing* is a weaker one. So the merged score is
    the mean and the merged confidence is scaled down by the spread: a 5-vs-1
    split (spread 4 on a 1-5 scale) collapses confidence to zero, which the
    graded gate then reads as "no information" rather than as a verdict.

    A single reviewer has zero spread, so its confidence passes through
    untouched — the N=1 default is byte-identical to the old behavior.
    """
    by_dim: dict[str, list[ReviewScore]] = {}
    for r in results:
        for s in r.scores:
            by_dim.setdefault(s.dimension, []).append(s)

    merged: list[ReviewScore] = []
    for dim, scores in by_dim.items():
        raw = [s.score for s in scores]
        mean = sum(raw) / len(raw)
        spread = (max(raw) - min(raw)) / 4.0  # 4 = full 1..5 range
        agreement = max(0.0, 1.0 - spread)
        confidence = (sum(s.confidence for s in scores) / len(scores)) * agreement
        merged.append(
            ReviewScore(
                dimension=dim,
                weight=max(s.weight for s in scores),
                score=round(mean),
                evidence="\n".join(f"[{i + 1}] {s.evidence}" for i, s in enumerate(scores)),
                confidence=round(min(1.0, max(0.0, confidence)), 3),
            )
        )
    return merged


def _merge_findings(results: list[ReviewResult], labels: list[str]) -> list[ReviewFinding]:
    """Dedupe findings across the panel, raising confidence on agreement.

    Independent confirmation is combined noisy-or (`1 - Π(1 - c)`): two
    reviewers each 60% sure of the same defect land at 84%, which is the
    honest reading of two independent looks. One reviewer's finding passes
    through at its own confidence.
    """
    merged: dict[tuple, ReviewFinding] = {}
    for label, result in zip(labels, results):
        for f in result.findings_or_derived():
            key = f.dedupe_key()
            existing = merged.get(key)
            if existing is None:
                merged[key] = f.model_copy(update={"reviewers": [label]})
                continue
            combined = 1.0 - (1.0 - existing.confidence) * (1.0 - f.confidence)
            existing.confidence = round(min(1.0, combined), 3)
            existing.reviewers = [*existing.reviewers, label]
            # Keep the harshest severity anyone assigned — a Critical seen by
            # one reviewer must not be softened by another's Minor.
            order = ["Note", "Minor", "Major", "Critical"]
            if order.index(f.severity.title() if f.severity.title() in order else "Minor") > order.index(
                existing.severity.title() if existing.severity.title() in order else "Minor"
            ):
                existing.severity = f.severity
            if f.evidence and f.evidence not in existing.evidence:
                existing.evidence = f"{existing.evidence}\n{f.evidence}".strip()
    return list(merged.values())


def merge_panel_results(results: list[ReviewResult], labels: list[str]) -> ReviewResult:
    """Fold N reviewers' results into one.

    Spec verdicts (missing/misunderstood/extra) are UNIONED, not intersected:
    the whole reason to run a panel is that reviewers find disjoint sets, so
    requiring consensus would throw away exactly the findings the panel exists
    to surface. Adjudication — not consensus — is what filters false positives.
    """
    if len(results) == 1:
        only = results[0]
        return only.model_copy(update={"panel": list(labels)})

    def _union(attr: str) -> list[str]:
        seen: list[str] = []
        for r in results:
            for item in getattr(r, attr):
                if item not in seen:
                    seen.append(item)
        return seen

    return ReviewResult(
        scores=_merge_scores(results),
        issues=[i for r in results for i in r.issues],
        findings=_merge_findings(results, labels),
        strengths=_union("strengths"),
        missing=_union("missing"),
        extra=_union("extra"),
        misunderstood=_union("misunderstood"),
        panel=list(labels),
    )


# WIRED: this is the active per-block quality review — called from
# tdd_engine._run_quality_review with the block's actual diff, the rubric,
# and the project's coding standards.
async def run_code_review(
    diff: str,
    rubric: str,
    config,
    *,
    spec_context: str = "",
    standards: str = "",
    implementer_report: str = "",
) -> ReviewResult:
    """Run the two-verdict code review (spec compliance + rubric scores).

    Runs every model in `config.review_panel` concurrently, each in its own
    fresh single-turn context (models miss their own errors in-context but
    catch them with fresh context, and different families find largely
    disjoint error sets), then merges. With no panel configured this is one
    call — unchanged from before.

    Args:
        diff: The git diff to review
        rubric: The rubric.md content
        config: BuildConfig for model selection (config.review_panel, else
            config.review_model, else config.model)
        spec_context: The spec scenarios this block must satisfy (verbatim)
        standards: Coding-standards doc contents pushed into the reviewer's
            context (empty → the prompt says "(no standards provided)")
        implementer_report: The implementer's own report + RED/GREEN evidence,
            presented to the reviewer as unverified claims

    Returns:
        ReviewResult with scores, findings, issues, and the
        missing/extra/misunderstood spec verdict.
    """
    prompt = CODE_REVIEW_PROMPT.format(
        diff=diff or "(no diff captured)",
        rubric=rubric,
        spec_context=spec_context or "(no spec context provided)",
        standards=standards or "(no standards provided)",
        implementer_report=implementer_report or "(no implementer report provided)",
    )

    models = _panel_models(config)
    log.info("Running code review (%d bytes diff, panel=%s)", len(diff), models)

    # Concurrent: the panel's whole cost is latency, and sdk.py's semaphore
    # (MAX_CONCURRENT_SDK_CALLS) already bounds how many actually run at once.
    results = await asyncio.gather(*[
        llm_call_structured(
            prompt, ReviewResult, model=m, max_retries=1, budget_label="review",
        )
        for m in models
    ])

    result = merge_panel_results(list(results), models)
    log.info(
        "Code review: panel=%d weighted_avg=%.1f passed=%s issues=%d findings=%d",
        len(models),
        result.weighted_average,
        result.passed,
        len(result.issues),
        len(result.findings),
    )
    return result


# NOTE: not currently wired into the pipeline. No caller invokes a dedicated
# security review; there is no active security gate.
async def run_security_review(
    diff: str,
    rubric: str,
    config,
) -> ReviewResult:
    """Run security-focused review of code changes.

    Args:
        diff: The git diff to review
        rubric: The rubric.md content (for context)
        config: BuildConfig for model selection

    Returns:
        ReviewResult with security-focused scores and issues.
    """
    prompt = SECURITY_REVIEW_PROMPT.format(
        diff=diff,
        rubric=rubric,
    )

    log.info("Running security review (%d bytes diff)", len(diff))
    result = await llm_call_structured(
        prompt,
        ReviewResult,
        model=config.model,
        max_retries=1,
        budget_label="security_review",
    )
    log.info(
        "Security review: weighted_avg=%.1f issues=%d",
        result.weighted_average,
        len(result.issues),
    )
    return result


# NOTE: not currently wired into the pipeline (pairs with run_security_review,
# which is also not wired; the review-fix loop in tdd_engine uses run_implementer).
async def run_review_fix(
    issues: list[dict],
    diff: str,
    config,
) -> AgentResult:
    """Spawn an agent to fix review issues.

    Args:
        issues: List of issue dicts from ReviewResult
        diff: The original diff for context
        config: BuildConfig for model and project dir

    Returns:
        AgentResult from the fix agent.
    """
    issues_text = "\n".join(
        f"- [{i.get('severity', 'Unknown')}] {i.get('dimension', '')}: {i.get('description', '')}"
        + (f" ({i.get('file_path', '')}:{i.get('line', '')})" if i.get('file_path') else "")
        for i in issues
    )

    prompt = (
        "Fix the following review issues in the codebase.\n\n"
        f"## Issues to Fix\n{issues_text}\n\n"
        f"## Original Diff Context\n```\n{diff[:5000]}\n```\n\n"
        "Fix each issue. Run tests after fixing to ensure nothing breaks.\n"
        f"Test command: {config.test_command or 'pytest -xvs'}\n"
        f"Project dir: {config.project_dir}\n"
    )

    log.info("Spawning review fix agent for %d issues", len(issues))
    result = await agent_call(
        prompt,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        model=config.model,
        max_turns=20,
        cwd=str(config.project_dir),
        budget_label="review_fix",
    )
    log.info("Review fix agent: success=%s turns=%d", result.success, result.turns_used)
    return result
