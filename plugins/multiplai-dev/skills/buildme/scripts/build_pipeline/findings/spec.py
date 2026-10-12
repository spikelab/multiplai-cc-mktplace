"""Change the specs a finding requires, before any code is fixed.

For every `spec_change` verdict, and every `code_fix` whose fix contradicts the
current spec (`Triage.spec_conflict`), one toolless `llm_call` per round writes
a delta in the ADDED/MODIFIED/REMOVED form `ChangeManager._apply_delta` reads,
saved as `respec-findings-r<N>.md`. The pipeline applies it to the change's
`requirements/<capability>.md` files and commits
`spec(findings-r<N>): <ids>`, with or without `--auto`.

This path writes its own delta file. A build's `run_respec_audit` stays
propose-only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from .. import git_ops, sdk
from ..change_manager import ChangeManager
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


def _requirement_block(text: str, name: str) -> str:
    m = re.search(rf"^### Requirement:[ \t]*{re.escape(name)}[ \t]*$.*?(?=^### Requirement:|^## |\Z)",
                  text, re.MULTILINE | re.DOTALL)
    return m.group(0).strip() if m else ""


def _section_requirements(delta: str) -> list[tuple[str, str, str]]:
    """[(operation, requirement name, its block in the delta)]."""
    out = []
    for sec in _SECTION_RE.finditer(delta):
        op, body = sec.group(1), sec.group(2)
        for req in _REQ_RE.finditer(body):
            out.append((op, req.group(1).strip(), _requirement_block(body, req.group(1).strip())))
    return out


def apply_delta_file(config: BuildConfig, change_dir: Path, delta_file: Path,
                     records: list[TriageRecord]) -> tuple[list[SpecChange], list[Path]]:
    """Apply `respec-findings-r<N>.md` to the change's requirements. Returns the
    changes (applied and declined) and the requirement files written. A
    capability that is not an existing requirements file is declined, never
    created: the name came from a model."""
    files = _requirement_files(change_dir)
    blocks, declined = parse_delta(delta_file.read_text())
    cm = ChangeManager(config.specs_dir)
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
        if not reqs:
            continue
        before_text = target.read_text()
        after_text = cm._apply_delta(before_text, delta)
        for op, name, block in reqs:
            before = _requirement_block(before_text, name)
            after = _requirement_block(after_text, name) if op != "REMOVED" else ""
            changes.append(SpecChange(capability=capability, requirement=name, operation=op,
                                      before=before, after=after or block, finding_keys=keys))
        if after_text != before_text:
            target.write_text(after_text)
            written.append(target)
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
