"""CLI for the review pipeline.

  review  — one target (--branch, --pr or --range), or code that is not a change
            (--tree [COMMIT] [--path DIR], or --dir DIR for a directory not under git;
            --trim also judges their existing tests and comments; --extra-finders is
            accepted and ignored);
            --plan-only prints the file groups and finder calls and stops
  batch   — a YAML list of targets, then rollups
  rollup  — regenerate HIGH/MEDIUM/LOW-only.md and runs.jsonl from existing findings.json files
  resume  — continue a review from its review-state.json
  post    — post the accepted findings as one PR review, one inline comment each
  assess-only — run the repeats and assess stages on saved reviews and compare
            their labels with the decisions already recorded (writes a report;
            changes no review)
  migrate — copy review directories written elsewhere (INBOX/reviews before
            multiplai-dev 0.46) into <out>/legacy/, with a ledger line per findings.json;
            the source is never changed

stdout contract: progress summary lines, a `summary: <path>` line per finished
target, then for review/batch/resume a `findings: <path>[ <path>...]` line
listing every findings.json written, and a final `checks: <path>[ <path>...]`
line listing the checks.json beside each. `review --plan-only` prints one line
per file group, a `groups: <n>  finder calls: <n>  files skipped: <n>` line and
a final `plan: <path>` line, and calls no model.

Exit codes: 0 done; 1 a target failed; 2 bad input, unresolvable target,
post refused, or another process is running a review of the target now
(runlock.py); 3 repository not trusted; 4 the budget circuit breaker stopped
the run (resume with a higher --max-cost-usd to continue), or `review` found
an interrupted run of the same target and head in the ledger whose saved
state is still in the target directory (resume it, or pass --restart); 5 the verifier gave no usable answer for some findings after
three tries each (resume retries only those); 6 the Claude usage limit was hit:
the checkpoint is saved, and the message names the reset time when the CLI
gave one and each stopped target's resume command (do not resume before the
reset).

Every run appends to `<data_dir>/reviews/ledger.jsonl` (see ledger.py),
whatever --out says.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .budget import DEFAULT_MAX_USD
from .models import MODES

log = logging.getLogger("review_pipeline")

SUBCOMMANDS = ("review", "batch", "rollup", "resume", "post", "assess-only", "migrate")


def default_out() -> Path:
    """`<data_dir>/reviews`: core's data directory (`$WORKSPACE/.multiplai/data`
    in a workspace), so every checkout, worktree and session writes one place.

    Never `./reviews`: the directory the user runs from is usually the
    repository under review, and the output would land in its working tree.
    review-viewer's `default_reviews_dir()` makes the same choice; they must
    agree, because the viewer finds a review by looking here."""
    from . import ledger

    return ledger.reviews_root()


def _trust_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trust-repo", action="store_true",
                   help="Confirm you trust the repository: its contents are sent to a model, which "
                        "reads the tree (equivalent to REVIEW_TRUST_REPO=1).")


def _budget_flag(p: argparse.ArgumentParser) -> None:
    # None when not given, so the run knows it is on the default (the cost NOTICE): `_budget_of`.
    p.add_argument("--max-cost-usd", type=float, default=None,
                   help=f"Circuit breaker per target, in USD (default {DEFAULT_MAX_USD:g}; 0 = no ceiling).")


def _budget_of(args: argparse.Namespace) -> dict:
    """`load_config`'s budget arguments: the default when --max-cost-usd was not given, 0 as no ceiling."""
    if args.max_cost_usd is None:
        return {"max_cost_usd": DEFAULT_MAX_USD, "max_cost_source": "default"}
    return {"max_cost_usd": args.max_cost_usd or None, "max_cost_source": "cli"}


def _deps_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-deps", action="store_true",
                   help="Do not clone the git dependencies uv.lock pins into the snapshot (review.yaml: deps)")


def _mode_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=MODES, default="full",
                   help="full (default): list every finding to act on, in Code, Tests and Docs sections. "
                        "critical: list only findings the verifier rates as breaking users or the business "
                        "after merge; the rest go to the review's appendix")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_pipeline",
        description="Code review with stages and gates enforced in code; writes findings.json (review-viewer v1).",
    )
    parser.add_argument("--session-id", default="", help="Claude Code session id, for log correlation")
    parser.add_argument("--out", default=None,
                        help="Output directory (default: <data_dir>/reviews, the multiplai data "
                             "directory: <workspace>/.multiplai/data/reviews in a workspace)")
    # The same two flags are accepted after the subcommand too
    # (`review --out DIR`). SUPPRESS keeps an absent one from overwriting the
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session-id", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--out", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + ",".join(SUBCOMMANDS) + "}")

    rv = sub.add_parser("review", parents=[common],
                        help="Review one branch, PR or commit range, or a whole tree or directory")
    rv.add_argument("--repo", help="Path to the repository (every target but --dir)")
    which = rv.add_mutually_exclusive_group(required=True)
    which.add_argument("--branch", help="Review origin/<branch> against its merge-base with the default branch")
    which.add_argument("--pr", type=int, help="Review a GitHub PR (needs gh)")
    which.add_argument("--range", dest="range_", metavar="BASE..HEAD", help="Review a commit range")
    which.add_argument("--tree", nargs="?", const="HEAD", metavar="COMMIT",
                       help="Review every file at COMMIT (default HEAD), not a change")
    which.add_argument("--dir", metavar="PATH",
                       help="Review a directory that is not under git: it is copied into the output "
                            "directory and committed there, never written to")
    rv.add_argument("--path", help="With --tree: review only this directory of the repository")
    rv.add_argument("--trim", action="store_true",
                    help="With --tree or --dir: also run the trim finder over the existing tests and comments "
                         "(a change review runs it unless review.yaml sets trim: false)")
    rv.add_argument("--extra-finders", nargs="?", const="", default=None, metavar="N",
                    help="Accepted and ignored: one finder per piece of the diff now runs on every change "
                         "review (prints a notice)")
    rv.add_argument("--paths", action="store_true",
                    help="With --tree or --dir: also map the code paths from each entry point before the finders "
                         "and write paths.json (a change review always does)")
    rv.add_argument("--plan-only", action="store_true",
                    help="Print the file groups and how many finder calls the review would make, "
                         "write plan.txt, and stop before any model call")
    rv.add_argument("--base-branch", help="Default branch for --branch (default: origin/HEAD)")
    rv.add_argument("--fetch", action="store_true", help="git fetch origin first (never done otherwise)")
    rv.add_argument("--ticket", action="append", default=[], help="Ticket id for the header (repeatable)")
    rv.add_argument("--deployed-in", help="Branch to report whether head is deployed in (origin/<name>)")
    rv.add_argument("--restart", action="store_true",
                    help="Start over even when the ledger shows an interrupted run of this target and head "
                         "(without it, such a run exits 4 and names the resume command)")
    _trust_flag(rv)
    _budget_flag(rv)
    _mode_flag(rv)
    _deps_flag(rv)

    bt = sub.add_parser("batch", parents=[common], help="Review a YAML list of targets, then write rollups")
    bt.add_argument("file", help="YAML list of {repo, branch|pr|range, tickets, deployed_in}")
    bt.add_argument("--parallel", type=int, default=2, help="Targets reviewed at once (default 2)")
    _trust_flag(bt)
    _budget_flag(bt)
    _mode_flag(bt)

    ru = sub.add_parser("rollup", parents=[common], help="Regenerate HIGH/MEDIUM/LOW-only.md and runs.jsonl from findings.json files")
    ru.add_argument("paths", nargs="*", help="findings.json files in order (default: every review's latest one under <out>)")

    rs = sub.add_parser("resume", parents=[common], help="Continue a review from <out>/<repo>/<object>/review-state.json")
    rs.add_argument("target_dir", help="The review's directory, <out>/<repo>/<object>")
    _trust_flag(rs)
    _budget_flag(rs)
    _deps_flag(rs)

    po = sub.add_parser("post", parents=[common],
                        help="Post the accepted findings as one PR review, one inline comment each")
    po.add_argument("target_dir", help="The review's directory, <out>/<repo>/<object>")
    po.add_argument("--decisions", help="The viewer's decisions.json; only accepted findings are posted")

    ao = sub.add_parser("assess-only", parents=[common],
                        help="Run repeats and assess on saved reviews; report labels beside recorded decisions")
    ao.add_argument("target_dirs", nargs="+", help="Review directories, <out>/<repo>/<object>; those without "
                                                   "viewer/decisions.json are skipped")
    ao.add_argument("--report", required=True, help="Markdown report to write")
    _trust_flag(ao)
    _budget_flag(ao)

    mg = sub.add_parser("migrate", parents=[common],
                        help="Copy review directories from another place into <out>/legacy/, never changing the source")
    mg.add_argument("--from", dest="source", required=True,
                    help="The directory holding review directories (e.g. <workspace>/INBOX/reviews)")
    return parser


TRUST_MESSAGE = (
    "Not run: the review skill sends this repository's contents to a model and lets it read the "
    "tree. If you trust the repository, re-run with --trust-repo (or set REVIEW_TRUST_REPO=1)."
)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from multiplai_core.env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    setup_logging("review-pipeline", session_id=args.session_id,
                  packages=("review_pipeline", "code_facts"))
    log.info("Session settings: model=%s effort=%s",
             os.environ.get("MULTIPLAI_SESSION_MODEL") or "not found",
             os.environ.get("MULTIPLAI_SESSION_EFFORT") or "not found")

    if getattr(args, "trust_repo", False):
        os.environ["REVIEW_TRUST_REPO"] = "1"

    from . import orchestrator, render, rounds, sdk, target
    from .config import load_config as read_config

    def load_config(root: Path, **kwargs):
        config = read_config(root, **kwargs)
        if getattr(args, "no_deps", False):
            config.deps = False
        return config
    from .post import PostError, post
    from .stages.verify import VerifyIncomplete

    out = Path(args.out).expanduser().resolve() if args.out else default_out().resolve()

    if args.command == "rollup":
        paths = [Path(p).resolve() for p in args.paths] or None
        written = render.write_rollups(out, paths)
        print("rollups: " + " ".join(str(p) for p in written))
        runs, n, skipped = render.write_runs(out, paths)
        print(f"runs: {runs} ({n} review{'' if n == 1 else 's'}, {skipped} skipped: no run recorded)")
        return 0

    if args.command == "migrate":
        from .migrate import MigrateError, migrate
        try:
            result = migrate(Path(args.source), out, session_id=args.session_id)
        except MigrateError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        finished = f", finished {len(result.finished)} an earlier run stopped part way" if result.finished else ""
        print(f"migrate: copied {len(result.copied)}{finished}, skipped {len(result.skipped)} already copied, "
              f"{result.lines} ledger lines; into {result.dest}")
        if result.config:
            print(f"migrate: copied {Path(args.source) / 'review.yaml'} to {result.config}")
        return 0

    if args.command == "post":
        try:
            print(post(Path(args.target_dir), Path(args.decisions) if args.decisions else None,
                       session_id=args.session_id))
        except PostError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        return 0

    if args.command == "review":
        if bool(args.dir) == bool(args.repo):
            print("ERROR: pass --repo <path> with --branch, --pr, --range or --tree, or --dir <path> alone",
                  file=sys.stderr)
            return 2
        if args.path and args.tree is None:
            print("ERROR: --path goes with --tree", file=sys.stderr)
            return 2
        if args.trim and args.tree is None and not args.dir:
            print("ERROR: --trim goes with --tree or --dir; a change review runs the trim finder unless "
                  "review.yaml sets trim: false", file=sys.stderr)
            return 2
        if args.paths and args.tree is None and not args.dir:
            print("ERROR: --paths goes with --tree or --dir; a change review always maps its paths",
                  file=sys.stderr)
            return 2
        if args.dir:
            try:
                target.check_out_dir(args.dir, out)  # before anything creates --out
            except target.TargetError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
        spec = orchestrator.TargetSpec(repo=args.repo or "", branch=args.branch, pr=args.pr, range=args.range_,
                                       tickets=args.ticket, deployed_in=args.deployed_in,
                                       base_branch=args.base_branch, fetch=args.fetch,
                                       tree=args.tree, path=args.path, dir=args.dir, mode=args.mode,
                                       restart=args.restart, trim=args.trim,
                                       extra_finders=args.extra_finders is not None, paths=args.paths)
        if args.plan_only:
            # Reads git only: nothing reaches a model, so no trust is needed and no ledger is kept.
            try:
                out.mkdir(parents=True, exist_ok=True)
                config = load_config(out, **_budget_of(args))
                text, plan_path = orchestrator.plan_only(spec, out, config)
            except orchestrator.ReviewError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            print(text, end="")
            print(f"plan: {plan_path}")
            return 0

    if not sdk.repo_is_trusted():
        print(TRUST_MESSAGE, file=sys.stderr)
        return 3

    max_cost = _budget_of(args)
    try:
        if args.command == "review":
            out.mkdir(parents=True, exist_ok=True)
            print(f"out: {out}", flush=True)
            path = asyncio.run(orchestrator.review(spec, out, load_config(out, **max_cost),
                                                   session_id=args.session_id))
            render.write_rollups(out)
            render.write_runs(out)
            print(f"findings: {path}")
            print(f"checks: {path.parent / 'checks.json'}")
            return 0

        if args.command == "resume":
            target_dir = Path(args.target_dir).expanduser().resolve()
            root = rounds.out_root(target_dir)
            path = asyncio.run(orchestrator.resume(target_dir, load_config(root, **max_cost),
                                                   session_id=args.session_id))
            render.write_rollups(root)
            render.write_runs(root)
            print(f"findings: {path}")
            print(f"checks: {path.parent / 'checks.json'}")
            return 0

        if args.command == "assess-only":
            results, skipped = [], []
            limit: sdk.UsageLimitError | None = None
            for raw in args.target_dirs:
                target_dir = Path(raw).expanduser().resolve()
                if limit is not None:
                    skipped.append(f"{target_dir.name}: not started: {limit}")
                    continue
                if not (target_dir / "viewer" / "decisions.json").is_file():
                    skipped.append(f"{target_dir.name}: no viewer/decisions.json")
                    continue
                try:
                    result = asyncio.run(orchestrator.assess_only(
                        target_dir, load_config(rounds.out_root(target_dir), **max_cost)))
                except sdk.RepoTrustError:
                    raise
                except sdk.UsageLimitError as e:
                    # Every later review would fail the same way: stop, and keep the report so far.
                    limit = e
                    skipped.append(f"{target_dir.name}: stopped: {e}")
                    continue
                except orchestrator.budget.BudgetExceededError as e:
                    # One review over budget skips that review only; the others' results are kept.
                    skipped.append(f"{target_dir.name}: stopped at the cost limit ({e})")
                    continue
                except orchestrator.ReviewError as e:
                    skipped.append(f"{target_dir.name}: {e}")
                    continue
                except Exception as e:  # noqa: BLE001 — one failed review must not lose the report
                    log.error("assess-only failed for %s", target_dir, exc_info=True)
                    first = (str(e).splitlines() or [""])[0][:200]
                    skipped.append(f"{target_dir.name}: failed ({type(e).__name__}: {first})")
                    continue
                results.append(result)
                print(f"{target_dir.name}: {len(result.rows)} findings labelled (${result.cost_usd:.2f})", flush=True)
            report = Path(args.report).expanduser().resolve()
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(orchestrator.assess_report(results, skipped), encoding="utf-8")
            print(f"report: {report}")
            if limit is not None:
                print(f"STOPPED: {limit}\nAfter the limit resets, run assess-only again for the reviews the "
                      "report lists as stopped or not started.", file=sys.stderr)
                return 6
            return 0

        if args.command == "batch":
            specs = orchestrator.load_batch(Path(args.file), args.mode)
            out.mkdir(parents=True, exist_ok=True)
            print(f"out: {out}", flush=True)
            stopped: sdk.UsageLimitError | None = None
            try:
                written, failures = asyncio.run(orchestrator.batch(
                    specs, out, load_config(out, **max_cost), parallel=args.parallel,
                    session_id=args.session_id))
            except sdk.UsageLimitError as e:
                stopped, written, failures = e, e.written, e.failures
            print("findings: " + " ".join(str(p) for p in written))
            print("checks: " + " ".join(str(p.parent / "checks.json") for p in written))
            if stopped is not None:
                raise stopped
            return 1 if failures else 0
    except orchestrator.InterruptedRun as e:
        print(f"STOPPED: {e}\nTo start over instead, pass --restart.", file=sys.stderr)
        return 4
    except orchestrator.ReviewError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except sdk.RepoTrustError:
        print(TRUST_MESSAGE, file=sys.stderr)
        return 3
    except orchestrator.budget.BudgetExceededError as e:
        kept = f"\n{e.kept}" if getattr(e, "kept", "") else ""
        print(f"STOPPED: {e}\n{e.diagnosis}\nResume with: python -m review_pipeline resume <out>/<repo>/<object> "
              f"--max-cost-usd <higher>{kept}", file=sys.stderr)
        return 4
    except VerifyIncomplete as e:
        print(f"STOPPED: {e}\nThe other verdicts are kept. Retry only these findings with: "
              f"python -m review_pipeline resume <out>/<repo>/<object> --trust-repo", file=sys.stderr)
        return 5
    except sdk.UsageLimitError as e:
        dirs = getattr(e, "stopped_dirs", None) or [getattr(e, "target_dir", None) or "<out>/<repo>/<object>"]
        flags = (f" --max-cost-usd {args.max_cost_usd:g}" if getattr(args, "max_cost_usd", None) is not None else "") \
            + (" --no-deps" if getattr(args, "no_deps", False) else "")
        commands = "\n".join(f"  python -m review_pipeline resume {d} --trust-repo{flags}" for d in dirs)
        not_started = " A target listed as not started has no checkpoint: review it after the reset." \
            if args.command == "batch" else ""
        print(f"STOPPED: {e}\nEvery answer that came back before the stop is kept. Do not retry before the "
              f"limit resets; then continue with:\n{commands}\nOnly resume continues a stopped review: a plain "
              f"review of the same target starts over.{not_started}", file=sys.stderr)
        return 6
    except sdk.AgentCallError as e:
        log.error("review failed: %s", e, exc_info=True)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    from multiplai_core import pin_session_settings

    # First, before any step or child process starts: every model call of
    # this run sees the session's model and effort as they were at launch.
    pin_session_settings()
    sys.exit(main())
