"""Spec generator pipeline — creates all OpenSpec artifacts in dependency order.

Entry point: run_spec_generator(config, args)

Flow:
1. Bootstrap change directory
2. Generate artifacts in dependency order (proposal -> requirements+design -> tasks -> rubric)
3. Run design audit
4. Return exit code
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

from .change_manager import ChangeManager, ARTIFACT_DAG
from .dependencies import NewDependency, design_decisions_text, detect_new_dependencies
from .gates import unknowns_gate
from .models import ArtifactStatus, BuildPhase
from .rubric import detect_change_type, generate_rubric
from .state import BuildState, SpecGenState

log = logging.getLogger(__name__)


async def run_spec_generator(config, args=None) -> int:
    """Main entry point for spec generation pipeline.

    Args:
        config: BuildConfig with project settings
        args: Optional CLI args namespace

    Returns:
        Exit code (0=success, 1=failure)
    """
    print(f"PHASE: spec_generation — {config.change_name}")

    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    change_dir = cm.create_change(config.change_name)

    # Load or create state
    state = _load_or_create_state(config)

    # Ensure spec_gen sub-state exists
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
        state.checkpoint(config.state_file_path())

    try:
        # Generate artifacts in dependency order
        log.info("START phase=ARTIFACT_GENERATION change=%s", config.change_name)
        await _generate_all_artifacts(cm, change_dir, config, state)
        log.info("DONE phase=ARTIFACT_GENERATION")

        # Run design audit and fold its findings back into the specs
        # (best-effort — failures don't block the build)
        log.info("START phase=DESIGN_AUDIT")
        print("PHASE: design_audit")
        state.advance_to(BuildPhase.DESIGN_AUDIT, config.state_file_path())
        await run_design_audit_stage(
            change_dir, config, state, config.state_file_path(),
        )

        print("PHASE: spec_generation_complete")
        return 0

    except Exception as e:
        log.error("FAIL phase=SPEC_GENERATION error=%s", e, exc_info=True)
        print(f"PHASE: spec_generation_failed — {e}")
        return 1


async def _generate_all_artifacts(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate all artifacts in dependency order, with resume support."""
    from .llm_steps.spec_steps import generate_artifact

    max_iterations = len(ARTIFACT_DAG) * 2  # safety limit
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        ready = cm.ready_artifacts(change_dir)

        if not ready:
            # Check if all done
            status = cm.artifact_status(change_dir)
            if all(s == ArtifactStatus.DONE for s in status.values()):
                log.info("All artifacts generated")
                break
            # Deadlock — some artifacts can't be created
            blocked = [a for a, s in status.items() if s == ArtifactStatus.BLOCKED]
            log.error("Deadlock: blocked artifacts %s with nothing ready", blocked)
            raise RuntimeError(f"Artifact generation deadlocked: {blocked}")

        for artifact_id in ready:
            # Skip if already completed in this or previous run
            if state.spec_gen and artifact_id in state.spec_gen.completed_artifacts:
                log.info("SKIP artifact=%s reason=already-completed", artifact_id)
                continue

            log.info("START artifact=%s", artifact_id)
            await _generate_single_artifact(
                cm, change_dir, artifact_id, config, state,
            )
            log.info("DONE artifact=%s", artifact_id)

    # Unknowns-gate resume durability: same problem as the tasks audit below —
    # the gate runs after unknowns.md is written, so a crash mid-gate leaves
    # the artifact DONE and the DAG loop never re-enters it. The checkpoint,
    # not file existence, is the record that the gate ran.
    if state.spec_gen and not state.spec_gen.explainers_done:
        unknowns_path = change_dir / "unknowns.md"
        if not getattr(config, "explainers_active", True):
            # --skip-explainers on a resumed build: never run detection or the
            # gate's LLM regeneration — that is exactly what the flag exists
            # to prevent. Write the skip-marker document when the crash
            # predates it (mirroring _generate_unknowns) so the DAG stays
            # satisfiable.
            log.info("SKIP phase=UNKNOWNS_GATE reason=explainers-disabled")
            if not unknowns_path.exists():
                unknowns_path.write_text(
                    f"{UNKNOWNS_HEADER}\n{EXPLAINERS_SKIPPED_LINE}\n"
                )
            state.spec_gen.explainers_done = True
            state.checkpoint(config.state_file_path())
        elif unknowns_path.exists():
            log.info(
                "Unknowns gate not recorded complete — running it now "
                "(resume durability)"
            )
            deps = detect_new_dependencies(change_dir, config.project_dir)
            if deps:
                await _audit_unknowns(
                    change_dir,
                    cm.artifact_context(change_dir, "unknowns"),
                    config,
                    unknowns_path,
                    deps,
                )
            state.spec_gen.explainers_done = True
            state.checkpoint(config.state_file_path())
    elif state.spec_gen:
        log.info("SKIP phase=UNKNOWNS_GATE reason=recorded-complete-in-state")

    # Tasks-audit resume durability: the audit runs after tasks.md is
    # written, so a crash mid-audit leaves the artifact DONE (file exists)
    # and the DAG loop above never re-enters it. The checkpoint state — not
    # file existence — is the record of audit completion; re-run it here
    # when the artifact exists but the audit isn't recorded complete.
    if state.spec_gen and not state.spec_gen.tasks_audit_done:
        context = cm.artifact_context(change_dir, "tasks")
        tasks_path = change_dir / context["output_path"]
        if tasks_path.exists():
            log.info(
                "Tasks-shape audit not recorded complete — running it now "
                "(resume durability)"
            )
            await _audit_tasks_shape(change_dir, context, config, tasks_path)
            state.spec_gen.tasks_audit_done = True
            state.checkpoint(config.state_file_path())
    elif state.spec_gen:
        log.info("SKIP phase=TASKS_SHAPE_AUDIT reason=recorded-complete-in-state")

    # Verify completeness
    final_status = cm.artifact_status(change_dir)
    done_count = sum(1 for s in final_status.values() if s == ArtifactStatus.DONE)
    log.info("Artifact generation complete: %d/%d done", done_count, len(ARTIFACT_DAG))


async def _generate_single_artifact(
    cm: ChangeManager,
    change_dir: Path,
    artifact_id: str,
    config,
    state: BuildState,
) -> None:
    """Generate a single artifact, handling requirements specially (one per capability)."""
    context = cm.artifact_context(change_dir, artifact_id)

    # Thread the explainers into the task breakdown: the edge cases are only
    # worth writing down if the blocks that touch them name them as acceptance
    # criteria (which is what turns them into tests).
    if artifact_id == "tasks":
        context["unknowns_content"] = read_unknowns(change_dir)

    if artifact_id == "requirements":
        await _generate_requirements(cm, change_dir, config, state)
    elif artifact_id == "unknowns":
        await _generate_unknowns(cm, change_dir, config, state)
    elif artifact_id == "rubric":
        await _generate_rubric(change_dir, config, state)
    else:
        from .llm_steps.spec_steps import generate_artifact

        content = await generate_artifact(
            artifact_id,
            context,
            config,
            interview_summary=state.interview_summary or "",
            research=state.research_path or "",
            codebase_analysis=(
                state.spec_gen.codebase_analysis_path
                if state.spec_gen else ""
            ) or "",
        )

        # Write the artifact
        output_path = change_dir / context["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        log.info("Wrote artifact: %s", output_path)

        # Tasks-shape audit: catch horizontal decomposition before the rubric
        # and implementation build on a layered breakdown. Prompt instructions
        # alone drift — this audit is the enforcement. Completion is recorded
        # in checkpoint state (not file existence): tasks.md already exists at
        # this point, so a crash mid-audit would otherwise mark the artifact
        # DONE and silently skip the audit on resume (see
        # _generate_all_artifacts' durability pass).
        if artifact_id == "tasks":
            await _audit_tasks_shape(change_dir, context, config, output_path)
            if state.spec_gen:
                state.spec_gen.tasks_audit_done = True

    # Mark completed
    if state.spec_gen:
        state.spec_gen.completed_artifacts.append(artifact_id)
        state.checkpoint(config.state_file_path())

    print(f"PHASE: artifact_{artifact_id}_complete")


async def _generate_requirements(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate requirement files — one per capability from the proposal."""
    from .llm_steps.spec_steps import generate_artifact

    proposal_path = change_dir / "proposal.md"
    if not proposal_path.exists():
        raise RuntimeError("Cannot generate requirements: proposal.md missing")

    # Extract capability names from proposal
    capabilities = _extract_capabilities(proposal_path.read_text())
    if not capabilities:
        # Fallback: generate a single requirement file
        capabilities = [config.change_name or "main"]
        log.warning("No capabilities found in proposal, using fallback: %s", capabilities)

    req_dir = change_dir / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)

    for cap_name in capabilities:
        req_file = req_dir / f"{cap_name}.md"
        if req_file.exists():
            log.info("Requirements for %s already exist, skipping", cap_name)
            continue

        context = cm.artifact_context(change_dir, "requirements")
        context["capability_name"] = cap_name

        content = await generate_artifact("requirements", context, config)

        req_file.write_text(content)
        log.info("Wrote requirements: %s", req_file)


UNKNOWNS_HEADER = "# Unknowns — what we are about to depend on\n"
NO_NEW_DEPENDENCIES_LINE = "No dependencies new to this project."
EXPLAINERS_SKIPPED_LINE = (
    "Explainers skipped (--skip-explainers / explainers.enabled: false)."
)


def read_unknowns(change_dir: Path) -> str:
    """unknowns.md's text, or a placeholder when it hasn't been written."""
    path = change_dir / "unknowns.md"
    if path.exists():
        return path.read_text()
    return "(no unknowns document)"


def _dependency_list_text(deps: list[NewDependency]) -> str:
    return "\n".join(
        f"- `{d.name}` — named in {', '.join(d.mentioned_in) or 'the specs'}; {d.evidence}"
        for d in deps
    ) or "(none)"


async def _generate_unknowns(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Write unknowns.md — one explainer per dependency new to this project.

    B1: before the build depends on a new tool/library/service, write down its
    contract and its edge cases (the Whisper-silence class of surprise). One
    concurrent LLM call per detected dependency, then the structural gate with
    its single regeneration pass.

    Both "nothing new" and "explainers switched off" still write the file: the
    artifact DAG stays satisfied and the absence is a recorded finding rather
    than a silent skip.
    """
    from .llm_steps.spec_steps import run_explainer

    output_path = change_dir / "unknowns.md"

    if not getattr(config, "explainers_active", True):
        log.info("SKIP phase=EXPLAINERS reason=disabled")
        print("PHASE: explainers_skipped")
        output_path.write_text(f"{UNKNOWNS_HEADER}\n{EXPLAINERS_SKIPPED_LINE}\n")
        if state.spec_gen:
            state.spec_gen.explainers_done = True
        return

    log.info("START phase=EXPLAINERS")
    print("PHASE: explainers")
    deps = detect_new_dependencies(change_dir, config.project_dir)

    if not deps:
        log.info("DONE phase=EXPLAINERS dependencies=0")
        output_path.write_text(
            f"{UNKNOWNS_HEADER}\n{NO_NEW_DEPENDENCIES_LINE}\n\n"
            "The proposal's Impact section and the design's Decisions section "
            "name nothing that is absent from this project's manifests.\n"
        )
        print("PHASE: explainers_complete — 0 new dependencies")
        if state.spec_gen:
            state.spec_gen.explainers_done = True
        return

    log.info(
        "Explaining %d new dependencies: %s",
        len(deps), ", ".join(d.name for d in deps),
    )
    usage_context = design_decisions_text(change_dir)
    results = await asyncio.gather(
        *[run_explainer(dep, config, usage_context=usage_context) for dep in deps],
        return_exceptions=True,
    )

    sections: list[str] = []
    for dep, result in zip(deps, results):
        if isinstance(result, BaseException):
            # A failed explainer is a recorded hole, not a silent one — the gate
            # sees the empty section and drives the regeneration pass.
            log.warning("Explainer failed for %s: %s", dep.name, result)
            sections.append(
                f"## {dep.name}\n\n"
                f"### What it is\n(explainer call failed: {result})\n\n"
                f"### The contract we rely on\n(not written)\n\n"
                f"### Edge cases & failure modes\n\n"
                f"### Assumptions we are making\n\n"
                f"### How we would find out cheaply\n(not written)\n"
            )
        else:
            sections.append(str(result).strip())

    output_path.write_text(
        UNKNOWNS_HEADER + "\n" + "\n\n".join(sections).strip() + "\n"
    )
    log.info("Wrote artifact: %s", output_path)

    context = cm.artifact_context(change_dir, "unknowns")
    await _audit_unknowns(change_dir, context, config, output_path, deps)
    if state.spec_gen:
        state.spec_gen.explainers_done = True


async def _audit_unknowns(
    change_dir: Path,
    context: dict,
    config,
    output_path: Path,
    deps: list[NewDependency],
) -> None:
    """Gate unknowns.md; regenerate ONCE when a required list came back empty.

    Same single-pass shape as ``_audit_tasks_shape``: the gate's findings are
    injected into exactly one regeneration call. A regenerated document that
    still fails is logged and stands — there is no re-audit loop, so a
    stubbornly incomplete explainer costs one extra call, not an unbounded
    number.
    """
    from .llm_steps.spec_steps import generate_artifact

    log.info("START phase=UNKNOWNS_GATE")
    print("PHASE: unknowns_gate")
    result = unknowns_gate(output_path.read_text(), deps)
    if result.passed:
        log.info("DONE phase=UNKNOWNS_GATE findings=0")
        return

    findings = result.metadata.get("findings", [])
    log.warning(
        "DONE phase=UNKNOWNS_GATE findings=%d — one regeneration pass",
        len(findings),
    )
    for finding in findings:
        log.warning("  finding %s", finding)

    context = dict(context)
    context["current_unknowns"] = output_path.read_text()
    context["dependency_list"] = _dependency_list_text(deps)
    findings_text = "\n".join(f"- {f}" for f in findings) or result.reason

    try:
        content = await generate_artifact(
            "unknowns", context, config, audit_findings=findings_text,
        )
    except Exception as regen_err:
        log.warning(
            "Unknowns regeneration failed (non-fatal, first pass stands): %s",
            regen_err,
        )
        print(f"PHASE: unknowns_regeneration_failed — {regen_err}")
        return

    output_path.write_text(content)
    log.info("Rewrote artifact after unknowns gate: %s", output_path)
    print(f"PHASE: unknowns_regenerated — {len(findings)} findings")

    # Report-only re-check: no second regeneration, so the pass count is fixed
    # at one no matter how incomplete the document remains.
    recheck = unknowns_gate(content, deps)
    if not recheck.passed:
        log.warning(
            "Unknowns still incomplete after the single regeneration pass "
            "(accepted, no loop): %s",
            recheck.reason,
        )
        print("PHASE: unknowns_still_incomplete — accepted after one pass")


# Placeholder text that defers specification to the implementer. Deterministic
# counterpart to the audit prompt's Placeholders check — the regex always runs,
# even when the LLM audit errors out.
_PLACEHOLDER_RE = re.compile(
    r"\bTBD\b|\bTODO\b|add appropriate error handling|similar to block \d+",
    re.IGNORECASE,
)


def scan_placeholders(text: str) -> list[dict]:
    """Deterministic TBD/TODO scan over a generated artifact. Returns audit-
    finding dicts (same shape as the LLM audit) — non-empty triggers the
    single tasks regeneration pass."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _PLACEHOLDER_RE.search(line):
            findings.append({
                "category": "placeholder",
                "severity": "major",
                "description": f"Placeholder text on line {lineno}: {line.strip()[:120]}",
                "suggestion": "Replace with the concrete content — exact names, "
                              "signatures, literal values (repeat code verbatim "
                              "rather than referencing another block).",
            })
    return findings


async def _audit_tasks_shape(
    change_dir: Path,
    context: dict,
    config,
    output_path: Path,
) -> None:
    """Audit tasks.md for horizontal decomposition; regenerate ONCE on findings.

    Findings come from two sources: the deterministic placeholder scan (always
    runs) and the LLM shape/traceability audit. Any findings trigger exactly
    one regeneration pass with the findings injected into the tasks prompt
    (no re-audit loop). LLM audit failures are non-fatal — the deterministic
    findings still count; with none, the first-pass tasks.md stands.
    """
    from .llm_steps.spec_steps import generate_artifact, run_tasks_audit

    log.info("START phase=TASKS_SHAPE_AUDIT")
    print("PHASE: tasks_shape_audit")
    findings = scan_placeholders(output_path.read_text())
    if findings:
        log.warning("Placeholder scan found %d deterministic findings", len(findings))
    try:
        findings += await run_tasks_audit(change_dir, config)
    except Exception as audit_err:
        log.warning("Tasks shape audit failed (non-fatal): %s", audit_err)
        print(f"PHASE: tasks_shape_audit_skipped — {audit_err}")
        if not findings:
            return

    if not findings:
        log.info("DONE phase=TASKS_SHAPE_AUDIT findings=0")
        return

    log.warning(
        "DONE phase=TASKS_SHAPE_AUDIT findings=%d — one regeneration pass",
        len(findings),
    )
    for finding in findings:
        log.warning(
            "  finding severity=%s desc=%s",
            finding.get("severity", "?"),
            finding.get("description", "?"),
        )

    findings_text = "\n".join(
        f"- [{f.get('severity', '?')}] {f.get('description', '')}"
        + (f" Fix: {f['suggestion']}" if f.get("suggestion") else "")
        for f in findings
    )
    try:
        content = await generate_artifact(
            "tasks", context, config, audit_findings=findings_text,
        )
    except Exception as regen_err:
        log.warning(
            "Tasks regeneration failed (non-fatal, first pass stands): %s", regen_err
        )
        print(f"PHASE: tasks_regeneration_failed — {regen_err}")
        return
    output_path.write_text(content)
    log.info("Rewrote artifact after shape audit: %s", output_path)
    # One commit per regeneration pass, so the branch history shows the audit
    # actually changed something. No-op unless the pipeline owns a branch.
    _commit_change_dir(
        config, change_dir, "docs(specs): regenerate tasks.md after shape audit",
    )
    print(f"PHASE: tasks_regenerated_after_shape_audit — {len(findings)} findings")


def _commit_change_dir(config, change_dir: Path, message: str) -> None:
    """Commit the change directory after a regeneration pass.

    No-op unless the pipeline owns a branch (``--no-worktree`` stays
    byte-identical to the pre-git-lifecycle pipeline), and no-op when the
    change directory is not under the project — the commit is explicit-path.
    """
    from . import git_ops

    try:
        rel = str(change_dir.relative_to(config.project_dir))
    except (ValueError, TypeError):
        return
    if rel:
        git_ops.commit_stage(config, message, [rel])


async def _generate_rubric(
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate rubric.md using the rubric module."""
    content = await generate_rubric(change_dir, config)
    rubric_path = change_dir / "rubric.md"
    rubric_path.write_text(content)
    log.info("Wrote rubric: %s", rubric_path)


async def _run_audit(change_dir: Path, config) -> list[dict]:
    """Run design audit on generated artifacts."""
    from .llm_steps.spec_steps import run_design_audit
    return await run_design_audit(change_dir, config)


# Severities that earn a regeneration pass. A minor gap is a clarification,
# not a reason to spend two more spec-generation calls.
DESIGN_AUDIT_ACTIONABLE = ("critical", "major")


def design_audit_findings_text(gaps: list[dict]) -> str:
    """The findings block injected into the single design/tasks regeneration.

    Empty string when the audit surfaced nothing at critical or major severity
    — the caller reads that as "report only, nothing to regenerate".
    """
    actionable = [
        g for g in gaps or []
        if isinstance(g, dict) and g.get("severity") in DESIGN_AUDIT_ACTIONABLE
    ]
    if not actionable:
        return ""
    return "\n".join(
        f"- [{g.get('severity', '?')}] ({g.get('category', 'design-audit')}) "
        f"{g.get('description', '')}"
        + (f" Fix: {g['suggestion']}" if g.get("suggestion") else "")
        for g in actionable
    )


async def run_design_audit_stage(
    change_dir: Path,
    config,
    state: BuildState,
    state_path: Path,
) -> list[dict]:
    """Run the design audit and fold its findings back into design.md/tasks.md.

    This is the ONE place the design audit's outcome is acted on; both callers
    (``run_spec_generator`` and the orchestrator's DESIGN_AUDIT phase) go
    through it, so the audit cannot regenerate twice in one build. The second
    caller to arrive costs nothing at all — ``spec_gen.design_audit_done`` is
    checked before the audit call, not after it.

    Shape (deliberately identical to ``apply_prototype_findings`` and
    ``_audit_tasks_shape``): critical/major gaps drive **one** regeneration
    pass of design.md and then tasks.md, the pass is committed, and the audit
    is then re-run **report-only** so the build log records whether the
    critique landed. There is no loop — a document that still has gaps after
    one pass stands, and costs one extra audit call rather than an unbounded
    number.

    Never raises: an audit or regeneration failure is logged and the existing
    artifacts stand. Returns the first-pass gaps (what the audit found on the
    artifacts as generated); the re-check reports its own remaining count.
    """
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    if state.spec_gen.design_audit_done:
        # The stage already ran to completion in this build (or before a
        # resume). Checked BEFORE the audit call, not after: a full build
        # reaches this function twice — once inside run_spec_generator, once in
        # the orchestrator's DESIGN_AUDIT phase — and the audit reads all four
        # artifacts, so re-running it to discover there is nothing left to do
        # is a whole LLM call spent on a log line.
        log.info("SKIP phase=DESIGN_AUDIT reason=recorded-complete-in-state")
        print("PHASE: design_audit_skipped — already run for this build")
        return []

    try:
        gaps = await _run_audit(change_dir, config)
    except Exception as audit_err:
        # Deliberately NOT recorded as done: an LLM failure is a reason to try
        # again (the second call site, or a resume), unlike a completed audit.
        log.warning("Design audit LLM call failed (non-fatal): %s", audit_err)
        print(f"PHASE: design_audit_skipped — {audit_err}")
        return []

    _log_design_audit_gaps(gaps)

    findings_text = design_audit_findings_text(gaps)
    if not findings_text:
        _mark_design_audit_done(state, state_path)
        return gaps

    if state.spec_gen.design_audit_regen_done:
        # A resume, or the second of the two call sites in the same build: the
        # documents already absorbed this critique once. Report only.
        log.info("SKIP phase=DESIGN_AUDIT_FEEDBACK reason=recorded-complete-in-state")
        print("PHASE: design_audit_feedback_skipped — regeneration already applied")
        _mark_design_audit_done(state, state_path)
        return gaps

    regenerated = await _apply_design_audit_findings(change_dir, config, findings_text)
    # Recorded whether or not anything was rewritten: the pass has been spent,
    # and a failed regeneration is not a reason to try again later.
    state.spec_gen.design_audit_regen_done = True
    state.checkpoint(state_path)

    if regenerated:
        await _reaudit_after_design_feedback(change_dir, config)
    _mark_design_audit_done(state, state_path)
    return gaps


def _mark_design_audit_done(state: BuildState, state_path: Path) -> None:
    """Record that the audit stage ran to completion, and checkpoint it.

    Separate from ``design_audit_regen_done``: an audit that found nothing
    actionable never regenerates, so the regen flag alone cannot tell the
    second call site "there is nothing here for you" — it would re-audit to
    rediscover that. This flag is the record that the *stage* is spent.
    """
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    state.spec_gen.design_audit_done = True
    state.checkpoint(state_path)


def _log_design_audit_gaps(gaps: list[dict]) -> None:
    """Record what the audit found, at the severity it found it."""
    dict_gaps = [g for g in gaps or [] if isinstance(g, dict)]
    if not dict_gaps:
        log.info("DONE phase=DESIGN_AUDIT gaps=0")
        return
    critical_gaps = [g for g in dict_gaps if g.get("severity") == "critical"]
    if critical_gaps:
        log.warning(
            "DONE phase=DESIGN_AUDIT gaps=%d critical=%d",
            len(dict_gaps), len(critical_gaps),
        )
        for gap in critical_gaps:
            log.warning(
                "  gap category=%s desc=%s",
                gap.get("category", "?"), gap.get("description", "?"),
            )
        print(f"PHASE: design_audit_warnings — {len(critical_gaps)} critical gaps")
    else:
        log.info("DONE phase=DESIGN_AUDIT gaps=%d critical=0", len(dict_gaps))


async def _apply_design_audit_findings(
    change_dir: Path, config, findings_text: str,
) -> int:
    """Regenerate design.md and then tasks.md ONCE from the audit's findings.

    Returns the number of artifacts rewritten. design.md goes first so the
    tasks regeneration reads the corrected design off disk. A regeneration
    failure is non-fatal — that artifact's first pass stands.
    """
    from .llm_steps.spec_steps import generate_artifact

    cm = ChangeManager(config.specs_dir)
    regenerated = 0

    for artifact_id in ("design", "tasks"):
        context = cm.artifact_context(change_dir, artifact_id)
        if artifact_id == "tasks":
            context["unknowns_content"] = read_unknowns(change_dir)
        output_path = change_dir / context["output_path"]
        if not output_path.exists():
            log.warning(
                "SKIP design-audit feedback for %s — %s does not exist",
                artifact_id, output_path,
            )
            continue
        try:
            content = await generate_artifact(
                artifact_id, context, config, audit_findings=findings_text,
            )
        except Exception as regen_err:  # non-fatal: first pass stands
            log.warning(
                "Design-audit regeneration of %s failed (non-fatal): %s",
                artifact_id, regen_err,
            )
            continue
        output_path.write_text(content)
        regenerated += 1
        log.info("Rewrote %s from design audit findings", output_path)

    if regenerated:
        # One commit per regeneration pass, so the branch history shows the
        # audit actually changed something.
        _commit_change_dir(
            config,
            change_dir,
            "docs(specs): regenerate design.md and tasks.md after design audit",
        )
    print(f"PHASE: design_audit_feedback_applied — {regenerated} artifact(s)")
    return regenerated


async def _reaudit_after_design_feedback(change_dir: Path, config) -> None:
    """Re-run the audit once, REPORT-ONLY — did the critique land?

    No third artifact pass follows this, whatever it says; its only job is to
    put the answer in the build log.
    """
    try:
        remaining = await _run_audit(change_dir, config)
    except Exception as audit_err:
        log.warning("Design audit re-check failed (non-fatal): %s", audit_err)
        print(f"PHASE: design_audit_recheck_skipped — {audit_err}")
        return

    still_open = [
        g for g in remaining or []
        if isinstance(g, dict) and g.get("severity") in DESIGN_AUDIT_ACTIONABLE
    ]
    if still_open:
        log.warning(
            "Design audit re-check: %d critical/major gap(s) remain after the "
            "single regeneration pass (accepted, no loop)",
            len(still_open),
        )
        for gap in still_open:
            log.warning(
                "  remaining gap severity=%s category=%s desc=%s",
                gap.get("severity", "?"),
                gap.get("category", "?"),
                gap.get("description", "?"),
            )
        print(
            f"PHASE: design_audit_recheck — {len(still_open)} critical/major "
            "gap(s) remain, accepted after one pass"
        )
    else:
        log.info("Design audit re-check: no critical or major gaps remain")
        print("PHASE: design_audit_recheck — clean")


def _extract_capabilities(proposal_text: str) -> list[str]:
    """Extract capability names from proposal markdown.

    Looks for lines like: - `capability-name`: description
    under the New Capabilities section.
    """
    import re

    capabilities = []
    # Match backtick-wrapped capability names in list items
    pattern = re.compile(r"^-\s+`([a-z0-9-]+)`", re.MULTILINE)
    for match in pattern.finditer(proposal_text):
        capabilities.append(match.group(1))

    return capabilities


def _load_or_create_state(config) -> BuildState:
    """Load existing state or create new one."""
    state_path = config.state_file_path()
    if state_path.exists():
        try:
            state = BuildState.load(state_path)
            log.info("Resumed state from %s", state_path)
            return state
        except Exception as e:
            log.warning("Failed to load state, starting fresh: %s", e)

    state = BuildState(
        change_name=config.change_name,
        mode=config.mode,
        tier=config.tier,
        state_file=str(state_path),
        phase=BuildPhase.SPEC_GENERATION,
        spec_gen=SpecGenState(),
    )
    state.checkpoint(state_path)
    return state
