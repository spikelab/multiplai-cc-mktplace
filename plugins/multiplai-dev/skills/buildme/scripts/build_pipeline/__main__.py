"""CLI entry point for the build pipeline.

Subcommands for dev/debug use (only /buildme is user-facing):
  build         — Full orchestrator (mode detect → interview → research → specs → build)
  spec-generate — Artifact generation pipeline
  tdd           — TDD implementation engine
  apply         — Manual single-agent change application
  archive       — Archive a completed change (merge delta specs → main registry)
  migrate       — Migrate legacy openspec/ layout to new specs/ layout
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
             "build agents. Required for build/spec-generate/tdd/apply on any "
             "repo you did not author (equivalent to BUILDME_TRUST_REPO=1).",
    )


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
    _add_trust_repo_flag(build)

    # --- spec-generate ---
    spec = sub.add_parser("spec-generate", help="Artifact generation pipeline")
    spec.add_argument("--change", required=True, help="Change name")
    spec.add_argument("--project-dir", default=".", help="Project directory")
    spec.add_argument("--interview-summary", default="", help="Interview summary text")
    spec.add_argument("--research-path", default="", help="Path to research output")
    _add_trust_repo_flag(spec)

    # --- tdd ---
    tdd = sub.add_parser("tdd", help="TDD implementation engine")
    tdd.add_argument("--change", required=True, help="Change name")
    tdd.add_argument("--project-dir", default=".", help="Project directory")
    tdd.add_argument("--block", type=int, help="Start from specific block number")
    _add_trust_repo_flag(tdd)

    # --- apply ---
    apply_ = sub.add_parser("apply", help="Manual single-agent change application")
    apply_.add_argument("--change", default="", help="Change name (auto-selects if only one)")
    apply_.add_argument("--project-dir", default=".", help="Project directory")
    apply_.add_argument("--block", type=int, help="Start from specific block number")
    _add_trust_repo_flag(apply_)

    # --- archive ---
    archive = sub.add_parser("archive", help="Archive a completed change")
    archive.add_argument("--change", required=True, help="Change name")
    archive.add_argument("--project-dir", default=".", help="Project directory")
    archive.add_argument("--no-merge", action="store_true", help="Skip merging delta specs into main registry")

    # --- migrate ---
    migrate = sub.add_parser("migrate", help="Migrate legacy openspec/ to new specs/ layout")
    migrate.add_argument("--project-dir", default=".", help="Project directory")
    migrate.add_argument("--dry-run", action="store_true", help="Show what would change without modifying anything")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Propagate the repo-trust opt-in to the agent layer (sdk._repo_is_trusted).
    if getattr(args, "trust_repo", False):
        os.environ["BUILDME_TRUST_REPO"] = "1"

    from .config import BuildConfig
    from .env import load_env
    from log_utils import setup_logging

    load_env()
    session_id = getattr(args, "session_id", "") or ""
    logger = setup_logging(
        "build-pipeline",
        session_id=session_id,
        stderr=True,
        package="build_pipeline",
    )

    if args.command == "build":
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

    elif args.command == "apply":
        from .apply import run_apply
        config = BuildConfig.from_cli_args(args)
        return asyncio.run(run_apply(config, args))

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

    elif args.command == "migrate":
        from pathlib import Path
        from .migrate import run_migrate
        return run_migrate(Path(args.project_dir), dry_run=args.dry_run)

    return 2


if __name__ == "__main__":
    sys.exit(main())
