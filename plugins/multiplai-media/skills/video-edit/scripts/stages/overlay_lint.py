"""Checks an overlay page before any frame is rendered (`pipeline.py lint-overlay`).

Each check catches a page that looks right in a browser but renders wrong
when it is seeked frame by frame (stages/overlay.py).

Errors (render refuses the page):
  - Math.random(), crypto.getRandomValues(): frames differ between renders.
  - Date.now(), new Date(): the page's clock is the overlay's time, starting
    at 0 (January 1970), not today.
  - a CSS transition: it runs on the browser's clock and ignores seeking.
  - an `infinite` iteration count (CSS) or `iterations: Infinity` (WAAPI).
  - a font stack in which no named family is available (embedded with
    @font-face, or installed on this machine): the text drops to a generic font.
  - any http(s):// URL other than an XML namespace: the page gets no network.

A library inlined in `<script data-library="gsap@3.12.5">…</script>` is not
checked: GSAP and lottie-web call Math.random and Date.now internally
without making frames differ.

Warnings (printed; render continues):
  - a font stack whose first family is not available, so a later one renders;
  - @keyframes or a script tween that animates width, height, left, top,
    font-size or letter-spacing: they re-lay out the page every frame and stutter;
  - a keyed page with a colour close enough to its key colour to be keyed out.

Rules adapted from HyperFrames' linter (heygen-com/hyperframes @ bb9e507,
Apache-2.0): packages/lint/src/rules/core.ts:296,892-937, fonts.ts:209,
gsap.ts:1918.
"""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
from dataclasses import dataclass

GENERIC_FAMILIES = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "emoji", "math",
    "fangsong", "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded",
    # the platform's UI font: present on a Mac, a generic fallback elsewhere
    "-apple-system", "blinkmacsystemfont",
    "inherit", "initial", "unset", "revert",
}
LAYOUT_PROPS_CSS = ("width", "height", "left", "top", "font-size", "letter-spacing")
LAYOUT_PROPS_JS = ("width", "height", "left", "top", "fontSize", "letterSpacing")


@dataclass
class Finding:
    severity: str           # "error" or "warning"
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: line {self.line}: {self.message}"


def _blocks(html: str, tag: str) -> list[tuple[int, str]]:
    """(offset, text) of each <tag>…</tag> body, except inlined libraries."""
    return [(m.start(2), m.group(2)) for m in
            re.finditer(rf"<{tag}\b([^>]*)>(.*?)</{tag}\s*>", html, re.DOTALL | re.IGNORECASE)
            if "data-library" not in m.group(1).lower()]


def _library_spans(html: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"<script\b[^>]*data-library[^>]*>.*?</script\s*>",
                                          html, re.DOTALL | re.IGNORECASE)]


def _style_attrs(html: str) -> list[tuple[int, str]]:
    return [(m.start(2), m.group(2)) for m in
            re.finditer(r"""\sstyle\s*=\s*(["'])(.*?)\1""", html, re.DOTALL | re.IGNORECASE)]


def _blank_comments(text: str, js: bool) -> str:
    """Comments replaced by spaces, so offsets still match the page."""
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.DOTALL)
    if js:
        text = re.sub(r"(?m)^(\s*)//.*$", lambda m: m.group(1) + " " * (len(m.group()) - len(m.group(1))), text)
    return text


def _line(html: str, offset: int) -> int:
    return html.count("\n", 0, offset) + 1


def _families(value: str) -> list[str]:
    return [f.strip().strip("'\"").strip() for f in value.split(",") if f.strip()]


@functools.lru_cache(maxsize=1)
def _fc_default() -> str | None:
    return _fc_match("video-edit-no-such-font-family")


@functools.lru_cache(maxsize=256)
def _fc_match(family: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "--format=%{family}", family],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def installed(family: str) -> bool | None:
    """Whether fontconfig resolves `family` to a font of its own (an alias such as
    Arial → Liberation Sans counts) rather than its default; None without fontconfig."""
    got = _fc_match(family)
    if got is None:
        return None
    names = [n.strip().lower() for n in got.split(",")]
    return family.lower() in names or got != _fc_default()


def _check_fonts(html: str, css: list[tuple[int, str]], out: list[Finding]) -> None:
    declared = set()
    for _, text in css:
        for m in re.finditer(r"@font-face\s*\{([^}]*)\}", text, re.IGNORECASE):
            fam = re.search(r"font-family\s*:\s*([^;]+)", m.group(1), re.IGNORECASE)
            if fam:
                declared.update(f.lower() for f in _families(fam.group(1)))
    unchecked = False
    for off, text in css:
        for m in re.finditer(r"(@font-face\s*\{[^}]*\})|font-family\s*:\s*([^;}]+)", text, re.IGNORECASE):
            if m.group(1):
                continue
            stack = _families(m.group(2))
            named = [f for f in stack if f.lower() not in GENERIC_FAMILIES and not f.lower().startswith("var(")]
            if not named:
                continue
            line = _line(html, off + m.start())
            avail = []
            for f in named:
                ok = f.lower() in declared or installed(f)
                if ok is None:
                    unchecked = True
                    ok = False
                avail.append(ok)
            if unchecked and not any(avail):
                continue
            if not any(avail):
                out.append(Finding("error", line,
                    f"no family in `font-family: {m.group(2).strip()}` is available ({', '.join(named)}): "
                    "the text would render in a generic font. Embed the font with "
                    "@font-face { font-family: …; src: url(data:font/woff2;base64,…) }."))
            elif not avail[0]:
                first = next(f for f, ok in zip(named, avail) if ok)
                out.append(Finding("warning", line,
                    f"{named[0]} is not available here, so the text renders in {first}, "
                    f"the next family in the stack. Embed {named[0]} with @font-face to use it."))
    if unchecked:
        out.append(Finding("warning", 1,
            "could not check which fonts are installed (no fc-match on this machine); "
            "fonts without @font-face may render in a fallback."))


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _colours(text: str) -> list[tuple[int, tuple[int, int, int]]]:
    """Colours in declaration values (after a colon), so `#bad {` selectors are not read."""
    found = []
    for d in re.finditer(r":([^;{}]+)", text):
        v, base = d.group(1), d.start(1)
        for m in re.finditer(r"#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})\b", v, re.IGNORECASE):
            found.append((base + m.start(), _hex_rgb(m.group())))
        for m in re.finditer(r"rgba?\(\s*(\d+)\s*,?\s*(\d+)\s*,?\s*(\d+)", v, re.IGNORECASE):
            found.append((base + m.start(), (int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    return found


def key_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """ffmpeg colorkey's distance: 0 is the same colour, 1 is black to white."""
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / (3 * 255 ** 2)) ** 0.5


def lint(html: str, mode: str = "keyed", key_color: str = "#00FF00",
         key_reach: float = 0.38) -> list[Finding]:
    """Findings for one page, errors first. `key_reach` is the colorkey
    similarity plus its blend: a colour nearer the key than that is removed."""
    out: list[Finding] = []
    css = [(o, _blank_comments(t, js=False)) for o, t in _blocks(html, "style") + _style_attrs(html)]
    js = [(o, _blank_comments(t, js=True)) for o, t in _blocks(html, "script")]

    for off, text in js:
        for pat, what in ((r"Math\.random\s*\(", "Math.random() makes frames differ between renders; use fixed values or a seeded generator"),
                          (r"crypto\.getRandomValues\s*\(", "crypto.getRandomValues() makes frames differ between renders; use fixed values"),
                          (r"Date\.now\s*\(", "Date.now() reads the overlay's clock, which starts at 0 (January 1970), not today; write fixed values"),
                          (r"new\s+Date\s*\(\s*\)", "new Date() is the overlay's clock, which starts at 0 (January 1970), not today; write the date out")):
            for m in re.finditer(pat, text):
                out.append(Finding("error", _line(html, off + m.start()), what))
        for m in re.finditer(r"iterations\s*:\s*Infinity\b", text):
            out.append(Finding("error", _line(html, off + m.start()),
                               "iterations: Infinity: give the animation a finite count that covers the overlay"))
        for m in re.finditer(r"\.(animate|to|from|fromTo)\s*\(", text):
            call = text[m.end():m.end() + 600].split(");", 1)[0]
            seen = set()
            for k in re.finditer(rf"\b({'|'.join(LAYOUT_PROPS_JS)})\s*:", call):
                if k.group(1) in seen:
                    continue
                seen.add(k.group(1))
                out.append(Finding("warning", _line(html, off + m.end() + k.start()),
                                   f"a script animates {k.group(1)}, which re-lays out the page every frame and "
                                   "stutters; animate transform or opacity instead"))

    for off, text in css:
        for m in re.finditer(r"\btransition(-duration)?\s*:\s*([^;}]+)", text, re.IGNORECASE):
            value = m.group(2).strip().lower()
            if value in ("none", "0", "0s", "0ms", "all 0s") or re.fullmatch(r"(\w+\s+)?0m?s(\s.*)?", value):
                continue
            out.append(Finding("error", _line(html, off + m.start()),
                               f"`{m.group().strip()}`: a CSS transition runs on the browser's clock and ignores "
                               "seeking; use a CSS animation with a delay instead"))
        for m in re.finditer(r"\banimation(-iteration-count)?\s*:[^;}]*\binfinite\b", text, re.IGNORECASE):
            out.append(Finding("error", _line(html, off + m.start()),
                               "an infinite CSS animation: give it a finite iteration count that covers the overlay"))
        for kf in re.finditer(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^}]*\})*)\s*\}", text, re.IGNORECASE):
            seen = set()
            for p in re.finditer(rf"(?<![\w-])({'|'.join(LAYOUT_PROPS_CSS)})\s*:", kf.group(2), re.IGNORECASE):
                if p.group(1).lower() in seen:
                    continue
                seen.add(p.group(1).lower())
                out.append(Finding("warning", _line(html, off + kf.start(2) + p.start()),
                                   f"@keyframes {kf.group(1)} animates {p.group(1)}, which re-lays out the page "
                                   "every frame and stutters; animate transform or opacity instead"))

    _check_fonts(html, css, out)

    libraries = _library_spans(html)
    for m in re.finditer(r"""(?<![\w:-])(https?://[^\s"'()<>]+)""", html, re.IGNORECASE):
        if any(a <= m.start() < b for a, b in libraries):
            continue
        before = html[max(0, m.start() - 40):m.start()]
        if re.search(r"xmlns(:\w+)?\s*=\s*[\"']$", before):
            continue
        out.append(Finding("error", _line(html, m.start()),
                           f"{m.group(1)}: the page gets no network; inline the file (a data: URI or inline SVG)"))

    if mode == "keyed":
        key = _hex_rgb(key_color)
        for off, text in css:
            for pos, rgb in _colours(text):
                d = key_distance(rgb, key)
                if 0 < d < key_reach:
                    out.append(Finding("warning", _line(html, off + pos),
                                       f"colour rgb{rgb} is close to the key colour {key_color} "
                                       f"(distance {d:.2f}, keyed out below {key_reach:.2f}): it will turn "
                                       "transparent; pick a colour further away or use mode alpha"))

    return sorted(out, key=lambda f: (f.severity != "error", f.line))


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]
