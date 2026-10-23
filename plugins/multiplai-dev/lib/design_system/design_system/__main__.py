"""CLI: `python -m design_system list | show [NAME] | check [NAME|PATH] | path [NAME] | create NAME [--dir PATH]`.

Exit 0 on success, 1 on any failure (a bad setting, an unknown name, a folder that fails `check`).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from . import settings, tokens

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _folder(arg: str | None) -> Path:
    """A path when *arg* looks like one (has a separator, or starts with `.` or `~`), else a design system's name."""
    if arg and ("/" in arg or "\\" in arg or arg.startswith((".", "~"))):
        return Path(arg).expanduser().resolve()
    return settings.resolve(arg)


def _list(_: argparse.Namespace) -> int:
    default = settings.default_name() or settings.NEUTRAL_NAME
    d = settings.systems_dir()
    print(f"folder: {d if d else '(not set; only neutral)'}")
    for name in settings.names():
        print(f"{'*' if name == default else ' '} {name}")
    return 0


def _show(args: argparse.Namespace) -> int:
    path = _folder(args.name)
    print(f"path: {path}")
    readme = path / "README.md"
    if readme.is_file():
        print(readme.read_text(encoding="utf-8").rstrip())
    blocks = tokens.parse((path / "tokens.css").read_text(encoding="utf-8"))
    for label, block in (
        (tokens.LIGHT, blocks.light),
        (tokens.DARK_MEDIA, blocks.dark_media),
        (tokens.DARK_ATTR, blocks.dark_attr),
    ):
        print(f"\n{label}:")
        for key, value in (block or {}).items():
            print(f"  {key}: {value}")
    return 0


def _check(args: argparse.Namespace) -> int:
    path = _folder(args.target)
    print(f"check: {path}")
    if (path / "tokens.css").is_file():
        for r in tokens.ratios(
            tokens.parse((path / "tokens.css").read_text(encoding="utf-8"))
        ):
            ratio = f"{r.ratio:.2f}:1" if r.ratio is not None else "unknown"
            print(f"  {r.scheme}: {r.text} on {r.background} {ratio}")
    problems = tokens.check_folder(path)
    for p in problems:
        print(f"FAIL {p}")
    print("check: ok" if not problems else f"check: {len(problems)} problem(s)")
    return 1 if problems else 0


def _path(args: argparse.Namespace) -> int:
    print(_folder(args.name))
    return 0


def _create(args: argparse.Namespace) -> int:
    if not NAME_RE.fullmatch(args.name) or args.name == settings.NEUTRAL_NAME:
        print(
            f"create: {args.name!r} is not a usable name (letters, digits, '.', '_', '-'; not 'neutral')",
            file=sys.stderr,
        )
        return 1
    if args.dir:
        base = Path(args.dir).expanduser()
        if not base.is_absolute():
            print(
                f"create: --dir must be an absolute path, got {args.dir!r}",
                file=sys.stderr,
            )
            return 1
    else:
        base = settings.systems_dir()
        if base is None:
            print(
                f"create: no folder to create it in; pass --dir PATH or set {settings.DIR_KEY}",
                file=sys.stderr,
            )
            return 1
    target = base / args.name
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            settings.NEUTRAL, target
        )  # refuses an existing target: nothing is overwritten
    except FileExistsError:
        print(
            f"create: {target} already exists; remove it or pick another name",
            file=sys.stderr,
        )
        return 1
    print(f"created: {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="design_system",
        description="Resolve, list, show and check design systems.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "list", help="The design systems that exist; * marks the default"
    ).set_defaults(fn=_list)
    sh = sub.add_parser("show", help="A design system's README and token values")
    sh.add_argument(
        "name",
        nargs="?",
        help="Its name (default: the configured default, else neutral)",
    )
    sh.set_defaults(fn=_show)
    ck = sub.add_parser(
        "check", help="Check a design system against the token contract and WCAG AA"
    )
    ck.add_argument(
        "target",
        nargs="?",
        help="A name or a folder path (default: the configured default)",
    )
    ck.set_defaults(fn=_check)
    pa = sub.add_parser("path", help="The folder of a design system")
    pa.add_argument(
        "name",
        nargs="?",
        help="Its name (default: the configured default, else neutral)",
    )
    pa.set_defaults(fn=_path)
    cr = sub.add_parser("create", help="Copy the neutral theme to a new design system")
    cr.add_argument("name", help="The new design system's name")
    cr.add_argument(
        "--dir", help="The folder of design systems (default: the configured one)"
    )
    cr.set_defaults(fn=_create)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except settings.SettingsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
