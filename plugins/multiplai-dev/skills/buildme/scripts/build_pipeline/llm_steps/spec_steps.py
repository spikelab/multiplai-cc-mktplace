"""LLM step functions for spec generation and design audit.

Spec generation calls llm_call() with the appropriate prompt template; the
adversarial audits call agent_call() with a read-only tool set. Both return
structured output.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

from code_facts import CallFn

from ..prompts.spec_generation import (
    PROPOSAL_PROMPT,
    SPEC_PROMPT,
    DESIGN_PROMPT,
    TASKS_PROMPT,
)
from ..prompts.design_audit import DESIGN_AUDIT_PROMPT, TASKS_AUDIT_PROMPT
from ..prompts.explainer import EXPLAINER_PROMPT, UNKNOWNS_REGEN_PROMPT
from ..sdk import agent_call, agent_call_structured, llm_call, extract_json, LLMCallError

log = logging.getLogger(__name__)

# The audits are reviews, so they get the reviewer's tool set and nothing more:
# read-only. An auditor that could `Write` or `Edit` would fix the design it was
# asked to judge, and the one-regeneration-pass rule (see CLAUDE.md, "Audit
# feedback is one pass, decided in one place") would no longer be the only way
# an artifact changes. `test_no_review_step_can_write` asserts the list.
AUDITOR_TOOLS = ["Read", "Grep", "Glob"]

# Enough turns to open the artifacts' neighbours and grep the codebase for a
# name the design claims exists; not enough to go wandering.
AUDITOR_MAX_TURNS = 30

# The most registry text one prompt carries; whole files are dropped from the
# end, and named, to stay under it (judgment).
REGISTRY_TEXT_LIMIT = 60_000
REGISTRY_HEADING = "## Current behaviour (specs/registry)"
REGISTRY_REQUIREMENTS_RULE = (
    "These files are what the code does today. A requirement this change alters goes under "
    "`## MODIFIED Requirements` with the same `### Requirement:` name as here; a requirement it "
    "removes goes under `## REMOVED Requirements` with that name; only new requirements go under "
    "`## ADDED Requirements`."
)
REGISTRY_PROPOSAL_RULE = (
    "These capabilities are what the code does today, one file each in specs/registry/. A "
    "capability this change alters goes under `### Modified Capabilities` with exactly the name "
    "listed here, so its requirements are written against that file; a new capability goes under "
    "`### New Capabilities` with a name not listed here."
)
REGISTRY_PLAN_RULE = (
    "These files are what the code does today, for the capabilities this change's requirements "
    "name. Keep that behaviour unless a requirement above changes it."
)


async def _audit_agent_text(prompt: str, config, *, budget_label: str) -> str:
    """Run one audit as a read-only subagent, returning its raw text.

    The audits read a JSON list rather than a Pydantic model, so this is the
    `agent_call` twin of the `llm_call` they used to make: same
    return-the-text contract, same non-fatal degradation (a failed agent hands
    back its partial output, which the caller's JSON parse then rejects with
    the warning it already had).
    """
    result = await agent_call(
        prompt,
        allowed_tools=AUDITOR_TOOLS,
        model=config.model_for(budget_label),
        effort=config.effort_for(budget_label),
        max_turns=AUDITOR_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label=budget_label,
    )
    if not result.success:
        log.warning("%s subagent failed: %s", budget_label, result.error)
    return result.output or ""


async def generate_artifact(
    artifact_id: str,
    context: dict,
    config,  # BuildConfig — avoided import to prevent circular
    *,
    interview_summary: str = "",
    research: str = "",
    codebase_analysis: str = "",
    reference_docs: str = "",
    audit_findings: str = "",
) -> str:
    """Generate a single artifact's content via llm_call.

    Args:
        artifact_id: Which artifact to generate (proposal, specs, design, tasks)
        context: From change_manager.artifact_context() — template, instruction, deps
        config: BuildConfig for model, project context, tier settings
        interview_summary: For proposal generation
        research: Research findings for proposal generation
        codebase_analysis: Existing code analysis for design generation
        reference_docs: Inlined stack/framework reference docs
            (config.reference_docs_text()) for `design` and `tasks`. Inlined
            rather than read on demand because this call's system prompt
            forbids tools — the prompt is the only channel.
        audit_findings: Findings injected on a single regeneration pass —
            tasks-shape audit findings (run_tasks_audit) or prototype notes
            (prototype_steps.apply_prototype_findings). Used by `design` and
            `tasks`.

    Returns:
        Generated markdown content.
    """
    prompt = _build_prompt(
        artifact_id,
        context,
        config,
        interview_summary=interview_summary,
        research=research,
        codebase_analysis=codebase_analysis,
        reference_docs=reference_docs,
        audit_findings=audit_findings,
    )

    log.info("Generating artifact: %s", artifact_id)
    result = await llm_call(
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
        budget_label=f"spec:{artifact_id}",
    )
    log.info("Artifact %s generated (%d chars)", artifact_id, len(result))
    return result


def _build_prompt(
    artifact_id: str,
    context: dict,
    config,
    *,
    interview_summary: str = "",
    research: str = "",
    codebase_analysis: str = "",
    reference_docs: str = "",
    audit_findings: str = "",
) -> str:
    """Build the prompt string for an artifact type."""
    template = context.get("template", "")
    instruction = context.get("instruction", "")
    project_context = context.get("context", "")

    if artifact_id == "proposal":
        names = ", ".join(context.get("registry") or [])
        return PROPOSAL_PROMPT.format(
            project_context=project_context,
            interview_summary=interview_summary or "(none provided)",
            research=research or "(no research conducted)",
            instruction=instruction,
            template=template,
        ) + (f"\n\n{REGISTRY_HEADING}\n\n{REGISTRY_PROPOSAL_RULE}\n\n"
             f"{_cap(f'Capabilities in specs/registry: {names}')}\n" if names else "")
    elif artifact_id == "requirements":
        # Requirements generation needs proposal content
        proposal_content = _read_dep(context, "proposal", config)
        return SPEC_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            capability_name=context.get("capability_name", "unknown"),
            instruction=instruction,
            template=template,
        ) + _registry_section(config, context, [context.get("capability_name", "unknown")],
                              REGISTRY_REQUIREMENTS_RULE)
    elif artifact_id == "design":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        return DESIGN_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            specs_content=specs_content,
            codebase_analysis=codebase_analysis or "(new project)",
            reference_docs=reference_docs or "(none available)",
            audit_findings=audit_findings or "(none — first pass)",
            instruction=instruction,
            template=template,
        ) + _registry_section(config, context, _change_capabilities(config), REGISTRY_PLAN_RULE)
    elif artifact_id == "tasks":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        design_content = _read_dep(context, "design", config)
        return TASKS_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            specs_content=specs_content,
            design_content=design_content,
            use_cases_content=_read_dep(context, "use-cases", config),
            granularity=config.task_granularity,
            unknowns_content=context.get("unknowns_content") or "(none detected)",
            reference_docs=reference_docs or "(none available)",
            audit_findings=audit_findings or "(none — first pass)",
            instruction=instruction,
            template=template,
        ) + _registry_section(config, context, _change_capabilities(config), REGISTRY_PLAN_RULE)
    elif artifact_id == "unknowns":
        # Regeneration pass only — the first pass is one run_explainer call per
        # dependency (see spec_generator._generate_unknowns).
        return UNKNOWNS_REGEN_PROMPT.format(
            project_context=project_context,
            dependency_list=context.get("dependency_list") or "(none)",
            current_unknowns=context.get("current_unknowns") or "(empty)",
            audit_findings=audit_findings or "(none)",
            instruction=instruction,
            template=template,
        )
    else:
        raise ValueError(f"Unknown artifact type: {artifact_id}")


def _read_dep(context: dict, dep_id: str, config) -> str:
    """Read a dependency artifact's content from disk."""
    deps = context.get("dependencies", {})
    dep_file = deps.get(dep_id, "")
    if not dep_file:
        return "(not available)"
    path = config.change_dir / dep_file
    if path.exists():
        return path.read_text()
    return "(not available)"


def _read_specs(config) -> str:
    """Read all requirement files from the change directory.

    Requirements are written flat as change_dir/requirements/<capability>.md
    (see spec_generator._generate_requirements). The capability name is the
    file stem — mirrors tdd_engine.assemble_context.
    """
    req_dir = config.change_dir / "requirements"
    if not req_dir.exists():
        return "(no specs yet)"
    parts = []
    for req_file in sorted(req_dir.glob("*.md")):
        cap_name = req_file.stem
        parts.append(f"### {cap_name}\n{req_file.read_text()}")
    return "\n\n".join(parts) if parts else "(no specs yet)"


def _change_capabilities(config) -> list[str]:
    """The stems of the change's own `requirements/*.md`, sorted."""
    return sorted(p.stem for p in (config.change_dir / "requirements").glob("*.md"))


_REGISTRY_REQUIREMENT = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$", re.MULTILINE)


def _read_registry(config, context: dict, names: list[str]) -> str:
    """The text of `specs/registry/<name>.md` for each of *names* that exists, each
    under `### <name> (specs/registry/<name>.md)`, then one line naming every other
    registry capability. Over `REGISTRY_TEXT_LIMIT` characters, whole files are
    dropped from the end and named instead, with their requirement names; names
    still over it are cut (`_cap`)."""
    registry = config.specs_dir / "registry"
    shown = [n for n in names if (registry / f"{n}.md").is_file()]
    others = [n for n in context.get("registry") or [] if n not in shown]
    tail = f"Other capabilities in specs/registry: {', '.join(others)}" if others else ""
    texts = {n: (registry / f"{n}.md").read_text() for n in shown}
    parts = [f"### {n} (specs/registry/{n}.md)\n{texts[n]}" for n in shown]
    dropped: list[str] = []

    def named(n: str) -> str:
        """A left-out file, with its requirement names, which a MODIFIED or REMOVED
        requirement must reuse."""
        reqs = _REGISTRY_REQUIREMENT.findall(texts[n])
        return f"{n} (specs/registry/{n}.md; requirements: {', '.join(reqs)})" if reqs \
            else f"{n} (specs/registry/{n}.md)"

    def render() -> str:
        left_out = (f"Left out to stay under {REGISTRY_TEXT_LIMIT:,} characters: "
                    + ", ".join(named(n) for n in dropped)) if dropped else ""
        return "\n\n".join(p for p in (*parts, left_out, tail) if p)

    while parts and len(render()) > REGISTRY_TEXT_LIMIT:
        parts.pop()
        dropped.insert(0, shown[len(parts)])
    return _cap(render())


def _cap(text: str) -> str:
    """*text* within `REGISTRY_TEXT_LIMIT` characters: cut at the end, saying so,
    when the names alone (left-out files' requirements, other capabilities) are
    over it."""
    if len(text) <= REGISTRY_TEXT_LIMIT:
        return text
    note = f"\n(cut at {REGISTRY_TEXT_LIMIT:,} characters)"
    return text[:REGISTRY_TEXT_LIMIT - len(note)] + note


def _registry_section(config, context: dict, names: list[str], rule: str) -> str:
    """The `## Current behaviour (specs/registry)` section appended to a prompt,
    or "" when the registry is empty, so the prompt is then today's byte for byte."""
    if not context.get("registry"):
        return ""
    return f"\n\n{REGISTRY_HEADING}\n\n{rule}\n\n{_read_registry(config, context, names)}\n"


async def run_explainer(dep, config, usage_context: str = "") -> str:
    """Write one dependency's section of `unknowns.md` (B1 explainer gate).

    ``dep`` is a ``dependencies.NewDependency``. Research tools are allowed —
    the point of the explainer is to find the documented edge cases (the
    Whisper-silence class) rather than recall them. Returns the markdown
    section; the caller concatenates one per dependency.
    """
    prompt = EXPLAINER_PROMPT.format(
        dep_name=dep.name,
        mentioned_in=", ".join(getattr(dep, "mentioned_in", []) or []) or "(unrecorded)",
        evidence=getattr(dep, "evidence", "") or "(no manifest evidence recorded)",
        project_context=getattr(config, "project_description", "") or "(none)",
        usage_context=usage_context or "(see the design's Decisions section)",
    )

    log.info("Running explainer for dependency: %s", dep.name)
    return await llm_call(
        prompt,
        model=config.model_for("explainer"),
        effort=config.effort_for("explainer"),
        allowed_tools=["WebSearch", "WebFetch", "Read", "Glob", "Grep"],
        max_turns=12,
    )


async def run_design_audit(change_dir: Path, config) -> list[dict]:
    """Run adversarial design audit on generated artifacts.

    Runs as a read-only subagent, so the auditor can open the codebase the
    design claims to change instead of judging four documents in isolation.

    Returns list of gap dicts with category, severity, description, suggestion.
    """
    proposal = _read_file(change_dir / "proposal.md")
    design = _read_file(change_dir / "design.md")
    tasks = _read_file(change_dir / "tasks.md")

    specs_parts = []
    req_dir = change_dir / "requirements"
    if req_dir.exists():
        for req_file in sorted(req_dir.glob("*.md")):
            cap_name = req_file.stem
            specs_parts.append(f"### {cap_name}\n{req_file.read_text()}")
    specs_content = "\n\n".join(specs_parts) if specs_parts else "(no specs)"

    # Detect change type for type-specific audit questions
    from ..rubric import detect_change_type
    change_type = detect_change_type(change_dir)

    prompt = DESIGN_AUDIT_PROMPT.format(
        proposal_content=proposal,
        specs_content=specs_content,
        design_content=design,
        tasks_content=tasks,
        change_type=change_type,
    )

    log.info("Running design audit on %s", change_dir.name)
    raw = await _audit_agent_text(prompt, config, budget_label="design_audit")

    try:
        gaps = extract_json(raw)
        if isinstance(gaps, list):
            log.info("Design audit found %d gaps", len(gaps))
            return gaps
        return []
    except (ValueError, json.JSONDecodeError):
        log.warning("Design audit returned non-JSON response")
        return []


async def run_tasks_audit(change_dir: Path, config) -> list[dict]:
    """Audit tasks.md for horizontal (layer-by-layer) decomposition.

    Checks that each block is a vertical slice — end-to-end exercisable when it
    completes — rather than a layer (schema block, API block, UI block, final
    wiring block). Runs as a read-only subagent. Returns a list of finding
    dicts with category, severity, description, suggestion; empty list when the
    shape is clean.
    """
    tasks = _read_file(change_dir / "tasks.md")
    design = _read_file(change_dir / "design.md")

    # Specs feed the traceability check: every WHEN/THEN must map to a block.
    specs_parts = []
    req_dir = change_dir / "requirements"
    if req_dir.exists():
        for req_file in sorted(req_dir.glob("*.md")):
            specs_parts.append(f"### {req_file.name}\n{req_file.read_text()}")
    specs_content = "\n\n".join(specs_parts) or "(no requirements files)"

    prompt = TASKS_AUDIT_PROMPT.format(
        design_content=design,
        specs_content=specs_content,
        tasks_content=tasks,
    )

    log.info("Running tasks shape audit on %s", change_dir.name)
    raw = await _audit_agent_text(prompt, config, budget_label="tasks_audit")

    try:
        findings = extract_json(raw)
        if isinstance(findings, list):
            dict_findings = [f for f in findings if isinstance(f, dict)]
            if len(dict_findings) != len(findings):
                log.warning(
                    "Tasks shape audit dropped %d non-dict findings",
                    len(findings) - len(dict_findings),
                )
            log.info("Tasks shape audit found %d findings", len(dict_findings))
            return dict_findings
        # A non-list JSON payload (e.g. the model wrapped the findings in an
        # object) would previously pass silently as "no findings" — warn so
        # the drop is visible in the log.
        log.warning(
            "Tasks shape audit returned JSON %s instead of a list — "
            "treating as no findings",
            type(findings).__name__,
        )
        return []
    except (ValueError, json.JSONDecodeError):
        log.warning("Tasks shape audit returned non-JSON response")
        return []


async def _codebase_analysis_call(prompt: str, schema, *, config, budget_label: str = "", **kwargs):
    """buildme's call function for `code_facts`: one tool-using agent with a
    structured answer, re-run whole on a failure (`agent_call_structured`), all
    spend under `codebase_analysis` whatever `code_facts` names the task."""
    return await agent_call_structured(
        prompt, schema, model=config.model_for("codebase_analysis"), effort=config.effort_for("codebase_analysis"),
        budget_label="codebase_analysis", **kwargs,
    )


async def run_codebase_analysis(project_dir: Path, config, *, call: CallFn | None = None) -> str:
    """Explain the existing codebase as checked, cited facts (`code_facts`).

    Wired as BuildPhase.CODEBASE_ANALYSIS (orchestrator, between RESEARCH and
    SPEC_GENERATION); the report is written to
    `specs/changes/<name>/codebase-analysis.md` and inlined into the design
    prompt so the design extends the repo that exists.

    One `code_facts` agent per slice of the project's source files at HEAD,
    with the conventions section on, at most `codebase_analysis_max_units`
    slices (`code_analysis.limit_scope` keeps the files the change's documents
    name first, then the most imported, and lists the rest under "Not
    analysed"). A fact is kept only when one of its citations is found at
    HEAD. The surviving facts are rendered under Architecture, Patterns,
    Dependencies and Conventions, each with its `path:line` links, and written
    to `facts.json` beside the report, in review's `facts.v1` format.

    A failed slice degrades to a labelled "(analysis failed: ...)" line rather
    than taking the others down: a partial picture of the codebase is worth
    more to the design than none. A project with no commit
    gets `code_analysis.no_commit_report` instead: labelled as not analysed,
    with its source files listed. *call* replaces the model call (tests).
    """
    if not project_dir.exists():
        return "(new project — no existing code)"
    from .. import code_analysis

    md_path = config.codebase_analysis_path
    call = call or functools.partial(_codebase_analysis_call, config=config)
    analysis = await code_analysis.analyse(
        project_dir, call=call, max_units=config.codebase_analysis_max_units,
        name=config.project_name or project_dir.name, change_dir=config.change_dir,
    )
    if analysis is None:
        return code_analysis.no_commit_report(project_dir)
    log.info("Codebase analysis: %d facts kept, %d dropped, %d slice(s) failed, %d file(s) not analysed",
             len(analysis.gated.facts), len(analysis.gated.dropped), len(analysis.failed),
             len(analysis.not_analysed))
    code_analysis.write_facts_file(analysis, md_path.parent)
    return code_analysis.render_markdown(analysis, project_dir, md_path.parent)


def _read_file(path: Path) -> str:
    """Read a file, returning placeholder if missing."""
    if path.exists():
        return path.read_text()
    return "(not available)"
