"""The review stops between the stages, and the ticket wait in stage 3.

A stop (`review_stop`), at the end of stage 1 (`proposal`), stage 2
(`components`) and stage 3 (`specs`, before the consistency check): the change dir is committed (`git_ops.commit_change_dir`, the
change dir alone), the feature branch is pushed when origin lacks the commit,
the step is recorded `passed`, then `SPEC:REVIEW:<stage>` and
`SPEC:BRANCH:<branch>` are printed and the run exits 3. The push means "ready
for review"; the next `resume` means "approved" and goes on past the stop,
after `branch.prepare` has pulled the branch (`__main__`).

Recorded only once pushed, so a run killed before the commit commits what is
on disk at its next run, regenerating nothing (row S2), and a failed push
(exit 1) is pushed by the next run (row S3), which then stops as above. With
`--auto` there is no stop: the step is recorded `skipped` and nothing is
committed until the handoff.

The ticket wait (`tickets_step`, row S6): `tickets.json` is written when
missing, committed and pushed with the prototype, `steps.tickets` is recorded
`pending`, and the run prints `SPEC:TICKETS:<path>` and exits 3. The caller
shows the tickets, creates them (or a person does), and resumes with
`--ticket <component>=<REF>` for each component that has no ticket yet, or
`--tickets-later`; any other resume refuses (exit 2), printing the flags it
expects and recording none of the refs it was given. With `--auto` there is no wait: the components keep `pending`.

A run started before the staged flow (pm-spec 0.8.0 and earlier, row S14)
has no stage records: `skip_before_staged_flow` records each stage step
`skipped`, so its resume only checks and hands off. The parent of a pm-spec
0.7.2 epic split (row S16) holds no specs of its own: `stop_at_old_split`
records the stages skipped and points at its children, writing nothing else.
"""

from __future__ import annotations

import logging

from multiplai_core.specs import read_change_meta

from . import branch, git_ops, layout, tickets
from . import findings as F
from .run import (
    EXIT_FAILED,
    EXIT_REFUSED,
    EXIT_WAITING,
    MARKER_BRANCH,
    MARKER_REVIEW,
    MARKER_TICKETS,
    Run,
    Stop,
)

log = logging.getLogger(__name__)

PROPOSAL, COMPONENTS, PROTOTYPE, TICKETS, SPECS = (
    "proposal",
    "components",
    "prototype",
    "tickets",
    "specs",
)

MESSAGES = {
    PROPOSAL: "proposal for review",
    COMPONENTS: "components for review",
    TICKETS: "prototype and tickets",
    SPECS: "specs for review",
}


STAGES = (PROPOSAL, COMPONENTS, PROTOTYPE, TICKETS, SPECS)
BEFORE_STAGED_FLOW = "before the staged flow"
OLD_SPLIT = f"{BEFORE_STAGED_FLOW}: the specs are in the children"


def predates_staged_flow(run: Run) -> bool:
    """Row S14: the run state records the old flow's check or handoff and no
    stage step (`prototype` was a step before the stages too), or the change
    dir is in the old layout (`requirements/`, no `specs.md`)."""
    steps = run.state.steps
    if (run.state.handoff is not None or "consistency" in steps) and _no_stage_record(
        run
    ):
        return True
    change_dir = run.config.change_dir
    return (change_dir / layout.OLD_DIR).is_dir() and not (
        change_dir / layout.SPECS_FILE
    ).is_file()


def _no_stage_record(run: Run) -> bool:
    return not any(
        name in run.state.steps for name in (PROPOSAL, COMPONENTS, TICKETS, SPECS)
    )


def old_split_children(run: Run) -> list[str]:
    """Row S16: the child change names of a pm-spec 0.7.2 epic split parent,
    `[]` for any other run. Such a parent's state has a `split` record with
    several children and no stage record (the staged flow records the
    proposal before it splits) but those this row wrote; its `.change.yaml`
    lists the children."""
    split = run.state.split
    if split is None or split.single:
        return []
    if not all(
        run.state.steps[name].reason == OLD_SPLIT
        for name in (PROPOSAL, COMPONENTS, TICKETS, SPECS)
        if name in run.state.steps
    ):
        return []
    try:
        listed = read_change_meta(run.config.change_dir).get("children")
    except (OSError, UnicodeError, ValueError):
        listed = None
    if isinstance(listed, list) and listed:
        return [c for c in listed if isinstance(c, str)]
    return [f"{run.config.change_name}-{c.id}" for c in split.children]


def stop_at_old_split(run: Run) -> bool:
    """Row S16: True, the stages recorded skipped and the children printed,
    for the parent of a pm-spec 0.7.2 epic split; False otherwise."""
    children = old_split_children(run)
    if not children:
        return False
    for name in STAGES:
        if name not in run.state.steps:
            F.record_skipped(run, name, "", OLD_SPLIT)
    name = run.config.change_name
    print(
        f"{name} was split into child changes by pm-spec 0.7.2 or earlier, and its "
        "specs live in the children. Nothing was changed. Resume each child:\n"
        + "\n".join(f"  resume {child}" for child in children),
        flush=True,
    )
    return True


def skip_before_staged_flow(run: Run) -> None:
    """Row S14: record each stage step with no record `skipped`."""
    if not predates_staged_flow(run):
        return
    names = [name for name in STAGES if name not in run.state.steps]
    for name in names:
        F.record_skipped(run, name, "", BEFORE_STAGED_FLOW)
    if names:
        run.progress(
            "STAGES",
            f"Recorded {', '.join(names)} skipped: the run started {BEFORE_STAGED_FLOW}",
        )


def done(run: Run, name: str) -> bool:
    step = run.state.steps.get(name)
    return bool(step and step.outcome in ("passed", "skipped"))


def commit_and_push(run: Run, name: str) -> None:
    """Commit the change dir and push the feature branch when origin lacks a commit."""
    message = f"specs({run.config.change_name}): {MESSAGES[name]}"
    try:
        sha = git_ops.commit_change_dir(
            run.config.project_dir, run.config.change_dir, message
        )
    except git_ops.GitError as e:
        run.progress(name.upper() + "_ERROR", f"The commit failed: {e}")
        print(
            f"ERROR: {name}: the commit failed: {e}\n"
            f"Fix that, then run `resume {run.config.change_name}`.",
            flush=True,
        )
        raise Stop(EXIT_FAILED, str(e)) from e
    if sha:
        run.progress(name.upper(), f"Committed {sha}")
    where = run.config.place
    if where is None or not where.push or not branch.ahead(where):
        return
    try:
        branch.push(where)
    except branch.PushFailed as e:
        run.progress(name.upper() + "_ERROR", f"The push failed: {e}")
        print(
            f"ERROR: {name}: the files are committed, but `git push origin {where.branch}` "
            f"failed: {e}\nFix that, then run `resume {run.config.change_name}`: it pushes "
            "and stops for review again.",
            flush=True,
        )
        raise Stop(EXIT_FAILED, str(e)) from e
    run.progress(name.upper(), f"Pushed {where.branch}")


def _print_branch(run: Run) -> None:
    if run.config.place is not None:
        print(f"{MARKER_BRANCH}:{run.config.place.branch}", flush=True)


def review_stop(run: Run, name: str) -> None:
    """Stop for review after stage *name* (module docstring); returns when it is behind us."""
    if done(run, name):
        return
    if run.state.options.auto:
        F.record_skipped(run, name, "", "--auto: no review stop")
        return
    commit_and_push(run, name)
    F.record_passed(run, name, "", "Pushed for review")
    print(f"{MARKER_REVIEW}:{name}", flush=True)
    _print_branch(run)
    raise Stop(EXIT_WAITING, f"review of the {name}")


def _flag_lines(run: Run, missing: list[str]) -> str:
    flags = " ".join(f"--ticket {c}=<ID>" for c in missing)
    name = run.config.change_name
    return f"  resume {name} {flags}\nor, to create them later:\n  resume {name} --tickets-later"


def tickets_step(run: Run, refs: dict[str, str], later: bool) -> None:
    """Write `tickets.json`, then wait for the components' tickets (module docstring)."""
    if done(run, TICKETS):
        if refs:
            print(
                "Note: the tickets step is behind this change; --ticket is ignored.",
                flush=True,
            )
        return
    change_dir = run.config.change_dir
    path = change_dir / tickets.FILE
    if not path.is_file():
        tickets.write(change_dir, run.config.project_dir)
    if run.state.options.auto:
        F.record_skipped(
            run, TICKETS, "", "--auto: the components' tickets stay pending"
        )
        return
    step = run.state.steps.get(TICKETS)
    if step is None or step.outcome != "pending":
        commit_and_push(run, TICKETS)
        run.state.step(TICKETS).outcome = "pending"
        run.state.step(TICKETS).reason = "Waiting for the components' tickets"
        run.save()
        print(f"{MARKER_TICKETS}:{path}", flush=True)
        _print_branch(run)
        raise Stop(EXIT_WAITING, "waiting for the tickets")
    unknown = sorted(c for c in refs if not _known(run, c))
    if unknown:
        print(
            f"ERROR: --ticket names {', '.join(unknown)}, which is not a component of "
            f"{run.config.change_name}",
            flush=True,
        )
        raise Stop(EXIT_REFUSED, "unknown component")
    missing = [c for c in tickets.pending(change_dir) if c not in refs]
    if missing and not later:
        # Nothing is written: the refs given are taken with the rest, or not at all.
        print(
            f"ERROR: {', '.join(missing)} have no ticket yet. Nothing was recorded. Pass "
            "every component's ticket id:\n"
            + _flag_lines(run, tickets.pending(change_dir)),
            flush=True,
        )
        raise Stop(EXIT_REFUSED, "tickets pending")
    if refs:
        tickets.apply_refs(change_dir, refs)
        run.progress(
            "TICKETS", "Recorded " + ", ".join(f"{c}={r}" for c, r in refs.items())
        )
    if missing:
        F.record_skipped(
            run, TICKETS, "", "--tickets-later: " + ", ".join(missing) + " pending"
        )
    else:
        F.record_passed(run, TICKETS, "", "Every component has its ticket")


def _known(run: Run, component: str) -> bool:
    return (run.config.change_dir / "components" / component / "plan.md").is_file()
