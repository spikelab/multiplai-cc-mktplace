"""Prototype-first stage — one cheap artifact that proves the shape.

Two steps live here:

- `run_prototype(config)` spawns a single agent that writes a mockup / sample
  output / CLI transcript plus NOTES.md inside `specs/changes/<name>/prototype/`
  and nowhere else. The write boundary is enforced here in code
  (`_files_outside`), not only stated in the prompt.
- `apply_prototype_findings(config)` folds the notes' DISPROVES /
  OPEN_QUESTIONS content back into design.md and tasks.md with exactly one
  regeneration pass each — the same `generate_artifact(..., audit_findings=...)`
  mechanism the tasks-shape audit uses.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..change_manager import ChangeManager
from ..gates import (
    parse_prototype_notes,
    prototype_gate,
    slot_has_content,
)
from ..models import GateResult
from ..prompts.prototype import PROTOTYPE_PROMPT
from ..sdk import agent_call

log = logging.getLogger(__name__)

PROTOTYPE_TOOLS = ["Read", "Write", "Glob", "Grep"]
MAX_PROTOTYPE_ATTEMPTS = 2  # first pass + one retry, then the phase fails


async def run_prototype(config) -> GateResult:
    """Produce the prototype artifact + NOTES.md. Returns the gate's verdict.

    One retry is allowed. A failed writer blocks the build because it may
    have left partial edits; the caller preserves the checkpoint for resume.
    """
    prototype_dir = config.prototype_dir
    prototype_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_prototype_prompt(config, prototype_dir)

    result: GateResult = GateResult(passed=False, reason="Prototype never ran")
    for attempt in range(1, MAX_PROTOTYPE_ATTEMPTS + 1):
        log.info("START step=PROTOTYPE attempt=%d/%d", attempt, MAX_PROTOTYPE_ATTEMPTS)
        agent = await agent_call(
            prompt,
            allowed_tools=PROTOTYPE_TOOLS,
            model=config.model_for("prototype"),
            effort=config.effort_for("prototype"),
            cwd=str(prototype_dir),
        )

        outside = _files_outside(agent.files_changed, prototype_dir)
        if outside:
            log.error(
                "Prototype agent wrote outside its directory: %s", outside,
            )
            return GateResult(
                passed=False,
                reason=(
                    "Prototype agent wrote outside "
                    f"{prototype_dir}: {outside}. The prototype stage may only "
                    "produce files under the change's prototype/ directory."
                ),
                action="prototype_write_boundary",
                metadata={"outside": outside},
            )

        if not agent.success:
            return GateResult(passed=False, reason=f"Prototype writer failed: {agent.error}")

        result = prototype_gate(prototype_dir)
        if result.passed:
            log.info("DONE step=PROTOTYPE attempt=%d — %s", attempt, result.reason)
            return result
        if result.action == "prototype_blocked":
            # The agent honestly reported BLOCKED/NEEDS_CONTEXT — the specs do
            # not say enough to draw the shape. A retry re-asks the same
            # unanswerable question, so fail the phase now with its stated
            # blocker instead of a misleading empty-slot failure.
            log.warning("Prototype agent blocked (attempt %d): %s", attempt, result.reason)
            return result
        log.warning("Prototype gate failed (attempt %d): %s", attempt, result.reason)

    return result


def _build_prototype_prompt(config, prototype_dir: Path) -> str:
    change_dir = config.change_dir
    return PROTOTYPE_PROMPT.format(
        change_name=config.change_name,
        proposal_content=_read(change_dir / "proposal.md"),
        design_content=_read(change_dir / "design.md"),
        prototype_dir=prototype_dir,
    )


def _files_outside(files_changed: list[str], prototype_dir: Path) -> list[str]:
    """Paths the agent reported touching that are not under prototype_dir.

    Relative paths are resolved against prototype_dir (the agent's cwd), which
    is how the SDK reports them for a cwd-scoped run.
    """
    root = prototype_dir.resolve()
    outside: list[str] = []
    for entry in files_changed or []:
        path = Path(entry)
        resolved = (path if path.is_absolute() else prototype_dir / path).resolve()
        if not resolved.is_relative_to(root):
            outside.append(str(resolved))
    return outside


def read_prototype_notes(prototype_dir: Path) -> dict[str, str]:
    """The parsed NOTES.md slots ({} when there is no NOTES.md)."""
    notes_path = prototype_dir / "NOTES.md"
    if not notes_path.exists():
        return {}
    return parse_prototype_notes(notes_path.read_text())


def prototype_findings(notes: dict[str, str]) -> list[dict]:
    """The notes' DISPROVES and OPEN QUESTIONS slots as findings for the one
    design/tasks rewrite (empty when neither has content)."""
    findings = []
    if slot_has_content(notes.get("disproves")):
        findings.append({
            "category": "prototype-disproves", "severity": "major",
            "description": f"The prototype disproved part of the current design: {notes['disproves'].strip()}",
            "suggestion": "change the design/tasks to match what the prototype showed."})
    if slot_has_content(notes.get("open_questions")):
        findings.append({
            "category": "prototype-open-questions", "severity": "minor",
            "description": f"The prototype left open questions: {notes['open_questions'].strip()}",
            "suggestion": "decide each one explicitly in the document rather than leaving it for the implementer."})
    return findings


def prototype_findings_text(notes: dict[str, str], findings: list[dict] | None = None) -> str:
    """The audit-findings block fed back into design/tasks regeneration.

    Empty string when the prototype surfaced nothing actionable — the caller
    treats that as "no regeneration needed". `findings` are the notes'
    findings with ids (`spec_generator.number_findings`); each line then
    starts with its finding's id.
    """
    parts = [
        "- " + (f"{f['finding_id']} " if f.get("finding_id") else "")
        + f"[{f['severity']}] {f['description']} Fix: {f['suggestion']}"
        for f in (prototype_findings(notes) if findings is None else findings)
    ]
    if slot_has_content(notes.get("proves")):
        parts.append(
            "- [note] The prototype established this shape — keep the design "
            f"consistent with it: {notes['proves'].strip()}"
        )
    if not any(
        slot_has_content(notes.get(k)) for k in ("disproves", "open_questions")
    ):
        return ""
    return "\n".join(parts)


def primary_prototype_artifact(prototype_dir: Path) -> Path | None:
    """The artifact a human should open first — the mockup if there is one.

    Returned for the non-`--auto` review checkpoint, which prints it as a
    `file://` URL: the pipeline runs in a container whose localhost is not the
    user's, so the shared filesystem mount is the reliable channel.
    """
    if not prototype_dir.exists():
        return None
    files = [
        p for p in sorted(prototype_dir.rglob("*"))
        if p.is_file() and p.name != "NOTES.md"
    ]
    if not files:
        return None
    for suffix in (".html", ".htm", ".md", ".txt"):
        for path in files:
            if path.suffix.lower() == suffix:
                return path
    return files[0]


async def apply_prototype_findings(config, state=None) -> int:
    """Send prototype feedback to the planning owner for one coherent revision.

    The prototype gets one rewrite, like the audits: its numbered findings are
    saved as it is admitted, and a resume after that records the step blocked
    on them instead of rewriting again. A rewrite that still has blocking
    findings is recorded blocked on them.
    """
    from ..spec_generator import (
        PlanBlocked, PlanRepairRejected, _load_or_create_state, block_on_findings, number_findings,
        record_advisory, rewrite_plan, saved_findings,
    )
    notes = read_prototype_notes(config.prototype_dir)
    if not prototype_findings_text(notes):
        return 0
    name = "prototype"
    state = state or _load_or_create_state(config)
    sg = state.spec_gen
    assert sg is not None
    state_path = config.state_file_path()
    numbered = number_findings(name, prototype_findings(notes))
    if sg.repair_attempts(name):
        reason = "Prototype feedback repair already attempted"
        block_on_findings(config, state, state_path, name, reason, sg.steps[name].repair_findings or numbered)
        raise PlanBlocked(reason)
    sg.admit_repair(name, saved_findings(numbered))
    state.checkpoint(state_path)
    try:
        return await rewrite_plan(config, state, prototype_findings_text(notes, numbered), design=True, step=name)
    except PlanRepairRejected as rejection:
        record_advisory(config, name, rejection.advisory)
        block_on_findings(config, state, state_path, name, str(rejection), rejection.blocking)
        raise


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else "(not available)"
