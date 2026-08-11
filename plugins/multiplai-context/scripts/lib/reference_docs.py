"""Mechanical injection of the user's dev reference docs.

`$CLAUDE_CONFIG_DIR/reference/dev/` holds prescriptive engineering standards
(uv/Python, Django/DRF, React/Next.js, Swift, …). Those docs are NOT memory:
memory is context *about* the user and is routed by relevance to the prompt,
while a standards doc applies because of what the project *is*, not because of
how the prompt was phrased. Routing them through the memory router would make
adherence depend on wording — the failure this module exists to remove.

So detection is deterministic and manifest-driven: find the project, read its
manifests, map the stack to doc filenames, and inject a short pointer block
naming the files and their section index. Pointers, not contents — the Django
doc alone is 60k chars and would crowd out the actual conversation. The main
agent holds `Read` and can pull what it needs; it only has to know the doc
exists and what is in it.

Announced once per (session, project) rather than every turn: it is a fact
about the project, not an answer to the prompt, so repeating it each turn buys
nothing. The re-announce window covers compaction, which drops the block out
of context without touching this state.

Degrades to injecting nothing — with no error — when `reference/dev/` does not
exist. That is the vanilla-Claude-Code case (the docs ship with multiplai-kit),
and the degradation contract requires the plugin work without it.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

# Manifest filename → stack key. The key vocabulary is deliberately the same
# one buildme's `_DEFAULT_REFERENCE_DOCS` uses (multiplai-dev's
# build_pipeline/config.py), so the two mechanisms name the same docs for the
# same project. Keep them in step — see reference/dev/README.md, which states
# the renaming contract for the docs themselves.
_MANIFEST_KEYS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "pyproject"),
    ("package.json", "package"),
    ("Package.swift", "Package"),
    ("Cargo.toml", "Cargo"),
    ("go.mod", "go"),
    ("requirements.txt", "pyproject"),
)

# Stack/framework key → doc filenames under reference/dev/. A name with no
# file on disk is skipped (the map may name a doc that has not been written).
STACK_DOCS: dict[str, list[str]] = {
    "pyproject": ["uv-python-best-practices.md", "python-project-structure.md"],
    "package": ["bun-vite-react-best-practices.md"],
    "Package": ["swift-best-practices.md", "swift-testing-strategies.md"],
    "Cargo": [],
    "go": [],
    "django": ["django-drf-best-practices.md"],
    "react": ["react-nextjs-best-practices.md"],
    "fastapi": ["fastapi-best-practices.md"],
}

# Turns before a project's block is announced again. Sized to outlive a
# compaction (which silently removes the earlier injection) without
# re-announcing inside one working stretch.
REANNOUNCE_AFTER_TURNS = 30

# Section headers listed per doc. The index is a navigation aid, not the
# table of contents — a 40-header dump would defeat the point of pointers.
MAX_SECTIONS_LISTED = 14

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Path-ish tokens in a prompt: at least one separator, no whitespace. Used to
# find the project the user is pointing at when cwd is a workspace root that
# holds many projects and carries no manifest of its own.
_PATH_TOKEN_RE = re.compile(r"[\w.@~-]+(?:/[\w.@-]+)+/?")

_STATE_KEY = "dev_references"


def reference_dir() -> Path:
    """`$CLAUDE_CONFIG_DIR/reference/dev`, expanded. May not exist."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return base / "reference" / "dev"


def _requirement_name(spec: str) -> str:
    """The bare package name from a requirement line, lowercased.

    `Django>=5.0`, `django[argon2]==5.0`, `  django  # c` → `django`. Blanks,
    comments and flag lines (`-r other.txt`) yield "", which never equals a
    name being looked for.
    """
    text = spec.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return ""
    name = re.split(r"[\[\s<>=!~;,]", text, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _python_requirements(project_dir: Path) -> set[str]:
    """Declared Python dependency names from pyproject.toml + requirements.txt.

    Every parse failure degrades to "no dependencies seen" for that file: a
    malformed manifest must cost a framework hint, never the prompt.
    """
    names: set[str] = set()
    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = {}
        specs: list[str] = []
        project = data.get("project")
        if isinstance(project, dict):
            deps = project.get("dependencies")
            if isinstance(deps, list):
                specs.extend(d for d in deps if isinstance(d, str))
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    if isinstance(group, list):
                        specs.extend(d for d in group if isinstance(d, str))
        tool = data.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, dict) else None
        if isinstance(poetry, dict):
            for section in ("dependencies", "dev-dependencies"):
                group = poetry.get(section)
                if isinstance(group, dict):
                    specs.extend(str(name) for name in group)
        names.update(filter(None, (_requirement_name(s) for s in specs)))

    reqs = project_dir / "requirements.txt"
    if reqs.is_file():
        try:
            lines = reqs.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        names.update(filter(None, (_requirement_name(line) for line in lines)))
    return names


def _node_dependencies(project_dir: Path) -> set[str]:
    """Declared dependency names from package.json, all three sections."""
    manifest = project_dir / "package.json"
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(str(k).lower() for k in section)
    return names


def detect_stack_keys(project_dir: Path) -> list[str]:
    """Stack and framework keys visible in `project_dir`'s manifests.

    Manifest keys come from filenames; framework keys have to be read out of
    the dependency lists, because a Django app and a plain library are both
    `pyproject`. Order is manifests first, then frameworks, each deduped —
    it drives the order docs are listed in.
    """
    keys: list[str] = []
    for filename, key in _MANIFEST_KEYS:
        if (project_dir / filename).is_file() and key not in keys:
            keys.append(key)

    python_deps = _python_requirements(project_dir)
    if (project_dir / "manage.py").is_file() or "django" in python_deps:
        keys.append("django")
    if "fastapi" in python_deps:
        keys.append("fastapi")

    node_deps = _node_dependencies(project_dir)
    if "react" in node_deps or "next" in node_deps:
        keys.append("react")
    return keys


def _has_manifest(directory: Path) -> bool:
    return any((directory / filename).is_file() for filename, _ in _MANIFEST_KEYS)


def find_project_dir(cwd: str | Path) -> Path | None:
    """Nearest ancestor of `cwd` (inclusive) carrying a manifest, or None.

    Stops at `$HOME` and at the filesystem root so a stray manifest in a home
    directory cannot claim every session.
    """
    try:
        start = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not start.is_dir():
        start = start.parent
    home = Path.home().resolve()
    for candidate in (start, *start.parents):
        if _has_manifest(candidate):
            return candidate
        if candidate == home:
            break
    return None


def projects_from_prompt(prompt: str, cwd: str | Path, limit: int = 2) -> list[Path]:
    """Project dirs the prompt points at, for the many-projects-in-one-cwd case.

    A workspace root holding `PROJECTS/<name>/…` carries no manifest of its
    own, so `find_project_dir(cwd)` finds nothing there and the standards for
    the project actually being worked on would never load. Path-ish tokens in
    the prompt are the available signal: resolve each against cwd and walk up
    to its manifest.

    Untrusted-content note: prompt text is only ever used to *resolve a path
    that must already exist on disk*, never executed and never read from. A
    token naming nothing is dropped.
    """
    if not prompt:
        return []
    try:
        base = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    found: list[Path] = []
    for token in _PATH_TOKEN_RE.findall(prompt)[:40]:
        token = token.rstrip("/:,.")
        if not token:
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        project = find_project_dir(candidate)
        if project is not None and project != base and project not in found:
            found.append(project)
            if len(found) >= limit:
                break
    return found


def doc_names_for(keys: list[str]) -> list[str]:
    """Doc filenames for `keys`, in key order, deduped."""
    names: list[str] = []
    for key in keys:
        for name in STACK_DOCS.get(key, []):
            if name not in names:
                names.append(name)
    return names


def section_index(text: str, limit: int = MAX_SECTIONS_LISTED) -> list[str]:
    """H2 headers of a doc, truncated to `limit` with a trailing count marker."""
    headers = [m.group(1).strip() for m in _H2_RE.finditer(text)]
    if len(headers) <= limit:
        return headers
    remaining = len(headers) - limit
    return [*headers[:limit], f"(+{remaining} more)"]


def resolve_docs(names: list[str]) -> list[tuple[Path, list[str]]]:
    """Existing docs among `names`, each with its section index.

    A named doc with no file is skipped silently here — the caller logs the
    gap, because "the map names a doc nobody wrote" and "the kit is not
    installed" want different messages.
    """
    ref_dir = reference_dir()
    resolved: list[tuple[Path, list[str]]] = []
    for name in names:
        path = ref_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        resolved.append((path, section_index(text)))
    return resolved


def build_block(project_dir: Path, docs: list[tuple[Path, list[str]]]) -> str:
    """The injected DEV REFERENCES section, or "" when there is nothing to say.

    Names paths and sections only. The instruction is scoped to *writing or
    changing code* so it does not fire on a question about the project.
    """
    if not docs:
        return ""
    lines = [
        "=== DEV REFERENCES ===",
        "",
        f"Engineering standards that apply to {project_dir.name} because of its "
        "stack. These are prescriptive (unlike memory, which is context and can "
        "be stale): before writing or changing code in this project, Read the "
        "relevant doc — at least the sections your change touches. Cite the doc "
        "when a standard decides a design choice, and say so explicitly if you "
        "are departing from one.",
        "",
    ]
    for path, sections in docs:
        lines.append(f"- {path}")
        if sections:
            lines.append(f"  Sections: {' · '.join(sections)}")
    return "\n".join(lines)


def should_announce(
    state: dict,
    project_dir: Path,
    turn_index: int,
) -> bool:
    """Whether this project's block is due.

    Due when never announced this session, or when the recorded turn is more
    than `REANNOUNCE_AFTER_TURNS` behind. `turn_index` of 0 means the caller
    has no turn counter (cooldown disabled), which reads as once-per-session.
    """
    announced = state.get(_STATE_KEY)
    if not isinstance(announced, dict):
        return True
    previous = announced.get(str(project_dir))
    if not isinstance(previous, int):
        return True
    if turn_index <= 0:
        return False
    return (turn_index - previous) > REANNOUNCE_AFTER_TURNS


def record_announced(state: dict, project_dir: Path, turn_index: int) -> None:
    """Mark `project_dir` announced at `turn_index`, mutating `state` in place."""
    announced = state.get(_STATE_KEY)
    if not isinstance(announced, dict):
        announced = {}
    announced[str(project_dir)] = max(0, turn_index)
    state[_STATE_KEY] = announced


def clear_announcements(state: dict) -> bool:
    """Drop the announcement map from `state`; True when something was dropped.

    For pre_compact's post-compaction reset (P13): compaction removes the
    injected DEV REFERENCES block from the context, and a surviving
    announcement entry — with `should_announce`'s once-per-session reading
    of `turn_index <= 0` — would suppress re-announcement for the rest of
    the session. Dropping the key makes every project "never announced"
    again, which is the truth of the fresh window.
    """
    return state.pop(_STATE_KEY, None) is not None
