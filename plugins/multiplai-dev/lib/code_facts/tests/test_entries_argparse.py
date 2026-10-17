"""discover_entries: argparse subcommands, their handlers and known edges, walk rules, and ordering."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import code_facts
from conftest import line_of, make_tree, sentinel_prelude

REPO_ROOT = Path(__file__).resolve().parents[5]
LIVE_PR_SCRIPTS = REPO_ROOT / "plugins/multiplai-dev/skills/pr/scripts"


def entries_mod():
    return importlib.import_module("code_facts.entries")


def discover(root, seeds=(), **kw):
    return entries_mod().discover_entries(root, seeds, **kw)


def argparse_entries(disc, path=None):
    return [e for e in disc.entries
            if e.kind == "argparse_subcommand" and (path is None or e.location.path == path)]


def by_name(entries):
    return {e.name: e for e in entries}


def edges(entry):
    return {(k.from_.path, k.from_.line, k.to.path, k.to.line) for k in entry.known_edges}


FLAT = """\
import argparse


class Registry:
    def add_parser(self, name):
        return name


def build():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("open")
    sub.add_parser("close", help="close it")
    db = sub.add_parser("db")
    db_sub = db.add_subparsers()
    db_sub.add_parser("migrate")
    Registry().add_parser("not-a-subcommand")
    for name in ("a", "b"):
        sub.add_parser(name)
    return parser
"""


class TestArgparseSubcommands:
    def test_flat_and_nested_names_at_their_add_parser_lines(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": FLAT})
        found = by_name(argparse_entries(discover(root)))
        assert set(found) == {"open", "close", "db", "db migrate"}
        for name, needle in [("open", '"open"'), ("close", '"close"'), ("db", '"db"'), ("db migrate", '"migrate"')]:
            e = found[name]
            assert (e.location.path, e.location.line) == ("cli.py", line_of(FLAT, needle))
            assert e.handler is None and e.invocation is None
            assert e.known_edges == [] and e.also_invoked_by == []

    def test_non_literal_name_is_a_dynamic_name_diagnostic(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": FLAT})
        disc = discover(root)
        dynamic = [d for d in disc.diagnostics if d.reason == "dynamic_name"]
        assert [(d.path, d.line) for d in dynamic] == [("cli.py", line_of(FLAT, "sub.add_parser(name)"))]
        assert dynamic[0].detail


SET_DEFAULTS = """\
import argparse


def cmd_open(args):
    return 0


def build():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    p_open = sub.add_parser("open")
    p_open.set_defaults(func=cmd_open)
    return parser
"""

ARGS_FUNC = SET_DEFAULTS + """

def main(argv=None):
    args = build().parse_args(argv)
    return args.func(args)
"""


class TestSetDefaultsHandler:
    def test_handler_and_edge_from_set_defaults_without_dispatch(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": SET_DEFAULTS})
        e = by_name(argparse_entries(discover(root)))["open"]
        def_line = line_of(SET_DEFAULTS, "def cmd_open")
        assert (e.handler.path, e.handler.line, e.handler.symbol) == ("cli.py", def_line, "cmd_open")
        assert e.location.line == line_of(SET_DEFAULTS, '"open"')
        assert edges(e) == {("cli.py", line_of(SET_DEFAULTS, "set_defaults"), "cli.py", def_line)}

    def test_edge_from_args_func_dispatch_call(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": ARGS_FUNC})
        e = by_name(argparse_entries(discover(root)))["open"]
        assert edges(e) == {("cli.py", line_of(ARGS_FUNC, "args.func(args)"), "cli.py",
                             line_of(ARGS_FUNC, "def cmd_open"))}

    def test_out_of_repository_handler_is_null_with_one_diagnostic(self, tmp_path):
        src = SET_DEFAULTS.replace("import argparse\n", "import argparse\nfrom thirdparty.cli import ext\n")
        src = src.replace("func=cmd_open", "func=ext")
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["open"]
        assert e.handler is None and e.known_edges == []
        assert [(d.path, d.reason) for d in disc.diagnostics] == [("cli.py", "unresolved_handler")]

    def test_attribute_on_an_out_of_repository_module_is_diagnosed(self, tmp_path):
        src = SET_DEFAULTS.replace("import argparse\n", "import argparse\nimport thirdparty.cli\n")
        src = src.replace("func=cmd_open", "func=thirdparty.cli.ext")
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        disc = discover(root)
        assert by_name(argparse_entries(disc))["open"].handler is None
        assert [(d.line, d.reason) for d in disc.diagnostics] == [(line_of(src, '"open"'), "unresolved_handler")]
        assert "thirdparty.cli.ext" in disc.diagnostics[0].detail

    @pytest.mark.parametrize("binding", ["from . import commands", "import pkg.commands as commands"])
    def test_attribute_on_a_same_repository_module_resolves(self, tmp_path, binding):
        src = SET_DEFAULTS.replace("import argparse\n", f"import argparse\n{binding}\n")
        src = src.replace("func=cmd_open", "func=commands.cmd_open")
        commands = "\n\ndef cmd_open(args):\n    return 0\n"
        root = make_tree(tmp_path / "snap", {"pkg/__init__.py": "", "pkg/cli.py": src, "pkg/commands.py": commands})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["open"]
        assert (e.handler.path, e.handler.line, e.handler.symbol) == ("pkg/commands.py", 3, "cmd_open")
        assert edges(e) == {("pkg/cli.py", line_of(src, "set_defaults"), "pkg/commands.py", 3)}
        assert disc.diagnostics == []

    @pytest.mark.parametrize("func", ["lambda args: 0", "handler", "commands.missing", "commands.NOT_A_FUNCTION"])
    def test_unresolved_in_repository_handler_is_null_without_diagnostic(self, tmp_path, func):
        src = SET_DEFAULTS.replace("import argparse\n", "import argparse\nfrom . import commands\n")
        src = src.replace("    p_open.set_defaults(func=cmd_open)",
                          f"    handler = cmd_open\n    p_open.set_defaults(func={func})")
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": src, "pkg/commands.py": "NOT_A_FUNCTION = 1\n"})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["open"]
        assert e.handler is None and e.known_edges == []
        assert disc.diagnostics == []

    def test_nested_subcommand_handlers(self, tmp_path):
        src = """\
import argparse


def cmd_db(args):
    return 0


def cmd_migrate(args):
    return 1


def build():
    sub = argparse.ArgumentParser().add_subparsers()
    db = sub.add_parser("db")
    db.set_defaults(func=cmd_db)
    db_sub = db.add_subparsers()
    db_sub.add_parser("migrate").set_defaults(func=cmd_migrate)
"""
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        found = by_name(argparse_entries(discover(root)))
        assert {name: (e.location.line, e.handler.symbol, e.handler.line) for name, e in found.items()} == {
            "db": (line_of(src, '"db"'), "cmd_db", line_of(src, "def cmd_db")),
            "db migrate": (line_of(src, '"migrate"'), "cmd_migrate", line_of(src, "def cmd_migrate")),
        }


DICT_MAIN = """\
import argparse

from .commands import render_cmd, show_cmd

HANDLERS = {
    "render": render_cmd,
    "show": show_cmd,
}


def build():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("render")
    sub.add_parser("show")
    return parser
"""
DICT_DISPATCH = DICT_MAIN + """

def main(argv=None):
    args = build().parse_args(argv)
    return HANDLERS[args.command](args)
"""
DICT_COMMANDS = """\
def render_cmd(args):
    return 0


def show_cmd(args):
    return 0
"""


class TestDispatchDictHandler:
    def test_handler_imported_from_sibling_with_edge_from_dispatch_call(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": DICT_DISPATCH, "pkg/commands.py": DICT_COMMANDS})
        found = by_name(argparse_entries(discover(root)))
        dispatch = line_of(DICT_DISPATCH, "HANDLERS[args.command](args)")
        for name, symbol in [("render", "render_cmd"), ("show", "show_cmd")]:
            e = found[name]
            def_line = line_of(DICT_COMMANDS, f"def {symbol}")
            assert (e.handler.path, e.handler.line, e.handler.symbol) == ("pkg/commands.py", def_line, symbol)
            assert e.location == code_facts.Location(path="pkg/cli.py", line=line_of(DICT_DISPATCH, f'"{name}")'))
            assert edges(e) == {("pkg/cli.py", dispatch, "pkg/commands.py", def_line)}

    def test_edge_from_the_dict_entry_line_without_dispatch_call(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": DICT_MAIN, "pkg/commands.py": DICT_COMMANDS})
        e = by_name(argparse_entries(discover(root)))["show"]
        assert edges(e) == {("pkg/cli.py", line_of(DICT_MAIN, '"show": show_cmd'), "pkg/commands.py",
                             line_of(DICT_COMMANDS, "def show_cmd"))}

    def test_attribute_values_resolve_through_the_module(self, tmp_path):
        src = DICT_DISPATCH.replace("from .commands import render_cmd, show_cmd", "from . import commands")
        src = src.replace(": render_cmd", ": commands.render_cmd").replace(": show_cmd", ": commands.show_cmd")
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": src, "pkg/commands.py": DICT_COMMANDS})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["render"]
        def_line = line_of(DICT_COMMANDS, "def render_cmd")
        assert (e.handler.path, e.handler.line) == ("pkg/commands.py", def_line)
        assert edges(e) == {("pkg/cli.py", line_of(src, "HANDLERS[args.command](args)"), "pkg/commands.py", def_line)}
        assert disc.diagnostics == []

    def test_out_of_repository_value_is_diagnosed(self, tmp_path):
        src = DICT_MAIN.replace("from .commands import render_cmd, show_cmd", "from thirdparty import render_cmd")
        src = src.replace('    "show": show_cmd,\n', "")
        root = make_tree(tmp_path / "snap", {"pkg/__init__.py": "", "pkg/cli.py": src})
        disc = discover(root)
        found = by_name(argparse_entries(disc))
        assert found["render"].handler is None and found["show"].handler is None
        assert [(d.line, d.reason) for d in disc.diagnostics] == [(line_of(src, '"render")'), "unresolved_handler")]


HELPERS = """\
import argparse


def cmd_open(args):
    return 0


def cmd_migrate(args):
    return 1


def add_open(sub):
    p = sub.add_parser("open")
    p.set_defaults(func=cmd_open)


def add_migrate(subparsers):
    subparsers.add_parser("migrate").set_defaults(func=cmd_migrate)


def add_db(sub):
    db = sub.add_parser("db")
    add_migrate(subparsers=db.add_subparsers())


def build():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    add_open(sub)
    add_db(sub)
    return parser
"""
UNTRACED = """\
import argparse


class Cli:
    def __init__(self):
        self.sub = argparse.ArgumentParser().add_subparsers()

    def register(self):
        self.sub.add_parser("open")


def add_close(sub):
    sub.add_parser("close")
"""


class TestSubparsersPassedToHelpers:
    def test_helpers_given_subparsers_at_call_sites_declare_subcommands(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": HELPERS})
        disc = discover(root)
        found = by_name(argparse_entries(disc))
        assert {name: (e.location.line, e.handler and e.handler.symbol) for name, e in found.items()} == {
            "open": (line_of(HELPERS, '"open"'), "cmd_open"),
            "db": (line_of(HELPERS, '"db"'), None),
            "db migrate": (line_of(HELPERS, '"migrate"'), "cmd_migrate"),
        }
        assert disc.diagnostics == []

    def test_one_helper_under_two_parents_declares_one_subcommand_under_each(self, tmp_path):
        src = """\
import argparse


def add_list(sub):
    sub.add_parser("list")


def build():
    sub = argparse.ArgumentParser().add_subparsers()
    add_list(sub.add_parser("users").add_subparsers())
    add_list(sub.add_parser("groups").add_subparsers())
    add_list(sub.add_parser("groups").add_subparsers())
"""
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        found = argparse_entries(discover(root))
        # Both `list`s share one add_parser line and no handler, so they merge: one is the other's invoker.
        declared = [(e.name, e.location.line) for e in found] + [
            (i.name, i.location.line) for e in found for i in e.also_invoked_by]
        assert sorted(declared) == sorted([
            ("users", line_of(src, '"users"')), ("groups", line_of(src, '"groups"')),
            ("groups", line_of(src, '"groups"', 2)),
            ("users list", line_of(src, '"list"')), ("groups list", line_of(src, '"list"'))])

    def test_add_parser_on_an_untraced_receiver_is_an_unresolved_target(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": UNTRACED})
        disc = discover(root)
        assert argparse_entries(disc) == []
        assert [(d.path, d.line, d.reason) for d in disc.diagnostics] == [
            ("cli.py", line_of(UNTRACED, '"open"'), "unresolved_target"),
            ("cli.py", line_of(UNTRACED, '"close"'), "unresolved_target")]
        assert "self.sub" in disc.diagnostics[0].detail and "add_subparsers" in disc.diagnostics[0].detail

    def test_untraced_receiver_in_a_module_without_add_subparsers(self, tmp_path):
        src = "def register(sub):\n    sub.add_parser(\"open\")\n"
        root = make_tree(tmp_path / "snap", {"commands/open.py": src})
        assert [(d.line, d.reason) for d in discover(root).diagnostics] == [(2, "unresolved_target")]


class TestNamespacePackageHandlers:
    @pytest.mark.parametrize("binding, func, symbol", [
        ("import tools.cli", "tools.cli.cmd_open", "cmd_open"),
        ("import tools.cli", "tools.cli.not_a_def", None),
        ("from tools import cli", "cli.not_a_def", None),
    ])
    def test_namespace_package_targets_are_in_the_repository(self, tmp_path, binding, func, symbol):
        src = SET_DEFAULTS.replace("import argparse\n", f"import argparse\n{binding}\n")
        src = src.replace("func=cmd_open", f"func={func}")
        root = make_tree(tmp_path / "snap", {"app.py": src, "tools/cli.py": "def cmd_open(args):\n    return 0\n"})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["open"]
        assert (e.handler and (e.handler.path, e.handler.symbol)) == (symbol and ("tools/cli.py", symbol))
        assert disc.diagnostics == []


SAME_LEAF = """\
import argparse


def top_migrate(args):
    return 0


def db_migrate(args):
    return 1


def cmd_db(args):
    return 2


DB_COMMANDS = {"migrate": db_migrate}
COMMANDS = {"migrate": top_migrate, "db": cmd_db}


def build():
    sub = argparse.ArgumentParser().add_subparsers(dest="command")
    sub.add_parser("migrate")
    db = sub.add_parser("db")
    db.add_subparsers(dest="db_command").add_parser("migrate")
    return sub


def main(args):
    if args.command == "db" and args.db_command:
        return DB_COMMANDS[args.db_command](args)
    return COMMANDS[args.command](args)
"""


class TestDispatchDictSelection:
    @pytest.mark.parametrize("module", [DICT_MAIN, DICT_DISPATCH], ids=["no-dispatch-call", "dispatch-call"])
    def test_an_unrelated_dict_sharing_a_key_is_not_the_dispatch_dict(self, tmp_path, module):
        src = module.replace("\nHANDLERS = {", '\nPALETTE = {"render": show_cmd, "red": show_cmd}\nHANDLERS = {')
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": src, "pkg/commands.py": DICT_COMMANDS})
        e = by_name(argparse_entries(discover(root)))["render"]
        assert e.handler.symbol == "render_cmd"

    def test_only_dicts_called_as_dispatch_count_when_one_is(self, tmp_path):
        unused = '\nUNUSED = {"render": show_cmd, "show": render_cmd}'
        src = DICT_DISPATCH.replace("\nHANDLERS = {", unused + "\nHANDLERS = {")
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": src, "pkg/commands.py": DICT_COMMANDS})
        found = by_name(argparse_entries(discover(root)))
        assert (found["render"].handler.symbol, found["show"].handler.symbol) == ("render_cmd", "show_cmd")

    def test_same_leaf_name_at_two_depths_finds_its_own_dict(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": SAME_LEAF})
        found = by_name(argparse_entries(discover(root)))
        top, nested = found["migrate"], found["db migrate"]
        assert (top.location.line, top.handler.symbol) == (line_of(SAME_LEAF, '"migrate")'), "top_migrate")
        assert (nested.location.line, nested.handler.symbol) == (line_of(SAME_LEAF, '"migrate")', 2), "db_migrate")
        assert edges(top) == {("cli.py", line_of(SAME_LEAF, "COMMANDS[args.command](args)"),
                               "cli.py", line_of(SAME_LEAF, "def top_migrate"))}
        assert edges(nested) == {("cli.py", line_of(SAME_LEAF, "DB_COMMANDS[args.db_command](args)"),
                                  "cli.py", line_of(SAME_LEAF, "def db_migrate"))}
        assert found["db"].handler.symbol == "cmd_db"

    def test_dispatch_call_through_a_module_attribute_in_the_parser_module(self, tmp_path):
        main = LIVE_MAIN.replace("    return commands.run(args)", "    return commands.COMMANDS[args.command](args)")
        root = make_tree(tmp_path / "snap", {
            "pr_pipeline/__init__.py": "", "pr_pipeline/__main__.py": main,
            "pr_pipeline/commands.py": LIVE_COMMANDS.replace("    code = COMMANDS[args.command](args)\n", "")})
        e = by_name(argparse_entries(discover(root)))["open"]
        assert edges(e) == {("pr_pipeline/__main__.py", line_of(main, "commands.COMMANDS[args.command](args)"),
                             "pr_pipeline/commands.py", line_of(LIVE_COMMANDS, "def cmd_open"))}

    @pytest.mark.parametrize("mechanism", ["set_defaults", "dict"])
    def test_a_class_is_not_a_handler(self, tmp_path, mechanism):
        src = SET_DEFAULTS.replace("def cmd_open(args):\n    return 0\n", "class OpenCommand:\n    pass\n")
        if mechanism == "set_defaults":
            src = src.replace("func=cmd_open", "func=OpenCommand")
        else:
            src = src.replace("    p_open.set_defaults(func=cmd_open)\n", "") + '\nHANDLERS = {"open": OpenCommand}\n'
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        disc = discover(root)
        e = by_name(argparse_entries(disc))["open"]
        assert e.handler is None and e.known_edges == []
        assert disc.diagnostics == []


class TestArgsFuncDispatchCall:
    def test_func_calls_on_names_not_bound_from_parse_args_are_not_dispatch(self, tmp_path):
        src = SET_DEFAULTS.replace("import argparse\n", (
            "import argparse\n\n\nclass Job:\n    def run(self, obj):\n"
            "        self.func()\n        return obj.func(1)\n"))
        root = make_tree(tmp_path / "snap", {"cli.py": src})
        e = by_name(argparse_entries(discover(root)))["open"]
        assert edges(e) == {("cli.py", line_of(src, "set_defaults"), "cli.py", line_of(src, "def cmd_open"))}

    @pytest.mark.parametrize("parse", ["args = build().parse_args()", "args, _rest = build().parse_known_args()"])
    def test_dispatch_call_in_a_module_importing_the_parser(self, tmp_path, parse):
        main = f"from .cli import build\n\n\ndef main():\n    {parse}\n    return args.func(args)\n"
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": SET_DEFAULTS, "pkg/__main__.py": main})
        e = by_name(argparse_entries(discover(root)))["open"]
        assert edges(e) == {("pkg/__main__.py", line_of(main, "args.func(args)"),
                             "pkg/cli.py", line_of(SET_DEFAULTS, "def cmd_open"))}


def comparison_main(prelude: str = "") -> str:
    return prelude + """\
import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(prog="pr_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    op = sub.add_parser("open", help="open a PR")
    op.add_argument("--draft", action="store_true")
    ed = sub.add_parser("edit", help="edit a PR")
    ed.add_argument("pr")
    sub.add_parser("render", help="print the body")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "open":
        return 0
    if args.command == "edit":
        return 1
    if args.command == "render":
        return 2
    return 3
"""


LIVE_MAIN = """\
import argparse


def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("open")
    sub.add_parser("edit")
    sub.add_parser("render")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from . import commands
    return commands.run(args)
"""
LIVE_COMMANDS = """\
def cmd_render(args):
    return 0


def cmd_open(args):
    return 0


def cmd_edit(args):
    return 0


COMMANDS = {"open": cmd_open, "edit": cmd_edit, "render": cmd_render}


def run(args):
    code = COMMANDS[args.command](args)
    return code
"""


class TestPrPipelineShapes:
    def test_comparison_dispatch_lands_on_add_parser_lines_with_no_diagnostic(self, tmp_path):
        src = comparison_main()
        root = make_tree(tmp_path / "snap", {
            "skills/pr/scripts/pr_pipeline/__init__.py": "",
            "skills/pr/scripts/pr_pipeline/__main__.py": src})
        disc = discover(root)
        found = by_name(argparse_entries(disc))
        assert set(found) == {"open", "edit", "render"}
        for name in found:
            e = found[name]
            assert e.handler is None and e.known_edges == []
            assert e.location.path == "skills/pr/scripts/pr_pipeline/__main__.py"
            assert e.location.line == line_of(src, f'add_parser("{name}"')
        assert disc.diagnostics == []

    def test_dict_in_function_locally_imported_sibling(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "pr_pipeline/__init__.py": "", "pr_pipeline/__main__.py": LIVE_MAIN,
            "pr_pipeline/commands.py": LIVE_COMMANDS})
        found = by_name(argparse_entries(discover(root)))
        dispatch = line_of(LIVE_COMMANDS, "COMMANDS[args.command](args)")
        for name in ("open", "edit", "render"):
            e = found[name]
            def_line = line_of(LIVE_COMMANDS, f"def cmd_{name}")
            assert e.location.line == line_of(LIVE_MAIN, f'"{name}"')
            assert (e.handler.path, e.handler.line, e.handler.symbol) == (
                "pr_pipeline/commands.py", def_line, f"cmd_{name}")
            assert edges(e) == {("pr_pipeline/commands.py", dispatch, "pr_pipeline/commands.py", def_line)}

    @pytest.mark.skipif(not LIVE_PR_SCRIPTS.is_dir(), reason="live pr skill scripts absent")
    def test_live_pr_pipeline_copy(self, tmp_path):
        root = tmp_path / "snap"
        shutil.copytree(LIVE_PR_SCRIPTS, root / "scripts",
                        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
        commands_src = (LIVE_PR_SCRIPTS / "pr_pipeline/commands.py").read_text()
        main_src = (LIVE_PR_SCRIPTS / "pr_pipeline/__main__.py").read_text()
        found = by_name(argparse_entries(discover(root), path="scripts/pr_pipeline/__main__.py"))
        assert set(found) == {"open", "edit", "render"}
        dispatch = line_of(commands_src, "COMMANDS[args.command](args)")  # commands.py:409 at time of writing
        for name in ("open", "edit", "render"):
            e = found[name]
            def_line = line_of(commands_src, f"def cmd_{name}(")
            assert e.location.line == line_of(main_src, f'add_parser("{name}"')
            assert (e.handler.path, e.handler.line, e.handler.symbol) == (
                "scripts/pr_pipeline/commands.py", def_line, f"cmd_{name}")
            assert ("scripts/pr_pipeline/commands.py", dispatch,
                    "scripts/pr_pipeline/commands.py", def_line) in edges(e)


class TestMultiRepository:
    def test_colliding_tool_packages_resolve_within_their_own_repo(self, tmp_path):
        cli = ("import argparse\nfrom tool.commands import go\n\n\ndef build():\n"
               "    sub = argparse.ArgumentParser().add_subparsers()\n"
               "    sub.add_parser(\"go\").set_defaults(func=go)\n")
        root = make_tree(tmp_path / "snap", {
            "repo_a/pyproject.toml": "[project]\nname = 'a'\n",
            "repo_a/tool/__init__.py": "", "repo_a/tool/cli.py": cli,
            "repo_a/tool/commands.py": "\n\ndef go(args):\n    return 0\n",
            "repo_b/pyproject.toml": "[project]\nname = 'b'\n",
            "repo_b/tool/__init__.py": "",
            "repo_b/tool/commands.py": "def go(args):\n    return 1\n",
        })
        [e] = argparse_entries(discover(root))
        assert e.location.path == "repo_a/tool/cli.py"
        assert (e.handler.path, e.handler.line) == ("repo_a/tool/commands.py", 3)


class TestWalk:
    def test_excluded_directories_are_not_scanned(self, tmp_path):
        excluded = [".venv/lib/site-packages/somepkg", "node_modules/x", "build/x", "dist/x", "venv/x",
                    "site-packages/x", "__pycache__", ".git/x"]
        files = {f"{d}/cli.py": FLAT for d in excluded}
        files["keep/cli.py"] = FLAT
        root = make_tree(tmp_path / "snap", files)
        disc = discover(root)
        assert {e.location.path for e in disc.entries} == {"keep/cli.py"}
        assert {d.path for d in disc.diagnostics} == {"keep/cli.py"}

    def test_symlinks_escaping_the_root_are_not_followed(self, tmp_path):
        outside = make_tree(tmp_path / "outside", {"cli.py": FLAT, "pkg/cli.py": FLAT})
        root = make_tree(tmp_path / "snap", {"inside.py": "X = 1\n"})
        (root / "link").symlink_to(outside / "pkg", target_is_directory=True)
        (root / "linked.py").symlink_to(outside / "cli.py")
        disc = discover(root)
        assert disc.entries == [] and disc.diagnostics == []

    def test_symlink_loops_terminate(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"pkg/cli.py": SET_DEFAULTS})
        (root / "pkg/self").symlink_to(root / "pkg", target_is_directory=True)
        (root / "loop_a.py").symlink_to(root / "loop_b.py")
        (root / "loop_b.py").symlink_to(root / "loop_a.py")
        disc = discover(root)
        assert {(e.location.path, e.name) for e in disc.entries} == {("pkg/cli.py", "open")}
        assert disc.diagnostics == []

    def test_unparseable_files_give_parse_error_and_others_still_found(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "broken.py": "def broken(:\n", "binary.py": b"x = '\xff\xfe'\n", "cli.py": SET_DEFAULTS})
        disc = discover(root)
        assert [(d.path, d.line, d.reason) for d in disc.diagnostics] == [
            ("binary.py", None, "parse_error"), ("broken.py", None, "parse_error")]
        assert set(by_name(argparse_entries(disc))) == {"open"}


class _ReversedScandir:
    """os.scandir with entries yielded in reverse name order."""

    def __init__(self, it):
        with it:
            self._entries = iter(sorted(it, key=lambda e: e.name, reverse=True))

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _ordering_tree(tmp_path):
    return make_tree(tmp_path / "snap", {
        "b/cli.py": FLAT, "a/cli.py": SET_DEFAULTS, "a/zz.py": "def (\n", "c/cli.py": comparison_main()})


class TestDeterminism:
    def test_entries_and_diagnostics_are_sorted(self, tmp_path):
        disc = discover(_ordering_tree(tmp_path))
        keys = [(e.location.path, e.location.line, e.kind, e.name) for e in disc.entries]
        assert keys == sorted(keys) and len(keys) == 8
        dkeys = [(d.path, d.line or 0, d.reason) for d in disc.diagnostics]
        assert dkeys == sorted(dkeys)
        assert {("a/zz.py", "parse_error"), ("b/cli.py", "dynamic_name")} <= {(d.path, d.reason) for d in disc.diagnostics}

    def test_output_is_independent_of_filesystem_order(self, tmp_path, monkeypatch):
        root = _ordering_tree(tmp_path)
        first = discover(root).model_dump_json(by_alias=True)
        assert discover(root).model_dump_json(by_alias=True) == first
        real = os.scandir
        monkeypatch.setattr(os, "scandir", lambda path=".": _ReversedScandir(real(path)))
        assert discover(root).model_dump_json(by_alias=True) == first

    def test_a_prebuilt_index_gives_the_same_result(self, tmp_path):
        root = _ordering_tree(tmp_path)
        index = importlib.import_module("code_facts.pyindex").PyIndex.build(root)
        assert discover(root, index=index) == discover(root)


class TestNoExecution:
    def test_fixture_code_is_never_imported_run_or_spawned(self, tmp_path, monkeypatch):
        sentinel = tmp_path / "SENTINEL"
        root = make_tree(tmp_path / "snap", {
            "sentinelpkg/__init__.py": sentinel_prelude(sentinel),
            "sentinelpkg/__main__.py": comparison_main(sentinel_prelude(sentinel)),
            "sentinelpkg/cli.py": sentinel_prelude(sentinel) + SET_DEFAULTS,
        })

        def refuse(*a, **k):
            raise AssertionError("discovery spawned a process")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(os, "system", refuse)
        before = set(sys.modules)
        disc = discover(root)
        assert not sentinel.exists()
        assert not any(m.startswith("sentinelpkg") for m in set(sys.modules) - before)
        assert {(e.location.path, e.name) for e in argparse_entries(disc)} == {
            ("sentinelpkg/__main__.py", "open"), ("sentinelpkg/__main__.py", "edit"),
            ("sentinelpkg/__main__.py", "render"), ("sentinelpkg/cli.py", "open")}


class TestModelsAndExports:
    def test_entry_fields_in_order_and_known_edge_alias(self, tmp_path):
        mod = entries_mod()
        assert list(mod.Entry.model_fields) == [
            "kind", "name", "location", "handler", "invocation", "known_edges", "also_invoked_by"]
        assert list(mod.Diagnostic.model_fields) == ["path", "line", "reason", "detail"]
        root = make_tree(tmp_path / "snap", {"cli.py": SET_DEFAULTS})
        [e] = argparse_entries(discover(root))
        dumped = e.model_dump(mode="json", by_alias=True)
        assert dumped["known_edges"] == [{"from": {"path": "cli.py", "line": line_of(SET_DEFAULTS, "set_defaults")},
                                          "to": {"path": "cli.py", "line": line_of(SET_DEFAULTS, "def cmd_open")}}]
        assert mod.Entry.model_validate(dumped) == e

    def test_discover_entries_is_exported(self):
        assert code_facts.discover_entries is entries_mod().discover_entries
        assert "discover_entries" in code_facts.__all__
