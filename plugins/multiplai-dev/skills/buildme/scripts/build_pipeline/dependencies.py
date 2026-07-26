"""Detect dependencies that are NEW TO THIS PROJECT.

Pure code, no LLM calls. Feeds the unknowns/explainer gate (B1): before the
build depends on a tool, library, service, or data source it has never used,
buildme writes an explainer covering the contract and the edge cases (the
Whisper-silence class: what does it do on empty / malformed / oversized /
concurrent / offline input?).

The signal comes from two spec sections the generator already produces:

    proposal.md  → ``## Impact``    ("Affected code, APIs, dependencies, systems")
    design.md    → ``## Decisions`` (the chosen libraries and services)

Only **backtick-quoted tokens** inside those sections count as candidates.
Prose names are deliberately ignored: an explainer per capitalized word would
fire on every sentence, and the spec templates already tell the generator to
write dependency names in backticks.

Candidates are then subtracted against, in order:

1. the Python standard library (``sys.stdlib_module_names``) — this is the
   precision gate that keeps a `json` / `pathlib` / `asyncio` mention from
   minting an explainer;
2. language, runtime, and format words that name no dependency at all
   (``python``, ``npm``, ``git``, ``yaml``, …);
3. tokens that are plainly not package names — file paths, filenames with a
   code/data extension, shell-looking strings;
4. the project's own top-level modules (a design doc naming `build_pipeline`
   is referring to the project, not to a new dependency);
5. everything already declared in the project's manifests — ``pyproject.toml``,
   ``requirements*.txt``, ``package.json``, ``Package.swift``, ``Cargo.toml``,
   ``go.mod``. "New" means new *to this project*, so a library the project
   already ships with is not an unknown.

A returned :class:`NewDependency` carries the name, where it was mentioned,
and the manifest evidence that it is absent — the gate and the explainer
prompt both read those fields.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class NewDependency(BaseModel):
    """A tool/library/service named in the specs but absent from the manifests."""

    name: str
    # Human-readable mention sites, e.g. "proposal.md § Impact".
    mentioned_in: list[str] = Field(default_factory=list)
    # Why we believe it is new: which manifests were scanned and what they hold.
    evidence: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({', '.join(self.mentioned_in) or 'unknown site'})"


# --- Section extraction -------------------------------------------------------

# The spec templates write these as level-2 headings; match to the next
# level-2 heading (or EOF).
_IMPACT_RE = re.compile(
    r"^##\s+Impact\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
)
_DECISIONS_RE = re.compile(
    r"^##\s+Decisions\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
)
_BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")


def _section(text: str, pattern: re.Pattern[str]) -> str:
    m = pattern.search(text or "")
    return m.group(1) if m else ""


# --- Candidate filtering ------------------------------------------------------

# Words that name a language, runtime, packaging tool, protocol, or file format
# rather than a dependency this change would newly take on.
_GENERIC_TERMS = frozenset({
    "python", "python3", "py", "node", "nodejs", "deno", "bun", "npm", "npx",
    "pnpm", "yarn", "pip", "pipx", "uv", "poetry", "pdm", "hatch", "hatchling",
    "setuptools", "wheel", "conda", "brew", "apt", "make", "cmake", "cargo",
    "rust", "rustc", "go", "golang", "swift", "swiftpm", "spm", "xcode",
    "java", "kotlin", "javascript", "typescript", "js", "ts", "tsx", "jsx",
    "html", "css", "scss", "sql", "bash", "sh", "zsh", "shell", "git", "gh",
    "github", "gitlab", "docker", "dockerfile", "compose", "kubernetes", "k8s",
    "json", "yaml", "yml", "toml", "ini", "csv", "tsv", "xml", "markdown", "md",
    "http", "https", "rest", "grpc", "graphql", "api", "cli", "ui", "ux",
    "tdd", "bdd", "ci", "cd", "utf-8", "utf8", "ascii", "regex", "env",
    "readme", "license", "main", "src", "tests", "test", "lib", "bin", "docs",
    "true", "false", "none", "null", "todo", "tbd", "n/a", "na",
})

# A dependency name looks like a package identifier: letters/digits with
# ``.-_`` separators, optionally npm-scoped (``@scope/name``).
_NAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)?$")

# Filenames/paths, not packages.
_FILE_EXT_RE = re.compile(
    r"\.(py|pyi|js|mjs|cjs|jsx|ts|tsx|swift|rs|go|java|kt|rb|sh|bash|zsh|"
    r"md|markdown|txt|json|ya?ml|toml|ini|cfg|conf|lock|csv|tsv|xml|html|"
    r"css|scss|sql|env|log|png|jpg|svg|pdf)$",
    re.IGNORECASE,
)


def _canonical(token: str) -> str:
    """Normalize a package token for comparison.

    Lowercases, drops any version specifier / extras / trailing punctuation,
    and folds ``_`` to ``-`` (PEP 503 style), so ``Pillow>=10``, ``pillow``
    and ``pil low``-free variants all compare equal.
    """
    t = token.strip().strip(".,;:!?()[]{}\"'")
    # Cut at the first version-specifier or extras marker.
    t = re.split(r"[<>=!~\[\s]", t, maxsplit=1)[0]
    # npm ``pkg@1.2.3`` (but keep a leading @scope).
    if "@" in t[1:]:
        head, _, _tail = t[1:].partition("@")
        t = t[0] + head
    return t.strip().lower().replace("_", "-")


def _aliases(canonical_name: str) -> set[str]:
    """Names a declared dependency may be referred to by.

    Go module paths (``github.com/x/y``), npm scopes (``@scope/pkg``) and
    Swift package URLs are commonly cited by their last segment.
    """
    out = {canonical_name}
    if "/" in canonical_name:
        out.add(canonical_name.rsplit("/", 1)[-1])
    return {a for a in out if a}


def _is_plausible_dependency(token: str, canonical_name: str) -> bool:
    if len(canonical_name) < 2:
        return False
    if not _NAME_RE.match(canonical_name):
        return False
    if _FILE_EXT_RE.search(canonical_name):
        return False
    if canonical_name in _GENERIC_TERMS:
        return False
    # Stdlib is never "new" — this is the gate that stops an explainer firing
    # on every `pathlib` / `asyncio` / `json` mention in a real change.
    module_head = canonical_name.replace("-", "_").split(".")[0]
    if module_head in sys.stdlib_module_names:
        return False
    if canonical_name.replace("-", "_") in sys.stdlib_module_names:
        return False
    return True


# --- Manifest parsing ---------------------------------------------------------

_MANIFESTS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "Package.swift",
    "Cargo.toml",
    "go.mod",
)


def _declared_in_pyproject(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("Could not parse %s: %s", path, e)
        return names

    project = data.get("project", {}) or {}
    for dep in project.get("dependencies", []) or []:
        names.add(_canonical(str(dep)))
    for group in (project.get("optional-dependencies", {}) or {}).values():
        for dep in group or []:
            names.add(_canonical(str(dep)))
    for group in (data.get("dependency-groups", {}) or {}).values():
        for dep in group or []:
            if isinstance(dep, str):
                names.add(_canonical(dep))

    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {})
    for key in ("dependencies", "dev-dependencies", "group"):
        section = poetry.get(key, {}) or {}
        if key == "group":
            for grp in section.values():
                for name in (grp or {}).get("dependencies", {}) or {}:
                    names.add(_canonical(name))
        else:
            for name in section:
                names.add(_canonical(name))

    uv = ((data.get("tool", {}) or {}).get("uv", {}) or {})
    for dep in uv.get("dev-dependencies", []) or []:
        names.add(_canonical(str(dep)))
    return names


def _declared_in_requirements(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        names.add(_canonical(line))
    return names


def _declared_in_package_json(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not parse %s: %s", path, e)
        return names
    for key in (
        "dependencies", "devDependencies",
        "peerDependencies", "optionalDependencies",
    ):
        for name in (data.get(key, {}) or {}):
            names.add(_canonical(name))
    return names


_SWIFT_PKG_URL_RE = re.compile(r'url:\s*"([^"]+)"')
_SWIFT_PKG_NAME_RE = re.compile(r'\.package\(\s*name:\s*"([^"]+)"')
_SWIFT_PRODUCT_RE = re.compile(r'\.product\(\s*name:\s*"([^"]+)"')


def _declared_in_package_swift(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        text = path.read_text()
    except OSError:
        return names
    for url in _SWIFT_PKG_URL_RE.findall(text):
        last = url.rstrip("/").rsplit("/", 1)[-1]
        if last.endswith(".git"):
            last = last[: -len(".git")]
        names.add(_canonical(last))
    for name in _SWIFT_PKG_NAME_RE.findall(text):
        names.add(_canonical(name))
    for name in _SWIFT_PRODUCT_RE.findall(text):
        names.add(_canonical(name))
    return names


def _declared_in_cargo(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("Could not parse %s: %s", path, e)
        return names
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name in (data.get(key, {}) or {}):
            names.add(_canonical(name))
    return names


_GO_REQUIRE_LINE_RE = re.compile(r"^\s*(?:require\s+)?([\w.\-]+(?:/[\w.\-]+)+)\s+v", re.MULTILINE)


def _declared_in_go_mod(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        text = path.read_text()
    except OSError:
        return names
    for module in _GO_REQUIRE_LINE_RE.findall(text):
        names |= _aliases(_canonical(module))
    return names


_PARSERS = {
    "pyproject.toml": _declared_in_pyproject,
    "requirements.txt": _declared_in_requirements,
    "requirements-dev.txt": _declared_in_requirements,
    "package.json": _declared_in_package_json,
    "Package.swift": _declared_in_package_swift,
    "Cargo.toml": _declared_in_cargo,
    "go.mod": _declared_in_go_mod,
}


def declared_dependencies(project_dir: Path) -> tuple[set[str], list[str]]:
    """All dependency names declared by the project's manifests.

    Returns ``(canonical names incl. aliases, manifest filenames found)``.
    """
    declared: set[str] = set()
    found: list[str] = []
    for manifest in _MANIFESTS:
        path = project_dir / manifest
        if not path.is_file():
            continue
        found.append(manifest)
        for name in _PARSERS[manifest](path):
            declared |= _aliases(name)
    return declared, found


def project_module_names(project_dir: Path) -> set[str]:
    """Top-level module/package names the project itself owns.

    A design doc naming its own module is not declaring a new dependency.
    """
    names: set[str] = set()
    roots = [project_dir, project_dir / "src", project_dir / "lib"]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                names.add(_canonical(entry.name))
            elif entry.suffix in (".py", ".ts", ".js", ".swift", ".rs", ".go"):
                names.add(_canonical(entry.stem))
    return names


# --- Public API ---------------------------------------------------------------

def detect_new_dependencies(
    change_dir: Path, project_dir: Path
) -> list[NewDependency]:
    """Tools/libraries/services named in the specs but absent from the manifests.

    Reads ``proposal.md``'s ``## Impact`` and ``design.md``'s ``## Decisions``,
    collects backtick-quoted tokens, and subtracts the stdlib, generic
    language/format words, the project's own modules, and every manifest-declared
    dependency. Returns one entry per surviving name, sorted, each carrying its
    mention sites and the manifest evidence of absence.
    """
    proposal = _read(change_dir / "proposal.md")
    design = _read(change_dir / "design.md")

    sources = [
        ("proposal.md § Impact", _section(proposal, _IMPACT_RE)),
        ("design.md § Decisions", _section(design, _DECISIONS_RE)),
    ]

    declared, manifests = declared_dependencies(project_dir)
    own_modules = project_module_names(project_dir)

    mentions: dict[str, list[str]] = {}
    for label, section_text in sources:
        for raw in _BACKTICK_RE.findall(section_text):
            canonical_name = _canonical(raw)
            if not _is_plausible_dependency(raw, canonical_name):
                continue
            if _aliases(canonical_name) & declared:
                continue
            if _aliases(canonical_name) & own_modules:
                continue
            sites = mentions.setdefault(canonical_name, [])
            if label not in sites:
                sites.append(label)

    if manifests:
        evidence_suffix = (
            f"not declared in {', '.join(manifests)} "
            f"({len(declared)} declared name(s) scanned)"
        )
    else:
        evidence_suffix = (
            "no dependency manifest found in the project "
            "(pyproject.toml / package.json / Package.swift / Cargo.toml / go.mod)"
        )

    result = [
        NewDependency(
            name=name,
            mentioned_in=sites,
            evidence=evidence_suffix,
        )
        for name, sites in sorted(mentions.items())
    ]
    log.info(
        "Dependency scan: %d new to this project (%s)",
        len(result),
        ", ".join(d.name for d in result) or "none",
    )
    return result


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""
