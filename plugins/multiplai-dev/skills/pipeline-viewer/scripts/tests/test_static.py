"""The page's files: what index.html loads exists, scripts from the CDN are
pinned and carry integrity hashes, and the tokens are the design system's."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "pipeline_viewer" / "static"
NEUTRAL = (
    Path(__file__).resolve().parents[3]
    / "design-system"
    / "assets"
    / "neutral"
    / "tokens.css"
)


def _index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_every_local_file_the_page_loads_exists():
    names = re.findall(r'(?:src|href)="/static/([^"]+)"', _index())
    assert names, "index.html loads nothing from /static/"
    for name in names:
        assert (STATIC / name).is_file(), name


def test_cdn_files_are_pinned_with_integrity():
    tags = re.findall(r"<(?:script|link)[^>]*cdnjs\.cloudflare\.com[^>]*>", _index())
    assert tags
    for tag in tags:
        assert re.search(r"/\d+\.\d+\.\d+/", tag), tag
        assert 'integrity="sha512-' in tag, tag
        assert 'crossorigin="anonymous"' in tag, tag


def test_no_inline_script_or_style():
    # The server's CSP allows scripts and styles from files only.
    html = _index()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html
    assert " style=" not in html


def test_tokens_are_the_neutral_design_system():
    assert (STATIC / "tokens.css").read_text() == NEUTRAL.read_text()
