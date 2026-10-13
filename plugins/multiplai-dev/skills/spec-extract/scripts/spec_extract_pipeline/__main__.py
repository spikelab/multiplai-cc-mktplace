"""spec-extract command line.

    python -m spec_extract_pipeline run (--tree [COMMIT] [--path DIR] | --pr N | --dir PATH)
        [--repo DIR] [--out ROOT] [--fetch] [--restart]
        [--max-usd USD] [--max-units N] [--plan-only] [--trust-repo]
        [--apply [--specs-dir DIR]]

Running the same command again resumes the run in the same folder. Exit codes:
0 done, 1 failed, 2 refused, 3 the run folder's spend reached --max-usd,
4 the repository is not trusted, 5 another run holds the lock. It resolves,
scopes, explores, groups, writes and renders, then prints
`SPEC_EXTRACT:RENDERED <files> <run>/registry`; with `--apply` it then copies
the new registry files into `<specs-dir>/registry/` and prints
`SPEC_EXTRACT:APPLIED <n>`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import apply as apply_mod
from . import explore, gate, group, runlock, sdk, write
from . import render as render_mod
from . import report as report_mod
from . import scope as scope_mod
from . import state as state_mod
from . import target

log = logging.getLogger("spec_extract_pipeline")

EXIT_DONE, EXIT_FAILED, EXIT_REFUSED, EXIT_BUDGET, EXIT_UNTRUSTED, EXIT_LOCKED = 0, 1, 2, 3, 4, 5
PLAN_FILE = "plan.txt"


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
    run.add_argument("--max-usd", type=float, default=sdk.DEFAULT_MAX_USD,
                     help="Spend cap for this run folder, counted across every run that resumes it "
                          f"(default {sdk.DEFAULT_MAX_USD:g})")
    run.add_argument("--max-units", type=int, default=scope_mod.DEFAULT_MAX_UNITS,
                     help=f"Refuse a target that splits into more explore units (default {scope_mod.DEFAULT_MAX_UNITS})")
    run.add_argument("--plan-only", action="store_true",
                     help="Resolve and scope, print the units and the number of calls, write plan.txt, "
                          "and stop before any model call")
    run.add_argument("--trust-repo", action="store_true",
                     help="Confirm you trust the repository: its contents are sent to a model, which reads "
                          f"the tree (equivalent to {sdk.TRUST_ENV}=1)")
    run.add_argument("--apply", action="store_true",
                     help="After render, copy each new capability file into <specs-dir>/registry/; "
                          "a file already there is left untouched. Commits nothing")
    run.add_argument("--specs-dir", metavar="DIR",
                     help="With --apply: the specs directory (default <repo>/specs; required with --dir)")
    return parser


def _run(args: argparse.Namespace, now: Callable[[], datetime]) -> int:
    if args.trust_repo:
        os.environ[sdk.TRUST_ENV] = "1"
    apply_mod.refuse_early(args.apply, args.dir, args.specs_dir)
    root = (args.out or default_out()).expanduser().resolve()
    fresh = target.resolve(args.repo, tree=args.tree, path=args.path, pr=args.pr, dir=args.dir,
                           fetch=args.fetch)
    with contextlib.ExitStack() as lock:
        run_dir = target.run_folder(root, fresh, lock)
        if args.apply:  # refuse a --specs-dir apply could not use before paying for anything
            apply_mod.registry_dir(fresh, args.specs_dir, run_dir)
        if args.restart:
            old = target.restart(run_dir, now())
            if old is not None:
                print(f"SPEC_EXTRACT:RESTARTED {run_dir} (the earlier run is in {old})")
        saved = state_mod.load(run_dir)  # an unreadable state.json stops here, writing nothing (R23)
        if fresh.kind == "dir":
            fresh = target.import_dir(fresh, run_dir, now(),
                                      saved_head=saved.target.head_sha if saved else None)
        if saved is None:
            state = state_mod.State(target=fresh)
            state_mod.save(state, run_dir)
        else:
            # The saved target is never rewritten: a resumed --pr keeps the base_sha it
            # started with, which later steps read from state.target. Re-resolving after
            # the base branch moved, or the PR merged, gives another merge-base (R26).
            state = saved
        # After render nothing reads the snapshot: it is deleted, never extracted again (R10).
        rendered = "render" in state.steps_done
        if not rendered:
            target.snapshot(fresh, run_dir)
        if "resolve" not in state.steps_done:
            state.steps_done.append("resolve")
            state_mod.save(state, run_dir)
        print(f"SPEC_EXTRACT:RESOLVED {run_dir}", flush=True)
        # The saved target, read through this run's checkout. The run folder is shared by
        # every checkout of the remote (Decided 14), and the one the run started in may be
        # gone (row R30). Every other field, base_sha included, is the saved one (R26).
        git_target = state.target.model_copy(update={"repo": fresh.repo})

        # Scope: computed once from the saved target, then read from state on every
        # later step and every resume, never recomputed (R26).
        if state.scope is None:
            computed = scope_mod.compute(git_target)
            scope_mod.check(computed, args.max_units)
            state.scope = computed
            state.steps_done.append("scope")
            state_mod.save(state, run_dir)
        plan = scope_mod.plan_text(state.target, state.scope, report_mod.describe(state.target))
        if not rendered:  # after render the outputs are left alone (R24)
            report_mod.write_output(run_dir, PLAN_FILE, plan)
        if args.plan_only:
            print(plan, end="")
            print(f"SPEC_EXTRACT:PLAN {run_dir / PLAN_FILE}", flush=True)
            return EXIT_DONE
        registry = run_dir / render_mod.REGISTRY_DIR
        if rendered:  # R10: finish deleting the snapshot; never re-render
            render_mod.drop_snapshot(run_dir)
            print(f"SPEC_EXTRACT:RENDERED {len(list(registry.glob('*.md')))} {registry}", flush=True)
            return _apply(args, state, run_dir, git_target)

        ledger = sdk.start_budget(args.max_usd, state.budget)
        if "explore" not in state.steps_done:
            asyncio.run(explore.run(state, run_dir, call=sdk.agent_call_structured, ledger=ledger,
                                    model=sdk.model_for("explore")))
        answered, units = explore.finish(state, run_dir, now(), git_target)
        print(f"SPEC_EXTRACT:EXPLORED {answered}/{units}", flush=True)
        code = _group_write_render(state, run_dir, git_target, ledger)
        return _apply(args, state, run_dir, git_target) if code == EXIT_DONE else code


def _apply(args: argparse.Namespace, state: state_mod.State, run_dir: Path, git_target: target.Target) -> int:
    """`--apply`, after render (item 21); exit 0 without it."""
    if not args.apply:
        return EXIT_DONE
    registry = apply_mod.registry_dir(git_target, args.specs_dir, run_dir)
    applied, skipped = apply_mod.run(state, run_dir, registry)
    print(f"SPEC_EXTRACT:APPLIED {applied}", flush=True)
    for line in skipped:
        print(line, flush=True)
    return EXIT_DONE


def _ask_with_write_model(step: Coroutine[Any, Any, None]) -> None:
    """Run the group or write step; a backend that cannot run names the setting it read."""
    try:
        asyncio.run(step)
    except sdk.BackendUnavailable as e:
        e.setting = "spec_extract_write_model"  # read by main's message
        raise


def _group_write_render(state: state_mod.State, run_dir: Path, git_target: target.Target, ledger) -> int:
    """Group, write, gate and render, each from state (plan, items 14-17)."""
    scope = state.scope
    assert scope is not None
    gated = explore.gated_facts(git_target, scope, state.explore_answers, run_dir)
    model = sdk.model_for("write")  # the group call uses the write model too (Decided 18)
    _ask_with_write_model(group.run(state, run_dir, gated, call=sdk.agent_call_structured, ledger=ledger,
                                    model=model))
    grouping = group.finish(state, run_dir)
    scope_sections = report_mod.scope_sections(scope, explore.not_answered_files(scope.units, state.explore_answers))
    if grouping is None:
        report_mod.write_report(run_dir, state.target, scope_sections + [group.NOT_GROUPED])
        print("SPEC_EXTRACT:NOT_GROUPED", flush=True)
        print(f"spec-extract: group gave no answer in 2 attempts; see {run_dir / report_mod.REPORT_FILE}",
              file=sys.stderr)
        return EXIT_FAILED
    _ask_with_write_model(write.run(state, run_dir, grouping.capabilities, gated,
                                    call=sdk.agent_call_structured, ledger=ledger, model=model))
    if "write" not in state.steps_done:
        state.steps_done.append("write")
        state_mod.save(state, run_dir)
    checked = gate.check(grouping.capabilities, state.write_answers, scope.symbols, git_target,
                         run_dir / target.SNAPSHOT_DIR)
    files = render_mod.render(state, run_dir, grouping, checked, scope_sections)
    print(f"SPEC_EXTRACT:RENDERED {files} {run_dir / render_mod.REGISTRY_DIR}", flush=True)
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
    except (scope_mod.ScopeRefused, apply_mod.ApplyRefused) as e:
        if e.line:
            print(e.line, flush=True)
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except explore.BudgetStopped as e:
        print(f"spec-extract: {e}. Everything paid for is saved; re-run with a higher --max-usd "
              "to continue.", file=sys.stderr)
        return EXIT_BUDGET
    except sdk.BackendUnavailable as e:
        setting = getattr(e, "setting", "spec_extract_explore_model")
        print(f"spec-extract: {e}. Nothing was counted; fix the model setting "
              f"({setting} in multiplai.conf) or install claude-agent-sdk, "
              "then run the same command again.", file=sys.stderr)
        return EXIT_FAILED
    except sdk.RepoTrustError as e:
        print(e, file=sys.stderr)
        return EXIT_UNTRUSTED
    except runlock.Locked as e:
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_LOCKED
    except (state_mod.Unreadable, target.ImportLost) as e:
        print(f"spec-extract: {e}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:  # noqa: BLE001 — every other failure is exit 1 with its message
        log.debug("spec-extract failed", exc_info=True)
        print(f"spec-extract: failed: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
