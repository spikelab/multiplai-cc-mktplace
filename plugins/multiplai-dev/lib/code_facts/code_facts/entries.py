"""Entry-point discovery: the static scan that finds where a snapshot's code paths start.

`discover_entries` reads the snapshot through a `PyIndex` and never imports,
runs or spawns its code, and never calls a model. It returns entries and
diagnostics in a fixed order, whatever the filesystem order or the seed order.

argparse. One `argparse_subcommand` entry per `add_parser(<str literal>)` on a
value bound from `add_subparsers(...)`, named by its parents' names and its own,
space-joined. Parser values are followed through each scope's local names and
into the parameters of same-module functions they are passed to by name; an
`add_parser` on a receiver that cannot be followed is diagnosed as
`unresolved_target`. A handler resolves only through `set_defaults(func=<name>)`,
or through a dict literal that maps the subcommand's literal name to a function,
in the parser's module or in a same-repository module it imports. Dispatch on
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
import heapq
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


_Value = _Parser | _Subparsers
_Env = dict[str, _Value]
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEF_NODES = (*_FUNCTION_NODES, ast.ClassDef)
_SCOPE_NODES = (*_DEF_NODES, ast.Lambda)


class _ParserScan:
    """The subcommands one module declares, following parser values through the local names of each scope
    and into the parameters of the same-module functions they are passed to by name.
    """

    def __init__(self, path: str):
        self.path = path
        self._commands: dict[tuple[tuple[str, ...], int], _Subcommand] = {}
        self.diagnostics: list[Diagnostic] = []
        self._values: dict[int, _Value | None] = {}
        self._helpers: dict[str, int] = {}
        self._scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
        self._seeds: dict[int, list[_Env]] = {}

    @property
    def commands(self) -> list[_Subcommand]:
        return list(self._commands.values())

    def scan(self, tree: ast.Module) -> None:
        defs = sorted((n for n in ast.walk(tree) if isinstance(n, _DEF_NODES)), key=lambda n: (n.lineno, n.col_offset))
        self._scopes = [tree, *defs]
        self._helpers = _unique_functions(self._scopes)
        for position in _callers_first(self._scopes, self._helpers):
            # A function given parser values at its call sites is scanned once per distinct set of them.
            for env in self._seeds.pop(position, None) or [{}]:
                self._scan_body(self._scopes[position].body, dict(env))

    def _scan_body(self, body: list[ast.stmt], env: _Env) -> None:
        self._values = {}
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
                self._pass_to_helper(node, env)

    def _pass_to_helper(self, call: ast.Call, env: _Env) -> None:
        """Seed the parameters of a same-module function called by name with the parser values it is given."""
        position = self._helpers.get(call.func.id) if isinstance(call.func, ast.Name) else None
        if position is None:
            return
        arguments = self._scopes[position].args
        params = [a.arg for a in (*arguments.posonlyargs, *arguments.args)]
        seed: _Env = {}
        for param, arg in zip(params, call.args):
            if isinstance(arg, ast.Starred):
                break
            if (value := self._eval(arg, env)) is not None:
                seed[param] = value
        keyword_params = {*params, *(a.arg for a in arguments.kwonlyargs)}
        for keyword in call.keywords:
            if keyword.arg in keyword_params and (value := self._eval(keyword.value, env)) is not None:
                seed[keyword.arg] = value
        seeds = self._seeds.setdefault(position, [])
        if seed and seed not in seeds:
            seeds.append(seed)

    def _eval(self, expr: ast.expr, env: _Env) -> _Value | None:
        if isinstance(expr, ast.Name):
            return env.get(expr.id)
        if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)):
            return None
        # A call is reached again as a child of the statement it is in; it is evaluated, and recorded, once.
        if id(expr) not in self._values:
            self._values[id(expr)] = self._call(expr, expr.func, self._eval(expr.func.value, env))
        return self._values[id(expr)]

    def _call(self, call: ast.Call, callee: ast.Attribute, receiver: _Value | None) -> _Value | None:
        method = callee.attr
        if method == "add_subparsers":
            return _Subparsers(receiver.command if isinstance(receiver, _Parser) else None)
        if method == "add_parser" and isinstance(receiver, _Subparsers):
            return self._add_parser(call, receiver)
        if method == "add_parser" and receiver is None:
            self._untraced_add_parser(call, callee.value)
        if method == "set_defaults" and isinstance(receiver, _Parser) and receiver.command is not None:
            for keyword in call.keywords:
                if keyword.arg == "func":
                    receiver.command.func = (keyword.value, call.lineno)
        return None

    def _add_parser(self, call: ast.Call, subparsers: _Subparsers) -> _Parser | None:
        name = _add_parser_name(call)
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value):
            self.diagnostics.append(Diagnostic(
                path=self.path, line=call.lineno, reason="dynamic_name",
                detail=f"add_parser is given {_shown(name)}, not a string literal; the subcommand name is unknown"))
            return None
        prefix = subparsers.parent.names if subparsers.parent else ()
        key = ((*prefix, name.value), call.lineno)
        # A helper scanned once per parent it is given may reach one parent's subcommand twice.
        command = self._commands.setdefault(key, _Subcommand(*key))
        return _Parser(command)

    def _untraced_add_parser(self, call: ast.Call, receiver: ast.expr) -> None:
        self.diagnostics.append(Diagnostic(
            path=self.path, line=call.lineno, reason="unresolved_target",
            detail=f"add_parser({_shown(_add_parser_name(call))}) is called on {ast.unparse(receiver)}, "
                   "which is not traced to an add_subparsers() value in this module; no subcommand is emitted"))


def _add_parser_name(call: ast.Call) -> ast.expr | None:
    return call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "name"), None)


def _shown(expr: ast.expr | None) -> str:
    return ast.unparse(expr) if expr is not None else "nothing"


def _walk_scope(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """The nodes of one scope in source order, leaving out nested function, class and lambda scopes."""
    stack: list[ast.AST] = [n for n in reversed(body) if not isinstance(n, _SCOPE_NODES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in reversed(list(ast.iter_child_nodes(node))) if not isinstance(c, _SCOPE_NODES))


def _unique_functions(scopes: Sequence[ast.AST]) -> dict[str, int]:
    """`name -> position in scopes` of each function whose name no other function or class of the module has."""
    positions: dict[str, list[int]] = {}
    for position, scope in enumerate(scopes):
        if isinstance(scope, _DEF_NODES):
            positions.setdefault(scope.name, []).append(position)
    return {name: found[0] for name, found in positions.items()
            if len(found) == 1 and isinstance(scopes[found[0]], _FUNCTION_NODES)}


def _callers_first(scopes: Sequence[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef],
                   helpers: dict[str, int]) -> list[int]:
    """Scope positions ordered so that each function follows every scope calling it by name, so that its
    call sites are scanned first; source order breaks ties and call cycles.
    """
    callees = [{helpers[n.func.id] for n in _walk_scope(scope.body)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers} - {position}
               for position, scope in enumerate(scopes)]
    waiting = [0] * len(scopes)
    for called in callees:
        for position in called:
            waiting[position] += 1
    ready = [position for position, count in enumerate(waiting) if count == 0]
    heapq.heapify(ready)
    order: list[int] = []
    done: set[int] = set()
    while len(order) < len(scopes):
        if not ready:
            ready = [min(set(range(len(scopes))) - done)]
        position = heapq.heappop(ready)
        if position in done:
            continue
        done.add(position)
        order.append(position)
        for callee in callees[position]:
            waiting[callee] -= 1
            if waiting[callee] == 0:
                heapq.heappush(ready, callee)
    return order


def _argparse_entries(index: PyIndex, dispatch: _DispatchIndex, path: str,
                      tree: ast.Module) -> tuple[list[Entry], list[Diagnostic]]:
    if not any(isinstance(n, ast.Attribute) and n.attr == "add_parser" for n in ast.walk(tree)):
        return [], []
    scan = _ParserScan(path)
    scan.scan(tree)
    entries, diagnostics = [], list(scan.diagnostics)
    for command in scan.commands:
        siblings = frozenset(c.names[-1] for c in scan.commands if c.names[:-1] == command.names[:-1])
        handler, edge, problem = _resolve_handler(index, dispatch, path, command, siblings)
        entries.append(Entry(kind="argparse_subcommand", name=command.name,
                             location=Location(path=path, line=command.line), handler=handler, invocation=None,
                             known_edges=[edge] if edge else [], also_invoked_by=[]))
        if problem is not None:
            diagnostics.append(Diagnostic(path=path, line=command.line, reason="unresolved_handler",
                                          detail=f"subcommand {command.name!r}: {problem}"))
    return entries, diagnostics


# A handler resolution: the handler, its known edge, and why a handler mechanism found did not resolve.
_Resolution = tuple[Handler | None, KnownEdge | None, str | None]


def _resolve_handler(index: PyIndex, dispatch: _DispatchIndex, path: str, command: _Subcommand,
                     siblings: frozenset[str]) -> _Resolution:
    """Resolve through `set_defaults(func=...)` when the subparser calls it, else through a dispatch dict."""
    if command.func is not None:
        return _set_defaults_handler(index, dispatch, path, command.func)
    return _dispatch_dict_handler(index, dispatch, path, command, siblings)


def _set_defaults_handler(index: PyIndex, dispatch: _DispatchIndex, path: str,
                          func: tuple[ast.expr, int]) -> _Resolution:
    """The edge starts at the first `args.func(...)` call of the modules `dispatch.call_modules` names, or at the
    `set_defaults` line when there is none.
    """
    expr, line = func
    reference = _reference(expr)
    if reference is None:
        return None, None, None
    handler = _function(index, index.resolve_name(path, line, reference))
    if handler is None:
        return None, None, _external_problem(index, f"set_defaults at {path}:{line}", path, line, reference)
    start = dispatch.func_call(path) or (path, line)
    return handler, _edge(*start, handler), None


def _dispatch_dict_handler(index: PyIndex, dispatch: _DispatchIndex, path: str, command: _Subcommand,
                           siblings: frozenset[str]) -> _Resolution:
    """The subcommand's item in a dict literal of this module or of a same-repository module it imports.

    When some candidate dict is called as `<DICT>[...](...)`, only such dicts count. Of those left, the dict
    whose keys best match the names of the subcommand's siblings wins, so a nested `db migrate` and a
    top-level `migrate` find their own dicts; then search order. The edge starts at the dict's dispatch call,
    or at the item's line when there is none. Comparisons such as `args.command == "x"` never resolve.
    """
    leaf = command.names[-1]
    candidates = [(module, literal, dispatch.dict_call(path, module, literal.name))
                  for module in [path, *index.imported_files(path)]
                  for literal in dispatch.facts(module).dicts if leaf in literal.items]
    if any(start for _, _, start in candidates):
        candidates = [candidate for candidate in candidates if candidate[2]]
    candidates.sort(key=lambda candidate: candidate[1].mismatch(siblings))
    unresolved = None
    for module, literal, start in candidates:
        item = literal.items[leaf]
        handler = _function(index, index.resolve_name(module, item.value_line, item.value))
        if handler is not None:
            return handler, _edge(*(start or (module, item.key_line)), handler), None
        if unresolved is None:
            unresolved = _external_problem(index, f"dispatch dict entry at {module}:{item.key_line}",
                                           module, item.value_line, item.value)
    return None, None, unresolved


def _function(index: PyIndex, handler: Handler | None) -> Handler | None:
    """`handler` when it is a function `def`; None for a class or nothing."""
    if handler is None:
        return None
    found = next((d for d in index.defs(handler.path) if d.start == handler.line and d.name == handler.symbol), None)
    return handler if found is not None and not found.is_class else None


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
    """`"<key>": <value reference>` in a dict literal."""
    value: str
    key_line: int
    value_line: int


@dataclass(frozen=True)
class _DictLiteral:
    """A dict literal's string-keyed reference items; `name` is the name it is assigned to, if any."""
    name: str | None
    line: int
    items: dict[str, _DictItem]

    def mismatch(self, siblings: frozenset[str]) -> tuple[int, int]:
        """Sorts first for the dict with fewest keys that are not sibling names, then most that are."""
        keys = self.items.keys()
        return len(keys - siblings), -len(keys & siblings)


@dataclass(frozen=True)
class _DispatchFacts:
    """A module's dispatch sites: the first `args.func(...)` line (`args` bound from `.parse_args(...)`), its
    dict literals in source order, and the first `<DICT>[...](...)` line per dict name.
    """
    func_call: int | None
    dicts: list[_DictLiteral]
    dict_calls: dict[str, int]


class _DispatchIndex:
    """`_DispatchFacts` per module, and where a parser module's dispatch calls may be, each computed once."""

    def __init__(self, index: PyIndex):
        self._index = index
        self._facts: dict[str, _DispatchFacts] = {}
        self._call_modules: dict[str, list[str]] = {}

    def facts(self, path: str) -> _DispatchFacts:
        if path not in self._facts:
            tree = self._index.tree(path)
            self._facts[path] = _dispatch_facts(tree) if tree else _DispatchFacts(None, [], {})
        return self._facts[path]

    def call_modules(self, path: str) -> list[str]:
        """The parser module, the same-repository modules importing it (a `__main__.py` calling
        `build_parser()`, say), then the ones it imports: where its dispatch call may be.
        """
        if path not in self._call_modules:
            found = [path, *self._index.importing_files(path), *self._index.imported_files(path)]
            self._call_modules[path] = list(dict.fromkeys(found))
        return self._call_modules[path]

    def func_call(self, path: str) -> tuple[str, int] | None:
        """`(module, line)` of the first `args.func(...)` dispatch call for the parser module `path`."""
        for module in self.call_modules(path):
            line = self.facts(module).func_call
            if line is not None:
                return module, line
        return None

    def dict_call(self, path: str, module: str, name: str | None) -> tuple[str, int] | None:
        """`(module, line)` of the first `<name>[...](...)` call: in the dict's `module`, else wherever the
        parser module `path`'s dispatch calls may be.
        """
        if name is None:
            return None
        for candidate in dict.fromkeys([module, *self.call_modules(path)]):
            line = self.facts(candidate).dict_calls.get(name)
            if line is not None:
                return candidate, line
        return None


_PARSE_METHODS = frozenset({"parse_args", "parse_known_args", "parse_intermixed_args",
                            "parse_known_intermixed_args"})


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


def _parsed_names(node: ast.AST) -> list[str]:
    """The names an assignment binds to a `.parse_args(...)`-style result (the first of a tuple target)."""
    if not (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in _PARSE_METHODS):
        return []
    names = []
    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
        if isinstance(target, ast.Tuple) and target.elts:
            target = target.elts[0]
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _func_dispatch_line(tree: ast.Module) -> int | None:
    """The first `<name>.func(...)` line whose `<name>` is bound from a `.parse_args(...)` call in the same scope."""
    lines = []
    for body in [tree.body] + [n.body for n in ast.walk(tree) if isinstance(n, _DEF_NODES)]:
        nodes = list(_walk_scope(body))
        parsed = {name for node in nodes for name in _parsed_names(node)}
        lines += [n.lineno for n in nodes
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "func"
                  and isinstance(n.func.value, ast.Name) and n.func.value.id in parsed]
    return min(lines, default=None)


def _dispatch_facts(tree: ast.Module) -> _DispatchFacts:
    nodes = list(ast.walk(tree))
    dict_names = _assigned_dict_names(nodes)
    dicts: list[_DictLiteral] = []
    dict_calls: dict[str, int] = {}
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Subscript):
            # `COMMANDS[...](...)`, or `commands.COMMANDS[...](...)` for a dict of another module.
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(
                target, ast.Attribute) else None
            if name is not None:
                dict_calls[name] = min(dict_calls.get(name, node.lineno), node.lineno)
        elif isinstance(node, ast.Dict):
            items = {}
            for key, value in zip(node.keys, node.values):
                reference = _reference(value)
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and reference is not None:
                    items[key.value] = _DictItem(reference, key.lineno, value.lineno)
            if items:
                dicts.append(_DictLiteral(dict_names.get(id(node)), node.lineno, items))
    dicts.sort(key=lambda literal: literal.line)
    return _DispatchFacts(_func_dispatch_line(tree), dicts, dict_calls)


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
