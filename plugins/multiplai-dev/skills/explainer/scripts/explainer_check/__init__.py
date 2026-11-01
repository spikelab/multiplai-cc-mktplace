"""Checks an explainer page and draws its SVGs to PNG. Never calls a model.

`check` reads one HTML page and returns one reason per failure: too many words open before a fold-out
is opened, a title that is not a short name, a colour typed outside the design system's tokens, a
`var(--x)` no block defines, a resource loaded from a host the page may not use, an SVG that is not
well-formed or holds a script, a link out or an unlabelled picture, an id used twice, and a code
citation whose lines are not at its commit. `render` draws each SVG in the light and the dark theme.
"""

from __future__ import annotations

from .check import Options, check_page
from .page import Page, parse_page
from .render import RenderUnavailable, render_svgs

__all__ = [
    "Options",
    "Page",
    "RenderUnavailable",
    "check_page",
    "parse_page",
    "render_svgs",
]
