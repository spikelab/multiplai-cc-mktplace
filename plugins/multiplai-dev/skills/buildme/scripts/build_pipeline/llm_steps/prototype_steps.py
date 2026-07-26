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

    Failure is the *phase's* failure, never the build's — the caller logs a
    diagnosis and continues. One retry, then stop (a second failing attempt is
    evidence the change is not prototypable, not a reason to loop).
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
            model=config.model,
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
            log.warning(
                "Prototype agent call failed (attempt %d): %s", attempt, agent.error,
            )

        result = prototype_gate(prototype_dir)
        if result.passed:
            log.info("DONE step=PROTOTYPE attempt=%d — %s", attempt, result.reason)
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


def prototype_findings_text(notes: dict[str, str]) -> str:
    """The audit-findings block fed back into design/tasks regeneration.

    Empty string when the prototype surfaced nothing actionable — the caller
    treats that as "no regeneration needed".
    """
    parts = []
    if slot_has_content(notes.get("disproves")):
        parts.append(
            "- [major] The prototype disproved part of the current design: "
            f"{notes['disproves'].strip()} "
            "Fix: change the design/tasks to match what the prototype showed."
        )
    if slot_has_content(notes.get("open_questions")):
        parts.append(
            "- [minor] The prototype left open questions: "
            f"{notes['open_questions'].strip()} "
            "Fix: decide each one explicitly in the document rather than "
            "leaving it for the implementer."
        )
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


async def apply_prototype_findings(config) -> int:
    """Regenerate design.md and tasks.md ONCE from the prototype's notes.

    Returns the number of artifacts regenerated (0 when the notes carry nothing
    actionable). No re-audit loop: one pass, then the documents stand. A
    regeneration failure is non-fatal — the existing artifact stays.
    """
    from .spec_steps import generate_artifact

    notes = read_prototype_notes(config.prototype_dir)
    findings_text = prototype_findings_text(notes)
    if not findings_text:
        log.info("SKIP step=PROTOTYPE_FEEDBACK reason=no-actionable-findings")
        return 0

    cm = ChangeManager(config.specs_dir)
    change_dir = config.change_dir
    regenerated = 0

    for artifact_id in ("design", "tasks"):
        context = cm.artifact_context(change_dir, artifact_id)
        output_path = change_dir / context["output_path"]
        if not output_path.exists():
            log.warning(
                "SKIP prototype feedback for %s — %s does not exist",
                artifact_id, output_path,
            )
            continue
        try:
            content = await generate_artifact(
                artifact_id, context, config, audit_findings=findings_text,
            )
        except Exception as regen_err:  # non-fatal: first pass stands
            log.warning(
                "Prototype feedback regeneration of %s failed (non-fatal): %s",
                artifact_id, regen_err,
            )
            continue
        output_path.write_text(content)
        regenerated += 1
        log.info("Rewrote %s from prototype findings", output_path)

    print(f"PHASE: prototype_feedback_applied — {regenerated} artifact(s)", flush=True)
    return regenerated


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else "(not available)"
