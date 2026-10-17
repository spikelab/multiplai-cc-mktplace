"""The patches a test applies, read statically, and the functions a patch target names.

Patch forms, each with a literal target:

- `mock.patch("<dotted>")` or `unittest.mock.patch("<dotted>")`, however
  `mock` or `patch` was imported: as a decorator of the test (or of its
  `Test*` class), as a `with` item, or through `.start()`;
- `mock.patch.object(<obj>, "<attr>")` in the same three places, `<obj>`
  resolved through the file's imports;
- `monkeypatch.setattr("<dotted>", value)` or
  `monkeypatch.setattr(<obj>, "<attr>", value)`, anywhere in the body.

A test applies its own patches and those of the pytest fixtures it uses: the
ones it requests by parameter name, the `autouse=True` ones, and, in turn, the
ones those fixtures request. A fixture is a module-level function decorated
with `pytest.fixture`, in the test module or in a `conftest.py` from the test's
directory up to the snapshot root; a name is looked up in that order, so a
nearer definition hides a farther one. As in pytest, the parameters a `mock`
decorator fills, `self`, and parameters with defaults request nothing.
"""

from __future__ import annotations

import ast
import logging
import posixpath
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from .pyindex import Def, PyIndex, dotted_reference

log = logging.getLogger(__name__)

FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

_PATCH = frozenset({"unittest.mock.patch", "mock.patch"})
_PATCH_OBJECT = frozenset({f"{name}.object" for name in _PATCH})
_MONKEYPATCH_SETATTR = "monkeypatch.setattr"
_FIXTURE = "pytest.fixture"
_NESTED = (*FUNCTION_DEFS, ast.ClassDef)


@dataclass(frozen=True, order=True)
class Patch:
    """A patch of the dotted `target`, applied at `line` of `file`."""
    file: str
    line: int
    target: str


@dataclass(frozen=True)
class Symbol:
    """A function or class a patch can replace: its `def`, and the places that look up a name bound to it."""
    path: str
    line: int
    qualname: str
    lookups: tuple[tuple[str, int], ...] = ()

    @classmethod
    def of(cls, definition: Def, path: str, lookups: Iterable[tuple[str, int]] = ()) -> Symbol:
        return cls(path, definition.start, definition.qualname, tuple(lookups))

    def named_by(self, index: PyIndex, target: str) -> bool:
        """Whether patching `target` replaces this symbol: at its defining module, or under a name its lookups
        resolve to it.
        """
        module = index.module_name(self.path)
        if module is not None and target == f"{module}.{self.qualname}":
            return True
        for path, line in self.lookups:
            lookup_module = index.module_name(path)
            if lookup_module is None or not target.startswith(f"{lookup_module}."):
                continue
            found = index.resolve_name(path, line, target.removeprefix(f"{lookup_module}."))
            if found is not None and (found.path, found.line) == (self.path, self.line):
                return True
        return False


@dataclass(frozen=True)
class _Fixture:
    name: str
    file: str
    autouse: bool
    requests: tuple[str, ...]
    patches: tuple[Patch, ...]


class PatchScan:
    """The patches the tests of one snapshot apply; each file's fixtures are read once."""

    def __init__(self, index: PyIndex):
        self._index = index
        self._fixtures: dict[str, dict[str, _Fixture]] = {}

    def of_test(self, test_file: str, func: FunctionNode, owner: ast.ClassDef | None) -> tuple[Patch, ...]:
        """The patches applied to a test, `owner` its `Test*` class, sorted by (file, line, target)."""
        decorators = [*func.decorator_list, *(owner.decorator_list if owner is not None else ())]
        found = set(self._patches(test_file, decorators, func))
        mocked = sum(self._fills_parameter(test_file, d) for d in decorators)
        requested = requested_fixtures(func, skip=mocked + (owner is not None))
        chain = [self._fixtures_of(f) for f in _fixture_files(test_file)]
        used = [f for name in requested if (f := _lookup(chain, name)) is not None]
        used += [f for level in chain for f in level.values() if f.autouse]
        found |= _fixture_patches(chain, used)
        return tuple(sorted(found))

    # --- fixtures --------------------------------------------------------------------------------

    def _fixtures_of(self, path: str) -> dict[str, _Fixture]:
        if path not in self._fixtures:
            tree = self._index.tree(path)
            fixtures: dict[str, _Fixture] = {}
            for node in tree.body if tree is not None else ():
                if isinstance(node, FUNCTION_DEFS) and (fixture := self._fixture(path, node)) is not None:
                    fixtures[fixture.name] = fixture
            self._fixtures[path] = fixtures
        return self._fixtures[path]

    def _fixture(self, path: str, func: FunctionNode) -> _Fixture | None:
        marker = next((d for d in func.decorator_list if self._is_fixture_marker(path, d)), None)
        if marker is None:
            return None
        options = {k.arg: k.value for k in marker.keywords} if isinstance(marker, ast.Call) else {}
        name = options.get("name")
        autouse = options.get("autouse")
        return _Fixture(
            name=name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else func.name,
            file=path, autouse=isinstance(autouse, ast.Constant) and autouse.value is True,
            requests=requested_fixtures(func), patches=tuple(self._patches(path, [], func)))

    def _is_fixture_marker(self, path: str, decorator: ast.expr) -> bool:
        reference = dotted_reference(decorator.func if isinstance(decorator, ast.Call) else decorator)
        return reference is not None and self._index.qualified_name(path, decorator.lineno, reference) == _FIXTURE

    # --- patch forms -----------------------------------------------------------------------------

    def _patches(self, path: str, decorators: Sequence[ast.expr], func: FunctionNode) -> Iterator[Patch]:
        for decorator in decorators:
            if isinstance(decorator, ast.Call):
                yield from self._mock_patch(path, decorator)
        for node in body_nodes(func):
            match node:
                case ast.With(items=items) | ast.AsyncWith(items=items):
                    for item in items:
                        if isinstance(item.context_expr, ast.Call):
                            yield from self._mock_patch(path, item.context_expr)
                case ast.Call(func=ast.Attribute(attr="start", value=ast.Call() as started)):
                    yield from self._mock_patch(path, started)
                case ast.Call(func=callee) if dotted_reference(callee) == _MONKEYPATCH_SETATTR:
                    yield from self._setattr(path, node)

    def _mock_patch(self, path: str, call: ast.Call) -> Iterator[Patch]:
        target = None
        match self._patcher(path, call):
            case "patch":
                target = _string(call_argument(call, 0, "target"))
            case "object":
                target = self._attribute(path, call, call_argument(call, 0, "target"),
                                         call_argument(call, 1, "attribute"))
        if target is not None:
            yield Patch(path, call.lineno, target)

    def _setattr(self, path: str, call: ast.Call) -> Iterator[Patch]:
        first = call_argument(call, 0, "target")
        dotted = _string(first)
        target = dotted if dotted is not None else self._attribute(path, call, first, call_argument(call, 1, "name"))
        if target is not None:
            yield Patch(path, call.lineno, target)

    def _patcher(self, path: str, call: ast.Call) -> str | None:
        """"patch" or "object" for a `mock.patch` or `mock.patch.object` call, else None."""
        reference = dotted_reference(call.func)
        qualified = self._index.qualified_name(path, call.lineno, reference) if reference else None
        if qualified in _PATCH:
            return "patch"
        return "object" if qualified in _PATCH_OBJECT else None

    def _attribute(self, path: str, call: ast.Call, owner: ast.expr | None, name: ast.expr | None) -> str | None:
        """`<owner's dotted name>.<name>`, `owner` resolved through the file's imports."""
        attribute = _string(name)
        reference = dotted_reference(owner) if owner is not None else None
        qualified = self._index.qualified_name(path, call.lineno, reference) if reference else None
        return f"{qualified}.{attribute}" if qualified is not None and attribute is not None else None

    def _fills_parameter(self, path: str, decorator: ast.expr) -> bool:
        """Whether a decorator passes the test a mock: a patcher given no replacement `new`."""
        if not isinstance(decorator, ast.Call):
            return False
        kind = self._patcher(path, decorator)
        positional = {"patch": 1, "object": 2}.get(kind or "")
        return (positional is not None and len(decorator.args) <= positional
                and all(k.arg != "new" for k in decorator.keywords))


def requested_fixtures(func: FunctionNode, skip: int = 0) -> tuple[str, ...]:
    """The fixture names a function requests: its parameters without defaults, the first `skip` left out."""
    args = func.args
    required = args.args[:max(0, len(args.args) - len(args.defaults))]
    keyword_only = [a for a, default in zip(args.kwonlyargs, args.kw_defaults, strict=True) if default is None]
    return tuple(a.arg for a in [*required[skip:], *keyword_only])


def body_nodes(func: FunctionNode) -> Iterator[ast.AST]:
    """The nodes of a function body, outside the `def`s and classes nested in it."""
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def call_argument(call: ast.Call, position: int, keyword: str | None) -> ast.expr | None:
    """The call's argument at `position`, else its `keyword=` argument."""
    if position < len(call.args) and not any(isinstance(a, ast.Starred) for a in call.args[:position + 1]):
        return call.args[position]
    return next((k.value for k in call.keywords if keyword is not None and k.arg == keyword), None)


def _string(expr: ast.expr | None) -> str | None:
    return expr.value if isinstance(expr, ast.Constant) and isinstance(expr.value, str) else None


def _fixture_files(test_file: str) -> list[str]:
    """The test module, then each `conftest.py` from its directory up to the snapshot root."""
    files = [test_file]
    directory = posixpath.dirname(test_file)
    while True:
        files.append(posixpath.join(directory, "conftest.py"))
        if not directory:
            return files
        directory = posixpath.dirname(directory)


def _lookup(chain: Sequence[dict[str, _Fixture]], name: str,
            requester: _Fixture | None = None) -> _Fixture | None:
    """The nearest fixture called `name`; a fixture requesting its own name gets the one it hides."""
    start = 0
    if requester is not None and requester.name == name:
        start = next((i for i, level in enumerate(chain) if level.get(name) is requester), len(chain)) + 1
    return next((level[name] for level in chain[start:] if name in level), None)


def _fixture_patches(chain: Sequence[dict[str, _Fixture]], used: Iterable[_Fixture]) -> set[Patch]:
    """The patches of the fixtures used, and of the fixtures they request in turn."""
    found: set[Patch] = set()
    pending, seen = list(used), set()
    while pending:
        fixture = pending.pop()
        if (fixture.file, fixture.name) in seen:
            continue
        seen.add((fixture.file, fixture.name))
        found.update(fixture.patches)
        pending += [f for name in fixture.requests if (f := _lookup(chain, name, fixture)) is not None]
    return found
