"""The checks `check` runs on a page. Each returns failure reasons; an empty list means the page passes."""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .page import Page

# The model page (a pipeline followed through six stages, with cost and road-map sections) has 1,670.
MAX_WORDS = 2000
# Hosts a published page may load scripts from; each script URL must name an exact version.
SCRIPT_HOSTS = (
    "cdnjs.cloudflare.com",
    "cdn.jsdelivr.net/npm/",
    "unpkg.com",
    "cdn.tailwindcss.com",
    "code.jquery.com",
)
STYLESHEET_HOSTS = ("fonts.googleapis.com",)
PRECONNECT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
# Style blocks that hold the design system itself, where colour values belong.
TOKEN_STYLES = {"tokens", "fonts"}

COLOUR_FUNC = re.compile(
    r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(", re.IGNORECASE
)
HEX = re.compile(r"(?<![\w(])#[0-9a-fA-F]{3,8}\b")
NAMED = re.compile(
    r"(?:^|[;{\s])(?:color|fill|stroke|background(?:-color)?|border(?:-[a-z]+)?(?:-color)?|outline(?:-color)?|"
    r"stop-color|text-decoration-color|box-shadow)\s*:[^;}]*?\b"
    r"(white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|brown|navy|teal|silver)\b",
    re.IGNORECASE,
)
VAR_USE = re.compile(r"var\(\s*(--[\w-]+)\s*(,)?")
VAR_DEF = re.compile(r"(--[\w-]+)\s*:")
# SVG attributes that take a colour.
PAINT_ATTRS = {"fill", "stroke", "stop-color", "flood-color", "lighting-color", "color"}
PAINT_OK = re.compile(
    r"^(?:none|currentcolor|transparent|inherit|var\(--[\w-]+\)|url\(#[\w-]+\))$",
    re.IGNORECASE,
)
SVG_BANNED = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video"}
CITE = re.compile(
    r"^(?P<key>[\w.-]+)@(?P<sha>[0-9a-f]{7,40}):(?P<path>[^:]+):(?P<start>\d+)(?:-(?P<end>\d+))?$"
)


@dataclass
class Options:
    max_words: int = MAX_WORDS
    repos: dict[str, Path] = field(
        default_factory=dict
    )  # citation repo key -> git checkout


def _host_path(url: str) -> str:
    u = urlparse(url if "//" in url else "")
    return (u.netloc + u.path) if u.netloc else ""


def _external(url: str) -> bool:
    return bool(re.match(r"^(?:https?:)?//", url.strip(), re.IGNORECASE))


def check_words(page: Page, opts: Options) -> list[str]:
    if page.open_words > opts.max_words:
        return [
            (
                f"{page.open_words:,} words are shown before any fold-out is opened; at most {opts.max_words:,}. "
                "Move detail into <details> fold-outs or cut it."
            )
        ]
    return []


def check_title(page: Page) -> list[str]:
    reasons = []
    n = len(page.title.split())
    if not 2 <= n <= 4:
        reasons.append(
            f"the <title> {page.title!r} has {n} words; it is a name of 2 to 4 words"
        )
    if page.h1 != 1:
        reasons.append(f"the page has {page.h1} <h1> elements; it needs exactly one")
    return reasons


def check_resources(page: Page) -> list[str]:
    reasons = []
    for src in page.scripts:
        hp = _host_path(src)
        if not hp or not any(hp.startswith(h) for h in SCRIPT_HOSTS):
            reasons.append(f"the script {src!r} is not from {', '.join(SCRIPT_HOSTS)}")
        elif not EXACT_VERSION.search(src):
            reasons.append(f"the script {src!r} does not name an exact version")
    for rel, href in page.links:
        if not _external(href):
            continue
        hp = _host_path(href)
        if "stylesheet" in rel and any(hp.startswith(h) for h in STYLESHEET_HOSTS):
            continue
        if "preconnect" in rel and any(hp.startswith(h) for h in PRECONNECT_HOSTS):
            continue
        reasons.append(f"the <link rel={rel!r}> loads {href!r} from outside the page")
    for tag, src in page.media:
        if _external(src):
            reasons.append(f"the <{tag}> loads {src!r} from outside the page")
    for sid, css in page.styles:
        if re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", css, re.IGNORECASE) and (
            sid != "fonts"
        ):
            reasons.append(
                f"a <style> block (id {sid!r}) loads a resource; only the fonts block may"
            )
    return reasons


def _colour_literals(text: str) -> list[str]:
    found = [m.group(0) for m in HEX.finditer(text)]
    found += [m.group(0) + "…)" for m in COLOUR_FUNC.finditer(text)]
    found += [m.group(1) for m in NAMED.finditer(text)]
    return found


def check_colours(page: Page) -> list[str]:
    reasons = []
    for sid, css in page.styles:
        if sid in TOKEN_STYLES:
            continue
        for lit in dict.fromkeys(_colour_literals(css)):
            reasons.append(
                f"the <style> block (id {sid or 'none'!r}) types the colour {lit}; use a design-system token"
            )
    for attr in page.style_attrs:
        for lit in dict.fromkeys(_colour_literals(attr)):
            reasons.append(
                f"the style attribute {attr!r} types the colour {lit}; use a design-system token"
            )
    return reasons


def check_tokens(page: Page) -> list[str]:
    reasons = []
    tokens = page.tokens_css()
    if not tokens.strip():
        reasons.append(
            'the page has no <style id="tokens"> block holding the design system\'s tokens.css'
        )
    else:
        squashed = re.sub(r"\s+", "", tokens).replace("'", '"')
        if "prefers-color-scheme:dark" not in squashed:
            reasons.append(
                "the tokens block has no @media (prefers-color-scheme: dark) block"
            )
        if ':root[data-theme="dark"]' not in squashed:
            reasons.append('the tokens block has no :root[data-theme="dark"] block')
    defined = set()
    for _, css in page.styles:
        defined |= set(VAR_DEF.findall(css))
    used: dict[str, bool] = {}
    for text in [css for _, css in page.styles] + page.style_attrs + page.svgs:
        for m in VAR_USE.finditer(text):
            used[m.group(1)] = used.get(m.group(1), False) or bool(m.group(2))
    for name, has_fallback in used.items():
        if name not in defined and not has_fallback:
            reasons.append(f"var({name}) is used but no style block defines {name}")
    return reasons


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def check_svgs(page: Page) -> list[str]:
    reasons = []
    for n, svg in enumerate(page.svgs, 1):
        where = f"SVG {n}"
        try:
            root = ET.fromstring(svg)
        except ET.ParseError as e:
            reasons.append(
                f"{where} is not well-formed XML ({e}); write &#160; for &nbsp; and close every element"
            )
            continue
        attrs = {k.lower(): v for k, v in root.attrib.items()}
        if "viewbox" not in attrs:
            reasons.append(f"{where} has no viewBox")
        labelled = (
            attrs.get("aria-label")
            or attrs.get("aria-labelledby")
            or any(_local(c.tag) == "title" for c in root)
        )
        if attrs.get("role") != "img" or not labelled:
            reasons.append(
                f'{where} needs role="img" and an aria-label or a <title> saying what it shows'
            )
        for el in root.iter():
            tag = _local(el.tag)
            if tag in SVG_BANNED:
                reasons.append(f"{where} holds a <{tag}> element")
            for k, v in el.attrib.items():
                name = _local(k)
                if name.startswith("on"):
                    reasons.append(f"{where}: <{tag}> has an event handler {name}")
                elif name == "href" and not (
                    v.startswith("#")
                    or (tag == "image" and v.startswith("data:image/"))
                ):
                    reasons.append(
                        f"{where}: <{tag}> links to {v!r}; only #id is allowed"
                    )
                elif name in PAINT_ATTRS and not PAINT_OK.match(v.strip()):
                    reasons.append(
                        f"{where}: <{tag} {name}={v!r}> types a colour; use none, currentColor or var(--token)"
                    )
                elif name == "style":
                    for lit in _colour_literals(v):
                        reasons.append(
                            f"{where}: <{tag}> style types the colour {lit}; use a design-system token"
                        )
    return reasons


def check_ids(page: Page) -> list[str]:
    seen: dict[str, int] = {}
    for i in page.ids:
        seen[i] = seen.get(i, 0) + 1
    return [f"the id {i!r} is used {c} times" for i, c in seen.items() if c > 1]


def _git_lines(repo: Path, sha: str, path: str) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
        return None
    return out.stdout.splitlines()


def check_cites(page: Page, opts: Options) -> list[str]:
    reasons = []
    cache: dict[tuple[str, str, str], list[str] | None] = {}
    for cite in page.cites:
        m = CITE.match(cite.raw.strip())
        if not m:
            reasons.append(
                f"the citation {cite.raw!r} is not <repo key>@<sha>:<path>:<start>[-<end>]"
            )
            continue
        key, sha, path = m.group("key"), m.group("sha"), m.group("path")
        start = int(m.group("start"))
        end = int(m.group("end") or start)
        repo = opts.repos.get(key)
        if repo is None:
            reasons.append(
                f"the citation {cite.raw!r} names the repo {key!r}; pass --repo {key}=<checkout>"
            )
            continue
        lines = cache.setdefault((key, sha, path), _git_lines(repo, sha, path))
        if lines is None:
            reasons.append(f"the citation {cite.raw!r}: {path} is not at {sha}")
            continue
        if not 1 <= start <= end <= len(lines):
            reasons.append(
                f"the citation {cite.raw!r}: lines {start}-{end} are outside the file's {len(lines)} lines"
            )
            continue
        if cite.quote:
            want = " ".join(cite.quote.split())
            got = " ".join(" ".join(lines[start - 1 : end]).split())
            if want not in got:
                reasons.append(
                    f"the citation {cite.raw!r}: its data-quote is not in those lines"
                )
    return reasons


def check_page(page: Page, opts: Options | None = None) -> list[str]:
    opts = opts or Options()
    return (
        check_title(page)
        + check_words(page, opts)
        + check_tokens(page)
        + check_colours(page)
        + check_resources(page)
        + check_svgs(page)
        + check_ids(page)
        + check_cites(page, opts)
    )
