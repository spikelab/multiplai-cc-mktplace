"""`python -m explainer_check check|new|render <page.html>`.

check   prints one `FAIL: <reason>` line per failure, then `check: ok` (exit 0) or `check: N problem(s)`
        (exit 1).
new     writes a new page from the skill's shell (`assets/shell.html`), with the design system's
        `fonts.css` and `tokens.css` inlined; it never overwrites a file.
render  draws each SVG of the page to `<out>/<n>-light.png` and `<out>/<n>-dark.png` and prints each
        path; exits 1 when `resvg-py` has no wheel for this platform or an SVG cannot be drawn.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .check import MAX_WORDS, Options, check_page
from .page import parse_page
from .render import RenderUnavailable, render_svgs

SHELL = Path(__file__).resolve().parents[2] / "assets" / "shell.html"


def new_page(out: Path, design_system_dir: Path) -> str:
    """The shell with the design system in *design_system_dir* inlined, written to *out*. Returns an
    error message, or "" on success."""
    if out.exists():
        return f"new: {out} exists; pick another name or remove it"
    tokens = design_system_dir / "tokens.css"
    if not tokens.is_file():
        return f"new: {design_system_dir} has no tokens.css"
    fonts = design_system_dir / "fonts.css"
    text = SHELL.read_text(encoding="utf-8")
    text = text.replace("/*TOKENS*/", tokens.read_text(encoding="utf-8"))
    text = text.replace(
        "/*FONTS*/", fonts.read_text(encoding="utf-8") if fonts.is_file() else ""
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return ""


def _repo(value: str) -> tuple[str, Path]:
    key, sep, path = value.partition("=")
    if not sep or not key or not path:
        raise argparse.ArgumentTypeError(f"--repo takes key=path, got {value!r}")
    return key, Path(path).expanduser()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="explainer_check",
        description="Check an explainer page, or draw its SVGs to PNG. Makes no model call.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser(
        "check", help="check the page and print one FAIL line per failure"
    )
    c.add_argument("page", type=Path)
    c.add_argument(
        "--max-words",
        type=int,
        default=MAX_WORDS,
        help=f"words shown before any fold-out is opened (default {MAX_WORDS:,})",
    )
    c.add_argument(
        "--repo",
        type=_repo,
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="the git checkout for a citation's repo key (repeatable)",
    )
    n = sub.add_parser(
        "new", help="write a new page from the shell, with a design system inlined"
    )
    n.add_argument("page", type=Path)
    n.add_argument(
        "--design-system",
        type=Path,
        required=True,
        help="the design-system folder (`python -m design_system path` prints it)",
    )
    r = sub.add_parser("render", help="draw each SVG to PNG, light and dark")
    r.add_argument("page", type=Path)
    r.add_argument("--out", type=Path, required=True, help="folder for the PNGs")
    args = ap.parse_args(argv)

    if args.cmd == "new":
        err = new_page(args.page, args.design_system.expanduser())
        print(err or args.page, file=sys.stderr if err else sys.stdout)
        return 1 if err else 0

    try:
        source = args.page.read_text(encoding="utf-8")
    except OSError as e:
        print(f"{args.cmd}: cannot read {args.page}: {e}", file=sys.stderr)
        return 1
    page = parse_page(source)

    if args.cmd == "check":
        reasons = check_page(page, Options(args.max_words, dict(args.repo)))
        for reason in reasons:
            print(f"FAIL: {reason}")
        print(
            f"check: {len(reasons)} problem(s)" if reasons else "check: ok",
            f"({page.open_words:,} words open, {len(page.svgs)} SVG, {len(page.cites)} citation(s))",
        )
        return 1 if reasons else 0

    if not page.svgs:
        print("render: the page has no SVG")
        return 0
    try:
        paths = render_svgs(page, args.out)
    except RenderUnavailable as e:
        print(
            f"render: resvg-py cannot be imported on this platform ({e}); "
            "open the page in a browser to look at its pictures",
            file=sys.stderr,
        )
        return 1
    except ValueError as e:
        print(f"render: {e}", file=sys.stderr)
        return 1
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
