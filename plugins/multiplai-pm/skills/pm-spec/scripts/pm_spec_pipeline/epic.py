"""The components step of stage 2: one component, or several, each with its plan.

This replaces the epic split. One toolless structured call reads the ticket,
the PRD, the interview, the approved proposal and the use cases, and answers
one or more components, each with a plan (the plan format is unchanged). The
answer is checked in code: unique lowercase hyphenated ids, every dependency
names a component, no cycle, each plan holds `# Plan:` and `## Done means`;
for the ticket's recorded child refs (`--ticket-child`), a `ticket_ref` is one
of them and each of them is the `ticket_ref` of exactly one component; with
`--no-epic-split`, exactly one component. One retry lists the problems.

The answer is saved in the run state (`split`) before any file is written. A
run that finds it makes no call: it writes the files that are missing and
keeps the ones written (row S5).

Several components:

- `components/<id>/plan.md`, and `components/<id>/.change.yaml` with
  `spec_status: draft`, `depends_on` (the ids of the components it depends
  on, `[]` for none), the feature branch (`branch`) and, for a component that
  delivers a ticket child item, `ticket_refs`;
- `components.md`, a table of component, depends on and ticket (`pending`
  until it has one), in dependency order, then why the work is split;
- `children` in the change's `.change.yaml`: the component ids in that order.

One component: its `plan.md` in the change dir, and nothing else. One run
covers every component: none has a run of its own.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path

from multiplai_core.specs import read_change_meta, write_change_meta
from pydantic import BaseModel, Field

from . import intake, sdk
from .artifacts import plan_title, sha
from .prompts import UNTRUSTED_NOTICE
from .prompts.epic_split import (
    CHILDREN_RULE,
    EPIC_SPLIT_PROMPT,
    EPIC_SPLIT_RETRY,
    ONE_COMPONENT_RULE,
)
from .publish import atomic_write
from .run import Run
from .state import EpicChild, SplitRecord

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLAN_TITLE = "# Plan:"
PLAN_DONE = "## Done means"
COMPONENTS_FILE = "components.md"
COMPONENTS_DIR = "components"
PENDING = "pending"


class SplitAnswer(BaseModel):
    reason: str = ""
    components: list[EpicChild] = Field(default_factory=list)


def split_inputs(run: Run) -> str:
    change_dir = run.config.change_dir
    parts = []
    prd = intake.prd_text(change_dir)
    if prd:
        parts.append("## PRD\n" + intake.fenced(prd, "prd.md"))
    for name, heading in (
        ("ticket.md", "Ticket"),
        ("interview.md", "Interview"),
        ("proposal.md", "Proposal (approved)"),
        ("use-cases.md", "Use cases"),
    ):
        path = change_dir / name
        if path.is_file():
            parts.append(f"## {heading}\n{path.read_text()}")
    return "\n\n".join(parts) or "## Input\n(no ticket, PRD, interview or proposal was given)"


def inputs_hash(run: Run) -> str:
    children = intake.ticket_refs(run.config.change_dir)["children"]
    return sha(json.dumps({"inputs": split_inputs(run), "children": children}, sort_keys=True))


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return path[path.index(dep) :] + [dep]
            if dep not in state and (found := visit(dep)):
                return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return None


def check_answer(answer: SplitAnswer, ticket_children: list[str], *, one: bool = False) -> list[str]:
    comps = answer.components
    if not comps:
        return ["the answer gave no component; give at least one, with its plan"]
    if one and len(comps) != 1:
        return [f"the work is kept as one component; the answer gave {len(comps)}"]
    errors: list[str] = []
    seen: set[str] = set()
    for c in comps:
        if not ID_RE.match(c.id):
            errors.append(f"component id {c.id!r} is not lowercase letters and digits joined by hyphens")
        if c.id in seen:
            errors.append(f"component id {c.id!r} is used twice")
        seen.add(c.id)
        if c.ticket_ref and c.ticket_ref not in ticket_children:
            errors.append(
                f"component {c.id} names ticket_ref {c.ticket_ref!r}, which is not one of the ticket's "
                f"child items ({', '.join(ticket_children) or 'none'})"
            )
    given = [c.ticket_ref for c in comps if c.ticket_ref]
    for ref in ticket_children:
        if ref not in given:
            errors.append(
                f"the ticket's child item {ref} has no component: give it one, with `ticket_ref` {ref!r}"
            )
        elif given.count(ref) > 1:
            errors.append(f"the ticket's child item {ref} is given twice; it belongs to one component")
    for c in comps:
        for dep in c.depends_on:
            if dep not in seen:
                errors.append(f"component {c.id} depends on {dep!r}, which is not a component")
    if cycle := _cycle({c.id: [d for d in c.depends_on if d in seen] for c in comps}):
        errors.append("the dependencies have a cycle: " + " -> ".join(cycle))
    for c in comps:
        if PLAN_TITLE not in c.plan:
            errors.append(f"component {c.id}: the plan has no `{PLAN_TITLE}` line")
        if PLAN_DONE not in c.plan:
            errors.append(f"component {c.id}: the plan has no `{PLAN_DONE}` section")
    return errors


def waves(children: list[EpicChild]) -> list[str]:
    """Component ids in an order where each comes after everything it depends on."""
    done: list[str] = []
    left = list(children)
    while left:
        ready = [c for c in left if all(d in done for d in c.depends_on)] or left
        done.extend(c.id for c in ready)
        left = [c for c in left if c.id not in done]
    return done


async def ask_split(run: Run, ticket_children: list[str]) -> SplitAnswer:
    one = not run.state.options.epic_split
    errors: list[str] = []
    for attempt in (1, 2):
        prompt = EPIC_SPLIT_PROMPT.format(
            notice=UNTRUSTED_NOTICE,
            inputs=split_inputs(run),
            date=datetime.date.today().isoformat(),
            children_rule=CHILDREN_RULE.format(children=", ".join(ticket_children))
            if ticket_children
            else "",
            one_rule=ONE_COMPONENT_RULE if one else "",
            errors=EPIC_SPLIT_RETRY.format(errors="\n".join(f"- {e}" for e in errors))
            if errors
            else "",
        )
        answer = await sdk.text_structured(
            prompt, SplitAnswer, config=run.config, step="epic_split", label="components"
        )
        errors = check_answer(answer, ticket_children, one=one)
        if not errors:
            return answer
        log.warning("components answer %d rejected: %s", attempt, "; ".join(errors))
    raise ValueError("The components answer was rejected twice: " + "; ".join(errors))


_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def components_table(rows: list[tuple[str, str, str]]) -> str:
    """The `| Component | Depends on | Ticket |` table, padded."""
    header = ("Component", "Depends on", "Ticket")
    widths = [max(len(r[i]) for r in [header, *rows]) + 1 for i in range(3)]

    def line(cells) -> str:
        return "|" + "|".join(f" {c.ljust(w)} " for c, w in zip(cells, widths)) + "|"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(header), sep, *(line(r) for r in rows)])


def components_md(title: str, split: SplitRecord) -> str:
    by_id = {c.id: c for c in split.children}
    rows = [
        (cid, ", ".join(by_id[cid].depends_on), by_id[cid].ticket_ref or PENDING)
        for cid in waves(split.children)
    ]
    n = len(rows)
    count = _NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else str(n)
    text = f"# Components: {title}\n\n{components_table(rows)}\n"
    if split.reason.strip():
        text += f"\nWhy {count}: {split.reason.strip()}\n"
    return text


def _proposal_title(run: Run) -> str:
    path = run.config.change_dir / "proposal.md"
    m = re.search(r"^#\s+(.+?)\s*$", path.read_text() if path.is_file() else "", re.MULTILINE)
    return m.group(1) if m else run.config.change_name


def write_files(run: Run, split: SplitRecord) -> None:
    """Write the stage 2 files the saved answer gives that are missing; keep the rest."""
    change_dir = run.config.change_dir
    if split.single:
        child = split.children[0]
        plan = change_dir / "plan.md"
        if not plan.is_file():
            atomic_write(plan, child.plan.rstrip() + "\n")
        return
    by_id = {c.id: c for c in split.children}
    order = waves(split.children)
    for cid in order:
        child = by_id[cid]
        comp = change_dir / COMPONENTS_DIR / cid
        comp.mkdir(parents=True, exist_ok=True)
        if not (comp / ".change.yaml").is_file():
            meta: dict = {"spec_status": "draft", "depends_on": list(child.depends_on)}
            if run.config.place is not None:
                meta["branch"] = run.config.place.branch
            if child.ticket_ref:
                meta["ticket_refs"] = {"epic": child.ticket_ref, "children": []}
            write_change_meta(comp, meta)
        # The plan last: a component dir counts once its plan.md is there.
        if not (comp / "plan.md").is_file():
            atomic_write(comp / "plan.md", child.plan.rstrip() + "\n")
    if not (change_dir / COMPONENTS_FILE).is_file():
        atomic_write(change_dir / COMPONENTS_FILE, components_md(_proposal_title(run), split))
    if "children" not in read_change_meta(change_dir):
        write_change_meta(change_dir, {"children": order})


async def run_components(run: Run) -> None:
    """Ask for the components unless the answer is saved, then write the missing files."""
    split = run.state.split
    if split is None:
        ticket_children = intake.ticket_refs(run.config.change_dir)["children"]
        answer = await ask_split(run, ticket_children)
        split = SplitRecord(
            inputs=inputs_hash(run),
            single=len(answer.components) == 1,
            reason=answer.reason,
            children=answer.components,
        )
        run.state.split = split
        run.save()
        run.progress(
            "COMPONENTS",
            ("One component: " if split.single else f"{len(split.children)} components: ")
            + split.reason,
        )
    write_files(run, split)
    print("PHASE:COMPONENTS", flush=True)


def component_title(comp_dir: Path) -> str:
    plan = comp_dir / "plan.md"
    return plan_title(plan.read_text() if plan.is_file() else "", comp_dir.name)
