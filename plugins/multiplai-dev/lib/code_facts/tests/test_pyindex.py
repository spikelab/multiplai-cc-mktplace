"""PyIndex: one static ast pass over a snapshot — module names, repo roots, import resolution, scopes."""

from __future__ import annotations

import importlib

import pytest
from conftest import line_of, make_tree


def build(root):
    return importlib.import_module("code_facts.pyindex").PyIndex.build(root)


CMDS = "def handler():\n    return 'cmds'\n\n\ndef tried():\n    return 1\n\n\ndef x():\n    return 2\n"
OTHER = "\n\ndef handler():\n    return 'other'\n"
CLI = """\
from typing import TYPE_CHECKING
from pkg.cmds import handler

try:
    from pkg.cmds import tried
except ImportError:
    pass

if TYPE_CHECKING:
    from pkg.tc import ghost


def local_fn():
    return handler()  # module-scope handler


def main():
    from pkg.other import handler
    from pkg.cmds import x
    return handler()  # function-scope handler


class Runner:
    def go(self):
        from pkg.cmds import x as method_x
        return method_x()  # method-scope import


import requests
"""


@pytest.fixture
def snap(tmp_path):
    return make_tree(
        tmp_path / "snap",
        {
            "pkg/__init__.py": "",
            "pkg/cmds.py": CMDS,
            "pkg/other.py": OTHER,
            "pkg/tc.py": "def ghost():\n    pass\n",
            "pkg/cli.py": CLI,
            "pkg/sub/__init__.py": "",
            "pkg/sub/c.py": "def c():\n    pass\n",
            "loose/run.py": "import helper\n",
            "loose/helper.py": "def h():\n    pass\n",
            "repo_a/pyproject.toml": "[project]\nname = 'a'\n",
            "repo_a/tool/__init__.py": "",
            "repo_a/tool/__main__.py": "from tool.cli import main\n",
            "repo_a/tool/cli.py": "def main():\n    pass\n",
            "repo_a/scripts/pyproject.toml": "[project]\nname = 'a-scripts'\n",
            "repo_a/scripts/job.py": "import only_in_b\n",
            "repo_b/.git/HEAD": "ref: refs/heads/main\n",
            "repo_b/tool/__init__.py": "",
            "repo_b/tool/cli.py": "def main():\n    pass\n",
            "repo_b/only_in_b.py": "X = 1\n",
            "repo_c/pyproject.toml": "[project]\nname = 'c'\n",
            "repo_c/src/tool/__init__.py": "",
            "repo_c/src/tool/cli.py": "def main():\n    pass\n",
            "repo_c/tool_user.py": "from tool.cli import main\n",
            "notes.md": "not python\n",
            "bad.py": "def broken(:\n",
            "bin.py": b"x = '\xff\xfe'\n",
            "nul.py": b"x = 1\x00\n",
            "bom.py": b"\xef\xbb\xbfdef with_bom():\n    pass\n",
            "latin.py": "# -*- coding: latin-1 -*-\n\n\ndef caf\u00e9():\n    return '\u00e9'\n".encode(
                "latin-1"
            ),
        },
    )


class TestModuleNames:
    def test_dotted_names_relative_to_the_files_repository(self, snap):
        idx = build(snap)
        assert idx.module_name("pkg/cli.py") == "pkg.cli"
        assert idx.module_name("pkg/__init__.py") == "pkg"
        assert idx.module_name("pkg/sub/c.py") == "pkg.sub.c"
        assert idx.module_name("repo_a/tool/cli.py") == "tool.cli"
        assert idx.module_name("repo_a/tool/__main__.py") == "tool.__main__"
        assert idx.module_name("repo_c/src/tool/cli.py") == "tool.cli"

    def test_non_python_and_missing_files_have_no_module(self, snap):
        idx = build(snap)
        assert idx.module_name("notes.md") is None
        assert idx.module_name("pkg/nope.py") is None


class TestRepoRoot:
    def test_nearest_marker_at_or_below_root(self, snap):
        idx = build(snap)
        assert idx.repo_root("repo_a/tool/cli.py") == "repo_a"
        assert idx.repo_root("repo_a/scripts/job.py") == "repo_a/scripts"
        assert idx.repo_root("repo_b/tool/cli.py") == "repo_b"  # .git marks a root too
        assert idx.repo_root("repo_c/src/tool/cli.py") == "repo_c"

    def test_falls_back_to_the_snapshot_root(self, snap):
        idx = build(snap)
        assert idx.repo_root("pkg/cli.py") == ""
        assert idx.repo_root("loose/run.py") == idx.repo_root("pkg/cli.py")


class TestResolveModule:
    def test_absolute_import_under_repo_root(self, snap):
        idx = build(snap)
        assert idx.resolve_module("pkg/cli.py", "pkg.cmds") == "pkg/cmds.py"
        assert idx.resolve_module("pkg/cli.py", "pkg.sub") == "pkg/sub/__init__.py"

    def test_same_repository_only_with_colliding_packages(self, snap):
        idx = build(snap)
        assert (
            idx.resolve_module("repo_a/tool/__main__.py", "tool.cli")
            == "repo_a/tool/cli.py"
        )
        assert idx.resolve_module("repo_a/scripts/job.py", "only_in_b") is None

    def test_src_layout(self, snap):
        assert (
            build(snap).resolve_module("repo_c/tool_user.py", "tool.cli")
            == "repo_c/src/tool/cli.py"
        )

    def test_importing_files_own_directory(self, snap):
        assert build(snap).resolve_module("loose/run.py", "helper") == "loose/helper.py"

    def test_relative_imports(self, snap):
        idx = build(snap)
        assert idx.resolve_module("pkg/cli.py", "cmds", level=1) == "pkg/cmds.py"
        assert idx.resolve_module("pkg/sub/c.py", "cmds", level=2) == "pkg/cmds.py"
        assert idx.resolve_module("pkg/cli.py", "sub.c", level=1) == "pkg/sub/c.py"

    def test_third_party_is_unresolved(self, snap):
        assert build(snap).resolve_module("pkg/cli.py", "requests") is None


class TestResolveName:
    def test_function_scope_wins_over_module_scope(self, snap):
        idx = build(snap)
        h = idx.resolve_name(
            "pkg/cli.py", line_of(CLI, "# function-scope handler"), "handler"
        )
        assert (h.path, h.line, h.symbol) == (
            "pkg/other.py",
            line_of(OTHER, "def handler"),
            "handler",
        )

    def test_module_scope_import_outside_the_function(self, snap):
        idx = build(snap)
        h = idx.resolve_name(
            "pkg/cli.py", line_of(CLI, "# module-scope handler"), "handler"
        )
        assert (h.path, h.line, h.symbol) == ("pkg/cmds.py", 1, "handler")

    def test_function_local_import_binds_only_inside_its_function(self, snap):
        idx = build(snap)
        # `x` is imported inside main(); at module scope it is unbound.
        assert (
            idx.resolve_name("pkg/cli.py", line_of(CLI, "# module-scope handler"), "x")
            is None
        )
        h = idx.resolve_name(
            "pkg/cli.py", line_of(CLI, "# function-scope handler"), "x"
        )
        assert (h.path, h.line) == ("pkg/cmds.py", line_of(CMDS, "def x"))

    def test_method_and_try_imports_are_collected(self, snap):
        idx = build(snap)
        h = idx.resolve_name(
            "pkg/cli.py", line_of(CLI, "# method-scope import"), "method_x"
        )
        assert (h.path, h.line) == ("pkg/cmds.py", line_of(CMDS, "def x"))
        t = idx.resolve_name(
            "pkg/cli.py", line_of(CLI, "# module-scope handler"), "tried"
        )
        assert (t.path, t.line) == ("pkg/cmds.py", line_of(CMDS, "def tried"))

    def test_type_checking_imports_are_excluded(self, snap):
        assert (
            build(snap).resolve_name(
                "pkg/cli.py", line_of(CLI, "# module-scope handler"), "ghost"
            )
            is None
        )

    def test_locally_defined_function(self, snap):
        h = build(snap).resolve_name("pkg/cli.py", line_of(CLI, "def main"), "local_fn")
        assert (h.path, h.line, h.symbol) == (
            "pkg/cli.py",
            line_of(CLI, "def local_fn"),
            "local_fn",
        )

    def test_dotted_names_through_module_bindings(self, snap):
        idx = build(snap)
        assert (
            idx.resolve_name("pkg/cli.py", 1, "pkg.cmds.handler") is None
        )  # `pkg` is unbound in cli.py
        src = "import pkg.cmds\nimport pkg.cmds as c\nfrom pkg import cmds\nfrom . import sub\n"
        root = make_tree(snap, {"pkg/dotted.py": src})
        idx = build(root)
        for name in ("pkg.cmds.handler", "c.handler", "cmds.handler"):
            h = idx.resolve_name("pkg/dotted.py", 4, name)
            assert (h.path, h.line, h.symbol) == ("pkg/cmds.py", 1, "handler"), name
        h = idx.resolve_name("pkg/dotted.py", 4, "sub.c.c")
        assert (h.path, h.line) == ("pkg/sub/c.py", 1)
        assert idx.resolve_name("pkg/dotted.py", 4, "c.missing") is None

    def test_is_external_only_for_imports_leaving_the_repository(self, snap):
        idx = build(snap)
        line = line_of(CLI, "# module-scope handler")
        assert idx.is_external("pkg/cli.py", line, "requests")
        assert idx.is_external("pkg/cli.py", line, "requests.get")
        assert not idx.is_external("pkg/cli.py", line, "handler")
        assert not idx.is_external("pkg/cli.py", line, "local_fn")
        assert not idx.is_external("pkg/cli.py", line, "nowhere")
        assert not idx.is_external("repo_a/tool/__main__.py", 1, "main")
        root = make_tree(
            snap,
            {
                "pkg/reexport.py": "from requests import get\n",
                "pkg/user.py": "from pkg.reexport import get\nfrom .sub import nothing\n",
            },
        )
        idx = build(root)
        assert idx.is_external("pkg/user.py", 1, "get")
        assert not idx.is_external("pkg/user.py", 2, "nothing")

    @pytest.mark.parametrize(
        "binding, name",
        [
            ("import tools.cli", "tools.cli.not_a_def"),
            ("import tools", "tools.cli"),
            ("from tools import cli", "cli.not_a_def"),
            ("from tools import nothing", "nothing"),
        ],
    )
    def test_namespace_package_in_the_repository_is_not_external(
        self, tmp_path, binding, name
    ):
        root = make_tree(
            tmp_path / "snap",
            {"app.py": f"{binding}\n", "tools/cli.py": "def run():\n    return 0\n"},
        )
        assert not build(root).is_external("app.py", 1, name)
        other = make_tree(
            tmp_path / "other", {"app.py": "import tool.cli\n", "tools/cli.py": ""}
        )
        assert build(other).is_external("app.py", 1, "tool.cli.run")

    def test_unknown_and_third_party_names(self, snap):
        idx = build(snap)
        line = line_of(CLI, "# module-scope handler")
        assert idx.resolve_name("pkg/cli.py", line, "nowhere") is None
        assert idx.resolve_name("pkg/cli.py", line, "requests") is None


class TestEnclosingScope:
    def test_innermost_def_or_class(self, snap):
        idx = build(snap)
        go = line_of(CLI, "def go")
        assert idx.enclosing_scope("pkg/cli.py", go + 2) == (go, go + 2)
        runner = line_of(CLI, "class Runner")
        assert idx.enclosing_scope("pkg/cli.py", runner) == (runner, go + 2)
        main = line_of(CLI, "def main")
        assert idx.enclosing_scope("pkg/cli.py", main + 1) == (main, main + 3)

    def test_module_level_line_gives_the_whole_module(self, snap):
        idx = build(snap)
        assert idx.enclosing_scope("pkg/cli.py", line_of(CLI, "import requests")) == (
            1,
            len(CLI.splitlines()),
        )


class TestParseErrors:
    def test_unparseable_files_are_reported_and_the_rest_indexed(self, snap):
        idx = build(snap)
        assert {path for path, _ in idx.parse_errors()} == {
            "bad.py",
            "bin.py",
            "nul.py",
        }
        assert all(detail for _, detail in idx.parse_errors())
        assert idx.module_name("pkg/cli.py") == "pkg.cli"

    def test_bom_and_coding_cookie_files_parse(self, snap):
        idx = build(snap)
        assert [(d.name, d.start) for d in idx.defs("bom.py")] == [("with_bom", 1)]
        assert [(d.name, d.start) for d in idx.defs("latin.py")] == [("caf\u00e9", 4)]
        assert idx.line_count("latin.py") == 5
