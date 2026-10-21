"""The UI or output-format prototype, before the requirements are final (row S4).

Runs only when `prototype_required` is true for the change (a UI, or a
user-visible output format, from the proposal and requirements), or when
`--prototype` / `prototype: {enabled: true}` forces it; `--no-prototype` /
`enabled: false` turns it off. One agent with `Read`, `Write`, `Glob` and
`Grep` writes a mockup, sample output or transcript plus `NOTES.md` into
`prototype/`; every path it reports writing is checked against that folder in
code, and `prototype_gate` checks the result (one retry).

The notes' `DISPROVES` (major) and `OPEN_QUESTIONS` (minor) become numbered
findings `PT-F<n>` for the step's one rewrite, of the proposal and the
requirements, published as one change; the consistency check that follows is
its recheck. Once the prototype step has passed or been skipped it never runs
again for the change, as in buildme.

Copied from buildme's `llm_steps/prototype_steps.py` and `gates.py`
(`prototype_required`, `parse_prototype_notes`, `prototype_gate`) and
`rubric.detect_change_type`, then adapted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import artifacts, sdk
from . import findings as F
from .prompts.prototype import PROTOTYPE_PROMPT
from .publish import publish
from .rewrite import check_texts, rewrite_proposal_and_requirements
from .run import EXIT_FAILED, Run, Stop

log = logging.getLogger(__name__)

NAME = "prototype"
PROTOTYPE_TOOLS = ["Read", "Write", "Glob", "Grep"]
MAX_PROTOTYPE_ATTEMPTS = 2

_FRONTEND_KEYWORDS = re.compile(
    r"\b(react|vue|angular|css|html|component|ui|ux|button|form|page|"
    r"frontend|browser|dom|tailwind|stylesheet)\b", re.IGNORECASE)
_BACKEND_KEYWORDS = re.compile(
    r"\b(api|endpoint|database|db|migration|model|schema|server|"
    r"backend|rest|graphql|queue|worker|celery|cron)\b", re.IGNORECASE)
_INFRA_KEYWORDS = re.compile(
    r"\b(terraform|docker|kubernetes|k8s|ci|cd|pipeline|deploy|"
    r"infra|infrastructure|monitoring|logging|helm|ansible)\b", re.IGNORECASE)
_OUTPUT_FORMAT_RE = re.compile(
    r"\b(report|reports|export|exports|dashboard|mockup|wireframe|"
    r"screenshot|printout|invoice|receipt|spreadsheet|pdf|"
    r"cli output|command.line output|terminal output|console output|"
    r"stdout|sample output|output format|output schema|response schema|"
    r"json schema|rendered (?:page|view|output|document)|"
    r"user.visible|user.facing (?:output|format|document))\b", re.IGNORECASE)

PROTOTYPE_SLOTS = ("PROVES", "DISPROVES", "OPEN_QUESTIONS", "STATUS")
_SLOT_HEADER_RE = re.compile(
    r"^\s*(?:[-*]\s*|#{1,6}\s*|\*\*)?(" + "|".join(PROTOTYPE_SLOTS) + r")\*{0,2}\s*:",
    re.MULTILINE | re.IGNORECASE)
_EMPTY_SLOT_VALUES = {"", "none", "none.", "n/a", "na", "-", "—", "(none)", "nothing", "tbd"}


def _spec_text(change_dir: Path) -> str:
    parts = [(change_dir / "proposal.md").read_text()] if (change_dir / "proposal.md").is_file() else []
    parts += [p.read_text() for p in sorted((change_dir / "requirements").glob("*.md"))]
    return " ".join(parts)


def detect_change_type(change_dir: Path) -> str:
    """`frontend`, `backend`, `fullstack` or `infra`, by keyword counts (buildme's rule)."""
    text = _spec_text(change_dir)
    if not text:
        return "backend"
    front, back, infra = (len(r.findall(text)) for r in (_FRONTEND_KEYWORDS, _BACKEND_KEYWORDS, _INFRA_KEYWORDS))
    if infra > front and infra > back:
        return "infra"
    if front > 0 and back > 0:
        if front >= 3 and back >= 3:
            return "fullstack"
        return "frontend" if front > back else "backend"
    if front > back:
        return "frontend"
    return "backend"


def prototype_required(change_dir: Path) -> bool:
    """A UI change (frontend or fullstack), or one whose proposal or requirements
    describe a user-visible output format."""
    if detect_change_type(change_dir) in ("frontend", "fullstack"):
        return True
    return bool(_OUTPUT_FORMAT_RE.search(_spec_text(change_dir)))


def decision(run: Run) -> tuple[bool, str]:
    mode = run.state.options.prototype
    if mode == "false":
        return False, "disabled (--no-prototype / prototype.enabled: false)"
    if mode == "true":
        return True, "forced on (--prototype / prototype.enabled: true)"
    if prototype_required(run.config.change_dir):
        return True, "a UI or a user-visible output format"
    return False, "not a UI or output-format change"


def parse_prototype_notes(text: str) -> dict[str, str]:
    notes: dict[str, str] = {}
    matches = list(_SLOT_HEADER_RE.finditer(text or ""))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        notes[match.group(1).lower()] = text[match.end():end].strip()
    return notes


def slot_has_content(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().strip("*_` ").lower() not in _EMPTY_SLOT_VALUES


@dataclass
class Gate:
    passed: bool
    reason: str
    action: str = ""
    artifacts: list[str] = field(default_factory=list)


def prototype_gate(prototype_dir: Path) -> Gate:
    """An artifact besides NOTES.md, and NOTES.md with PROVES (content), OPEN_QUESTIONS (non-empty),
    DISPROVES and STATUS present; STATUS BLOCKED or NEEDS_CONTEXT fails without a retry."""
    notes_path = prototype_dir / "NOTES.md"
    if not prototype_dir.is_dir():
        return Gate(False, f"Prototype directory not created: {prototype_dir}", "retry")
    if not notes_path.is_file():
        return Gate(False, f"Prototype NOTES.md missing in {prototype_dir}", "retry")
    notes_text = notes_path.read_text()
    notes = parse_prototype_notes(notes_text)
    status = (notes.get("status") or "").strip().strip("*_` ")
    word = status.split()[0].upper().rstrip(".,;:") if status else ""
    if word in ("BLOCKED", "NEEDS_CONTEXT"):
        return Gate(False, f"Prototype agent reported STATUS: {word}. Its notes:\n{notes_text[-1500:]}", "blocked")
    found = [str(p) for p in sorted(prototype_dir.rglob("*")) if p.is_file() and p != notes_path]
    if not found:
        return Gate(False, "Prototype produced no artifact file besides NOTES.md", "retry")
    missing = [s for s in PROTOTYPE_SLOTS if s.lower() not in notes]
    if missing:
        return Gate(False, f"Prototype NOTES.md is missing REQUIRED slots: {missing}", "retry", found)
    if not slot_has_content(notes.get("proves")):
        return Gate(False, "Prototype NOTES.md has an empty PROVES: slot", "retry", found)
    if not (notes.get("open_questions") or "").strip():
        return Gate(False, "Prototype NOTES.md has an empty OPEN_QUESTIONS: slot", "retry", found)
    return Gate(True, f"Prototype produced {len(found)} artifact file(s) with complete NOTES.md", "", found)


def files_outside(files_changed: list[str], prototype_dir: Path) -> list[str]:
    root = prototype_dir.resolve()
    outside = []
    for entry in files_changed or []:
        path = Path(entry)
        resolved = (path if path.is_absolute() else prototype_dir / path).resolve()
        if not resolved.is_relative_to(root):
            outside.append(str(resolved))
    return outside


def prototype_findings(notes: dict[str, str]) -> list[dict]:
    out = []
    if slot_has_content(notes.get("disproves")):
        out.append({"category": "prototype-disproves", "severity": "major",
                    "description": f"The prototype disproved part of the specs: {notes['disproves'].strip()}",
                    "suggestion": "change the proposal and the requirements to match what the prototype showed."})
    if slot_has_content(notes.get("open_questions")):
        out.append({"category": "prototype-open-questions", "severity": "minor",
                    "description": f"The prototype left open questions: {notes['open_questions'].strip()}",
                    "suggestion": "decide each one in the requirements rather than leaving it for the builder."})
    return out


async def run_prototype(run: Run) -> Gate:
    change_dir = run.config.change_dir
    prototype_dir = change_dir / "prototype"
    prototype_dir.mkdir(parents=True, exist_ok=True)
    prompt = PROTOTYPE_PROMPT.format(change_name=run.config.change_name,
                                     proposal_content=artifacts.proposal_text(run) or "(not available)",
                                     requirements_content=artifacts.all_requirements_text(change_dir),
                                     prototype_dir=prototype_dir)
    gate = Gate(False, "Prototype never ran")
    for attempt in range(1, MAX_PROTOTYPE_ATTEMPTS + 1):
        log.info("START step=PROTOTYPE attempt=%d/%d", attempt, MAX_PROTOTYPE_ATTEMPTS)
        try:
            answer = await sdk.agent_text(prompt, config=run.config, step="prototype", label="prototype",
                                          tools=PROTOTYPE_TOOLS, cwd=str(prototype_dir))
        except sdk.CallFailed as e:
            return Gate(False, f"Prototype writer failed: {e}")
        if outside := files_outside(answer.files_changed, prototype_dir):
            return Gate(False, f"Prototype agent wrote outside {prototype_dir}: {outside}", "boundary")
        gate = prototype_gate(prototype_dir)
        if gate.passed or gate.action == "blocked":
            return gate
        log.warning("Prototype gate failed (attempt %d): %s", attempt, gate.reason)
    return gate


def prototype_revision(change_dir: Path) -> str:
    parts = [(change_dir / "proposal.md").read_text() if (change_dir / "proposal.md").is_file() else ""]
    parts += [f"{p.name}\0{p.read_text()}" for p in sorted((change_dir / "requirements").glob("*.md"))]
    return artifacts.sha("\0\0".join(parts))


async def run_prototype_stage(run: Run) -> None:
    change_dir = run.config.change_dir
    step = run.state.steps.get(NAME)
    if step and step.outcome in ("passed", "skipped"):
        return
    revision = prototype_revision(change_dir)
    if step and F.rewrite_pending(run, NAME) and step.rewrite_published:
        # Row S4, after the rewrite published: go on to the consistency check.
        F.record_passed(run, NAME, revision, "Prototype findings applied")
        return
    should_run, reason = decision(run)
    if not should_run:
        log.info("SKIP phase=PROTOTYPE reason=%s", reason)
        F.record_skipped(run, NAME, revision, reason)
        return
    F.reopen_if_edited(run, NAME, revision)
    F.refuse_if_blocked(run, NAME, revision)
    print("PHASE:PROTOTYPE", flush=True)
    gate = await run_prototype(run)
    if not gate.passed:
        F.record_error(run, NAME, gate.reason)
        print(f"ERROR: prototype: {gate.reason}", flush=True)
        raise Stop(EXIT_FAILED, gate.reason)
    notes = parse_prototype_notes((change_dir / "prototype" / "NOTES.md").read_text())
    found = prototype_findings(notes)
    if not found:
        F.record_passed(run, NAME, revision, gate.reason)
        return
    step = run.state.step(NAME)
    if step.repair_attempts:
        # Row S4, stopped before the rewrite published: no second rewrite.
        F.block_on_findings(run, NAME, revision, "Prototype findings rewrite already attempted",
                            step.repair_findings or F.number_findings(NAME, found))
    numbered = F.number_findings(NAME, found)
    F.admit_repair(run, NAME, revision, numbered)
    files = await rewrite_proposal_and_requirements(run, run.state.step(NAME).repair_findings, label=NAME)
    if problems := check_texts(run, files):
        blocking, _ = F.recheck_verdict([], [], [], deterministic=problems)
        F.block_on_findings(run, NAME, revision, "The prototype rewrite broke a spec file", blocking)
    publish(run, NAME, files)
    F.record_passed(run, NAME, prototype_revision(change_dir), "Prototype findings applied")
    run.progress("PROTOTYPE_PASSED", "Prototype findings applied:\n" + F.findings_report(numbered))
