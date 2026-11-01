"""Draws each SVG of a page to PNG, in the light and the dark theme, so the session can look at it.

A renderer draws an SVG alone, without the page around it. So each SVG gets the page's own CSS inside it,
with each `var(--token)` replaced by the design system's value for the theme, at-rules (`@media`,
`@font-face`, …) removed, font families swapped for `sans-serif` or `monospace` (the renderer has
neither), a pixel size, the page's `--fg` as its text colour and a `--surface` background. `resvg-py`
ships its own wheels and needs no system library; it is imported only here, so a platform without a
wheel loses the PNGs (`RenderUnavailable`) and nothing else. Only SVGs are drawn: the page as a whole is
the user's to look at in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import design_system
from design_system.tokens import resolve as resolve_token

from .check import TOKEN_STYLES
from .page import Page

SVG_NS = "http://www.w3.org/2000/svg"
ROOT = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)")
FONT = re.compile(r"(font-family\s*:)([^;}]*)", re.IGNORECASE)
AT_RULE = re.compile(r"@[\w-]+[^{;]*")
# SVG attributes whose case an XML renderer needs, as a browser's HTML parser restores it.
CAMEL = {
    a.lower(): a
    for a in (
        "viewBox",
        "refX",
        "refY",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "preserveAspectRatio",
        "textLength",
        "lengthAdjust",
    )
}
CAMEL_ATTR = re.compile(r"(\s)(" + "|".join(CAMEL) + r")(\s*=)", re.IGNORECASE)
THEMES = ("light", "dark")
MIN_PX, MAX_PX = 800, 1600


class RenderUnavailable(Exception):
    """`resvg-py` will not import on this platform, so no PNG can be drawn."""


def theme_values(tokens: str, theme: str) -> dict[str, str]:
    """Every token of *tokens* (a design system's `tokens.css` text) with its value for *theme*."""
    blocks = design_system.parse(tokens)
    light = blocks.light or {}
    scheme = (
        {**light, **(blocks.dark_media or blocks.dark_attr or {})}
        if theme == "dark"
        else light
    )
    return {
        name: value
        for name in scheme
        if (value := resolve_token(name, scheme, light)) is not None
    }


def _drop_at_rules(css: str) -> str:
    """*css* without its at-rules: a block one (`@media {…}`) is removed with everything inside it."""
    out: list[str] = []
    i = 0
    while i < len(css):
        m = AT_RULE.match(css, i) if css[i] == "@" else None
        if not m:
            out.append(css[i])
            i += 1
            continue
        j = m.end()
        if j < len(css) and css[j] == ";":
            i = j + 1
            continue
        if j < len(css) and css[j] == "{":
            depth = 0
            while j < len(css):
                depth += {"{": 1, "}": -1}.get(css[j], 0)
                j += 1
                if depth == 0:
                    break
        i = j
    return "".join(out)


def _inline_css(css: str, values: dict[str, str]) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = _drop_at_rules(css)
    for _ in range(3):  # a fallback can itself hold a var()
        css = VAR.sub(lambda m: values.get(m.group(1)) or m.group(2) or "inherit", css)
    return FONT.sub(
        lambda m: (
            m.group(1) + ("monospace" if "mono" in m.group(2).lower() else "sans-serif")
        ),
        css,
    )


def _view_box(root: str) -> tuple[float, float]:
    m = re.search(r"viewBox\s*=\s*[\"']([^\"']*)", root, re.IGNORECASE)
    parts = (m.group(1) if m else "").replace(",", " ").split()
    try:
        return float(parts[2]), float(parts[3])
    except (IndexError, ValueError):
        return 1000.0, 600.0


def standalone(
    svg: str, page: Page, theme: str, around: list[str] | None = None
) -> str:
    """*svg*, one SVG of *page*, as a file a renderer draws alone in *theme*. Its content goes inside a
    `<g>` with *around*, the classes of the page elements around it, so a rule such as `.fig .node`
    matches as it does on the page."""
    values = theme_values(page.tokens_css(), theme)
    svg = CAMEL_ATTR.sub(
        lambda m: m.group(1) + CAMEL[m.group(2).lower()] + m.group(3), svg
    )
    for _ in range(3):
        svg = VAR.sub(lambda m: values.get(m.group(1)) or m.group(2) or "inherit", svg)
    root = ROOT.search(svg)
    if root is None:
        return svg
    vb_w, vb_h = _view_box(root.group(0))
    width = max(MIN_PX, min(MAX_PX, round(vb_w * 2)))
    height = round(width * vb_h / vb_w) if vb_w else width
    fg = values.get("--fg", "#000000")
    tag = re.sub(
        r"\s(?:width|height|xmlns|font-family|color)\s*=\s*(\"[^\"]*\"|'[^']*')",
        "",
        root.group(0),
    )
    tag = (
        tag[:-1].rstrip("/").rstrip()
        + f' xmlns="{SVG_NS}" width="{width}" height="{height}" font-family="sans-serif" color="{fg}">'
    )
    page_css = "\n".join(css for sid, css in page.styles if sid not in TOKEN_STYLES)
    style = f"<style>text{{fill:{fg}}}\n{_inline_css(page_css, values)}</style>"
    surface = values.get("--surface", "#ffffff")
    background = (
        f'<rect x="0" y="0" width="{vb_w:g}" height="{vb_h:g}" fill="{surface}"/>'
    )
    if svg.rstrip().endswith("/>") and root.end() == len(svg.rstrip()):
        return f"{tag}<defs>{style}</defs>{background}</svg>"
    inner = svg[root.end() :].rstrip()
    if inner.lower().endswith("</svg>"):
        inner = inner[: -len("</svg>")]
    group = f'<g class="{" ".join(dict.fromkeys(around or []))}">'
    return f"{svg[: root.start()]}{tag}<defs>{style}</defs>{background}{group}{inner}</g></svg>"


def render_svgs(page: Page, folder: Path) -> list[Path]:
    """Draw every SVG of *page* as `<n>-light.png` and `<n>-dark.png` in *folder*; return their paths.
    Raises `RenderUnavailable` when `resvg-py` will not import, and ValueError naming the SVG it cannot
    draw."""
    try:
        import resvg_py
    except ImportError as e:
        raise RenderUnavailable(str(e)) from e
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, svg in enumerate(page.svgs, 1):
        around = page.svg_ancestors[n - 1] if n <= len(page.svg_ancestors) else []
        for theme in THEMES:
            path = folder / f"{n}-{theme}.png"
            try:
                data = resvg_py.svg_to_bytes(
                    svg_string=standalone(svg, page, theme, around)
                )
            except Exception as e:  # resvg raises its own error types
                raise ValueError(f"SVG {n} ({theme}): {e}") from e
            path.write_bytes(bytes(data))
            paths.append(path)
    return paths
