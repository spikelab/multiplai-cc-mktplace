"""CLI for the walkthrough pipeline.

  run     — explain one module end to end, or a change (`--scope pr`: a PR or a commit range)
  resume  — continue a run from its walkthrough-state.json (`--rewrite`: write it again after trace)
  check   — re-check every `path:line` and snippet in a finished walkthrough
  render  — rewrite a finished run's walkthrough files from its state; no model call

stdout contract: one summary line per stage, then `walkthrough: <path>` for
each file written, or `check: N checked, M failed` (and, when the file has path
hops, `check: N hops checked` on stderr). `run` and `resume` print
`SPEND:<usd>:<tokens>` last, on every exit but a kill: what this process's
model calls cost. `render` prints the same `walkthrough:` lines, then
`SPEND:0.000000:0`.

Exit codes: 0 done; 1 error (bad input, untrusted repo, failed check, failed
run); 2 the budget circuit breaker stopped the run with a checkpoint (resume
with a higher --max-usd to continue).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from .budget import DEFAULT_MAX_USD

log = logging.getLogger("walkthrough_pipeline")

SUBCOMMANDS = ("run", "resume", "check", "render")

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
        return Path(
            (Path(config_dir) / ".workspace").read_text(encoding="utf-8").strip()
        ).expanduser()
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
    p.add_argument(
        "--trust-repo",
        action="store_true",
        help="Confirm you trust the repositories read: their contents are sent to a model "
        "(equivalent to WALKTHROUGH_TRUST_REPO=1).",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=DEFAULT_MAX_USD,
        help=f"Circuit breaker in USD (default {DEFAULT_MAX_USD:g}; 0 = no ceiling).",
    )


def _repo_key(text: str) -> str:
    """A key is a snapshot directory's name and the first segment of every citation."""
    if text in (".", "..") or not re.fullmatch(r"[^/\\|`\s]+", text):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a directory-like name (no '/', '|', '`' or spaces)"
        )
    return text


def _positive_int(value: str) -> int:
    """A whole number of at least 1 (`--scenarios`: the path cap)."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walkthrough_pipeline",
        description="Explain a module end to end; every claim's citation is re-read at the commit.",
    )
    parser.add_argument(
        "--session-id", default="", help="Claude Code session id, for log correlation"
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{" + ",".join(SUBCOMMANDS) + "}"
    )

    rn = sub.add_parser("run", help="Explain one module, or a PR or commit range")
    rn.add_argument(
        "target",
        nargs="?",
        help="Path to the module (a directory inside a git repository). With --scope pr: the "
        "repository (default: --repo, else the current directory)",
    )
    rn.add_argument(
        "--scope",
        choices=("module", "pr"),
        default="module",
        help="module: explain the target directory; pr: explain a change, given by --pr or --range",
    )
    rn.add_argument(
        "--pr",
        type=_positive_int,
        metavar="N",
        help="With --scope pr: the PR to explain (needs the GitHub CLI `gh`)",
    )
    rn.add_argument(
        "--range",
        dest="range_",
        metavar="BASE..HEAD",
        help="With --scope pr: the commits to explain, read against the merge-base of BASE and HEAD",
    )
    rn.add_argument(
        "--repo",
        help="With --scope pr: the repository (default: the current directory)",
    )
    rn.add_argument(
        "--fetch",
        action="store_true",
        help="With --scope pr: run `git fetch --quiet origin` first (the only write to the repository)",
    )
    rn.add_argument(
        "--expect-head",
        metavar="SHA",
        help="With --scope pr: refuse to start unless the change's head is this commit",
    )
    rn.add_argument(
        "--commit",
        metavar="SHA",
        help="Module scope: explain the directory at this commit instead of HEAD (no checkout)",
    )
    rn.add_argument(
        "--depth", choices=("overview", "standard", "deep"), default="standard"
    )
    rn.add_argument(
        "--mode",
        choices=("human", "agent"),
        default="human",
        help="human: Markdown walkthrough, HTML page and review-viewer walkthrough.json (a module only); "
        "agent: compact Markdown for coding agents (a module only)",
    )
    rn.add_argument(
        "--name",
        help="Base file name (default: the module directory's name, lower-cased)",
    )
    rn.add_argument(
        "--output",
        help="Directory for the walkthrough files (default: INBOX/ if it exists, else .)",
    )
    rn.add_argument(
        "--runs-dir",
        help="Directory for run state, snapshots and boundary.json "
        "(default: <workspace>/INBOX/walkthrough-runs)",
    )
    rn.add_argument(
        "--search-root",
        help="Directory whose repositories are searched for references "
        "(default: the parent of the outermost directory holding several repos)",
    )
    rn.add_argument(
        "--repos", nargs="+", help="Search exactly these repositories instead"
    )
    rn.add_argument(
        "--repo-key",
        type=_repo_key,
        metavar="KEY",
        help="The module repository's name in the walkthrough (default: its directory's name)",
    )
    rn.add_argument(
        "--docs",
        help="Vendor docs base URL; its /llms.txt lists the pages that may be fetched",
    )
    rn.add_argument(
        "--docs-dir",
        action="append",
        default=[],
        help="Local vendor docs directory (repeatable)",
    )
    rn.add_argument(
        "--docs-pages", type=int, default=30, help="Most doc pages fetched (default 30)"
    )
    rn.add_argument(
        "--api-collection", help="A Bruno/OpenCollection directory of API requests"
    )
    rn.add_argument(
        "--unit-lines",
        type=int,
        default=6000,
        help="Most lines per explore unit (default 6000)",
    )
    rn.add_argument(
        "--scenarios",
        type=_positive_int,
        default=None,
        help="Cap on the number of paths traced (default: 21).",
    )
    rn.add_argument(
        "--lsp-timeout",
        type=float,
        default=300.0,
        help="Seconds pyright-langserver gets for references (default 300; 0 = skip LSP)",
    )
    _run_flags(rn)

    rs = sub.add_parser(
        "resume", help="Continue a run from <runs-dir>/<slug>/walkthrough-state.json"
    )
    rs.add_argument("run_dir", help="The run's directory")
    rs_docs = rs.add_mutually_exclusive_group()
    rs_docs.add_argument(
        "--docs", help="Replace the saved vendor docs URL (after a docs-gate stop)"
    )
    rs_docs.add_argument(
        "--no-docs",
        action="store_true",
        help="Go on without vendor docs (after a docs-gate stop)",
    )
    rs.add_argument(
        "--rewrite",
        action="store_true",
        help="Write the walkthrough again from a run past its trace stage: keeps the explore, docs and "
        "trace answers and runs only the write and render stages (their model calls are paid)",
    )
    _run_flags(rs)

    ck = sub.add_parser(
        "check", help="Re-check every path:line and snippet in a finished walkthrough"
    )
    ck.add_argument("walkthrough", help="The walkthrough Markdown file")
    ck.add_argument(
        "--at-head",
        action="store_true",
        help="Check against each repo's current HEAD instead of the commits in the header "
        "(says whether the code has moved since)",
    )
    ck.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="Where a repo in the header lives now, when it has moved",
    )

    rd = sub.add_parser(
        "render",
        help="Rewrite a finished run's walkthrough files from its state, with no model call",
    )
    rd.add_argument("run_dir", help="The run's directory")
    rd.add_argument(
        "--output",
        help="Directory for the walkthrough files (default: the run's saved --output, "
        "else the run directory)",
    )
    rd.add_argument(
        "--trust-repo",
        action="store_true",
        help="Accepted and ignored: render runs no agent",
    )
    return parser


def _check_scope(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The flags that go with each scope; argparse's own error (exit 2) for the rest."""
    if args.command != "run":
        return
    if args.scope == "pr":
        if (args.pr is None) == (args.range_ is None):
            parser.error("--scope pr needs exactly one of --pr and --range")
        if args.commit:
            parser.error(
                "--commit goes with a module; a change is read at its own head"
            )
        if args.mode == "agent":
            parser.error("--scope pr writes the human-mode walkthrough only")
        if (
            args.repo
            and args.target
            and Path(args.repo).resolve() != Path(args.target).resolve()
        ):
            parser.error("give the repository once: as the target or as --repo")
        return
    if not args.target:
        parser.error("the module directory is required")
    for flag, value in (
        ("--pr", args.pr),
        ("--range", args.range_),
        ("--repo", args.repo),
        ("--fetch", args.fetch),
        ("--expect-head", args.expect_head),
    ):
        if value:
            parser.error(f"{flag} goes with --scope pr")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_scope(parser, args)
    if args.command == "check":
        return _main(args)
    try:
        return _main(args)
    finally:
        from .budget import spend_marker

        print(spend_marker(), flush=True)


def _main(args: argparse.Namespace) -> int:
    from multiplai_core.env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    setup_logging(
        "walkthrough-pipeline",
        session_id=args.session_id,
        packages=("walkthrough_pipeline", "code_facts"),
    )
    log.info(
        "Session settings: model=%s effort=%s",
        os.environ.get("MULTIPLAI_SESSION_MODEL") or "not found",
        os.environ.get("MULTIPLAI_SESSION_EFFORT") or "not found",
    )

    if args.command == "check":
        from .check import check_file, hop_count

        overrides = dict(x.split("=", 1) for x in args.repo if "=" in x)
        checked, failures = check_file(
            Path(args.walkthrough), at_head=args.at_head, repo_paths=overrides
        )
        for f in failures:
            print(f"FAIL {f}")
        print(f"check: {checked} checked, {len(failures)} failed")
        hops = hop_count(
            Path(args.walkthrough).expanduser().read_text(encoding="utf-8")
        )
        if hops:
            # On stderr, so the stdout line above stays byte for byte what parsers expect.
            print(f"check: {hops} hops checked", file=sys.stderr)
        return 1 if failures or checked == 0 else 0

    if args.command == "render":
        return _render(args)

    if getattr(args, "trust_repo", False):
        os.environ["WALKTHROUGH_TRUST_REPO"] = "1"

    from . import budget, orchestrator, sdk, target
    from .config import load_config

    if not sdk.repo_is_trusted():
        print(TRUST_MESSAGE, file=sys.stderr)
        return 1

    max_cost = args.max_usd or None
    try:
        if args.command == "run":
            runs = (
                Path(args.runs_dir).expanduser().resolve()
                if args.runs_dir
                else default_runs_dir()
            )
            output = (
                Path(args.output).expanduser().resolve()
                if args.output
                else default_output()
            )
            opts = orchestrator.RunOptions(
                target=(
                    args.target or args.repo or "."
                    if args.scope == "pr"
                    else args.target
                ),
                scope=args.scope,
                commit=args.commit,
                pr=args.pr,
                range=args.range_,
                fetch=args.fetch,
                expect_head=args.expect_head,
                depth=args.depth,
                mode=args.mode,
                name=args.name,
                output=str(output),
                search_root=args.search_root,
                repos=args.repos or [],
                repo_key=args.repo_key,
                docs=args.docs,
                docs_dirs=args.docs_dir,
                docs_pages=args.docs_pages,
                api_collection=args.api_collection,
                unit_lines=args.unit_lines,
                lsp_timeout=args.lsp_timeout,
                **(
                    {}
                    if args.scenarios is None
                    else {"scenarios": args.scenarios, "scenarios_given": True}
                ),
            )
            state, run_dir = orchestrator.prepare(opts, runs)
            print(f"run: {run_dir}", flush=True)
            paths = asyncio.run(
                orchestrator.run_state(
                    state,
                    run_dir,
                    load_config(run_dir, max_cost_usd=max_cost),
                    session_id=args.session_id,
                )
            )
        else:
            run_dir = Path(args.run_dir).expanduser().resolve()
            docs = (
                None
                if args.no_docs
                else (args.docs if args.docs else orchestrator.NO_CHANGE)
            )
            paths = asyncio.run(
                orchestrator.resume(
                    run_dir,
                    load_config(run_dir, max_cost_usd=max_cost),
                    session_id=args.session_id,
                    docs=docs,
                    rewrite=args.rewrite,
                )
            )
        for p in paths:
            print(f"walkthrough: {p}")
        return 0
    except (orchestrator.WalkError, target.MissingToolError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except sdk.RepoTrustError:
        print(TRUST_MESSAGE, file=sys.stderr)
        return 1
    except budget.BudgetExceededError as e:
        print(
            f"STOPPED: {e}\n{e.diagnosis}\nResume with: python -m walkthrough_pipeline resume <run dir> "
            f"--trust-repo --max-usd <higher>",
            file=sys.stderr,
        )
        return 2
    except sdk.AgentCallError as e:
        log.error("walkthrough failed: %s", e, exc_info=True)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _render(args: argparse.Namespace) -> int:
    """Write the outputs of a run past its write stage from walkthrough-state.json. No agent, no ledger;
    the state file and progress.log are left as they are."""
    from . import render, target
    from .state import STATE_FILE, load_state

    run_dir = Path(args.run_dir).expanduser().resolve()
    state = load_state(run_dir / STATE_FILE)
    if state is None or not state.past("write"):
        print(
            f"render: {run_dir} has not finished its write stage; continue it with resume {run_dir} --trust-repo",
            file=sys.stderr,
        )
        return 1
    unreadable = render.unreadable_commit(state)
    if unreadable is not None:
        print(f"render: cannot read {unreadable}", file=sys.stderr)
        return 1
    out_dir = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path(state.options.get("output") or run_dir)
    )
    try:
        paths = render.write_outputs(state, out_dir)
    except (target.TargetError, OSError) as e:
        print(f"render: {e}", file=sys.stderr)
        return 1
    for p in paths:
        print(f"walkthrough: {p}")
    return 0


if __name__ == "__main__":
    from multiplai_core import pin_session_settings

    # First, before any step or child process starts: every model call of
    # this run sees the session's model and effort as they were at launch.
    pin_session_settings()
    sys.exit(main())
