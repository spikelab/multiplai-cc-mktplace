"""CLI: `open`, `edit`, `render`.

Exit codes: 0 done; 1 a gate rejected the body (nothing written to GitHub);
2 refused, or `gh`/`git`/the model call failed (the message says why).

Stdout lines a caller can parse: `PUSHED:<branch>`, `PR:<url>`,
`PR_EDITED:<url>`, `DIFF:<files> files, +<added> -<removed>`,
`PR_DIAGNOSIS:<reason>`. `render` prints only the body on stdout; its
`DIFF:` and `PR_DIAGNOSIS:` lines go to stderr.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_pipeline",
        description="Open or edit a GitHub PR whose body follows the repo's PR template.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", required=True, help="the git repository (or worktree) the PR is from")
        p.add_argument("--change-dir", required=True,
                       help="the change's documents: specs/changes/<name>/ or its archive")
        p.add_argument("--section", action="append", default=[], metavar="FILE",
                       help="an extra body section; the file's first line is its Markdown heading (repeatable)")

    op = sub.add_parser("open", help="push the branch and open a PR from the template")
    common(op)
    op.add_argument("--base", help="the base branch (default: the repo's default branch)")
    op.add_argument("--head", help="the branch to open the PR from (default: the current branch)")
    op.add_argument("--title", help="the PR title (default: buildme: <change-name>)")
    op.add_argument("--ready", action="store_true", help="open it ready for review instead of as a draft")
    op.add_argument("--no-push", action="store_true", help="do not push the branch first")
    op.add_argument("--ticket-url", help="the ticket's URL (default: a url: line in <change-dir>/ticket.md)")
    op.add_argument("--dry-run", action="store_true",
                    help="print the commands and the body; write nothing to git or GitHub")

    ed = sub.add_parser("edit", help="refresh the sections this skill owns in an open PR's body")
    ed.add_argument("pr", help="the PR number or URL")
    common(ed)
    ed.add_argument("--push", action="store_true", help="push the PR's branch before editing")
    ed.add_argument("--dry-run", action="store_true",
                    help="print the commands and the body; write nothing to git or GitHub")

    rd = sub.add_parser("render", help="print the body `open` would write; no gh call, no push")
    common(rd)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from multiplai_core.env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    setup_logging("pr-pipeline", propagate_loggers=("pr_pipeline", "multiplai_core"))

    from . import commands

    return commands.run(args)


if __name__ == "__main__":
    sys.exit(main())
