"""What a change starts from, saved in the change dir so it travels with the change.

- `prd.md`   — a PRD passed as `--prd`, the primary source when there is one
- `ticket.md` — a ticket (an epic or work item and its children) passed as
  text with `--ticket-file`, fenced as untrusted; its refs (`--ticket-ref`,
  `--ticket-child`) are kept in `.change.yaml` as `ticket_refs`, opaque strings
- `plan.md`  — a plan passed as `--plan-file`
- `interview.md` and `interview-questions.md` — written by the interview

Copied from buildme's `intake.py` (`save_ticket`, `save_plan`,
`render_interview`, the questions file), then adapted. Whoever runs pm-spec
fetches the ticket; the pipeline only ever sees its text, and names no ticket
system.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from multiplai_core.specs import read_change_meta, write_change_meta
from multiplai_core.untrusted import fence

log = logging.getLogger(__name__)


def fenced(text: str, source: str) -> str:
    """*text* inside an untrusted-content fence, as one string."""
    return "\n".join(fence(text, source))


def absolute(raw: str, base: Path) -> Path:
    """*raw* as an absolute path; a relative one is read from *base* (`--project-dir`)."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(base) / path


def create_change(change_dir: Path, *, parent: str | None = None) -> None:
    """The change dir and its `.change.yaml`, with `spec_status: draft` when new.
    An existing `.change.yaml` keeps every key it has."""
    change_dir.mkdir(parents=True, exist_ok=True)
    meta = read_change_meta(change_dir)
    updates: dict = {}
    if not meta:
        updates = {"schema": "spec-driven", "created": date.today().isoformat()}
    if "spec_status" not in meta:
        updates["spec_status"] = "draft"
    if parent and meta.get("parent") != parent:
        updates["parent"] = parent
    if updates:
        write_change_meta(change_dir, updates)


def save_ticket(change_dir: Path, ticket_file: Path, *, ref: str = "", url: str = "",
                children: list[str] | None = None) -> Path:
    """Copy the ticket text to `ticket.md`, fenced as untrusted, and record its refs."""
    source = url or ref or ticket_file.name
    header = [f"# Ticket {ref}".rstrip(), ""]
    if url:
        header += [f"Source: {url}", ""]
    if children:
        header += ["Children: " + ", ".join(children), ""]
    dest = change_dir / "ticket.md"
    dest.write_text("\n".join(header) + fenced(ticket_file.read_text(), source) + "\n")
    if ref or children:
        write_change_meta(change_dir, {"ticket_refs": {"epic": ref or None, "children": list(children or [])}})
    log.info("Saved ticket %s to %s", ref or ticket_file.name, dest)
    return dest


def save_copy(change_dir: Path, src: Path, name: str) -> Path:
    dest = change_dir / name
    dest.write_text(src.read_text())
    log.info("Saved %s to %s", src, dest)
    return dest


def ticket_refs(change_dir: Path) -> dict:
    """`{"epic": <ref or None>, "children": [<ref>, ...]}` from `.change.yaml`."""
    raw = read_change_meta(change_dir).get("ticket_refs") or {}
    if not isinstance(raw, dict):
        return {"epic": None, "children": []}
    children = raw.get("children") or []
    return {"epic": raw.get("epic") or None,
            "children": [str(c) for c in children] if isinstance(children, list) else []}


def read_context_files(paths: list[str]) -> list[tuple[str, str]]:
    """`(name, content)` for each context file that exists."""
    out = []
    for raw in paths or []:
        path = Path(raw)
        if path.is_file():
            out.append((path.name, path.read_text()))
        else:
            log.warning("Context file not found: %s", raw)
    return out


def prd_text(change_dir: Path) -> str:
    path = change_dir / "prd.md"
    return path.read_text() if path.is_file() else ""


def prd_headings(text: str) -> list[str]:
    """The text of every markdown heading in a PRD, in order."""
    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.MULTILINE)]


def interview_inputs(change_dir: Path, summary: str, contexts: list[tuple[str, str]]) -> str:
    """What the interview is about: PRD (fenced), ticket (already fenced), plan, context, summary."""
    parts = []
    prd = prd_text(change_dir)
    if prd:
        parts.append("## PRD\n" + fenced(prd, "prd.md"))
    ticket = change_dir / "ticket.md"
    if ticket.is_file():
        parts.append("## Ticket\n" + ticket.read_text())
    plan = change_dir / "plan.md"
    if plan.is_file():
        parts.append("## Plan\n" + plan.read_text())
    for name, content in contexts:
        parts.append(f"## Context: {name}\n{content}")
    if summary:
        parts.append(f"## Interview\n{summary}")
    return "\n\n".join(parts)


def render_interview(change_dir: Path, *, summary: str = "", contexts: list[tuple[str, str]] | None = None,
                     questions: list[str] | None = None, answers: list | None = None, replies: str = "",
                     replies_source: str = "ticket replies") -> str:
    """The text of `interview.md`.

    Sections, each only when it has content: a pointer to `prd.md`, the ticket
    (already fenced), `## Plan`, one `## Context: <name>` per context file,
    `## Interview` for a summary, then `## Questions` / `## Answers` /
    `## Assumptions` (self mode) or `## Questions` / `## Replies` (ticket mode).
    An answer with empty `grounded_in` goes under Assumptions, never Answers.
    """
    out = ["# Interview", ""]
    if (change_dir / "prd.md").is_file():
        out += ["## PRD", "", "The PRD is `prd.md` in this change dir; it is the primary source.", ""]
    ticket = change_dir / "ticket.md"
    if ticket.is_file():
        out += ["## Ticket", "", ticket.read_text().rstrip(), ""]
    plan = change_dir / "plan.md"
    if plan.is_file():
        out += ["## Plan", "", plan.read_text().rstrip(), ""]
    for name, content in contexts or []:
        out += [f"## Context: {name}", "", content.rstrip(), ""]
    if summary:
        out += ["## Interview", "", summary.rstrip(), ""]
    if questions:
        out += ["## Questions", ""]
        out += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        out.append("")
    if answers:
        grounded = [a for a in answers if not a.is_assumption]
        assumed = [a for a in answers if a.is_assumption]
        if grounded:
            out += ["## Answers", ""]
            for a in grounded:
                out += [f"**Q: {a.question}**", "", a.answer.rstrip(), "",
                        f"_Grounded in: {a.grounded_in.strip()}_", ""]
        if assumed:
            out += ["## Assumptions", "",
                    "Nothing in the inputs decides these; each answer is an assumption to confirm.", ""]
            for a in assumed:
                out += [f"**Q: {a.question}**", "", a.answer.rstrip(), ""]
    if replies:
        out += ["## Replies", "", fenced(replies, replies_source), ""]
    return "\n".join(out).rstrip() + "\n"


def read_questions(path: Path) -> list[str]:
    """Questions back out of `interview-questions.md` (numbered lines)."""
    if not path.is_file():
        return []
    return [m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", path.read_text(), re.MULTILINE)]


def write_questions(path: Path, questions: list[str], ref: str = "") -> None:
    lines = [f"# Questions for {ref}".rstrip() if ref else "# Questions", "",
             "pm-spec needs these answered before it writes the spec. "
             "Post them on the ticket; pass the replies to the next run with --interview-replies.", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    path.write_text("\n".join(lines) + "\n")
