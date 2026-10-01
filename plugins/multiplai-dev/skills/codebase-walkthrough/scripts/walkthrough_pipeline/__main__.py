"""CLI for the walkthrough pipeline.

  run     — explain one module end to end
  resume  — continue a run from its walkthrough-state.json
  check   — re-check every `path:line` and snippet in a finished walkthrough

stdout contract: one summary line per stage, then `walkthrough: <path>` for
each file written, or `check: N checked, M failed`.

Exit codes: 0 done; 1 error (bad input, untrusted repo, failed check, failed
run); 2 the budget circuit breaker stopped the run with a checkpoint (resume
with a higher --max-usd to continue).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .budget import DEFAULT_MAX_USD

log = logging.getLogger("walkthrough_pipeline")

SUBCOMMANDS = ("run", "resume", "check")

TRUST_MESSAGE = (
    "Not run: the walkthrough skill sends the module's repository, and every repository that "
    "refers to it, to a model and lets it read them. If you trust them, re-run with "
    "--trust-repo (or set WALKTHROUGH_TRUST_REPO=1)."
)


def workspace_root() -> Path | None:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return None
    try:
        return Path((Path(config_dir) / ".workspace").read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        return None


def default_runs_dir() -> Path:
    """`<workspace>/INBOX/walkthrough-runs` when the workspace has an INBOX, else
    `~/.multiplai/walkthrough-runs`. Never inside the repository being read."""
    ws = workspace_root()
    if ws and (ws / "INBOX").is_dir():
        return ws / "INBOX" / "walkthrough-runs"
    return Path.home() / ".multiplai" / "walkthrough-runs"


def default_output() -> Path:
    """`INBOX/` when it exists (workspace or current directory), else the current directory."""
    ws = workspace_root()
    if ws and (ws / "INBOX").is_dir():
        return ws / "INBOX"
    if (Path.cwd() / "INBOX").is_dir():
        return Path.cwd() / "INBOX"
    return Path.cwd()


def _run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trust-repo", action="store_true",
                   help="Confirm you trust the repositories read: their contents are sent to a model "
                        "(equivalent to WALKTHROUGH_TRUST_REPO=1).")
    p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD,
                   help=f"Circuit breaker in USD (default {DEFAULT_MAX_USD:g}; 0 = no ceiling).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walkthrough_pipeline",
        description="Explain a module end to end; every claim's citation is re-read at the commit.",
    )
    parser.add_argument("--session-id", default="", help="Claude Code session id, for log correlation")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + ",".join(SUBCOMMANDS) + "}")

    rn = sub.add_parser("run", help="Explain one module")
    rn.add_argument("target", help="Path to the module (a directory inside a git repository)")
    rn.add_argument("--depth", choices=("overview", "standard", "deep"), default="standard")
    rn.add_argument("--mode", choices=("human", "agent"), default="human",
                    help="human: Markdown + HTML walkthrough; agent: compact Markdown for coding agents")
    rn.add_argument("--name", help="Base file name (default: the module directory's name, lower-cased)")
    rn.add_argument("--output", help="Directory for the walkthrough files (default: INBOX/ if it exists, else .)")
    rn.add_argument("--runs-dir", help="Directory for run state, snapshots and boundary.json "
                                       "(default: <workspace>/INBOX/walkthrough-runs)")
    rn.add_argument("--search-root", help="Directory whose repositories are searched for references "
                                          "(default: the parent of the outermost directory holding several repos)")
    rn.add_argument("--repos", nargs="+", help="Search exactly these repositories instead")
    rn.add_argument("--docs", help="Vendor docs base URL; its /llms.txt lists the pages that may be fetched")
    rn.add_argument("--docs-dir", action="append", default=[], help="Local vendor docs directory (repeatable)")
    rn.add_argument("--docs-pages", type=int, default=30, help="Most doc pages fetched (default 30)")
    rn.add_argument("--api-collection", help="A Bruno/OpenCollection directory of API requests")
    rn.add_argument("--unit-lines", type=int, default=6000, help="Most lines per explore unit (default 6000)")
    rn.add_argument("--scenarios", type=int, default=6, help="Most end-to-end scenarios traced (default 6)")
    rn.add_argument("--lsp-timeout", type=float, default=300.0,
                    help="Seconds pyright-langserver gets for references (default 300; 0 = skip LSP)")
    _run_flags(rn)

    rs = sub.add_parser("resume", help="Continue a run from <runs-dir>/<slug>/walkthrough-state.json")
    rs.add_argument("run_dir", help="The run's directory")
    _run_flags(rs)

    ck = sub.add_parser("check", help="Re-check every path:line and snippet in a finished walkthrough")
    ck.add_argument("walkthrough", help="The walkthrough Markdown file")
    ck.add_argument("--at-head", action="store_true",
                    help="Check against each repo's current HEAD instead of the commits in the header "
                         "(says whether the code has moved since)")
    ck.add_argument("--repo", action="append", default=[], metavar="KEY=PATH",
                    help="Where a repo in the header lives now, when it has moved")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from multiplai_core.env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    setup_logging("walkthrough-pipeline", session_id=args.session_id,
                  propagate_loggers=("walkthrough_pipeline", "multiplai_core"))

    if args.command == "check":
        from .check import check_file
        overrides = dict(x.split("=", 1) for x in args.repo if "=" in x)
        checked, failures = check_file(Path(args.walkthrough), at_head=args.at_head, repo_paths=overrides)
        for f in failures:
            print(f"FAIL {f}")
        print(f"check: {checked} checked, {len(failures)} failed")
        return 1 if failures or checked == 0 else 0

    if getattr(args, "trust_repo", False):
        os.environ["WALKTHROUGH_TRUST_REPO"] = "1"

    from . import budget, orchestrator, sdk
    from .config import load_config

    if not sdk.repo_is_trusted():
        print(TRUST_MESSAGE, file=sys.stderr)
        return 1

    max_cost = args.max_usd or None
    try:
        if args.command == "run":
            runs = Path(args.runs_dir).expanduser().resolve() if args.runs_dir else default_runs_dir()
            output = Path(args.output).expanduser().resolve() if args.output else default_output()
            opts = orchestrator.RunOptions(
                target=args.target, depth=args.depth, mode=args.mode, name=args.name,
                output=str(output), search_root=args.search_root, repos=args.repos or [],
                docs=args.docs, docs_dirs=args.docs_dir, docs_pages=args.docs_pages,
                api_collection=args.api_collection, unit_lines=args.unit_lines,
                scenarios=args.scenarios, lsp_timeout=args.lsp_timeout,
            )
            state, run_dir = orchestrator.prepare(opts, runs)
            print(f"run: {run_dir}", flush=True)
            paths = asyncio.run(orchestrator.run_state(state, run_dir, load_config(run_dir, max_cost_usd=max_cost),
                                                       session_id=args.session_id))
        else:
            run_dir = Path(args.run_dir).expanduser().resolve()
            paths = asyncio.run(orchestrator.resume(run_dir, load_config(run_dir, max_cost_usd=max_cost),
                                                    session_id=args.session_id))
        for p in paths:
            print(f"walkthrough: {p}")
        return 0
    except orchestrator.WalkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except sdk.RepoTrustError:
        print(TRUST_MESSAGE, file=sys.stderr)
        return 1
    except budget.BudgetExceededError as e:
        print(f"STOPPED: {e}\n{e.diagnosis}\nResume with: python -m walkthrough_pipeline resume <run dir> "
              f"--trust-repo --max-usd <higher>", file=sys.stderr)
        return 2
    except sdk.AgentCallError as e:
        log.error("walkthrough failed: %s", e, exc_info=True)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
