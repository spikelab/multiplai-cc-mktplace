"""PyIndex: one static `ast` pass over the Python files of a snapshot.

Nothing in the snapshot is imported or run. The index records, per `.py` file:
its repository root, its dotted module name, its `def`/`class` spans, the
names its imports and definitions bind in each scope, and the imports
themselves. It also lists every walked file of any type, so scanners of
`pyproject.toml` and the like share the walk's exclusions. Paths are POSIX and
relative to the snapshot root; the snapshot root itself is `""`.

Repository root. The nearest ancestor directory, at or below the snapshot
root, that holds `.git` or `pyproject.toml`; the snapshot root otherwise.

Imports. Collected at every nesting level (function bodies, `try` blocks,
methods) except inside `if TYPE_CHECKING:`, whose imports never run. A
function-local import binds only inside that function. Name and module
resolution never leaves the importing file's repository: absolute imports are
searched under `<repo>/`, then `<repo>/src/`, then the importing file's own
directory.

Import graph. Every collected import is an edge. Absolute imports the
repository lacks fall back to the other repository roots, and their `src/`, in
sorted order, so a skill reaches a library it installs as a path dependency.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import posixpath
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .paths import Handler

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages", "build", "dist"})
REPO_MARKERS = (".git", "pyproject.toml")


@dataclass(frozen=True)
class Def:
    """A `def` or `class` statement: `qualname` dots the enclosing definitions' names."""
    name: str
    qualname: str
    start: int
    end: int
    is_class: bool


# What a name is bound to in a scope.
@dataclass(frozen=True)
class _DefBinding:
    name: str
    line: int


@dataclass(frozen=True)
class _ModuleBinding:
    """`import a.b` (binds `a` to module `a`) or `import a.b as c` (binds `c` to `a.b`)."""
    module: str


@dataclass(frozen=True)
class _FromBinding:
    """`from <level dots><module> import <attr> [as <name>]`."""
    module: str
    level: int
    attr: str


_Binding = _DefBinding | _ModuleBinding | _FromBinding


@dataclass(frozen=True)
class _Import:
    """One import statement's target: `names` is empty for `import <module>`."""
    module: str
    level: int
    names: tuple[str, ...]

    def dotted_targets(self) -> list[str]:
        """The dotted modules the statement imports, relative to its `level`.

        `import a.b` also imports `a`; `from x import y` also names `x.y`, which counts when it is a module.
        """
        if self.names:
            return [self.module] + [f"{self.module}.{n}" if self.module else n for n in self.names]
        parts = self.module.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


@dataclass
class _Scope:
    start: int
    end: int
    is_class: bool
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)


@dataclass
class _File:
    lines: int
    tree: ast.Module | None = None
    defs: list[Def] = field(default_factory=list)
    scopes: list[_Scope] = field(default_factory=list)  # pre-order: an enclosing scope precedes its inner ones
    module_bindings: dict[str, list[_Binding]] = field(default_factory=dict)
    imports: list[_Import] = field(default_factory=list)


class PyIndex:
    """The parsed Python files of one snapshot, and the name and import resolution over them."""

    def __init__(self, files: dict[str, _File], repo_roots: frozenset[str], parse_errors: dict[str, str],
                 all_files: frozenset[str]):
        self._files = files
        self._repo_roots = repo_roots
        self._parse_errors = parse_errors
        self._all_files = all_files
        self._imported: dict[str, list[str]] = {}
        self._edges: dict[str, frozenset[str]] = {}

    @classmethod
    def build(cls, root: Path) -> PyIndex:
        root = Path(root)
        real_root = root.resolve()
        files: dict[str, _File] = {}
        repo_roots: set[str] = set()
        parse_errors: dict[str, str] = {}
        all_files: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            if any(marker in dirnames or marker in filenames for marker in REPO_MARKERS):
                repo_roots.add(rel_dir)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath, name)
                if not _inside(full, real_root) or not full.is_file():
                    continue
                rel = posixpath.join(rel_dir, name)
                all_files.add(rel)
                if not name.endswith(".py"):
                    continue
                files[rel], error = _index_file(full, rel)
                if error is not None:
                    parse_errors[rel] = error
        log.debug("indexed %d Python files under %s (%d unparseable)", len(files), root, len(parse_errors))
        return cls(files, frozenset(repo_roots), parse_errors, frozenset(all_files))

    # --- files ----------------------------------------------------------------------------

    def trees(self) -> Iterator[tuple[str, ast.Module]]:
        """`(path, tree)` of every parsed file, in path order."""
        for path in sorted(self._files):
            tree = self._files[path].tree
            if tree is not None:
                yield path, tree

    def tree(self, path: str) -> ast.Module | None:
        """The parsed module, or None when `path` is not an indexed file or did not parse."""
        info = self._files.get(path)
        return info.tree if info else None

    def line_count(self, path: str) -> int | None:
        """The number of lines of an indexed `.py` file, or None when `path` is not one."""
        info = self._files.get(path)
        return info.lines if info else None

    def defs(self, path: str) -> list[Def]:
        """Every `def` and `class` of `path` in source order, nested ones included."""
        info = self._files.get(path)
        return list(info.defs) if info else []

    def files_named(self, name: str) -> list[str]:
        """Every walked file of the snapshot, of any type, whose base name is `name`, in path order."""
        return sorted(f for f in self._all_files if posixpath.basename(f) == name)

    def walked_files(self) -> list[str]:
        """Every walked file of the snapshot, of any type, in path order."""
        return sorted(self._all_files)

    def has_file(self, path: str) -> bool:
        """Whether `path` is a walked file of the snapshot, of any type."""
        return path in self._all_files

    def parse_errors(self) -> list[tuple[str, str]]:
        """`(path, detail)` of every `.py` file that could not be decoded or parsed, in path order."""
        return sorted(self._parse_errors.items())

    # --- names ----------------------------------------------------------------------------

    def repo_root(self, path: str) -> str:
        directory = posixpath.dirname(path)
        while directory and directory not in self._repo_roots:
            directory = posixpath.dirname(directory)
        return directory

    def module_name(self, path: str) -> str | None:
        if path not in self._files:
            return None
        repo = self.repo_root(path)
        rel = path[len(repo) + 1:] if repo else path
        parts = rel.removeprefix("src/").removesuffix(".py").split("/")
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or None

    def resolve_module(self, importer: str, dotted: str, level: int = 0) -> str | None:
        """The file `importer` gets for `import <dotted>` (or `from <level dots><dotted>`), same repository only."""
        parts = dotted.split(".") if dotted else []
        repo = self.repo_root(importer)
        if level:
            base = posixpath.dirname(importer)
            for _ in range(level - 1):
                if not base:
                    return None
                base = posixpath.dirname(base)
            bases = [base]
        elif parts:
            bases = self._absolute_bases(importer)
        else:
            return None
        for base in bases:
            found = self._module_file(posixpath.join(base, *parts))
            if found is not None and self.repo_root(found) == repo:
                return found
        return None

    def resolve_name(self, path: str, line: int, name: str) -> Handler | None:
        """The `def` or `class` that `name` means at `line` of `path`: function scope first, then module scope.

        `name` may be dotted, as in `commands.cmd_open`: its first part must
        then be bound to a module, and the rest names a definition in that
        module or one of its submodules. Imports are followed, re-exports
        included, within the repository. None when the name is unbound, bound
        to a module, or imported from outside the repository.
        """
        head, _, rest = name.partition(".")
        bindings = self._visible(path, line, head)
        if not rest:
            return self._resolve_bindings(path, bindings, set())
        submodule, _, attr = rest.rpartition(".")
        for binding in bindings:
            target = self._bound_module(path, binding, submodule)
            if target is not None:
                found = self._resolve_bindings(target, self._files[target].module_bindings.get(attr, []), set())
                if found is not None:
                    return found
        return None

    def resolve_global(self, path: str, name: str) -> Handler | None:
        """The `def` or `class` that the module-level name `name` of `path` means, imports followed as for
        `resolve_name`; None when `path` is not indexed or the name does not resolve.
        """
        info = self._files.get(path)
        return self._resolve_bindings(path, info.module_bindings.get(name, []), set()) if info else None

    def is_external(self, path: str, line: int, name: str) -> bool:
        """Whether `name` (or the first part of a dotted name) at `line` of `path` is imported from outside the
        repository, directly or through a same-repository re-export.
        """
        return self._external(path, self._visible(path, line, name.partition(".")[0]), set())

    def enclosing_def(self, path: str, line: int) -> Def | None:
        """The innermost `def` or `class` whose span holds `line`."""
        containing = [d for d in self.defs(path) if d.start <= line <= d.end]
        return containing[-1] if containing else None

    def enclosing_function(self, path: str, line: int) -> Def | None:
        """The innermost function `def` whose span holds `line`; classes are skipped."""
        containing = [d for d in self.defs(path) if not d.is_class and d.start <= line <= d.end]
        return containing[-1] if containing else None

    def enclosing_scope(self, path: str, line: int) -> tuple[int, int]:
        """The innermost `def`/`class` span holding `line`, or the whole module. KeyError for an unindexed path."""
        info = self._files[path]
        inner = self.enclosing_def(path, line)
        return (inner.start, inner.end) if inner else (1, info.lines)

    def importing_files(self, path: str) -> list[str]:
        """The indexed files that import `path`, as `imported_files` counts imports, in path order."""
        return [p for p in sorted(self._files) if path in self.imported_files(p)]

    def imported_files(self, path: str) -> list[str]:
        """The same-repository files `path` imports at any nesting level, in path order.

        `import a.b` also imports `a`; `from x import y` also imports `x/y.py` when that is a module.
        """
        if path not in self._imported:
            self._imported[path] = sorted(self._import_targets(path, self.resolve_module))
        return list(self._imported[path])

    # --- import graph ---------------------------------------------------------------------

    def import_edges(self, path: str) -> frozenset[str]:
        """The files `path` imports at any nesting level, other repositories included (see the module doc).

        Empty for a file that is not indexed or did not parse.
        """
        if path not in self._edges:
            self._edges[path] = frozenset(self._import_targets(path, self._resolve_import))
        return self._edges[path]

    def import_distance(self, source: str, targets: frozenset[str]) -> int | None:
        """The fewest import edges from `source` to any of `targets`: 0 when `source` is one, None with no path."""
        frontier, seen, distance = {source}, {source}, 0
        while frontier:
            if not frontier.isdisjoint(targets):
                return distance
            frontier = {edge for f in frontier for edge in self.import_edges(f)} - seen
            seen |= frontier
            distance += 1
        return None

    # --- internals ------------------------------------------------------------------------

    def _import_targets(self, path: str, resolve: Callable[[str, str, int], str | None]) -> set[str]:
        """The files `path`'s imports resolve to through `resolve`, `path` itself left out."""
        info = self._files.get(path)
        imports = info.imports if info else []
        found = {target for imp in imports for dotted in imp.dotted_targets()
                 if (target := resolve(path, dotted, imp.level)) is not None}
        found.discard(path)
        return found

    def _resolve_import(self, importer: str, dotted: str, level: int = 0) -> str | None:
        """`resolve_module`, falling back, for an absolute import, to each other repository root and its `src/`.

        Other repositories are tried in sorted order; a hit counts only when the
        file belongs to the repository searched.
        """
        found = self.resolve_module(importer, dotted, level)
        if found is not None or level or not dotted:
            return found
        own = self.repo_root(importer)
        for repo in sorted((self._repo_roots | {""}) - {own}):
            for base in (repo, posixpath.join(repo, "src")):
                found = self._module_file(posixpath.join(base, *dotted.split(".")))
                if found is not None and self.repo_root(found) == repo:
                    return found
        return None

    def _absolute_bases(self, importer: str) -> list[str]:
        repo = self.repo_root(importer)
        return [repo, posixpath.join(repo, "src"), posixpath.dirname(importer)]

    def _in_repository(self, importer: str, dotted: str) -> bool:
        """Whether absolute `import <dotted>` from `importer` names a same-repository module or package,
        a namespace package (a directory with no `__init__.py`) included.
        """
        if self.resolve_module(importer, dotted) is not None:
            return True
        repo = self.repo_root(importer)
        prefixes = tuple(posixpath.join(base, *dotted.split(".")) + "/" for base in self._absolute_bases(importer))
        return any(f.startswith(prefixes) and self.repo_root(f) == repo for f in self._files)

    def _module_file(self, stem: str) -> str | None:
        candidates = [f"{stem}.py", posixpath.join(stem, "__init__.py")] if stem else ["__init__.py"]
        return next((c for c in candidates if c in self._files), None)

    def _visible(self, path: str, line: int, name: str) -> list[_Binding]:
        info = self._files.get(path)
        if info is None:
            return []
        containing = [s for s in info.scopes if s.start <= line <= s.end]
        for depth, scope in enumerate(reversed(containing)):
            # A class body's names are not visible from the methods inside it.
            if scope.is_class and depth > 0:
                continue
            if name in scope.bindings:
                return scope.bindings[name]
        return info.module_bindings.get(name, [])

    def _resolve_bindings(self, path: str, bindings: list[_Binding], seen: set[tuple[str, str]]) -> Handler | None:
        for binding in bindings:
            match binding:
                case _DefBinding(name=name, line=line):
                    return Handler(path=path, line=line, symbol=name)
                case _FromBinding(module=module, level=level, attr=attr):
                    target = self.resolve_module(path, module, level)
                    if target is None or (target, attr) in seen:
                        continue
                    seen.add((target, attr))
                    found = self._resolve_bindings(target, self._files[target].module_bindings.get(attr, []), seen)
                    if found is not None:
                        return found
        return None

    def _bound_module(self, path: str, binding: _Binding, submodule: str) -> str | None:
        """The same-repository file for `<module bound by binding>.<submodule>`, or None."""
        match binding:
            case _ModuleBinding(module=module):
                level = 0
            case _FromBinding(module=module, level=level, attr=attr):
                module = f"{module}.{attr}" if module else attr
            case _:
                return None
        return self.resolve_module(path, f"{module}.{submodule}" if submodule else module, level)

    def _external(self, path: str, bindings: list[_Binding], seen: set[tuple[str, str]]) -> bool:
        for binding in bindings:
            match binding:
                case _ModuleBinding(module=module):
                    if not self._in_repository(path, module):
                        return True
                case _FromBinding(module=module, level=level, attr=attr):
                    target = self.resolve_module(path, module, level)
                    if target is None:
                        # A relative import never leaves the repository.
                        if level == 0 and not self._in_repository(path, module):
                            return True
                    elif (target, attr) not in seen:
                        seen.add((target, attr))
                        if self._external(target, self._files[target].module_bindings.get(attr, []), seen):
                            return True
        return False


def _inside(full: Path, real_root: Path) -> bool:
    """False for a symlink that resolves outside the snapshot, or cannot be resolved (a loop)."""
    if not full.is_symlink():
        return True
    try:
        return full.resolve().is_relative_to(real_root)
    except (OSError, RuntimeError):
        return False


def _index_file(full: Path, rel: str) -> tuple[_File, str | None]:
    """The file's index record, and the reason it could not be decoded or parsed (None when it parsed)."""
    try:
        data = full.read_bytes()
    except OSError as e:
        return _File(lines=0), f"unreadable: {e}"
    try:
        # decode_source honours a BOM or PEP 263 cookie and translates newlines.
        text = importlib.util.decode_source(data)
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError, RecursionError) as e:
        lines = _count_lines(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
        return _File(lines=lines), f"{type(e).__name__}: {e}"
    info = _File(lines=_count_lines(text), tree=tree)
    _Collector(info).body(tree.body, info.module_bindings, "")
    return info, None


def _count_lines(text: str | bytes) -> int:
    newline = "\n" if isinstance(text, str) else b"\n"
    return text.count(newline) + (1 if text and not text.endswith(newline) else 0)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


class _Collector:
    """Fills a `_File` with the definitions, scopes, bindings and imports of its statements."""

    def __init__(self, info: _File):
        self._info = info

    def body(self, stmts: list[ast.stmt], bindings: dict[str, list[_Binding]], prefix: str) -> None:
        for stmt in stmts:
            self._node(stmt, bindings, prefix)

    def _node(self, node: ast.AST, bindings: dict[str, list[_Binding]], prefix: str) -> None:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                bindings.setdefault(node.name, []).append(_DefBinding(node.name, node.lineno))
                qualname = prefix + node.name
                end = node.end_lineno or node.lineno
                self._info.defs.append(Def(node.name, qualname, node.lineno, end, isinstance(node, ast.ClassDef)))
                scope = _Scope(node.lineno, end, isinstance(node, ast.ClassDef))
                self._info.scopes.append(scope)
                self.body(node.body, scope.bindings, qualname + ".")
            case ast.Import(names=aliases):
                for alias in aliases:
                    self._info.imports.append(_Import(alias.name, 0, ()))
                    if alias.asname:
                        bindings.setdefault(alias.asname, []).append(_ModuleBinding(alias.name))
                    else:
                        top = alias.name.split(".")[0]
                        bindings.setdefault(top, []).append(_ModuleBinding(top))
            case ast.ImportFrom(module=module, level=level, names=aliases):
                names = tuple(a.name for a in aliases if a.name != "*")
                self._info.imports.append(_Import(module or "", level, names))
                for alias in aliases:
                    if alias.name != "*":
                        binding = _FromBinding(module or "", level, alias.name)
                        bindings.setdefault(alias.asname or alias.name, []).append(binding)
            case ast.If() if _is_type_checking(node.test):
                self.body(node.orelse, bindings, prefix)
            case _:
                # Statements nest inside compound statements, `except` handlers and `match` cases.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                        self._node(child, bindings, prefix)
