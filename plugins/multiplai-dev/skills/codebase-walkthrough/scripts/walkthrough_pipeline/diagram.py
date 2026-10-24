"""The diagram stage's picture outside the model: PNGs for a model to look at, and the figure on the page.

`standalone` turns one of the model's SVGs into a file a renderer can draw on its own: the page's icon
symbols in its `<defs>`, its CSS inlined with each `var(--token)` replaced by the design system's value for
the theme, the design system's fonts swapped for `sans-serif` and `monospace` (the renderer has neither),
a size in px and a `--surface` background. `render_pngs` draws the three PNGs a look reads with `resvg-py`,
which ships its own wheels and needs no system library; it is imported only here, so a platform without a
wheel loses the drawn picture (`DiagramUnavailable`) and nothing else. `page_figure` puts both SVGs on the
page, with their ids made unique on it. `gates.diagram_gate` has passed every SVG these functions get.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import design_system
from design_system.tokens import resolve as resolve_token

SVG_NS = "http://www.w3.org/2000/svg"
# Each PNG a look reads: (name, theme, layout, width in px).
PNGS = (
    ("light-wide", "light", "wide", 1600),
    ("dark-wide", "dark", "wide", 1600),
    ("light-tall", "light", "tall", 840),
)
ROOT = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)")
FONT = re.compile(r"(font-family\s*:)([^;}]*)", re.IGNORECASE)
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
    )
}
CAMEL_ATTR = re.compile(r"(\s)(" + "|".join(CAMEL) + r")(\s*=)", re.IGNORECASE)
ID_ATTR = re.compile(r"(\sid\s*=\s*)([\"'])(.*?)\2")
HREF_ATTR = re.compile(r"(\shref\s*=\s*)([\"'])#(.*?)\2")
URL_REF = re.compile(r"url\(#([^)]*)\)")
LABELLED = re.compile(r"(\saria-(?:labelledby|describedby)\s*=\s*)([\"'])(.*?)\2")


class DiagramUnavailable(Exception):
    """`resvg-py` will not import on this platform, so no PNG can be drawn."""


def theme_values(tokens: str, theme: str) -> dict[str, str]:
    """Every token of the design system's `tokens.css` text with its value for *theme* ("light" or "dark"),
    each `var()` inside it resolved."""
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


def _inline_css(css: str, values: dict[str, str]) -> str:
    """*css* with each `var(--x)` replaced by its value (its fallback, else `inherit`, when unknown) and each
    font family by `monospace` when it names a monospace font, else `sans-serif`."""
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


def standalone(svg: str, css: str, tokens: str, theme: str, width_px: int) -> str:
    """One of the model's SVGs as a file a renderer draws alone, in *theme* of the design system whose
    `tokens.css` text is *tokens*, *width_px* wide."""
    values = theme_values(tokens, theme)
    svg = CAMEL_ATTR.sub(
        lambda m: m.group(1) + CAMEL[m.group(2).lower()] + m.group(3), svg
    )
    root = ROOT.search(svg)
    if root is None:
        return svg
    vb_w, vb_h = _view_box(root.group(0))
    height = round(width_px * vb_h / vb_w) if vb_w else width_px
    tag = re.sub(
        r"\s(?:width|height|xmlns|font-family)\s*=\s*(\"[^\"]*\"|'[^']*')",
        "",
        root.group(0),
    )
    tag = (
        tag[:-1].rstrip("/").rstrip()
        + f' xmlns="{SVG_NS}" width="{width_px}" height="{height}" font-family="sans-serif">'
    )
    # Imported here: render imports this module.
    from .render import ICON_SPRITE

    symbols = "".join(re.findall(r"<symbol\b.*?</symbol>", ICON_SPRITE, re.DOTALL))
    style = f"<style>{_inline_css(css, values)}</style>"
    surface = values.get("--surface", "#ffffff")
    background = (
        f'<rect x="0" y="0" width="{vb_w:g}" height="{vb_h:g}" fill="{surface}"/>'
    )
    body = svg[root.end() :]
    return f"{svg[: root.start()]}{tag}<defs>{symbols}{style}</defs>{background}{body}"


def render_pngs(out, tokens: str, folder: Path, round_no: int) -> list[Path]:
    """Draw *out*'s SVGs (a `DiagramOutput`) as `round-N-light-wide.png`, `round-N-dark-wide.png` and
    `round-N-light-tall.png` in *folder*; return their paths. Raises `DiagramUnavailable` when `resvg-py`
    will not import, and ValueError when it cannot draw an SVG."""
    try:
        import resvg_py
    except ImportError as e:
        raise DiagramUnavailable(str(e)) from e
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, theme, layout, width in PNGS:
        svg = standalone(getattr(out, layout), out.css, tokens, theme, width)
        path = folder / f"round-{round_no}-{name}.png"
        path.write_bytes(bytes(resvg_py.svg_to_bytes(svg_string=svg)))
        paths.append(path)
    return paths


def _prefixed(svg: str, prefix: str, extra: str, alt: str) -> str:
    """*svg* with each of its ids, and each reference to one, prefixed with *prefix*; its root gets the
    class *extra*, `role="img"` and *alt* as its label. References to the page's icons stay as they are."""
    ids = {m.group(3) for m in ID_ATTR.finditer(svg)}
    svg = ID_ATTR.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{prefix}{m.group(3)}{m.group(2)}", svg
    )
    svg = HREF_ATTR.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}#"
            + (prefix if m.group(3) in ids else "")
            + f"{m.group(3)}{m.group(2)}"
        ),
        svg,
    )
    svg = URL_REF.sub(
        lambda m: f"url(#{prefix if m.group(1) in ids else ''}{m.group(1)})", svg
    )
    svg = LABELLED.sub(
        lambda m: (
            m.group(1)
            + m.group(2)
            + " ".join(prefix + i if i in ids else i for i in m.group(3).split())
            + m.group(2)
        ),
        svg,
    )
    root = ROOT.search(svg)
    if root is None:
        return svg
    tag = root.group(0)
    cls = re.search(r"\sclass\s*=\s*([\"'])(.*?)\1", tag)
    tag = re.sub(r"\s(?:class|role|aria-[\w-]+)\s*=\s*(\"[^\"]*\"|'[^']*')", "", tag)
    classes = " ".join(x for x in (extra, cls.group(2) if cls else "") if x)
    tag = (
        tag[:-1].rstrip("/").rstrip()
        + f' class="{classes}" role="img" aria-label="{html.escape(alt)}">'
    )
    return svg[: root.start()] + tag + svg[root.end() :]


def page_figure(diagram: dict, caption: str = "How it works") -> str:
    """The `<figure class="diagram">` holding both SVGs of *diagram* (`state.diagram`): the template shows
    `wide` at 700 px and up and `tall` below. Ids become `dw-<id>` and `dt-<id>`, so the two never clash."""
    alt = diagram.get("alt") or caption
    return (
        f'<figure class="diagram" aria-label="{html.escape(caption)}">'
        + _prefixed(diagram["wide"], "dw-", "dg-wide", alt)
        + _prefixed(diagram["tall"], "dt-", "dg-tall", alt)
        + "</figure>"
    )
