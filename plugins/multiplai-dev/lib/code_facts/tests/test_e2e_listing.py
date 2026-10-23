"""Static end-to-end test listing: which snapshot tests invoke each kept path's entry.

Every snapshot is written into `tmp_path`; nothing in it is imported, collected
or run. Each path here has a single retained hop, so no test can move to
`mocks_hop_2` and every match lands in `tests.e2e`.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
from conftest import line_of, make_tree, sentinel_prelude
from trace_fixtures import FakeCall, chain_hops, cli_snapshot, commit_snapshot, run_map

REPO = Path(__file__).resolve().parents[5]
LIVE_PR = REPO / "plugins" / "multiplai-dev" / "skills" / "pr" / "scripts"


def lib(name: str) -> Any:
    """A code_facts name from whichever module defines it, looked up at call time."""
    for mod in (
        "code_facts",
        "code_facts.e2e_tests",
        "code_facts.e2e",
        "code_facts.paths",
        "code_facts.entries",
        "code_facts.pyindex",
    ):
        try:
            module = importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"code_facts does not define {name!r}")


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


# --- the snapshot program -------------------------------------------------------------------

# The parser lives in `pkg/__main__.py`, its handlers in `pkg/cli.py`: the pr_pipeline shape.
CLI = src("""
    def handle_open(args):
        return 0


    def handle_render(args):
        return 0


    def tool_main():
        return 0
    """)

MAIN = src("""
    import argparse

    from pkg.cli import handle_open, handle_render


    def main(argv=None):
        parser = argparse.ArgumentParser(prog="pkg")
        sub = parser.add_subparsers(dest="command", required=True)
        p_open = sub.add_parser("open")
        p_open.set_defaults(func=handle_open)
        p_render = sub.add_parser("render")
        p_render.set_defaults(func=handle_render)
        args = parser.parse_args(argv)
        return args.func(args)


    def build(argv):
        return list(argv)


    if __name__ == "__main__":
        main()
    """)

# Same names as `pkg`, in another module: never the `pkg` entries.
OTHER = src("""
    import argparse


    def handle_open(args):
        return 0


    def main(argv=None):
        parser = argparse.ArgumentParser(prog="other")
        parser.add_argument("command")
        return parser.parse_args(argv)
    """)

CLICK = src("""
    import click


    @click.group()
    def cli():
        pass


    @cli.command()
    def sync():
        click.echo("sync")


    @cli.command()
    def push():
        click.echo("push")
    """)

PYPROJECT = '[project]\nname = "x"\nversion = "0"\n\n[project.scripts]\nmytool = "pkg.cli:tool_main"\n'

BASE = {
    "pyproject.toml": PYPROJECT,
    "pkg/__init__.py": "",
    "pkg/cli.py": CLI,
    "pkg/__main__.py": MAIN,
    "other/__init__.py": "",
    "other/module.py": OTHER,
    "app/__init__.py": "",
    "app/cmd.py": CLICK,
}

IMPORTS = (
    "import runpy\nimport subprocess\nimport sys\n\nimport pytest\n\nfrom pkg.__main__ import main\n"
    "from pkg.cli import handle_open, handle_render\n\n\n"
)


def snapshot(
    tmp_path: Path, files: dict[str, str | bytes], *, base: dict[str, str] = BASE
) -> Path:
    return make_tree(tmp_path / "snap", {**base, **files})


# --- listing -------------------------------------------------------------------------------


def traced(pid: str, entry: Any) -> Any:
    """A kept path `pid` from `entry` with one hop at the entry's location."""
    loc = entry.location
    hop = lib("PathHop")(
        symbol_called=entry.name.split()[-1],
        path=loc.path,
        line_start=loc.line,
        line_end=loc.line,
        quote="x",
    )
    return lib("TracedPath")(
        id=pid,
        entry=lib("to_path_entry")(entry),
        hops=[hop],
        stops_at=None,
        break_reason=None,
        changed_lines_covered=[],
        tests=lib("E2EListing")(e2e=[], mocks_hop_2=[]),
    )


def entries_named(root: Path, *names: str) -> dict[str, Any]:
    """Discovered entries keyed by name; every name must be discovered."""
    discovered = lib("discover_entries")(root).entries
    found = {e.name: e for e in discovered}
    assert len(found) == len(discovered), (
        f"entry names collide: {[e.name for e in discovered]}"
    )
    missing = [n for n in names if n not in found]
    assert not missing, f"not discovered: {missing}; have {sorted(found)}"
    return {n: found[n] for n in names}


def listing(root: Path, *names: str) -> tuple[dict[str, Any], list[Any]]:
    """(E2EListing per entry name, skips) from `list_e2e_tests` over one path per named entry."""
    entries = entries_named(root, *names)
    by_id = {f"p{n}": e for n, e in enumerate(entries.values(), start=1)}
    paths = [traced(pid, e) for pid, e in by_id.items()]
    out, skips = lib("list_e2e_tests")(
        root, paths, by_id, index=lib("PyIndex").build(root)
    )
    assert set(out) == set(by_id), "every path id gets a listing"
    return {e.name: out[pid] for pid, e in by_id.items()}, skips


def ids(listed: Any) -> list[str]:
    return [t.node_id for t in listed.e2e]


def rec(listed: Any, node_id: str) -> Any:
    (record,) = [t for t in listed.e2e if t.node_id == node_id]
    return record


# --- discovery scope -----------------------------------------------------------------------


def test_module_level_and_class_based_tests_are_found(tmp_path):
    body = IMPORTS + src("""
        def test_open():
            assert handle_open(None) == 0


        class TestRender:
            def test_basic(self):
                assert main(["render"]) == 0
        """)
    root = snapshot(tmp_path, {"tests/test_cli.py": body})
    out, _ = listing(root, "open", "render")
    assert ids(out["open"]) == ["tests/test_cli.py::test_open"]
    assert ids(out["render"]) == ["tests/test_cli.py::TestRender::test_basic"]
    opened, rendered = out["open"].e2e[0], out["render"].e2e[0]
    assert (opened.file, opened.line, opened.match, opened.invocation_line) == (
        "tests/test_cli.py",
        line_of(body, "def test_open"),
        "direct_call",
        line_of(body, "handle_open(None)"),
    )
    assert (rendered.line, rendered.match, rendered.invocation_line) == (
        line_of(body, "def test_basic"),
        "cli_argv",
        line_of(body, 'main(["render"])'),
    )


def test_non_tests_nested_functions_and_excluded_directories_are_ignored(tmp_path):
    ignored = IMPORTS + src("""
        def run_helper():
            return handle_open(None)


        def test_outer():
            def inner():
                return handle_open(None)
            return inner


        class Helpers:
            def test_in_plain_class(self):
                return handle_open(None)
        """)
    vendored = (
        "from pkg.cli import handle_open\n\n\ndef test_x():\n    handle_open(None)\n"
    )
    files = {"tests/test_cli.py": ignored, "tests/helpers.py": vendored}
    for d in (
        ".venv/lib",
        "venv",
        "node_modules/x",
        "site-packages",
        "__pycache__",
        ".git/hooks",
    ):
        files[f"{d}/test_vendor.py"] = vendored
    files["tests/cli_test.py"] = vendored.replace("test_x", "test_suffix")
    out, _ = listing(snapshot(tmp_path, files), "open")
    assert ids(out["open"]) == ["tests/cli_test.py::test_suffix"]


def test_build_and_dist_directories_are_still_searched(tmp_path):
    # Discovery prunes them, test listing does not.
    vendored = (
        "from pkg.cli import handle_open\n\n\ndef test_x():\n    handle_open(None)\n"
    )
    out, _ = listing(
        snapshot(
            tmp_path, {"build/test_built.py": vendored, "dist/test_dist.py": vendored}
        ),
        "open",
    )
    assert ids(out["open"]) == [
        "build/test_built.py::test_x",
        "dist/test_dist.py::test_x",
    ]


def test_paths_are_relative_to_the_snapshot_root_in_multi_repo_snapshots(tmp_path):
    files = {f"repo-b/{k}": v for k, v in BASE.items()}
    files["repo-a/pyproject.toml"] = '[project]\nname = "a"\nversion = "0"\n'
    files["repo-a/a.py"] = "X = 1\n"
    files["repo-b/tests/test_hook.py"] = (
        "from pkg.cli import handle_open\n\n\ndef test_x():\n    handle_open(None)\n"
    )
    root = make_tree(tmp_path / "snap", files)
    out, _ = listing(root, "open")
    (record,) = out["open"].e2e
    assert record.file == "repo-b/tests/test_hook.py"
    assert record.node_id == "repo-b/tests/test_hook.py::test_x"


# --- direct calls --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "from pkg.cli import handle_open as h\n\n\ndef test_x():\n    h(None)\n",
        "import pkg.cli as cli\n\n\ndef test_x():\n    cli.handle_open(None)\n",
        "def test_x():\n    from pkg.cli import handle_open\n    handle_open(None)\n",
    ],
    ids=["alias", "module-attribute", "function-local-import"],
)
def test_call_resolving_to_the_handler_is_a_direct_call(tmp_path, body):
    out, _ = listing(snapshot(tmp_path, {"tests/test_x.py": body}), "open", "render")
    assert [(t.node_id, t.match) for t in out["open"].e2e] == [
        ("tests/test_x.py::test_x", "direct_call")
    ]
    assert out["render"].e2e == []


def test_same_name_from_another_module_does_not_match(tmp_path):
    body = src("""
        from other.module import handle_open, main


        def test_other():
            handle_open(None)
            main(["open"])
        """)
    out, _ = listing(snapshot(tmp_path, {"tests/test_other.py": body}), "open")
    assert out["open"].e2e == []


# --- CLI subcommand argv -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    ['["render", "--out", "x"]', '("render",)', '["render", "--repo", str(tmp_path)]'],
    ids=["list", "tuple", "list-with-a-computed-option-value"],
)
def test_main_with_subcommand_argv_lists_that_subcommand_only(tmp_path, argv):
    body = IMPORTS + f"def test_r(tmp_path):\n    main({argv})\n"
    out, _ = listing(snapshot(tmp_path, {"tests/test_r.py": body}), "open", "render")
    record = rec(out["render"], "tests/test_r.py::test_r")
    assert (record.match, record.invocation_line) == (
        "cli_argv",
        line_of(body, "main("),
    )
    assert out["open"].e2e == []


def test_dispatch_on_args_command_still_matches(tmp_path):
    # Comparison dispatch: the entry sits on its add_parser line, and its e2e test still matches.
    main = src("""
        import argparse


        def cmd_open(args):
            return 0


        def main(argv=None):
            parser = argparse.ArgumentParser(prog="cmp")
            sub = parser.add_subparsers(dest="command", required=True)
            sub.add_parser("open")
            args = parser.parse_args(argv)
            if args.command == "open":
                return cmd_open(args)
            return 1
        """)
    root = make_tree(
        tmp_path / "snap",
        {
            "cmp/__init__.py": "",
            "cmp/__main__.py": main,
            "tests/test_cmp.py": src("""
        from cmp.__main__ import main


        def test_open():
            main(["open"])
        """),
        },
    )
    out, _ = listing(root, "open")
    assert [(t.node_id, t.match) for t in out["open"].e2e] == [
        ("tests/test_cmp.py::test_open", "cli_argv")
    ]


def test_non_literal_argv_does_not_match(tmp_path):
    body = IMPORTS + src("""
        def test_local():
            argv = ["open"]
            main(argv)


        @pytest.mark.parametrize("argv", [["open"]])
        def test_param(argv):
            main(argv)


        def test_built():
            main(build(["open"]))
        """)
    out, _ = listing(snapshot(tmp_path, {"tests/test_n.py": body}), "open", "render")
    assert out["open"].e2e == [] and out["render"].e2e == []


@pytest.mark.parametrize("flag", ["--help", "-h", "--version"])
def test_help_and_version_argv_is_excluded(tmp_path, flag):
    # Such a run exits in argparse before the handler is reached.
    body = IMPORTS + f'def test_h():\n    main(["open", "{flag}"])\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_h.py": body}), "open")
    assert out["open"].e2e == []


# The pr_pipeline shape: a parser builder, and a dispatcher the parsed args are handed to.
BUILDER = src("""
    import argparse

    from pkg import commands


    def build_parser():
        parser = argparse.ArgumentParser(prog="pkg")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("open")
        return parser


    def main(argv=None):
        return commands.run(build_parser().parse_args(argv))
    """)
COMMANDS = "def run(args):\n    return 0\n"


def test_a_bare_builder_parse_reaches_no_handler_and_is_not_listed(tmp_path):
    body = src("""
        import pytest

        from pkg import commands
        from pkg.__main__ import build_parser


        def run(argv):
            return commands.run(build_parser().parse_args(argv))


        def test_refused():
            with pytest.raises(SystemExit):
                build_parser().parse_args(["open", "--draft", "--ready"])


        def test_parsed_and_inspected():
            args = build_parser().parse_args(["open"])
            assert isinstance(args.command, str) and args.command == "open"


        def test_through_the_helper():
            assert run(["open"]) == 0


        def test_handed_straight_to_the_dispatcher():
            commands.run(build_parser().parse_args(["open"]))


        def test_handed_to_the_dispatcher_by_name():
            args = build_parser().parse_args(["open"])
            commands.run(args)


        def test_dispatched_through_set_defaults():
            args = build_parser().parse_args(["open"])
            args.func(args)
        """)
    root = make_tree(
        tmp_path / "snap",
        {
            "pkg/__init__.py": "",
            "pkg/__main__.py": BUILDER,
            "pkg/commands.py": COMMANDS,
            "tests/test_b.py": body,
        },
    )
    out, _ = listing(root, "open")
    assert [(t.node_id.split("::")[1], t.match) for t in out["open"].e2e] == [
        ("test_through_the_helper", "cli_argv"),
        ("test_handed_straight_to_the_dispatcher", "cli_argv"),
        ("test_handed_to_the_dispatcher_by_name", "cli_argv"),
        ("test_dispatched_through_set_defaults", "cli_argv"),
    ]


def test_a_function_that_does_not_parse_args_is_not_the_program(tmp_path):
    body = 'from pkg.__main__ import build\n\n\ndef test_b():\n    build(["open"])\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_b.py": body}), "open")
    assert out["open"].e2e == []


def test_click_cli_runner_invocation(tmp_path):
    body = src("""
        from click.testing import CliRunner

        from app.cmd import cli


        def test_sync():
            CliRunner().invoke(cli, ["sync"])
        """)
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_click.py": body}), "cli sync", "cli push"
    )
    record = rec(out["cli sync"], "tests/test_click.py::test_sync")
    assert (record.match, record.invocation_line) == (
        "cli_runner",
        line_of(body, "invoke("),
    )
    assert out["cli push"].e2e == []


# --- subprocess, runpy, scripts and hooks --------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        'subprocess.run([sys.executable, "-m", "pkg", "render"])',
        'subprocess.call([sys.executable, "-m", "pkg", "render"])',
        'subprocess.check_call(["python", "-m", "pkg", "render"])',
        'subprocess.check_output(["python3", "-m", "pkg", "render"])',
        'subprocess.Popen([sys.executable, "-m", "pkg", "render"]).wait()',
        'subprocess.run("python3 -m pkg render", shell=True)',
    ],
)
def test_python_m_subprocess_lists_the_subcommand_path(tmp_path, call):
    body = IMPORTS + f"def test_s():\n    {call}\n"
    out, _ = listing(snapshot(tmp_path, {"tests/test_s.py": body}), "open", "render")
    record = rec(out["render"], "tests/test_s.py::test_s")
    assert (record.match, record.invocation_line) == (
        "subprocess",
        line_of(body, "subprocess."),
    )
    assert out["open"].e2e == []


def test_python_m_of_another_module_does_not_match(tmp_path):
    body = (
        IMPORTS
        + 'def test_s():\n    subprocess.run([sys.executable, "-m", "otherpkg", "render"])\n'
    )
    out, _ = listing(snapshot(tmp_path, {"tests/test_s.py": body}), "render")
    assert out["render"].e2e == []


def test_runpy_run_module_matches_the_main_module_entry(tmp_path):
    body = IMPORTS + 'def test_m():\n    runpy.run_module("pkg", run_name="__main__")\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_m.py": body}), "pkg")
    record = rec(out["pkg"], "tests/test_m.py::test_m")
    assert (record.match, record.invocation_line) == (
        "runpy",
        line_of(body, "runpy.run_module"),
    )


def test_console_script_name_matches_the_project_script(tmp_path):
    body = IMPORTS + 'def test_tool():\n    subprocess.run(["mytool", "build"])\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_tool.py": body}), "mytool")
    assert [(t.node_id, t.match) for t in out["mytool"].e2e] == [
        ("tests/test_tool.py::test_tool", "subprocess")
    ]


def test_hook_command_string_matches_the_hook_entry(tmp_path):
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "python3 hooks/guard.py"}],
                }
            ]
        }
    }
    body = 'import subprocess\n\n\ndef test_guard():\n    subprocess.run("python3 hooks/guard.py", shell=True)\n'
    root = snapshot(
        tmp_path,
        {
            ".claude/settings.json": json.dumps(settings, indent=2),
            "hooks/guard.py": "import sys\nprint('guard')\n",
            "tests/test_guard.py": body,
        },
    )
    out, _ = listing(root, "PreToolUse:Bash")
    assert [(t.node_id, t.match) for t in out["PreToolUse:Bash"].e2e] == [
        ("tests/test_guard.py::test_guard", "subprocess")
    ]


def test_running_only_a_merged_invoker_lists_the_kept_path(tmp_path):
    # The hook and the SKILL.md command both reach tool.py's main(); the skill command is kept.
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "python3 tool.py"}]}]
        }
    }
    files = {
        "tool.py": "def main():\n    return 0\n\n\nmain()\n",
        "skills/x/SKILL.md": "# x\n\n```bash\npython3 -m tool\n```\n",
        ".claude/settings.json": json.dumps(settings, indent=2),
        "tests/test_hook.py": 'import subprocess\n\n\ndef test_hook():\n    subprocess.run("python3 tool.py", '
        "shell=True)\n",
    }
    root = snapshot(tmp_path, files)
    entry = entries_named(root, "x: tool")["x: tool"]
    assert entry.kind == "skill_command" and [
        i.kind for i in entry.also_invoked_by
    ] == ["hook_command"]
    out, _ = listing(root, "x: tool")
    assert ids(out["x: tool"]) == ["tests/test_hook.py::test_hook"]


# --- records: dedup, ties and order --------------------------------------------------------


def test_repeated_invocation_is_listed_once_at_its_earliest_line(tmp_path):
    body = (
        IMPORTS
        + 'def test_twice():\n    main(["open"])\n    x = 1\n    main(["open"])\n'
    )
    out, _ = listing(snapshot(tmp_path, {"tests/test_t.py": body}), "open")
    (record,) = out["open"].e2e
    assert record.invocation_line == line_of(body, 'main(["open"])', 1)


def test_earliest_line_wins_over_match_kind_order(tmp_path):
    body = IMPORTS + src("""
        def test_both():
            subprocess.run([sys.executable, "-m", "pkg", "open"])
            handle_open(None)
        """)
    out, _ = listing(snapshot(tmp_path, {"tests/test_t.py": body}), "open")
    (record,) = out["open"].e2e
    assert (record.match, record.invocation_line) == (
        "subprocess",
        line_of(body, "subprocess.run"),
    )


def test_matches_on_one_line_prefer_direct_call(tmp_path):
    body = IMPORTS + 'def test_tie():\n    assert main(["open"]) == handle_open(None)\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_t.py": body}), "open")
    (record,) = out["open"].e2e
    assert (record.match, record.invocation_line) == (
        "direct_call",
        line_of(body, "assert main"),
    )


def test_one_test_appears_on_two_paths(tmp_path):
    body = IMPORTS + 'def test_both():\n    main(["open"])\n    main(["render"])\n'
    out, _ = listing(snapshot(tmp_path, {"tests/test_t.py": body}), "open", "render")
    assert ids(out["open"]) == ids(out["render"]) == ["tests/test_t.py::test_both"]


def test_records_are_sorted_by_file_line_and_node_id(tmp_path):
    many = IMPORTS + "".join(
        f"def test_{n}():\n    handle_open(None)\n\n\n"
        for n in ("zeta", "alpha", "mid")
    )
    one = "from pkg.cli import handle_open\n\n\ndef test_a():\n    handle_open(None)\n"
    root = snapshot(
        tmp_path,
        {"tests/b/test_b.py": many, "tests/a/test_a.py": one, "tests/test_c.py": one},
    )
    out, _ = listing(root, "open")
    assert [(t.file, t.line, t.node_id) for t in out["open"].e2e] == [
        ("tests/a/test_a.py", 4, "tests/a/test_a.py::test_a"),
        (
            "tests/b/test_b.py",
            line_of(many, "def test_zeta"),
            "tests/b/test_b.py::test_zeta",
        ),
        (
            "tests/b/test_b.py",
            line_of(many, "def test_alpha"),
            "tests/b/test_b.py::test_alpha",
        ),
        (
            "tests/b/test_b.py",
            line_of(many, "def test_mid"),
            "tests/b/test_b.py::test_mid",
        ),
        ("tests/test_c.py", 4, "tests/test_c.py::test_a"),
    ]


class _ReversedScandir:
    """`os.scandir` with its entries in reverse order."""

    def __init__(self, real, path):
        with real(path) as it:
            self._entries = iter(list(reversed(list(it))))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def close(self):
        pass


def test_reversed_filesystem_enumeration_gives_byte_identical_listings(
    tmp_path, monkeypatch
):
    calls = IMPORTS + "".join(
        f'def test_{n}():\n    main(["open"])\n    handle_render(None)\n\n\n'
        for n in ("b", "a", "c")
    )
    files = {f"tests/{d}/test_{d}.py": calls for d in ("x", "y", "z")}
    files["tests/test_broken.py"] = "def test_(:\n"
    files["tests/test_bad.py"] = b"x = '\xff\xfe'\n"
    root = snapshot(tmp_path, files)

    def dump() -> str:
        out, skips = listing(root, "open", "render")
        return json.dumps(
            {
                "lists": {
                    n: listed.model_dump(mode="json") for n, listed in out.items()
                },
                "skips": [s.model_dump(mode="json") for s in skips],
            },
            ensure_ascii=False,
        )

    first = dump()
    real_scandir, real_listdir = os.scandir, os.listdir
    monkeypatch.setattr(
        os, "scandir", lambda path=".": _ReversedScandir(real_scandir, path)
    )
    monkeypatch.setattr(
        os, "listdir", lambda path=".": list(reversed(real_listdir(path)))
    )
    assert dump() == first
    assert len(json.loads(first)["lists"]["open"]["e2e"]) == 9


# --- empty lists and unreadable files ------------------------------------------------------


def test_a_snapshot_without_test_files_gives_empty_lists_for_every_path(tmp_path):
    out, skips = listing(
        snapshot(tmp_path, {}), "open", "render", "pkg", "mytool", "cli sync"
    )
    assert {n: (listed.e2e, listed.mocks_hop_2) for n, listed in out.items()} == {
        n: ([], []) for n in ("open", "render", "pkg", "mytool", "cli sync")
    }
    assert skips == []


def test_a_syntax_error_file_is_skipped_and_the_rest_still_listed(tmp_path):
    ok = "from pkg.cli import handle_open\n\n\ndef test_ok():\n    handle_open(None)\n"
    root = snapshot(
        tmp_path,
        {
            "tests/test_broken.py": "def test_broken(:\n    pass\n",
            "tests/test_ok.py": ok,
            "tests/test_empty.py": "",
        },
    )
    out, skips = listing(root, "open")
    assert ids(out["open"]) == ["tests/test_ok.py::test_ok"]
    assert [s.model_dump() for s in skips] == [
        {"file": "tests/test_broken.py", "reason": "syntax_error"}
    ]
    index = lib("E2EIndex").build(root, lib("PyIndex").build(root))
    assert index.skips() == skips


def test_a_non_utf8_file_is_skipped_with_decode_error(tmp_path):
    root = snapshot(
        tmp_path, {"tests/test_latin.py": b"def test_x():\n    s = '\xe9\xff'\n"}
    )
    _, skips = listing(root, "open")
    assert [s.model_dump() for s in skips] == [
        {"file": "tests/test_latin.py", "reason": "decode_error"}
    ]


# --- never executed ------------------------------------------------------------------------


def test_listing_never_imports_runs_or_spawns_snapshot_code(tmp_path, monkeypatch):
    marker = tmp_path / "ran.txt"
    body = (
        sentinel_prelude(marker)
        + "\nfrom pkg.cli import handle_open\n\n\ndef test_x():\n    handle_open(None)\n"
    )
    root = snapshot(
        tmp_path,
        {
            "tests/test_side_effects.py": body,
            "tests/conftest.py": sentinel_prelude(marker),
            "tests/helpers.py": sentinel_prelude(marker),
        },
    )

    def refuse(*args, **kwargs):
        raise AssertionError("listing spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    out, _ = listing(root, "open")
    assert ids(out["open"]) == ["tests/test_side_effects.py::test_x"]
    assert not marker.exists()


# --- helper inlining -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper,call",
    [
        ("def run(argv):\n    return main(argv)\n", 'run(["open", "--x"])'),
        ("def run(argv):\n    return main(list(argv))\n", 'run(("open",))'),
        ('def run(argv):\n    return main(["open", *argv])\n', 'run(["--draft"])'),
        (
            "def run(prefix, argv):\n    return main([*prefix, *argv])\n",
            'run(["open"], ["--x"])',
        ),
    ],
    ids=["param", "list-of-param", "literal-then-param", "prefix-then-param"],
)
def test_a_helper_forwarding_literal_argv_matches_at_the_test_call_line(
    tmp_path, helper, call
):
    body = IMPORTS + helper + f"\n\ndef test_h():\n    x = 0\n    {call}\n"
    out, _ = listing(snapshot(tmp_path, {"tests/test_h.py": body}), "open", "render")
    record = rec(out["open"], "tests/test_h.py::test_h")
    assert (record.match, record.invocation_line) == ("cli_argv", line_of(body, call))
    assert out["render"].e2e == []


def test_helpers_from_conftest_and_the_tests_tree_are_inlined(tmp_path):
    helper = (
        "from pkg.__main__ import main\n\n\ndef {name}(argv):\n    return main(argv)\n"
    )
    files = {
        "tests/conftest.py": helper.format(name="cli"),
        "tests/support/drive.py": helper.format(name="drive"),
        "tests/test_c.py": 'from conftest import cli\n\n\ndef test_c():\n    cli(["open"])\n',
        "tests/test_d.py": 'from support.drive import drive\n\n\ndef test_d():\n    drive(["open"])\n',
    }
    out, _ = listing(snapshot(tmp_path, files), "open")
    assert [(t.node_id, t.match) for t in out["open"].e2e] == [
        ("tests/test_c.py::test_c", "cli_argv"),
        ("tests/test_d.py::test_d", "cli_argv"),
    ]


def test_inlining_goes_two_levels_deep_and_no_further(tmp_path):
    body = IMPORTS + src("""
        def one(argv):
            return main(argv)


        def two(argv):
            return one(argv)


        def three(argv):
            return two(argv)


        def test_two_levels():
            two(["open"])


        def test_three_levels():
            three(["open"])


        def test_non_literal():
            argv = ["open"]
            one(argv)
        """)
    out, _ = listing(snapshot(tmp_path, {"tests/test_deep.py": body}), "open")
    assert ids(out["open"]) == ["tests/test_deep.py::test_two_levels"]


# --- the produced interfaces ---------------------------------------------------------------


def test_match_invocations_gives_the_records_list_e2e_tests_publishes(tmp_path):
    body = (
        IMPORTS
        + 'def test_a():\n    main(["open"])\n\n\ndef test_b():\n    handle_open(None)\n'
    )
    root = snapshot(tmp_path, {"tests/test_m.py": body})
    entry = entries_named(root, "open")["open"]
    index = lib("E2EIndex").build(root, lib("PyIndex").build(root))
    matched = lib("match_invocations")(index, entry, root=root)
    out, _ = listing(root, "open")
    assert [t.model_dump() for t in matched] == [
        t.model_dump() for t in out["open"].e2e
    ]
    assert [t.node_id for t in matched] == [
        "tests/test_m.py::test_a",
        "tests/test_m.py::test_b",
    ]


def test_map_paths_fills_tests_and_listing_skips_with_no_extra_model_call(tmp_path):
    test_cli = (
        'from pkg.cli import main\n\n\ndef test_opal():\n    main(["opal", "--fast"])\n'
    )
    base = cli_snapshot(tmp_path / "base", ["opal", "ruby"])
    snap = commit_snapshot(
        tmp_path / "snap",
        {
            **base.files,
            "tests/test_cli.py": test_cli,
            "tests/test_broken.py": "def test_(:\n",
        },
    )
    call = FakeCall(trace=lambda name: {"hops": chain_hops(snap, name)})
    result = run_map(snap, call)
    by_name = {p.entry.name: p for p in result.paths}
    assert [
        (t.node_id, t.match, t.invocation_line) for t in by_name["opal"].tests.e2e
    ] == [("tests/test_cli.py::test_opal", "cli_argv", 5)]
    assert by_name["ruby"].tests.e2e == [] and by_name["ruby"].tests.mocks_hop_2 == []
    assert [s.model_dump() for s in result.test_listing_skips] == [
        {"file": "tests/test_broken.py", "reason": "syntax_error"}
    ]
    assert sorted(c.stage for c in call.calls) == ["trace", "trace"]


# --- the live pr skill ---------------------------------------------------------------------


@pytest.mark.skipif(
    not (LIVE_PR / "pr_pipeline" / "__main__.py").exists(),
    reason="skills/pr/scripts absent",
)
def test_live_pr_scripts_list_the_tests_that_go_through_the_run_helper(tmp_path):
    root = tmp_path / "pr"
    shutil.copytree(
        LIVE_PR,
        root,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"),
    )
    out, skips = listing(root, "open", "edit", "render")
    assert skips == []
    listed = {n: {t.node_id: t for t in out[n].e2e} for n in out}
    node = "tests/test_commands.py::{}".format
    opened = listed["open"][
        node("test_open_pushes_and_opens_a_draft_pr_from_a_body_file")
    ]
    text = (root / "tests" / "test_commands.py").read_text()
    assert (opened.match, opened.invocation_line) == (
        "cli_argv",
        line_of(text, 'assert run(["open"'),
    )
    assert node("test_render_prints_only_the_body_and_calls_no_gh") in listed["render"]
    assert node("test_edit_preserves_a_human_change_outside_markers") in listed["edit"]
    assert (
        node("test_edit_keeps_a_ticket_link_given_only_to_open")
        in listed["open"].keys() & listed["edit"].keys()
    )
    assert (
        node("test_render_prints_only_the_body_and_calls_no_gh") not in listed["open"]
    )
    # It only parses: argparse refuses the argv and no handler runs.
    assert node("test_open_draft_and_ready_together_are_refused") not in listed["open"]
