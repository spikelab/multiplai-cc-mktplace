"""Find the repo's PR template and parse it into sections.

Discovery follows GitHub's documented locations (docs.github.com, "Creating a
pull request template for your repository", fetched 2026-10-11): a
`pull_request_template.md` in `.github/`, the root or `docs/`, or a
`PULL_REQUEST_TEMPLATE/` directory in any of the three. That page does not say
whether names are case-insensitive, so names are matched in any case: that
cannot miss a template GitHub would use. `pr.template` in
`<repo>/specs/config.yaml` overrides discovery. When the repo has no template,
or a template directory holds several and `pr.template` picks none, the
standard template that ships with this skill is used.

A section is a Markdown heading (any level) and the text up to the next
heading of the same or higher level. Each `Section` holds only its own blocks
(up to the next heading of any level); `subtree_end` gives the index where its
nested sections end. Headings, checkbox lines and HTML comments are kept byte
for byte. Lines inside a fenced code block or an HTML comment are never
headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

STANDARD_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "pull_request_template.md"

# Searched in this order; "" is the repo root.
LOCATIONS = (".github", "", "docs")
TEMPLATE_NAME = "pull_request_template.md"
TEMPLATE_DIR = "pull_request_template"
# The `pr.template` value buildme setup writes by default: discover, as when unset.
AUTO_TEMPLATE = "auto"


class TemplateError(Exception):
    """A template or config that cannot be used. Exit code 2."""


@dataclass(frozen=True)
class PrConfig:
    template: str | None = None
    draft: bool = True


@dataclass(frozen=True)
class Discovery:
    path: Path
    standard: bool
    diagnosis: str | None = None


def load_config(repo: Path) -> PrConfig:
    """`pr.template` and `pr.draft` from `<repo>/specs/config.yaml`; defaults when absent or `auto`."""
    path = repo / "specs" / "config.yaml"
    if not path.is_file():
        return PrConfig()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise TemplateError(f"{path} is not valid YAML: {e}") from e
    pr = data.get("pr") if isinstance(data, dict) else None
    if pr is None:
        return PrConfig()
    if not isinstance(pr, dict):
        raise TemplateError(f"`pr:` in {path} must be a mapping, got {type(pr).__name__}")
    template = pr.get("template")
    if template is not None and not isinstance(template, str):
        raise TemplateError(f"`pr.template` in {path} must be a repo-relative path")
    draft = pr.get("draft", True)
    if not isinstance(draft, bool):
        raise TemplateError(f"`pr.draft` in {path} must be true or false")
    if template == AUTO_TEMPLATE:
        template = None
    return PrConfig(template=template or None, draft=draft)


def _entry(directory: Path, lower_name: str, *, want_dir: bool) -> Path | None:
    """The entry of *directory* whose name is *lower_name* in any case (first by sorted name)."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for p in entries:
        if p.name.lower() == lower_name and (p.is_dir() if want_dir else p.is_file()):
            return p
    return None


def _rel(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def discover(repo: Path, config: PrConfig | None = None) -> Discovery:
    """The template to use for *repo*, and a diagnosis when the choice needs one."""
    repo = repo.resolve()
    config = config or load_config(repo)
    if config.template:
        path = (repo / config.template).resolve()
        if not path.is_relative_to(repo):
            raise TemplateError(f"pr.template points outside the repository: {config.template}")
        if not path.is_file():
            raise TemplateError(f"pr.template names a file that does not exist: {config.template}")
        return Discovery(path, standard=False)
    for loc in LOCATIONS:
        base = repo / loc if loc else repo
        if not base.is_dir():
            continue
        single = _entry(base, TEMPLATE_NAME, want_dir=False)
        if single:
            return Discovery(single, standard=False)
        directory = _entry(base, TEMPLATE_DIR, want_dir=True)
        if directory is None:
            continue
        mds = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".md"),
                     key=lambda p: p.name)
        if len(mds) == 1:
            return Discovery(mds[0], standard=False)
        if len(mds) > 1:
            names = ", ".join(_rel(repo, p) for p in mds)
            return Discovery(STANDARD_TEMPLATE, standard=True, diagnosis=(
                "several templates, used the standard one; set pr.template in specs/config.yaml "
                f"to pick one of: {names}"))
    return Discovery(STANDARD_TEMPLATE, standard=True)


# --- parsing ----------------------------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CHECKBOX_RE = re.compile(r"^(\s*[-*+]\s+\[)([ xX])(\]\s.*|\])$")


@dataclass
class Block:
    """One run of a section's body: `comment`, `checkbox` (one line), `blank` or `text`."""

    kind: str
    text: str
    id: str = ""  # checkbox id (`cb1`, `cb2`, … in template order); "" for other kinds


@dataclass
class Section:
    level: int  # 1-6; 0 for the preamble before the first heading
    heading: str  # the heading line exactly as in the template; "" for the preamble
    title: str
    key: str  # unique slug of the title; "" for the preamble
    blocks: list[Block] = field(default_factory=list)
    subtree_end: int = 0  # index (exclusive) in the section list where this section's nested sections end

    @property
    def checkboxes(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "checkbox"]

    @property
    def has_text(self) -> bool:
        return any(b.kind == "text" for b in self.blocks)


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "section"


def normalize_title(title: str) -> str:
    """Lower case, letters, digits and single spaces only: `## Companion artifacts:` → `companion artifacts`."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", title.lower()).split())


def set_tick(line: str, ticked: bool) -> str:
    """A checkbox line with its box set; the rest of the line unchanged."""
    m = _CHECKBOX_RE.match(line)
    if not m:
        return line
    return f"{m.group(1)}{'x' if ticked else ' '}{m.group(3)}"


def is_checkbox(line: str) -> bool:
    return bool(_CHECKBOX_RE.match(line))


def parse(text: str) -> list[Section]:
    """Split *text* into sections; the first is always the preamble (possibly empty)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sections = [Section(level=0, heading="", title="", key="")]
    fence: str | None = None
    comment: list[str] | None = None
    text_run: list[str] = []
    checkbox_n = 0

    def flush_text() -> None:
        if text_run:
            sections[-1].blocks.append(Block("text", "\n".join(text_run)))
            text_run.clear()

    for line in lines:
        if comment is not None:
            comment.append(line)
            if "-->" in line:
                sections[-1].blocks.append(Block("comment", "\n".join(comment)))
                comment = None
            continue
        if fence is not None:
            text_run.append(line)
            m = _FENCE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip()[len(m.group(1)):]:
                fence = None
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            text_run.append(line)
            continue
        if line.lstrip().startswith("<!--"):
            flush_text()
            after = line.split("<!--", 1)[1]
            if "-->" in after:
                sections[-1].blocks.append(Block("comment", line))
            else:
                comment = [line]
            continue
        h = _HEADING_RE.match(line)
        if h:
            flush_text()
            title = (h.group(2) or "").strip()
            sections.append(Section(level=len(h.group(1)), heading=line, title=title, key=""))
            continue
        if is_checkbox(line):
            flush_text()
            checkbox_n += 1
            sections[-1].blocks.append(Block("checkbox", line, id=f"cb{checkbox_n}"))
            continue
        if not line.strip():
            flush_text()
            sections[-1].blocks.append(Block("blank", line))
            continue
        text_run.append(line)
    if comment is not None:  # an unclosed comment runs to the end, as in HTML
        sections[-1].blocks.append(Block("comment", "\n".join(comment)))
    flush_text()

    seen: dict[str, int] = {}
    for s in sections[1:]:
        base = slug(s.title)
        seen[base] = seen.get(base, 0) + 1
        s.key = base if seen[base] == 1 else f"{base}-{seen[base]}"
    for i, s in enumerate(sections):
        if s.level == 0:
            s.subtree_end = i + 1
            continue
        end = i + 1
        while end < len(sections) and sections[end].level > s.level:
            end += 1
        s.subtree_end = end
    return sections


def load(path: Path) -> list[Section]:
    try:
        return parse(path.read_text())
    except (OSError, UnicodeDecodeError) as e:
        raise TemplateError(f"cannot read the PR template {path}: {e}") from e
