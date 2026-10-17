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

click and typer. In a module importing either, a `click_command` entry per
`@click.command`/`@click.group` or `@<group>.command`/`@<group>.group` on a
click group defined in the module, and a `typer_command` entry per
`@<app>.command`/`@<app>.callback` on a `typer.Typer()` of the module. The entry
sits on the decorator line and its handler is the decorated `def`. Its name is
the explicit name argument, or the function name with `_` made `-`, after the
names of its parent groups and of the `add_typer(..., name=...)` mounts above
it; a sub-app mounted with no name merges into its parent.

`[project.scripts]`. A `project_script` entry per key of every `pyproject.toml`,
on the key's line; `"<module>:<function>"` resolves within that repository,
under its root then its `src/`, and the key line gets a known edge to the `def`.

`__main__`. A `main_module` entry per package `__main__.py` and per non-test
module with a top-level `if __name__ == "__main__":` guard, on the guard line,
or line 1 of a guardless `__main__.py`. Its handler is the one same-repository
function the guard body (or the module body) calls, looking through `sys.exit`,
`exit`, `SystemExit`, `asyncio.run` and `raise SystemExit(...)`.

`SKILL.md` commands. A `skill_command` entry per `python[3] -m <module>`
command in a `bash`, `sh`, `shell`, `zsh` or `console` fence of a `SKILL.md`,
named `<skill dir>: <module> [<first positional>]`. The module is looked for in
the command's `PYTHONPATH`, `cd`, `--directory` and `--project` directories,
the `SKILL.md` directory and its `scripts/`, then the repository root and its
`src/`. When the first positional names an argparse subcommand of that module,
the known edge goes to the subcommand's handler, or to its `add_parser` line
when it has none; otherwise it goes to the module's `main_module` location.

Hooks. A `hook_command` entry per `"type": "command"` hook of a `hooks.json` or
`.claude/settings*.json`, named `<event>[:<matcher>]`, on its command string's
line. Past its launchers (`sh run.sh`, `uv run`, ...), a `python -m` resolves as
in a `SKILL.md`, else the first `.py` it names resolves as a `main_module`
script; a command naming neither targets its launcher or file at line 1.

A command entry's handler is the subcommand's handler, or the handler of the
`main_module` location its edge reaches, so a command merges into the entry it
runs.

Seeds. A caller seed becomes a `seed` entry whose handler is the named symbol,
or the innermost `def`/`class` enclosing the seed's line.

Merging. Entries whose handlers are the same `def`/`class` become one: the
entry of best `KIND_RANK`, the others listed in its `also_invoked_by`. An entry
whose handler is null or not Python merges only with one of identical kind and
location.

Diagnostics. Every candidate examined but not emitted gets exactly one; an
entry whose handler mechanism names a function imported from outside the
repository is emitted with a null handler and an `unresolved_handler`
diagnostic, as is a `main_module` whose guard calls several functions. Any
other handler that does not resolve (a lambda, a local variable, a script with
no function call) leaves the handler null with no diagnostic.
"""

from __future__ import annotations

import ast
import fnmatch
import heapq
import logging
import posixpath
import re
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from .commands import (CommandLine, first_positional, hook_commands, is_hooks_file, looks_like_python_m,
                       module_search_roots, parse_command_line, python_module, resolve_python_m, shell_commands)
from .paths import (KIND_RANK, DiscoveredKind, Handler, Invoker, Location, PathDiagnostic, PathEntry, Seed,
                    _Strict, normalise_rel_path)
from .paths import EntryKind as OutputKind
from .pyindex import PyIndex

log = logging.getLogger(__name__)

EntryKind = Literal["argparse_subcommand", "click_command", "typer_command", "project_script", "main_module",
                    "skill_command", "hook_command", "seed"]

# The kind paths.v1 publishes for each discovery kind.
OUTPUT_KIND: dict[EntryKind, DiscoveredKind] = {
    "argparse_subcommand": "argparse", "click_command": "click", "typer_command": "typer", "main_module": "main",
    "project_script": "script", "skill_command": "skill_command", "hook_command": "hook", "seed": "seed",
}

# The kind of an entry the model fallback adds; it is published as is and never merges.
ModelKind = Literal["model"]

# The discovery diagnostic is the paths.v1 diagnostic, so it is published unchanged.
Diagnostic = PathDiagnostic


class KnownEdge(_Strict):
    """A call edge discovery proved statically: a dispatch line to the `def` it reaches."""
    # `from` is a keyword; dump with `by_alias=True` to publish it under that name.
    model_config = ConfigDict(populate_by_name=True)

    from_: Location = Field(alias="from")
    to: Location


class EntryInvoker(_Strict):
    """An entry merged into another, under its discovery kind; paths.v1 publishes it as an `Invoker`."""
    kind: EntryKind
    name: str
    location: Location


class Entry(_Strict):
    """A discovered entry point, or one the model fallback added. `known_edges` is internal: paths.v1 does not
    publish it.
    """
    kind: EntryKind | ModelKind
    name: str = Field(min_length=1)
    location: Location
    handler: Handler | None
    invocation: str | None
    known_edges: list[KnownEdge]
    also_invoked_by: list[EntryInvoker]


def output_kind(entry: Entry) -> OutputKind:
    """The kind paths.v1 publishes for `entry`."""
    return "model" if entry.kind == "model" else OUTPUT_KIND[entry.kind]


def to_path_entry(entry: Entry) -> PathEntry:
    """The entry as paths.v1 publishes it: output kinds, its invokers' included, and no known edges."""
    invokers = [Invoker(kind=OUTPUT_KIND[i.kind], name=i.name, location=i.location) for i in entry.also_invoked_by]
    return PathEntry(kind=output_kind(entry), name=entry.name, location=entry.location, handler=entry.handler,
                     invocation=entry.invocation, also_invoked_by=invokers)


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
    for found, problems in _scans(root, index):
        entries += found
        diagnostics += problems
    subcommands = _Subcommands(index, entries)
    for found, problems in _command_scans(root, index, subcommands):
        entries += found
        diagnostics += problems
    for seed, path in zip(seeds, seed_paths, strict=True):
        result = _seed_entry(index, seed, path)
        (entries if isinstance(result, Entry) else diagnostics).append(result)
    entries = merge_entries(entries)
    entries.sort(key=lambda e: (e.location.path, e.location.line, e.kind, e.name))
    diagnostics.sort(key=lambda d: (d.path, d.line or 0, d.reason, d.detail))
    log.info("discovered %d entries with %d diagnostics under %s", len(entries), len(diagnostics), root)
    return Discovery(entries=entries, diagnostics=diagnostics)


# What one scanner found: the entries it emits, and the diagnostics for what it examined but did not emit.
_Found = tuple[list[Entry], list[Diagnostic]]


def _scans(root: Path, index: PyIndex) -> Iterator[_Found]:
    """What each scanner finds, module by module in path order, then `pyproject.toml` by `pyproject.toml`."""
    dispatch = _DispatchIndex(index)
    for path, tree in index.trees():
        yield _argparse_entries(index, dispatch, path, tree)
        yield _decorated_command_entries(path, tree)
        yield _main_module_entries(index, path, tree)
    for path in index.files_named("pyproject.toml"):
        yield _project_script_entries(root, index, path)


def _unreadable(path: str, error: Exception) -> _Found:
    """What a scanner finds in a file it cannot read or parse: no entries, and one file-level `parse_error`."""
    return [], [Diagnostic(path=path, line=None, reason="parse_error", detail=f"{type(error).__name__}: {error}")]


def _handler_location(handler: Handler) -> Location:
    """The location of a handler's `def` (or of a non-Python target's line)."""
    return Location(path=handler.path, line=handler.line)


def _edge(from_path: str, from_line: int, to: Handler) -> KnownEdge:
    return KnownEdge(from_=Location(path=from_path, line=from_line), to=_handler_location(to))


def _static_entry(kind: EntryKind, name: str, path: str, line: int, handler: Handler | None,
                  known_edges: Iterable[KnownEdge] = ()) -> Entry:
    """An entry declared at `path:line` of the source: no invocation text, and no invokers until merged."""
    return Entry(kind=kind, name=name, location=Location(path=path, line=line), handler=handler, invocation=None,
                 known_edges=list(known_edges), also_invoked_by=[])


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
        self._scopes = [tree, *_in_source_order(n for n in ast.walk(tree) if isinstance(n, _DEF_NODES))]
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


def _argparse_entries(index: PyIndex, dispatch: _DispatchIndex, path: str, tree: ast.Module) -> _Found:
    if not any(isinstance(n, ast.Attribute) and n.attr == "add_parser" for n in ast.walk(tree)):
        return [], []
    scan = _ParserScan(path)
    scan.scan(tree)
    entries, diagnostics = [], list(scan.diagnostics)
    for command in scan.commands:
        siblings = frozenset(c.names[-1] for c in scan.commands if c.names[:-1] == command.names[:-1])
        handler, edge, problem = _resolve_handler(index, dispatch, path, command, siblings)
        entries.append(_static_entry("argparse_subcommand", command.name, path, command.line, handler,
                                     [edge] if edge else []))
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


# --- click and typer --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CommandGroup:
    """What commands are declared on: a click group or a typer app, with the names it puts before theirs.
    `prefix` is None when one of those names is not a literal.
    """
    kind: EntryKind
    prefix: tuple[str, ...] | None


_CLICK_ROOT = _CommandGroup("click_command", ())
# The decorator methods that declare a command, per kind of group.
_COMMAND_METHODS: dict[EntryKind, frozenset[str]] = {
    "click_command": frozenset({"command", "group"}),
    "typer_command": frozenset({"command", "callback"}),
}
_ANY_COMMAND_METHOD = frozenset().union(*_COMMAND_METHODS.values())


def _decorated_command_entries(path: str, tree: ast.Module) -> _Found:
    """The click and typer commands a module declares on its own groups and apps, in source order.

    A click group is known from its decorator on, as click registers it when the
    decorator runs; a typer app's mounts are all known up front, as typer names
    commands when the app runs.
    """
    if not _imports_any(tree, {"click", "typer"}):
        return [], []
    groups: dict[str, _CommandGroup] = dict(_typer_apps(tree))
    entries: list[Entry] = []
    diagnostics: list[Diagnostic] = []
    functions = _in_source_order(n for n in ast.walk(tree) if isinstance(n, _FUNCTION_NODES))
    for func in functions:
        for decorator in func.decorator_list:
            site = _decorator_site(decorator)
            if site is None:
                continue
            receiver, method, call = site
            group = _CLICK_ROOT if receiver == "click" else groups.get(receiver)
            if group is None:
                if method in _ANY_COMMAND_METHOD:
                    diagnostics.append(Diagnostic(
                        path=path, line=decorator.lineno, reason="unresolved_target",
                        detail=f"@{receiver}.{method} on {func.name}: {receiver} is not traced to a click group or "
                               "typer app of this module; no command is emitted"))
                continue
            if method not in _COMMAND_METHODS[group.kind]:
                continue
            names = _command_names(group, receiver, method, call, func)
            if isinstance(names, str):
                diagnostics.append(Diagnostic(path=path, line=decorator.lineno, reason="dynamic_name", detail=names))
                continue
            if group.kind == "click_command" and method == "group":
                groups[func.name] = _CommandGroup("click_command", names)
            handler = Handler(path=path, line=func.lineno, symbol=func.name)
            entries.append(_static_entry(group.kind, " ".join(names), path, decorator.lineno, handler))
    return entries, diagnostics


def _command_names(group: _CommandGroup, receiver: str, method: str, call: ast.Call | None,
                   func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...] | str:
    """The full name of a command declared by `@<receiver>.<method>(...)` on `group`: the group's prefix, then
    the explicit name or the function name with `_` made `-`. `call` is None for a bare decorator. When a part
    is not a literal, the `dynamic_name` diagnostic's detail instead.
    """
    explicit = _call_name_argument(call) if call is not None else None
    if _is_unset(explicit):
        leaf = func.name.replace("_", "-")
    elif (leaf := _literal_str(explicit)) is None:
        return (f"@{receiver}.{method} on {func.name} is given {_shown(explicit)}, not a string "
                "literal; the command name is unknown")
    if group.prefix is None:
        return (f"@{receiver}.{method} on {func.name}: {receiver} is mounted by add_typer under a "
                "name that is not a string literal; the command name is unknown")
    return (*group.prefix, leaf)


def _typer_apps(tree: ast.Module) -> dict[str, _CommandGroup]:
    """`name -> group` of each `typer.Typer()` the module assigns to a name, prefixed by its `add_typer` mounts.

    A mount's name is its `name=` argument, else the sub-app's own `Typer(name=...)`;
    a mount with neither merges the sub-app's commands into its parent's.
    """
    created: dict[str, ast.expr | None] = {}
    mounts: dict[str, tuple[str, ast.expr | None]] = {}
    for node in _in_source_order(n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.Call))):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is not None and _is_typer_constructor(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in (t for t in targets if isinstance(t, ast.Name)):
                    created[target.id] = _keyword(node.value, "name")
        elif (isinstance(node.func, ast.Attribute) and node.func.attr == "add_typer"
              and isinstance(node.func.value, ast.Name) and node.args and isinstance(node.args[0], ast.Name)):
            # A sub-app mounted twice keeps its first mount.
            mounts.setdefault(node.args[0].id, (node.func.value.id, _keyword(node, "name")))

    def prefix(app: str, seen: frozenset[str]) -> tuple[str, ...] | None:
        if app not in mounts or app in seen:
            return ()
        parent, name = mounts[app]
        base = prefix(parent, seen | {app})
        segment = name if not _is_unset(name) else created.get(app)
        if _is_unset(segment):
            return base
        literal = _literal_str(segment)
        return None if base is None or literal is None else (*base, literal)

    return {app: _CommandGroup("typer_command", prefix(app, frozenset())) for app in created}


def _decorator_site(decorator: ast.expr) -> tuple[str, str, ast.Call | None] | None:
    """`(receiver, method, call)` of a `@<receiver>.<method>(...)` or bare `@<receiver>.<method>` decorator."""
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
        return None
    return target.value.id, target.attr, call


def _call_name_argument(call: ast.Call) -> ast.expr | None:
    """A command decorator's name: its first positional argument or its `name=` keyword."""
    return call.args[0] if call.args else _keyword(call, "name")


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_unset(expr: ast.expr | None) -> bool:
    return expr is None or (isinstance(expr, ast.Constant) and expr.value is None)


def _literal_str(expr: ast.expr | None) -> str | None:
    """The value of a non-empty string literal; None for anything else."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str) and expr.value:
        return expr.value
    return None


def _is_typer_constructor(expr: ast.expr) -> bool:
    """`typer.Typer(...)` or `Typer(...)`."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    return (isinstance(func, ast.Name) and func.id == "Typer") or (
        isinstance(func, ast.Attribute) and func.attr == "Typer" and isinstance(func.value, ast.Name)
        and func.value.id == "typer")


def _imports_any(tree: ast.Module, packages: set[str]) -> bool:
    """Whether the module imports, at any level, a module of one of the top-level `packages`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in packages for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] in packages:
            return True
    return False


def _in_source_order[N: ast.AST](nodes: Iterable[N]) -> list[N]:
    return sorted(nodes, key=lambda n: (n.lineno, n.col_offset))


# --- [project.scripts] ------------------------------------------------------------------------


def _project_script_entries(root: Path, index: PyIndex, path: str) -> _Found:
    """One entry per `[project.scripts]` key of the `pyproject.toml` at `path` whose target resolves."""
    try:
        text = (root / path).read_bytes().decode("utf-8")
        document = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return _unreadable(path, e)
    project = document.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return [], []
    entries: list[Entry] = []
    diagnostics: list[Diagnostic] = []
    for name, target in scripts.items():
        line = _script_key_line(text, name)
        handler = _script_handler(index, path, target) if name else None
        if handler is None:
            diagnostics.append(Diagnostic(
                path=path, line=line, reason="unresolved_target",
                detail=f"[project.scripts] {name!r} = {target!r} does not name a function of this repository"))
            continue
        entries.append(_static_entry("project_script", name, path, line, handler, [_edge(path, line, handler)]))
    return entries, diagnostics


def _script_handler(index: PyIndex, pyproject: str, target: object) -> Handler | None:
    """The `def` a `"<module>:<function>"` script target names, searched for in the pyproject's repository."""
    if not isinstance(target, str):
        return None
    module, _, function = (part.strip() for part in target.partition(":"))
    if not (module and function):
        return None
    module_file = index.resolve_module(pyproject, module)
    return _function(index, index.resolve_global(module_file, function)) if module_file else None


_TOML_TABLE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _script_key_line(text: str, key: str) -> int:
    """The line declaring script `key`: in a `[project.scripts]` table, or as `scripts.<key> =` or inside an
    inline `scripts = {...}` under `[project]`. tomllib keeps no line numbers, so the text is scanned.
    """
    quoted = "|".join(re.escape(form) for form in (key, f'"{key}"', f"'{key}'"))
    in_scripts = re.compile(rf"^\s*(?:{quoted})\s*=")
    in_project = re.compile(rf"^\s*scripts\s*(?:\.\s*(?:{quoted})\s*=|=\s*\{{(?:[^}}]*,)?\s*(?:{quoted})\s*=)")
    table = None
    for number, line in enumerate(text.splitlines(), start=1):
        if header := _TOML_TABLE.match(line):
            table = re.sub(r"\s*\.\s*", ".", header.group(1)).replace('"', "").replace("'", "")
        elif (table == "project.scripts" and in_scripts.match(line)) or (
                table == "project" and in_project.match(line)):
            return number
    # Unreachable for a key tomllib parsed from a table this scan recognises.
    return 1


# --- __main__ ---------------------------------------------------------------------------------

_TRANSPARENT_WRAPPERS = frozenset({"sys.exit", "exit", "SystemExit", "asyncio.run"})


def _main_module_entries(index: PyIndex, path: str, tree: ast.Module) -> _Found:
    """The module's `main_module` entry: for a package `__main__.py`, or a non-test module with a guard."""
    package_main = posixpath.basename(path) == "__main__.py"
    if _is_test_file(path) or not (package_main or _main_guard(tree)):
        return [], []
    location, callees = _main_callees(index, path, tree)
    module = index.module_name(path) or path
    handler = _sole(callees)
    entry = _static_entry("main_module", module.removesuffix(".__main__") if package_main else module,
                          path, location.line, handler, [_edge(path, location.line, handler)] if handler else [])
    if len(callees) < 2:
        return [entry], []
    called = ", ".join(f"{h.symbol} ({h.path}:{h.line})" for h in callees)
    return [entry], [Diagnostic(path=path, line=location.line, reason="unresolved_handler",
                                detail=f"{module} calls several functions of this repository: {called}")]


def main_module_handler(index: PyIndex, path: str) -> tuple[Location, Handler | None]:
    """Where the `main_module` entry of `path` sits, and its handler: the one same-repository function called.

    The location is the top-level `if __name__ == "__main__":` line, or line 1
    without one. Its handler is None unless exactly one such function is called.
    """
    tree = index.tree(path)
    if tree is None:
        return Location(path=path, line=1), None
    location, callees = _main_callees(index, path, tree)
    return location, _sole(callees)


def _sole(callees: list[Handler]) -> Handler | None:
    """The handler of a `main_module`: its one same-repository callee, or None for none or several."""
    return callees[0] if len(callees) == 1 else None


def _main_callees(index: PyIndex, path: str, tree: ast.Module) -> tuple[Location, list[Handler]]:
    """The entry location, and the distinct same-repository functions the guard body (else the module body)
    calls, in source order.
    """
    guard = _main_guard(tree)
    callees: dict[tuple[str, int], Handler] = {}
    for call in _entry_calls(guard.body if guard else tree.body):
        reference = _reference(call.func)
        found = _function(index, index.resolve_name(path, call.lineno, reference)) if reference else None
        if found is not None:
            callees.setdefault((found.path, found.line), found)
    return Location(path=path, line=guard.lineno if guard else 1), list(callees.values())


def _entry_calls(body: list[ast.stmt]) -> Iterator[ast.Call]:
    """The outermost calls of a scope, outside nested scopes, looking through the transparent wrappers'
    arguments: `sys.exit(main(argv))` yields `main(argv)` alone.
    """
    stack: list[ast.AST] = [n for n in reversed(body) if not isinstance(n, _SCOPE_NODES)]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            if _reference(node.func) not in _TRANSPARENT_WRAPPERS:
                yield node
                continue
            children: list[ast.AST] = [*node.args, *(k.value for k in node.keywords)]
        else:
            children = list(ast.iter_child_nodes(node))
        stack.extend(c for c in reversed(children) if not isinstance(c, _SCOPE_NODES))


def _main_guard(tree: ast.Module) -> ast.If | None:
    """The first top-level `if __name__ == "__main__":`, either way round."""
    for node in tree.body:
        test = node.test if isinstance(node, ast.If) else None
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
            sides = (test.left, test.comparators[0])
            if (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
                    and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)):
                return node
    return None


def _is_test_file(path: str) -> bool:
    """`test_*.py`, `*_test.py`, `conftest.py`, or anything under a `tests/` directory."""
    *directories, name = path.split("/")
    return ("tests" in directories or name == "conftest.py"
            or fnmatch.fnmatchcase(name, "test_*.py") or fnmatch.fnmatchcase(name, "*_test.py"))


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
    return _static_entry("seed", seed.name or default_name, path, line, handler)


# --- SKILL.md and hook commands -----------------------------------------------------------------


class _Subcommands:
    """The top-level argparse subcommands discovered per module file, for a command that names one."""

    def __init__(self, index: PyIndex, entries: Iterable[Entry]):
        self._index = index
        self._by_file: dict[str, dict[str, Entry]] = {}
        argparse = (e for e in entries if e.kind == "argparse_subcommand" and " " not in e.name)
        for entry in sorted(argparse, key=lambda e: (e.location.path, e.location.line)):
            self._by_file.setdefault(entry.location.path, {}).setdefault(entry.name, entry)

    def find(self, module_file: str, name: str) -> Entry | None:
        """Subcommand `name` declared in `module_file`, else in the first same-repository module it imports."""
        for path in [module_file, *self._index.imported_files(module_file)]:
            found = self._by_file.get(path, {}).get(name)
            if found is not None:
                return found
        return None


@dataclass(frozen=True)
class _CommandTarget:
    """Where a command lands: the end of its known edge, and the handler its entry takes."""
    to: Location
    handler: Handler | None

    @classmethod
    def at_file(cls, path: str) -> _CommandTarget:
        """Line 1 of a launcher or other non-Python file the command runs, with a symbol-less handler."""
        return cls(Location(path=path, line=1), Handler(path=path, line=1, symbol=None))


def _command_scans(root: Path, index: PyIndex, subcommands: _Subcommands) -> Iterator[_Found]:
    """What each command scanner finds: `SKILL.md` by `SKILL.md`, then hooks file by hooks file, in path order."""
    for path in index.files_named("SKILL.md"):
        yield _skill_command_entries(root, index, subcommands, path)
    for path in filter(is_hooks_file, index.walked_files()):
        yield _hook_entries(root, index, subcommands, path)


def _skill_command_entries(root: Path, index: PyIndex, subcommands: _Subcommands, path: str) -> _Found:
    """One entry per `python -m` command in the shell fences of the `SKILL.md` at `path`."""
    try:
        text = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _unreadable(path, e)
    skill = posixpath.basename(posixpath.dirname(path)) or root.absolute().name
    entries: list[Entry] = []
    diagnostics: list[Diagnostic] = []
    for line, invocation in shell_commands(text):
        try:
            command = parse_command_line(invocation, source_file=path, root=root, index=index)
        except ValueError as e:
            if looks_like_python_m(invocation):
                diagnostics.append(Diagnostic(path=path, line=line, reason="unresolved_target",
                                              detail=f"{invocation!r} cannot be tokenised: {e}"))
            continue
        module = python_module(command.tokens)
        if module is None:
            continue
        module_name, arguments = module
        target = _module_target(index, subcommands, command, path, module_name, arguments)
        if isinstance(target, str):
            diagnostics.append(Diagnostic(path=path, line=line, reason="unresolved_target", detail=target))
            continue
        positional = first_positional(arguments)
        entry_name = f"{skill}: {module_name}" + (f" {positional}" if positional else "")
        entries.append(_command_entry("skill_command", entry_name, path, line, invocation, target))
    return entries, diagnostics


def _hook_entries(root: Path, index: PyIndex, subcommands: _Subcommands, path: str) -> _Found:
    """One entry per command hook of the hooks or settings file at `path` whose target resolves."""
    try:
        hooks = hook_commands((root / path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return _unreadable(path, e)
    entries: list[Entry] = []
    diagnostics: list[Diagnostic] = []
    for hook in hooks:
        try:
            command = parse_command_line(hook.command, source_file=path, root=root, index=index)
        except ValueError as e:
            target: _CommandTarget | str = f"the command cannot be tokenised: {e}"
        else:
            target = _hook_target(index, subcommands, command, path)
        if isinstance(target, str):
            diagnostics.append(Diagnostic(path=path, line=hook.line, reason="unresolved_target",
                                          detail=f"hook {hook.name} runs {hook.command!r}: {target}"))
            continue
        entries.append(_command_entry("hook_command", hook.name, path, hook.line, hook.command, target))
    return entries, diagnostics


def _hook_target(index: PyIndex, subcommands: _Subcommands, command: CommandLine,
                 source_file: str) -> _CommandTarget | str:
    """What a hook runs past its launchers: its `python -m` module, else the first existing `.py` it names.
    When neither exists, the stripped launcher or else the first existing file it names, at line 1. The
    reason it has no target, when none of them exists.
    """
    module = python_module(command.tokens)
    missing: str | None = None
    if module is not None:
        target = _module_target(index, subcommands, command, source_file, *module)
        if isinstance(target, _CommandTarget):
            return target
        missing = target
    else:
        scripts = [command.snapshot_path(token) for token in command.tokens if token.endswith(".py")]
        script = next((s for s in scripts if s is not None and index.has_file(s)), None)
        if script is not None:
            return _CommandTarget(*main_module_handler(index, script))
        if scripts:
            missing = "no script it names exists in the snapshot"
    named = (command.snapshot_path(token) for token in command.tokens)
    file = command.launcher or next((f for f in named if f and index.has_file(f)), None)
    if file is None:
        return missing or "it runs no file of the snapshot"
    return _CommandTarget.at_file(file)


def _module_target(index: PyIndex, subcommands: _Subcommands, command: CommandLine, source_file: str,
                   module: str, arguments: Sequence[str]) -> _CommandTarget | str:
    """Where `python -m <module> <arguments>` lands, or the reason it lands nowhere."""
    roots = module_search_roots(command, source_file, index.repo_root(source_file))
    module_file = resolve_python_m(module, roots, index)
    if module_file is None:
        return f"python -m {module}: no such module under {', '.join(repr(r) for r in roots)}"
    positional = first_positional(arguments)
    subcommand = subcommands.find(module_file, positional) if positional else None
    if subcommand is None:
        return _CommandTarget(*main_module_handler(index, module_file))
    if subcommand.handler is None:
        return _CommandTarget(subcommand.location, None)
    return _CommandTarget(_handler_location(subcommand.handler), subcommand.handler)


def _command_entry(kind: EntryKind, name: str, path: str, line: int, invocation: str,
                   target: _CommandTarget) -> Entry:
    """An entry for the command `invocation` at `path:line`, with one known edge to where it lands."""
    location = Location(path=path, line=line)
    return Entry(kind=kind, name=name, location=location, handler=target.handler, invocation=invocation,
                 known_edges=[KnownEdge(from_=location, to=target.to)], also_invoked_by=[])


# --- merging ----------------------------------------------------------------------------------


def merge_entries(entries: Sequence[Entry]) -> list[Entry]:
    """One entry per handler `def`/`class`, the others that share it listed in its `also_invoked_by`.

    The kept entry has the best `KIND_RANK`, then the smallest location. An
    entry whose handler is null or has no symbol (a launcher, a non-Python file)
    merges only with entries of identical kind and location. Groups keep the
    order of their first entry.
    """
    groups: dict[tuple[str | int, ...], list[Entry]] = {}
    for entry in entries:
        groups.setdefault(_merge_key(entry), []).append(entry)
    return [_merged(group) for group in groups.values()]


def _merge_key(entry: Entry) -> tuple[str | int, ...]:
    handler = entry.handler
    if handler is not None and handler.symbol is not None:
        return "handler", handler.path, handler.line
    return "declaration", entry.kind, entry.location.path, entry.location.line


def _merged(group: list[Entry]) -> Entry:
    if len(group) == 1:
        return group[0]
    kept, *others = sorted(group, key=lambda e: (KIND_RANK[OUTPUT_KIND[e.kind]], e.location.path,
                                                 e.location.line, e.name))
    invokers = set(kept.also_invoked_by)
    for other in others:
        invokers.add(EntryInvoker(kind=other.kind, name=other.name, location=other.location))
        invokers.update(other.also_invoked_by)
    edges = {edge for entry in group for edge in entry.known_edges}
    return kept.model_copy(update={
        "also_invoked_by": sorted(invokers, key=lambda i: (i.location.path, i.location.line, i.kind, i.name)),
        "known_edges": sorted(edges, key=lambda e: (e.from_.path, e.from_.line, e.to.path, e.to.line)),
    })
