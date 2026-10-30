"""LLM step functions for spec generation, the explainers and the codebase
analysis.

The design and tasks writer calls agent_call() with a read-only tool set;
the unknowns regeneration pass calls llm_call() with no tools. The plan
check that reviews what they write is `plan_review_steps`.
"""

from __future__ import annotations

import functools
import logging
import re
from pathlib import Path

from code_facts import CallFn
from multiplai_core.specs import read_change_meta

from ..change_manager import (
    requirement_texts,
    spec_issue_ref,
)
from ..prompts.explainer import EXPLAINER_PROMPT, UNKNOWNS_REGEN_PROMPT
from ..prompts.spec_generation import DESIGN_PROMPT, TASKS_PROMPT
from ..sdk import (
    LLMCallError,
    agent_call,
    agent_call_structured,
    llm_call,
)

log = logging.getLogger(__name__)

# A read-only agent's tool set (the findings stage's self-check,
# `findings.code`): the reviewer's, and nothing more. An agent that could
# `Write` or `Edit` would fix what it was asked to judge.
# `test_no_review_step_can_write` asserts the list.
AUDITOR_TOOLS = ["Read", "Grep", "Glob"]

# Enough turns to open a file's neighbours and grep the codebase for a name;
# not enough to go wandering.
AUDITOR_MAX_TURNS = 30

# The design and tasks writer reads what it describes, with the auditors' tool
# set: read-only, so the document it returns is the only thing it changes. More
# turns than an audit because it opens every file the plan and specs name
# before writing about them (judgment).
WRITER_MAX_TURNS = 40

# `agent_call` takes no system prompt, so the writer's instructions open its
# prompt.
WRITER_INSTRUCTIONS = """\
You are a technical specification writer. You hold Read, Grep and Glob in the
project's repository, and nothing that can change it.

- Before describing a file that plan.md or the specs below name, open it with
  Read.
- Check every claim you make about existing code (a name, an endpoint, a
  table) with Grep or Read before you write it down.
- Your final message is the document only, in markdown, starting at its first
  `#` heading. Put nothing before that heading.

"""

# The most registry text one prompt carries; whole files are dropped from the
# end, and named, to stay under it (judgment).
REGISTRY_TEXT_LIMIT = 60_000
REGISTRY_HEADING = "## Current behaviour (specs/registry)"
REGISTRY_PLAN_RULE = (
    "These files are what the code does today, for the capabilities this change's requirements "
    "name. Keep that behaviour unless a requirement above changes it."
)

DEPENDENCY_SPECS_HEADING = "## Requirements of the components this one depends on"
DEPENDENCY_SPECS_RULE = (
    "These are the specs.md of the components this one depends on: the `depends_on` "
    "in its .change.yaml, and theirs in turn. Those components build these "
    "requirements; this one uses what they provide as stated here, and does not "
    "build it again."
)


async def generate_artifact(
    artifact_id: str,
    context: dict,
    config,  # BuildConfig — avoided import to prevent circular
    *,
    research: str = "",
    codebase_analysis: str = "",
    reference_docs: str = "",
    audit_findings: str = "",
) -> str:
    """Generate a single artifact's content.

    `design` and `tasks` run as a read-only subagent (`agent_call` with
    AUDITOR_TOOLS in the project dir), so the writer opens the code it
    describes. The unknowns regeneration pass stays a no-tools `llm_call`.

    Args:
        artifact_id: Which artifact to generate (design, tasks, or the
            unknowns regeneration pass)
        context: From change_manager.artifact_context() — template, instruction, deps
        config: BuildConfig for model, project context, tier settings
        research: Research findings (research.md) for design generation
        codebase_analysis: Existing code analysis for design generation
        reference_docs: Inlined stack/framework reference docs
            (config.reference_docs_text()) for `design` and `tasks`. Inlined
            rather than read on demand because the writer is not handed the
            docs' paths — the prompt is the only channel.
        audit_findings: Findings injected on a single regeneration pass:
            the plan check's findings. Used by `design` and `tasks`.

    Returns:
        Generated markdown content.

    Raises:
        LLMCallError: the writer agent failed, or its output has no line
            starting with `#`.
    """
    prompt = _build_prompt(
        artifact_id,
        context,
        config,
        research=research,
        codebase_analysis=codebase_analysis,
        reference_docs=reference_docs,
        audit_findings=audit_findings,
    )

    log.info("Generating artifact: %s", artifact_id)
    if artifact_id in ("design", "tasks"):
        agent = await agent_call(
            WRITER_INSTRUCTIONS + prompt,
            allowed_tools=AUDITOR_TOOLS,
            model=config.model_for("spec_artifact"),
            effort=config.effort_for("spec_artifact"),
            max_turns=WRITER_MAX_TURNS,
            cwd=str(config.project_dir),
            budget_label=f"spec:{artifact_id}",
        )
        if not agent.success:
            raise LLMCallError(
                f"{artifact_id} writer failed: {agent.error or agent.output}"
            )
        result = _from_first_heading(agent.output or "", artifact_id)
        log.info("Artifact %s generated (%d chars)", artifact_id, len(result))
        return result
    result = await llm_call(
        prompt,
        model=config.model_for("spec_artifact"),
        effort=config.effort_for("spec_artifact"),
        system_prompt=(
            "You are a technical specification generator. Your ONLY job is to "
            "generate the requested document content based on the context provided "
            "in the prompt. Output the document directly. You have no tools: do "
            "not explore code or request more information. All the context you "
            "need is in the prompt. Output ONLY markdown content."
        ),
        budget_label=f"spec:{artifact_id}",
    )
    log.info("Artifact %s generated (%d chars)", artifact_id, len(result))
    return result


def _from_first_heading(text: str, artifact_id: str) -> str:
    """The writer's document: its text from the first line starting with `#`.

    An agent's final message can open with a sentence about what it read;
    that sentence is not part of the document. No such line means no
    document."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("#"):
            return "".join(lines[i:])
    raise LLMCallError(f"{artifact_id} writer returned no line starting with '#'")


def _build_prompt(
    artifact_id: str,
    context: dict,
    config,
    *,
    research: str = "",
    codebase_analysis: str = "",
    reference_docs: str = "",
    audit_findings: str = "",
) -> str:
    """Build the prompt string for an artifact type."""
    template = context.get("template", "")
    instruction = context.get("instruction", "")
    project_context = context.get("context", "")

    if artifact_id == "design":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        return (
            DESIGN_PROMPT.format(
                project_context=project_context,
                proposal_content=proposal_content,
                plan_content=_read_plan(config),
                specs_content=specs_content,
                spec_ref=spec_issue_ref(config.change_dir),
                codebase_analysis=codebase_analysis or "(new project)",
                research=research or "(no research conducted)",
                reference_docs=reference_docs or "(none available)",
                audit_findings=audit_findings or "(none — first pass)",
                instruction=instruction,
                template=template,
            )
            + _registry_section(
                config, context, _change_capabilities(config), REGISTRY_PLAN_RULE
            )
            + dependency_specs_section(config.change_dir, config.specs_dir)
        )
    elif artifact_id == "tasks":
        proposal_content = _read_dep(context, "proposal", config)
        specs_content = _read_specs(config)
        design_content = _read_dep(context, "design", config)
        return (
            TASKS_PROMPT.format(
                project_context=project_context,
                proposal_content=proposal_content,
                plan_content=_read_plan(config),
                specs_content=specs_content,
                design_content=design_content,
                use_cases_content=_read_dep(context, "use-cases", config),
                granularity=config.task_granularity,
                unknowns_content=context.get("unknowns_content") or "(none detected)",
                reference_docs=reference_docs or "(none available)",
                audit_findings=audit_findings or "(none — first pass)",
                instruction=instruction,
                template=template,
            )
            + _registry_section(
                config, context, _change_capabilities(config), REGISTRY_PLAN_RULE
            )
            + dependency_specs_section(config.change_dir, config.specs_dir)
        )
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


def _read_plan(config) -> str:
    """The `plan.md` pm-spec wrote for the component (or for a change with one
    component, in the change dir): its scope, what is out of scope and what
    done means."""
    return _plan_at(config.change_dir)


def _plan_at(change_dir: Path) -> str:
    path = Path(change_dir) / "plan.md"
    return path.read_text() if path.is_file() else "(no plan.md)"


def _read_specs(config) -> str:
    """Read the change's requirements, one section per capability.

    pm-spec writes them as `# Capability: <name>` sections of
    change_dir/specs.md, or, in an older change, as flat files,
    change_dir/requirements/<capability>.md (`requirement_texts`) — mirrors
    tdd_engine.assemble_context.
    """
    parts = [
        f"### {cap_name}\n{text}"
        for cap_name, text in requirement_texts(config.change_dir).items()
    ]
    return "\n\n".join(parts) if parts else "(no specs yet)"


def _change_capabilities(config) -> list[str]:
    """The change's own capability names (`requirement_texts`), sorted."""
    return sorted(requirement_texts(config.change_dir))


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
    tail = (
        f"Other capabilities in specs/registry: {', '.join(others)}" if others else ""
    )
    texts = {n: (registry / f"{n}.md").read_text() for n in shown}
    parts = [f"### {n} (specs/registry/{n}.md)\n{texts[n]}" for n in shown]
    dropped: list[str] = []

    def named(n: str) -> str:
        """A left-out file, with its requirement names, which a MODIFIED or REMOVED
        requirement must reuse."""
        reqs = _REGISTRY_REQUIREMENT.findall(texts[n])
        return (
            f"{n} (specs/registry/{n}.md; requirements: {', '.join(reqs)})"
            if reqs
            else f"{n} (specs/registry/{n}.md)"
        )

    def render() -> str:
        left_out = (
            (
                f"Left out to stay under {REGISTRY_TEXT_LIMIT:,} characters: "
                + ", ".join(named(n) for n in dropped)
            )
            if dropped
            else ""
        )
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
    return text[: REGISTRY_TEXT_LIMIT - len(note)] + note


def _registry_section(config, context: dict, names: list[str], rule: str) -> str:
    """The `## Current behaviour (specs/registry)` section appended to a prompt,
    or "" when the registry is empty, so the prompt is then today's byte for byte."""
    if not context.get("registry"):
        return ""
    return f"\n\n{REGISTRY_HEADING}\n\n{rule}\n\n{_read_registry(config, context, names)}\n"


def _dependency_dirs(change_dir: Path) -> list[Path]:
    """The change dirs of every component *change_dir* depends on: its
    `.change.yaml` `depends_on`, then theirs, each once, nearest first. A
    dependency is a sibling dir (`components/<id>` beside a component,
    `changes/<name>` beside a change). One whose dir is gone, or whose
    `.change.yaml` cannot be read, is skipped with a warning."""
    found: list[Path] = []
    seen = {Path(change_dir).name}
    queue = [Path(change_dir)]
    while queue:
        current = queue.pop(0)
        try:
            deps = read_change_meta(current).get("depends_on") or []
        except (OSError, UnicodeError, ValueError) as e:
            log.warning("Cannot read the dependencies of %s: %s", current, e)
            continue
        if not isinstance(deps, list):
            log.warning("depends_on in %s/.change.yaml is not a list", current)
            continue
        for dep in deps:
            if not isinstance(dep, str) or dep in seen:
                continue
            seen.add(dep)
            dep_dir = Path(change_dir).parent / dep
            if not dep_dir.is_dir():
                log.warning("%s depends on %s, which has no dir", current.name, dep)
                continue
            found.append(dep_dir)
            queue.append(dep_dir)
    return found


def dependency_spec_files(change_dir: Path) -> list[Path]:
    """The `specs.md` of each dir `_dependency_dirs` finds that has one, nearest
    first: what `dependency_specs_section` reads and `spec_generator._plan_hashes`
    hashes, so the prompt and the plan revision never disagree."""
    files = [d / "specs.md" for d in _dependency_dirs(change_dir)]
    return [f for f in files if f.is_file()]


def dependency_specs_section(change_dir: Path, specs_dir: Path) -> str:
    """The `## Requirements of the components this one depends on` section
    appended to the design, tasks and plan-check prompts: the `specs.md` of each
    component in the transitive `depends_on` (`_dependency_dirs`), each under
    `### <id> (<path>)`. Over `REGISTRY_TEXT_LIMIT` characters, whole files are
    dropped from the end and named instead, with their requirement names; names
    still over it are cut (`_cap`). "" when no dependency has a `specs.md`, so a
    component with no dependency gets today's prompt byte for byte."""
    files = dependency_spec_files(change_dir)
    if not files:
        return ""
    base = Path(specs_dir).parent

    def label(f: Path) -> str:
        try:
            return f"{f.parent.name} ({f.relative_to(base)})"
        except ValueError:
            return f"{f.parent.name} ({f})"

    texts = [f.read_text() for f in files]
    parts = [f"### {label(f)}\n{text}" for f, text in zip(files, texts)]
    dropped: list[str] = []

    def named(i: int) -> str:
        reqs = _REGISTRY_REQUIREMENT.findall(texts[i])
        return (
            f"{label(files[i])[:-1]}; requirements: {', '.join(reqs)})"
            if reqs
            else label(files[i])
        )

    def render() -> str:
        left_out = (
            f"Left out to stay under {REGISTRY_TEXT_LIMIT:,} characters: "
            + ", ".join(dropped)
            if dropped
            else ""
        )
        return "\n\n".join(p for p in (*parts, left_out) if p)

    while parts and len(render()) > REGISTRY_TEXT_LIMIT:
        parts.pop()
        dropped.insert(0, named(len(parts)))
    return (
        f"\n\n{DEPENDENCY_SPECS_HEADING}\n\n{DEPENDENCY_SPECS_RULE}\n\n"
        f"{_cap(render())}\n"
    )


async def run_explainer(dep, config, usage_context: str = "") -> str:
    """Write one dependency's section of `unknowns.md` (B1 explainer gate).

    ``dep`` is a ``dependencies.NewDependency``. Research tools are allowed —
    the point of the explainer is to find the documented edge cases (the
    Whisper-silence class) rather than recall them. Returns the markdown
    section; the caller concatenates one per dependency.
    """
    prompt = EXPLAINER_PROMPT.format(
        dep_name=dep.name,
        mentioned_in=", ".join(getattr(dep, "mentioned_in", []) or [])
        or "(unrecorded)",
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
        budget_label="explainer",
    )


async def _codebase_analysis_call(
    prompt: str, schema, *, config, budget_label: str = "", **kwargs
):
    """buildme's call function for `code_facts`: one tool-using agent with a
    structured answer, re-run whole on a failure (`agent_call_structured`), all
    spend under `codebase_analysis` whatever `code_facts` names the task."""
    return await agent_call_structured(
        prompt,
        schema,
        model=config.model_for("codebase_analysis"),
        effort=config.effort_for("codebase_analysis"),
        budget_label="codebase_analysis",
        **kwargs,
    )


async def run_codebase_analysis(
    project_dir: Path, config, *, call: CallFn | None = None
) -> str:
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
        project_dir,
        call=call,
        max_units=config.codebase_analysis_max_units,
        name=config.project_name or project_dir.name,
        change_dir=config.change_dir,
    )
    if analysis is None:
        return code_analysis.no_commit_report(project_dir)
    log.info(
        "Codebase analysis: %d facts kept, %d dropped, %d slice(s) failed, %d file(s) not analysed",
        len(analysis.gated.facts),
        len(analysis.gated.dropped),
        len(analysis.failed),
        len(analysis.not_analysed),
    )
    code_analysis.write_facts_file(analysis, md_path.parent)
    return code_analysis.render_markdown(analysis, project_dir, md_path.parent)
