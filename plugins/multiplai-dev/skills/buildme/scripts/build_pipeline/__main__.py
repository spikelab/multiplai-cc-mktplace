"""CLI entry point for the build pipeline.

Subcommands for dev/debug use (only /buildme is user-facing):
  build         — Full orchestrator (mode detect → interview → research → specs → build)
  spec-generate — Artifact generation pipeline
  tdd           — TDD implementation engine
  findings      — Fix a PR's review comments, reply on the PR, re-review (rounds)
  archive       — Archive a completed change (merge delta specs → main registry)
  setup         — Write or merge specs/config.yaml (the wrapper asks the questions)
  config-get    — Print one resolved specs/config.yaml value (e.g. intake.provider)

Exit codes: 0 done (an approved epic prints EPIC:APPROVED, runs its children and
prints EPIC:SUMMARY), 1 failure (for an epic: a child failed), 2 usage,
or a refusal that wrote nothing (the epic's lock is held, or an approved epic
file changed), 3 waiting on a human or the wrapper
(stdout says which: INTERVIEW:WAITING, RESEARCH:NEEDED, SPLIT:REQUIRED, EPIC:SPLIT,
EPIC:MERGE_WAITING or FINDINGS:REVIEW_READY), 4 an agent timed out, 130 or 143 an
epic stopped by SIGINT or SIGTERM.
"""

from __future__ import annotations

import argparse
import asyncio
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
             "Default: explainers on (also settable as "
             "`explainers: {enabled: false}` in specs/config.yaml).",
    )


def _add_prototype_flags(p: argparse.ArgumentParser) -> None:
    """Override the prototype stage's auto-detection in either direction.
    Without a flag, `prototype: {enabled: auto|true|false}` in
    specs/config.yaml decides; with neither, the applicability rule does."""
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--prototype",
        action="store_true",
        help="Always run the prototype stage before the build, whatever the "
             "change type.",
    )
    group.add_argument(
        "--no-prototype",
        action="store_true",
        help="Skip the prototype stage (the skip and its reason are logged).",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_pipeline",
        description="Deterministic build pipeline for the buildme skill ecosystem",
    )
    # Global flag available to all subcommands
    parser.add_argument("--session-id", default="", help="Claude Code session ID for log correlation")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- build ---
    build = sub.add_parser("build", help="Full build orchestrator")
    build.add_argument("--mode", choices=["scratch", "brief", "only"], default="scratch")
    build.add_argument("--change", default="", help="Change name (required for build-only)")
    build.add_argument("--project-dir", default=".", help="Project directory")
    build.add_argument("--auto", action="store_true", help="Skip review checkpoint")
    build.add_argument("--spec-only", action="store_true", help="Stop after spec generation")
    build.add_argument("--skip-research", action="store_true", help="Skip research phase")
    build.add_argument("--interview-summary", default="", help="Pre-gathered interview summary")
    build.add_argument("--context-files", nargs="*", default=[], help="Brief/context file paths")
    build.add_argument("--research-path", default="",
                       help="Research file; its content is saved as research.md in the "
                            "change dir and fed to the proposal")
    build.add_argument("--ticket-file", default="",
                       help="Ticket the wrapper fetched, as Markdown; saved (fenced as "
                            "untrusted) as ticket.md in the change dir")
    build.add_argument("--ticket-ref", default="", help="Ticket reference, e.g. DB-2267 or #12")
    build.add_argument("--ticket-url", default="", help="Ticket URL")
    build.add_argument("--plan-file", default="",
                       help="A plan-skill plan (# Plan: + ## Done means); recorded and "
                            "saved as plan.md")
    build.add_argument("--interview-replies", default="",
                       help="interview: ticket — file holding the ticket replies to the "
                            "posted questions")
    build.add_argument("--accept-size", action="store_true",
                       help="Build a plan the size gate says should be split, as one PR")
    build.add_argument("--approve-split", action="store_true",
                       help="Approve the epic split this build printed as EPIC:SPLIT, "
                            "as split.md and plans/ are on disk now")
    build.add_argument("--no-epic-split", action="store_true",
                       help="Build the input as one change: skip the EPIC_SPLIT decision "
                            "(an epic's child builds pass this)")
    build.add_argument("--base-ref", default="",
                       help="The commit a new worktree branches from (default: the source "
                            "checkout's HEAD); a build that re-binds to its worktree ignores it")
    _add_trust_repo_flag(build)
    _add_lenient_review_flag(build)
    _add_skip_explainers_flag(build)
    _add_prototype_flags(build)
    _add_git_lifecycle_flags(build)

    # --- spec-generate ---
    spec = sub.add_parser("spec-generate", help="Artifact generation pipeline")
    spec.add_argument("--change", required=True, help="Change name")
    spec.add_argument("--project-dir", default=".", help="Project directory")
    spec.add_argument("--interview-summary", default="", help="Interview summary text")
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
        help="Fix a PR's review comments through the spec and TDD process, reply, re-review")
    findings.add_argument("--pr", type=int, required=True, help="The PR number")
    findings.add_argument("--change", default="",
                          help="Change name (default: from the PR's buildme/<name> branch)")
    findings.add_argument("--project-dir", default=".", help="Project directory")
    findings.add_argument("--auto", action="store_true",
                          help="Unattended: post each re-review's Code findings, and its Tests "
                               "findings on source files, without the viewer")
    findings.add_argument("--max-rounds", type=_positive_int, default=None,
                          help="Fix rounds before the loop stops (overrides findings.max_rounds)")
    _add_trust_repo_flag(findings)

    # --- archive ---
    archive = sub.add_parser("archive", help="Archive a completed change")
    archive.add_argument("--change", required=True, help="Change name")
    archive.add_argument("--project-dir", default=".", help="Project directory")
    archive.add_argument("--no-merge", action="store_true", help="Skip merging delta specs into main registry")

    # --- setup ---
    setup = sub.add_parser("setup", help="Write or merge specs/config.yaml")
    setup.add_argument("--project-dir", default=".", help="Project directory")
    setup.add_argument("--provider", choices=["plane", "github", "none"],
                       help="intake.provider")
    setup.add_argument("--plane-project", help="intake.plane.project (Plane project key)")
    setup.add_argument("--interview", choices=["self", "ticket", "user"], help="interview")
    setup.add_argument("--architecture", help="architecture (repo-relative path)")
    setup.add_argument("--pr-template", help="pr.template: auto or a path")
    setup.add_argument("--findings-authors",
                       help="findings.authors: collaborators, or login,login,…")

    # --- config-get ---
    cget = sub.add_parser("config-get", help="Print one resolved specs/config.yaml value")
    cget.add_argument("key", help="Dotted key, e.g. intake.provider or interview")
    cget.add_argument("--project-dir", default=".", help="Project directory")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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

    from .config import BuildConfig
    from .env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    session_id = getattr(args, "session_id", "") or ""
    # setup_logging always attaches a stderr handler and derives the log file
    # from the component name.
    logger = setup_logging(
        "build-pipeline",
        session_id=session_id,
    )

    if args.command == "setup":
        from .setup import run_setup
        return run_setup(args)

    elif args.command == "config-get":
        from .setup import config_get
        return config_get(args)

    elif args.command == "build":
        from .orchestrator import run_orchestrator
        config = BuildConfig.from_cli_args(args)
        return asyncio.run(run_orchestrator(config, args))

    elif args.command == "spec-generate":
        from .spec_generator import run_spec_generator
        config = BuildConfig.from_cli_args(args)
        return asyncio.run(run_spec_generator(config, args))

    elif args.command == "tdd":
        from .tdd_engine import run_tdd_engine
        config = BuildConfig.from_cli_args(args)
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
            print(f"ERROR: Change '{args.change}' not found at {change_dir}", file=sys.stderr)
            return 1
        dest = cm.archive_change(change_dir, merge_specs=not args.no_merge)
        print(f"Archived to: {dest}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
