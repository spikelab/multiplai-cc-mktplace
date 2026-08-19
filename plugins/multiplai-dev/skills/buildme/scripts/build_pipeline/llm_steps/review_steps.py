"""LLM step functions for code review.

The review itself runs as a read-only subagent (agent_call_structured with
`REVIEWER_TOOLS`), producing ReviewResult objects for gate evaluation.
"""

from __future__ import annotations

import asyncio
import logging
import math

from ..models import AgentResult, ReviewFinding, ReviewResult, ReviewScore
from ..prompts.review import CODE_REVIEW_PROMPT
from ..sdk import agent_call, agent_call_structured, LLMCallError

log = logging.getLogger(__name__)

# The reviewer's tool set, and the reason it is a constant: a reviewer that can
# `Write`, `Edit` or `Bash` is a reviewer that can fix what it found, which
# destroys the adjudication seam (findings are proposals the orchestrator
# disposes of) and walks straight past `unchanged_tests_gate`, whose whole
# premise is that only the implementer's windows are writable. Read-only, no
# exceptions — `test_no_review_step_can_write` asserts it.
REVIEWER_TOOLS = ["Read", "Grep", "Glob"]

# Reviewers are not implementers: enough turns to open the changed files, grep
# for callers and read the conventions docs, not enough to go wandering.
REVIEWER_MAX_TURNS = 30


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

    A dimension only ONE member of a larger panel scored is discounted by its
    coverage: nobody corroborated it, and zero spread would otherwise hand an
    uncorroborated score full confidence — indistinguishable from unanimous
    agreement. With a single-member panel coverage is 1.0, so the N=1 default
    stays byte-identical to the old behavior.
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
        coverage = len(scores) / len(results)
        confidence = (
            (sum(s.confidence for s in scores) / len(scores)) * agreement * coverage
        )
        merged.append(
            ReviewScore(
                dimension=dim,
                weight=max(s.weight for s in scores),
                # Explicit half-up. Bare round() is banker's rounding, which
                # sends a 2/3 split to 2 and a 3/4 split to 4 — a tie-break rule
                # nobody chose and one that differs by dimension.
                score=math.floor(mean + 0.5),
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

    Each reviewer is a read-only subagent (`REVIEWER_TOOLS`) rooted at the
    project dir, so it can open the file around a hunk, grep for a changed
    symbol's callers, and read the conventions docs before making a claim —
    the diff is still ground truth for *what changed*, but it is no longer the
    only thing the reviewer can see.

    Runs every model in `config.review_panel` concurrently, each in its own
    fresh context (models miss their own errors in-context but catch them with
    fresh context, and different families find largely disjoint error sets),
    then merges. With no panel configured this is one call.

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
    #
    # return_exceptions=True is load-bearing: without it a single member raising
    # (post-retry, or a cross-family backend that isn't reachable) propagates out
    # of here and the caller marks the block FAILED — making a 3-member panel ~3×
    # MORE likely to fail the review than the single-reviewer default, the exact
    # opposite of the feature's purpose. Survivors are merged; only an
    # all-members-failed panel is a real failure.
    settled = await asyncio.gather(*[
        agent_call_structured(
            prompt, ReviewResult, allowed_tools=REVIEWER_TOOLS,
            model=m, effort=config.review_effort,
            max_retries=1, max_turns=REVIEWER_MAX_TURNS,
            cwd=str(config.project_dir), budget_label="review",
        )
        for m in models
    ], return_exceptions=True)

    results: list[ReviewResult] = []
    survivors: list[str] = []
    for model_name, outcome in zip(models, settled):
        if isinstance(outcome, BaseException):
            log.warning("Review panel member %s failed, dropping it from the merge: %s",
                        model_name, outcome)
            continue
        results.append(outcome)
        survivors.append(model_name)

    if not results:
        # Every member failed — there is no review, so raise rather than hand
        # back an empty ReviewResult that would score 0 and read as "rejected".
        raise LLMCallError(
            f"Review panel failed on every member ({', '.join(models)}); see the "
            f"warnings above for each member's error."
        )
    if len(results) < len(models):
        log.warning("Review panel degraded: %d of %d members returned (%s)",
                    len(results), len(models), ", ".join(survivors))

    result = merge_panel_results(results, survivors)
    log.info(
        "Code review: panel=%d weighted_avg=%.1f passed=%s issues=%d findings=%d",
        len(models),
        result.weighted_average,
        # The configured policy, not the default — otherwise this line reports a
        # verdict the gate does not apply (see ReviewResult.passed_with).
        result.passed_with(getattr(config, "review_gate", None)),
        len(result.issues),
        len(result.findings),
    )
    return result


# NOTE: not currently wired into the pipeline. No caller invokes it; the
# review-fix loop in tdd_engine uses run_implementer instead.
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
        effort=config.agent_effort,
        max_turns=20,
        cwd=str(config.project_dir),
        budget_label="review_fix",
    )
    log.info("Review fix agent: success=%s turns=%d", result.success, result.turns_used)
    return result
