"""The one rewrite a checked step gets: requirements files (and, for the
prototype, the proposal) rewritten to fix numbered findings.

Every new text is computed before any file changes, and checked in code: a
requirements file must pass `multiplai_core.specs.valid_requirement_text` and a
proposal `artifacts.proposal_errors`. A text that fails becomes a
deterministic finding, which blocks the step (`findings.recheck_verdict`).
The caller publishes the passing set as one change (`publish.publish`).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from multiplai_core.specs import expected_capabilities, valid_requirement_text

from . import artifacts, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts import spec as P
from .run import Run

log = logging.getLogger(__name__)

_REQ_PATH_RE = re.compile(r"(?:^|/)requirements/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\.md)?$")


def change_caps(change_dir: Path) -> list[str]:
    return sorted(p.stem for p in (change_dir / "requirements").glob("*.md"))


def cited_caps(change_dir: Path, finding: dict) -> list[str]:
    """The capabilities whose requirements file a finding's places or text name."""
    caps = change_caps(change_dir)
    found: list[str] = []
    for place in finding.get("places") or []:
        raw = str(place.get("file", "") if isinstance(place, dict) else place).strip().strip("`")
        m = _REQ_PATH_RE.search(raw)
        stem = m.group(1) if m else Path(raw).stem
        if stem in caps and stem not in found:
            found.append(stem)
    text = str(finding.get("text") or finding.get("description") or "")
    for cap in caps:
        if cap not in found and (f"requirements/{cap}" in text or f"`{cap}`" in text):
            found.append(cap)
    return found


def findings_lines(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        places = "; ".join(f"{p.get('file')} ({p.get('where')})" if isinstance(p, dict) else str(p)
                           for p in f.get("places") or [])
        lines.append(f"- {f.get('finding_id', '')} [{f.get('severity', '?')}] ({f.get('category', '?')}) "
                     f"{f.get('text', '')}" + (f" Places: {places}." if places else ""))
    return "\n".join(lines)


async def rewrite_requirements(run: Run, findings: list[dict], caps: list[str], *, label: str,
                               proposal: str | None = None) -> dict[str, str]:
    """`{"requirements/<cap>.md": new text}` for each of *caps*, each call shown
    the findings that cite that file and the findings that cite none."""
    change_dir = run.config.change_dir
    proposal = artifacts.proposal_text(run) if proposal is None else proposal
    use_cases = (change_dir / "use-cases.md").read_text() if (change_dir / "use-cases.md").is_file() else ""
    everything = artifacts.all_requirements_text(change_dir)
    out: dict[str, str] = {}
    for cap in caps:
        # The findings that cite this file, and those that cite no file at all.
        mine = [f for f in findings if cap in (cited_caps(change_dir, f) or [cap])]
        current = (change_dir / "requirements" / f"{cap}.md").read_text()
        prompt = P.REWRITE_REQUIREMENTS_PROMPT.format(
            project_context=run.config.project_context, proposal_content=proposal or "(not available)",
            use_cases_content=use_cases or "(not available)", requirements_content=everything,
            capability_name=cap, current_content=current, findings=findings_lines(mine))
        out[f"requirements/{cap}.md"] = await sdk.text_call(prompt, config=run.config, step="spec_artifact",
                                                            label=f"{label}:rewrite:{cap}")
    return out


async def rewrite_proposal(run: Run, findings: list[dict], *, label: str) -> str:
    change_dir = run.config.change_dir
    has_sources = bool(artifacts.source_rules(run))
    prompt = P.REWRITE_PROPOSAL_PROMPT.format(
        notice=UNTRUSTED_NOTICE, project_context=run.config.project_context,
        interview=(change_dir / "interview.md").read_text() if (change_dir / "interview.md").is_file() else "(none)",
        prd_section=artifacts.prd_section(run), proposal_content=artifacts.proposal_text(run),
        requirements_content=artifacts.all_requirements_text(change_dir), findings=findings_lines(findings),
        sources_note=", and `## Sources` (one line per capability)" if has_sources else "")
    return await sdk.text_call(prompt, config=run.config, step="spec_artifact", label=f"{label}:rewrite:proposal")


async def rewrite_proposal_and_requirements(run: Run, findings: list[dict], *, label: str) -> dict[str, str | None]:
    """The prototype's rewrite: the proposal, then each capability it names —
    an existing file rewritten, a new one written — and None (moved aside) for
    a file whose capability the new proposal drops."""
    change_dir = run.config.change_dir
    files: dict[str, str | None] = {"proposal.md": await rewrite_proposal(run, findings, label=label)}
    caps = expected_capabilities(files["proposal.md"] or "")
    existing = change_caps(change_dir)
    if caps:
        files.update(await rewrite_requirements(run, findings, [c for c in caps if c in existing], label=label,
                                                proposal=files["proposal.md"]))
        for cap in [c for c in caps if c not in existing]:
            prompt = artifacts.requirements_prompt(run, cap, proposal=files["proposal.md"])
            files[f"requirements/{cap}.md"] = await sdk.text_call(prompt, config=run.config, step="spec_artifact",
                                                                  label=f"{label}:rewrite:{cap}")
        for cap in existing:
            if cap not in caps:
                files[f"requirements/{cap}.md"] = None
    return files


def check_texts(run: Run, files: dict[str, str | None]) -> list[dict]:
    """A deterministic finding for each rewritten text that breaks its file's format."""
    problems = []
    for rel, text in sorted(files.items()):
        if text is None:
            continue
        if rel == "proposal.md":
            for error in artifacts.proposal_errors(run, text):
                problems.append({"category": "format", "severity": "critical",
                                 "description": f"the rewritten proposal.md cannot be used: {error}"})
        elif not valid_requirement_text(text):
            problems.append({"category": "format", "severity": "critical",
                             "description": f"the rewritten {rel} is not a requirements file: every requirement "
                                            "needs a name and a scenario with WHEN and THEN"})
    return problems
