"""The token contract of a design-system folder, and the check that holds a folder to it.

A folder holds `tokens.css` and `README.md` (required) and `fonts.css`, `components.md` and `assets/`
(optional). `tokens.css` puts every token on bare `:root`, then redefines the colours twice for dark:
under `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }` and under
`:root[data-theme="dark"]`, each with `color-scheme: dark`. Pages that inline the file then follow the
reader's setting and an explicit `data-theme` alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Tokens whose value changes with the scheme: required in the light block and in both dark blocks.
COLOURS: tuple[str, ...] = (
    "--bg",
    "--surface",
    "--sunk",
    "--fg",
    "--muted",
    "--line",
    "--accent",
    "--accent-soft",
    "--ok",
    "--ok-soft",
    "--warn",
    "--warn-soft",
    "--bad",
    "--bad-soft",
    "--code-bg",
    "--code-fg",
)

# Type and shape: required in the light block only; the dark blocks inherit them.
SHAPE: tuple[str, ...] = (
    "--font-display",
    "--font-body",
    "--font-mono",
    "--step--1",
    "--step-0",
    "--step-1",
    "--step-2",
    "--step-3",
    "--step-4",
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-5",
    "--space-6",
    "--radius",
    "--measure",
)

REQUIRED: tuple[str, ...] = COLOURS + SHAPE

# (text, background): each must reach WCAG AA for body text in every scheme.
CONTRAST_PAIRS: tuple[tuple[str, str], ...] = (
    ("--fg", "--bg"),
    ("--fg", "--surface"),
    ("--fg", "--sunk"),
    ("--muted", "--bg"),
    ("--muted", "--surface"),
    ("--accent", "--bg"),
    ("--accent", "--surface"),
    ("--fg", "--accent-soft"),
    ("--code-fg", "--code-bg"),
    ("--ok", "--ok-soft"),
    ("--ok", "--surface"),
    ("--warn", "--warn-soft"),
    ("--warn", "--surface"),
    ("--bad", "--bad-soft"),
    ("--bad", "--surface"),
)

AA = 4.5

LIGHT = "light (:root)"
DARK_MEDIA = 'dark (@media … :root:not([data-theme="light"]))'
DARK_ATTR = 'dark (:root[data-theme="dark"])'

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STYLE_END = re.compile(r"</style", re.IGNORECASE)
_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)")


@dataclass
class Blocks:
    """The declarations of the three token blocks; a block missing from the file is None."""

    light: dict[str, str] | None = None
    dark_media: dict[str, str] | None = None
    dark_attr: dict[str, str] | None = None
    problems: list[str] = field(default_factory=list)


def _norm(selector: str) -> str:
    return re.sub(r"\s+", "", selector).replace("'", '"')


def _rules(css: str) -> list[tuple[str, str]]:
    """Top-level `(prelude, body)` pairs of *css*, matched by braces. Raises ValueError on unbalanced braces."""
    out: list[tuple[str, str]] = []
    depth, start, prelude_start = 0, 0, 0
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced braces")
            if depth == 0:
                out.append((css[prelude_start:start].strip(), css[start + 1 : i]))
                prelude_start = i + 1
        elif ch == ";" and depth == 0:
            prelude_start = i + 1  # an at-rule with no block, such as @import
    if depth != 0:
        raise ValueError("unbalanced braces")
    return out


def _declarations(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in body.split(";"):
        name, sep, value = decl.partition(":")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


def parse(css: str) -> Blocks:
    """The three token blocks of a `tokens.css` text. Several bare `:root` blocks are merged in order."""
    blocks = Blocks()
    try:
        rules = _rules(_COMMENT.sub("", css))
    except ValueError as e:
        blocks.problems.append(f"tokens.css: {e}")
        return blocks
    for prelude, body in rules:
        sel = _norm(prelude)
        if sel == ":root":
            blocks.light = {**(blocks.light or {}), **_declarations(body)}
        elif sel == ':root[data-theme="dark"]':
            blocks.dark_attr = {**(blocks.dark_attr or {}), **_declarations(body)}
        elif sel.startswith("@media") and "prefers-color-scheme:dark" in sel:
            try:
                inner = _rules(body)
            except ValueError as e:
                blocks.problems.append(f"tokens.css: {e}")
                continue
            for p, b in inner:
                if _norm(p) == ':root:not([data-theme="light"])':
                    blocks.dark_media = {
                        **(blocks.dark_media or {}),
                        **_declarations(b),
                    }
    return blocks


def _hex(value: str) -> tuple[float, float, float] | None:
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})(ff)?", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.fullmatch(
        r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)\s*(?:[,/]\s*(1|1\.0+|100%)\s*)?\)", v
    )
    if m:
        return tuple(int(m.group(i)) / 255 for i in (1, 2, 3))  # type: ignore[return-value]
    return None


def resolve(
    name: str,
    scheme: dict[str, str],
    fallback: dict[str, str],
    seen: frozenset = frozenset(),
) -> str | None:
    """The value of *name* in *scheme* (else *fallback*), with every `var()` replaced; None when unresolvable."""
    if name in seen:
        return None
    value = scheme.get(name, fallback.get(name))
    if value is None:
        return None
    while (m := _VAR.search(value)) is not None:
        inner = resolve(m.group(1), scheme, fallback, seen | {name})
        if inner is None:
            inner = m.group(2)
        if inner is None:
            return None
        value = value[: m.start()] + inner + value[m.end() :]
    return value


def _luminance(rgb: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float | None:
    """The WCAG contrast ratio of two colour values, or None when either is not a solid hex or rgb() colour."""
    a, b = _hex(fg), _hex(bg)
    if a is None or b is None:
        return None
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@dataclass
class Ratio:
    scheme: str
    text: str
    background: str
    ratio: float | None


def ratios(blocks: Blocks) -> list[Ratio]:
    """Every contrast pair in every scheme present; dark values fall back to the light block."""
    light = blocks.light or {}
    out: list[Ratio] = []
    for label, scheme, fallback in (
        (LIGHT, light, {}),
        (DARK_MEDIA, blocks.dark_media, light),
        (DARK_ATTR, blocks.dark_attr, light),
    ):
        if scheme is None:
            continue
        for text, background in CONTRAST_PAIRS:
            t = resolve(text, scheme, fallback)
            b = resolve(background, scheme, fallback)
            out.append(
                Ratio(label, text, background, contrast(t, b) if t and b else None)
            )
    return out


def check_folder(path: Path) -> list[str]:
    """Every way the folder at *path* breaks the contract; empty when it holds."""
    path = Path(path)
    if not path.is_dir():
        return [f"{path} is not a directory"]
    problems: list[str] = []
    if not (path / "README.md").is_file():
        problems.append("README.md is missing")
    tokens = path / "tokens.css"
    if not tokens.is_file():
        return [*problems, "tokens.css is missing"]
    for name in ("tokens.css", "fonts.css"):
        f = path / name
        if f.is_file() and _STYLE_END.search(f.read_text(encoding="utf-8")):
            problems.append(
                f"{name} contains </style, which would end the page's style element"
            )
    blocks = parse(tokens.read_text(encoding="utf-8"))
    problems.extend(blocks.problems)
    for label, block, required in (
        (LIGHT, blocks.light, REQUIRED),
        (DARK_MEDIA, blocks.dark_media, COLOURS),
        (DARK_ATTR, blocks.dark_attr, COLOURS),
    ):
        if block is None:
            problems.append(f"tokens.css has no {label} block")
            continue
        missing = [t for t in required if t not in block]
        if missing:
            problems.append(f"{label} is missing {', '.join(missing)}")
        if block is not blocks.light and block.get("color-scheme") != "dark":
            problems.append(f"{label} does not set color-scheme: dark")
    for r in ratios(blocks):
        if r.ratio is None:
            problems.append(
                f"{r.scheme}: cannot check {r.text} on {r.background} (not a solid #hex or rgb() colour)"
            )
        elif r.ratio < AA:
            problems.append(
                f"{r.scheme}: {r.text} on {r.background} is {r.ratio:.2f}:1, below {AA}:1"
            )
    return problems
