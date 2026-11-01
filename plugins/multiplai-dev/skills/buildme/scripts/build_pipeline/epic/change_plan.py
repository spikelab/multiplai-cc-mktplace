"""The implementation plan of a change of several components: one plan for
every component, written by the change's build and approved by an engineer
(stop 4) before any component builds.

pm-spec writes one proposal per change and one `specs.md` per component;
buildme mirrors that with one `implementation-plan.md` per change, beside the
proposal, and one `tasks.md` per component. Each component reads the change's
plan (`change_manager.reads_change_plan`) and writes none.

`ensure` runs in the coordinator before any component is launched, on the
feature branch as origin holds it (`feature_branch.checkout_tip`):

1. A change whose components were each planned on their own (one holds its
   plan, the change none) is left as it is: each component writes its own.
2. No plan yet: the shared codebase analysis runs, `generate_change_plan`
   writes the plan from the proposal, `components.md`, every component's
   `plan.md` and `specs.md` and the change's `research.md`, and it is
   committed and pushed. A `SPEC_ISSUE:` line in it stops the run with
   `spec-issues.md` at the change, as a single build's design step does
   (exit 4). An existing plan is never written again: delete it to have one
   written.
3. Unless the run has `--auto`, the plan needs an engineer's approval: a
   commit on the feature branch with `Stop: 4`, `Revision: <revision>` and
   `Approved-by:` (or `Decided-by:`) trailers, the trailers a single build's
   stop 4 reads. `--approve-plan` makes it in git `user.name`'s name and
   pushes it. With none, `plan-approval.md` is written beside the plan,
   committed and pushed, and `PLAN:WAITING:<path>` printed; the coordinator
   then launches no component, and ends the run with exit 3.

The revision hashes the plan and everything it was written from, so an edit
to the plan, or to a component's specs after `pm-spec revise`, asks for the
approval again.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from multiplai_core.specs import component_dirs

from .. import feature_branch, git_ops, readiness
from ..change_manager import PLAN_FILE, plan_path, reads_change_plan
from ..io_utils import atomic_write

log = logging.getLogger(__name__)

# The change's documents the plan is written from, beside each component's
# plan.md and specs.md.
CHANGE_INPUTS = ("proposal.md", "use-cases.md", "components.md", "research.md")
COMPONENT_INPUTS = ("plan.md", "specs.md")


@dataclass
class Outcome:
    """What `ensure` found. *waiting*: the plan waits for its approval, with
    the request at that path. *exit*: the run ends now with that code, having
    printed why."""

    waiting: Path | None = None
    exit: int | None = None


def planned_once(change: Path) -> bool:
    """Whether *change* builds all its components from one plan: it has
    components, and at least one of them reads the change's plan."""
    return any(reads_change_plan(d) for d in component_dirs(change))


def revision(change: Path) -> str:
    """A sha256 of the change's plan and of the documents it was written
    from, each as its path relative to *change*, a NUL and its bytes."""
    files = {PLAN_FILE: plan_path(change)} | {
        name: change / name for name in CHANGE_INPUTS
    }
    for d in component_dirs(change):
        files |= {f"components/{d.name}/{n}": d / n for n in COMPONENT_INPUTS}
    digest = hashlib.sha256()
    for name in sorted(files):
        path = files[name]
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def request_text(name: str, change: Path, rev: str, branch: str) -> str:
    """plan-approval.md for the change's plan: what to approve, what each
    component builds from it, and the two ways to record the approval."""
    from ..orchestrator import PLAN_APPROVAL_STOP

    components = [f"- `components/{d.name}/`" for d in component_dirs(change)]
    return "\n".join(
        [
            f"# Approve the implementation plan: {name}",
            "",
            f"Plan revision: `{rev}`",
            "",
            "buildme builds no component of this change until an engineer approves",
            "this plan.",
            "",
            "## To approve",
            "",
            f"- `{plan_path(change).name}`: how the whole change will be implemented,",
            "  for every component. Approve this file.",
            "",
            "## Built from it",
            "",
            *components,
            "",
            "Each component writes its own `tasks.md` from this plan and its",
            "`specs.md` once the plan is approved.",
            "",
            "## How to approve",
            "",
            "Re-run the same build command with `--approve-plan`: it commits the",
            f"approval in your name (git `user.name`) on `{branch}` and pushes it.",
            "Or commit it yourself on that branch and push it, with these trailers:",
            "",
            "```",
            f'git commit --allow-empty -m "review({name}): approve the implementation plan" \\',
            f'  --trailer "Stop: {PLAN_APPROVAL_STOP}" --trailer "Revision: {rev}" \\',
            '  --trailer "Approved-by: <your name>"',
            "```",
            "",
            "Then re-run the same build command. An edit to the plan, or to what it",
            "was written from, makes a new revision, and the build asks again.",
            "To have buildme write the plan again, delete it and re-run.",
            "",
        ]
    )


async def ensure(
    config,
    repo: Path,
    branch: str,
    *,
    approve: bool,
    analysis: Callable[[str], Awaitable[Path | None]],
) -> Outcome:
    """Write the change's plan when it has none, and look for its approval
    (see the module docstring). *analysis*: the coordinator's shared
    codebase analysis at a commit, None when it failed."""
    from ..code_analysis import SHARED_REPORT
    from ..llm_steps.spec_steps import generate_change_plan
    from ..orchestrator import (
        PLAN_APPROVAL_FILE,
        _approval_message,
        _requested_revision,
    )
    from ..spec_generator import design_spec_issues

    name = config.change_name
    project_dir = Path(config.project_dir)
    got = await asyncio.to_thread(feature_branch.checkout_tip, repo, branch)
    if isinstance(got, str):
        return _failed(f"cannot read {name}'s implementation plan on {branch}: {got}")
    w, tip = got
    prefix = git_ops.path_prefix(project_dir) or ""
    project = w / prefix if prefix else w
    change = project / Path(config.change_dir).relative_to(project_dir)
    if not change.is_dir():  # archived on the branch: nothing left to build
        return Outcome()
    if not planned_once(change):
        log.info(
            "CHANGE_PLAN %s: each component was planned on its own; no change plan",
            name,
        )
        return Outcome()
    rel = change.relative_to(w).as_posix()

    if not plan_path(change).is_file():
        log.info("CHANGE_PLAN %s: writing the plan at %s", name, tip[:12])
        directory = await analysis(tip)
        report = directory / SHARED_REPORT if directory is not None else None
        at = copy.copy(config)
        at.project_dir = project
        at.specs_dir = project / Path(config.specs_dir).relative_to(project_dir)
        research = change / "research.md"
        text = await generate_change_plan(
            change,
            at,
            codebase_analysis=report.read_text() if report and report.is_file() else "",
            research=research.read_text() if research.is_file() else "",
            reference_docs=config.reference_docs_text(),
        )
        issues = design_spec_issues(text)
        if issues:
            path = readiness.write_issues(change, issues, change=name)
            why = feature_branch.commit_and_push(
                repo,
                w,
                branch,
                f"spec({name}): requirements issues the implementation plan found",
                [rel],
            )
            if why is not None:
                return _failed(why)
            return Outcome(exit=readiness.refuse_issues(path))
        atomic_write(change / PLAN_FILE, text)
        why = feature_branch.commit_and_push(
            repo,
            w,
            branch,
            f"docs(specs): {name} — implementation plan",
            [f"{rel}/{PLAN_FILE}"],
        )
        if why is not None:
            return _failed(why)
        log.info("CHANGE_PLAN %s: plan written and pushed to %s", name, branch)

    rev = revision(change)
    if config.auto:
        log.info("SKIP CHANGE_PLAN approval change=%s reason=--auto", name)
        return Outcome()
    found = _approval(w, rev)
    if found is None and approve:
        who = git_ops.user_name(project_dir)
        if not who:
            print(
                "ERROR:--approve-plan needs git user.name; set it "
                '(git config user.name "<your name>") and re-run',
                flush=True,
            )
            return Outcome(exit=1)
        why = feature_branch.commit_and_push(
            repo, w, branch, _approval_message(name, rev, who), None
        )
        if why is not None:
            return _failed(why)
        found = _approval(w, rev)
    if found is not None:
        sha, who = found
        log.info(
            "CHANGE_PLAN %s: revision %s approved by %s in %s",
            name,
            rev[:12],
            who,
            sha[:12],
        )
        return Outcome()

    request = change / PLAN_APPROVAL_FILE
    if _requested_revision(request) != rev:
        request.write_text(request_text(name, change, rev, branch))
        why = feature_branch.commit_and_push(
            repo,
            w,
            branch,
            f"docs(specs): {name} — ask for the plan's approval",
            [f"{rel}/{PLAN_APPROVAL_FILE}"],
        )
        if why is not None:
            return _failed(why)
    log.info("CHANGE_PLAN %s: waiting for the approval of %s", name, rev[:12])
    from ..intake import MARKER_PLAN_WAITING

    print(f"{MARKER_PLAN_WAITING}:{request}", flush=True)
    return Outcome(waiting=request)


def _approval(w: Path, rev: str) -> tuple[str, str] | None:
    """The newest commit on *w*'s HEAD approving *rev*, as (sha, who)."""
    from ..orchestrator import PLAN_APPROVAL_STOP

    commits = git_ops.commit_trailers(w)
    if commits is None:
        raise git_ops.GitLifecycleError(
            f"cannot read the commits of {w} to look for the plan approval"
        )
    for sha, trailers in commits:
        if (
            trailers.get("stop") == PLAN_APPROVAL_STOP
            and trailers.get("revision") == rev
        ):
            who = trailers.get("approved-by") or trailers.get("decided-by")
            if who:
                return sha, who
    return None


def _failed(why: str) -> Outcome:
    log.error("CHANGE_PLAN %s", why)
    print(f"ERROR:{why}", flush=True)
    return Outcome(exit=1)
