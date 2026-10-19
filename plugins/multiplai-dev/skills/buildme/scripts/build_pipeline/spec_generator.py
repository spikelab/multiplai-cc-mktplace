"""Spec generator pipeline — creates all OpenSpec artifacts in dependency order.

Entry point: run_spec_generator(config, args)

Flow:
1. Bootstrap change directory
2. Generate artifacts in dependency order
   (proposal -> use-cases+requirements+design -> unknowns -> tasks -> rubric)
3. Run design audit
4. Return exit code
"""

from __future__ import annotations

from build_pipeline.budget import BudgetExceededError

import asyncio
import hashlib
import json
import copy
import shutil
import os
import tempfile
import logging
import re
import sys
from pathlib import Path

from . import budget
from .change_manager import ChangeManager, ARTIFACT_DAG, expected_capabilities, valid_requirement, valid_requirement_text
from .dependencies import NewDependency, design_decisions_text, detect_new_dependencies
from .gates import unknowns_gate
from .models import ArtifactStatus, BuildPhase
from .rubric import detect_change_type, generate_rubric
from .state import BuildState, SpecGenState

log = logging.getLogger(__name__)


async def run_spec_generator(config, args=None, *, standalone: bool = True) -> int:
    """Main entry point for spec generation pipeline.

    Args:
        config: BuildConfig with project settings
        args: Optional CLI args namespace

    Returns:
        Exit code (0=success, 1=failure)
    """
    print(f"PHASE: spec_generation — {config.change_name}")

    if standalone:
        budget.reset()
        budget.configure(max_tokens=config.budget_max_tokens, max_usd=config.budget_max_usd)
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    change_dir = cm.create_change(config.change_name)

    # Load or create state
    state = _load_or_create_state(config)
    if standalone:
        budget.get_budget().load_state(state.budget)

    # Ensure spec_gen sub-state exists
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
        state.checkpoint(config.state_file_path())

    try:
        # Generate artifacts in dependency order
        log.info("START phase=ARTIFACT_GENERATION change=%s", config.change_name)
        await _generate_all_artifacts(cm, change_dir, config, state)
        log.info("DONE phase=ARTIFACT_GENERATION")

        # Full builds prototype first, then audit/review the final plan once.
        if standalone:
            log.info("START phase=DESIGN_AUDIT")
            print("PHASE: design_audit")
            await run_design_audit_stage(change_dir, config, state, config.state_file_path())
            state.advance_to(BuildPhase.DESIGN_AUDIT, config.state_file_path())

        print("PHASE: spec_generation_complete")
        return 0

    except budget.BudgetExceededError:
        state.checkpoint(config.state_file_path())
        raise
    except Exception as e:
        state.checkpoint(config.state_file_path())
        log.error("FAIL phase=SPEC_GENERATION error=%s", e, exc_info=True)
        print(f"PHASE: spec_generation_failed — {e}")
        return 1
    except BaseException:
        state.checkpoint(config.state_file_path())
        raise


async def _generate_all_artifacts(
    cm: ChangeManager,
    change_dir: Path,
    config,
    state: BuildState,
) -> None:
    """Generate all artifacts in dependency order, with resume support."""
    from .llm_steps.spec_steps import generate_artifact

    reconcile_plan(config, state)
    max_iterations = len(ARTIFACT_DAG) * 2  # safety limit
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        status = cm.artifact_status(change_dir, state.spec_gen.stale_artifacts if state.spec_gen else [])
        ready = [name for name, outcome in status.items() if outcome == ArtifactStatus.READY]

        if not ready:
            # Check if all done
            status = cm.artifact_status(change_dir, state.spec_gen.stale_artifacts if state.spec_gen else [])
            if all(s == ArtifactStatus.DONE for s in status.values()):
                log.info("All artifacts generated")
                break
            # Deadlock — some artifacts can't be created
            blocked = [a for a, s in status.items() if s == ArtifactStatus.BLOCKED]
            log.error("Deadlock: blocked artifacts %s with nothing ready", blocked)
            raise RuntimeError(f"Artifact generation deadlocked: {blocked}")

        for artifact_id in ready:
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
            _record_explainer_outcome(config, state)
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
            _record_explainer_outcome(config, state)
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
            await _audit_tasks_shape(change_dir, context, config, tasks_path, state)
            state.spec_gen.tasks_audit_done = True
            state.checkpoint(config.state_file_path())
    elif state.spec_gen:
        log.info("SKIP phase=TASKS_SHAPE_AUDIT reason=recorded-complete-in-state")

    reconcile_plan(config, state)
    if state.spec_gen and "rubric" in state.spec_gen.stale_artifacts:
        await _generate_rubric(change_dir, config, state)
        reconcile_plan(config, state, regenerated={"rubric"})

    # Verify completeness
    final_status = cm.artifact_status(change_dir, state.spec_gen.stale_artifacts if state.spec_gen else [])
    done_count = sum(1 for s in final_status.values() if s == ArtifactStatus.DONE)
    log.info("Artifact generation complete: %d/%d done", done_count, len(ARTIFACT_DAG))
    if done_count != len(ARTIFACT_DAG):
        raise RuntimeError("Planning artifacts are incomplete")


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
    elif artifact_id == "use-cases":
        await _generate_use_cases(change_dir, context, config, state)
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
            research=read_research(state),
            codebase_analysis=read_codebase_analysis(state),
            reference_docs=config.reference_docs_text(),
        )

        # Write the artifact
        output_path = change_dir / context["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(output_path, content)
        log.info("Wrote artifact: %s", output_path)

        # Tasks-shape audit: catch horizontal decomposition before the rubric
        # and implementation build on a layered breakdown. Prompt instructions
        # alone drift — this audit is the enforcement. Completion is recorded
        # in checkpoint state (not file existence): tasks.md already exists at
        # this point, so a crash mid-audit would otherwise mark the artifact
        # DONE and silently skip the audit on resume (see
        # _generate_all_artifacts' durability pass).
        if artifact_id == "tasks":
            await _audit_tasks_shape(change_dir, context, config, output_path, state)
            if state.spec_gen:
                state.spec_gen.tasks_audit_done = True

    reconcile_plan(config, state, regenerated={artifact_id})
    if state.spec_gen and artifact_id == "tasks":
        state.spec_gen.tasks_audit_done = True
    if state.spec_gen and artifact_id == "unknowns":
        _record_explainer_outcome(config, state)

    # Mark completed. `completed_artifacts` is a resume ledger keyed by id, so
    # a re-generated artifact (the use-cases backfill regenerates tasks and
    # rubric) must not be entered twice.
    if state.spec_gen:
        if artifact_id not in state.spec_gen.completed_artifacts:
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

    proposal = proposal_path.read_text()
    capabilities = expected_capabilities(proposal)
    if not capabilities:
        raise ValueError("Proposal must name at least one capability before requirements can complete")
    source = hashlib.sha256(proposal.encode()).hexdigest()
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    changed = state.spec_gen.requirements_source not in ("", source)
    force_members = "requirements" in state.spec_gen.stale_artifacts
    state.spec_gen.expected_capabilities = capabilities
    if changed:
        state.spec_gen.requirements_members = {}
    state.spec_gen.requirements_source = source
    state.checkpoint(config.state_file_path())

    req_dir = change_dir / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)

    for cap_name in capabilities:
        req_file = req_dir / f"{cap_name}.md"
        if valid_requirement(req_file) and ((not changed and not force_members) or state.spec_gen.requirements_members.get(cap_name) == source):
            log.info("Requirements for %s already exist, skipping", cap_name)
            continue

        context = cm.artifact_context(change_dir, "requirements")
        context["capability_name"] = cap_name

        content = await generate_artifact("requirements", context, config)

        if not valid_requirement_text(content):
            raise ValueError(f"Invalid requirements for {cap_name}: expected Requirement/Scenario/WHEN/THEN")
        atomic_write(req_file, content)
        state.spec_gen.requirements_members[cap_name] = source
        state.checkpoint(config.state_file_path())
        log.info("Wrote requirements: %s", req_file)


# The use-cases prompt lives here rather than in prompts/spec_generation.py
# because `spec_steps._build_prompt` dispatches on a fixed set of artifact ids
# and raises ValueError on anything else — this artifact therefore builds its
# own prompt and calls llm_call directly. Moving the template into
# prompts/spec_generation.py and adding the matching `_build_prompt` branch is
# the tidier home for it, and is a change to files this work item does not own.
USE_CASES_PROMPT = """\
You are generating the use cases document for a spec-driven change.

## Project Context
{project_context}

## Interview Summary
{interview_summary}

## Proposal
{proposal_content}

## Instructions
{instruction}

## Output Format
Generate the document as markdown following this template structure:

{template}

Rules:
- Every persona block names who they are, the job they are doing, and the
  constraint they are under
- Every use case reads exactly: <persona> wants <goal> so that <observable outcome>
- Every use case names a persona defined in the Personas section
- Every outcome is observable from OUTSIDE the system — something the persona
  can see, read, or measure without reading the code. "The cache is warmed" is
  not an outcome; "the page loads before they finish reading the heading" is
- No use case invents a persona, a goal, or an outcome the proposal and the
  interview do not support

Output ONLY the markdown content. No commentary.
"""


async def _generate_use_cases(
    change_dir: Path,
    context: dict,
    config,
    state: BuildState,
) -> None:
    """Write use-cases.md — the personas and the outcomes they can observe.

    Separate from `generate_artifact` for the reason stated on
    ``USE_CASES_PROMPT``: that function's prompt builder knows a fixed set of
    artifact ids. The call is otherwise identical — same model, same effort,
    same tool-free system prompt, one budget label of its own.
    """
    from .sdk import llm_call

    proposal_path = change_dir / "proposal.md"
    proposal_content = (
        proposal_path.read_text() if proposal_path.exists() else "(not available)"
    )

    prompt = USE_CASES_PROMPT.format(
        project_context=context.get("context", ""),
        interview_summary=state.interview_summary or "(none provided)",
        proposal_content=proposal_content,
        instruction=context.get("instruction", ""),
        template=context.get("template", ""),
    )

    content = await llm_call(
        prompt,
        model=config.model_for("spec_artifact"),
        effort=config.effort_for("spec_artifact"),
        system_prompt=(
            "You are a technical specification generator. Your ONLY job is to "
            "generate the requested document content based on the context provided "
            "in the prompt. Output the document directly — do NOT attempt to use "
            "tools, explore code, or request more information. All the context you "
            "need is in the prompt. Output ONLY markdown content."
        ),
        budget_label="spec:use-cases",
    )

    output_path = change_dir / (context.get("output_path") or "use-cases.md")
    atomic_write(output_path, content)
    log.info("Wrote artifact: %s", output_path)


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


def read_codebase_analysis(state: BuildState) -> str:
    """The CODEBASE_ANALYSIS phase's report, as text for the design prompt.

    The checkpoint stores a *path* (`spec_gen.codebase_analysis_path`); the
    prompt needs the contents. A path that no longer resolves falls back to
    the stored string rather than dropping it, and an absent one returns ""
    so the prompt says "(new project)" exactly as it did before the phase
    existed.
    """
    raw = (state.spec_gen.codebase_analysis_path if state.spec_gen else "") or ""
    if not raw:
        return ""
    path = Path(raw)
    if path.is_file():
        return path.read_text()
    log.warning("Recorded codebase analysis is not a readable file: %s", raw)
    return raw


def read_research(state: BuildState) -> str:
    """The research file's *content*, fenced as untrusted, for the proposal prompt.

    The checkpoint stores a path (`state.research_path`); the prompt's
    `## Research Findings` slot needs what the file says. Research is written
    from web pages and other external text, so it goes in an untrusted-content
    fence: data for the proposal, never instructions to it. No path recorded
    returns "" (the prompt then says "(no research conducted)"); a recorded
    path that is not a readable file warns and returns "(none provided)".
    """
    raw = state.research_path or ""
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_file():
        log.warning("Recorded research is not a readable file: %s", raw)
        return "(none provided)"
    from multiplai_core.untrusted import fence

    return "\n".join(fence(path.read_text(), source=path.name)) or "(none provided)"


def _dependency_list_text(deps: list[NewDependency]) -> str:
    return "\n".join(
        f"- `{d.name}` — named in {', '.join(d.mentioned_in) or 'the specs'}; {d.evidence}"
        for d in deps
    ) or "(none)"


def _record_explainer_outcome(config, state: BuildState) -> None:
    """Advisory explanations never impersonate missing required evidence."""
    if state.spec_gen is None:
        return
    if not getattr(config, "explainers_active", True):
        state.spec_gen.record_step("explainers", "skipped", "disabled")
        return
    deps = detect_new_dependencies(config.change_dir, config.project_dir)
    result = unknowns_gate((config.change_dir / "unknowns.md").read_text(), deps)
    state.spec_gen.record_step("explainers", "passed" if result.passed else "advisory", result.reason)


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
            _record_explainer_outcome(config, state)
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
            _record_explainer_outcome(config, state)
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
        if isinstance(result, BudgetExceededError) or (isinstance(result, BaseException) and not isinstance(result, Exception)):
            raise result
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
        _record_explainer_outcome(config, state)


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
    except BudgetExceededError:
        raise
    except Exception as regen_err:
        log.warning(
            "Unknowns regeneration failed (non-fatal, first pass stands): %s",
            regen_err,
        )
        print(f"PHASE: unknowns_regeneration_failed — {regen_err}")
        return

    atomic_write(output_path, content)
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


# What a block calls its own work: "these", "this slice", "the criteria".
# Never a noun of its own ("Authentication is already handled by Block 1's
# middleware"): that is a side concern another block covers, which says
# nothing about whether this block adds behaviour.
_BLOCK_ITSELF = (
    r"(?:these|they|them|this(?:\s+(?:block|slice|task|step|work))?"
    r"|(?:all\s+(?:of\s+)?)?(?:these|the)\s+(?:criteria|scenarios|cases"
    r"|requirements|behaviou?rs?|checks|assertions))"
)
# A block that says, in its own words, that an earlier block already does its
# job: "these are satisfied by Block 1", "this slice is satisfied by Block 1",
# "Block 1's implementation already handles these". Phrases that only say the
# block USES something an earlier block built ("the tokenizer already built in
# Block 1") are deliberately not here: that is how an ordinary leaf block reads.
_SATISFIED_EARLIER_RE = re.compile(
    rf"\b{_BLOCK_ITSELF}\s+(?:is|are|was|were|has\s+been|have\s+been)\s+"
    r"(?:already\s+)?(?:satisfied|implemented|delivered|built|covered|handled"
    r"|met)\b[^.\n]{0,60}\bBlock\s+\d+"
    r"|(?:^|(?<=\.))\s*(?:already\s+)?(?:satisfied|covered|delivered)\s+by\b"
    r"[^.\n]{0,60}\bBlock\s+\d+"
    r"|\bBlock\s+\d+(?:'s\s+\w+)?\s+already\s+"
    rf"(?:satisfies|covers|meets|handles|implements|delivers)\s+{_BLOCK_ITSELF}\b",
    re.IGNORECASE | re.MULTILINE,
)
# A block that says it writes no code. Not "no code change to the core": a
# "no code" scoped to anything but this block is about another part of it.
_NO_CODE_RE = re.compile(
    r"(?:\b(?:no|without)\s+(?:new\s+|production\s+|source\s+|further\s+)?"
    r"(?:code|implementation)\s+(?:changes?|is\s+(?:needed|required)|needed"
    r"|required|work)\b"
    r"|\b(?:needs|requires)\s+no\s+(?:new\s+)?(?:code|implementation)\b"
    r"|\bno\s+(?:new|production|source)\s+code\b)"
    r"(?!\s+(?:to|in|for|on|outside|beyond)\s+(?!this\b|these\b|here\b))",
    re.IGNORECASE,
)
# Test work: "Add tests for tabs", "Write assertions", "Assert …", "Tests for …".
_TEST_WORK_RE = re.compile(
    r"\b(?:add|write|extend|create|expand)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:tests?|test\s+cases?|assertions?)\b",
    re.IGNORECASE,
)
_TEST_WORK_START_RE = re.compile(
    r"^\W*(?:(?:add|write|extend|create|expand)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:tests?|test\s+cases?|assertions?)\b|assert\b|tests?\b|coverage\b)",
    re.IGNORECASE,
)
_ADD_FILE_START_RE = re.compile(
    r"^\W*(?:add|write|extend|create)\s+`([^`]+)`", re.IGNORECASE,
)
# A backticked file path: no spaces or brackets, and a directory or a known
# file extension (so `textkit.summary.summarize` and `str.split` are not paths).
_PATH_TOKEN_RE = re.compile(r"`([\w./-]+)`")
_PATH_EXTENSIONS = frozenset((
    "py pyi js jsx ts tsx mjs cjs go rs rb java kt kts swift c h cc cpp hpp cs "
    "php scala sh bash sql html htm css scss vue svelte toml json yaml yml cfg "
    "ini md txt lock xml gradle"
).split())
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__)/"
    r"|(?:^|/)(?:test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(?:test|spec)\.[^/]+|conftest\.py)$"
)


def _named_paths(text: str) -> list[str]:
    paths = []
    for token in _PATH_TOKEN_RE.findall(text):
        name = token.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ("/" in token and not token.startswith("/")) or ext in _PATH_EXTENSIONS:
            paths.append(token)
    return paths


def _left_unchanged(path: str, text: str) -> bool:
    """The block says it leaves this file alone."""
    p = re.escape(path)
    return bool(
        re.search(rf"`{p}`[^.;\n]{{0,40}}\bunchanged\b", text, re.IGNORECASE)
        or re.search(
            rf"\b(?:no\s+changes?\s+to|(?:do\s+not|don't|never)\s+"
            rf"(?:modify|change|touch|edit))\s+(?:the\s+)?`{p}`",
            text, re.IGNORECASE,
        )
    )


def _block_bodies(text: str) -> dict[int, tuple[str, str]]:
    """Each block's heading and text, prose AND checkboxes, without its
    Interfaces and Satisfies lines. ``parse_blocks`` keeps only the checkbox
    lines of a checkbox block, which drops the prose where a block says what
    it is."""
    header = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)
    matches = list(header.finditer(text))
    bodies = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end]
        body = re.split(r"^Interfaces:\s*$", body, maxsplit=1, flags=re.MULTILINE)[0]
        body = re.sub(r"^Satisfies:.*$", "", body, flags=re.MULTILINE)
        bodies[int(match.group(1))] = (match.group(2), body)
    return bodies


def _first_instruction(body: str) -> str:
    """The first sentence of a block's first line, without a checkbox or an
    item number (`- [ ] 2.1 `)."""
    for line in body.splitlines():
        line = re.sub(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?",
                      "", line).strip()
        if line:
            return re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return ""


def _is_test_work(heading: str, body: str) -> bool:
    """The block says it is test work: its heading names a test file or opens
    with tests, or its first instruction adds tests or a test file. A block
    whose tests are a side note ("Tests in `tests/test_cli.py`") is not."""
    first = _first_instruction(body)
    added = _ADD_FILE_START_RE.match(first)
    heading_paths = _named_paths(heading)
    return bool(
        (heading_paths and all(_TEST_PATH_RE.search(p) for p in heading_paths))
        or _TEST_WORK_START_RE.match(heading)
        or _TEST_WORK_START_RE.match(first)
        or (added and _TEST_PATH_RE.search(added.group(1)))
    )


# A step that writes code. A test-first block writes its tests and then its
# code ("- [ ] 2.2 Implement HMAC verification", "…, then implement signature
# checking", "Make count split on tabs"), often without naming its source file.
# A check is not code ("Confirm", "Verify", "Assert", "Make sure", "Make no
# change"): a block whose steps only test and check is the block the scan
# exists to catch.
_CODE_VERB_RE = re.compile(
    r"^(?:implement|make(?!\s+(?:no|sure)\b)|build|wire|handle|raise|return"
    r"|reject|accept|support|change(?!\s+nothing\b)|update|modify|fix"
    r"|refactor|compute|calculate|parse|split|emit|print|output|store|save"
    r"|persist|load|define|register|expose|convert|validate|enforce|route"
    r"|send|replace|rename|move|remove|delete|strip|normali[sz]e|format"
    r"|render|generate|introduce|extract|guard|cache|append|insert|sort"
    r"|filter|merge|(?:de)?seriali[sz]e|encode|decode|hash|sign|wrap"
    r"|add|write|create|extend|expand)\b",
    re.IGNORECASE,
)
# "Add", "Write" and the like write code unless what they add is a test.
_WRITE_VERB_RE = re.compile(r"^(?:add|write|create|extend|expand)\b", re.IGNORECASE)
_TEST_WORD_RE = re.compile(
    r"\b(?:tests?|assert\w*|fixtures?|parametri[sz]\w*)\b", re.IGNORECASE,
)
# Steps: sentences, clauses after ";", and "…, then …".
_STEP_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|,?\s+(?:and\s+)?then\s+", re.IGNORECASE)
_STEP_LEAD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:(?:then|and|also|finally|next|first)\b[,\s]*)*",
    re.IGNORECASE,
)


def _has_code_step(body: str) -> bool:
    """Some step of the block, prose or checkbox, writes code: it opens with
    a verb that changes code, and an "add"/"write" step adds something that
    is not a test."""
    for line in body.splitlines():
        for step in _STEP_SPLIT_RE.split(line):
            step = _STEP_LEAD_RE.sub("", step).strip()
            if not _CODE_VERB_RE.match(step):
                continue
            if _WRITE_VERB_RE.match(step) and (
                _TEST_WORD_RE.search(step)
                or any(_TEST_PATH_RE.search(p) for p in _named_paths(step))
            ):
                continue
            return True
    return False


def scan_no_behaviour_blocks(tasks_path: Path) -> list[dict]:
    """Deterministic scan for blocks that add no behaviour.

    Such a block's tests pass before it is built, so the TDD RED gate fails it
    and the build with it. A block is flagged only when it produces nothing
    (`Produces: (none)`) AND its own text says it adds no code:
    - it says an earlier block already satisfies it, with itself as the
      subject ("these are satisfied by Block 1"; never a side concern such as
      "authentication is already handled by Block 1's middleware");
    - it says it needs no code, unscoped ("no code change", never "no code
      change to the core");
    - it names no file it changes but test files and is test work (its
      heading or first instruction adds tests or a test file; a leaf block
      need not name its source file, so "Tests in `tests/x.py`" is not
      enough), and no step of it writes code;
    - every file it names is one it leaves unchanged, it adds tests, and no
      step of it writes code.
    A step writes code when it opens with a verb that changes code
    ("Implement …", "Make count split on tabs", "then reject …", "Add a
    `--summary` flag"), so a test-first block (tests, then the code) is
    never flagged on its test file.
    `Produces: (none)` alone only says no later block calls it, and "Verify" or
    "Confirm" is an ordinary verb for new behaviour (verify a signature), so
    neither is ever enough. A block that names no file is never flagged on
    files. The suggestion tells the rewrite to keep a block that does add
    behaviour and name its code, so a false positive costs one rewrite, never
    the block. Returns audit-finding dicts (same shape as
    ``scan_placeholders``, plus the block's ``block`` number and ``name``).
    """
    from .tdd_engine import parse_blocks

    if not tasks_path.exists():
        return []
    bodies = _block_bodies(tasks_path.read_text())
    findings = []
    for block in parse_blocks(tasks_path):
        if block.produces:
            continue
        heading, body = bodies.get(block.number, (block.name, block.description))
        text = f"{heading}\n{body}"
        paths = _named_paths(text)
        changed = [p for p in paths if not _left_unchanged(p, text)]
        if _SATISFIED_EARLIER_RE.search(text):
            why = "it says an earlier block already satisfies it"
        elif _NO_CODE_RE.search(text):
            why = "it says it needs no code"
        elif not paths or any(not _TEST_PATH_RE.search(p) for p in changed):
            continue
        elif _has_code_step(body):
            continue
        elif changed and _is_test_work(heading, body):
            why = (
                "it is test work and names no code to write (every file it "
                "changes is a test file)"
            )
        elif not changed and _TEST_WORK_RE.search(text):
            why = (
                "it adds tests and names no code to write (every file it "
                "names is one it leaves unchanged)"
            )
        else:
            continue
        source = re.search(r"Block\s+(\d+)", " ".join(block.consumes))
        home = f"Block {source.group(1)}" if source else "the earlier block that builds it"
        findings.append({
            "category": "no-new-behaviour",
            "severity": "critical",
            "block": block.number,
            "name": block.name,
            "description": (
                f"Block {block.number} \"{block.name}\" adds no behaviour: it "
                f"produces nothing and {why}, so its tests would pass before "
                "it is built and the TDD RED gate would fail the build."
            ),
            "suggestion": (
                f"If {home} already delivers these criteria, move them and "
                f"their tests into {home} and remove this block. If this block "
                "does add behaviour, keep it and name the code it changes and "
                "what that code does, so its tests fail until it is built."
            ),
        })
    return findings


def tasks_findings_text(findings: list[dict]) -> str:
    """Audit findings as the ``audit_findings`` text of a tasks rewrite."""
    return "\n".join(
        f"- [{f.get('severity', '?')}] {f.get('description', '')}"
        + (f" Fix: {f['suggestion']}" if f.get("suggestion") else "")
        for f in findings
    )


def report_no_behaviour_blocks_left(tasks_path: Path) -> list[dict]:
    """After a rewrite, name any block the scan still flags.

    Every audit here rewrites once and lets the plan stand, so this only
    reports: a block named here will most likely fail the TDD RED gate.
    """
    left = scan_no_behaviour_blocks(tasks_path)
    if left:
        names = ", ".join(f"Block {f['block']}" for f in left)
        log.warning(
            "No-behaviour blocks remain after the rewrite (the TDD RED gate "
            "will likely fail them): %s", names,
        )
        print(f"PHASE: no_behaviour_blocks_remain — {names}", flush=True)
    return left


async def _audit_tasks_shape(
    change_dir: Path,
    context: dict,
    config,
    output_path: Path,
    state: BuildState | None = None,
) -> None:
    """Audit tasks.md for horizontal decomposition; regenerate ONCE on findings.

    Findings come from three sources: the deterministic placeholder scan and
    no-behaviour block scan (both always run) and the LLM shape/traceability
    audit. Any findings trigger exactly
    one regeneration pass with the findings injected into the tasks prompt
    followed by a required recheck. Missing audit evidence and unresolved
    blocking findings stop planning. Repair admission survives interruption.
    """
    from .llm_steps.spec_steps import generate_artifact, run_tasks_audit

    log.info("START phase=TASKS_SHAPE_AUDIT")
    print("PHASE: tasks_shape_audit")
    findings = scan_placeholders(output_path.read_text())
    findings += scan_no_behaviour_blocks(output_path)
    if findings:
        log.warning("Deterministic scans found %d findings", len(findings))
    findings += await run_tasks_audit(change_dir, config)
    findings = [f for f in findings if f.get("severity", "major") in {"critical", "major"}]

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

    if state and state.spec_gen:
        if state.spec_gen.tasks_audit_regen_done:
            raise RuntimeError("Tasks audit repair already attempted; resolve blocking findings before resuming")
        state.spec_gen.tasks_audit_regen_done = True
        state.checkpoint(config.state_file_path())

    findings_text = tasks_findings_text(findings)
    content = await generate_artifact(
        "tasks", context, config, reference_docs=config.reference_docs_text(),
        audit_findings=findings_text,
    )
    atomic_write(output_path, content)
    log.info("Rewrote artifact after shape audit: %s", output_path)
    # One commit per regeneration pass, so the branch history shows the audit
    # actually changed something. No-op unless the pipeline owns a branch.
    _commit_change_dir(
        config, change_dir, "docs(specs): regenerate tasks.md after shape audit",
    )
    print(f"PHASE: tasks_regenerated_after_shape_audit — {len(findings)} findings")
    remaining = scan_placeholders(output_path.read_text()) + scan_no_behaviour_blocks(output_path)
    remaining += await run_tasks_audit(change_dir, config)
    if any(f.get("severity", "major") in {"critical", "major"} for f in remaining):
        raise RuntimeError("Tasks audit still has blocking findings after bounded repair")


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
    atomic_write(rubric_path, content)
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
    """Audit the current revision, request one owner repair, and require recheck.

    Failed or unresolved blocking reviews checkpoint a blocked outcome. An
    unchanged passed revision costs no additional model call on resume.
    """
    reconcile_plan(config, state)
    sg = state.spec_gen
    assert sg is not None
    if sg.step_passed("design_audit"):
        return []
    try:
        gaps = await _run_audit(change_dir, config)
        text = design_audit_findings_text(gaps)
        if text and not sg.design_audit_regen_done:
            sg.design_audit_regen_done = True
            state.checkpoint(state_path)
            await rewrite_plan(config, state, text, design=True)
            gaps = await _run_audit(change_dir, config)
            text = design_audit_findings_text(gaps)
        if text:
            sg.record_step("design_audit", "blocked", text)
            state.checkpoint(state_path)
            raise RuntimeError("Design audit still has blocking findings after bounded repair")
        sg.record_step("design_audit")
        state.checkpoint(state_path)
        return gaps
    except BaseException:
        if not sg.step_passed("design_audit"):
            sg.record_step("design_audit", "blocked", "Audit or repair did not complete")
        state.checkpoint(state_path)
        raise




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






def _extract_capabilities(proposal_text: str) -> list[str]:
    """Extract capability names from proposal markdown.

    Looks for lines like: - `capability-name`: description
    under the New Capabilities section.

    Scoped to `## Capabilities`, because the template tells the generator to
    wrap every dependency name in backticks under `## Impact` — and a
    whole-file scan turned `django-ses` into a capability and wrote
    `requirements/django-ses.md` for it. A proposal with no `## Capabilities`
    heading falls back to the whole document minus `## Impact`: the old
    behaviour, minus the one section known to carry backticked
    non-capabilities.
    """
    return expected_capabilities(proposal_text)


def _load_or_create_state(config) -> BuildState:
    """Load existing state or create new one."""
    state_path = config.state_file_path()
    if state_path.exists():
        state = BuildState.load(state_path)
        log.info("Resumed state from %s", state_path)
        return state

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


def atomic_write(path: Path, content: str) -> None:
    """Publish a complete document; interruption leaves the previous file intact."""
    if not content.strip():
        raise ValueError(f"Empty planning document: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _plan_hashes(config) -> dict[str, str]:
    files = {name: config.change_dir / name for name in (
        "proposal.md", "use-cases.md", "design.md", "unknowns.md", "tasks.md", "rubric.md",
        "interview.md", "research.md", "codebase-analysis.md")}
    files.update({"requirements/" + p.name: p for p in (config.change_dir / "requirements").glob("*.md")})
    files.update({"registry/" + p.name: p for p in (config.specs_dir / "registry").glob("*.md")})
    files["config.yaml"] = config.specs_dir / "config.yaml"
    hashes = {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in files.items() if p.is_file()}
    hashes["references"] = hashlib.sha256(config.reference_docs_text().encode()).hexdigest()
    hashes["granularity"] = hashlib.sha256(config.task_granularity.encode()).hexdigest()
    return hashes


def reconcile_plan(config, state: BuildState, *, regenerated: set[str] | None = None) -> None:
    """Invalidate the explicit planning dependencies and publish their revision.

    The state is the only evidence ledger. Existing filenames remain the public
    interface; their existence alone cannot validate dependent artifacts.
    """
    if state.spec_gen is None:
        state.spec_gen = SpecGenState()
    sg = state.spec_gen
    recovered = _finish_plan_publication(config)
    retired = False
    proposal = config.change_dir / "proposal.md"
    expected = set(expected_capabilities(proposal.read_text())) if proposal.exists() else set()
    if expected:
        for path in (config.change_dir / "requirements").glob("*.md"):
            if path.stem not in expected:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                destination = config.change_dir / "superseded-requirements" / f"{path.stem}-{digest}.md"
                destination.parent.mkdir(exist_ok=True)
                os.replace(path, destination)
                retired = True
    current = _plan_hashes(config)
    previous = sg.artifact_hashes
    changed = {key for key in previous.keys() | current.keys() if previous.get(key) != current.get(key)}
    stale = set(sg.stale_artifacts)
    if retired:
        stale.update({"tasks", "rubric"})
    if previous:
        if "proposal.md" in changed:
            stale.update({"requirements", "use-cases", "design", "unknowns", "tasks", "rubric"})
        if any(key.startswith("requirements/") for key in changed) or "use-cases.md" in changed:
            stale.update({"tasks", "rubric"})
        if "design.md" in changed:
            stale.update({"unknowns", "tasks", "rubric"})
        if "unknowns.md" in changed:
            stale.update({"tasks", "rubric"})
        if "tasks.md" in changed:
            stale.add("rubric")
        if changed & {"interview.md", "research.md"}:
            stale.update(ARTIFACT_DAG)
        if changed & {"config.yaml", "references", "granularity", "codebase-analysis.md"} or any(
                key.startswith("registry/") for key in changed):
            stale.update({"design", "unknowns", "tasks", "rubric"})
    if not ChangeManager(config.specs_dir)._artifact_exists(config.change_dir, "requirements/*.md"):
        stale.update({"tasks", "rubric"})
    stale -= (regenerated or set()) | recovered
    sg.stale_artifacts = sorted(stale)
    revision = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
    if revision != sg.revision:
        # Evidence for any earlier revision is stale. Prototype and explanation
        # stages are advisory and do not authorize implementation.
        sg.revision = revision
        state.verification = None
        state.split_required = False
        state.size_accepted = False
    sg.artifact_hashes = current
    if recovered:
        state.checkpoint(config.state_file_path())
        (config.change_dir / ".plan-publication.json").unlink(missing_ok=True)


def _finish_plan_publication(config) -> set[str]:
    """Complete an interrupted, prevalidated publication, refusing intervening edits."""
    journal = config.change_dir / ".plan-publication.json"
    if not journal.exists():
        return set()
    files = json.loads(journal.read_text())
    for name, item in files.items():
        if Path(name).name != name or name not in {"design.md", "tasks.md", "rubric.md", "unknowns.md"}:
            raise ValueError("Invalid planning publication journal")
        path = config.change_dir / name
        actual = path.read_text() if path.exists() else None
        if actual not in (item["before"], item["after"]):
            raise RuntimeError(f"Planning publication interrupted and {name} changed; preserve edits before recovery")
    for name, item in files.items():
        atomic_write(config.change_dir / name, item["after"])
    return {Path(name).stem for name in files}


async def rewrite_plan(config, state: BuildState, findings_text: str, *, design: bool = False) -> int:
    """The sole feedback publisher: stage, validate, then publish one revision.

    Failed generation leaves the original set intact. A crash during publication
    leaves a journal that must reconcile before any consumer accepts the files.
    """
    from .llm_steps.spec_steps import generate_artifact
    from .tdd_engine import parse_blocks

    reconcile_plan(config, state)
    names = ["design", "tasks"] if design else ["tasks"]
    with tempfile.TemporaryDirectory(prefix="buildme-plan-") as directory:
        staged = copy.copy(config)
        staged.specs_dir = Path(directory) / "specs"
        staged._change_dir_override = ""
        staged.change_dir = staged.specs_dir / "changes" / config.change_name
        shutil.copytree(config.change_dir, staged.change_dir)
        registry = config.specs_dir / "registry"
        if registry.exists():
            shutil.copytree(registry, staged.specs_dir / "registry")
        source_config = config.specs_dir / "config.yaml"
        if source_config.exists():
            shutil.copy2(source_config, staged.specs_dir / "config.yaml")
        cm = ChangeManager(staged.specs_dir)
        for name in list(names):
            if name == "tasks" and design:
                await _generate_unknowns(cm, staged.change_dir, staged, state.model_copy(deep=True))
                names.append("unknowns")
            context = cm.artifact_context(staged.change_dir, name)
            context["unknowns_content"] = read_unknowns(staged.change_dir)
            text = await generate_artifact(name, context, staged, audit_findings=findings_text,
                                          codebase_analysis=read_codebase_analysis(state),
                                          reference_docs=config.reference_docs_text())
            atomic_write(staged.change_dir / f"{name}.md", text)
        if not parse_blocks(staged.change_dir / "tasks.md"):
            raise ValueError("Rewritten tasks contain no implementation blocks")
        if scan_no_behaviour_blocks(staged.change_dir / "tasks.md"):
            raise ValueError("Rewritten tasks still contain blocks with no behavior")
        if not cm._artifact_exists(staged.change_dir, "requirements/*.md"):
            raise ValueError("Cannot publish a plan with incomplete capability requirements")
        from .llm_steps.spec_steps import run_tasks_audit
        findings = await run_tasks_audit(staged.change_dir, staged)
        if any(f.get("severity") in {"critical", "major"} for f in findings):
            raise ValueError("Rewritten tasks still have blocking shape findings")
        # The rubric describes this exact task/design set.
        await _generate_rubric(staged.change_dir, staged, state)
        names.append("rubric")
        publication = {}
        for name in names:
            filename = f"{name}.md"
            original = config.change_dir / filename
            publication[filename] = {"before": original.read_text() if original.exists() else None,
                                     "after": (staged.change_dir / filename).read_text()}
        atomic_write(config.change_dir / ".plan-publication.json", json.dumps(publication))
        _finish_plan_publication(config)
    reconcile_plan(config, state, regenerated=set(names))
    # Unknowns were an input to this bounded rewrite; a changed design must
    # regenerate them before final validation, through the normal artifact DAG.
    state.checkpoint(config.state_file_path())
    assert state.spec_gen is not None
    state.spec_gen.tasks_audit_done = True
    state.checkpoint(config.state_file_path())
    _commit_change_dir(config, config.change_dir,
                       "docs(specs): publish " + ("design audit" if design else "plan review") + " repair")
    return 2 if design else 1


def assert_plan_current(config, state: BuildState, *, archived: bool = False) -> None:
    """Check the accepted documents after implementation, without rewriting them.

    Archiving merges these requirements into the registry; that expected output
    is covered by final verification, while the original registry input remains
    in the accepted planning revision.
    """
    if state.spec_gen is None or not state.spec_gen.revision:
        raise RuntimeError("Planning revision evidence is missing; validate the plan before implementation")
    if (config.change_dir / ".plan-publication.json").exists():
        raise RuntimeError("Planning publication is incomplete")
    if not state.spec_gen.design_audit_done or not state.spec_gen.plan_review_done:
        raise RuntimeError("Required planning review evidence is missing or stale")
    current = _plan_hashes(config)
    expected = state.spec_gen.artifact_hashes
    keys = current.keys() | expected.keys()
    changed = [key for key in keys if not (archived and key.startswith("registry/"))
               and current.get(key) != expected.get(key)]
    if changed or state.spec_gen.stale_artifacts:
        state.verification = None
        raise RuntimeError("Plan changed after implementation; revalidate before building: " + ", ".join(sorted(changed)))
