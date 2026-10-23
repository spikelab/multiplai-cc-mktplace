"""The three commands, `new`, `resume` and `revise`, as stages over one run.

In order, each skipped when the run state or the files say it is done:

1. finish a rewrite whose publication was interrupted (`publish.finish`);
2. the handoff left part way, or a ready record gone stale (row S8);
3. the interview;
4. stage 1: `proposal.md`, then the review stop (rows S1-S3): commit, push,
   `SPEC:REVIEW:proposal`, exit 3;
5. stage 2: `use-cases.md`, the components and their plans, then the review
   stop (rows S4, S5): `SPEC:REVIEW:components`, exit 3;
6. stage 3: the prototype, `tickets.json` and the ticket wait (row S6:
   `SPEC:TICKETS:<path>`, exit 3), then each component's `specs.md` (row S7),
   then the review stop (row S13): `SPEC:REVIEW:specs`, exit 3;
7. on the resume after it, the consistency check, then the handoff (row S8).

A run pm-spec 0.8.0 or earlier started has its stage steps recorded skipped
first (row S14, `stages.skip_before_staged_flow`), so it runs only 2 and 7.

`--auto` makes no stop and no ticket wait. Every file on disk is kept, whoever
wrote it: an edit pulled from the branch at a stop is an input of the stages
after it (rows S9, S11). A change adopted as it stands (specs pm-spec did not
write) runs only 7. While `spec_issues` is set or a revise is blocked, a
resume refuses (exit 4) and hands nothing off; a revise whose rewrite is
admitted is carried on.

The run lock (row S12) is held for the whole command. Before it, `__main__`
places the run on the change's feature branch and pulls it (`branch.place`,
`branch.prepare`; row S10): `config.project_dir` is the checkout pm-spec
writes in, its own worktree unless `--project-dir` is on that branch or is a
build's worktree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.specs import write_change_meta

from . import (
    artifacts,
    branch,
    epic,
    handoff,
    intake,
    interview,
    layout,
    prototype,
    publish,
    revise,
    runlock,
    sdk,
    stages,
)
from . import findings as F
from . import state as state_mod
from .config import SpecConfig
from .run import (
    EXIT_BLOCKED,
    EXIT_DONE,
    EXIT_REFUSED,
    MARKER_BLOCKED,
    Run,
    Stop,
)
from .state import ContextFile, Options, RunState

log = logging.getLogger(__name__)


@dataclass
class NewInputs:
    prd: str = ""
    ticket_file: str = ""
    ticket_ref: str = ""
    ticket_url: str = ""
    ticket_children: list[str] = field(default_factory=list)
    plan_file: str = ""
    context_files: list[str] = field(default_factory=list)
    interview_summary: str = ""
    interview_mode: str = ""
    prototype: str = ""
    epic_split: bool = True


def _missing(path: Path, flag: str) -> None:
    print(f"ERROR: {flag} {path}: no such file", flush=True)
    raise Stop(EXIT_REFUSED, f"{flag} not found")


def intake_new(config: SpecConfig, inputs: NewInputs) -> RunState:
    """The change dir, the inputs saved into it, and the run state that records them."""
    base = config.project_dir
    files = {}
    for flag, raw in (
        ("--prd", inputs.prd),
        ("--ticket-file", inputs.ticket_file),
        ("--plan-file", inputs.plan_file),
    ):
        if raw:
            path = intake.absolute(raw, base)
            if not path.is_file():
                _missing(path, flag)
            files[flag] = path
    contexts = intake.read_context_files(
        [str(intake.absolute(p, base)) for p in inputs.context_files]
    )
    change_dir = config.change_dir
    intake.create_change(change_dir)
    if config.place is not None:
        branch.record(change_dir, config.place.branch)
    if "--prd" in files:
        intake.save_copy(change_dir, files["--prd"], "prd.md")
    if "--plan-file" in files:
        # The plan is an input, read by the interview and kept in interview.md:
        # `plan.md` in a change dir is a component's plan, written in stage 2.
        path = files["--plan-file"]
        contexts.insert(0, (path.name, path.read_text()))
    if "--ticket-file" in files:
        intake.save_ticket(
            change_dir,
            files["--ticket-file"],
            ref=inputs.ticket_ref,
            url=inputs.ticket_url,
            children=inputs.ticket_children,
        )
    elif inputs.ticket_ref or inputs.ticket_children:
        write_change_meta(
            change_dir,
            {
                "ticket_refs": {
                    "epic": inputs.ticket_ref or None,
                    "children": list(inputs.ticket_children),
                }
            },
        )
    options = Options(
        interview_mode=config.interview_mode(inputs.interview_mode),
        auto=config.auto,
        prototype=config.prototype_mode(inputs.prototype),
        epic_split=inputs.epic_split,
        ticket_ref=inputs.ticket_ref,
        ticket_url=inputs.ticket_url,
        ticket_children=list(inputs.ticket_children),
    )
    return RunState(
        change=config.change_name,
        options=options,
        intake_done=True,
        summary=inputs.interview_summary,
        contexts=[ContextFile(name=n, content=c) for n, c in contexts],
    )


def _ready_again(run: Run) -> bool:
    """Row S8, and an edit after the ready record (row S9). True when the run
    has nothing left to do."""
    h = run.state.handoff
    if h is None:
        return False
    if not h.committed or not h.pushed:
        if handoff.pending_commit(run):
            handoff.retry_commit(run)
            return True
        return False
    ready, reason = layout.ready(run.config.change_dir)
    if ready:
        handoff.commit_ticket(run)
        return True
    log.info(
        "The change was ready and its files changed since (%s); checking again", reason
    )
    layout.mark_all_draft(run.config.change_dir)
    run.state.handoff = None
    run.save()
    return False


def _refuse_while_issues_are_open(run: Run) -> None:
    """While `spec_issues` is set or a revise is blocked, `resume` hands
    nothing off: the issues are `revise`'s, under its own rules."""
    handoff.refuse_open_issues(run)
    step = run.state.steps.get(revise.NAME)
    if step and step.outcome == "blocked":
        print(F.refusal_text(revise.NAME, step), flush=True)
        print(f"Fix the files, then run `revise {run.config.change_name}`.", flush=True)
        print(f"{MARKER_BLOCKED}:{run.config.change_dir}", flush=True)
        raise Stop(EXIT_BLOCKED, "revise blocked")


@dataclass
class TicketInputs:
    """`resume --ticket <component>=<REF>` (repeatable) and `--tickets-later`."""

    refs: dict[str, str] = field(default_factory=dict)
    later: bool = False


async def advance(
    run: Run, *, replies_file: str = "", tickets_in: TicketInputs | None = None
) -> int:
    tickets_in = tickets_in or TicketInputs()
    publish.finish(run)
    if (from_respec := revise.in_flight(run)) is not None:
        log.info("A revise is in flight; resuming it")
        await revise.run_revise(run, from_respec=from_respec)
        return EXIT_DONE
    _refuse_while_issues_are_open(run)
    if not run.state.adopted:
        stages.skip_before_staged_flow(run)
    if _ready_again(run):
        return EXIT_DONE
    if not run.state.adopted:
        await interview.run_interview(run, replies_file=replies_file)
        # Stage 1: the problem.
        if not stages.done(run, stages.PROPOSAL):
            await artifacts.generate_proposal(run)
        stages.review_stop(run, stages.PROPOSAL)
        # Stage 2: the components.
        if not stages.done(run, stages.COMPONENTS):
            await artifacts.generate_use_cases(run)
            await epic.run_components(run)
        stages.review_stop(run, stages.COMPONENTS)
        # Stage 3: the prototype, the tickets, the specs.
        await prototype.run_prototype_stage(run)
        stages.tickets_step(run, tickets_in.refs, tickets_in.later)
        if not stages.done(run, stages.SPECS):
            await artifacts.generate_specs(run)
        stages.review_stop(run, stages.SPECS)
    # The check and the handoff, once the specs are approved.
    await handoff.check_and_hand_off(run)
    return EXIT_DONE


def _load(config: SpecConfig) -> RunState | None:
    try:
        return state_mod.load(config.run_dir)
    except state_mod.Unreadable as e:
        print(f"ERROR: {e}", flush=True)
        raise Stop(EXIT_REFUSED, str(e)) from e


async def cmd_new(
    config: SpecConfig,
    inputs: NewInputs,
    *,
    replies_file: str = "",
    max_usd: float | None = sdk.DEFAULT_MAX_USD,
) -> int:
    if _load(config) is not None:
        print(
            f"ERROR: {config.change_name} already has a pm-spec run in {config.run_dir}; "
            f"continue it with `resume {config.change_name}`",
            flush=True,
        )
        raise Stop(EXIT_REFUSED, "run exists")
    state_mod.ensure_run_dir(config.run_dir)
    with runlock.hold(config.run_dir):
        sdk.start_budget(max_usd, None)
        run = Run(config, intake_new(config, inputs))
        run.save()
        run.progress(
            "INTAKE", f"Change {config.change_name} started in {config.change_dir}"
        )
        return await advance(run, replies_file=replies_file)


def _no_run(config: SpecConfig) -> None:
    print(
        f"ERROR: {config.change_name} has no pm-spec run in {config.run_dir}; "
        f"start one with `new {config.change_name}`",
        flush=True,
    )
    raise Stop(EXIT_REFUSED, "no run")


def _start(config: SpecConfig, max_usd: float | None) -> Run:
    state = _load(config)
    if state is None:
        _no_run(config)
    sdk.start_budget(max_usd, state.budget)
    config.auto = state.options.auto
    return Run(config, state)


def _adopt(config: SpecConfig, max_usd: float | None) -> Run:
    """A run for a change whose specs were written without pm-spec (by hand, or
    by buildme before pm-spec existed — buildme's refusal for such a change
    names `pm-spec resume`). Its files are taken as they stand: the run does
    only the consistency check and the handoff (`RunState.adopted`), makes no
    other model call and writes no spec or interview file."""
    sdk.start_budget(max_usd, None)
    run = Run(
        config,
        RunState(
            change=config.change_name,
            options=Options(epic_split=False),
            intake_done=True,
            interview_done=True,
            adopted=True,
        ),
    )
    run.save()
    run.progress("ADOPT", f"Adopted the specs in {config.change_dir} as they stand")
    return run


def _adoptable(config: SpecConfig) -> bool:
    """A proposal, and specs: a `specs.md` (the change's or a component's) or
    the old layout's capability files."""
    change_dir = config.change_dir
    if not (change_dir / "proposal.md").is_file():
        return False
    return bool(layout.units(change_dir))


async def cmd_resume(
    config: SpecConfig,
    *,
    replies_file: str = "",
    max_usd: float | None = sdk.DEFAULT_MAX_USD,
    tickets_in: TicketInputs | None = None,
) -> int:
    if _load(config) is None and not _adoptable(config):
        _no_run(config)
    state_mod.ensure_run_dir(config.run_dir)
    with runlock.hold(config.run_dir):
        run = (
            _start(config, max_usd)
            if _load(config) is not None
            else _adopt(config, max_usd)
        )
        return await advance(run, replies_file=replies_file, tickets_in=tickets_in)


async def cmd_revise(
    config: SpecConfig,
    *,
    from_respec: bool = False,
    max_usd: float | None = sdk.DEFAULT_MAX_USD,
) -> int:
    config.use_archive_if_gone()
    if _load(config) is None and not _adoptable(config):
        _no_run(config)
    state_mod.ensure_run_dir(config.run_dir)
    with runlock.hold(config.run_dir):
        run = (
            _start(config, max_usd)
            if _load(config) is not None
            else _adopt(config, max_usd)
        )
        publish.finish(run)
        await revise.run_revise(run, from_respec=from_respec)
        return EXIT_DONE
