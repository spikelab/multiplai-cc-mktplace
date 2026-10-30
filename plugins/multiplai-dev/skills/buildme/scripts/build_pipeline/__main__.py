"""CLI entry point for the build pipeline.

Subcommands for dev/debug use (only /buildme is user-facing):
  build         — Full orchestrator, from specs pm-spec marked ready (research →
                  design and tasks → build), or an epic pm-spec split
  spec-generate — Design, unknowns, tasks and rubric for a ready change
  tdd           — TDD implementation engine
  findings      — Fix a PR's review comments, reply on the PR, re-review (rounds)
  archive       — Archive a completed change (merge delta specs → main registry)
  setup         — Write or merge specs/config.yaml (the wrapper asks the questions)
  config-get    — Print one resolved specs/config.yaml value (e.g. git.pr)
  plan-revision — Print the planning values to re-bind a build after an upgrade
  epic status   — Print one JSON line per child of an epic; launches nothing

Exit codes: 0 done (an epic runs its children and prints EPIC:SUMMARY), 1 failure
(for an epic: a child failed), 2 usage, or a refusal that wrote nothing
(SPEC:NOT_READY: the specs are not marked ready by pm-spec, changed since, or
are not committed; or the epic's lock is held), 3 waiting on a human or the
wrapper (stdout says which: RESEARCH:NEEDED, SPLIT:REQUIRED, EPIC:MERGE_WAITING,
EPIC:BUDGET or FINDINGS:REVIEW_READY), 4 SPEC:ISSUES:<path> (buildme wrote a
requirements problem for `pm-spec revise` to spec-issues.md) or an agent timed out,
130 or 143 an epic stopped by SIGINT or SIGTERM (129 or 143 for any `build` or
`findings` stopped by SIGHUP or SIGTERM). `build` and `findings` print
SPEND:<usd>:<tokens> last, on every exit path but a kill; `:incomplete` follows
it when a skill they ran (the pr skill, a walkthrough run) printed no spend.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys


def _add_trust_repo_flag(p: argparse.ArgumentParser) -> None:
    """Opt-in gate for commands that spawn auto-approving (bypassPermissions)
    agents. Without it, agent_call refuses to run (see sdk._repo_is_trusted)."""
    p.add_argument(
        "--trust-repo",
        action="store_true",
        help="Confirm you trust this repo's specs/ before running auto-approving "
        "build agents. Required for build/spec-generate/tdd on any "
        "repo you did not author (equivalent to BUILDME_TRUST_REPO=1).",
    )


def _add_lenient_review_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--lenient-review",
        action="store_true",
        help="Accept-and-continue when a block's code review stays below "
        "threshold after all fix iterations (pre-0.4 behavior, for "
        "overnight runs). Default: fail the build.",
    )


def _add_skip_explainers_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--skip-explainers",
        action="store_true",
        help="Skip the unknowns/edge-case explainer pass for dependencies new "
        "to this project. unknowns.md is still written, recording the skip. "
        "Default: explainers off (`explainers: {enabled: false}` in "
        "specs/config.yaml); the flag matters only when "
        "`explainers: {enabled: true}` turns them on.",
    )


def _add_git_lifecycle_flags(p: argparse.ArgumentParser) -> None:
    """Git lifecycle controls. Defaults: worktree on, push on, PR draft.

    `--no-worktree --no-push --no-pr` reproduces the pre-git-lifecycle
    pipeline exactly (build in place, on the caller's branch). The buildme
    SKILL.md wrapper passes --no-worktree when it is already inside one.
    """
    p.add_argument(
        "--no-worktree",
        action="store_true",
        help="Build in the project dir on the current branch instead of "
        "creating a dedicated worktree + branch (pre-0.5 behavior; use "
        "when already inside a worktree).",
    )
    p.add_argument(
        "--no-push",
        action="store_true",
        help="Do not push the build's branch to origin.",
    )
    p.add_argument(
        "--no-pr",
        action="store_true",
        help="Do not open a pull request after pushing.",
    )
    p.add_argument(
        "--pr-ready",
        action="store_true",
        help="Open the PR ready-for-review instead of the default draft.",
    )


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return n


def _usd_limit(value: str) -> float:
    try:
        usd = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be a number of US dollars, not {value}"
        ) from None
    if not math.isfinite(usd) or usd < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {value}")
    return usd


# The review skill's severities (`review_pipeline.models.SEVERITIES`), copied:
# buildme does not import review_pipeline.
_SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def _severity_list(value: str) -> tuple[str, ...]:
    names = tuple(
        dict.fromkeys(v.strip().upper() for v in value.split(",") if v.strip())
    )
    bad = [n for n in names if n not in _SEVERITIES]
    if not names or bad:
        raise argparse.ArgumentTypeError(
            f"must be a comma-separated list of {', '.join(_SEVERITIES)}, not {value!r}"
        )
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_pipeline",
        description="Deterministic build pipeline for the buildme skill ecosystem",
    )
    # Global flag available to all subcommands
    parser.add_argument(
        "--session-id", default="", help="Claude Code session ID for log correlation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- build ---
    build = sub.add_parser("build", help="Full build orchestrator")
    build.add_argument(
        "--change",
        required=True,
        help="Change name: specs/changes/<name>/, its specs marked ready by pm-spec",
    )
    build.add_argument("--project-dir", default=".", help="Project directory")
    build.add_argument("--auto", action="store_true", help="Skip review checkpoint")
    build.add_argument(
        "--spec-only",
        action="store_true",
        help="Stop after the design, tasks and their reviews",
    )
    build.add_argument(
        "--skip-research", action="store_true", help="Skip research phase"
    )
    build.add_argument(
        "--research-path",
        default="",
        help="Research file; its content is saved as research.md in the "
        "change dir and fed to the design",
    )
    build.add_argument(
        "--accept-size",
        action="store_true",
        help="Build a plan the size gate says should be split, as one PR",
    )
    build.add_argument(
        "--approve-plan",
        action="store_true",
        help="Under review.plan_approval: commit your approval of the "
        "implementation plan at its current revision (Approved-by: git "
        "user.name) and go on",
    )
    build.add_argument(
        "--epic-child",
        action="store_true",
        help="This build is an epic's child (the coordinator passes it): "
        "no spec-extract",
    )
    build.add_argument(
        "--base-ref",
        default="",
        help="The commit a new worktree branches from (default: the source "
        "checkout's HEAD); a build that re-binds to its worktree ignores it",
    )
    build.add_argument(
        "--branch",
        default="",
        help="The change's feature branch on origin, where pm-spec pushed its specs; it "
        "starts with <TICKET>- (default: the branch its .change.yaml records, else the one "
        "origin branch named <TICKET>-<change>). A single change builds on it and opens its "
        "PR to the default branch; an epic's children target it",
    )
    build.add_argument(
        "--force",
        action="store_true",
        help="Start a new build even if this change already published",
    )
    _add_trust_repo_flag(build)
    _add_lenient_review_flag(build)
    _add_skip_explainers_flag(build)
    _add_git_lifecycle_flags(build)

    # --- spec-generate ---
    spec = sub.add_parser("spec-generate", help="Artifact generation pipeline")
    spec.add_argument("--change", required=True, help="Change name")
    spec.add_argument("--project-dir", default=".", help="Project directory")
    spec.add_argument("--research-path", default="", help="Path to research output")
    _add_trust_repo_flag(spec)
    _add_skip_explainers_flag(spec)

    # --- tdd ---
    tdd = sub.add_parser("tdd", help="TDD implementation engine")
    tdd.add_argument("--change", required=True, help="Change name")
    tdd.add_argument("--project-dir", default=".", help="Project directory")
    tdd.add_argument("--block", type=int, help="Start from specific block number")
    _add_trust_repo_flag(tdd)
    _add_lenient_review_flag(tdd)

    # --- findings ---
    findings = sub.add_parser(
        "findings",
        help="Fix a PR's review comments through the spec and TDD process, reply, re-review",
    )
    findings.add_argument("--pr", type=int, required=True, help="The PR number")
    findings.add_argument(
        "--change",
        default="",
        help="Change name (default: from the PR's <TICKET>-<name> or buildme/<name> branch)",
    )
    findings.add_argument("--project-dir", default=".", help="Project directory")
    findings.add_argument(
        "--auto",
        action="store_true",
        help="Unattended: post each re-review's Code findings, and its Tests "
        "findings on source files, without the viewer",
    )
    findings.add_argument(
        "--max-rounds",
        type=_positive_int,
        default=None,
        help="Fix rounds before the loop stops (overrides findings.max_rounds)",
    )
    findings.add_argument(
        "--max-usd",
        type=_usd_limit,
        default=None,
        help="Stop before the next round once this process and the PR's reviews "
        "it ran have spent this many US dollars (counted from 0 in each run)",
    )
    findings.add_argument(
        "--severities",
        type=_severity_list,
        default=None,
        help="With --auto, post only the re-review's findings of these "
        "severities, e.g. HIGH,MEDIUM (default: every severity)",
    )
    findings.add_argument(
        "--code-only",
        action="store_true",
        help="With --auto, post only the re-review's Code findings, not its "
        "Tests findings on source files",
    )
    findings.add_argument(
        "--no-review",
        action="store_true",
        help="End each round after its replies, with no re-review (an epic's "
        "final fix)",
    )
    findings.add_argument(
        "--no-escalation",
        action="store_true",
        help="Never run the last round's fix as the escalated fix (an epic's "
        "fixer, whose --max-rounds allows one round at a time)",
    )
    _add_trust_repo_flag(findings)

    # --- archive ---
    archive = sub.add_parser("archive", help="Archive a completed change")
    archive.add_argument("--change", required=True, help="Change name")
    archive.add_argument("--project-dir", default=".", help="Project directory")
    archive.add_argument(
        "--no-merge",
        action="store_true",
        help="Skip merging delta specs into main registry",
    )

    # --- setup ---
    setup = sub.add_parser("setup", help="Write or merge specs/config.yaml")
    setup.add_argument("--project-dir", default=".", help="Project directory")
    setup.add_argument("--architecture", help="architecture (repo-relative path)")
    setup.add_argument("--pr-template", help="pr.template: auto or a path")
    setup.add_argument(
        "--findings-authors", help="findings.authors: collaborators, or login,login,…"
    )

    # --- config-get ---
    cget = sub.add_parser(
        "config-get", help="Print one resolved specs/config.yaml value"
    )
    cget.add_argument("key", help="Dotted key, e.g. git.pr or architecture")
    cget.add_argument("--project-dir", default=".", help="Project directory")

    # --- plan-revision ---
    prev = sub.add_parser(
        "plan-revision",
        help="Print the planning values a build's checkpoint needs after an "
        "upgrade changed how config.yaml is hashed; writes nothing",
    )
    prev.add_argument("--change", required=True, help="Change name")
    prev.add_argument("--project-dir", default=".", help="The build's worktree")

    # --- epic ---
    epic = sub.add_parser("epic", help="Read an epic's state")
    epic_sub = epic.add_subparsers(dest="epic_command", required=True)
    status = epic_sub.add_parser(
        "status",
        help="Print one JSON line per child: state, head, target, blocker, next "
        "action and spend; launches and writes nothing",
    )
    status.add_argument("--change", required=True, help="The epic's change name")
    status.add_argument("--project-dir", default=".", help="Project directory")

    for command in (build, spec, tdd, findings):
        command.add_argument(
            "--accept-unknown-spend",
            action="store_true",
            help="Resume despite prior interrupted calls with unknown usage; "
            "totals remain lower bounds and the cap covers known spend only",
        )
    return parser


# Commands that print `SPEND:<usd>:<tokens>` (budget.spend_marker) on every
# exit path, a raised exception, Ctrl-C, SIGTERM and SIGHUP included: what this
# process spent, which an epic adds up across attempts
# (plan-buildme-epic-mode-2026-10-12.md, item 18). SIGTERM and SIGHUP raise
# SystemExit(128 + signal) for the whole run, as they already did during a
# wait on a skill (`git_ops._exit_on_termination`); an epic's coordinator sends
# SIGTERM to its children when it is stopped. Only a kill (SIGKILL) prints
# nothing; the epic then counts the attempt as unknown.
SPEND_COMMANDS = ("build", "findings", "tdd")


class _ChangeTag(logging.Filter):
    """Puts `[change:<name>] ` in front of each record's message, so the
    daily log tells apart two builds started from one session."""

    def __init__(self, change: str):
        super().__init__()
        self.tag = f"[change:{change}] "

    def filter(self, record: logging.LogRecord) -> bool:
        # One record passes every handler; tag it once.
        if not getattr(record, "buildme_change", False):
            record.msg = self.tag + str(record.msg)
            record.buildme_change = True
        return True


def tag_log_records(change: str) -> None:
    """Tag every record the build's log handlers write with *change*."""
    if not change:
        return
    for name in ("build_pipeline", "code_facts"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, _ChangeTag) for f in handler.filters):
                handler.addFilter(_ChangeTag(change))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in SPEND_COMMANDS:
        return _main(args)
    from . import git_ops

    try:
        with git_ops._exit_on_termination(interrupt=False):
            return _main(args)
    finally:
        from .budget import spend_marker

        print(spend_marker(), flush=True)


def _main(args: argparse.Namespace) -> int:
    # Earliest common entry point for every subcommand that takes --change:
    # refuse a name that normalizes to nothing (e.g. '!!!') here, cleanly,
    # instead of dying much later on a 'buildme/' branch or a collapsed
    # specs/changes path (normalize_change_name raises ValueError for these).
    change = getattr(args, "change", "") or ""
    if change:
        from .change_manager import normalize_change_name

        try:
            normalize_change_name(change)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    # Input files: relative paths are read from --project-dir (the process
    # cwd is the scripts dir under `uv run --directory`). Whether a named file
    # that does not exist is a usage error depends on the checkpoint, so the
    # orchestrator decides that (intake.check_input_files).
    if args.command == "build":
        from .intake import resolve_input_paths

        resolve_input_paths(args)

    # Propagate the repo-trust opt-in to the agent layer (sdk._repo_is_trusted).
    if getattr(args, "trust_repo", False):
        os.environ["BUILDME_TRUST_REPO"] = "1"

    from multiplai_core.log_utils import setup_logging

    from .config import BuildConfig
    from .env import load_env

    load_env()
    session_id = getattr(args, "session_id", "") or ""
    # setup_logging always attaches a stderr handler and derives the log file
    # from the component name. Every build_pipeline.*, code_facts.* and
    # multiplai_core.* record at INFO and above lands in that file.
    logger = setup_logging(
        "build-pipeline",
        session_id=session_id,
        packages=("build_pipeline", "code_facts"),
    )
    logger.info(
        "Session settings: model=%s effort=%s",
        os.environ.get("MULTIPLAI_SESSION_MODEL") or "not found",
        os.environ.get("MULTIPLAI_SESSION_EFFORT") or "not found",
    )

    if args.command == "epic":
        from .epic.status import run_cli as run_epic_status

        return run_epic_status(args)

    if args.command == "setup":
        from .setup import run_setup

        return run_setup(args)

    elif args.command == "config-get":
        from .setup import config_get

        return config_get(args)

    elif args.command == "plan-revision":
        import json
        from pathlib import Path

        from .spec_generator import rebound_values

        config = BuildConfig(
            project_dir=Path(args.project_dir).resolve(), change_name=args.change
        )
        config.specs_dir = config.project_dir / "specs"
        print(json.dumps(rebound_values(config), indent=2))
        return 0

    elif args.command == "build":
        from .orchestrator import run_orchestrator

        config = BuildConfig.from_cli_args(args)
        tag_log_records(config.change_name)
        return asyncio.run(run_orchestrator(config, args))

    elif args.command == "spec-generate":
        from .spec_generator import run_spec_generator

        config = BuildConfig.from_cli_args(args)
        tag_log_records(config.change_name)
        return asyncio.run(run_spec_generator(config, args))

    elif args.command == "tdd":
        from .tdd_engine import run_tdd_engine

        config = BuildConfig.from_cli_args(args)
        tag_log_records(config.change_name)
        return asyncio.run(run_tdd_engine(config, args))

    elif args.command == "findings":
        from .findings.cli import run_cli

        return run_cli(args)

    elif args.command == "archive":
        from .change_manager import ChangeManager

        config = BuildConfig.from_cli_args(args)
        cm = ChangeManager(config.specs_dir)
        change_dir = config.change_dir
        if not change_dir.exists():
            print(
                f"ERROR: Change '{args.change}' not found at {change_dir}",
                file=sys.stderr,
            )
            return 1
        from multiplai_core.specs import change_root

        if change_root(change_dir) != change_dir:
            # A change with components archives once, all of them together.
            print(
                f"ERROR: '{args.change}' is a component; archive its change: "
                f"--change {change_root(change_dir).name}",
                file=sys.stderr,
            )
            return 1
        dest = cm.archive_change(change_dir, merge_specs=not args.no_merge)
        print(f"Archived to: {dest}")
        return 0

    return 2


if __name__ == "__main__":
    from multiplai_core import pin_session_settings

    # First, before any step or child process starts: every model call of
    # this run sees the session's model and effort as they were at launch.
    pin_session_settings()
    sys.exit(main())
