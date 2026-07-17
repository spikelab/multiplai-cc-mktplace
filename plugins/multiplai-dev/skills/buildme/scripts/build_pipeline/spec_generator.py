"""Spec generator pipeline — creates all OpenSpec artifacts in dependency order.

Entry point: run_spec_generator(config, args)

Flow:
1. Bootstrap change directory
2. Generate artifacts in dependency order (proposal -> requirements+design -> tasks -> rubric)
3. Run design audit
4. Return exit code
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .change_manager import ChangeManager, ARTIFACT_DAG
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

        # Run design audit (best-effort — failures don't block the build)
        log.info("START phase=DESIGN_AUDIT")
        print("PHASE: design_audit")
        state.advance_to(BuildPhase.DESIGN_AUDIT, config.state_file_path())
        try:
            gaps = await _run_audit(change_dir, config)
        except Exception as audit_err:
            log.warning("Design audit LLM call failed (non-fatal): %s", audit_err)
            print(f"PHASE: design_audit_skipped — {audit_err}")
            gaps = []

        if gaps:
            critical_gaps = [g for g in gaps if g.get("severity") == "critical"]
            if critical_gaps:
                log.warning("DONE phase=DESIGN_AUDIT gaps=%d critical=%d", len(gaps), len(critical_gaps))
                for gap in critical_gaps:
                    log.warning("  gap category=%s desc=%s", gap.get("category", "?"), gap.get("description", "?"))
                print(f"PHASE: design_audit_warnings — {len(critical_gaps)} critical gaps")
            else:
                log.info("DONE phase=DESIGN_AUDIT gaps=%d critical=0", len(gaps))
        else:
            log.info("DONE phase=DESIGN_AUDIT gaps=0")

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

    if artifact_id == "requirements":
        await _generate_requirements(cm, change_dir, config, state)
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


async def _audit_tasks_shape(
    change_dir: Path,
    context: dict,
    config,
    output_path: Path,
) -> None:
    """Audit tasks.md for horizontal decomposition; regenerate ONCE on findings.

    Any findings trigger exactly one regeneration pass with the findings
    injected into the tasks prompt (no re-audit loop). Audit failures are
    non-fatal — the first-pass tasks.md stands.
    """
    from .llm_steps.spec_steps import generate_artifact, run_tasks_audit

    log.info("START phase=TASKS_SHAPE_AUDIT")
    print("PHASE: tasks_shape_audit")
    try:
        findings = await run_tasks_audit(change_dir, config)
    except Exception as audit_err:
        log.warning("Tasks shape audit failed (non-fatal): %s", audit_err)
        print(f"PHASE: tasks_shape_audit_skipped — {audit_err}")
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
    print(f"PHASE: tasks_regenerated_after_shape_audit — {len(findings)} findings")


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
