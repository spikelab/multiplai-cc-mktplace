"""buildme's one planning check: a read-only review of design.md and tasks.md.

It replaced the design audit, the tasks audit and the plan review, which read
the same documents three times. The planning owner publishes at most one
repair for this stage. A required recheck blocks critical findings not
`resolved` and major ones `still_present`. Passed evidence belongs to the
reviewed input revision, so an unchanged resume needs no model call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from multiplai_core.specs import change_root

from build_pipeline.budget import BudgetExceededError

from ..change_manager import requirement_sources, spec_issue_ref
from ..models import PlanEdits, PlanReviewFinding, PlanReviewResult
from ..prompts.plan_review import (
    MAX_PLAN_FINDINGS,
    NO_USE_CASES,
    PLAN_CHECK_PROMPT,
    changed_lines_section,
    plan_section,
    prior_findings_section,
)
from ..prompts.spec_generation import PLAN_EDIT_PROMPT
from ..sdk import agent_call_structured

log = logging.getLogger(__name__)

# Read-only, like every reviewer. `test_no_review_step_can_write` in
# test_review_steps.py asserts the shape of lists like this one.
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


_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}


def keep_most_severe(
    findings: list[PlanReviewFinding], label: str
) -> list[PlanReviewFinding]:
    """The findings sorted critical, major, minor (stable within a severity),
    cut to the first `MAX_PLAN_FINDINGS`. Logs how many it dropped."""
    ranked = sorted(findings, key=lambda f: _SEVERITY_RANK.get(f.severity, 3))
    if len(ranked) > MAX_PLAN_FINDINGS:
        log.info("%s: kept %d of %d findings", label, MAX_PLAN_FINDINGS, len(ranked))
    return ranked[:MAX_PLAN_FINDINGS]


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
            if assessment.should_split
            else "no cut is indicated. "
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
        lines.append(
            _signature_line(
                "Consumed but produced outside this plan",
                split.unresolved_consumes,
            )
        )

    lines.append("")
    lines.append(
        "### Atomicity — "
        + (
            "SPLIT: high-risk work ships beside unrelated feature work"
            if atomicity.should_split
            else "no unrelated feature work to isolate"
        )
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
            f"block {n} ({name})"
            for n, name in zip(
                atomicity.unrelated_block_numbers, atomicity.unrelated_block_names
            )
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
        lines.append(
            f"{group.index + 1}. {_blocks_phrase(group.block_numbers)} — "
            + "; ".join(group.block_names)
        )
        lines.append(_signature_line("produces", group.produces))
        lines.append(_signature_line("consumes", group.consumes))
    if split.boundaries:
        lines += ["", "## Cuts", ""]
        for boundary in split.boundaries:
            crossing = (
                ", ".join(boundary.crossing_signatures)
                if boundary.crossing_signatures
                else "nothing"
            )
            lines.append(
                f"- after PR {boundary.after_group + 1}: between block "
                f"{boundary.last_block_before} and block {boundary.first_block_after}; "
                f"crosses {crossing}"
            )
    lines += ["", "## Why", "", split.reason or "(no reason recorded)"]
    lines += ["", "## Atomicity", "", atomicity.reason or "(no high-risk work found)"]
    for high_risk in atomicity.high_risk_blocks:
        evidence = "; ".join(
            f"{kind}: "
            + ", ".join(f'"{kw}"' for kw in high_risk.matched_keywords.get(kind, []))
            for kind in high_risk.kinds
        )
        lines.append(
            f"- block {high_risk.number} ({high_risk.name}) — matched {evidence}"
        )
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
                "dependency graph",
                change_dir / "tasks.md",
            )
            return "", None
        if not any(block.produces or block.consumes for block in blocks):
            log.info(
                "%d block(s) in %s declare no Interfaces: signatures — no "
                "dependency graph to hand the reviewer",
                len(blocks),
                change_dir.name,
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
            change_dir,
            split_err,
        )
        return "", None


async def run_plan_review(
    change_dir: Path,
    config,
    *,
    split_context: str = "",
    prior: list[dict] | None = None,
    changed_lines: str = "",
) -> PlanReviewResult:
    """Check design.md and tasks.md against the specs, the use cases, the
    rubric, the constraints and the code (`PLAN_CHECK_PROMPT`).

    Runs as a read-only subagent on `config.model_for("plan_review")` and
    `config.effort_for("plan_review")`. Returns the findings; raises whatever `agent_call_structured`
    raises (the required stage records a blocked outcome). With `prior` (the
    findings a rewrite was asked to fix) it also asks for a status for each;
    with `changed_lines` (the rewrite's diff) it reports new findings only in
    or caused by those lines, and a new critical one anywhere.
    """
    # A component's proposal and use cases are its change's, so it is
    # reviewed against its plan.md, which says what it builds of them.
    root = change_root(change_dir)
    use_cases = _read(root / "use-cases.md", NO_USE_CASES)
    specs = "\n\n".join(
        f"### {label}\n{text}"
        for label, text in requirement_sources(change_dir).items()
    )
    from ..rubric import detect_change_type

    prompt = (
        PLAN_CHECK_PROMPT.format(
            proposal_content=_read(root / "proposal.md", "(no proposal)"),
            plan_section=plan_section(
                _read(change_dir / "plan.md", "(no plan.md)"),
                component=root != Path(change_dir),
            ),
            specs_content=specs or "(no specs)",
            design_content=_read(change_dir / "design.md", "(no design)"),
            use_cases_content=use_cases,
            tasks_content=_read(change_dir / "tasks.md", "(no tasks)"),
            rubric_content=_read(change_dir / "rubric.md", "(no rubric)"),
            split_context=split_context,
            change_type=detect_change_type(change_dir),
            spec_ref=spec_issue_ref(change_dir),
            block_trigger=getattr(config, "plan_split_block_trigger", 10),
            package_trigger=getattr(config, "plan_split_package_trigger", 5),
        )
        + prior_findings_section(prior)
        + changed_lines_section(changed_lines)
    )

    model = config.model_for("plan_review")
    effort = config.effort_for("plan_review")

    log.info("Running plan review on %s", change_dir.name)
    result = await agent_call_structured(
        prompt,
        PlanReviewResult,
        allowed_tools=PLAN_REVIEW_TOOLS,
        model=model,
        effort=effort,
        max_turns=PLAN_REVIEW_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="plan_review",
    )
    result.findings = keep_most_severe(list(result.findings), "Plan review")
    return result


async def run_plan_edit(change_dir: Path, config, findings: list[dict]) -> PlanEdits:
    """Ask for the exact replacements in design.md and tasks.md that fix these
    numbered findings (`PLAN_EDIT_PROMPT`). *change_dir* is the staged copy
    the edits will be applied to. Read-only tools, on the `spec` stage's model
    under `plan_rewrite`; `spec_generator.apply_plan_edits` applies them."""
    root = change_root(change_dir)
    specs = "\n\n".join(
        f"### {label}\n{text}"
        for label, text in requirement_sources(change_dir).items()
    )
    prompt = PLAN_EDIT_PROMPT.format(
        findings=findings_lines(findings),
        design_content=_read(change_dir / "design.md", "(no design)"),
        tasks_content=_read(change_dir / "tasks.md", "(no tasks)"),
        proposal_content=_read(root / "proposal.md", "(no proposal)"),
        plan_section=plan_section(
            _read(change_dir / "plan.md", "(no plan.md)"),
            component=root != Path(change_dir),
        ),
        use_cases_content=_read(root / "use-cases.md", NO_USE_CASES),
        specs_content=specs or "(no specs)",
    )
    log.info("Running plan edit on %s", change_dir.name)
    return await agent_call_structured(
        prompt,
        PlanEdits,
        allowed_tools=PLAN_REVIEW_TOOLS,
        model=config.model_for("plan_rewrite"),
        effort=config.effort_for("plan_rewrite"),
        max_turns=PLAN_REVIEW_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="plan_rewrite",
    )


def findings_lines(findings: list[dict]) -> str:
    """One line per numbered finding: its id, severity, category and text."""
    return "\n".join(
        f"- {f.get('finding_id', '?')} [{f.get('severity', '?')}] "
        f"({f.get('category', '?')}) {f.get('text', '')}"
        for f in findings
    )


def plan_review_actionable(
    findings: list[PlanReviewFinding],
) -> list[PlanReviewFinding]:
    """The findings that earn the rewrite: critical or major, and never
    `oversized-plan`, a proposal for a human, not an instruction for a
    rewriter (see PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES)."""
    return [
        f
        for f in findings or []
        if f.severity in PLAN_REVIEW_ACTIONABLE
        and f.category not in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES
    ]


def _finding_line(f: PlanReviewFinding) -> str:
    return (
        f"{f.file_path}"
        + (f" @ {f.location}" if f.location else "")
        + f": {f.claim}"
        + (f" Reason: {f.reason}" if f.reason else "")
        + (f" Fix: {f.suggestion}" if f.suggestion else "")
    )


def finding_dicts(findings: list[PlanReviewFinding]) -> list[dict]:
    """Plan check findings in the shape the recheck, the checkpoint and
    `audit_spec_issues` use: `claim` is the `description`."""
    return [
        {
            "category": f.category,
            "severity": f.severity,
            "text": _finding_line(f),
            "description": f.claim,
            "suggestion": f.suggestion,
            "file_path": f.file_path,
            "needs_requirements_change": f.needs_requirements_change,
            "requirement": f.requirement,
        }
        for f in findings
    ]


def log_plan_review_findings(findings: list[PlanReviewFinding]) -> None:
    """Record what the review found, at the severity it found it."""
    if not findings:
        log.info("DONE phase=PLAN_REVIEW findings=0")
        return
    critical = [f for f in findings if f.severity == "critical"]
    if critical:
        log.warning(
            "DONE phase=PLAN_REVIEW findings=%d critical=%d",
            len(findings),
            len(critical),
        )
        for finding in critical:
            log.warning(
                "  finding category=%s at=%s claim=%s",
                finding.category,
                finding.location or finding.file_path,
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
            "nothing was split",
            len(finding.proposed_cut),
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
    """Run the plan check and fold its findings back into design.md and tasks.md.

    The check's model findings and the deterministic scans
    (`scan_placeholders`, `scan_no_behaviour_blocks`) are read together.
    Critical and major ones request one staged rewrite through the planning
    owner, then a required recheck of that revision, which is given the
    findings the rewrite was asked to fix and blocks on a critical one not
    `resolved`, a major one `still_present`, and new critical findings
    (`recheck_verdict`). A critical or major finding whose fix needs a
    requirements change raises `SpecIssuesFound`, at the first check and at
    the recheck. Unresolved findings and failed check calls record a blocked
    outcome and raise; they cannot authorize TDD.
    """
    from ..readiness import SpecIssuesFound
    from ..spec_generator import (
        PlanBlocked,
        PlanRepairRejected,
        assess_listed,
        audit_spec_issues,
        block_on_findings,
        from_scans,
        log_findings,
        model_listed,
        next_finding_number,
        number_findings,
        open_findings,
        plan_edits_diff,
        planning_spend,
        raise_spec_issues,
        recheck_verdict,
        reconcile_plan,
        record_advisory,
        record_blocked_review,
        refuse_if_blocked,
        reopen_if_edited,
        rewrite_plan,
        rewrite_published,
        saved_findings,
        scan_assessments,
    )

    name = "plan_review"
    reconcile_plan(config, state)
    sg = state.spec_gen
    assert sg is not None
    if sg.step_passed(name):
        return []
    reopen_if_edited(state, name, state_path)
    refuse_if_blocked(sg, name)
    admitted = sg.steps[name].revision if rewrite_published(sg, name) else None

    def stop(why: str, blocking: list[dict]) -> None:
        block_on_findings(config, state, state_path, name, why, blocking)
        raise PlanBlocked(why)

    try:
        if admitted is None:
            result = await run_plan_review(
                change_dir, config, split_context=_split_context(config, change_dir)
            )
            raise_spec_issues(audit_spec_issues(finding_dicts(result.findings)), name)
            scanned = _scan_findings(change_dir)
            findings = list(result.findings) + scanned
            log_plan_review_findings(findings)
            if not plan_review_actionable(findings):
                sg.record_step(name)
                state.checkpoint(state_path)
                return findings
            numbered = number_findings(
                name,
                [
                    *finding_dicts(plan_review_actionable(result.findings)),
                    *from_scans(finding_dicts(plan_review_actionable(scanned))),
                ],
            )
        while True:
            if admitted is None:
                attempts = sg.repair_attempts(name)
                if attempts >= config.planning_max_rewrites:
                    stop(
                        f"{attempts} of planning.max_rewrites "
                        f"{config.planning_max_rewrites} rewrites used; "
                        f"{len(numbered)} blocking findings left",
                        open_findings(numbered),
                    )
                spent = planning_spend()
                if spent >= config.planning_max_usd:
                    stop(
                        f"planning spend ${spent:.2f} reached planning.max_usd "
                        f"${config.planning_max_usd:.2f}",
                        open_findings(numbered),
                    )
                sg.admit_repair(
                    name,
                    saved_findings(numbered),
                    max_rewrites=config.planning_max_rewrites,
                    start_count=len(numbered),
                )
                state.checkpoint(state_path)
                admitted = sg.steps[name].revision
                await rewrite_plan(
                    config,
                    state,
                    numbered,
                    attempt=sg.repair_attempts(name),
                    step=name,
                )
            attempt = sg.repair_attempts(name)
            listed = sg.steps[name].repair_findings
            result = await run_plan_review(
                change_dir,
                config,
                split_context=_split_context(config, change_dir),
                prior=model_listed(listed) or None,
                changed_lines=plan_edits_diff(change_dir, attempt),
            )
            raise_spec_issues(audit_spec_issues(finding_dicts(result.findings)), name)
            scanned_findings = _scan_findings(change_dir)
            scanned = finding_dicts(scanned_findings)
            findings = list(result.findings) + scanned_findings
            log_plan_review_findings(findings)
            new = [
                f
                for f in finding_dicts(result.findings)
                if f["category"] not in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES
            ]
            given = list(result.prior_assessments) + scan_assessments(listed, scanned)
            blocking, advisory = recheck_verdict(
                listed, given, new, deterministic=scanned
            )
            sg.steps[name].repair_findings = saved_findings(
                assess_listed(listed, given)
            )
            log_findings(name, "listed", sg.steps[name].repair_findings)
            record_advisory(config, name, advisory)
            if not blocking:
                sg.record_step(name)
                state.checkpoint(state_path)
                return findings
            # A listed finding of the scans is counted by the scans' own
            # findings, which every recheck runs again.
            left = [f for f in blocking if f.get("source") != "scan"]
            started = sg.steps[name].blocking_counts[-1:] or [len(listed)]
            if len(left) >= started[0]:
                stop(
                    f"rewrite {attempt} left {len(left)} blocking findings "
                    f"(was {started[0]})",
                    blocking,
                )
            carried = [
                {k: v for k, v in f.items() if k not in ("status", "evidence")}
                for f in left
                if f.get("finding_id")
            ]
            numbered = carried + number_findings(
                name,
                [
                    *[f for f in new if f.get("severity") == "critical"],
                    *from_scans(scanned),
                ],
                start=next_finding_number(listed),
            )
            admitted = None
    except PlanBlocked:
        raise
    except SpecIssuesFound as found:
        # Blocked at this revision: after `pm-spec revise` changes the
        # requirements, the step gets its one rewrite back (`reopen_if_edited`).
        record_blocked_review(
            config, state, state_path, name, str(found), "", final=True
        )
        raise
    except PlanRepairRejected as rejection:
        record_advisory(config, name, rejection.advisory)
        block_on_findings(
            config, state, state_path, name, str(rejection), rejection.blocking
        )
        raise
    except BaseException as error:
        record_blocked_review(
            config,
            state,
            state_path,
            name,
            str(error) or type(error).__name__,
            "",
            revision=admitted,
        )
        raise


def _scan_findings(change_dir: Path) -> list[PlanReviewFinding]:
    """The deterministic scans of tasks.md, as plan check findings: the
    placeholder scan and the no-behaviour block scan.

    They always run, so a placeholder or a block the model misses still
    reaches the one rewrite, and they block the recheck whatever the model
    says: they are code facts, not model judgments.
    """
    from ..spec_generator import scan_no_behaviour_blocks, scan_placeholders

    tasks = change_dir / "tasks.md"
    placeholders = [
        PlanReviewFinding(
            category=f["category"],
            severity=f["severity"],
            file_path="tasks.md",
            location=f["description"].split(":", 1)[0],
            claim=f["description"],
            suggestion=f["suggestion"],
        )
        for f in scan_placeholders(tasks.read_text() if tasks.exists() else "")
    ]
    return placeholders + [
        PlanReviewFinding(
            category=f["category"],
            severity=f["severity"],
            file_path="tasks.md",
            location=f"Block {f['block']} — {f['name']}",
            claim=f["description"],
            suggestion=f["suggestion"],
        )
        for f in scan_no_behaviour_blocks(tasks)
    ]
