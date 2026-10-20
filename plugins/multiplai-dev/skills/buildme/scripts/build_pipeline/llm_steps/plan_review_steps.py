"""Read-only review of the final plan, after prototype and design repair.

The planning owner publishes at most one repair for this stage. A required
recheck blocks unresolved major findings. Passed evidence belongs to the
reviewed input revision, so an unchanged resume needs no model call.
"""

from __future__ import annotations

from build_pipeline.budget import BudgetExceededError

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
    and 2 worth running, never a verdict. The block trigger's default of 10 is
    an unmeasured placeholder and the rendered text says so, so no reviewer
    treats a count as a finding.

    ``PlanSplitAssessment.should_split`` — the module's own composite answer —
    is rendered too, labelled as arithmetic rather than a finding. Computing it
    and then leaving it out had the reviewer re-derive from prose the one thing
    this seam exists to hand it.

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
        "buildme's own arithmetic over the three checks: "
        + (
            "THIS PLAN COMES APART. "
            if assessment.should_split else
            "no cut is indicated. "
        )
        + "That is arithmetic, not a finding — an `oversized-plan` finding is "
        "still yours to make or to withhold, and the parse below is the "
        "evidence you weigh.",
        "",
        f"### Separability — {split.group_count} independently-shippable group(s)"
        + ("" if split.splittable else " (the work is atomic)"),
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
            f"- Cut after group {boundary.after_group + 1}"
            + (" (clean)" if boundary.is_clean else "")
            + f": between block {boundary.last_block_before} and block "
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


def render_split_proposal(assessment, change_name: str) -> str:
    """``split-proposal.md`` for the size gate: the cut, written for a ticket
    comment or for the user. Block groups (the proposed PRs, in ship order),
    the cut boundaries, ``split.reason`` and the atomicity evidence.

    Pure: formats the assessment, reads nothing, changes nothing.
    """
    split = assessment.split
    atomicity = assessment.atomicity
    lines = [
        f"# Split proposal: {change_name}",
        "",
        "buildme stopped after plan review: this plan is larger than one PR "
        "should be and its blocks come apart cleanly. Proposed split below. "
        "To build it as one PR anyway, re-run the same command with "
        "`--accept-size`.",
        "",
        f"Blocks: {assessment.block_count} (size trigger >{assessment.block_trigger}: "
        f"{'fired' if assessment.size_triggered else 'not fired'}). "
        f"Top-level packages: {assessment.spread.count} (package trigger "
        f">{assessment.package_trigger}: "
        f"{'fired' if assessment.package_triggered else 'not fired'}). "
        "The triggers are unmeasured placeholders, not findings.",
        "",
        f"## Proposed PRs — {split.group_count} group(s), in ship order",
        "",
    ]
    for group in split.groups:
        lines.append(f"{group.index + 1}. {_blocks_phrase(group.block_numbers)} — "
                     + "; ".join(group.block_names))
        lines.append(_signature_line("produces", group.produces))
        lines.append(_signature_line("consumes", group.consumes))
    if split.boundaries:
        lines += ["", "## Cuts", ""]
        for boundary in split.boundaries:
            crossing = (", ".join(boundary.crossing_signatures)
                        if boundary.crossing_signatures else "nothing")
            lines.append(
                f"- after PR {boundary.after_group + 1}: between block "
                f"{boundary.last_block_before} and block {boundary.first_block_after}; "
                f"crosses {crossing}")
    lines += ["", "## Why", "", split.reason or "(no reason recorded)"]
    lines += ["", "## Atomicity", "", atomicity.reason or "(no high-risk work found)"]
    for high_risk in atomicity.high_risk_blocks:
        evidence = "; ".join(
            f"{kind}: " + ", ".join(f'"{kw}"' for kw in high_risk.matched_keywords.get(kind, []))
            for kind in high_risk.kinds
        )
        lines.append(f"- block {high_risk.number} ({high_risk.name}) — matched {evidence}")
    return "\n".join(lines).rstrip() + "\n"


def _split_context(config, change_dir: Path) -> str:
    """The prompt's `{split_context}` text; see :func:`split_assessment`."""
    return split_assessment(config, change_dir)[0]


def split_assessment(config, change_dir: Path):
    """``(split_context, assessment)`` for tasks.md — one parse, two consumers.

    The text feeds the plan-review prompt; the :class:`PlanSplitAssessment`
    feeds the size gate after plan review (``orchestrator._run_size_gate``),
    so the gate and the reviewer judge the same parsed graph. ``assessment`` is
    None whenever no graph was built (no tasks.md, no blocks, no `Interfaces:`
    signatures, or an unexpected failure): without signatures every block
    looks independent, and stopping a build on that would be a false alarm.

    The context block describes the plan's independently-shippable groups.
    It parses `tasks.md` with the engine's own parser
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
            return "", None
        if not any(block.produces or block.consumes for block in blocks):
            log.info(
                "%d block(s) in %s declare no Interfaces: signatures — no "
                "dependency graph to hand the reviewer",
                len(blocks), change_dir.name,
            )
            return NO_GRAPH_NOTE, None

        assessment = assess_plan_split(
            blocks,
            block_trigger=getattr(config, "plan_split_block_trigger", 10),
            package_trigger=getattr(config, "plan_split_package_trigger", 5),
        )
        return render_split_context(assessment), assessment
    except BudgetExceededError:
        raise
    except Exception as split_err:  # a review without the graph beats no review
        log.warning(
            "Could not build the block dependency graph for %s (non-fatal): %s",
            change_dir, split_err,
        )
        return "", None


async def run_plan_review(
    change_dir: Path,
    config,
    *,
    split_context: str = "",
) -> PlanReviewResult:
    """Review tasks.md against the rubric, the constraints and the use cases.

    Runs as a read-only subagent on `config.model_for("plan_review")` and
    `config.effort_for("plan_review")`. Returns the findings; raises whatever `agent_call_structured`
    raises (the required stage records a blocked outcome).
    """
    use_cases = _read(change_dir / "use-cases.md", NO_USE_CASES)

    prompt = PLAN_REVIEW_PROMPT.format(
        proposal_content=_read(change_dir / "proposal.md", "(no proposal)"),
        design_content=_read(change_dir / "design.md", "(no design)"),
        use_cases_content=use_cases,
        tasks_content=_read(change_dir / "tasks.md", "(no tasks)"),
        rubric_content=_read(change_dir / "rubric.md", "(no rubric)"),
        split_context=split_context,
        block_trigger=getattr(config, "plan_split_block_trigger", 10),
        package_trigger=getattr(config, "plan_split_package_trigger", 5),
    )

    model = config.model_for("plan_review")
    effort = config.effort_for("plan_review")

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

    Blocking findings request one staged rewrite through the planning owner,
    then a required recheck of that revision. Unresolved findings and failed
    review calls record a blocked outcome and raise; they cannot authorize TDD.
    """
    from ..spec_generator import reconcile_plan, rewrite_plan, record_blocked_review
    reconcile_plan(config, state)
    sg = state.spec_gen
    assert sg is not None
    if sg.step_passed("plan_review"):
        return []
    previous = sg.steps.get("plan_review")
    if previous and previous.revision == sg.step_revision("plan_review") and previous.blocking_findings:
        raise RuntimeError(previous.reason)
    text = ""
    findings_revision = sg.step_revision("plan_review")
    try:
        result = await run_plan_review(change_dir, config, split_context=_split_context(config, change_dir))
        findings = list(result.findings) + _no_behaviour_findings(change_dir)
        text = plan_review_findings_text(findings)
        if text and not sg.repair_attempts("plan_review"):
            sg.admit_repair("plan_review")
            state.checkpoint(state_path)
            await rewrite_plan(config, state, text)
            text = ""  # The old findings do not describe the published revision.
            findings_revision = sg.step_revision("plan_review")
            result = await run_plan_review(change_dir, config, split_context=_split_context(config, change_dir))
            findings = list(result.findings) + _no_behaviour_findings(change_dir)
            text = plan_review_findings_text(findings)
        log_plan_review_findings(findings)
        if text:
            raise RuntimeError("Plan review still has blocking findings after bounded repair")
        sg.record_step("plan_review")
        state.checkpoint(state_path)
        return findings
    except BaseException as error:
        record_blocked_review(config, state, state_path, "plan_review",
                              str(error) or type(error).__name__,
                              text if findings_revision == sg.step_revision("plan_review") else "")
        raise


def _no_behaviour_findings(change_dir: Path) -> list[PlanReviewFinding]:
    """The deterministic no-behaviour block scan, as plan review findings.

    The design audit's rewrite of tasks.md comes after the shape audit, so the
    scan runs here too: a block the reviewer model misses still reaches the
    one regeneration, and a review whose model call fails still rewrites the
    scan's blocks, once per build (``plan_review_scan_regen_done``), so a
    resume reviews again. The prototype feedback pass, after this review,
    runs the same scan on what it writes.
    """
    from ..spec_generator import scan_no_behaviour_blocks

    return [
        PlanReviewFinding(
            category=f["category"],
            severity=f["severity"],
            file_path="tasks.md",
            location=f"Block {f['block']} — {f['name']}",
            claim=f["description"],
            reason=f["suggestion"],
        )
        for f in scan_no_behaviour_blocks(change_dir / "tasks.md")
    ]
