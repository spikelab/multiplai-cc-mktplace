"""End-to-end test listing: the snapshot tests that statically invoke each kept path's entry.

Static only. Test files (`test_*.py`, `*_test.py`) are parsed with `ast`, and
nothing is imported, collected, run or spawned; no model is called. Directories
named `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and
`site-packages` are skipped; `build/` and `dist/`, which discovery prunes, are
searched. A test file that cannot be decoded or parsed is skipped with reason
`decode_error` or `syntax_error`.

Tests. Module-level `test*` functions, and `test*` methods of module-level
`Test*` classes. Calls inside a nested `def` or `class` do not count.

Invocations. Every call in a test body that can run an entry is reduced to a
key (what it reaches) and its literal argv. An entry, and each of its
`also_invoked_by` invokers, is reduced to the keys that reach it and the words
the argv must hold, in order:

- `direct_call`: a callee that resolves, through the test's imports, to the
  entry's handler `def`.
- `cli_argv`: for an argparse subcommand, a callee that resolves to a function
  of the parser's module whose body calls `.parse_args(` or
  `.parse_known_args(`, or `<f>().parse_args(argv)` with `f` a function of that
  module whose result is handed to a handler (an argument of a call to a
  function of the snapshot, directly or through the name it is assigned to, or
  `<name>.func(...)`): a bare parse runs no handler. Argv must hold the
  subcommand words.
- `cli_runner`: `<runner>.invoke(<app>, <args>)`, `app` resolving to a click
  group or command, or a typer app, of the command's module; args must hold the
  command words `app` does not supply itself. A typer app reaches the commands
  of the apps mounted below it; a callback runs whenever its app does; and an
  app with one command, no callback and no sub-app runs it with no name.
- `subprocess`: `subprocess.run`, `call`, `check_call`, `check_output` or
  `Popen` of a command: `python -m <module> ...` (`sys.executable`, `python`,
  `python3`), `python <script>.py ...`, or a console script by name. It reaches
  a subcommand's module with its words, a `main_module`, a `[project.scripts]`
  name, and a `SKILL.md` or hook command line (its first positional word must
  then be present too). A program is run by a console script naming a function
  of its parser module, or its click or typer app, and by `python -m` of a module
  whose `main_module` handler is such a function: these reach its subcommands
  with the same words.
- `runpy`: `runpy.run_module("<module>", ...)`, reaching a `main_module`.

Argv is literal when it is a list or tuple display, whose elements that are not
string literals are unknown words (`str(tmp_path)`), or, for a command or click
args, a string. Argv holding `-h`, `--help` or `--version` never matches:
argparse exits before any handler runs.

Helpers (D15). A function of the test module, of a `conftest.py` in the test's
directory or above, or of a non-test module under the test's `tests/` tree,
called with literal arguments, is inlined up to two levels deep: a match form
in its body whose argv uses a parameter (`p`, `[*prefix, *p]`, `["x", *p]`,
`list(p)`) is matched with the literal substituted, at the test's call line.

Patches (see `e2e_patches`). A test is not listed for a path when it patches
the entry's handler, at its defining module or under the name a known-edge
`from` line looks up, or the function holding a known-edge `from` line: the
handler never runs. A listed test moves from `e2e` to `mocks_hop_2` when it
patches the symbol at hop 2 of the retained hops, at its defining module or
under a name hop 1's lines look up; a path with one retained hop has no hop 2.

Records. One per test per path: its earliest invocation line, ties broken in
the order `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`;
sorted by `(file, line, node_id)`. A mocked record adds the earliest hop-2
patch, which may be in a fixture or `conftest.py`.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import logging
import posixpath
import shlex
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import get_args

from .commands import (
    first_positional,
    hook_commands,
    parse_command_line,
    python_module,
    python_script,
    shell_commands,
)
from .e2e_patches import (
    FUNCTION_DEFS,
    FunctionNode,
    Patch,
    PatchScan,
    Symbol,
    body_nodes,
    call_argument,
)
from .entries import (
    ConsoleScript,
    Entry,
    cli_routes,
    console_scripts,
    main_module_handler,
)
from .paths import (
    E2EListing,
    E2ETest,
    Handler,
    ListingSkip,
    Location,
    MatchKind,
    MockedE2ETest,
    PathHop,
    SkipReason,
    TracedPath,
)
from .pyindex import Def, PyIndex, dotted_reference, snapshot_files

log = logging.getLogger(__name__)

LISTING_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}
)
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
HELPER_DEPTH = 2
MATCH_ORDER: tuple[MatchKind, ...] = get_args(MatchKind)

_EXCLUDED_FLAGS = frozenset({"-h", "--help", "--version"})
_PARSE_METHODS = frozenset({"parse_args", "parse_known_args"})
_SUBPROCESS_CALLS = frozenset(
    f"subprocess.{name}"
    for name in ("run", "call", "check_call", "check_output", "Popen")
)
_RUN_MODULE = "runpy.run_module"
# An element of a literal argv that is not a string literal.
_UNKNOWN = ""

Argv = tuple[str, ...]
# A literal argument: a list or tuple display's words, or a string.
_Literal = Argv | str
_Env = Mapping[str, _Literal]
# What an invocation reaches: ("def", path, line) | ("program", parser module path) | ("app", path, name)
# | ("module", dotted name) | ("script", path) | ("command", console script name).
_Key = tuple[str | int, ...]
_Reached = tuple[_Key, Argv]
# The snapshot path a script word of a command names, or None outside the snapshot.
_PathOf = Callable[[str], str | None]


@dataclass(frozen=True)
class Invocation:
    """A call a test makes that can run code: what it reaches, the literal argv, and the test's line."""

    match: MatchKind
    key: _Key
    argv: Argv
    line: int


@dataclass(frozen=True)
class E2ECandidate:
    """A test function, the invocations its body makes (helpers inlined), and the patches applied to it."""

    node_id: str
    file: str
    line: int
    invocations: tuple[Invocation, ...]
    patches: tuple[Patch, ...]


class E2EIndex:
    """The test functions of a snapshot and their invocations, parsed once and matched against every entry."""

    def __init__(
        self,
        index: PyIndex,
        tests: Sequence[E2ECandidate],
        skips: Sequence[ListingSkip],
        scripts: Sequence[ConsoleScript] = (),
    ):
        self.index = index
        self.tests = tuple(tests)
        self.scripts = tuple(scripts)
        self._skips = tuple(sorted(skips, key=lambda s: s.file))
        self._by_id = {t.node_id: t for t in self.tests}

    @classmethod
    def build(cls, root: Path, index: PyIndex) -> E2EIndex:
        """Parse the test files under `root`. `index` is the snapshot's `PyIndex`; files it prunes are added."""
        root = Path(root)
        py_files = [
            rel
            for rel in snapshot_files(root, LISTING_SKIP_DIRS)
            if rel.endswith(".py")
        ]
        unindexed = [rel for rel in py_files if index.line_count(rel) is None]
        if unindexed:
            index = index.including(root, unindexed)
        functions = _Functions(index)
        patch_scan = PatchScan(index)
        tests: list[E2ECandidate] = []
        skips: list[ListingSkip] = []
        for rel in filter(is_test_file, py_files):
            tree = index.tree(rel)
            if tree is None:
                skips.append(
                    ListingSkip(file=rel, reason=_unreadable_reason(root / rel))
                )
            else:
                tests += _TestScan(index, functions, patch_scan, rel).candidates(tree)
        log.debug(
            "listed %d tests under %s (%d test files skipped)",
            len(tests),
            root,
            len(skips),
        )
        return cls(index, tests, skips, console_scripts(root, index))

    def skips(self) -> list[ListingSkip]:
        """The test files that could not be decoded or parsed, in path order."""
        return list(self._skips)

    def candidate(self, node_id: str) -> E2ECandidate:
        """The test with `node_id`; KeyError when the snapshot has none."""
        return self._by_id[node_id]


def list_e2e_tests(
    root: Path,
    paths: Sequence[TracedPath],
    entries: Mapping[str, Entry],
    *,
    index: PyIndex,
) -> tuple[dict[str, E2EListing], list[ListingSkip]]:
    """The tests of each path, keyed by path id, and the test files skipped. `entries` maps a path id to the
    entry the path was traced from.
    """
    e2e_index = E2EIndex.build(root, index)
    listings = {
        path.id: _split_on_hop_2(
            e2e_index,
            match_invocations(e2e_index, entries[path.id], root=root),
            path.hops,
        )
        for path in paths
    }
    return listings, e2e_index.skips()


def match_invocations(
    e2e_index: E2EIndex, entry: Entry, *, root: Path
) -> list[E2ETest]:
    """The tests that invoke `entry` or one of its invokers and leave its handler in place, one record each,
    sorted by (file, line, node_id).
    """
    index = e2e_index.index
    targets = _Targets(Path(root), index, e2e_index.scripts).of(entry)
    bypassed = _bypassed_by_patching(index, entry)
    records = []
    for test in e2e_index.tests:
        if any(symbol.patched_by(index, test.patches) for symbol in bypassed):
            continue
        hits = [i for i in test.invocations if any(t.reached_by(i) for t in targets)]
        if hits:
            first = min(hits, key=lambda i: (i.line, MATCH_ORDER.index(i.match)))
            records.append(
                E2ETest(
                    node_id=test.node_id,
                    file=test.file,
                    line=test.line,
                    match=first.match,
                    invocation_line=first.line,
                )
            )
    return sorted(records, key=lambda r: (r.file, r.line, r.node_id))


def applied_patches(
    e2e_index: E2EIndex, node_id: str, *, root: Path
) -> list[tuple[str, str, int]]:
    """`(dotted target, file, line)` of each patch applied to the test `node_id`, by its decorators and body or
    by the fixtures it uses, sorted by (file, line, target). `root` is the snapshot root `e2e_index` was built
    from; every file is relative to it.
    """
    return [(p.target, p.file, p.line) for p in e2e_index.candidate(node_id).patches]


def is_test_file(path: str) -> bool:
    """`test_*.py` or `*_test.py`: the files test listing parses for tests."""
    name = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in TEST_FILE_PATTERNS)


def _unreadable_reason(full: Path) -> SkipReason:
    """Why a test file the index could not parse is skipped."""
    try:
        importlib.util.decode_source(full.read_bytes())
    # decode_source raises SyntaxError for an undecodable line 1 or 2, or a bad encoding cookie.
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError):
        return "decode_error"
    return "syntax_error"


# --- what patches move or drop ------------------------------------------------------------------


def _split_on_hop_2(
    e2e_index: E2EIndex, records: Sequence[E2ETest], hops: Sequence[PathHop]
) -> E2EListing:
    """The records, those of tests that patch the symbol at hop 2 moved to `mocks_hop_2`."""
    index = e2e_index.index
    hop_2 = _hop_2_symbol(index, hops)
    e2e: list[E2ETest] = []
    mocked: list[MockedE2ETest] = []
    for record in records:
        patch = (
            None
            if hop_2 is None
            else hop_2.patched_by(index, e2e_index.candidate(record.node_id).patches)
        )
        if patch is None:
            e2e.append(record)
        else:
            mocked.append(
                MockedE2ETest(
                    **record.model_dump(), patch_file=patch.file, patch_line=patch.line
                )
            )
    return E2EListing(e2e=e2e, mocks_hop_2=mocked)


def _hop_2_symbol(index: PyIndex, hops: Sequence[PathHop]) -> Symbol | None:
    """The function or class hop 2 cites, looked up under the names hop 1's lines see; None with no hop 2."""
    if len(hops) < 2:
        return None
    first, second = hops[0], hops[1]
    cited = _cited_def(index, second)
    if cited is None:
        return None
    # Lines in one scope see the same names: one lookup per innermost definition.
    lookups: dict[Def | None, int] = {}
    for line in range(first.line_start, first.line_end + 1):
        lookups.setdefault(index.enclosing_def(first.path, line), line)
    return Symbol.of(
        cited, second.path, ((first.path, line) for line in lookups.values())
    )


def _cited_def(index: PyIndex, hop: PathHop) -> Def | None:
    """The last `def` or `class`, in source order, named like the last part of the hop's called symbol and
    either starting within the hop's cited lines or spanning their first line; None when there is none.
    """
    name = hop.symbol_called.rpartition(".")[2]
    cited = [
        d
        for d in index.defs(hop.path)
        if d.name == name
        and (
            hop.line_start <= d.start <= hop.line_end
            or d.start <= hop.line_start <= d.end
        )
    ]
    return cited[-1] if cited else None


def _bypassed_by_patching(index: PyIndex, entry: Entry) -> list[Symbol]:
    """What a test must not patch for the entry's handler to run: the handler, under the names its known-edge
    `from` lines look up too, and each function holding a known-edge `from` line.
    """
    edge_lines = [(edge.from_.path, edge.from_.line) for edge in entry.known_edges]
    symbols = []
    handler = entry.handler
    if handler is not None and handler.symbol is not None:
        definition = index.handler_def(handler)
        if definition is not None:
            symbols.append(Symbol.of(definition, handler.path, edge_lines))
    for path, line in edge_lines:
        holder = index.enclosing_function(path, line)
        if holder is not None:
            symbols.append(Symbol.of(holder, path))
    return symbols


# --- what reaches an entry ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """A way to reach an entry: an invocation with `key` whose argv holds `words`, in order."""

    key: _Key
    words: Argv = ()

    def reached_by(self, invocation: Invocation) -> bool:
        return invocation.key == self.key and _in_order(self.words, invocation.argv)


def _is_target_in(
    target_path: str, target_name: str | None, path: str, name: str | None
) -> bool:
    """Whether a target is in `path`, and is called `name` when one is given."""
    return target_path == path and (name is None or target_name == name)


def _in_order(words: Argv, argv: Argv) -> bool:
    """Whether `words` occur in `argv` in order, not necessarily adjacent."""
    remaining = iter(argv)
    return all(word in remaining for word in words)


class _Targets:
    """The targets of an entry: its handler, and the ways its own declaration and each invoker are run."""

    def __init__(self, root: Path, index: PyIndex, scripts: Sequence[ConsoleScript]):
        self._root = root
        self._index = index
        self._scripts = scripts

    def of(self, entry: Entry) -> frozenset[_Target]:
        found: set[_Target] = set()
        if entry.handler is not None and entry.handler.symbol is not None:
            found.add(_Target(("def", entry.handler.path, entry.handler.line)))
        found |= self._declared(
            entry.kind, entry.name, entry.location, entry.invocation
        )
        for invoker in entry.also_invoked_by:
            found |= self._declared(invoker.kind, invoker.name, invoker.location, None)
        return frozenset(found)

    def _declared(
        self, kind: str, name: str, location: Location, invocation: str | None
    ) -> set[_Target]:
        """The targets of one declaration; `invocation` is the command text when the caller has it."""
        words = tuple(name.split(" "))
        match kind:
            case "argparse_subcommand":
                program = location.path
                launchers = [
                    ("program", program),
                    *(("module", m) for m in self._program_modules(program)),
                    *self._scripts_running(program),
                ]
                return {_Target(key, words) for key in launchers}
            case "click_command" | "typer_command":
                return self._app_targets(location.path, name)
            case "main_module":
                return {_Target(("module", name)), _Target(("script", location.path))}
            case "project_script":
                return {_Target(("command", name))}
            case "skill_command" | "hook_command":
                commands = (
                    [invocation]
                    if invocation is not None
                    else self._commands_at(kind, location)
                )
                return {
                    t
                    for command in commands
                    if (t := self._command_target(command, location.path))
                }
            case _:
                return set()

    def _program_modules(self, program: str) -> list[str]:
        """The modules `python -m` runs the parser module `program` as: its own, and its package's when the
        package's `__main__.py` hands over to a function of `program`.
        """
        own = self._index.module_name(program)
        own_modules = [own.removesuffix(".__main__")] if own else []
        return list(dict.fromkeys([*own_modules, *self._modules_running(program)]))

    def _modules_running(self, path: str, name: str | None = None) -> list[str]:
        """The modules, `path`'s own or its package's, whose `main_module` handler is a function of `path`, the
        one called `name` when given.
        """
        package_main = posixpath.join(posixpath.dirname(path), "__main__.py")
        modules = []
        for main in dict.fromkeys((path, package_main)):
            module = self._index.module_name(main)
            _, handler = main_module_handler(self._index, main)
            if (
                module
                and handler is not None
                and _is_target_in(handler.path, handler.symbol, path, name)
            ):
                modules.append(module.removesuffix(".__main__"))
        return modules

    def _scripts_running(self, path: str, name: str | None = None) -> list[_Key]:
        """The console scripts whose target is in `path`, the one called `name` when given. A target is the `def`
        its name means, else the module-level name itself.
        """
        keys: list[_Key] = []
        for script in self._scripts:
            handler = script.handler
            target = (
                (handler.path, handler.symbol)
                if handler is not None
                else (script.module_file, script.attribute)
            )
            if _is_target_in(*target, path, name):
                keys.append(("command", script.name))
        return keys

    def _app_targets(self, path: str, command: str) -> set[_Target]:
        """Each way to run the click or typer command `command` of the module through one of its apps: a test
        runner invoking the app, a console script naming it, or `python -m` of a module that calls it, with the
        command name words the app leaves to its arguments.
        """
        tree = self._index.tree(path)
        found: set[_Target] = set()
        for route in cli_routes(path, tree) if tree is not None else ():
            if route.command == command:
                launchers = [
                    ("app", path, route.app),
                    *self._scripts_running(path, route.app),
                    *(("module", m) for m in self._modules_running(path, route.app)),
                ]
                found |= {_Target(key, route.words) for key in launchers}
        return found

    def _commands_at(self, kind: str, location: Location) -> list[str]:
        """The command text of a `SKILL.md` or hook invoker, re-read at its location."""
        try:
            text = (self._root / location.path).read_text(encoding="utf-8-sig")
            if kind == "hook_command":
                return [
                    hook.command
                    for hook in hook_commands(text)
                    if hook.line == location.line
                ]
            return [
                command
                for line, command in shell_commands(text)
                if line == location.line
            ]
        except (OSError, ValueError) as e:
            log.debug(
                "cannot re-read the command at %s:%d: %s",
                location.path,
                location.line,
                e,
            )
            return []

    def _command_target(self, command: str, source_file: str) -> _Target | None:
        """What running `command`, as written in `source_file`, looks like; its first positional must be passed."""
        try:
            line = parse_command_line(
                command, source_file=source_file, root=self._root, index=self._index
            )
        except ValueError:
            return None
        reached = _reached_command(line.tokens, line.snapshot_path)
        if reached is None:
            return None
        key, arguments = reached
        positional = first_positional(arguments)
        return _Target(key, (positional,) if positional else ())


def _reached_command(tokens: Sequence[str], script_path: _PathOf) -> _Reached | None:
    """What a command line runs, and its arguments: a `python -m` module, a `.py` script (by snapshot path), or
    a program by name.
    """
    if (module := python_module(tokens)) is not None:
        return ("module", module[0]), tuple(module[1])
    if (script := python_script(tokens)) is not None:
        path = script_path(script[0])
        return (("script", path), tuple(script[1])) if path is not None else None
    if tokens and tokens[0]:
        return ("command", posixpath.basename(tokens[0])), tuple(tokens[1:])
    return None


# --- what a test invokes ------------------------------------------------------------------------


class _ScriptPath:
    """The snapshot path a script word in a test's command names: relative to the test's repository root."""

    def __init__(self, base: str):
        self._base = base

    def __call__(self, token: str) -> str | None:
        if token.startswith("/"):
            return None
        path = posixpath.normpath(posixpath.join(self._base, token))
        return None if path == ".." or path.startswith("../") else path


class _Functions:
    """The function `def`s handlers resolve to, looked up once per snapshot."""

    def __init__(self, index: PyIndex):
        self._index = index
        self._defs: dict[tuple[str, int], FunctionNode | None] = {}

    def of(self, handler: Handler) -> FunctionNode | None:
        """The `def` of the handler, matched by line and name; None when it is not a function."""
        key = (handler.path, handler.line)
        if key not in self._defs:
            tree = self._index.tree(handler.path)
            nodes = ast.walk(tree) if tree is not None else ()
            self._defs[key] = next(
                (
                    n
                    for n in nodes
                    if isinstance(n, FUNCTION_DEFS)
                    and n.lineno == handler.line
                    and n.name == handler.symbol
                ),
                None,
            )
        return self._defs[key]

    def first_parameter(self, handler: Handler) -> str | None:
        """The name of the function's first positional parameter, the one its argv is passed as."""
        func = self.of(handler)
        positional = func.args.posonlyargs + func.args.args if func is not None else []
        return positional[0].arg if positional else None

    def parses_args(self, handler: Handler) -> bool:
        """Whether the function's body calls `.parse_args(` or `.parse_known_args(`."""
        func = self.of(handler)
        return func is not None and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _PARSE_METHODS
            for n in ast.walk(func)
        )


# What a literal argv of one match form reaches.
_Reach = Callable[[_Literal], _Reached | None]
# A match form found in a call: its kind, its argv expression (None when it passes none), and what it reaches.
_Form = tuple[MatchKind, ast.expr | None, _Reach]


def _argv_reaches(key: _Key, value: _Literal) -> _Reached | None:
    """A program's argv: a list or tuple; argparse would read a string as single characters."""
    return (key, value) if isinstance(value, tuple) else None


def _args_reaches(key: _Key, value: _Literal) -> _Reached | None:
    """A click runner's args: a list or tuple, or a string it splits as a shell would."""
    words = _shell_words(value)
    return (key, words) if words is not None else None


def _command_reaches(script_path: _PathOf, value: _Literal) -> _Reached | None:
    """A subprocess command: a list or tuple, or a string split as a shell would."""
    words = _shell_words(value)
    return _reached_command(words, script_path) if words is not None else None


def _module_reaches(value: _Literal) -> _Reached | None:
    """`runpy.run_module`'s module name: a string."""
    return (("module", value), ()) if isinstance(value, str) else None


def _shell_words(value: _Literal) -> Argv | None:
    """The words of a list or tuple as they are, or of a string split as a shell would; None when unsplittable."""
    if isinstance(value, tuple):
        return value
    try:
        return tuple(shlex.split(value))
    except ValueError:
        return None


class _TestScan:
    """The test functions of one test file, and the invocations of each."""

    def __init__(
        self,
        index: PyIndex,
        functions: _Functions,
        patch_scan: PatchScan,
        test_file: str,
    ):
        self._index = index
        self._functions = functions
        self._patch_scan = patch_scan
        self._file = test_file
        self._tests_tree = _tests_tree(test_file)
        self._script_path = _ScriptPath(index.repo_root(test_file))

    def candidates(self, tree: ast.Module) -> list[E2ECandidate]:
        # A later `def` of the same name replaces the earlier one, as in Python.
        found: dict[str, E2ECandidate] = {}
        for name, func, owner in _test_functions(tree):
            invocations = tuple(self._body(func, self._file, {}, 0))
            found[name] = E2ECandidate(
                node_id=f"{self._file}::{name}",
                file=self._file,
                line=func.lineno,
                invocations=invocations,
                patches=self._patch_scan.of_test(self._file, func, owner),
            )
        return list(found.values())

    def _body(
        self, func: FunctionNode, path: str, env: _Env, depth: int
    ) -> Iterator[Invocation]:
        """The invocations of a test body (depth 0) or of a helper inlined at `depth` with parameters `env`."""
        handed_on = self._handed_on(func, path)
        for call in _calls(func):
            yield from self._call(call, path, env, depth, call in handed_on)

    def _handed_on(self, func: FunctionNode, path: str) -> set[ast.Call]:
        """The calls of a body whose result reaches a handler: an argument of a call to a function of the
        snapshot, directly or through a name it is assigned to, or a name whose `.func(...)` is called.
        """
        assigned: dict[str, list[ast.Call]] = {}
        for node in body_nodes(func):
            if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(
                node.value, ast.Call
            ):
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                ):
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(node.value)
        handed: set[ast.Call] = set()
        for call in _calls(func):
            callee = call.func
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr == "func"
                and isinstance(callee.value, ast.Name)
            ):
                handed.update(assigned.get(callee.value.id, ()))
            reference = dotted_reference(callee)
            if (
                reference is None
                or self._index.resolve_name(path, call.lineno, reference) is None
            ):
                continue
            for argument in [*call.args, *(k.value for k in call.keywords)]:
                if isinstance(argument, ast.Call):
                    handed.add(argument)
                elif isinstance(argument, ast.Name):
                    handed.update(assigned.get(argument.id, ()))
        return handed

    def _call(
        self, call: ast.Call, path: str, env: _Env, depth: int, handed_on: bool
    ) -> Iterator[Invocation]:
        line = call.lineno
        reference = dotted_reference(call.func)
        handler = self._index.resolve_name(path, line, reference) if reference else None
        if depth == 0 and handler is not None:
            yield Invocation(
                "direct_call", ("def", handler.path, handler.line), (), line
            )
        for match, expr, reach in self._forms(
            call, path, reference, handler, handed_on
        ):
            # Inside a helper, only a form fed by a parameter is the test's invocation.
            if depth and not _uses(expr, env):
                continue
            value = self._literal(expr, path, env)
            reached = reach(value) if value is not None else None
            if reached is not None and _EXCLUDED_FLAGS.isdisjoint(reached[1]):
                yield Invocation(match, reached[0], reached[1], line)
        if (
            handler is not None
            and depth < HELPER_DEPTH
            and self._is_helper(handler.path)
        ):
            yield from self._inlined(call, handler, path, env, depth)

    def _forms(
        self,
        call: ast.Call,
        path: str,
        reference: str | None,
        handler: Handler | None,
        handed_on: bool,
    ) -> Iterator[_Form]:
        """The match forms `call` has, whatever its argv turns out to be. A builder parse counts only when its
        result is `handed_on` to a handler: a bare parse runs none.
        """
        line = call.lineno
        if handler is not None and self._functions.parses_args(handler):
            yield (
                "cli_argv",
                call_argument(call, 0, self._functions.first_parameter(handler)),
                partial(_argv_reaches, ("program", handler.path)),
            )
        callee = call.func
        if (
            handed_on
            and isinstance(callee, ast.Attribute)
            and isinstance(callee.value, ast.Call)
        ):
            builder_ref = (
                dotted_reference(callee.value.func)
                if callee.attr in _PARSE_METHODS
                else None
            )
            builder = (
                self._index.resolve_name(path, line, builder_ref)
                if builder_ref
                else None
            )
            if builder is not None:
                yield (
                    "cli_argv",
                    call_argument(call, 0, "args"),
                    partial(_argv_reaches, ("program", builder.path)),
                )
        if isinstance(callee, ast.Attribute) and callee.attr == "invoke":
            app = self._app(path, line, call_argument(call, 0, "cli"))
            if app is not None:
                yield (
                    "cli_runner",
                    call_argument(call, 1, "args"),
                    partial(_args_reaches, app),
                )
        qualified = (
            self._index.qualified_name(path, line, reference) if reference else None
        )
        if qualified in _SUBPROCESS_CALLS:
            yield (
                "subprocess",
                call_argument(call, 0, "args"),
                partial(_command_reaches, self._script_path),
            )
        elif qualified == _RUN_MODULE:
            yield "runpy", call_argument(call, 0, "mod_name"), _module_reaches

    def _app(self, path: str, line: int, expr: ast.expr | None) -> _Key | None:
        """The click or typer app `expr` names: a function, or a module-level name of the module it comes from."""
        reference = dotted_reference(expr) if expr is not None else None
        if reference is None:
            return None
        handler = self._index.resolve_name(path, line, reference)
        if handler is not None and handler.symbol is not None:
            return "app", handler.path, handler.symbol
        module, _, name = (
            self._index.qualified_name(path, line, reference) or ""
        ).rpartition(".")
        module_file = self._index.resolve_module(path, module) if module else None
        return ("app", module_file, name) if module_file is not None else None

    def _literal(self, expr: ast.expr | None, path: str, env: _Env) -> _Literal | None:
        """The literal value of an argv expression; None when it is not literal. No argv is an empty one."""
        match expr:
            case None:
                return ()
            case ast.Constant(value=str() as text):
                return text
            case ast.Name(id=name) if name in env:
                return env[name]
            case ast.List(elts=elements) | ast.Tuple(elts=elements):
                return self._words(elements, path, env)
            case ast.Call(
                func=ast.Name(id="list" | "tuple"), args=[inner], keywords=[]
            ):
                value = self._literal(inner, path, env)
                return value if isinstance(value, tuple) else None
        return None

    def _words(self, elements: list[ast.expr], path: str, env: _Env) -> Argv | None:
        words: list[str] = []
        for element in elements:
            if isinstance(element, ast.Starred):
                spread = self._literal(element.value, path, env)
                if not isinstance(spread, tuple):
                    return None
                words += spread
            elif isinstance(element, ast.Constant) and isinstance(element.value, str):
                words.append(element.value)
            elif self._is_executable(element, path):
                words.append("python")
            else:
                words.append(_UNKNOWN)
        return tuple(words)

    def _is_executable(self, expr: ast.expr, path: str) -> bool:
        reference = dotted_reference(expr)
        return (
            reference is not None
            and self._index.qualified_name(path, expr.lineno, reference)
            == "sys.executable"
        )

    def _inlined(
        self, call: ast.Call, helper: Handler, path: str, env: _Env, depth: int
    ) -> Iterator[Invocation]:
        """The invocations of a helper called with literal arguments, at the call's line."""
        func = self._functions.of(helper)
        bound = self._bind(func, call, path, env) if func is not None else {}
        if not bound:
            return
        for invocation in self._body(func, helper.path, bound, depth + 1):
            yield replace(invocation, line=call.lineno)

    def _bind(
        self, func: FunctionNode, call: ast.Call, path: str, env: _Env
    ) -> dict[str, _Literal]:
        """The helper's parameters that `call` passes a literal to."""
        positional = [a.arg for a in func.args.posonlyargs + func.args.args]
        named = {*positional, *(a.arg for a in func.args.kwonlyargs)}
        pairs: list[tuple[str, ast.expr]] = []
        for param, arg in zip(positional, call.args):
            if isinstance(arg, ast.Starred):
                break
            pairs.append((param, arg))
        pairs += [(k.arg, k.value) for k in call.keywords if k.arg in named]
        bound = {}
        for param, arg in pairs:
            value = self._literal(arg, path, env)
            if value is not None:
                bound[param] = value
        return bound

    def _is_helper(self, path: str) -> bool:
        """The test module, a `conftest.py` at or above the test's directory, or a non-test module of its
        `tests/` tree.
        """
        if path == self._file:
            return True
        directory, name = posixpath.split(path)
        if name == "conftest.py":
            return not directory or self._file.startswith(directory + "/")
        return (
            self._tests_tree is not None
            and path.startswith(self._tests_tree + "/")
            and not is_test_file(path)
        )


def _test_functions(
    tree: ast.Module,
) -> Iterator[tuple[str, FunctionNode, ast.ClassDef | None]]:
    """`(qualified name, def, class)` of each module-level `test*` function (class None) and `test*` method of
    a `Test*` class.
    """
    for node in tree.body:
        if isinstance(node, FUNCTION_DEFS) and node.name.startswith("test"):
            yield node.name, node, None
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, FUNCTION_DEFS) and item.name.startswith("test"):
                    yield f"{node.name}::{item.name}", item, node


def _calls(func: FunctionNode) -> Iterator[ast.Call]:
    """The calls of a function body, outside the `def`s and classes nested in it."""
    return (node for node in body_nodes(func) if isinstance(node, ast.Call))


def _uses(expr: ast.expr | None, env: _Env) -> bool:
    """Whether `expr` reads one of the helper parameters bound in `env`."""
    return expr is not None and any(
        isinstance(n, ast.Name) and n.id in env for n in ast.walk(expr)
    )


def _tests_tree(test_file: str) -> str | None:
    """The nearest directory named `tests` holding the test file."""
    parts = test_file.split("/")[:-1]
    for end in range(len(parts), 0, -1):
        if parts[end - 1] == "tests":
            return "/".join(parts[:end])
    return None
