#!/usr/bin/env python3
"""Render a deepen candidates JSON file as a self-contained HTML report.

Usage:
    render_report.py --in <candidates.json> [--repo NAME] [--out PATH] [--open]
                     [--design-system-dir PATH]

The page is styled with a design-system folder's tokens.css (and fonts.css when it has one):
--design-system-dir, else the neutral theme the design-system skill ships beside this one.

If --out is omitted, writes to <tmpdir>/architecture-review-<ts>.html and prints the
path on stdout. With --open, also opens the report in the user's default browser.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

# scripts/ → deepen/ → skills/: the design-system skill's neutral theme sits beside this skill.
NEUTRAL = Path(__file__).resolve().parents[2] / "design-system" / "assets" / "neutral"

STRENGTH_CLASSES = {
    "Strong": "bg-[var(--ok-soft)] text-[var(--ok)]",
    "Worth exploring": "bg-[var(--warn-soft)] text-[var(--warn)]",
    "Speculative": "bg-[var(--sunk)] text-[var(--muted)]",
}


def strength_class(strength: str) -> str:
    return STRENGTH_CLASSES.get(strength, "bg-[var(--sunk)] text-[var(--muted)]")


def design_css(design_dir: Path | None = None) -> tuple[str, str]:
    """`(fonts.css, tokens.css)` of *design_dir* (default: neutral); fonts is empty when the folder has none.

    Raises ValueError when tokens.css is missing, or when either file contains `</style`, which would end
    the page's style element.
    """
    folder = Path(design_dir) if design_dir else NEUTRAL
    tokens = folder / "tokens.css"
    if not tokens.is_file():
        raise ValueError(f"design system {folder}: tokens.css is missing")
    fonts = folder / "fonts.css"
    out = (
        fonts.read_text(encoding="utf-8") if fonts.is_file() else "",
        tokens.read_text(encoding="utf-8"),
    )
    for name, text in zip(("fonts.css", "tokens.css"), out, strict=True):
        if "</style" in text.lower():
            raise ValueError(f"design system {folder}: {name} contains </style")
    return out


def render(
    payload: dict, repo: str | None = None, design_dir: Path | None = None
) -> str:
    fonts_css, tokens_css = design_css(design_dir)
    here = Path(__file__).resolve().parent
    env = Environment(
        loader=FileSystemLoader(here / "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["strength_class"] = strength_class
    template = env.get_template("report.html.j2")

    candidates = payload.get("candidates", [])
    for c in candidates:
        c.setdefault("wins", [])
        c.setdefault("files", [])
        c.setdefault("adr_callout", None)
        c.setdefault("diagram", None)

    return template.render(
        repo=repo or payload.get("repo") or "(unknown repo)",
        generated_at=payload.get("generated_at")
        or datetime.now(UTC).isoformat(timespec="seconds"),
        candidates=candidates,
        top_recommendation=payload.get("top_recommendation"),
        # Not escaped: CSS inside <style> is raw text, and design_css refused `</style`.
        fonts_css=Markup(fonts_css),
        tokens_css=Markup(tokens_css),
    )


def default_out_path() -> Path:
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return tmpdir / f"architecture-review-{stamp}.html"


def open_in_browser(path: Path) -> None:
    """Cross-platform 'open this file in the default app' helper."""
    platform = sys.platform

    if platform == "darwin":
        cmd = ["open", str(path)]
    elif platform.startswith("linux") or platform.startswith("freebsd"):
        if not shutil.which("xdg-open"):
            print(f"[deepen] no xdg-open found; open manually: {path}", file=sys.stderr)
            return
        cmd = ["xdg-open", str(path)]
    elif platform.startswith("win"):
        cmd = ["cmd", "/c", "start", "", str(path)]
    else:
        print(
            f"[deepen] unknown platform {platform}; open manually: {path}",
            file=sys.stderr,
        )
        return

    try:
        subprocess.run(cmd, check=False)
    except OSError as e:
        print(
            f"[deepen] failed to launch viewer ({e}); open manually: {path}",
            file=sys.stderr,
        )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Candidates JSON file.")
    p.add_argument("--repo", help="Repo name (overrides payload.repo).")
    p.add_argument(
        "--out", help="Output HTML path (defaults to a fresh file in $TMPDIR)."
    )
    p.add_argument(
        "--open", action="store_true", help="Open the report in the default browser."
    )
    p.add_argument(
        "--design-system-dir",
        help="Design-system folder to style the report with (default: the neutral theme).",
    )
    args = p.parse_args(argv)

    payload = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    try:
        html = render(
            payload,
            repo=args.repo,
            design_dir=Path(args.design_system_dir).expanduser()
            if args.design_system_dir
            else None,
        )
    except ValueError as e:
        print(f"[deepen] {e}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else default_out_path()
    out.write_text(html, encoding="utf-8")
    print(out)

    if args.open:
        open_in_browser(out)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
