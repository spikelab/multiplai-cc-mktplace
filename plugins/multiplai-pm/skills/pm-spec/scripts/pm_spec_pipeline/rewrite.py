"""The one rewrite a checked step gets: spec files rewritten to fix numbered findings.

A spec file is one of `layout.units`: a component's `specs.md` (or the
change's own), or, in a change adopted in the old layout, one capability's
file. Every new text is computed before any file changes, and checked in code
(`layout.unit_text_valid`: `valid_specs_text` for a `specs.md`, which also
refuses a capability name that is not kebab-case or appears twice). A text
that fails becomes a deterministic finding, which blocks the step
(`findings.recheck_verdict`). The caller publishes the passing set as one
change (`publish.publish`).

Each answer has a strict shape (`parse_answer`): the one line `UNCHANGED`, or
the whole file between a `BEGIN` and an `END` line. Only the text between them
is the file; anything outside is dropped, and a `specs.md` may not change the
text above its first `# Capability:` line. A file the answer leaves as it was
is not in the set, so it stays byte for byte as it is; an answer of any other
shape is a deterministic finding too. A real-model run on 2026-10-23 wrote the
model's "...so it is returned unchanged." note as line 1 of a specs.md, and
`valid_specs_text` passed it, since it ignores text before the first
`# Capability:` heading.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from . import artifacts, layout, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts import spec as P
from .run import Run

log = logging.getLogger(__name__)

UNCHANGED = "UNCHANGED"
BEGIN = "=== FILE BEGIN ==="
END = "=== FILE END ==="


_CAPABILITY_RE = re.compile(r"^# Capability:", re.MULTILINE)
NAMES_ONLY = "(capability names and requirement titles only; no finding here cites it)"


def preamble(text: str) -> str:
    """The text of a `specs.md` above its first `# Capability:` line, which
    `valid_specs_text` does not read."""
    m = _CAPABILITY_RE.search(text)
    return (text[: m.start()] if m else text).strip()


class ContractError(ValueError):
    """An answer that is neither `UNCHANGED` nor one delimited file."""


class Rewritten(NamedTuple):
    """`rewrite_units`' result: *files*, `{<spec file>: new text}` for each
    file whose text changed; *problems*, a deterministic finding for each
    answer that broke the output contract."""

    files: dict[str, str]
    problems: list[dict]


def parse_answer(answer: str) -> str | None:
    """The file text a rewrite answer holds, or None for `UNCHANGED`.

    The file is the lines between the one `BEGIN` line and the one `END` line
    after it; any text outside them is dropped. With no such lines, an answer
    holding the line `UNCHANGED` is unchanged. Raises `ContractError` for any
    other answer, and for one that has both a file and `UNCHANGED`."""
    lines = answer.splitlines()
    marks = [line.strip() for line in lines]
    begins = [i for i, m in enumerate(marks) if m == BEGIN]
    ends = [i for i, m in enumerate(marks) if m == END]
    unchanged = UNCHANGED in marks
    if not begins and not ends:
        if unchanged:
            return None
        raise ContractError(
            f"the answer is neither the line {UNCHANGED} nor a file between "
            f"{BEGIN} and {END}"
        )
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ContractError(f"the answer does not hold one {BEGIN} ... {END} file")
    outside = lines[: begins[0]] + lines[ends[0] + 1 :]
    if any(line.strip() == UNCHANGED for line in outside):
        raise ContractError(f"the answer holds both a file and {UNCHANGED}")
    return "\n".join(lines[begins[0] + 1 : ends[0]]) + "\n"


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


def target_units(change_dir: Path, findings: list[dict]) -> list[str]:
    """The spec files a rewrite for *findings* rewrites: those they cite, or
    every one when a finding cites none (one between the proposal and the use
    cases, say)."""
    named = [cited_units(change_dir, f) for f in findings]
    if not all(named):
        return layout.units(change_dir)
    return sorted({u for us in named for u in us})


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
) -> Rewritten:
    """The new text of each of *units* the answers change (`Rewritten`), each
    call shown the findings that cite that file and the findings that cite
    none, in full the other spec files those findings cite, and every other
    spec file by its capability names and requirement titles."""
    change_dir = run.config.change_dir
    out: dict[str, str] = {}
    problems: list[dict] = []
    for unit in units:
        mine = [f for f in findings if unit in (cited_units(change_dir, f) or [unit])]
        specs = Path(unit).name == layout.SPECS_FILE
        current = (change_dir / unit).read_text()
        cited = {u for f in mine for u in cited_units(change_dir, f)} - {unit}
        others = [u for u in layout.units(change_dir) if u != unit]
        everything = (
            "\n\n".join(
                f"### {u}\n{(change_dir / u).read_text()}"
                if u in cited
                else f"### {u} {NAMES_ONLY}\n{layout.outline((change_dir / u).read_text())}"
                for u in others
            )
            or "(none)"
        )
        prompt = P.REWRITE_SPECS_PROMPT.format(
            notice=UNTRUSTED_NOTICE,
            project_context=run.config.project_context,
            proposal_content=artifacts.proposal_text(run) or "(not available)",
            use_cases_content=artifacts.use_cases_text(run) or "(not available)",
            specs_content=everything,
            unit=unit,
            current_content=current,
            findings=findings_lines(mine),
            format_note=", one `# Capability: <kebab-case-name>` line over each capability"
            if specs
            else "",
            unchanged=UNCHANGED,
            begin=BEGIN,
            end=END,
        )
        call_label = f"{label}:rewrite:{unit_label(run, unit)}"
        answer = await sdk.text_call(
            prompt, config=run.config, step="spec_artifact", label=call_label
        )
        try:
            text = parse_answer(answer)
            # A text with no capability at all is `check_texts`' to refuse.
            if (
                specs
                and text is not None
                and _CAPABILITY_RE.search(text)
                and preamble(text) != preamble(current)
            ):
                raise ContractError(
                    "it changes the text above the first `# Capability:` line"
                )
        except ContractError as e:
            log.warning("%s: %s; answer starts %r", call_label, e, answer[:200])
            problems.append(
                {
                    "category": "format",
                    "severity": "critical",
                    "description": f"the rewrite of {unit} was not written: {e}",
                }
            )
            continue
        if text is None or text.rstrip() == current.rstrip():
            log.info("%s: unchanged", call_label)
            continue
        out[unit] = text
    return Rewritten(out, problems)


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
