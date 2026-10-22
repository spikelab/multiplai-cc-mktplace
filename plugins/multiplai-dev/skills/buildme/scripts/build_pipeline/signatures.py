"""The signatures section of the test writer's context (`assemble_context`, role
`test_writer`).

A test writer that changes existing code spent most of its turns opening the
files its tests call. This section hands it what those files define instead:
for each Python module the block names, its public classes, fields, methods,
functions and constants with their signatures and first docstring lines; the
fixtures and helpers of the module's nearest `tests/` directory; the tests
there that already call the module; and one short test file as an example.

Read with `ast` from the working tree as it is now, because earlier blocks
have changed it, with git listing the files. No model call. Python only: any
other project, or a block that names no Python module, gets "".
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from .code_analysis import SKIP_DIRS, _block_text, _named_in, is_test_file
from .mutation import PYTHON_MARKERS

log = logging.getLogger(__name__)

MAX_CHARS = 40_000
EXAMPLE_LINES = 200
# Room kept free for the line that says what was cut.
_CUT_NOTE_ROOM = 1_000
_VALUE_CHARS = 80

HEADING = "## Signatures of the existing code this block's tests will call"
INTRO = ("Read from the working tree now. Use them instead of reading these files; "
         "open a file only for what is not here.")
PINS_HEADING = "### Tests that already call this code"
EXAMPLE_HEADING = "### Example test file"

_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
# A backticked name, dotted name or call: `trace`, `pkg.trace`, `trace(paths) -> list`.
_CODE_NAME = re.compile(r"([A-Za-z_][\w.]*)(?:\(.*\))?(?:\s*->.*)?")
_CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]*$")


def signatures_section(block, project_dir: Path) -> str:
    """The section for *block* in *project_dir*, or "" (see the module docstring).
    Never raises: a failure leaves the section out."""
    try:
        return _section(block, Path(project_dir))
    except Exception:  # context is best-effort; the block must still run
        log.warning("signatures: section left out for block %s", block.name, exc_info=True)
        return ""


def _section(block, project_dir: Path) -> str:
    if not any((project_dir / m).exists() for m in PYTHON_MARKERS):
        return ""
    files = _Files(project_dir)
    text = _block_text(block)
    modules = _modules(text, block, files)
    if not modules:
        return ""
    named_words = set(_IDENTIFIER.findall(text))
    out = _Section()

    symbols: set[str] = set()
    for path in modules:
        tree = files.tree(path)
        if tree is None:
            out.group(f"### `{path}`", ["(could not be parsed; open the file)"], f"`{path}`")
            continue
        entries = _entries(tree, named_words)
        symbols |= _top_level_names(tree, named_words)
        out.group(_module_title(path, tree), entries, f"`{path}`")

    test_dirs = list(dict.fromkeys(d for d in (_tests_dir(project_dir, m) for m in modules) if d))
    for tests_dir in test_dirs:
        for path in files.helpers(tests_dir):
            tree = files.tree(path)
            if tree is not None:
                entries = _entries(tree, set(), fixtures=True)
                if entries:
                    out.group(_module_title(path, tree), entries, f"`{path}`")

    pins = _pins(files, test_dirs, symbols, named_words)
    if pins:
        out.group(PINS_HEADING, [f"- `{p}`" for p in pins], "tests that already call this code")

    example = _example(files, test_dirs, modules)
    if example:
        out.group(*example, "the example test file")

    return out.render()


class _Section:
    """The section's lines, filled in order until MAX_CHARS, with a record of
    what did not fit."""

    def __init__(self):
        self.lines = [HEADING, "", INTRO]
        self.size = sum(len(x) + 1 for x in self.lines)
        self.cut: list[str] = []

    def _fits(self, text: str) -> bool:
        return self.size + len(text) + 1 <= MAX_CHARS - _CUT_NOTE_ROOM

    def _add(self, text: str) -> None:
        self.lines.append(text)
        self.size += len(text) + 1

    def group(self, title: str, entries: list[str], label: str) -> None:
        if not self._fits("\n" + title):
            self.cut.append(label)
            return
        self._add("")
        self._add(title)
        for i, entry in enumerate(entries):
            if not self._fits(entry):
                self.cut.append(f"{len(entries) - i} of {len(entries)} entries of {label}")
                return
            self._add(entry)

    def render(self) -> str:
        if self.cut:
            note = "Cut to stay under 40,000 characters: " + "; ".join(self.cut) + "."
            if len(note) > _CUT_NOTE_ROOM - 2:
                note = note[:_CUT_NOTE_ROOM - 6] + " …"
            self._add("")
            self._add(note)
        return "\n".join(self.lines)


class _Files:
    """The project's Python files on disk, each parsed at most once."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.paths = _python_files(project_dir)
        self._trees: dict[str, ast.Module | None] = {}

    def text(self, path: str) -> str | None:
        try:
            return (self.project_dir / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def tree(self, path: str) -> ast.Module | None:
        if path not in self._trees:
            text = self.text(path)
            try:
                self._trees[path] = None if text is None else ast.parse(text)
            except (SyntaxError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def sources(self) -> list[str]:
        return [p for p in self.paths if not is_test_file(p)]

    def helpers(self, tests_dir: str) -> list[str]:
        """The files directly in *tests_dir* that are not tests: conftest.py
        first, then fakes and fixture builders."""
        names = [p for p in self.paths if str(PurePosixPath(p).parent) == tests_dir
                 and not _is_test_module(p) and PurePosixPath(p).name != "__init__.py"]
        return sorted(names, key=lambda p: (PurePosixPath(p).name != "conftest.py", p))

    def tests(self, tests_dir: str) -> list[str]:
        prefix = "" if tests_dir == "." else tests_dir + "/"
        return [p for p in self.paths if p.startswith(prefix) and _is_test_module(p)]


def _python_files(project_dir: Path) -> list[str]:
    """Every `.py` file in the working tree: tracked and untracked, without the
    ignored ones, relative to *project_dir*. Walks the directory when git cannot
    say."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
            cwd=project_dir, capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        paths = {p for p in out.split("\0") if p}
    except (OSError, subprocess.SubprocessError):
        paths = set()
        for root, dirs, names in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                if name.endswith(".py"):
                    paths.add((Path(root) / name).relative_to(project_dir).as_posix())
    return sorted(p for p in paths if (project_dir / p).is_file())


def _is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _distinctive(name: str) -> bool:
    """A name with `_` or a capital in it: a one-word lowercase name (`path`,
    `value`) is more often a variable or an attribute than a definition."""
    return "_" in name.strip("_") or any(c.isupper() for c in name)


def _modules(text: str, block, files: _Files) -> list[str]:
    """The source modules *text* names by path, then the one module that defines
    each name the block consumes or puts in backticks, when only one does."""
    sources = files.sources()
    named = sorted(_named_in(text, sources))
    wanted = {n for entry in block.consumes for n in _IDENTIFIER.findall(entry)}
    for span in _BACKTICKED.findall(text):
        m = _CODE_NAME.fullmatch(span.strip())
        if m:
            wanted.add(m.group(1).rsplit(".", 1)[-1])
    wanted = {n for n in wanted if _distinctive(n)}
    found: set[str] = set()
    if wanted:
        definers: dict[str, list[str]] = {}
        for path in sources:
            body = files.text(path)
            if body is None or not any(n in body for n in wanted):
                continue
            tree = files.tree(path)
            for name in _top_level_names(tree, wanted) if tree else ():
                if name in wanted:
                    definers.setdefault(name, []).append(path)
        found = {owners[0] for owners in definers.values() if len(owners) == 1}
    return named + sorted(found - set(named))


def _tests_dir(project_dir: Path, module: str) -> str | None:
    """The nearest `tests/` directory at or above *module*'s directory."""
    d = PurePosixPath(module).parent
    while True:
        candidate = d / "tests"
        if (project_dir / candidate).is_dir():
            return str(candidate)
        if str(d) == ".":
            return None
        d = d.parent


def _public(name: str, extra: set[str]) -> bool:
    return not name.startswith("_") or name in extra


def _top_level_names(tree: ast.Module, extra: set[str]) -> set[str]:
    """The classes and functions *tree* defines at the top level that its
    section lists: the public ones and the private ones in *extra*."""
    return {n.name for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and _public(n.name, extra)}


def _module_title(path: str, tree: ast.Module) -> str:
    doc = _first_line(ast.get_docstring(tree))
    return f"### `{path}`" + (f"\n{doc}" if doc else "")


def _first_line(doc: str | None) -> str:
    lines = (doc or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _short(node: ast.AST) -> str:
    text = " ".join(ast.unparse(node).split())
    return text if len(text) <= _VALUE_CHARS else text[:_VALUE_CHARS - 1] + "…"


def _is_fixture(node: ast.AST) -> bool:
    return any("fixture" in ast.unparse(d) for d in getattr(node, "decorator_list", []))


def _decorated(node, line: str, indent: str = "") -> str:
    lines = [f"{indent}@{_short(d)}" for d in node.decorator_list]
    return "\n".join([*lines, line])


def _with_doc(line: str, node) -> str:
    doc = _first_line(ast.get_docstring(node))
    return f"{line}  # {doc}" if doc else line


def _arg(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        text += (" = " if arg.annotation is not None else "=") + _short(default)
    return text


def _arguments(a: ast.arguments) -> str:
    """*a* as PEP 8 writes it (`ast.unparse` writes `x: int=0`)."""
    positional = [*a.posonlyargs, *a.args]
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    parts = []
    for i, (arg, default) in enumerate(zip(positional, defaults)):
        parts.append(_arg(arg, default))
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg:
        parts.append("*" + _arg(a.vararg, None))
    elif a.kwonlyargs:
        parts.append("*")
    parts += [_arg(arg, default) for arg, default in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        parts.append("**" + _arg(a.kwarg, None))
    return ", ".join(parts)


def _function(node, indent: str = "") -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    line = f"{indent}{prefix} {node.name}({_arguments(node.args)})"
    if node.returns is not None:
        line += f" -> {ast.unparse(node.returns)}"
    return _decorated(node, _with_doc(line, node), indent)


def _class(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    line = f"class {node.name}({', '.join(bases)}):" if bases else f"class {node.name}:"
    lines = [_decorated(node, _with_doc(line, node))]
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and _public(item.target.id, set()):
            value = f" = {_short(item.value)}" if item.value is not None else ""
            lines.append(f"    {item.target.id}: {ast.unparse(item.annotation)}{value}")
        elif (isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)
              and _public(item.targets[0].id, set())):
            lines.append(f"    {item.targets[0].id} = {_short(item.value)}")
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(item.name, {"__init__"}):
            lines.append(_function(item, "    "))
    return "\n".join(lines)


def _entries(tree: ast.Module, extra: set[str], *, fixtures: bool = False) -> list[str]:
    """One entry per constant, class and function *tree* defines at the top
    level: the public ones, the private ones in *extra* and, with *fixtures*,
    every pytest fixture."""
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _public(node.name, extra):
            out.append(_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                _public(node.name, extra) or (fixtures and _is_fixture(node))):
            out.append(_function(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name) and _CONSTANT.match(t.id)]
            if names and node.value is not None:
                out.append(f"{names[0]} = {_short(node.value)}")
    return out


def _test_functions(tree: ast.Module):
    """`(node id suffix, function)` for each test function: top-level `test_*`,
    and `test_*` methods of top-level `Test*` classes."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    yield f"{node.name}::{item.name}", item


def _names_used(node: ast.AST) -> set[str]:
    used = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            used.add(n.attr)
    return used


def _pins(files: _Files, test_dirs: list[str], symbols: set[str], named_words: set[str]) -> list[str]:
    """The tests in *test_dirs* whose body names one of *symbols*, as
    `path::name`; those naming a symbol the block's text names come first."""
    if not symbols:
        return []
    found: list[tuple[bool, str]] = []
    for tests_dir in test_dirs:
        for path in files.tests(tests_dir):
            tree = files.tree(path)
            for suffix, fn in _test_functions(tree) if tree else ():
                hit = _names_used(fn) & symbols
                if hit:
                    found.append((not (hit & named_words), f"{path}::{suffix}"))
    return [node_id for _, node_id in sorted(dict.fromkeys(found))]


def _imports(tree: ast.Module) -> set[str]:
    """The last part of every dotted name *tree* imports."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name.rsplit(".", 1)[-1] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                names.add(n.module.rsplit(".", 1)[-1])
            names |= {a.name for a in n.names}
    return names


def _module_word(path: str) -> str:
    p = PurePosixPath(path)
    return p.parent.name if p.name == "__init__.py" else p.stem


def _example(files: _Files, test_dirs: list[str], modules: list[str]) -> tuple[str, list[str]] | None:
    """The shortest test file in *test_dirs* that imports one of *modules*,
    as a title and one fenced entry, cut at EXAMPLE_LINES lines."""
    words = {_module_word(m) for m in modules}
    best: tuple[int, str, list[str]] | None = None
    for tests_dir in test_dirs:
        for path in files.tests(tests_dir):
            tree = files.tree(path)
            text = files.text(path)
            if tree is None or text is None or not (_imports(tree) & words):
                continue
            lines = text.splitlines()
            if best is None or (len(lines), path) < best[:2]:
                best = (len(lines), path, lines)
    if best is None:
        return None
    count, path, lines = best
    shown = "\n".join(lines[:EXAMPLE_LINES]) + "\n"
    fence = "```"
    while fence in shown:
        fence += "`"
    title = f"{EXAMPLE_HEADING} `{path}`"
    if count > EXAMPLE_LINES:
        title += f" (its first {EXAMPLE_LINES} of {count} lines)"
    return title, [f"{fence}python\n{shown}{fence}"]
