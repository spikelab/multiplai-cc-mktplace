"""Read an overlay style file's frontmatter (references/styles/README.md) with the standard library.

A style file is Markdown with YAML frontmatter. The frontmatter uses a small
part of YAML: block mappings nested by indentation, and values that are a
quoted or bare string, a number, true/false/null, a flow mapping
(`{display: 800, body: 500}`) or a flow list (`[1180, 1420]`), with `#`
comments after them. `frontmatter` reads exactly that and refuses anything
else, so a style file that has outgrown it fails loudly instead of being
half read.

`panel_values` flattens the keys the job page's style panel shows into dotted
names (`palette.accent`, `fonts.weights.display`, `motion.enter_s`): the names
a `style` mailbox row uses for its changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

STYLES_DIR = Path(__file__).resolve().parents[2] / "references" / "styles"

_FENCE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_KEY = re.compile(
    r"^(?P<indent> *)(?P<key>[A-Za-z_][\w-]*)\s*:(?:\s+(?P<value>.*?))?\s*$"
)
_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
# The keys the style panel shows and edits; palette keys are whatever the file names.
PANEL_KEYS = (
    "fonts.display",
    "fonts.body",
    "fonts.weights.display",
    "fonts.weights.body",
    "shape.radius_px",
    "motion.enter_s",
    "motion.exit_s",
    "motion.stagger_s",
)


class StyleFileError(ValueError):
    pass


def _strip_comment(raw: str) -> str:
    """`raw` without a trailing `# comment` that sits outside quotes."""
    quote = None
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i].rstrip()
    return raw.rstrip()


def _split_flow(inner: str) -> list[str]:
    """The comma-separated items of a flow mapping or list body, quotes respected."""
    items, cur, quote = [], "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur += ch
        elif ch in "[]{}":
            raise StyleFileError("nested flow collections are not supported")
        elif ch == ",":
            if not cur.strip():
                raise StyleFileError(f"empty item in {inner!r}")
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if quote:
        raise StyleFileError(f"unclosed quote in {inner!r}")
    if cur.strip():
        items.append(cur.strip())
    elif items:
        raise StyleFileError(f"empty item in {inner!r}")
    return items


def scalar(raw: str) -> Any:
    """One YAML scalar: a quoted string, a number, true/false/null or a bare string."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        body = raw[1:-1]
        if raw[0] in body:
            raise StyleFileError(f"stray quote in {raw!r}")
        return body
    if raw[:1] and raw[:1] in "'\"":
        raise StyleFileError(f"unclosed quote in {raw!r}")
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("null", "~", ""):
        return None
    if _NUMBER.match(raw):
        return float(raw) if any(c in raw for c in ".eE") else int(raw)
    return raw


def value(raw: str) -> Any:
    """A value: a scalar, a flow mapping or a flow list."""
    raw = _strip_comment(raw)
    if raw.startswith("{"):
        if not raw.endswith("}"):
            raise StyleFileError(f"unclosed flow mapping {raw!r}")
        out = {}
        for item in _split_flow(raw[1:-1]):
            k, sep, v = item.partition(":")
            if not sep or not re.match(r"^[A-Za-z_][\w-]*$", k.strip()):
                raise StyleFileError(f"bad flow mapping item {item!r}")
            out[k.strip()] = scalar(v)
        return out
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise StyleFileError(f"unclosed flow list {raw!r}")
        return [scalar(x) for x in _split_flow(raw[1:-1])]
    return scalar(raw)


def frontmatter(text: str) -> dict:
    """The frontmatter of a style file as nested dicts; StyleFileError if it has none or is malformed."""
    m = _FENCE.match(text.lstrip("﻿"))
    if not m:
        raise StyleFileError(
            "no frontmatter: the file must start with a --- line and close it with another"
        )
    root: dict = {}
    stack: list[tuple[int, dict]] = [(0, root)]  # (indent of the keys in it, mapping)
    opened: tuple[dict, str, int] | None = (
        None  # a key with no value: a deeper block may follow
    )
    for n, line in enumerate(m.group(1).splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise StyleFileError(f"line {n}: indent with spaces, not tabs")
        km = _KEY.match(line)
        if not km:
            raise StyleFileError(
                f"line {n}: expected `key: value`, got {line.strip()!r}"
            )
        indent, key = len(km["indent"]), km["key"]
        if opened is not None:
            parent, k, at = opened
            opened = None
            if indent > at:
                parent[k] = {}
                stack.append((indent, parent[k]))
        while stack[-1][0] > indent:
            stack.pop()
        if stack[-1][0] != indent:
            raise StyleFileError(
                f"line {n}: indentation does not match any mapping above it"
            )
        parent = stack[-1][1]
        if key in parent:
            raise StyleFileError(f"line {n}: duplicate key {key!r}")
        raw = km["value"]
        if raw is None or not _strip_comment(raw):
            parent[key] = None
            opened = (parent, key, indent)
            continue
        try:
            parent[key] = value(raw)
        except StyleFileError as e:
            raise StyleFileError(f"line {n}: {e}") from None
    return root


def _flatten(d: dict, prefix: str, out: dict) -> None:
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            _flatten(v, name + ".", out)
        else:
            out[name] = v


def panel_values(fm: dict) -> dict[str, Any]:
    """The values the style panel shows: every `palette.*` key, then PANEL_KEYS, flat and dotted.

    A key the file does not set is left out; the page shows only what is there.
    """
    flat: dict[str, Any] = {}
    _flatten(fm, "", flat)
    out = {
        k: v for k, v in flat.items() if k.startswith("palette.") and isinstance(v, str)
    }
    out.update(
        {
            k: flat[k]
            for k in PANEL_KEYS
            if k in flat and not isinstance(flat[k], (list, dict))
        }
    )
    return out


def load(path: str | Path) -> dict:
    """{status: "ok", file, name, values} for a style file, or {status: "invalid", file, error}."""
    p = Path(path)
    try:
        fm = frontmatter(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, StyleFileError) as e:
        return {"status": "invalid", "file": str(p), "error": str(e)}
    return {
        "status": "ok",
        "file": str(p),
        "name": fm.get("name"),
        "values": panel_values(fm),
    }


def shipped(name: str) -> Path | None:
    """The style shipped as references/styles/<name>.md, or None."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$", name) or name == "README":
        return None
    p = STYLES_DIR / f"{name}.md"
    return p if p.is_file() else None
