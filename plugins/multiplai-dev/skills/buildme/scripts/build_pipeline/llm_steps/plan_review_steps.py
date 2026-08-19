"""The PLAN_REVIEW phase — a second engineer reads the plan before it is built.

The board's Planning column has always claimed "specs -> impl plan, **reviewed
by another eng**". This module is that review. It runs after DESIGN_AUDIT (so
`tasks.md` and `rubric.md` both exist) and before PROTOTYPE.

Three properties are load-bearing and none of them is decoration:

* **Read-only.** `PLAN_REVIEW_TOOLS` is `Read`/`Grep`/`Glob` and nothing else.
  A reviewer that could `Write` or `Edit` would fix the plan it was asked to
  judge, and the one-regeneration-pass rule would stop being the only way an
  artifact changes.
* **One pass.** The stage regenerates `tasks.md` at most once
  (`SpecGenState.plan_review_regen_done`) and then re-checks **report-only**.
  The stage as a whole is recorded in `SpecGenState.plan_review_done` and
  checked BEFORE the model call, so a resume costs nothing. A loop here is an
  unbounded bill, not a better review.
* **It proposes; a gate disposes.** Nothing here creates a ticket, moves a
  card, or splits `tasks.md`. An `oversized-plan` finding carries a *proposed*
  cut and stops there.

The plan puts this step in `review_steps.py`; it lives in its own module
because that file was being edited concurrently. Nothing about the phase
depends on which module it sits in.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import PlanReviewFinding, PlanReviewResult
from ..prompts.plan_review import NO_USE_CASES, PLAN_REVIEW_PROMPT
from ..sdk import agent_call_structured

log = logging.getLogger(__name__)

# Read-only, exactly like the design/tasks auditors. `test_no_review_step_can_write`
# in test_review_steps.py asserts the shape of lists like this one.
PLAN_REVIEW_TOOLS = ["Read", "Grep", "Glob"]

# Enough turns to open the plan's neighbours and grep the codebase for a name
# the plan claims exists; not enough to go wandering.
PLAN_REVIEW_MAX_TURNS = 30

# Severities that earn the single regeneration pass. A minor finding is a
# clarification, not a reason to spend another spec-generation call.
PLAN_REVIEW_ACTIONABLE = ("critical", "major")

# `oversized-plan` is deliberately NOT regenerable. Its output is a proposed
# cut across several tickets, and acting on it means creating tickets and
# splitting tasks.md — the irreversible act a human (or the next gate)
# performs, never this phase. Feeding it to a regeneration would have the
# rewriter silently perform the split inside one plan, which is the opposite of
# what the finding asks for.
PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES = ("oversized-plan",)


def _read(path: Path, missing: str) -> str:
    """A document's text, or an explicit placeholder when it is absent."""
    try:
        if path.exists():
            return path.read_text()
    except OSError as err:  # unreadable is the same as absent, but say so
        log.warning("Could not read %s for the plan review: %s", path, err)
    return missing


# How many signatures a single line of the rendered context lists before it
# says "+N more". This is prompt context, not a report: the reviewer has
# tasks.md in the same prompt and can read the rest there.
SPLIT_CONTEXT_SIGNATURE_LIMIT = 8

# What the context says when tasks.md parses but carries no `Interfaces:`
# section anywhere. There is no graph to hand over, so the reviewer does its
# own reading exactly as it did before this was wired — said out loud rather
# than returning "" so nobody mistakes the silence for "one atomic group".
NO_GRAPH_NOTE = (
    "## Block dependency partition\n"
    "(graph unavailable — the blocks in tasks.md declare no `Interfaces:` "
    "signatures, so buildme has no dependency graph to hand you. Do the "
    "separability partition yourself from the plan's prose.)"
)


def _blocks_phrase(numbers: list[int]) -> str:
    """`block 3` / `blocks 1, 2` — a finding names blocks the way a human does."""
    joined = ", ".join(str(n) for n in numbers)
    return f"block{'' if len(numbers) == 1 else 's'} {joined}"


def _signature_line(label: str, signatures: list[str]) -> str:
    """One `label: sig; sig; ...` line, capped, or `(none)`."""
    if not signatures:
        return f"    {label}: (none)"
    shown = signatures[:SPLIT_CONTEXT_SIGNATURE_LIMIT]
    text = "; ".join(shown)
    extra = len(signatures) - len(shown)
    if extra:
        text += f"; (+{extra} more)"
    return f"    {label}: {text}"


def render_split_context(assessment) -> str:
    """Render a :class:`PlanSplitAssessment` as the prompt's `{split_context}`.

    Two things a finding has to carry, so both are here verbatim: **which
    blocks go in which group**, and **the exact signature boundary each cut
    crosses**. The atomicity verdict quotes the literal keyword that matched
    (the classification is a substring heuristic, so the evidence travels with
    it) and the package spread names which block pulled the plan into which
    package.

    The triggers are reported as triggers: a fired count is what makes checks 1
    and 2 worth running, never a verdict. The block trigger's default of 8 is
    an unmeasured placeholder and the rendered text says so, so no reviewer
    treats a count as a finding.

    Pure: formats the assessment, reads nothing, changes nothing.
    """
    split = assessment.split
    atomicity = assessment.atomicity
    spread = assessment.spread

    lines = [
        "## Block dependency partition (parsed by buildme from `Interfaces:`)",
        "",
        "buildme parsed every block's `Interfaces:` section out of tasks.md and "
        "partitioned the resulting signature graph. Read this instead of "
        "redoing the partition by hand — and say so if the plan's prose "
        "contradicts it.",
        "",
        f"Blocks: {assessment.block_count}. "
        f"Size trigger (>{assessment.block_trigger}): "
        f"{'FIRED' if assessment.size_triggered else 'not fired'}. "
        f"Package trigger (>{assessment.package_trigger}): "
        f"{'FIRED' if assessment.package_triggered else 'not fired'}.",
        "A trigger is not a verdict, and the block trigger's default is an "
        "unmeasured placeholder — it only decides whether checks 1 and 2 are "
        "worth running.",
        "",
        f"### Separability — {split.group_count} independently-shippable group(s)",
    ]

    for group in split.groups:
        names = "; ".join(group.block_names)
        lines.append(
            f"- Group {group.index + 1}: {_blocks_phrase(group.block_numbers)} "
            f"— {names}"
        )
        lines.append(_signature_line("produces", group.produces))
        lines.append(_signature_line("consumes", group.consumes))

    for boundary in split.boundaries:
        lines.append(
            f"- Cut after group {boundary.after_group + 1}: between block "
            f"{boundary.last_block_before} and block "
            f"{boundary.first_block_after}"
        )
        if boundary.crossing_signatures:
            lines.append(_signature_line("crosses", boundary.crossing_signatures))
        else:
            lines.append("    crosses: nothing — no signature spans this cut")
        lines.append(_signature_line("boundary", boundary.boundary_signatures))

    lines.append(f"Verdict: {split.reason}")
    if split.unresolved_consumes:
        lines.append(_signature_line(
            "Consumed but produced outside this plan",
            split.unresolved_consumes,
        ))

    lines.append("")
    lines.append(
        "### Atomicity — "
        + ("SPLIT: high-risk work ships beside unrelated feature work"
           if atomicity.should_split else "no unrelated feature work to isolate")
    )
    lines.append(atomicity.reason)
    for high_risk in atomicity.high_risk_blocks:
        evidence = "; ".join(
            f"{kind}: "
            + ", ".join(f'"{kw}"' for kw in high_risk.matched_keywords.get(kind, []))
            for kind in high_risk.kinds
        )
        lines.append(
            f"- block {high_risk.number} ({high_risk.name}) — matched {evidence}"
        )
    if atomicity.unrelated_block_numbers:
        unrelated = ", ".join(
            f"block {n} ({name})" for n, name
            in zip(atomicity.unrelated_block_numbers, atomicity.unrelated_block_names)
        )
        lines.append(f"- unrelated feature work: {unrelated}")

    lines.append("")
    lines.append(f"### Package spread — {spread.count} top-level package(s)")
    if spread.packages:
        for package in spread.packages:
            lines.append(
                f"- {package}: "
                f"{_blocks_phrase(spread.blocks_by_package.get(package, []))}"
            )
    else:
        lines.append("- (no block names a path, so no package could be counted)")

    lines.append("")
    lines.append(
        "This is buildme's parse, not a finding. An `oversized-plan` finding "
        "still has to be yours, and it proposes a cut — it never performs one."
    )
    return "\n".join(lines)


def _split_context(config, change_dir: Path) -> str:
    """Context block describing the plan's independently-shippable groups.

    Parses `tasks.md` with the engine's own parser
    (:func:`build_pipeline.tdd_engine.parse_blocks` — the same one the build
    runs on, so the reviewer sees the blocks the builder will see, not a second
    parser's opinion of them), runs the three pure checks in
    :mod:`build_pipeline.plan_split`, and renders the result into the prompt's
    `{split_context}` slot. That replaces the reviewer's own reading of every
    `Interfaces:` section with buildme's parsed graph — a better input to the
    same check, not a new one.

    Thresholds come from config via ``getattr`` with a default, so a stale
    config object cannot crash the phase.

    **Degrades, never raises.** A missing tasks.md, an unparseable one, or an
    unexpected failure returns ""; a plan whose blocks declare no `Interfaces:`
    signatures returns :data:`NO_GRAPH_NOTE`. In every one of those cases the
    phase still runs and the prompt's check 2 still tells the reviewer to do
    the partition itself.

    It proposes; a gate disposes. Nothing here creates a ticket, moves a card,
    or writes a byte to tasks.md.
    """
    try:
        from ..plan_split import assess_plan_split
        from ..tdd_engine import parse_blocks

        blocks = parse_blocks(change_dir / "tasks.md")
        if not blocks:
            log.info(
                "No blocks parsed from %s — plan review runs without the "
                "dependency graph", change_dir / "tasks.md",
            )
            return ""
        if not any(block.produces or block.consumes for block in blocks):
            log.info(
                "%d block(s) in %s declare no Interfaces: signatures — no "
                "dependency graph to hand the reviewer",
                len(blocks), change_dir.name,
            )
            return NO_GRAPH_NOTE

        assessment = assess_plan_split(
            blocks,
            block_trigger=getattr(config, "plan_split_block_trigger", 8),
            package_trigger=getattr(config, "plan_split_package_trigger", 3),
        )
        return render_split_context(assessment)
    except Exception as split_err:  # a review without the graph beats no review
        log.warning(
            "Could not build the block dependency graph for %s (non-fatal): %s",
            change_dir, split_err,
        )
        return ""


async def run_plan_review(
    change_dir: Path,
    config,
    *,
    split_context: str = "",
) -> PlanReviewResult:
    """Review tasks.md against the rubric, the constraints and the use cases.

    Runs as a read-only subagent on `plan_review_model` / `plan_review_effort`,
    both read with a fallback so this does not depend on which config fields
    have landed. Returns the findings; raises whatever `agent_call_structured`
    raises (the stage above treats that as non-fatal).
    """
    use_cases = _read(change_dir / "use-cases.md", NO_USE_CASES)

    prompt = PLAN_REVIEW_PROMPT.format(
        proposal_content=_read(change_dir / "proposal.md", "(no proposal)"),
        design_content=_read(change_dir / "design.md", "(no design)"),
        use_cases_content=use_cases,
        tasks_content=_read(change_dir / "tasks.md", "(no tasks)"),
        rubric_content=_read(change_dir / "rubric.md", "(no rubric)"),
        split_context=split_context,
        block_trigger=getattr(config, "plan_split_block_trigger", 8),
        package_trigger=getattr(config, "plan_split_package_trigger", 3),
    )

    model = getattr(config, "plan_review_model", None) or config.review_model
    effort = getattr(config, "plan_review_effort", None) or config.review_effort

    log.info("Running plan review on %s", change_dir.name)
    return await agent_call_structured(
        prompt,
        PlanReviewResult,
        allowed_tools=PLAN_REVIEW_TOOLS,
        model=model,
        effort=effort,
        max_turns=PLAN_REVIEW_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="plan_review",
    )


def plan_review_findings_text(findings: list[PlanReviewFinding]) -> str:
    """The findings block injected into the single tasks.md regeneration.

    Empty string when nothing actionable survives the filter — the caller reads
    that as "report only, nothing to regenerate". `oversized-plan` never
    survives it: that finding is a proposal for a human, not an instruction for
    a rewriter (see PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES).
    """
    actionable = [
        f for f in findings or []
        if f.severity in PLAN_REVIEW_ACTIONABLE
        and f.category not in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES
    ]
    if not actionable:
        return ""
    return "\n".join(
        f"- [{f.severity}] ({f.category}) {f.file_path}"
        + (f" @ {f.location}" if f.location else "")
        + f": {f.claim}"
        + (f" Reason: {f.reason}" if f.reason else "")
        for f in actionable
    )


def log_plan_review_findings(findings: list[PlanReviewFinding]) -> None:
    """Record what the review found, at the severity it found it."""
    if not findings:
        log.info("DONE phase=PLAN_REVIEW findings=0")
        return
    critical = [f for f in findings if f.severity == "critical"]
    if critical:
        log.warning(
            "DONE phase=PLAN_REVIEW findings=%d critical=%d",
            len(findings), len(critical),
        )
        for finding in critical:
            log.warning(
                "  finding category=%s at=%s claim=%s",
                finding.category, finding.location or finding.file_path,
                finding.claim,
            )
        print(f"PHASE: plan_review_warnings — {len(critical)} critical findings")
    else:
        log.info("DONE phase=PLAN_REVIEW findings=%d critical=0", len(findings))

    proposed = [f for f in findings if f.category == "oversized-plan"]
    for finding in proposed:
        # Said out loud because it is an offer, and an offer nobody reads is a
        # split that never happens. Still only an offer: nothing acts on it.
        log.info(
            "PLAN_REVIEW proposes a split into %d ticket(s) — proposal only, "
            "nothing was split", len(finding.proposed_cut),
        )
        print(
            "PHASE: plan_review_split_proposed — "
            f"{len(finding.proposed_cut)} ticket(s) proposed (not applied)"
        )


async def run_plan_review_stage(
    change_dir: Path,
    config,
    state,
    state_path: Path,
) -> list[PlanReviewFinding]:
    """Run the plan review and fold its findings back into tasks.md.

    Same shape as ``spec_generator.run_design_audit_stage``, deliberately:
    critical/major findings drive **one** regeneration pass of tasks.md, the
    pass is committed, and the review is then re-run **report-only** so the
    build log records whether the critique landed. There is no loop — a plan
    that still has findings after one pass stands.

    Never raises: a review or regeneration failure is logged and the existing
    plan stands. Returns the first-pass findings.
    """
    from ..state import SpecGenState

    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    if state.spec_gen.plan_review_done:
        # Checked BEFORE the model call, not after it: the review reads five
        # artifacts, so re-running it on a resume to discover there is nothing
        # left to do is a whole agent call spent on a log line.
        log.info("SKIP phase=PLAN_REVIEW reason=recorded-complete-in-state")
        print("PHASE: plan_review_skipped — already run for this build")
        return []

    try:
        result = await run_plan_review(
            change_dir, config, split_context=_split_context(config, change_dir),
        )
    except Exception as review_err:
        # Deliberately NOT recorded as done: an LLM failure is a reason to try
        # again on a resume, unlike a completed review.
        log.warning("Plan review LLM call failed (non-fatal): %s", review_err)
        print(f"PHASE: plan_review_skipped — {review_err}")
        return []

    findings = list(result.findings)
    log_plan_review_findings(findings)

    findings_text = plan_review_findings_text(findings)
    if not findings_text:
        _mark_plan_review_done(state, state_path)
        return findings

    if state.spec_gen.plan_review_regen_done:
        # A resume after the regeneration checkpoint: the plan already absorbed
        # this critique once. Report only.
        log.info("SKIP phase=PLAN_REVIEW_FEEDBACK reason=recorded-complete-in-state")
        print("PHASE: plan_review_feedback_skipped — regeneration already applied")
        _mark_plan_review_done(state, state_path)
        return findings

    regenerated = await _apply_plan_review_findings(
        change_dir, config, state, findings_text,
    )
    # Recorded whether or not anything was rewritten: the pass has been spent,
    # and a failed regeneration is not a reason to try again later.
    state.spec_gen.plan_review_regen_done = True
    state.checkpoint(state_path)

    if regenerated:
        await _rereview_after_plan_feedback(change_dir, config)
    _mark_plan_review_done(state, state_path)
    return findings


def _mark_plan_review_done(state, state_path: Path) -> None:
    """Record that the review stage ran to completion, and checkpoint it.

    Separate from ``plan_review_regen_done``: a review that found nothing
    actionable never regenerates, so the regen flag alone cannot tell a resume
    "there is nothing here for you" — it would re-review to rediscover that.
    """
    from ..state import SpecGenState

    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    state.spec_gen.plan_review_done = True
    state.checkpoint(state_path)


async def _apply_plan_review_findings(
    change_dir: Path, config, state, findings_text: str,
) -> int:
    """Regenerate tasks.md ONCE from the plan review's findings.

    Returns the number of artifacts rewritten (0 or 1). Only tasks.md: the plan
    is what was reviewed, and rewriting design.md from a plan review would
    relitigate a document the design audit already settled. A regeneration
    failure is non-fatal — the reviewed plan stands.
    """
    from ..change_manager import ChangeManager
    from ..spec_generator import read_codebase_analysis, read_unknowns
    from .spec_steps import generate_artifact

    cm = ChangeManager(config.specs_dir)
    context = cm.artifact_context(change_dir, "tasks")
    context["unknowns_content"] = read_unknowns(change_dir)
    output_path = change_dir / context["output_path"]
    if not output_path.exists():
        log.warning(
            "SKIP plan-review feedback — %s does not exist", output_path,
        )
        return 0

    try:
        content = await generate_artifact(
            "tasks",
            context,
            config,
            codebase_analysis=read_codebase_analysis(state),
            reference_docs=config.reference_docs_text(),
            audit_findings=findings_text,
        )
    except Exception as regen_err:  # non-fatal: the reviewed plan stands
        log.warning(
            "Plan-review regeneration of tasks.md failed (non-fatal): %s",
            regen_err,
        )
        print("PHASE: plan_review_feedback_applied — 0 artifact(s)")
        return 0

    output_path.write_text(content)
    log.info("Rewrote %s from plan review findings", output_path)
    _commit_plan(config, change_dir)
    print("PHASE: plan_review_feedback_applied — 1 artifact(s)")
    return 1


def _commit_plan(config, change_dir: Path) -> None:
    """Commit the regenerated plan, so the branch history shows the review
    actually changed something.

    No-op unless the pipeline owns a branch, and no-op when the change
    directory is not under the project — the commit is explicit-path.
    """
    from .. import git_ops

    try:
        rel = str(change_dir.relative_to(config.project_dir))
    except (ValueError, TypeError):
        return
    if rel:
        git_ops.commit_stage(
            config, "docs(specs): regenerate tasks.md after plan review", [rel],
        )


async def _rereview_after_plan_feedback(change_dir: Path, config) -> None:
    """Re-run the review once, REPORT-ONLY — did the critique land?

    No second regeneration follows this, whatever it says; its only job is to
    put the answer in the build log.
    """
    try:
        result = await run_plan_review(
            change_dir, config, split_context=_split_context(config, change_dir),
        )
    except Exception as review_err:
        log.warning("Plan review re-check failed (non-fatal): %s", review_err)
        print(f"PHASE: plan_review_recheck_skipped — {review_err}")
        return

    still_open = [
        f for f in result.findings
        if f.severity in PLAN_REVIEW_ACTIONABLE
        and f.category not in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES
    ]
    if still_open:
        log.warning(
            "Plan review re-check: %d finding(s) still open after one pass",
            len(still_open),
        )
        print(f"PHASE: plan_review_recheck — {len(still_open)} still open")
    else:
        log.info("Plan review re-check: clean after one pass")
        print("PHASE: plan_review_recheck — clean")
