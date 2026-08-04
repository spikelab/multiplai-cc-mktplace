#!/usr/bin/env python3
"""Render a deepen candidates JSON file as a self-contained HTML report.

Usage:
    render_report.py --in <candidates.json> [--repo NAME] [--out PATH] [--open]

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
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

STRENGTH_CLASSES = {
    "Strong": "bg-emerald-100 text-emerald-800",
    "Worth exploring": "bg-amber-100 text-amber-800",
    "Speculative": "bg-slate-200 text-slate-700",
}


def strength_class(strength: str) -> str:
    return STRENGTH_CLASSES.get(strength, "bg-slate-200 text-slate-700")


def render(payload: dict, repo: str | None = None) -> str:
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
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidates=candidates,
        top_recommendation=payload.get("top_recommendation"),
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
        print(f"[deepen] unknown platform {platform}; open manually: {path}", file=sys.stderr)
        return

    try:
        subprocess.run(cmd, check=False)
    except OSError as e:
        print(f"[deepen] failed to launch viewer ({e}); open manually: {path}", file=sys.stderr)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Candidates JSON file.")
    p.add_argument("--repo", help="Repo name (overrides payload.repo).")
    p.add_argument("--out", help="Output HTML path (defaults to a fresh file in $TMPDIR).")
    p.add_argument("--open", action="store_true", help="Open the report in the default browser.")
    args = p.parse_args(argv)

    payload = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    html = render(payload, repo=args.repo)

    out = Path(args.out) if args.out else default_out_path()
    out.write_text(html, encoding="utf-8")
    print(out)

    if args.open:
        open_in_browser(out)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
