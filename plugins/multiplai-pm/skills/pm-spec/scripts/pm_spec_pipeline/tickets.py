"""`tickets.json`: the change as an epic and one task per capability, in a neutral format.

A JSON array of items, described by `references/tickets.schema.json`. Each
item has `id` (local and stable across runs: the change name for the epic,
`<change>/<capability>` for a task), `type` (`epic` or `task`), `title`,
`description`, `priority` (`none` | `low` | `medium` | `high` | `urgent`),
`labels`, `parent` (an item id or null), `acceptance` (a checklist) and `links`
(repo-relative paths), plus `ref` for an item that came in as a ref.

- The epic: the proposal's `# ` title (else the change name) and its `## Why`.
  Its `ref` is the ticket ref the change came in with, as recorded in
  `.change.yaml` (`ticket_refs.epic`): `--ticket-ref`, or for a child change of
  an epic split, the `--ticket-child` ref the split gave it (checked in code).
- One task per capability, under the epic: the requirement and scenario names
  of `requirements/<cap>.md` as `acceptance`, a link to that file. A task has
  no `ref`: no recorded ref maps to a capability, and a model-written line is
  not a record.
- Priority: a `Priority:` line in `prd.md`, else in `ticket.md`, else `none`;
  tasks take the epic's.
- Labels: the change name, and on tasks the capability name.

The file names no ticket system: mapping it into one is someone else's job.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from multiplai_core.specs import expected_capabilities, parse_requirements

from . import intake

FILE = "tickets.json"
PRIORITIES = ("none", "low", "medium", "high", "urgent")
_PRIORITY_RE = re.compile(
    r"^[\s>*_#-]*priority[*_\s]*[:=][\s*_`]*([A-Za-z0-9-]+)",
    re.IGNORECASE | re.MULTILINE,
)
_PRIORITY_WORDS = {
    "none": "none",
    "low": "low",
    "lowest": "low",
    "minor": "low",
    "p3": "low",
    "p4": "low",
    "medium": "medium",
    "normal": "medium",
    "moderate": "medium",
    "p2": "medium",
    "high": "high",
    "major": "high",
    "p1": "high",
    "urgent": "urgent",
    "critical": "urgent",
    "highest": "urgent",
    "blocker": "urgent",
    "p0": "urgent",
}


def stated_priority(text: str) -> str | None:
    """The first `Priority: <word>` line's value, mapped onto `PRIORITIES`; None when none is stated."""
    for m in _PRIORITY_RE.finditer(text or ""):
        value = _PRIORITY_WORDS.get(m.group(1).lower())
        if value:
            return value
    return None


def change_priority(change_dir: Path) -> str:
    for name in ("prd.md", "ticket.md"):
        path = change_dir / name
        if path.is_file() and (value := stated_priority(path.read_text())):
            return value
    return "none"


def _title(proposal: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", proposal, re.MULTILINE)
    title = m.group(1).strip() if m else ""
    return title if title and not title.startswith("<!--") else fallback


def _section(text: str, heading: str) -> str:
    m = re.search(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL).strip() if m else ""
    return body


def acceptance(requirements_text: str) -> list[str]:
    items = []
    for req in parse_requirements(requirements_text):
        prefix = "Removed requirement" if req.section == "REMOVED" else "Requirement"
        items.append(f"{prefix}: {req.name}")
        items += [f"Scenario: {s.name}" for s in req.scenarios]
    return items


def build(change_dir: Path, project_dir: Path) -> list[dict]:
    change = change_dir.name
    rel_dir = change_dir.resolve().relative_to(Path(project_dir).resolve()).as_posix()
    proposal = (change_dir / "proposal.md").read_text()
    refs = intake.ticket_refs(change_dir)
    priority = change_priority(change_dir)
    epic = {
        "id": change,
        "type": "epic",
        "title": _title(proposal, change),
        "description": _section(proposal, "Why") or _section(proposal, "What Changes"),
        "priority": priority,
        "labels": [change],
        "parent": None,
        "acceptance": [],
        "links": [f"{rel_dir}/proposal.md"]
        + (
            [f"{rel_dir}/use-cases.md"]
            if (change_dir / "use-cases.md").is_file()
            else []
        ),
    }
    if refs["epic"]:
        epic["ref"] = refs["epic"]
    items = [epic]
    for cap in expected_capabilities(proposal):
        path = change_dir / "requirements" / f"{cap}.md"
        text = path.read_text() if path.is_file() else ""
        names = [r.name for r in parse_requirements(text)]
        task = {
            "id": f"{change}/{cap}",
            "type": "task",
            "title": cap,
            "description": f"Requirements of `{cap}`: "
            + ("; ".join(names) if names else "(none)"),
            "priority": priority,
            "labels": [change, cap],
            "parent": change,
            "acceptance": acceptance(text),
            "links": [f"{rel_dir}/requirements/{cap}.md"],
        }
        items.append(task)
    return items


def write(change_dir: Path, project_dir: Path) -> Path:
    from .publish import atomic_write

    path = change_dir / FILE
    atomic_write(path, json.dumps(build(change_dir, project_dir), indent=2) + "\n")
    return path
