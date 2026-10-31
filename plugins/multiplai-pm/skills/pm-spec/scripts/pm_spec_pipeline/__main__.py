"""pm-spec command line.

    python -m pm_spec_pipeline new <change> --ticket-ref REF [--project-dir DIR] [--branch NAME]
        [--prd FILE] [--ticket-file FILE [--ticket-url URL] [--ticket-child REF ...]]
        [--plan-file FILE] [--context-files FILE ...] [--interview-summary TEXT]
        [--interview-mode self|user|ticket] [--interview-replies FILE] [--auto]
        [--prototype | --no-prototype] [--no-epic-split] [--trust-repo] [--max-usd USD]
    python -m pm_spec_pipeline resume <change> [--project-dir DIR] [--branch NAME]
        [--ticket-ref REF] [--ticket COMPONENT=REF ...] [--tickets-later]
        [--interview-replies FILE] [--trust-repo] [--max-usd USD]
    python -m pm_spec_pipeline revise <change> [--project-dir DIR] [--branch NAME]
        [--ticket-ref REF] [--from-respec] [--trust-repo] [--max-usd USD]

The specs are written on the change's feature branch (`<TICKET>-<change>`, or
`--branch`, which starts with `<TICKET>-` too), in pm-spec's own worktree of
it, and pushed to origin (`branch.py`). A change with no ticket ref, of its
own or a parent's, is refused (exit 2) until `--ticket-ref` names one.

pm-spec stops for review after the proposal, after the components and after
the specs (`SPEC:REVIEW:<stage>`), and waits for the components' tickets
(`SPEC:TICKETS:<path>`); `resume` goes on. `--auto` never stops.

Exit codes: 0 ready, 1 failed, 2 refused, 3 waiting (for a review, the
tickets or interview replies), 4 blocked on findings, 5 another run holds the
lock. The last line is `SPEND:<usd>:<tokens>`, what this process's model
calls cost.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from multiplai_core.specs import normalize_change_name

from . import branch, runlock, sdk
from . import state as state_mod
from .config import INTERVIEW_MODES, SpecConfig, read_specs_config
from .pipeline import NewInputs, TicketInputs, cmd_new, cmd_resume, cmd_revise
from .run import EXIT_FAILED, EXIT_LOCKED, EXIT_REFUSED, Stop

log = logging.getLogger("pm_spec_pipeline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_spec_pipeline",
        description="Write a change's specs, check they agree, and mark them ready.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("change", help="The change name (specs/changes/<change>/)")
        p.add_argument(
            "--project-dir",
            default=".",
            help="The repository (default: here); relative paths are read from it",
        )
        p.add_argument(
            "--branch",
            default="",
            help="The feature branch the specs go on; it starts with <TICKET>- (default: the "
            "one the change records, else <TICKET>-<change>)",
        )
        p.add_argument(
            "--trust-repo",
            action="store_true",
            help="Confirm you trust the repository: the interview, prototype and consistency agents "
            f"read it (equivalent to {sdk.TRUST_ENV}=1)",
        )
        p.add_argument(
            "--max-usd",
            type=float,
            default=sdk.DEFAULT_MAX_USD,
            help=f"Spend cap for the change's run, across resumes (default {sdk.DEFAULT_MAX_USD:g})",
        )

    def ticket_ref(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--ticket-ref",
            default="",
            help="The ticket's ref, as an opaque string; the feature branch is named after it "
            "(<TICKET>-<change>). Needed when neither the change nor a parent records one",
        )

    new = sub.add_parser("new", help="Start the specs of a change")
    common(new)
    new.add_argument(
        "--prd", default="", help="A PRD: the primary source, saved as prd.md"
    )
    new.add_argument(
        "--ticket-file",
        default="",
        help="The ticket's text (with its children's), as fetched",
    )
    ticket_ref(new)
    new.add_argument("--ticket-url", default="", help="The ticket's URL")
    new.add_argument(
        "--ticket-child",
        action="append",
        default=[],
        metavar="REF",
        help="The ref of one of the ticket's child items; once per child",
    )
    new.add_argument(
        "--plan-file",
        default="",
        help="A plan the interview reads, kept in interview.md",
    )
    new.add_argument(
        "--context-files",
        nargs="*",
        default=[],
        help="Further files the interview reads",
    )
    new.add_argument(
        "--interview-summary",
        default="",
        help="The summary of an interview the caller ran",
    )
    new.add_argument(
        "--interview-mode",
        choices=INTERVIEW_MODES,
        default="",
        help="self, user or ticket (default: specs/config.yaml `interview:`, else self with "
        "--auto, user without)",
    )
    new.add_argument(
        "--interview-replies",
        default="",
        help="ticket mode: the replies to the questions",
    )
    new.add_argument(
        "--auto",
        action="store_true",
        help="No human in the loop: the interview answers itself",
    )
    proto = new.add_mutually_exclusive_group()
    proto.add_argument(
        "--prototype",
        dest="prototype",
        action="store_const",
        const="true",
        default="",
        help="Run the prototype even when specs/config.yaml turns it off",
    )
    proto.add_argument(
        "--no-prototype",
        dest="prototype",
        action="store_const",
        const="false",
        help="Never run the prototype",
    )
    new.add_argument(
        "--no-epic-split", action="store_true", help="Keep the work as one component"
    )

    resume = sub.add_parser(
        "resume", help="Continue a change's run from where it stopped"
    )
    common(resume)
    ticket_ref(resume)
    resume.add_argument(
        "--interview-replies",
        default="",
        help="ticket mode: the replies to the questions",
    )
    resume.add_argument(
        "--ticket",
        action="append",
        default=[],
        metavar="COMPONENT=REF",
        help="At the ticket wait: the ticket created for a component; once per component",
    )
    resume.add_argument(
        "--tickets-later",
        action="store_true",
        help="At the ticket wait: go on, the components' tickets left pending",
    )
    resume.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="ID",
        help="At a consistency block after the specs review: leave this finding "
        "(CC-F<n>) as it is; repeatable, or comma-separated",
    )
    resume.add_argument(
        "--accept-reason",
        default="",
        help="Why the --accept findings are left as they are (required with --accept)",
    )
    resume.add_argument(
        "--check-again",
        action="store_true",
        help="Run the consistency check once more after it hit its round cap",
    )

    revise = sub.add_parser(
        "revise",
        help="Fix the requirements issues in spec-issues.md, then hand off again",
    )
    common(revise)
    ticket_ref(revise)
    revise.add_argument(
        "--from-respec",
        action="store_true",
        help="Read respec.md instead of spec-issues.md",
    )
    return parser


def _path(raw: str, base: str) -> str:
    if not raw:
        return ""
    path = os.path.expanduser(raw)
    return path if os.path.isabs(path) else os.path.join(os.path.abspath(base), path)


def _ticket_refs(args: argparse.Namespace) -> dict[str, str] | None:
    """`resume --ticket <component>=<REF>`, each checked as a ticket id
    (`branch._ticket_refusal`); None, the reason printed, when one is not."""
    refs: dict[str, str] = {}
    project = Path(args.project_dir).expanduser()
    repo = project if project.is_dir() else Path.cwd()
    change = normalize_change_name(args.change)
    for raw in getattr(args, "ticket", []):
        component, sep, ref = raw.partition("=")
        if not sep or not component.strip() or not ref.strip():
            print(
                f"ERROR: --ticket {raw!r}: expected <component>=<ticket id>",
                flush=True,
            )
            return None
        if why := branch._ticket_refusal(ref.strip(), change, repo):
            print(f"ERROR: --ticket {raw!r}: {why}. Nothing was recorded.", flush=True)
            return None
        refs[component.strip()] = ref.strip()
    return refs


def _component_refusal(name: str, command: str) -> str:
    """Why *name* is refused, or "": a `/` in it. One run writes and revises a
    change with all its components, so `<change>/<component>` names no run of
    its own; normalizing it would run the two together
    (`word-freq/word-frequencies` became `word-freqword-frequencies`)."""
    if "/" not in name:
        return ""
    change, _, component = name.partition("/")
    hint = (
        f" {name} names the component {component} of the change {change}: "
        f"run `{command} {change}`."
        if change.strip(".") and component and "/" not in component
        else ""
    )
    return (
        f"{name} is not a change name: a change name has no '/', and pm-spec "
        f"runs a change with all its components.{hint}"
    )


async def _dispatch(args: argparse.Namespace) -> int:
    if why := _component_refusal(args.change, args.command):
        print(f"ERROR: {why}", flush=True)
        return EXIT_REFUSED
    if args.command == "new" and args.no_epic_split and len(args.ticket_child) > 1:
        print(
            "ERROR: --no-epic-split keeps the work as one component, which can "
            "deliver only one --ticket-child; give at most one, or drop "
            "--no-epic-split. Nothing was written.",
            flush=True,
        )
        return EXIT_REFUSED
    # Checked before anything is written (row S6).
    refs = _ticket_refs(args) if args.command == "resume" else {}
    if refs is None:
        return EXIT_REFUSED
    # The checkout the run writes in is the feature branch's (`branch.place`);
    # the input files are still read from `--project-dir`.
    where = branch.place(
        args.project_dir,
        args.change,
        args.branch,
        command=args.command,
        ticket=args.ticket_ref,
    )
    branch.prepare(where)
    branch.adopt(where, args.project_dir, args.change, args.ticket_ref.strip())
    if args.command != "new":
        branch.record_ticket(
            where.project_dir
            / "specs"
            / "changes"
            / normalize_change_name(args.change),
            args.ticket_ref.strip(),
        )
    config = SpecConfig(
        project_dir=where.project_dir,
        change_name=args.change,
        auto=bool(getattr(args, "auto", False)),
        # `specs/config.yaml` as `--project-dir` holds it, else as the branch does.
        data=read_specs_config(Path(args.project_dir).expanduser() / "specs"),
        place=where,
    )
    replies = _path(getattr(args, "interview_replies", ""), args.project_dir)
    if args.command == "new":
        inputs = NewInputs(
            prd=_path(args.prd, args.project_dir),
            ticket_file=_path(args.ticket_file, args.project_dir),
            ticket_ref=args.ticket_ref,
            ticket_url=args.ticket_url,
            ticket_children=args.ticket_child,
            plan_file=_path(args.plan_file, args.project_dir),
            context_files=[_path(f, args.project_dir) for f in args.context_files],
            interview_summary=args.interview_summary,
            interview_mode=args.interview_mode,
            prototype=args.prototype,
            epic_split=not args.no_epic_split,
        )
        return await cmd_new(config, inputs, replies_file=replies, max_usd=args.max_usd)
    if args.command == "resume":
        return await cmd_resume(
            config,
            replies_file=replies,
            max_usd=args.max_usd,
            tickets_in=TicketInputs(refs=refs, later=args.tickets_later),
            accept=[i.strip() for a in args.accept for i in a.split(",") if i.strip()],
            accept_reason=args.accept_reason,
            check_again=args.check_again,
        )
    return await cmd_revise(config, from_respec=args.from_respec, max_usd=args.max_usd)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help, or a usage error argparse already printed
        return e.code if isinstance(e.code, int) else EXIT_REFUSED
    if args.trust_repo:
        os.environ[sdk.TRUST_ENV] = "1"
    from multiplai_core.log_utils import setup_logging

    setup_logging(
        "pm-spec",
        session_id=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        packages=("pm_spec_pipeline",),
    )
    try:
        return asyncio.run(_dispatch(args))
    except Stop as e:
        return e.code
    except runlock.Locked as e:
        print(f"pm-spec: {e}", flush=True)
        return EXIT_LOCKED
    except sdk.RepoTrustError as e:
        print(f"pm-spec: {e}", flush=True)
        return EXIT_REFUSED
    except state_mod.Unreadable as e:
        print(f"pm-spec: {e}", flush=True)
        return EXIT_REFUSED
    except sdk.BudgetExceededError as e:
        print(
            f"pm-spec: {e}. Everything paid for is saved; run `resume` with a higher --max-usd "
            "to continue.",
            flush=True,
        )
        return EXIT_FAILED
    except Exception as e:  # every other failure is exit 1 with its message
        log.debug("pm-spec failed", exc_info=True)
        print(f"pm-spec: failed: {e}", flush=True)
        return EXIT_FAILED


def cli(argv: Sequence[str] | None = None) -> int:
    """`main`, then `SPEND:<usd>:<tokens>` last, on every exit but a kill."""
    try:
        return main(argv)
    finally:
        print(sdk.spend_marker(), flush=True)


if __name__ == "__main__":
    from multiplai_core import pin_session_settings

    pin_session_settings()
    sys.exit(cli())
