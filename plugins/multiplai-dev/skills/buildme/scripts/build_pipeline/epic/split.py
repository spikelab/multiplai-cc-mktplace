"""The EPIC_SPLIT decision: one structured call, its inputs, and the checks on
its answer (plan-buildme-epic-mode-2026-10-12.md, items 1 and 4).

`epic_split` is the one model call. Tests patch it as a module attribute, so
callers reach it as `split.epic_split`, never through a `from` import.
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..prompts.epic_split import EPIC_SPLIT_PROMPT, EPIC_SPLIT_RETRY
from ..prompts.interview import UNTRUSTED_NOTICE
from ..sdk import llm_call_structured

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLAN_TITLE = "# Plan:"
PLAN_DONE = "## Done means"


class EpicChild(BaseModel):
    """One child change: its plan is written to `plans/<id>.md`."""

    id: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    repo: str | None = None
    plan: str = ""


class EpicSplitAnswer(BaseModel):
    single: bool
    reason: str = ""
    children: list[EpicChild] = Field(default_factory=list)


def split_inputs(change_dir: Path, codebase_analysis: Path, context_files: list[str]) -> str:
    """What the decision reads: the change dir's ticket, plan, interview and
    research, the context files, then the codebase analysis. The ticket is
    already fenced as untrusted (`intake.save_ticket`)."""
    parts = []
    for name, heading in (("ticket.md", "Ticket"), ("plan.md", "Plan"),
                          ("interview.md", "Interview"), ("research.md", "Research")):
        path = change_dir / name
        if path.is_file():
            parts.append(f"## {heading}\n{path.read_text()}")
    for raw in context_files or []:
        path = Path(raw)
        if path.is_file():
            parts.append(f"## Context: {path.name}\n{path.read_text()}")
        else:
            log.warning("Context file not found: %s", raw)
    if codebase_analysis.is_file():
        parts.append(f"## Codebase analysis\n{codebase_analysis.read_text()}")
    return "\n\n".join(parts) or "## Input\n(no ticket, plan, interview or context was given)"


async def epic_split(config, inputs: str, errors: list[str] | tuple[str, ...] = (), *,
                     call=None) -> EpicSplitAnswer:
    """One toolless structured call: one change, or several children with
    plans. *errors* are the previous answer's check failures, for the retry."""
    call = call or llm_call_structured
    retry = EPIC_SPLIT_RETRY.format(errors="\n".join(f"- {e}" for e in errors)) if errors else ""
    prompt = EPIC_SPLIT_PROMPT.format(notice=UNTRUSTED_NOTICE, inputs=inputs, errors=retry,
                                      date=datetime.date.today().isoformat())
    return await call(
        prompt,
        EpicSplitAnswer,
        model=config.model_for('epic_split'),
        effort=config.spec_effort,
        budget_label="epic_split",
    )


def check_plan(text: str, where: str) -> list[str]:
    """A child plan holds `# Plan:` and `## Done means`."""
    errors = []
    if PLAN_TITLE not in text:
        errors.append(f"{where}: the plan has no `{PLAN_TITLE}` line")
    if PLAN_DONE not in text:
        errors.append(f"{where}: the plan has no `{PLAN_DONE}` section")
    return errors


def check_children(children: list[EpicChild], plan_where=None) -> list[str]:
    """Item 4: unique lowercase hyphenated ids, every dependency names a
    child, no cycle, and each plan holds `# Plan:` and `## Done means`.
    *plan_where* names a child's plan in a message (default: the child id)."""
    plan_where = plan_where or (lambda c: f"child {c.id}")
    errors: list[str] = []
    seen: set[str] = set()
    for c in children:
        if not ID_RE.match(c.id):
            errors.append(f"child id {c.id!r} is not lowercase letters and digits joined by hyphens")
        if c.id in seen:
            errors.append(f"child id {c.id!r} is used twice")
        seen.add(c.id)
    for c in children:
        for dep in c.depends_on:
            if dep not in seen:
                errors.append(f"child {c.id} depends on {dep!r}, which is not a child")
    cycle = _cycle({c.id: [d for d in c.depends_on if d in seen] for c in children})
    if cycle:
        errors.append("the dependencies have a cycle: " + " -> ".join(cycle))
    for c in children:
        errors.extend(check_plan(c.plan, plan_where(c)))
    return errors


def check_answer(answer: EpicSplitAnswer) -> list[str]:
    """The checks on a model answer. `single: true` needs nothing more;
    several needs at least two children that pass `check_children`."""
    if answer.single:
        return []
    if len(answer.children) < 2:
        return [f"several changes need at least two children; the answer gave {len(answer.children)}"]
    return check_children(answer.children)


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle as a path of ids (first id repeated at the end), or None."""
    state: dict[str, int] = {}          # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if dep not in state:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def waves(children: list[EpicChild]) -> list[list[str]]:
    """The children in launch order: each wave depends only on earlier waves,
    so the ids within one wave can run in parallel. Assumes no cycle."""
    done: set[str] = set()
    left = [c for c in children]
    out: list[list[str]] = []
    while left:
        ready = [c.id for c in left if all(d in done for d in c.depends_on)]
        if not ready:           # a cycle: never reached after check_children
            out.append([c.id for c in left])
            break
        out.append(ready)
        done.update(ready)
        left = [c for c in left if c.id not in done]
    return out
