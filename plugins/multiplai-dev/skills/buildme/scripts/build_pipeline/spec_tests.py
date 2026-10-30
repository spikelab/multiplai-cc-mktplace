"""`tdd.spec_tests`: before a block's test writer, a second test writer writes
tests for the block's scenarios marked security or permission, from those
scenarios and the public interfaces they touch, without the design.

A scenario is marked when its `#### Scenario:` heading, or the
`### Requirement:` heading it sits under, contains the word "security" or
"permission" (or "permissions"), in any case: `[security]`, `@permission`
and "(permission)" all count. A marked requirement marks all its scenarios.

A block gets the marked scenarios of the capabilities its `Satisfies:` line
names. When a `Satisfies:` entry names requirements or scenarios of a
capability, the block gets only the marked ones among those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .change_manager import requirement_texts
from .models import BlockInfo

MARK_RE = re.compile(r"\b(?:security|permissions?)\b", re.IGNORECASE)
_REQUIREMENT_RE = re.compile(r"^###\s+Requirement:\s*(.*?)\s*$", re.MULTILINE)
_SCENARIO_RE = re.compile(r"^####\s+Scenario:\s*(.*?)\s*$", re.MULTILINE)

SPEC_TESTS_RULE = (
    "Write tests only for the security and permission scenarios under "
    "Specs / Contracts, from those scenarios and the public interfaces under "
    "Context. You are not given the design on purpose: do not open the "
    "change's design or implementation plan, and test what each scenario says "
    "through the interfaces listed, not how the code might be built. Another "
    "test writer writes the block's other tests after you."
)

SPEC_TESTS_DONE_HEADING = "## Tests already written from the specs alone"


@dataclass
class _Requirement:
    heading: str  # the `### Requirement:` line, "" before the first one
    name: str
    preamble: str  # the text between the heading and the first scenario
    scenarios: list[tuple[str, str]] = field(default_factory=list)  # (name, text)


def _requirements(text: str) -> list[_Requirement]:
    """*text*'s requirements, each with its scenarios' names and full text."""
    heads = list(_REQUIREMENT_RE.finditer(text))
    chunks = [("", "", text[: heads[0].start()] if heads else text)]
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunks.append((m.group(0), m.group(1), text[m.end() : end]))
    out = []
    for heading, name, body in chunks:
        scen = list(_SCENARIO_RE.finditer(body))
        if not heading and not scen:
            continue
        req = _Requirement(
            heading=heading,
            name=name,
            preamble=(body[: scen[0].start()] if scen else body).strip(),
        )
        for i, m in enumerate(scen):
            end = scen[i + 1].start() if i + 1 < len(scen) else len(body)
            req.scenarios.append((m.group(1), body[m.start() : end].strip()))
        out.append(req)
    return out


def _mentions(entry: str, name: str) -> bool:
    """Whether the `Satisfies:` *entry* names *name*, as whole words."""
    return bool(name) and bool(
        re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", entry, re.IGNORECASE)
    )


def block_marked_scenarios(block: BlockInfo, change_dir: Path) -> str:
    """The marked scenarios *block* satisfies, under their capability and
    requirement headings, word for word; "" when there are none."""
    parts: list[str] = []
    for cap, text in requirement_texts(change_dir).items():
        reqs = _requirements(text)
        entries = [e for e in block.satisfies if _mentions(e, cap)]
        specific = [
            e
            for e in block.satisfies
            if any(
                _mentions(e, r.name) or any(_mentions(e, s) for s, _ in r.scenarios)
                for r in reqs
            )
        ]
        if not entries and not specific:
            continue
        chosen: list[str] = []
        for req in reqs:
            req_marked = bool(MARK_RE.search(req.name))
            req_named = any(_mentions(e, req.name) for e in specific)
            scenarios = [
                body
                for name, body in req.scenarios
                if (req_marked or MARK_RE.search(name))
                and (
                    not specific
                    or req_named
                    or any(_mentions(e, name) for e in specific)
                )
            ]
            if scenarios:
                head = [req.heading] if req.heading else []
                if req.preamble and req.heading:
                    head.append(req.preamble)
                chosen.append("\n\n".join(head + scenarios))
        if chosen:
            parts.append(f"## Capability: {cap}\n\n" + "\n\n".join(chosen))
    return "\n\n".join(parts)


def spec_tests_context(
    block: BlockInfo, project_dir: Path, blocks: list[BlockInfo] | None
) -> str:
    """The public interfaces the spec-test writer gets: the block's own
    Produces/Consumes lines, earlier blocks' Produces signatures and the
    signatures of the existing code the block names. No design, requirements
    beyond the marked scenarios, unknowns, facts, memory files or docs."""
    from .signatures import signatures_section

    parts = [f"# Block {block.number}: {block.name}"]
    if block.satisfies:
        parts.append(f"Satisfies: {', '.join(block.satisfies)}")
    if block.produces or block.consumes:
        lines = [f"- Produces: {sig}" for sig in block.produces]
        lines += [f"- Consumes: {sig}" for sig in block.consumes]
        parts.append("## Interfaces (this block)\n" + "\n".join(lines))
    prior = [
        f"- Block {b.number} ({b.name}) produces: {sig}"
        for b in blocks or []
        if b.number < block.number
        for sig in b.produces
    ]
    if prior:
        parts.append(
            "## Interfaces from earlier blocks (use these signatures verbatim)\n"
            + "\n".join(prior)
        )
    sigs = signatures_section(block, project_dir)
    if sigs:
        parts.append(sigs)
    return "\n\n".join(parts)


def spec_tests_done_section(files: list[str]) -> str:
    """What the block's own test writer is told about the spec-test files."""
    return (
        f"{SPEC_TESTS_DONE_HEADING}\n"
        "A separate test writer, given only this block's security and "
        "permission scenarios and its public interfaces, wrote these test "
        "files:\n"
        + "\n".join(f"- {f}" for f in files)
        + "\nLeave them unchanged and do not test those scenarios again; "
        "write the block's other tests in other files."
    )
