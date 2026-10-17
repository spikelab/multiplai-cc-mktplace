"""Entry-point discovery: the static scan that finds where a snapshot's code paths start.

`discover_entries` reads the snapshot through a `PyIndex` and never imports,
runs or spawns its code, and never calls a model. It returns entries and
diagnostics in a fixed order, whatever the filesystem order or the seed order.

argparse. One `argparse_subcommand` entry per `add_parser(<str literal>)` on a
value bound from `add_subparsers(...)`, named by its parents' names and its own,
space-joined. Its handler resolves only through `set_defaults(func=<name>)`, or
through a dict literal that maps the subcommand's literal name to a function, in
the parser's module or in a same-repository module it imports. Dispatch on
`args.command == "x"` is never resolved: the entry stays at its `add_parser` line.

Seeds. A caller seed becomes a `seed` entry whose handler is the named symbol,
or the innermost `def`/`class` enclosing the seed's line.

Diagnostics. Every candidate examined but not emitted gets exactly one; an
entry whose handler mechanism names a function imported from outside the
repository is emitted with a null handler and an `unresolved_handler`
diagnostic. Any other handler that does not resolve (a lambda, a local
variable) leaves the handler null with no diagnostic.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from .paths import Handler, Invoker, Location, PathDiagnostic, Seed, _Strict, normalise_rel_path
from .pyindex import PyIndex

log = logging.getLogger(__name__)

EntryKind = Literal["argparse_subcommand", "click_command", "typer_command", "project_script", "main_module",
                    "skill_command", "hook_command", "seed"]

# The discovery diagnostic is the paths.v1 diagnostic, so it is published unchanged.
Diagnostic = PathDiagnostic


class KnownEdge(_Strict):
    """A call edge discovery proved statically: a dispatch line to the `def` it reaches."""
    # `from` is a keyword; dump with `by_alias=True` to publish it under that name.
    model_config = ConfigDict(populate_by_name=True)

    from_: Location = Field(alias="from")
    to: Location


class Entry(_Strict):
    """A discovered entry point. `known_edges` is internal: paths.v1 does not publish it."""
    kind: EntryKind
    name: str = Field(min_length=1)
    location: Location
    handler: Handler | None
    invocation: str | None
    known_edges: list[KnownEdge]
    also_invoked_by: list[Invoker]


class Discovery(_Strict):
    entries: list[Entry]
    diagnostics: list[Diagnostic]


def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery:
    """Every entry point of the snapshot at `root`, plus one entry per valid seed.

    `index` is a `PyIndex` already built for `root`. Raises ValueError for a
    seed whose path is absolute or escapes `root`.
    """
    seed_paths = [normalise_rel_path(root, seed.path) for seed in seeds]
    index = index if index is not None else PyIndex.build(root)
    entries: list[Entry] = []
    diagnostics = [Diagnostic(path=path, line=None, reason="parse_error", detail=detail)
                   for path, detail in index.parse_errors()]
    dispatch = _DispatchIndex(index)
    for path, tree in index.trees():
        found, problems = _argparse_entries(index, dispatch, path, tree)
        entries += found
        diagnostics += problems
    for seed, path in zip(seeds, seed_paths, strict=True):
        result = _seed_entry(index, seed, path)
        (entries if isinstance(result, Entry) else diagnostics).append(result)
    entries.sort(key=lambda e: (e.location.path, e.location.line, e.kind, e.name))
    diagnostics.sort(key=lambda d: (d.path, d.line or 0, d.reason, d.detail))
    log.info("discovered %d entries with %d diagnostics under %s", len(entries), len(diagnostics), root)
    return Discovery(entries=entries, diagnostics=diagnostics)


def _edge(from_path: str, from_line: int, to: Handler) -> KnownEdge:
    return KnownEdge(from_=Location(path=from_path, line=from_line), to=Location(path=to.path, line=to.line))


# --- argparse ---------------------------------------------------------------------------------


@dataclass(eq=False)
class _Subcommand:
    """One literal `add_parser` call; `func` is its last `set_defaults(func=...)` value and that call's line."""
    names: tuple[str, ...]
    line: int
    func: tuple[ast.expr, int] | None = None

    @property
    def name(self) -> str:
        return " ".join(self.names)


@dataclass(frozen=True)
class _Parser:
    """A parser value: the subcommand it was added as, or None for a top-level parser."""
    command: _Subcommand | None


@dataclass(frozen=True)
class _Subparsers:
    """The value `add_subparsers()` returns on a parser; `parent` as for `_Parser`."""
    parent: _Subcommand | None


_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_SCOPE_NODES = (*_DEF_NODES, ast.Lambda)


class _ParserScan:
    """The subcommands one module declares, following parser values through local names of each scope."""

    def __init__(self, path: str):
        self.path = path
        self.commands: list[_Subcommand] = []
        self.diagnostics: list[Diagnostic] = []
        self._values: dict[int, _Parser | _Subparsers | None] = {}

    def scan(self, tree: ast.Module) -> None:
        bodies = [tree.body] + [n.body for n in ast.walk(tree) if isinstance(n, _DEF_NODES)]
        for body in bodies:
            env: dict[str, _Parser | _Subparsers] = {}
            for node in _walk_scope(body):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    value = self._eval(node.value, env)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in (t for t in targets if isinstance(t, ast.Name)):
                        if value is None:
                            env.pop(target.id, None)
                        else:
                            env[target.id] = value
                elif isinstance(node, ast.Call):
                    self._eval(node, env)

    def _eval(self, expr: ast.expr, env: dict[str, _Parser | _Subparsers]) -> _Parser | _Subparsers | None:
        if isinstance(expr, ast.Name):
            return env.get(expr.id)
        if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)):
            return None
        # A call is reached again as a child of the statement it is in; it is evaluated, and recorded, once.
        if id(expr) not in self._values:
            self._values[id(expr)] = self._call(expr, expr.func.attr, self._eval(expr.func.value, env))
        return self._values[id(expr)]

    def _call(self, call: ast.Call, method: str,
              receiver: _Parser | _Subparsers | None) -> _Parser | _Subparsers | None:
        if method == "add_subparsers":
            return _Subparsers(receiver.command if isinstance(receiver, _Parser) else None)
        if method == "add_parser" and isinstance(receiver, _Subparsers):
            return self._add_parser(call, receiver)
        if method == "set_defaults" and isinstance(receiver, _Parser) and receiver.command is not None:
            for keyword in call.keywords:
                if keyword.arg == "func":
                    receiver.command.func = (keyword.value, call.lineno)
        return None

    def _add_parser(self, call: ast.Call, subparsers: _Subparsers) -> _Parser | None:
        name = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "name"), None)
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value):
            shown = ast.unparse(name) if name is not None else "nothing"
            self.diagnostics.append(Diagnostic(
                path=self.path, line=call.lineno, reason="dynamic_name",
                detail=f"add_parser is given {shown}, not a string literal; the subcommand name is unknown"))
            return None
        prefix = subparsers.parent.names if subparsers.parent else ()
        command = _Subcommand((*prefix, name.value), call.lineno)
        self.commands.append(command)
        return _Parser(command)


def _walk_scope(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """The nodes of one scope in source order, leaving out nested function, class and lambda scopes."""
    stack: list[ast.AST] = [n for n in reversed(body) if not isinstance(n, _SCOPE_NODES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in reversed(list(ast.iter_child_nodes(node))) if not isinstance(c, _SCOPE_NODES))


def _argparse_entries(index: PyIndex, dispatch: _DispatchIndex, path: str,
                      tree: ast.Module) -> tuple[list[Entry], list[Diagnostic]]:
    if not any(isinstance(n, ast.Attribute) and n.attr == "add_subparsers" for n in ast.walk(tree)):
        return [], []
    scan = _ParserScan(path)
    scan.scan(tree)
    entries, diagnostics = [], list(scan.diagnostics)
    for command in scan.commands:
        handler, edge, problem = _resolve_handler(index, dispatch, path, command)
        entries.append(Entry(kind="argparse_subcommand", name=command.name,
                             location=Location(path=path, line=command.line), handler=handler, invocation=None,
                             known_edges=[edge] if edge else [], also_invoked_by=[]))
        if problem is not None:
            diagnostics.append(Diagnostic(path=path, line=command.line, reason="unresolved_handler",
                                          detail=f"subcommand {command.name!r}: {problem}"))
    return entries, diagnostics


# A handler resolution: the handler, its known edge, and why a handler mechanism found did not resolve.
_Resolution = tuple[Handler | None, KnownEdge | None, str | None]


def _resolve_handler(index: PyIndex, dispatch: _DispatchIndex, path: str, command: _Subcommand) -> _Resolution:
    """Resolve through `set_defaults(func=...)` when the subparser calls it, else through a dispatch dict."""
    if command.func is not None:
        return _set_defaults_handler(index, dispatch, path, command.func)
    return _dispatch_dict_handler(index, dispatch, path, command)


def _set_defaults_handler(index: PyIndex, dispatch: _DispatchIndex, path: str,
                          func: tuple[ast.expr, int]) -> _Resolution:
    """The edge starts at the module's `args.func(...)` call, or at the `set_defaults` line when there is none."""
    expr, line = func
    reference = _reference(expr)
    if reference is None:
        return None, None, None
    handler = index.resolve_name(path, line, reference)
    if handler is None:
        return None, None, _external_problem(index, f"set_defaults at {path}:{line}", path, line, reference)
    return handler, _edge(path, dispatch.facts(path).func_call or line, handler), None


def _dispatch_dict_handler(index: PyIndex, dispatch: _DispatchIndex, path: str,
                           command: _Subcommand) -> _Resolution:
    """The first dict item, in this module then in each same-repository module it imports, keyed by the
    subcommand's own name and naming a function. The edge starts at the `<DICT>[...](...)` call in the dict's
    module, or at the item's line when there is none. Comparisons such as `args.command == "x"` never resolve.
    """
    unresolved = None
    for module in [path, *index.imported_files(path)]:
        facts = dispatch.facts(module)
        for item in facts.dict_items.get(command.names[-1], []):
            handler = index.resolve_name(module, item.value_line, item.value)
            if handler is not None:
                start = facts.dict_calls.get(item.dict_name) if item.dict_name else None
                return handler, _edge(module, start or item.key_line, handler), None
            if unresolved is None:
                unresolved = _external_problem(index, f"dispatch dict entry at {module}:{item.key_line}",
                                               module, item.value_line, item.value)
    return None, None, unresolved


def _reference(expr: ast.expr) -> str | None:
    """`a` or `a.b.c` for a name or an attribute chain on a name; None for anything else, such as a lambda."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    return ".".join([expr.id, *reversed(parts)])


def _external_problem(index: PyIndex, site: str, path: str, line: int, reference: str) -> str | None:
    """The `unresolved_handler` detail when an unresolved handler reference comes from outside the repository."""
    if not index.is_external(path, line, reference):
        return None
    return f"{site} maps it to {reference}, which is imported from outside this repository"


@dataclass(frozen=True)
class _DictItem:
    """`"<key>": <value reference>` in a dict literal; `dict_name` is the name the dict is assigned to, if any."""
    key: str
    value: str
    key_line: int
    value_line: int
    dict_name: str | None


@dataclass(frozen=True)
class _DispatchFacts:
    """A module's dispatch sites: the first `<x>.func(...)` line, its dict items, and each `<DICT>[...](...)` line."""
    func_call: int | None
    dict_items: dict[str, list[_DictItem]]
    dict_calls: dict[str, int]


class _DispatchIndex:
    """`_DispatchFacts` per module, computed once each."""

    def __init__(self, index: PyIndex):
        self._index = index
        self._facts: dict[str, _DispatchFacts] = {}

    def facts(self, path: str) -> _DispatchFacts:
        if path not in self._facts:
            tree = self._index.tree(path)
            self._facts[path] = _dispatch_facts(tree) if tree else _DispatchFacts(None, {}, {})
        return self._facts[path]


def _assigned_dict_names(nodes: list[ast.AST]) -> dict[int, str]:
    """`id(dict literal) -> name` for each dict literal assigned, alone, to a plain name."""
    names: dict[int, str] = {}
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(node.value, ast.Dict) and isinstance(target, ast.Name):
            names[id(node.value)] = target.id
    return names


def _dispatch_facts(tree: ast.Module) -> _DispatchFacts:
    nodes = list(ast.walk(tree))
    dict_names = _assigned_dict_names(nodes)
    func_calls: list[int] = []
    dict_calls: dict[str, int] = {}
    items: dict[str, list[_DictItem]] = {}
    for node in nodes:
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == "func" and isinstance(callee.value, ast.Name):
                func_calls.append(node.lineno)
            elif isinstance(callee, ast.Subscript) and isinstance(callee.value, ast.Name):
                name = callee.value.id
                dict_calls[name] = min(dict_calls.get(name, node.lineno), node.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                reference = _reference(value)
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and reference is not None:
                    items.setdefault(key.value, []).append(
                        _DictItem(key.value, reference, key.lineno, value.lineno, dict_names.get(id(node))))
    for found in items.values():
        found.sort(key=lambda item: item.key_line)
    return _DispatchFacts(min(func_calls, default=None), items, dict_calls)


# --- seeds ------------------------------------------------------------------------------------


def _seed_entry(index: PyIndex, seed: Seed, path: str) -> Entry | Diagnostic:
    """The seed's entry, or its `seed_not_found` diagnostic. A symbol seed ignores any line it also names."""
    def not_found(detail: str) -> Diagnostic:
        line = seed.line if seed.line is not None and seed.line >= 1 else None
        return Diagnostic(path=path, line=line, reason="seed_not_found", detail=detail)

    lines = index.line_count(path)
    if lines is None:
        return not_found(f"no Python file {path} in the snapshot")
    if index.tree(path) is None:
        return not_found(f"{path} could not be parsed")
    if seed.symbol is not None:
        defs = index.defs(path)
        found = next((d for d in defs if d.qualname == seed.symbol), None) or next(
            (d for d in defs if d.name == seed.symbol), None)
        if found is None:
            return not_found(f"no def or class {seed.symbol!r} in {path}")
        line = found.start
    elif seed.line is None:
        return not_found("the seed names neither a line nor a symbol")
    elif not 1 <= seed.line <= lines:
        return not_found(f"line {seed.line} is outside {path}, which has {lines} lines")
    else:
        line = seed.line
        found = index.enclosing_def(path, line)
    handler = Handler(path=path, line=found.start, symbol=found.name) if found else None
    default_name = f"{path}:{found.name}" if found else f"{path}:{line}"
    return Entry(kind="seed", name=seed.name or default_name, location=Location(path=path, line=line),
                 handler=handler, invocation=None, known_edges=[], also_invoked_by=[])
