"""`tickets.json`: the change's work items in a neutral format (stage 3).

A JSON array of items, described by `references/tickets.schema.json`. Each
item has `id` (local and stable across runs), `type` (`epic` or `task`),
`title`, `description`, `priority` (`none` | `low` | `medium` | `high` |
`urgent`), `labels`, `parent` (an item id or null), `acceptance` (a
checklist) and `links` (repo-relative paths), plus `ref` once the item has a
ticket.

- A change with several components: the change is the `epic` (id: the change
  name; the proposal's `# ` title and its `## Why`; `ref`: the change's ticket,
  `ticket_refs.epic` in `.change.yaml`), and each component is a `task` under
  it (id `<change>/<component>`; its plan's `# Plan:` title, its Objective,
  its `## Done means` as `acceptance`, a link to its `plan.md`; `ref`: the
  component's own `ticket_refs.epic`, once it has one).
- A change with one component: one `task` with no parent, the change's ticket
  as `ref`: that ticket is rewritten, and nothing new is created.
- Priority: a `Priority:` line in `prd.md`, else in `ticket.md`, else `none`;
  tasks take the epic's.
- Labels: the change name, and on tasks the component id.

`resume --ticket <component>=<REF>` records each component's ticket
(`apply_refs`): `ticket_refs` in its `.change.yaml`, the Ticket column of
`components.md`, and `ref` in `tickets.json`. The file names no ticket system:
creating the items in one is the caller's job.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from multiplai_core.specs import (
    component_dirs,
    parse_requirements,
    read_change_meta,
    requirement_texts,
    write_change_meta,
)

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


def _oneline(text: str) -> str:
    return " ".join(text.split())


def plan_title(plan: str, fallback: str) -> str:
    m = re.search(r"^#\s+Plan:\s*(.+?)\s*$", plan, re.MULTILINE)
    return m.group(1) if m else fallback


def objective(plan: str) -> str:
    """The plan's `**Objective:**` paragraph, on one line."""
    m = re.search(r"\*\*Objective:?\*\*:?\s*(.*?)(?=\n\s*\n|\Z)", plan, re.DOTALL)
    return _oneline(m.group(1)) if m else ""


_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)]|[a-z][.)])\s+\S")


def done_means(plan: str) -> list[str]:
    """The numbered criteria of the plan's `## Done means`, one entry each.

    A wrapped line joins the line above it with a space. A list nested under
    a criterion stays in that criterion's entry, each sub-item on its own line,
    indented as in the plan less the criterion's own indent. A numbered line is
    a new criterion unless it is indented as far as the criterion's text."""
    items: list[str] = []
    indent = 0  # where the current criterion's text starts
    for line in _section(plan, "Done means").splitlines():
        top = re.match(r"^(\s*)\d+[.)]\s+(.*)$", line)
        if top and (not items or len(top.group(1)) < indent):
            items.append(top.group(2).strip())
            indent = top.start(2)
        elif not line.strip() or not items:
            continue
        elif m := _LIST_ITEM_RE.match(line):
            depth = max(len(m.group(1)) - indent, 0)
            items[-1] += "\n" + " " * depth + line.strip()
        else:
            items[-1] += " " + line.strip()
    return items


def acceptance(requirements_text: str) -> list[str]:
    items = []
    for req in parse_requirements(requirements_text):
        prefix = "Removed requirement" if req.section == "REMOVED" else "Requirement"
        items.append(f"{prefix}: {req.name}")
        items += [f"Scenario: {s.name}" for s in req.scenarios]
    return items


def _read(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def own_ref(spec_dir: Path) -> str | None:
    raw = read_change_meta(spec_dir).get("ticket_refs") or {}
    value = raw.get("epic") if isinstance(raw, dict) else None
    return str(value) if value else None


def build(change_dir: Path, project_dir: Path) -> list[dict]:
    change = change_dir.name
    rel_dir = change_dir.resolve().relative_to(Path(project_dir).resolve()).as_posix()
    proposal = _read(change_dir / "proposal.md")
    ref = intake.ticket_refs(change_dir)["epic"]
    priority = change_priority(change_dir)
    links = [f"{rel_dir}/proposal.md"]
    comps = component_dirs(change_dir)
    if not comps:
        plan = _read(change_dir / "plan.md")
        if plan:
            accept = done_means(plan)
            links.append(f"{rel_dir}/plan.md")
        else:  # a change in the old layout, adopted: its requirement names
            accept = [
                a
                for text in requirement_texts(change_dir).values()
                for a in acceptance(text)
            ]
        item = {
            "id": change,
            "type": "task",
            "title": _title(proposal, change),
            "description": _section(proposal, "Why")
            or _section(proposal, "What Changes"),
            "priority": priority,
            "labels": [change],
            "parent": None,
            "acceptance": accept,
            "links": links,
        }
        if ref:
            item["ref"] = ref
        return [item]
    epic = {
        "id": change,
        "type": "epic",
        "title": _title(proposal, change),
        "description": _section(proposal, "Why") or _section(proposal, "What Changes"),
        "priority": priority,
        "labels": [change],
        "parent": None,
        "acceptance": [],
        "links": links,
    }
    if ref:
        epic["ref"] = ref
    items = [epic]
    for comp in _ordered(change_dir, comps):
        plan = _read(comp / "plan.md")
        task = {
            "id": f"{change}/{comp.name}",
            "type": "task",
            "title": plan_title(plan, comp.name),
            "description": objective(plan),
            "priority": priority,
            "labels": [change, comp.name],
            "parent": change,
            "acceptance": done_means(plan),
            "links": [f"{rel_dir}/components/{comp.name}/plan.md"],
        }
        if own := own_ref(comp):
            task["ref"] = own
        items.append(task)
    return items


def _ordered(change_dir: Path, comps: list[Path]) -> list[Path]:
    """*comps* in the order of the change's `children`, then any it does not list."""
    listed = read_change_meta(change_dir).get("children") or []
    by_name = {c.name: c for c in comps}
    out = [by_name.pop(n) for n in listed if isinstance(n, str) and n in by_name]
    return out + sorted(by_name.values())


def write(change_dir: Path, project_dir: Path) -> Path:
    from .publish import atomic_write

    path = change_dir / FILE
    atomic_write(path, json.dumps(build(change_dir, project_dir), indent=2) + "\n")
    return path


def pending(change_dir: Path) -> list[str]:
    """The components with no ticket of their own yet, in order; `[]` for a
    change with one component (its ticket is the change's)."""
    comps = component_dirs(change_dir)
    return [c.name for c in _ordered(change_dir, comps) if not own_ref(c)]


def _set_ticket_cell(text: str, component: str, ref: str) -> str:
    """*text* (`components.md`) with the Ticket cell of *component*'s row set to *ref*."""
    out = []
    for line in text.splitlines(keepends=True):
        cells = line.rstrip("\n").split("|")
        if len(cells) >= 5 and cells[1].strip().strip("`") == component:
            width = max(len(cells[3]) - 2, len(ref))
            cells[3] = f" {ref.ljust(width)} "
            line = "|".join(cells) + ("\n" if line.endswith("\n") else "")
        out.append(line)
    return "".join(out)


def apply_refs(change_dir: Path, refs: dict[str, str]) -> None:
    """Record each component's ticket: its `.change.yaml`, `components.md` and `tickets.json`."""
    from .publish import atomic_write

    table = change_dir / "components.md"
    for component, ref in refs.items():
        comp = change_dir / "components" / component
        write_change_meta(comp, {"ticket_refs": {"epic": ref, "children": []}})
        if table.is_file():
            atomic_write(table, _set_ticket_cell(table.read_text(), component, ref))
    path = change_dir / FILE
    if path.is_file():
        items = json.loads(path.read_text())
        for item in items:
            component = item.get("id", "").partition("/")[2]
            if component in refs:
                item["ref"] = refs[component]
        atomic_write(path, json.dumps(items, indent=2) + "\n")
