"""The proposal (stage 1), the use cases (stage 2) and each component's `specs.md` (stage 3).

Copied from buildme's `spec_generator.py` (`_generate_single_artifact`,
`_generate_use_cases`, `_generate_requirements`), then adapted.

Each file is written by one call with no tools, only when it is missing (or
holds nothing usable): a file on disk is kept as it is, whoever wrote it, so
an edit made during a review stays and is an input of every later stage
(rows S2, S5, S7, S9, S11). Each file written gets a record in the state, the
hash of its inputs and of the text it wrote, saved with `output: None` before
the call and completed after the write.

- `proposal.md`: from the intake files and the interview. With a PRD, its
  `## Sources` names the PRD sections it comes from, each a heading of the
  PRD; a proposal that does not is asked for once more with the problems
  listed, and refused after that.
- `use-cases.md`: from the proposal and the interview.
- `specs.md`, per component (`components/<id>/specs.md`, or `specs.md` in the
  change dir for a change with one component): from the component's
  `plan.md`, the change's proposal and use cases, the names of the other
  components, the prototype's file list and the registry. It must pass
  `multiplai_core.specs.valid_specs_text`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from multiplai_core.specs import valid_specs_text

from . import intake, layout, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts import spec as P
from .publish import atomic_write, markdown_text
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
    """The proposal's inputs: the intake files, the interview and the project context."""
    change_dir = run.config.change_dir
    parts = {
        name: _read(change_dir / name)
        for name in ("prd.md", "ticket.md", "interview.md")
    }
    parts["context"] = run.config.project_context
    return sha(json.dumps(parts, sort_keys=True))


def proposal_text(run: Run) -> str:
    return _read(run.config.change_dir / "proposal.md")


def use_cases_text(run: Run) -> str:
    return _read(run.config.change_dir / "use-cases.md")


def artifact_path(run: Run, key: str) -> Path:
    """`proposal`, `use-cases`, `specs` or `components/<id>/specs`, as a file."""
    return run.config.change_dir / f"{key}.md"


def specs_key(run: Run, spec_dir: Path) -> str:
    rel = layout.rel(run.config.change_dir, spec_dir / layout.SPECS_FILE)
    return rel.removesuffix(".md")


def record_current(run: Run) -> None:
    """Record every spec file on disk as the run's own. Called after a
    rewrite publishes, so the files it wrote are recorded."""
    for key in ["proposal", "use-cases"] + [
        u.removesuffix(".md")
        for u in layout.units(run.config.change_dir)
        if Path(u).name == layout.SPECS_FILE
    ]:
        path = artifact_path(run, key)
        if path.is_file():
            previous = run.state.artifacts.get(key)
            run.state.artifacts[key] = ArtifactRecord(
                inputs=previous.inputs if previous else "", output=sha(path.read_text())
            )


def _usable(run: Run, key: str) -> bool:
    """A file with text in it. A `specs.md` someone edited into a broken
    format is kept too: the consistency check reports it."""
    path = artifact_path(run, key)
    return path.is_file() and bool(path.read_text().strip())


# --- the PRD sources in `## Sources` ------------------------------------------

_SOURCES_RE = re.compile(
    r"^##\s+Sources\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
)
_PRD_LINE_RE = re.compile(
    r"^-\s+PRD\s*(?:§\s*)?[\"“']?(.+?)[\"”']?\s*$", re.MULTILINE | re.IGNORECASE
)


def prd_sources(proposal: str) -> list[str]:
    """The PRD headings `## Sources` names, one `- PRD "<heading>"` line each."""
    section = _SOURCES_RE.search(proposal)
    if not section:
        return []
    return [m.group(1).strip() for m in _PRD_LINE_RE.finditer(section.group(1))]


def _norm(heading: str) -> str:
    return " ".join(heading.replace("*", "").replace("`", "").split()).casefold()


def proposal_errors(run: Run, text: str) -> list[str]:
    """Why a proposal cannot be kept: it is empty, or (with a PRD) its
    `## Sources` names no PRD section, or names one the PRD does not have."""
    if not text.strip():
        return ["the proposal is empty"]
    prd = intake.prd_text(run.config.change_dir)
    if not prd:
        return []
    headings = {_norm(h) for h in intake.prd_headings(prd)}
    named = prd_sources(text)
    if not named:
        return ['`## Sources` names no PRD section (a line `- PRD "<heading>"`)']
    return [
        f"`## Sources` names the PRD section {h!r}, which is not a heading of the PRD"
        for h in named
        if _norm(h) not in headings
    ]


# --- prompts --------------------------------------------------------------------


def prd_section(run: Run) -> str:
    prd = intake.prd_text(run.config.change_dir)
    return f"\n## PRD (prd.md)\n{intake.fenced(prd, 'prd.md')}\n" if prd else ""


def source_rules(run: Run) -> str:
    if intake.prd_text(run.config.change_dir):
        return "\n" + P.PRD_RULE + "\n"
    return ""


def proposal_template(run: Run) -> str:
    if not intake.prd_text(run.config.change_dir):
        return P.PROPOSAL_TEMPLATE
    return P.PROPOSAL_TEMPLATE + P.SOURCES_TEMPLATE


def proposal_prompt(run: Run, errors: list[str] | None = None) -> str:
    prompt = P.PROPOSAL_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        interview=_read(run.config.change_dir / "interview.md") or "(none provided)",
        prd_section=prd_section(run),
        instruction=P.PROPOSAL_INSTRUCTION,
        source_rules=source_rules(run),
        template=proposal_template(run),
    )
    if errors:
        prompt += P.PROPOSAL_RETRY.format(errors="\n".join(f"- {e}" for e in errors))
    return prompt


def use_cases_prompt(run: Run) -> str:
    return P.USE_CASES_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        interview=_read(run.config.change_dir / "interview.md") or "(none provided)",
        proposal_content=proposal_text(run) or "(not available)",
        instruction=P.USE_CASES_INSTRUCTION,
        template=P.USE_CASES_TEMPLATE,
    )


def plan_title(plan: str, fallback: str) -> str:
    m = re.search(r"^#\s+Plan:\s*(.+?)\s*$", plan, re.MULTILINE)
    return m.group(1) if m else fallback


def prototype_files(change_dir: Path) -> list[str]:
    """The files in the change's `prototype/`, relative to the change dir."""
    folder = change_dir / "prototype"
    if not folder.is_dir():
        return []
    return [layout.rel(change_dir, p) for p in sorted(folder.rglob("*")) if p.is_file()]


def registry_section(run: Run) -> str:
    names = registry_names(run)
    if not names:
        return f"\n\n{P.REGISTRY_HEADING}\n\n{P.REGISTRY_EMPTY_RULE}\n"
    texts = [
        f"### {n} (specs/registry/{n}.md)\n{_read(registry_dir(run) / f'{n}.md')}"
        for n in names
    ]
    body = "\n\n".join(texts)[:REGISTRY_TEXT_LIMIT]
    return (
        f"\n\n{P.REGISTRY_HEADING}\n\n{P.REGISTRY_NAMES_RULE}\n\n{P.REGISTRY_REQUIREMENTS_RULE}\n\n"
        f"{P.REGISTRY_ONLY_RULE.format(names=', '.join(names))}\n\n"
        f"Capabilities in specs/registry: {', '.join(names)}\n\n{body}\n"
    )


def specs_prompt(run: Run, spec_dir: Path) -> str:
    change_dir = run.config.change_dir
    dirs = layout.spec_dirs(change_dir)
    component = run.config.change_name if spec_dir == change_dir else spec_dir.name
    others = [
        f"- `{d.name}`: {plan_title(_read(d / layout.PLAN_FILE), d.name)}"
        for d in dirs
        if d != spec_dir
    ]
    files = prototype_files(change_dir)
    prototype = (
        "Files a reviewer will look at with these specs (what things look like):\n"
        + "\n".join(f"- {f}" for f in files)
        if files
        else "(none)"
    )
    return P.SPECS_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        project_context=run.config.project_context,
        proposal_content=proposal_text(run) or "(not available)",
        use_cases_content=use_cases_text(run) or "(not available)",
        component=component,
        plan_content=_read(spec_dir / layout.PLAN_FILE) or "(no plan)",
        other_components="\n".join(others) or "(none: this is the whole change)",
        prototype_files=prototype,
        instruction=P.SPECS_INSTRUCTION,
        template=P.SPECS_TEMPLATE,
    ) + registry_section(run)


# --- generation -------------------------------------------------------------------


async def write_proposal(run: Run) -> str:
    """One proposal call, and one more with the problems listed when it cannot be kept."""
    text = await sdk.text_call(
        proposal_prompt(run),
        config=run.config,
        step="spec_artifact",
        label="spec:proposal",
    )
    errors = proposal_errors(run, text)
    if errors:
        log.warning("proposal rejected, asking once more: %s", "; ".join(errors))
        text = await sdk.text_call(
            proposal_prompt(run, errors),
            config=run.config,
            step="spec_artifact",
            label="spec:proposal",
        )
        errors = proposal_errors(run, text)
        if errors:
            raise ValueError("The proposal was rejected twice: " + "; ".join(errors))
    return text


async def write_specs(run: Run, spec_dir: Path) -> str:
    label = "spec:specs:" + (
        run.config.change_name if spec_dir == run.config.change_dir else spec_dir.name
    )
    text = await sdk.text_call(
        specs_prompt(run, spec_dir),
        config=run.config,
        step="spec_artifact",
        label=label,
    )
    if not valid_specs_text(text):
        raise ValueError(
            f"Invalid specs.md for {spec_dir.name}: expected `# Capability: <kebab-name>` "
            "sections, each with Requirement/Scenario/WHEN/THEN"
        )
    return text


def _inputs(run: Run, key: str) -> str:
    if key == "proposal":
        return intake_hash(run)
    parts = [proposal_text(run), _read(run.config.change_dir / "interview.md")]
    if key != "use-cases":
        parts += [
            use_cases_text(run),
            _read(artifact_path(run, key).parent / layout.PLAN_FILE),
        ]
    return sha("\0".join(parts))


async def produce(run: Run, key: str, spec_dir: Path | None = None) -> None:
    """Write the artifact *key* when its file is missing or unusable; keep it otherwise."""
    path = artifact_path(run, key)
    if _usable(run, key):
        log.info("artifact %s: kept the file on disk", key)
        if key not in run.state.artifacts or run.state.artifacts[key].output is None:
            run.state.artifacts[key] = ArtifactRecord(
                inputs=_inputs(run, key), output=sha(path.read_text())
            )
            run.save()
        return
    inputs = _inputs(run, key)
    run.state.artifacts[key] = ArtifactRecord(inputs=inputs, output=None)
    run.save()
    log.info("START artifact=%s", key)
    if key == "proposal":
        text = await write_proposal(run)
    elif key == "use-cases":
        text = await sdk.text_call(
            use_cases_prompt(run),
            config=run.config,
            step="spec_artifact",
            label="spec:use-cases",
        )
    else:
        text = await write_specs(run, spec_dir)
    text = markdown_text(path, text)
    atomic_write(path, text)
    run.state.artifacts[key] = ArtifactRecord(inputs=inputs, output=sha(text))
    run.save()
    print(f"PHASE:ARTIFACT:{key}", flush=True)


async def generate_proposal(run: Run) -> None:
    await produce(run, "proposal")
    if errors := proposal_errors(run, proposal_text(run)):
        raise ValueError("proposal.md cannot be used: " + "; ".join(errors))


async def generate_use_cases(run: Run) -> None:
    await produce(run, "use-cases")


async def generate_specs(run: Run) -> None:
    """`specs.md` for each component (or the change), in order; the ones on disk are kept."""
    for spec_dir in layout.spec_dirs(run.config.change_dir):
        await produce(run, specs_key(run, spec_dir), spec_dir)
