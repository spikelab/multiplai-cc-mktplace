"""What `check` and `render` need from one HTML page, read with `html.parser`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Text inside these is never shown as words on the page.
HIDDEN = {"script", "style", "svg", "template", "noscript", "head"}
# HTML elements with no closing tag.
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
SVG_OPEN = re.compile(r"<svg\b", re.IGNORECASE)
SVG_TAG = re.compile(r"<(/?)svg\b[^>]*?(/?)>", re.IGNORECASE)


@dataclass
class Cite:
    """One `data-cite="<repo key>@<sha>:<path>:<start>-<end>"` link, with its optional `data-quote`."""

    raw: str
    quote: str = ""


@dataclass
class Page:
    title: str = ""
    h1: int = 0
    open_words: int = 0
    styles: list[tuple[str, str]] = field(default_factory=list)  # (id, css)
    style_attrs: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)  # src of each external script
    inline_scripts: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)  # (rel, href)
    media: list[tuple[str, str]] = field(default_factory=list)  # (tag, src)
    ids: list[str] = field(default_factory=list)
    svgs: list[str] = field(default_factory=list)  # the source of each outermost <svg>
    cites: list[Cite] = field(default_factory=list)

    def tokens_css(self) -> str:
        """The `<style id="tokens">` block: the design system's tokens, inlined."""
        return "\n".join(css for sid, css in self.styles if sid == "tokens")


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = Page()
        self.stack: list[str] = []
        self.details = 0  # open <details> elements
        self.summary = 0  # open <summary> elements
        self.hidden = 0
        self.in_title = False
        self.style_id: str | None = None
        self.style_buf: list[str] = []
        self.words: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        p = self.page
        if a.get("id"):
            p.ids.append(a["id"])
        if a.get("style"):
            p.style_attrs.append(a["style"])
        if "data-cite" in a:
            p.cites.append(Cite(a["data-cite"], a.get("data-quote", "")))
        if tag == "script":
            if a.get("src"):
                p.scripts.append(a["src"])
            else:
                p.inline_scripts += 1
        elif tag == "link":
            p.links.append((a.get("rel", "").lower(), a.get("href", "")))
        elif tag in {"img", "iframe", "video", "audio", "source", "embed", "object"}:
            p.media.append((tag, a.get("src") or a.get("data") or ""))
        elif tag == "style":
            self.style_id = a.get("id", "")
            self.style_buf = []
        elif tag == "title" and not self.hidden_svg():
            self.in_title = True
        elif tag == "h1":
            p.h1 += 1
        if tag in VOID:
            return
        self.stack.append(tag)
        if tag == "details":
            self.details += 1
        elif tag == "summary":
            self.summary += 1
        if tag in HIDDEN:
            self.hidden += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def hidden_svg(self) -> bool:
        return "svg" in self.stack

    def handle_endtag(self, tag):
        if tag == "style" and self.style_id is not None:
            self.page.styles.append((self.style_id, "".join(self.style_buf)))
            self.style_id = None
        if tag == "title":
            self.in_title = False
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            if top == "details":
                self.details -= 1
            elif top == "summary":
                self.summary -= 1
            if top in HIDDEN:
                self.hidden -= 1
            if top == tag:
                break

    def handle_data(self, data):
        if self.style_id is not None:
            self.style_buf.append(data)
            return
        if self.in_title:
            self.page.title += data
            return
        if self.hidden:
            return
        # A closed fold-out shows its <summary> line and nothing else.
        if self.details and not self.summary:
            return
        self.words.extend(data.split())


def _svgs(source: str) -> list[str]:
    """The source text of each outermost `<svg>…</svg>`, a nested one kept inside its parent."""
    out: list[str] = []
    depth = 0
    start = 0
    for m in SVG_TAG.finditer(source):
        closing, selfclosing = m.group(1), m.group(2)
        if closing:
            if depth:
                depth -= 1
                if depth == 0:
                    out.append(source[start : m.end()])
        elif selfclosing:
            if depth == 0:
                out.append(m.group(0))
        else:
            if depth == 0:
                start = m.start()
            depth += 1
    return out


def parse_page(source: str) -> Page:
    r = _Reader()
    r.feed(source)
    r.close()
    page = r.page
    page.title = " ".join(page.title.split())
    page.open_words = len(r.words)
    page.svgs = _svgs(source)
    return page
