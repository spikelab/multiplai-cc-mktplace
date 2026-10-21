"""The epic split: one change, or several child change dirs (row S3).

One toolless structured call reads the ticket, the plan, the PRD and the
interview, and answers one change or several children, each with a plan. The
answer is checked in code (buildme's `epic/split.py` rules: unique lowercase
hyphenated ids, every dependency names a child, no cycle, each plan holds
`# Plan:` and `## Done means`, at least two children; plus, for the ticket's
recorded child refs: a `ticket_ref` is one of them, every one of them is the
`ticket_ref` of exactly one child, and with two or more of them the answer
cannot be one change), with one retry that lists the problems.

The answer is saved in the run state with the hash of the inputs it read,
before any child dir is written. A run that finds it, with the inputs
unchanged, makes no call: it creates the child change dirs that are missing
and prints the same markers. Changed inputs split again.

Each child is `specs/changes/<parent>-<id>/` with its own `.change.yaml`
(`parent`, and `ticket_refs` when it delivers a ticket child item), its plan as
`plan.md` (its own interview input), a copy of the parent's `prd.md`, and a run
state that carries the parent's options. A child dir counts as created once
its run state exists. Each child is then specified by its own run:
`resume <parent>-<id>`.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

from multiplai_core.specs import write_change_meta
from pydantic import BaseModel, Field

from . import intake, sdk
from .artifacts import sha
from . import state as state_mod
from .prompts import UNTRUSTED_NOTICE
from .prompts.epic_split import CHILDREN_RULE, EPIC_SPLIT_PROMPT, EPIC_SPLIT_RETRY
from .run import MARKER_CHILD, MARKER_SPLIT, Run
from .state import EpicChild, RunState, SplitRecord

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLAN_TITLE = "# Plan:"
PLAN_DONE = "## Done means"


class SplitAnswer(BaseModel):
    single: bool
    reason: str = ""
    children: list[EpicChild] = Field(default_factory=list)


def split_inputs(run: Run) -> str:
    change_dir = run.config.change_dir
    parts = []
    prd = intake.prd_text(change_dir)
    if prd:
        parts.append("## PRD\n" + intake.fenced(prd, "prd.md"))
    for name, heading in (("ticket.md", "Ticket"), ("plan.md", "Plan"), ("interview.md", "Interview")):
        path = change_dir / name
        if path.is_file():
            parts.append(f"## {heading}\n{path.read_text()}")
    return "\n\n".join(parts) or "## Input\n(no ticket, plan, PRD or interview was given)"


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
                return path[path.index(dep):] + [dep]
            if dep not in state and (found := visit(dep)):
                return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return None


def check_answer(answer: SplitAnswer, ticket_children: list[str]) -> list[str]:
    if answer.single:
        if len(ticket_children) >= 2:
            return [f"one change drops the ticket's child items {', '.join(ticket_children)}: answer several, "
                    "with one child per listed item, each naming it as its `ticket_ref`"]
        return []
    children = answer.children
    if len(children) < 2:
        return [f"several changes need at least two children; the answer gave {len(children)}"]
    errors: list[str] = []
    seen: set[str] = set()
    for c in children:
        if not ID_RE.match(c.id):
            errors.append(f"child id {c.id!r} is not lowercase letters and digits joined by hyphens")
        if c.id in seen:
            errors.append(f"child id {c.id!r} is used twice")
        seen.add(c.id)
        if c.ticket_ref and c.ticket_ref not in ticket_children:
            errors.append(f"child {c.id} names ticket_ref {c.ticket_ref!r}, which is not one of the ticket's "
                          f"child items ({', '.join(ticket_children) or 'none'})")
    given = [c.ticket_ref for c in children if c.ticket_ref]
    for ref in ticket_children:
        if ref not in given:
            errors.append(f"the ticket's child item {ref} has no child change: give it one, with "
                          f"`ticket_ref` {ref!r}")
        elif given.count(ref) > 1:
            errors.append(f"the ticket's child item {ref} is given twice; it belongs to one child")
    for c in children:
        for dep in c.depends_on:
            if dep not in seen:
                errors.append(f"child {c.id} depends on {dep!r}, which is not a child")
    if cycle := _cycle({c.id: [d for d in c.depends_on if d in seen] for c in children}):
        errors.append("the dependencies have a cycle: " + " -> ".join(cycle))
    for c in children:
        if PLAN_TITLE not in c.plan:
            errors.append(f"child {c.id}: the plan has no `{PLAN_TITLE}` line")
        if PLAN_DONE not in c.plan:
            errors.append(f"child {c.id}: the plan has no `{PLAN_DONE}` section")
    return errors


def waves(children: list[EpicChild]) -> list[str]:
    """Child ids in an order where each comes after everything it depends on."""
    done: list[str] = []
    left = list(children)
    while left:
        ready = [c for c in left if all(d in done for d in c.depends_on)] or left
        done.extend(c.id for c in ready)
        left = [c for c in left if c.id not in done]
    return done


async def ask_split(run: Run, ticket_children: list[str]) -> SplitAnswer:
    errors: list[str] = []
    for attempt in (1, 2):
        prompt = EPIC_SPLIT_PROMPT.format(
            notice=UNTRUSTED_NOTICE, inputs=split_inputs(run), date=datetime.date.today().isoformat(),
            children_rule=CHILDREN_RULE.format(children=", ".join(ticket_children)) if ticket_children else "",
            errors=EPIC_SPLIT_RETRY.format(errors="\n".join(f"- {e}" for e in errors)) if errors else "")
        answer = await sdk.text_structured(prompt, SplitAnswer, config=run.config, step="epic_split",
                                           label="epic_split")
        errors = check_answer(answer, ticket_children)
        if not errors:
            return answer
        log.warning("epic split answer %d rejected: %s", attempt, "; ".join(errors))
    raise ValueError("The epic split answer was rejected twice: " + "; ".join(errors))


def child_name(run: Run, child: EpicChild) -> str:
    return f"{run.config.change_name}-{child.id}"


def create_children(run: Run, split: SplitRecord) -> list[str]:
    """Create each child change dir that has no run state yet; return every child's name in order."""
    order = waves(split.children)
    by_id = {c.id: c for c in split.children}
    parent_prd = run.config.change_dir / "prd.md"
    names = []
    for cid in order:
        child = by_id[cid]
        name = child_name(run, child)
        names.append(name)
        child_dir = run.config.change_dir_for(name)
        run_dir = child_dir / ".pm-spec"
        if (run_dir / state_mod.STATE_FILE).is_file():
            continue
        intake.create_change(child_dir, parent=run.config.change_name)
        if child.ticket_ref:
            write_change_meta(child_dir, {"ticket_refs": {"epic": child.ticket_ref, "children": []}})
        (child_dir / "plan.md").write_text(child.plan.rstrip() + "\n")
        if parent_prd.is_file():
            (child_dir / "prd.md").write_text(parent_prd.read_text())
        # The parent's options, minus a second split; `interview: ticket` with
        # no ticket_ref falls back as it does for any change (`interview.effective_mode`).
        options = run.state.options.model_copy(update={
            "epic_split": False, "ticket_ref": child.ticket_ref, "ticket_url": "", "ticket_children": []})
        state_mod.save(RunState(change=name, options=options, parent=run.config.change_name, intake_done=True),
                       run_dir, ledger=False)
        log.info("Created child change %s", child_dir)
    return names


async def run_epic_split(run: Run) -> bool:
    """True when the change was split into children (the caller then stops, exit 0)."""
    if not run.state.options.epic_split or run.state.parent:
        return False
    current = inputs_hash(run)
    split = run.state.split
    if split is None or split.inputs != current:
        ticket_children = intake.ticket_refs(run.config.change_dir)["children"]
        answer = await ask_split(run, ticket_children)
        split = SplitRecord(inputs=current, single=answer.single, reason=answer.reason,
                            children=[] if answer.single else answer.children)
        run.state.split = split
        run.save()
        run.progress("EPIC_SPLIT", ("One change: " if split.single else f"{len(split.children)} children: ")
                     + split.reason)
    if split.single:
        return False
    names = create_children(run, split)
    print(f"{MARKER_SPLIT}:{run.config.change_dir}", flush=True)
    for name in names:
        print(f"{MARKER_CHILD}:{run.config.change_dir_for(name)}", flush=True)
    return True
