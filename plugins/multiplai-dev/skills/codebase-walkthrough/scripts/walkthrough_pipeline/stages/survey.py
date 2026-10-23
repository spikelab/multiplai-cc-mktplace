"""survey (`--scope codebase` only): three agents, each reading across the whole snapshot.

An explore agent reads one slice of the code, so it cannot see that a gate in one file is never reached
from a loop in another, and it reads a doc only when the doc falls in its slice. The survey asks three
questions of the whole snapshot (`prompts.SURVEY_QUESTIONS`): loops, caps, exits and gates that cannot
fire; state, logs and resume, reading the `--lib` directories too; and which command to run, from the
target's own docs. Their answers are stored in `state.explore_answers` under `survey:<question>` and go
through the same citation gate as explore's (`explore.gate_answers`), so their facts get ids after
explore's. Any other scope passes through without a call.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from code_facts import run_tasks

from .. import sdk
from ..models import ExploreOutput, TargetInfo, WalkState
from ..prompts import survey_task
from . import RunContext
from .explore import _ask_kwargs, gate_answers

QUESTIONS = ("loops", "lifecycle", "commands")
DOC_NAMES = ("SKILL.md", "README.md", "CHANGELOG.md", "CLAUDE.md")


def own_docs(target: TargetInfo, snap: Path) -> list[str]:
    """The target's own docs, workspace-relative: every SKILL.md, README.md, CHANGELOG.md and CLAUDE.md
    under its directory, then for each name the nearest one in a directory above it, up to the repo root."""
    base = snap / target.subpath if target.subpath else snap
    under = sorted(
        f.relative_to(snap).as_posix()
        for name in DOC_NAMES
        for f in base.rglob(name)
        if f.is_file()
    )
    above = []
    parts = PurePosixPath(target.subpath).parts if target.subpath else ()
    for name in DOC_NAMES:
        for i in range(len(parts) - 1, -1, -1):
            rel = PurePosixPath(*parts[:i], name).as_posix()
            if (snap / rel).is_file():
                above.append(rel)
                break
    return [target.ws(p) for p in under + above]


def survey_key(question: str) -> str:
    return f"survey:{question}"


def _libs(state: WalkState) -> list[str]:
    return [
        f"{lib['key']}/{lib['subpath']}".rstrip("/")
        for lib in state.options.get("libs_resolved") or []
    ]


async def run_survey(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("survey"):
        return state
    if state.options.get("scope") != "codebase":
        state.stage = "survey"
        return state
    t = state.target
    files = [(t.ws(f), t.file_lines.get(f, 0)) for f in t.files]
    docs = own_docs(t, ctx.snap_root / t.repo_key)
    tasks = [
        (
            survey_key(q),
            survey_task(t.package, q, files, docs, _libs(state), ExploreOutput),
        )
        for q in QUESTIONS
    ]
    dropped_before = len([d for d in state.dropped if d.stage == "explore"])
    await run_tasks(
        tasks,
        sdk.agent_call_structured,
        state.explore_answers,
        concurrency=ctx.config.concurrency,
        budget_label=lambda key: key,
        **_ask_kwargs(state, ctx),
    )
    # Survey answers after every other, in QUESTIONS order: a survey resumed after a stop that saved them
    # in another order gives the same fact and citation ids as one that never stopped.
    keys = [survey_key(q) for q in QUESTIONS]
    answers = state.explore_answers
    state.explore_answers = {
        **{k: v for k, v in answers.items() if k not in keys},
        **{k: answers[k] for k in keys if k in answers},
    }
    gate_answers(state, ctx)
    ctx.counts = {
        "facts": len([f for f in state.facts if f.source.startswith("survey:")]),
        "dropped": len([d for d in state.dropped if d.stage == "explore"])
        - dropped_before,
        "docs": len(docs),
        "failed_tasks": len(
            [
                q
                for q in QUESTIONS
                if "error" in state.explore_answers.get(survey_key(q), {})
            ]
        ),
    }
    state.stage = "survey"
    return state
