"""LLM step functions for spec generation and design audit.

Each function calls llm_call() with the appropriate prompt template
and returns structured output.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..prompts.spec_generation import (
    PROPOSAL_PROMPT,
    SPEC_PROMPT,
    DESIGN_PROMPT,
    TASKS_PROMPT,
)
from ..prompts.design_audit import DESIGN_AUDIT_PROMPT
from ..sdk import llm_call, extract_json, LLMCallError

log = logging.getLogger(__name__)


async def generate_artifact(
    artifact_id: str,
    context: dict,
    config,  # BuildConfig — avoided import to prevent circular
    *,
    interview_summary: str = "",
    research: str = "",
    codebase_analysis: str = "",
) -> str:
    """Generate a single artifact's content via llm_call.

    Args:
        artifact_id: Which artifact to generate (proposal, specs, design, tasks)
        context: From change_manager.artifact_context() — template, instruction, deps
        config: BuildConfig for model, project context, tier settings
        interview_summary: For proposal generation
        research: Research findings for proposal generation
        codebase_analysis: Existing code analysis for design generation

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
    )

    log.info("Generating artifact: %s", artifact_id)
    result = await llm_call(
        prompt,
        model=config.model,
        system_prompt=(
            "You are a technical specification generator. Your ONLY job is to "
            "generate the requested document content based on the context provided "
            "in the prompt. Output the document directly — do NOT attempt to use "
            "tools, explore code, or request more information. All the context you "
            "need is in the prompt. Output ONLY markdown content."
        ),
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
) -> str:
    """Build the prompt string for an artifact type."""
    template = context.get("template", "")
    instruction = context.get("instruction", "")
    project_context = context.get("context", "")

    if artifact_id == "proposal":
        return PROPOSAL_PROMPT.format(
            project_context=project_context,
            interview_summary=interview_summary or "(none provided)",
            research=research or "(no research conducted)",
            instruction=instruction,
            template=template,
        )
    elif artifact_id in ("specs", "requirements"):
        # Specs/requirements generation needs proposal content
        proposal_content = _read_dep(context, "proposal", config)
        return SPEC_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            capability_name=context.get("capability_name", "unknown"),
            instruction=instruction,
            template=template,
        )
    elif artifact_id == "design":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        return DESIGN_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            specs_content=specs_content,
            codebase_analysis=codebase_analysis or "(new project)",
            instruction=instruction,
            template=template,
        )
    elif artifact_id == "tasks":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        design_content = _read_dep(context, "design", config)
        return TASKS_PROMPT.format(
            project_context=project_context,
            proposal_content=proposal_content,
            specs_content=specs_content,
            design_content=design_content,
            granularity=config.task_granularity,
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
    """Read all spec files from the change directory."""
    specs_dir = config.change_dir / "specs"
    if not specs_dir.exists():
        return "(no specs yet)"
    parts = []
    for spec_file in sorted(specs_dir.glob("*/spec.md")):
        cap_name = spec_file.parent.name
        parts.append(f"### {cap_name}\n{spec_file.read_text()}")
    return "\n\n".join(parts) if parts else "(no specs yet)"


async def run_design_audit(change_dir: Path, config) -> list[dict]:
    """Run adversarial design audit on generated artifacts.

    Returns list of gap dicts with category, severity, description, suggestion.
    """
    proposal = _read_file(change_dir / "proposal.md")
    design = _read_file(change_dir / "design.md")
    tasks = _read_file(change_dir / "tasks.md")

    specs_parts = []
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec_file in sorted(specs_dir.glob("*/spec.md")):
            cap_name = spec_file.parent.name
            specs_parts.append(f"### {cap_name}\n{spec_file.read_text()}")
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
    raw = await llm_call(prompt, model=config.model)

    try:
        gaps = extract_json(raw)
        if isinstance(gaps, list):
            log.info("Design audit found %d gaps", len(gaps))
            return gaps
        return []
    except (ValueError, json.JSONDecodeError):
        log.warning("Design audit returned non-JSON response")
        return []


# NOTE: not currently wired into the pipeline. No caller runs the parallel
# codebase-analysis agents; kept for a future spec-grounding step.
async def run_codebase_analysis(project_dir: Path, config) -> str:
    """Spawn parallel explore agents to analyze the existing codebase.

    Returns a combined analysis string covering architecture, patterns, and conventions.
    """
    if not project_dir.exists():
        return "(new project — no existing code)"

    prompts = [
        (
            "Analyze the directory structure, module organization, and key entry points. "
            f"Project root: {project_dir}\n"
            "List: top-level modules, their responsibilities, main entry points. Be concise."
        ),
        (
            "Analyze coding patterns, naming conventions, and error handling patterns. "
            f"Project root: {project_dir}\n"
            "List: import style, class vs function patterns, error handling approach, test patterns. Be concise."
        ),
        (
            "Analyze dependencies, configuration, and integration points. "
            f"Project root: {project_dir}\n"
            "List: key dependencies, config loading pattern, external service integrations. Be concise."
        ),
    ]

    log.info("Running codebase analysis with 3 parallel agents")
    tasks = [
        llm_call(
            p,
            model=config.model,
            allowed_tools=["Read", "Glob", "Grep"],
            max_turns=5,
        )
        for p in prompts
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    parts = []
    labels = ["Architecture", "Patterns", "Dependencies"]
    for label, result in zip(labels, results):
        if isinstance(result, Exception):
            log.warning("Codebase analysis agent failed: %s", result)
            parts.append(f"## {label}\n(analysis failed: {result})")
        else:
            parts.append(f"## {label}\n{result}")

    return "\n\n".join(parts)


def _read_file(path: Path) -> str:
    """Read a file, returning placeholder if missing."""
    if path.exists():
        return path.read_text()
    return "(not available)"
