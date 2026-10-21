"""Proposal, use cases, then requirements one capability at a time (row S2).

Copied from buildme's `spec_generator.py` (`_generate_single_artifact`,
`_generate_use_cases`, `_generate_requirements`, and `reconcile_plan`'s move of
superseded requirements), then adapted.

Each artifact has a record in the state: the hash of its inputs and the hash
of the text it wrote. The record is saved with `output: None` before the call
and completed after the file is written, so:

- a finished artifact whose inputs are unchanged is kept, whatever its text
  (an edit by the user stays);
- one with no file, or whose record was started and not finished, is
  generated (again);
- a file the run never recorded (written by a person, or by an earlier tool)
  is adopted as it is;
- the proposal's inputs are the intake files and the interview; the use cases'
  and each requirement's inputs include the proposal's text, so a changed
  proposal regenerates them.

A requirements file whose capability the proposal no longer names is moved to
`superseded-requirements/<stem>-<sha256>.md`.

With a PRD, the proposal must name, under `## Sources`, the PRD heading behind
each capability; a proposal that does not is asked for once more with the
problems listed, and refused after that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from multiplai_core.specs import expected_capabilities, valid_requirement, valid_requirement_text

from . import intake, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts import spec as P
from .publish import atomic_write, supersede
from .run import Run
from .state import ArtifactRecord

log = logging.getLogger(__name__)

REGISTRY_TEXT_LIMIT = 60_000


def sha(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def registry_dir(run: Run) -> Path:
    return run.config.specs_dir / "registry"


def registry_names(run: Run) -> list[str]:
    return sorted(p.stem for p in registry_dir(run).glob("*.md"))


def intake_hash(run: Run) -> str:
    """The proposal's inputs: the intake files, the interview, the project context and the registry's names."""
    change_dir = run.config.change_dir
    parts = {name: _read(change_dir / name) for name in ("prd.md", "ticket.md", "plan.md", "interview.md")}
    parts["context"] = run.config.project_context
    parts["registry"] = ",".join(registry_names(run))
    parts["ticket_children"] = ",".join(intake.ticket_refs(change_dir)["children"])
    return sha(json.dumps(parts, sort_keys=True))


def proposal_text(run: Run) -> str:
    return _read(run.config.change_dir / "proposal.md")


def inputs_for(run: Run, key: str) -> str:
    change_dir = run.config.change_dir
    if key == "proposal":
        return intake_hash(run)
    if key == "use-cases":
        return sha(json.dumps({"proposal": proposal_text(run), "interview": _read(change_dir / "interview.md"),
                               "context": run.config.project_context}, sort_keys=True))
    cap = key.split("/", 1)[1]
    return sha(json.dumps({"proposal": proposal_text(run), "context": run.config.project_context,
                           "registry": _read(registry_dir(run) / f"{cap}.md")}, sort_keys=True))


def artifact_path(run: Run, key: str) -> Path:
    if key == "proposal":
        return run.config.change_dir / "proposal.md"
    if key == "use-cases":
        return run.config.change_dir / "use-cases.md"
    return run.config.change_dir / f"{key}.md"


def requirement_keys(run: Run) -> list[str]:
    return [f"requirements/{cap}" for cap in expected_capabilities(proposal_text(run))]


def record_current(run: Run) -> None:
    """Record every spec file on disk as the run's own, at its current inputs.
    Called after a rewrite publishes, so the files it wrote are kept."""
    for key in ["proposal", "use-cases", *requirement_keys(run)]:
        path = artifact_path(run, key)
        if path.is_file():
            run.state.artifacts[key] = ArtifactRecord(inputs=inputs_for(run, key), output=sha(path.read_text()))
    for key in [k for k in run.state.artifacts if k.startswith("requirements/")]:
        if not artifact_path(run, key).is_file():
            del run.state.artifacts[key]


def _usable(run: Run, key: str) -> bool:
    path = artifact_path(run, key)
    if key.startswith("requirements/"):
        return valid_requirement(path)
    return path.is_file() and bool(path.read_text().strip())


def _decide(run: Run, key: str) -> str:
    """`keep`, `adopt` or `generate` for one artifact."""
    record = run.state.artifacts.get(key)
    usable = _usable(run, key)
    if record is None:
        return "adopt" if usable else "generate"
    if record.output is None or not usable:
        return "generate"
    return "keep" if record.inputs == inputs_for(run, key) else "generate"


# --- the PRD citations and the ticket refs in `## Sources` ---------------------

_SOURCES_RE = re.compile(r"^##\s+Sources\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)
_SOURCE_LINE_RE = re.compile(r"^-\s+`([a-z0-9]+(?:-[a-z0-9]+)*)`\s*:\s*(.+?)\s*$", re.M)
_PRD_RE = re.compile(r"^PRD\s*(?:§\s*)?[\"“']?(.+?)[\"”']?$", re.I)
_TICKET_RE = re.compile(r"^ticket\s+(\S+)$", re.I)


def sources(proposal: str) -> dict[str, dict[str, list[str]]]:
    """`{capability: {"prd": [headings], "tickets": [refs]}}` from `## Sources`."""
    section = _SOURCES_RE.search(proposal)
    out: dict[str, dict[str, list[str]]] = {}
    if not section:
        return out
    for cap, rest in _SOURCE_LINE_RE.findall(section.group(1)):
        entry = out.setdefault(cap, {"prd": [], "tickets": []})
        for part in (p.strip() for p in rest.split(";")):
            if m := _PRD_RE.match(part):
                entry["prd"].append(m.group(1).strip())
            elif m := _TICKET_RE.match(part):
                entry["tickets"].append(m.group(1).strip().rstrip(".,"))
    return out


def _norm(heading: str) -> str:
    return " ".join(heading.replace("*", "").replace("`", "").split()).casefold()


def proposal_errors(run: Run, text: str) -> list[str]:
    """Why a proposal cannot be kept: no capability, or (with a PRD) a capability
    with no `## Sources` line naming a heading the PRD has."""
    caps = expected_capabilities(text)
    if not caps:
        return ["the proposal names no capability under `## Capabilities` (a list item `- `name``)"]
    prd = intake.prd_text(run.config.change_dir)
    if not prd:
        return []
    headings = {_norm(h) for h in intake.prd_headings(prd)}
    cited = sources(text)
    errors = []
    for cap in caps:
        named = cited.get(cap, {}).get("prd", [])
        if not named:
            errors.append(f"capability `{cap}` has no `## Sources` line naming the PRD section it comes from")
        elif not any(_norm(h) in headings for h in named):
            errors.append(f"capability `{cap}` names PRD section(s) {named}, which are not headings of the PRD")
    return errors


# --- prompts --------------------------------------------------------------------

def prd_section(run: Run) -> str:
    prd = intake.prd_text(run.config.change_dir)
    return f"\n## PRD (prd.md)\n{intake.fenced(prd, 'prd.md')}\n" if prd else ""


def source_rules(run: Run) -> str:
    rules = []
    if intake.prd_text(run.config.change_dir):
        rules.append(P.PRD_RULE)
    children = intake.ticket_refs(run.config.change_dir)["children"]
    if children:
        rules.append(P.TICKET_CHILDREN_RULE.format(children=", ".join(children)))
    return ("\n" + "\n\n".join(rules) + "\n") if rules else ""


def proposal_template(run: Run) -> str:
    forms = []
    if intake.prd_text(run.config.change_dir):
        forms.append('PRD "<heading>"')
    if intake.ticket_refs(run.config.change_dir)["children"]:
        forms.append("ticket <ref>")
    if not forms:
        return P.PROPOSAL_TEMPLATE
    return P.PROPOSAL_TEMPLATE + P.SOURCES_TEMPLATE.format(source_forms="; ".join(forms))


def _registry_names_section(run: Run) -> str:
    names = registry_names(run)
    if not names:
        return ""
    text = f"Capabilities in specs/registry: {', '.join(names)}"
    return f"\n\n{P.REGISTRY_HEADING}\n\n{P.REGISTRY_PROPOSAL_RULE}\n\n{text[:REGISTRY_TEXT_LIMIT]}\n"


def proposal_prompt(run: Run, errors: list[str] | None = None) -> str:
    prompt = P.PROPOSAL_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        interview=_read(run.config.change_dir / "interview.md") or "(none provided)",
        prd_section=prd_section(run),
        instruction=P.PROPOSAL_INSTRUCTION,
        source_rules=source_rules(run),
        template=proposal_template(run),
    ) + _registry_names_section(run)
    if errors:
        prompt += P.PROPOSAL_RETRY.format(errors="\n".join(f"- {e}" for e in errors))
    return prompt


def use_cases_prompt(run: Run) -> str:
    return P.USE_CASES_PROMPT.format(
        project_context=run.config.project_context,
        interview=_read(run.config.change_dir / "interview.md") or "(none provided)",
        proposal_content=proposal_text(run) or "(not available)",
        instruction=P.USE_CASES_INSTRUCTION,
        template=P.USE_CASES_TEMPLATE,
    )


def requirements_prompt(run: Run, cap: str, *, proposal: str | None = None) -> str:
    prompt = P.REQUIREMENTS_PROMPT.format(
        project_context=run.config.project_context,
        proposal_content=(proposal_text(run) if proposal is None else proposal) or "(not available)",
        capability_name=cap,
        instruction=P.REQUIREMENTS_INSTRUCTION,
        template=P.REQUIREMENTS_TEMPLATE,
    )
    current = _read(registry_dir(run) / f"{cap}.md")
    if current:
        prompt += (f"\n\n{P.REGISTRY_HEADING}\n\n{P.REGISTRY_REQUIREMENTS_RULE}\n\n"
                   f"### {cap} (specs/registry/{cap}.md)\n{current[:REGISTRY_TEXT_LIMIT]}\n")
    return prompt


def all_requirements_text(change_dir: Path) -> str:
    parts = [f"### requirements/{p.name}\n{p.read_text()}" for p in sorted((change_dir / "requirements").glob("*.md"))]
    return "\n\n".join(parts) or "(none)"


# --- generation -------------------------------------------------------------------

async def write_proposal(run: Run) -> str:
    """One proposal call, and one more with the problems listed when it cannot be kept."""
    text = await sdk.text_call(proposal_prompt(run), config=run.config, step="spec_artifact", label="spec:proposal")
    errors = proposal_errors(run, text)
    if errors:
        log.warning("proposal rejected, asking once more: %s", "; ".join(errors))
        text = await sdk.text_call(proposal_prompt(run, errors), config=run.config, step="spec_artifact",
                                   label="spec:proposal")
        errors = proposal_errors(run, text)
        if errors:
            raise ValueError("The proposal was rejected twice: " + "; ".join(errors))
    return text


async def write_requirements(run: Run, cap: str) -> str:
    text = await sdk.text_call(requirements_prompt(run, cap), config=run.config, step="spec_artifact",
                               label=f"spec:requirements:{cap}")
    if not valid_requirement_text(text):
        raise ValueError(f"Invalid requirements for {cap}: expected Requirement/Scenario/WHEN/THEN")
    return text


async def _produce(run: Run, key: str) -> None:
    decision = _decide(run, key)
    path = artifact_path(run, key)
    if decision == "keep":
        log.info("artifact %s: kept", key)
        return
    if decision == "adopt":
        log.info("artifact %s: adopted the file on disk", key)
        run.state.artifacts[key] = ArtifactRecord(inputs=inputs_for(run, key), output=sha(path.read_text()))
        run.save()
        return
    inputs = inputs_for(run, key)
    run.state.artifacts[key] = ArtifactRecord(inputs=inputs, output=None)
    run.save()
    log.info("START artifact=%s", key)
    if key == "proposal":
        text = await write_proposal(run)
    elif key == "use-cases":
        text = await sdk.text_call(use_cases_prompt(run), config=run.config, step="spec_artifact",
                                   label="spec:use-cases")
    else:
        text = await write_requirements(run, key.split("/", 1)[1])
    atomic_write(path, text)
    run.state.artifacts[key] = ArtifactRecord(inputs=inputs, output=sha(text))
    run.save()
    print(f"PHASE:ARTIFACT:{key}", flush=True)


def retire_superseded(run: Run) -> list[str]:
    """Move aside each requirements file the proposal no longer names."""
    expected = set(expected_capabilities(proposal_text(run)))
    moved = []
    for path in sorted((run.config.change_dir / "requirements").glob("*.md")):
        if expected and path.stem not in expected:
            supersede(run.config.change_dir, f"requirements/{path.name}")
            run.state.artifacts.pop(f"requirements/{path.stem}", None)
            moved.append(path.stem)
    if moved:
        run.save()
        run.progress("ARTIFACTS", "Moved superseded requirements aside: " + ", ".join(moved))
    return moved


async def generate_artifacts(run: Run) -> None:
    """Proposal, then use cases, then each capability's requirements, in that order."""
    await _produce(run, "proposal")
    if errors := proposal_errors(run, proposal_text(run)):
        raise ValueError("proposal.md cannot be used: " + "; ".join(errors))
    await _produce(run, "use-cases")
    (run.config.change_dir / "requirements").mkdir(exist_ok=True)
    for key in requirement_keys(run):
        await _produce(run, key)
    retire_superseded(run)
