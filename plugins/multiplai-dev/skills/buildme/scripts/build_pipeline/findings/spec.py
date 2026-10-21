"""Change the specs a finding requires, before any code is fixed.

For every `spec_change` verdict, and every `code_fix` whose fix contradicts the
current spec (`Triage.spec_conflict`), one toolless `llm_call` per round writes
a delta in the ADDED/MODIFIED/REMOVED form `multiplai_core.specs.apply_delta` reads,
saved as `respec-findings-r<N>.md`. The pipeline applies it to the change's
`requirements/<capability>.md` files with `_edit_change` below, not with
`apply_delta`, and commits
`spec(findings-r<N>): <ids>`, with or without `--auto`.

A change's `requirements/<capability>.md` is itself a delta, split into
`## ADDED/MODIFIED/REMOVED Requirements` sections, so each requirement is
edited inside its section (`multiplai_core.specs.apply_delta` is written for
registry files and would move later requirements across sections). When the
change is archived, which an `--auto` build always does, its requirements
were already merged into `specs/registry/<capability>.md`: the same edit is
made there too, since that file is the live spec. A requirement counts as
applied only when its text changed.

This path writes its own delta file. A build's `run_respec_audit` stays
propose-only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from .. import git_ops, sdk
from ..config import BuildConfig
from ..prompts.findings import SPEC_DELTA_PROMPT
from .models import SpecChange, TriageRecord, Verdict

log = logging.getLogger(__name__)

LlmCall = Callable[..., Awaitable[str]]

_CAPABILITY_RE = re.compile(r"^# Capability:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_DECLINED_RE = re.compile(r"^# Declined[ \t]*$", re.MULTILINE)
_FINDINGS_LINE_RE = re.compile(r"^Findings:[ \t]*(.*)$", re.MULTILINE)
_SECTION_RE = re.compile(
    r"^## (ADDED|MODIFIED|REMOVED) Requirements[ \t]*\n(.*?)(?=^## (?:ADDED|MODIFIED|REMOVED) Requirements|\Z)",
    re.MULTILINE | re.DOTALL,
)
_REQ_RE = re.compile(r"^### Requirement:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_NONE_LINE_RE = re.compile(r"^[ \t]*_None(?: proposed)?\._[ \t]*\n?", re.MULTILINE)
_HEADING2_RE = re.compile(r"^## (.*?)[ \t]*$", re.MULTILINE)
_SECTION_NAME_RE = re.compile(r"^(ADDED|MODIFIED|REMOVED|RENAMED) Requirements$")


def delta_path(change_dir: Path, round_n: int) -> Path:
    return Path(change_dir) / f"respec-findings-r{round_n}.md"


def short_id(record: TriageRecord) -> str:
    c = record.comment
    return c.finding_id or f"c{c.id}"


def needs_spec(records: list[TriageRecord]) -> list[TriageRecord]:
    """The records whose fix starts with a spec change."""
    out = []
    for r in records:
        t = r.triage
        if t is None:
            continue
        if t.verdict == Verdict.SPEC_CHANGE or (t.verdict == Verdict.CODE_FIX and t.spec_conflict):
            out.append(r)
    return out


def _requirement_files(change_dir: Path) -> dict[str, Path]:
    req_dir = Path(change_dir) / "requirements"
    if not req_dir.is_dir():
        return {}
    return {f.stem: f for f in sorted(req_dir.glob("*.md"))}


def _findings_block(records: list[TriageRecord]) -> str:
    parts = []
    for r in records:
        t = r.triage
        assert t is not None
        parts.append(
            f"- [{r.comment.key}] {t.verdict.value}"
            + (f" — requirement: {t.requirement}" if t.requirement else "")
            + f"\n  {t.reason}"
            + (f"\n  A failing test must show: {t.failing_test}" if t.failing_test else "")
        )
    return "\n".join(parts)


async def write_delta(config: BuildConfig, change_dir: Path, round_n: int,
                      records: list[TriageRecord], *, call: LlmCall | None = None) -> Path:
    """One toolless call; writes `respec-findings-r<N>.md` and returns its path.
    Raises when the call fails: a round never fixes code against a spec it
    was told is wrong."""
    call = call or sdk.llm_call
    files = _requirement_files(change_dir)
    requirements = "\n\n".join(f"### {name}\n{p.read_text()}" for name, p in files.items())
    prompt = SPEC_DELTA_PROMPT.format(
        change_name=config.change_name,
        findings=_findings_block(records),
        requirements=requirements or "(no requirements files)",
    )
    body = await call(
        prompt,
        model=config.model_for('findings_spec'),
        effort=config.effort_for('findings_spec'),
        system_prompt=("You are a specification editor writing a delta. Output the delta "
                       "directly as markdown. Do not use tools; everything you need is in "
                       "the prompt."),
        budget_label="findings_spec",
    )
    path = delta_path(change_dir, round_n)
    ids = ", ".join(short_id(r) for r in records)
    path.write_text(f"# Spec delta from review findings, round {round_n}\n\n"
                    f"Findings: {ids}\n\n{body.strip()}\n")
    return path


def parse_delta(text: str) -> tuple[list[tuple[str, list[str], str]], dict[str, str]]:
    """([(capability, finding keys, delta text)], {finding key: declined reason})."""
    declined: dict[str, str] = {}
    m = _DECLINED_RE.search(text)
    body = text
    if m:
        body = text[:m.start()]
        for line in text[m.end():].splitlines():
            line = line.strip()
            if line.startswith("- ") and ":" in line:
                key, _, reason = line[2:].partition(": ")
                if reason:
                    declined[key.strip()] = reason.strip()
    blocks = []
    heads = list(_CAPABILITY_RE.finditer(body))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        chunk = body[h.end():end]
        keys: list[str] = []
        fm = _FINDINGS_LINE_RE.search(chunk)
        if fm:
            keys = [k.strip().strip("[]") for k in fm.group(1).split(",") if k.strip()]
            chunk = chunk[:fm.start()] + chunk[fm.end():]
        blocks.append((h.group(1).strip(), keys, _NONE_LINE_RE.sub("", chunk).strip() + "\n"))
    return blocks, declined


def _find_requirement(text: str, name: str) -> re.Match | None:
    """The block of requirement *name*: its heading up to the next
    requirement, the next `## ` heading, or the end."""
    return re.search(rf"^### Requirement:[ \t]*{re.escape(name)}[ \t]*$.*?(?=^### Requirement:|^## |\Z)",
                     text, re.MULTILINE | re.DOTALL)


def _requirement_block(text: str, name: str) -> str:
    m = _find_requirement(text, name)
    return m.group(0).strip() if m else ""


def _section_at(text: str, pos: int) -> str | None:
    """ADDED, MODIFIED, REMOVED or RENAMED when *pos* is in that
    `## <op> Requirements` section; None when it is under no `## ` heading or
    another one."""
    op = None
    for h in _HEADING2_RE.finditer(text, 0, pos):
        m = _SECTION_NAME_RE.match(h.group(1))
        op = m.group(1) if m else None
    return op


def _join(head: str, block: str, rest: str) -> str:
    """*head*, then *block* (may be empty), then *rest*, one blank line
    between each and one newline at the end. Only the seams are touched."""
    parts = [x for x in (head.rstrip("\n"), block.strip(), rest.lstrip("\n")) if x]
    return "\n\n".join(parts).rstrip("\n") + "\n"


def _replace(text: str, m: re.Match, block: str) -> str:
    if m.group(0).strip() == block.strip():
        return text
    return _join(text[:m.start()], block, text[m.end():])


def _remove(text: str, m: re.Match) -> str:
    return _join(text[:m.start()], "", text[m.end():])


def _insert_in_section(text: str, op: str, block: str) -> str:
    """*block* at the end of the `## <op> Requirements` section (added at the
    end of the file when there is none), dropping its `_None._` line."""
    head = re.search(rf"^## {op} Requirements[ \t]*$", text, re.MULTILINE)
    if head is None:
        return _join(text, f"## {op} Requirements\n\n{block.strip()}", "")
    nxt = re.compile(r"^## ", re.MULTILINE).search(text, head.end())
    end = nxt.start() if nxt else len(text)
    section = _NONE_LINE_RE.sub("", text[:end][head.start():])
    return _join(text[:head.start()] + section, block, text[end:])


def _edit_registry(text: str, op: str, name: str, block: str) -> tuple[str, str]:
    """(*text* with the operation made, "") or (*text*, why it was not) for a
    registry file, where a requirement is replaced or removed wherever it is."""
    m = _find_requirement(text, name)
    if op == "ADDED":
        if m:
            return text, f"a requirement named {name!r} already exists"
        if re.search(r"^## ADDED Requirements[ \t]*$", text, re.MULTILINE):
            return _insert_in_section(text, "ADDED", block), ""
        return _join(text, block, ""), ""
    if m is None:
        return text, f"no requirement named {name!r}"
    return (_replace(text, m, block) if op == "MODIFIED" else _remove(text, m)), ""


def _edit_change(text: str, op: str, name: str, block: str,
                 registry: str | None) -> tuple[str, str]:
    """The same for the change's own requirements file, a delta in sections:
    the requirement keeps its section, and one the change did not touch yet
    (it is only in the *registry*) gets an entry in the MODIFIED or REMOVED
    section, which the archive merges."""
    m = _find_requirement(text, name)
    where = _section_at(text, m.start()) if m else None
    in_registry = bool(registry and _find_requirement(registry, name))
    if m is not None and where is None:
        return _edit_registry(text, op, name, block)
    if op == "ADDED":
        if m or in_registry:
            return text, f"a requirement named {name!r} already exists"
        return _insert_in_section(text, "ADDED", block), ""
    if m is None:
        if not in_registry:
            return text, f"no requirement named {name!r} in the change or the registry"
        return _insert_in_section(text, op, block), ""
    if where == "REMOVED":
        return text, f"the change already removes {name!r}"
    if op == "MODIFIED":
        return _replace(text, m, block), ""
    if where == "MODIFIED":   # the change edits a registry requirement: remove that instead
        return _insert_in_section(_remove(text, m), "REMOVED", block), ""
    return _remove(text, m), ""


def _section_requirements(delta: str) -> list[tuple[str, str, str]]:
    """[(operation, requirement name, its block in the delta)]."""
    out = []
    for sec in _SECTION_RE.finditer(delta):
        op, body = sec.group(1), sec.group(2)
        for req in _REQ_RE.finditer(body):
            out.append((op, req.group(1).strip(), _requirement_block(body, req.group(1).strip())))
    return out


def is_archived(config: BuildConfig, change_dir: Path) -> bool:
    return Path(change_dir).resolve().is_relative_to((Path(config.specs_dir) / "archive").resolve())


def apply_delta_file(config: BuildConfig, change_dir: Path, delta_file: Path,
                     records: list[TriageRecord]) -> tuple[list[SpecChange], list[Path]]:
    """Apply `respec-findings-r<N>.md` to the change's requirements, and to
    `specs/registry/` when the change is archived. Returns the changes
    (applied and declined) and the files written. A capability that is not an
    existing requirements file is declined, never created: the name came
    from a model. A requirement is applied only when every file it must
    change did change; otherwise neither is touched for it."""
    files = _requirement_files(change_dir)
    blocks, declined = parse_delta(delta_file.read_text())
    archived = is_archived(config, change_dir)
    changes: list[SpecChange] = []
    written: list[Path] = []
    covered: set[str] = set()
    for capability, keys, delta in blocks:
        covered.update(keys)
        reqs = _section_requirements(delta)
        target = files.get(capability)
        if target is None:
            for op, name, _ in reqs or [("MODIFIED", "(unnamed)", "")]:
                changes.append(SpecChange(capability=capability, requirement=name, operation=op,
                                          finding_keys=keys, applied=False,
                                          declined_reason="no such requirements file in the change"))
            continue
        reg_path = Path(config.specs_dir) / "registry" / f"{capability}.md"
        registry = reg_path.read_text() if reg_path.is_file() else None
        text, reg_text = target.read_text(), registry
        for op, name, block in reqs:
            before = _requirement_block(text, name) or _requirement_block(reg_text or "", name)
            new_text, why = _edit_change(text, op, name, block, reg_text)
            new_reg = reg_text
            if not why and archived:
                if reg_text is None:
                    why = f"the change is archived and specs/registry/{capability}.md is missing"
                else:
                    new_reg, why = _edit_registry(reg_text, op, name, block)
            if not why and new_text == text and new_reg == reg_text:
                why = "the delta's text is the same as the current requirement"
            if why:
                changes.append(SpecChange(capability=capability, requirement=name, operation=op,
                                          before=before, after=block, finding_keys=keys,
                                          applied=False, declined_reason=why))
                continue
            text, reg_text = new_text, new_reg
            after = _requirement_block(text, name) if op != "REMOVED" else ""
            changes.append(SpecChange(capability=capability, requirement=name, operation=op,
                                      before=before, after=after, finding_keys=keys))
        if text != target.read_text():
            target.write_text(text)
            written.append(target)
        if reg_text != registry:
            reg_path.write_text(reg_text)
            written.append(reg_path)
    for r in records:
        if r.comment.key in declined and r.comment.key not in covered:
            t = r.triage
            changes.append(SpecChange(capability="", requirement=(t.requirement if t else ""),
                                      operation="MODIFIED", finding_keys=[r.comment.key],
                                      applied=False, declined_reason=declined[r.comment.key]))
    return changes, written


def commit_spec(project_dir: Path, round_n: int, records: list[TriageRecord],
                paths: list[Path]) -> str | None:
    """`spec(findings-r<N>): <ids>` over exactly *paths*."""
    ids = ", ".join(short_id(r) for r in records)
    return git_ops.commit_paths(
        Path(project_dir), f"spec(findings-r{round_n}): {ids}",
        [str(p) for p in paths] + git_ops.BOOKKEEPING_EXCLUDES,
    )


async def run_spec_step(config: BuildConfig, change_dir: Path, round_n: int,
                        records: list[TriageRecord], *, extra_paths: list[Path] | None = None,
                        call: LlmCall | None = None) -> tuple[list[SpecChange], str | None]:
    """Write, apply and commit the round's spec delta. Returns the changes and
    the commit sha (None when no finding needed a spec change, or the delta
    changed nothing). *extra_paths* (e.g. `findings-r<N>.md`) ride along in
    the same commit."""
    wanted = needs_spec(records)
    if not wanted:
        return [], None
    delta_file = await write_delta(config, change_dir, round_n, wanted, call=call)
    changes, written = apply_delta_file(config, change_dir, delta_file, wanted)
    if not written:
        log.info("findings round %d: the spec delta changed no requirement", round_n)
    sha = commit_spec(config.project_dir, round_n, wanted,
                      [delta_file, *written, *(extra_paths or [])])
    return changes, sha
