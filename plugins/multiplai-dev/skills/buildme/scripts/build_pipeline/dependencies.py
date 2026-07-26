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
   minting an explainer. Applied only when the manifests leave Python
   plausible (pyproject/requirements present, or no manifest at all): in a
   pure JS/Rust project `secrets` or `queue` are real package names;
2. language, runtime, and format words that name no dependency at all
   (``python``, ``npm``, ``git``, ``yaml``, …);
3. tokens that are plainly not package names — file paths, filenames with a
   code/data extension, shell-looking strings;
4. the project's own top-level modules (a design doc naming `build_pipeline`
   is referring to the project, not to a new dependency);
5. everything the project already imports — modules AND imported symbols. A
   design doc naming `BaseModel` or `log_utils` is naming code the project
   already uses, not something it is about to take on;
6. names the sentence explicitly rejects ("use `tomllib` rather than adding
   `tomli`") — a dependency we decided against is not one we depend on;
7. everything already declared in the project's manifests — ``pyproject.toml``,
   ``requirements*.txt``, ``package.json``, ``Package.swift``, ``Cargo.toml``,
   ``go.mod``. "New" means new *to this project*, so a library the project
   already ships with is not an unknown.

A returned :class:`NewDependency` carries the name, where it was mentioned,
and the manifest evidence that it is absent — the gate and the explainer
prompt both read those fields.
"""

from __future__ import annotations

import builtins
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

# "use X rather than Y" / "instead of Y" — Y is a rejected alternative, not a
# dependency. The cue must directly GOVERN the token: only a phrase within a
# few glue words immediately before it counts, and another backticked token in
# between re-binds the cue to that token instead. So "Instead of `pandas`, we
# use `polars`." rejects pandas without also swallowing the adopted polars.
_NEGATION_RE = re.compile(
    r"(rather than|instead of|not adding|no need for|avoid(?:ing)?|"
    r"decided against|ruled out|as opposed to)\b",
    re.IGNORECASE,
)
_NEGATION_WINDOW = 40  # chars before the token examined for a governing cue
_NEGATION_MAX_GLUE_WORDS = 2  # e.g. "rather than adding `tomli`"


def _negated(line: str, token_start: int) -> bool:
    """Whether a negation cue directly governs the token at ``token_start``."""
    window = line[max(0, token_start - _NEGATION_WINDOW):token_start]
    cue = None
    for cue in _NEGATION_RE.finditer(window):
        pass  # keep the cue closest to the token
    if cue is None:
        return False
    between = window[cue.end():]
    # A backticked token between the cue and this one means the cue governs
    # THAT token, not this one.
    if "`" in between:
        return False
    return len(between.split()) <= _NEGATION_MAX_GLUE_WORDS


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

# Builtins are not packages. `sys.stdlib_module_names` only knows *top-level
# module* names, so `open`, `str` and `NotADirectoryError` sailed straight
# through it and each minted an explainer on the first real run.
_BUILTIN_NAMES = frozenset(
    n.lower().replace("_", "-") for n in dir(builtins) if not n.startswith("__")
)

# Public attribute names of the stdlib modules that real design prose actually
# name-drops. The same gap as above one level down: `scandir`, `DirEntry` and
# `entry.stat` are `os` members, not distributions.
#
# Introspected rather than hand-listed so it cannot drift from the interpreter,
# and confined to a fixed tuple of import-safe modules — importing arbitrary
# stdlib modules to build a denylist would be a side effect nobody asked a
# dependency scanner for.
_STDLIB_MEMBER_MODULES = (
    "builtins", "os", "os.path", "sys", "io", "re", "json", "pathlib", "shutil",
    "subprocess", "itertools", "functools", "collections", "datetime", "time",
    "math", "string", "typing", "dataclasses", "enum", "logging", "contextlib",
    "tempfile", "textwrap", "argparse", "hashlib", "base64", "uuid", "random",
    "csv", "glob", "stat", "socket", "struct", "threading", "asyncio",
)


def _stdlib_member_names() -> frozenset[str]:
    import importlib
    import pathlib

    # Method names of the types prose describes operations on: `ljust`,
    # `rglob`, `iterdir` are methods, so they are absent from `dir(module)`.
    types_ = (str, bytes, list, dict, set, tuple, pathlib.Path, pathlib.PurePath)

    names: set[str] = set()
    targets: list[object] = []
    for mod_name in _STDLIB_MEMBER_MODULES:
        try:
            targets.append(importlib.import_module(mod_name))
        except ImportError as exc:  # pragma: no cover - defensive
            log.debug("Skipping stdlib member scan for %s: %s", mod_name, exc)
    targets.extend(types_)
    for target in targets:
        for attr in dir(target):
            if attr.startswith("_"):
                continue
            names.add(attr.lower().replace("_", "-"))
    return frozenset(names)


_STDLIB_MEMBERS = _stdlib_member_names()

# `st_mtime`, `st_size`, `st_mode` — `os.stat_result` field names. They are
# attributes of a C struct, so `dir(os)` does not list them.
_STRUCT_FIELD_RE = re.compile(r"^st-[a-z]+$")

# A verb phrase harvested out of prose reads exactly like a hyphenated package
# name: "add a section" → `add-section`, "scan directory" → `scan-directory`.
# Deliberately a SMALL imperative list — a long one would start rejecting real
# packages whose names legitimately open with a verb (`build-essential`,
# `parse-torrent`), so this is scoped to the verbs that show up in design prose
# describing what the code will do.
_PROSE_VERBS = frozenset({
    "add", "scan", "get", "set", "run", "make", "build", "parse", "write",
    "read", "create", "delete", "update", "fetch", "load", "save", "render",
    "compute", "collect", "emit", "handle", "check", "detect", "resolve",
    "skip", "log", "return", "raise", "keep", "drop", "reject", "apply",
    "route", "wrap", "surface", "record", "gate", "suppress",
})

# Filenames/paths, not packages.
_FILE_EXT_RE = re.compile(
    r"\.(py|pyi|js|mjs|cjs|jsx|ts|tsx|swift|rs|go|java|kt|rb|sh|bash|zsh|"
    r"md|markdown|txt|json|ya?ml|toml|ini|cfg|conf|lock|mod|sum|csv|tsv|xml|"
    r"html|css|scss|sql|env|log|png|jpg|svg|pdf)$",
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


def _collapse(canonical_name: str) -> str:
    """``pkg.sub.thing`` → ``pkg``. A distribution is the head, not the path.

    Without this, `rich`, `rich.print`, `rich.console.console` and
    `rich.table.table` were four separate candidates and `rich` was explained
    four times over; and member accesses like `entry.stat` or `str.ljust`
    survived because their head never got tested against the stdlib.

    npm scopes (``@scope/pkg``) and Go module paths keep their slash form —
    those really are the distribution name — so only dots are collapsed.
    """
    if "." not in canonical_name:
        return canonical_name
    # Never collapse a filename — `package.json` must stay `package.json` so
    # the extension rule can reject it, not become a bogus `package` candidate.
    if _FILE_EXT_RE.search(canonical_name):
        return canonical_name
    return canonical_name.partition(".")[0]


def _is_plausible_dependency(canonical_name: str, *, python_ecosystem: bool = True) -> bool:
    if len(canonical_name) < 2:
        return False
    if not _NAME_RE.match(canonical_name):
        return False
    if _FILE_EXT_RE.search(canonical_name):
        return False
    if canonical_name in _GENERIC_TERMS:
        return False
    # In Python land every dotted token was collapsed to its head before this
    # point, so a surviving dot means a filename-shaped token we refused to
    # collapse. Elsewhere dots are legitimate (`lodash.debounce`).
    if python_ecosystem and "." in canonical_name:
        return False
    if _STRUCT_FIELD_RE.match(canonical_name):
        return False
    head_word = canonical_name.split("-", 1)[0]
    if "-" in canonical_name and head_word in _PROSE_VERBS:
        return False
    # Builtins and stdlib members: the two levels `sys.stdlib_module_names`
    # cannot see. Gated on the Python ecosystem for the same reason the stdlib
    # check below is — `open` and `register` could be real npm packages.
    if python_ecosystem:
        if canonical_name in _BUILTIN_NAMES:
            return False
        if canonical_name in _STDLIB_MEMBERS:
            return False
    # Stdlib is never "new" — this is the gate that stops an explainer firing
    # on every `pathlib` / `asyncio` / `json` mention in a real change. It
    # only applies where Python is plausible: `secrets` or `queue` are real
    # npm/cargo package names, so a pure JS/Rust project must not have its
    # dependencies swallowed by Python's stdlib list.
    if python_ecosystem:
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

# Manifests that make the project a Python one — the condition for applying
# the stdlib subtraction in _is_plausible_dependency. A project with no
# manifest at all keeps the subtraction (it may yet become Python, and the
# stdlib filter is the conservative default).
_PYTHON_MANIFESTS = frozenset({
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
})


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
        last = last.removesuffix(".git")
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


# --- Existing imports ---------------------------------------------------------

_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".swift", ".rs", ".go", ".java", ".kt",
})
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", ".build",
    "vendor", "site-packages", ".worktrees",
})
_MAX_SOURCE_FILES = 3000

_IMPORT_RES = (
    # Python: `import a.b`, `from a.b import X, Y`
    re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE),
    re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", re.MULTILINE),
    # JS/TS: `from 'pkg'`, `require('pkg')`, `import 'pkg'`
    re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]"""),
    # Swift: `import Foo`
    re.compile(r"^\s*import\s+([A-Za-z_]\w*)", re.MULTILINE),
    # Rust: `use foo::bar`
    re.compile(r"^\s*use\s+(\w+)", re.MULTILINE),
)


def existing_import_names(project_dir: Path) -> set[str]:
    """Module and symbol names the project's own source already imports.

    A design doc naming `BaseModel` or `log_utils` is naming code already in
    use — not a dependency this change newly takes on. Scanning the source is
    the cheapest precise signal for that, and it catches submodules and
    imported symbols no manifest lists.
    """
    names: set[str] = set()
    if not project_dir.is_dir():
        return names

    scanned = 0
    stack = [project_dir]
    while stack and scanned < _MAX_SOURCE_FILES:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
                continue
            if entry.suffix not in _SOURCE_SUFFIXES:
                continue
            scanned += 1
            if scanned > _MAX_SOURCE_FILES:
                break
            try:
                text = entry.read_text(errors="ignore")
            except OSError:
                continue
            for pattern in _IMPORT_RES:
                for match in pattern.finditer(text):
                    for group in match.groups():
                        if not group:
                            continue
                        for piece in re.split(r"[,\s]+", group):
                            piece = piece.strip("()").strip()
                            if not piece or piece in ("as", "import"):
                                continue
                            # The dotted path AND each of its segments:
                            # `multiplai_core.log_utils` also registers
                            # `multiplai_core` and `log_utils`.
                            for part in [piece, *re.split(r"[./]", piece)]:
                                canonical_part = _canonical(part)
                                if canonical_part:
                                    names.add(canonical_part)
    if scanned >= _MAX_SOURCE_FILES:
        log.info(
            "Import scan stopped at the %d-file cap in %s",
            _MAX_SOURCE_FILES, project_dir,
        )
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
    in_use = project_module_names(project_dir) | existing_import_names(project_dir)
    python_ecosystem = not manifests or any(m in _PYTHON_MANIFESTS for m in manifests)

    mentions: dict[str, list[str]] = {}
    for label, section_text in sources:
        for line in section_text.splitlines():
            for match in _BACKTICK_RE.finditer(line):
                raw = match.group(1)
                canonical_name = _canonical(raw)
                # Only in Python land: `lodash.debounce` really is an npm
                # distribution, so collapsing dots there would rename it.
                if python_ecosystem:
                    canonical_name = _collapse(canonical_name)
                if not _is_plausible_dependency(
                    canonical_name, python_ecosystem=python_ecosystem,
                ):
                    continue
                if _aliases(canonical_name) & declared:
                    continue
                if _aliases(canonical_name) & in_use:
                    continue
                # "use `tomllib` rather than adding `tomli`" — the rejected
                # alternative is not something we depend on.
                if _negated(line, match.start()):
                    log.debug(
                        "Skipping '%s' — rejected alternative in: %s",
                        canonical_name, line.strip()[:120],
                    )
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


def design_decisions_text(change_dir: Path) -> str:
    """The design's ``## Decisions`` section body ("" when absent).

    Feeds the explainer's usage_context slot — how this project intends to use
    the dependency, so the explainer researches the relevant edge cases.
    """
    return _section(_read(change_dir / "design.md"), _DECISIONS_RE).strip()


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""
