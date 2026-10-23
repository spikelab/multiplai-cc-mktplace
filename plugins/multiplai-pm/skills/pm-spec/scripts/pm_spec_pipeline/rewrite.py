"""The one rewrite a checked step gets: spec files rewritten to fix numbered findings.

A spec file is one of `layout.units`: a component's `specs.md` (or the
change's own), or, in a change adopted in the old layout, one capability's
file. Every new text is computed before any file changes, and checked in code
(`layout.unit_text_valid`: `valid_specs_text` for a `specs.md`, which also
refuses a capability name that is not kebab-case or appears twice). A text
that fails becomes a deterministic finding, which blocks the step
(`findings.recheck_verdict`). The caller publishes the passing set as one
change (`publish.publish`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import artifacts, layout, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts import spec as P
from .run import Run

log = logging.getLogger(__name__)


def unit_label(run: Run, unit: str) -> str:
    """The short name of a spec file in a call's label: the component id, the
    change name, or the capability of an old-layout file."""
    path = Path(unit)
    if path.name == layout.SPECS_FILE:
        return path.parent.name if path.parent.name else run.config.change_name
    return path.stem


def cited_units(change_dir: Path, finding: dict) -> list[str]:
    """The spec files a finding's places or text name, by path or by capability name."""
    units = layout.units(change_dir)
    caps = layout.capability_units(change_dir)
    found: list[str] = []

    def add(names: list[str]) -> None:
        found.extend(u for u in names if u not in found)

    for place in finding.get("places") or []:
        raw = (
            str(place.get("file", "") if isinstance(place, dict) else place)
            .strip()
            .strip("`")
        )
        where = str(place.get("where", "")) if isinstance(place, dict) else ""
        matched = [u for u in units if raw == u or raw.endswith("/" + u)]
        if not matched and Path(raw).stem in caps:
            matched = caps[Path(raw).stem]
        if not matched and Path(raw).name == layout.SPECS_FILE:
            # A bare `specs.md`: the capability named where it points, else every specs.md.
            matched = [u for cap, us in caps.items() if cap in where for u in us]
        add(matched)
    text = str(finding.get("text") or finding.get("description") or "")
    add([u for u in units if u in text])
    for cap, us in caps.items():
        if f"`{cap}`" in text:
            add(us)
    return found


def findings_lines(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        places = "; ".join(
            f"{p.get('file')} ({p.get('where')})" if isinstance(p, dict) else str(p)
            for p in f.get("places") or []
        )
        lines.append(
            f"- {f.get('finding_id', '')} [{f.get('severity', '?')}] ({f.get('category', '?')}) "
            f"{f.get('text', '')}" + (f" Places: {places}." if places else "")
        )
    return "\n".join(lines)


async def rewrite_units(
    run: Run, findings: list[dict], units: list[str], *, label: str
) -> dict[str, str]:
    """`{<spec file>: new text}` for each of *units*, each call shown the
    findings that cite that file and the findings that cite none."""
    change_dir = run.config.change_dir
    everything = layout.all_specs_text(change_dir)
    out: dict[str, str] = {}
    for unit in units:
        mine = [f for f in findings if unit in (cited_units(change_dir, f) or [unit])]
        specs = Path(unit).name == layout.SPECS_FILE
        prompt = P.REWRITE_SPECS_PROMPT.format(
            notice=UNTRUSTED_NOTICE,
            project_context=run.config.project_context,
            proposal_content=artifacts.proposal_text(run) or "(not available)",
            use_cases_content=artifacts.use_cases_text(run) or "(not available)",
            specs_content=everything,
            unit=unit,
            current_content=(change_dir / unit).read_text(),
            findings=findings_lines(mine),
            format_note=", one `# Capability: <kebab-case-name>` line over each capability"
            if specs
            else "",
        )
        out[unit] = await sdk.text_call(
            prompt,
            config=run.config,
            step="spec_artifact",
            label=f"{label}:rewrite:{unit_label(run, unit)}",
        )
    return out


def check_texts(run: Run, files: dict[str, str]) -> list[dict]:
    """A deterministic finding for each rewritten text that breaks its file's format."""
    problems = []
    for rel, text in sorted(files.items()):
        if not layout.unit_text_valid(rel, text):
            problems.append(
                {
                    "category": "format",
                    "severity": "critical",
                    "description": f"the rewritten {rel} is not a specs file: every capability needs "
                    "a kebab-case `# Capability:` name used once, and every requirement a name "
                    "and a scenario with WHEN and THEN",
                }
            )
    return problems
