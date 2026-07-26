"""The respec loop — implementation surprises become a proposed spec delta.

Two halves, both belonging to the same loop:

- `append_implementation_note()` runs *during* the build. Every note an agent
  reports is appended to `implementation-notes.md` the moment it arrives, so a
  crashed or interrupted build still leaves its learning on disk.
- `run_respec_audit()` runs *after* the build (BuildPhase.RESPEC). It reads the
  notes plus the current requirements/design and writes `respec.md`: a proposed
  delta in the same ADDED/MODIFIED/REMOVED format `change_manager._apply_delta`
  applies.

**Propose only.** `run_respec_audit` writes exactly one file — `respec.md` —
and reaches the model through `llm_call`, which grants no tools. There is no
code path here that can edit `requirements/*.md` or `design.md`; applying a
proposed delta stays a deliberate human (or next-change) decision. Both files
travel with the change into `archive/<date>-<name>/` and are never merged into
`registry/`.

Non-fatal by construction: any failure in the audit is logged and returns None;
the build still completes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import ImplementationNote
from ..prompts.respec import RESPEC_PROMPT
from ..sdk import llm_call

log = logging.getLogger(__name__)

NOTES_FILENAME = "implementation-notes.md"
RESPEC_FILENAME = "respec.md"

DELTA_SECTIONS = (
    "## ADDED Requirements",
    "## MODIFIED Requirements",
    "## REMOVED Requirements",
)

_NOTES_HEADER = """\
# Implementation Notes

Surprises reported by the build's agents in their `SURPRISES:` / `SPEC_IMPACT:`
slots, appended as they happened. `contradicts` means the block could only be
built by doing something the spec/design does not say (or says otherwise).
These notes are the input to the end-of-build respec proposal (`respec.md`);
no spec file is edited from them automatically.
"""


def notes_path(change_dir: Path) -> Path:
    return change_dir / NOTES_FILENAME


def format_implementation_note(note: ImplementationNote) -> str:
    """One note as a markdown section."""
    body = (note.surprises or "(no detail given)").strip()
    return (
        f"\n## Block {note.block_number} — {note.block_name} ({note.role})\n"
        f"- SPEC_IMPACT: {note.spec_impact}\n"
        f"- SURPRISES:\n"
        + "\n".join(f"  {line}" for line in body.splitlines())
        + "\n"
    )


def append_implementation_note(change_dir: Path, note: ImplementationNote) -> Path:
    """Append a note to implementation-notes.md, creating the file if needed.

    Called as the build runs (not at the end) so an interrupted build keeps
    what it learned.
    """
    path = notes_path(change_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_NOTES_HEADER)
    with path.open("a") as f:
        f.write(format_implementation_note(note))
    log.info(
        "Implementation note recorded (block=%d role=%s spec_impact=%s)",
        note.block_number, note.role, note.spec_impact,
    )
    return path


def merge_state_notes(notes_md: str, state) -> str:
    """Fold the notes persisted on the build state into the markdown notes.

    ``implementation-notes.md`` is appended as the build runs, but an append
    can fail (OSError) — the copy on each block (``BlockInfo.notes``, kept in
    the checkpoint) is the fallback that makes such a failure recoverable.
    A note whose formatted section is already in the markdown is not added
    again, so a healthy build reads back exactly its file.
    """
    tdd = getattr(state, "tdd", None) if state is not None else None
    blocks = getattr(tdd, "blocks", None) or []
    merged = notes_md
    for block in blocks:
        for note in getattr(block, "notes", None) or []:
            section = format_implementation_note(note)
            if section.strip() in merged:
                continue
            merged = (
                merged.rstrip() + "\n" + section
                if merged.strip() else _NOTES_HEADER + section
            )
            log.info(
                "Respec: recovered note from state (block=%d role=%s) missing from %s",
                note.block_number, note.role, NOTES_FILENAME,
            )
    return merged


def ensure_delta_sections(text: str) -> str:
    """Guarantee respec.md carries all three delta headings.

    `change_manager._apply_delta` keys off the ADDED/MODIFIED/REMOVED headings,
    so a proposal missing one reads as malformed rather than as "nothing
    proposed here". Missing sections are appended with an explicit empty body.
    """
    out = text.rstrip()
    for section in DELTA_SECTIONS:
        if section not in out:
            out += f"\n\n{section}\n\n_None proposed._"
    return out + "\n"


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _read_requirements(change_dir: Path) -> str:
    req_dir = change_dir / "requirements"
    if not req_dir.exists():
        return ""
    parts = [
        f"### {f.stem}\n{f.read_text()}"
        for f in sorted(req_dir.glob("*.md"))
    ]
    return "\n\n".join(parts)


def _write_respec(change_dir: Path, change_name: str, body: str) -> Path:
    path = change_dir / RESPEC_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Respec Proposal: {change_name}\n\n"
        "Proposed spec delta derived from `implementation-notes.md`. "
        "**Nothing here has been applied** — no requirements or design file was "
        "modified by the build. Apply it deliberately (or fold it into the next "
        "change) using the ADDED/MODIFIED/REMOVED delta format.\n\n"
    )
    path.write_text(header + ensure_delta_sections(body))
    return path


async def run_respec_audit(config, state=None) -> Path | None:
    """Propose a spec delta from the build's implementation notes.

    Reads `implementation-notes.md` merged with the notes persisted on
    ``state``'s blocks (the fallback for an append that failed mid-build),
    writes `respec.md` inside the change directory and returns its path
    (None when the audit could not run). Never modifies any other file.
    """
    change_dir = config.change_dir
    change_name = getattr(config, "change_name", "") or change_dir.name

    notes = merge_state_notes(_read(notes_path(change_dir)), state).strip()
    if not notes:
        # A build with no recorded surprises is itself a finding: record the
        # absence rather than silently skipping the artifact.
        path = _write_respec(
            change_dir, change_name,
            "_No implementation notes were recorded during this build, so no "
            "spec delta is proposed._\n",
        )
        log.info("Respec: no implementation notes — wrote empty proposal %s", path)
        return path

    prompt = RESPEC_PROMPT.format(
        change_name=change_name,
        notes=notes,
        requirements=_read_requirements(change_dir) or "(no requirements files)",
        design=_read(change_dir / "design.md") or "(no design.md)",
    )

    try:
        body = await llm_call(
            prompt,
            model=config.model,
            system_prompt=(
                "You are a specification editor proposing a delta. Output the "
                "proposed delta document directly as markdown — do not use "
                "tools, do not edit files. Everything you need is in the prompt."
            ),
        )
    except Exception as e:  # non-fatal by construction — the build still completes
        log.warning("Respec audit LLM call failed (non-fatal): %s", e)
        return None

    path = _write_respec(change_dir, change_name, body)
    log.info("Respec proposal written to %s (%d chars)", path, len(body))
    return path
