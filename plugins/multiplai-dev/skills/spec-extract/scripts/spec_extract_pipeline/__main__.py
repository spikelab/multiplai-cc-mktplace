"""spec-extract command line.

    python -m spec_extract_pipeline run (--tree [COMMIT] [--path DIR] | --pr N | --dir PATH)
        [--repo DIR] [--out ROOT] [--fetch] [--restart]

Running the same command again resumes the run in the same folder. Exit codes:
0 done, 1 failed, 2 refused, 5 another run holds the lock. This version stops
after the resolve step and prints `SPEC_EXTRACT:RESOLVED <run>`.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import runlock
from . import state as state_mod
from . import target

log = logging.getLogger("spec_extract_pipeline")

EXIT_DONE, EXIT_FAILED, EXIT_REFUSED, EXIT_LOCKED = 0, 1, 2, 5


def default_out() -> Path:
    """`<data_dir>/spec-extract`: multiplai-core's data directory, so every checkout and
    session writes one place, never the working tree of the repository being read."""
    from multiplai_core.paths import get_paths

    return Path(get_paths().data_dir) / "spec-extract"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spec_extract_pipeline",
                                     description="Write BDD capability specs from Python code.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run, or resume, spec-extract on one target.")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--tree", nargs="?", const="HEAD", metavar="COMMIT",
                       help="Read every Python file at COMMIT (default HEAD)")
    which.add_argument("--pr", type=int, metavar="N", help="Read the files PR N changed, at its head")
    which.add_argument("--dir", metavar="PATH",
                       help="Read a directory that is not under git; it is copied into the run folder")
    run.add_argument("--path", help="With --tree: only this directory of the commit")
    run.add_argument("--repo", default=".", help="The checkout for --tree and --pr (default: here)")
    run.add_argument("--out", type=Path, help="Root of the run folders (default: <data_dir>/spec-extract)")
    run.add_argument("--fetch", action="store_true", help="With --pr: fetch origin and the PR's head first")
    run.add_argument("--restart", action="store_true",
                     help="Rename the run folder to <object>.old-<timestamp> (never deleted) and start again")
    return parser


def _run(args: argparse.Namespace, now: Callable[[], datetime]) -> int:
    root = (args.out or default_out()).expanduser().resolve()
    fresh = target.resolve(args.repo, tree=args.tree, path=args.path, pr=args.pr, dir=args.dir,
                           fetch=args.fetch)
    with contextlib.ExitStack() as lock:
        run_dir = target.run_folder(root, fresh, lock)
        if args.restart:
            old = target.restart(run_dir, now())
            if old is not None:
                print(f"SPEC_EXTRACT:RESTARTED {run_dir} (the earlier run is in {old})")
        saved = state_mod.load(run_dir)  # an unreadable state.json stops here, writing nothing (R23)
        if fresh.kind == "dir":
            fresh = target.import_dir(fresh, run_dir, now())
        if saved is None:
            state = state_mod.State(target=fresh)
            state_mod.save(state, run_dir)
        else:
            state = saved
            if fresh.kind == "pr":
                # A resumed --pr keeps the base it started with: re-resolving after the base
                # branch moved, or the PR merged, gives another merge-base (R26).
                fresh = fresh.model_copy(update={"base_sha": saved.target.base_sha})
        target.snapshot(fresh, run_dir)
        if "resolve" not in state.steps_done:
            state.steps_done.append("resolve")
            state_mod.save(state, run_dir)
        print(f"SPEC_EXTRACT:RESOLVED {run_dir}", flush=True)
    return EXIT_DONE


def main(argv: Sequence[str] | None = None, *, now: Callable[[], datetime] | None = None) -> int:
    """Run the command line; return the exit code. *now* is the run's one clock."""
    clock = now or (lambda: datetime.now(timezone.utc))
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help, or a usage error argparse already printed
        return e.code if isinstance(e.code, int) else EXIT_REFUSED
    try:
        return _run(args, clock)
    except target.TargetError as e:
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except runlock.Locked as e:
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_LOCKED
    except state_mod.Unreadable as e:
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:  # noqa: BLE001 — every other failure is exit 1 with its message
        log.debug("spec-extract failed", exc_info=True)
        print(f"spec-extract: failed: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
