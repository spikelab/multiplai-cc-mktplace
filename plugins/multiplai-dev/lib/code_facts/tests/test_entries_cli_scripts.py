"""Block 5: click/typer, `[project.scripts]` and `__main__` discovery, and merging entries that share a handler.

Every snapshot is generated into `tmp_path`; no fixture commits a real `pyproject.toml`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from conftest import line_of, make_tree, sentinel_prelude
from trace_fixtures import FakeCall, commit_snapshot, lib


def entries_mod():
    return importlib.import_module("code_facts.entries")


def discover(root, seeds=()):
    return entries_mod().discover_entries(root, seeds)


def of_kind(disc, kind):
    return [e for e in disc.entries if e.kind == kind]


def by_name(entries):
    return {e.name: e for e in entries}


def edges(entry):
    return {(k.from_.path, k.from_.line, k.to.path, k.to.line) for k in entry.known_edges}


def handler(entry):
    return None if entry.handler is None else (entry.handler.path, entry.handler.line, entry.handler.symbol)


def diags(disc, reason):
    return [(d.path, d.line) for d in disc.diagnostics if d.reason == reason]


PYPROJECT_HEAD = '[project]\nname = "tool"\nversion = "0.1.0"\n\n[project.scripts]\n'


# --- click ---------------------------------------------------------------------------------------

CLICK = """\
import click


@click.group()
def cli():
    pass


@cli.command()
def build_all():
    pass


@cli.command("ship")
def do_ship():
    pass


@cli.command(name="deploy")
def do_deploy():
    pass


@cli.command()
@click.option("--force", is_flag=True)
def with_opt(force):
    pass


@cli.group()
def db():
    pass


@db.command()
def migrate():
    pass


@click.command()
def standalone_tool():
    pass
"""


class TestClick:
    def test_names_locations_and_handlers(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"app/__init__.py": "", "app/cli.py": CLICK})
        found = by_name(of_kind(discover(root), "click_command"))
        expected = {
            "cli": ("@click.group()", "def cli("),
            "cli build-all": ("@cli.command()", "def build_all("),
            "cli ship": ('@cli.command("ship")', "def do_ship("),
            "cli deploy": ('@cli.command(name="deploy")', "def do_deploy("),
            "cli with-opt": ("@cli.command()", "def with_opt("),
            "cli db": ("@cli.group()", "def db("),
            "cli db migrate": ("@db.command()", "def migrate("),
            "standalone-tool": ("@click.command()", "def standalone_tool("),
        }
        assert set(found) == set(expected)
        for name, (decorator, def_line) in expected.items():
            e = found[name]
            def_at = line_of(CLICK, def_line)
            # The decorator line is the nearest matching decorator above the def.
            decorator_at = max(i for i, text in enumerate(CLICK.splitlines(), start=1)
                               if text == decorator and i < def_at)
            symbol = def_line.removeprefix("def ").removesuffix("(")
            assert (e.location.path, e.location.line) == ("app/cli.py", decorator_at), name
            assert handler(e) == ("app/cli.py", def_at, symbol), name
            assert e.invocation is None and e.also_invoked_by == []

    def test_explicit_name_is_used_not_the_function_name(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": CLICK})
        names = {e.name for e in of_kind(discover(root), "click_command")}
        assert {"cli ship", "cli deploy"} <= names
        assert not any(n.endswith("do-ship") or n.endswith("do-deploy") for n in names)

    def test_stacked_decorators_anchor_on_the_command_decorator(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": CLICK})
        e = by_name(of_kind(discover(root), "click_command"))["cli with-opt"]
        assert e.location.line == line_of(CLICK, "def with_opt(") - 2


# --- typer ---------------------------------------------------------------------------------------

TYPER = """\
import typer

app = typer.Typer()
users_app = typer.Typer()
app.add_typer(users_app, name="users")


@app.callback()
def main_callback():
    pass


@app.command()
def hello_world():
    pass


@app.command("ship")
def do_ship():
    pass


@users_app.command()
def add_user():
    pass


@users_app.command(name="remove")
def rm_user():
    pass
"""


class TestTyper:
    def test_commands_with_sub_app_prefix(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": TYPER})
        found = by_name(of_kind(discover(root), "typer_command"))
        expected = {
            "hello-world": ("@app.command()", "def hello_world("),
            "ship": ('@app.command("ship")', "def do_ship("),
            "users add-user": ("@users_app.command()", "def add_user("),
            "users remove": ('@users_app.command(name="remove")', "def rm_user("),
        }
        for name, (decorator, def_line) in expected.items():
            assert name in found, (name, sorted(found))
            e = found[name]
            assert e.location.line == line_of(TYPER, decorator)
            symbol = def_line.removeprefix("def ").removesuffix("(")
            assert handler(e) == ("cli.py", line_of(TYPER, def_line), symbol)
        assert not any(n.endswith("add_user") or n.endswith("rm-user") for n in found)

    def test_callback_is_an_entry_at_its_decorator(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": TYPER})
        callbacks = [e for e in of_kind(discover(root), "typer_command")
                     if e.location.line == line_of(TYPER, "@app.callback()")]
        assert len(callbacks) == 1
        assert handler(callbacks[0]) == ("cli.py", line_of(TYPER, "def main_callback("), "main_callback")

    def test_typer_is_not_reported_as_click(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": TYPER})
        disc = discover(root)
        assert of_kind(disc, "typer_command") and of_kind(disc, "click_command") == []


# --- [project.scripts] ----------------------------------------------------------------------------


class TestProjectScripts:
    def test_flat_layout_entry_and_edge(self, tmp_path):
        pyproject = PYPROJECT_HEAD + 'tool = "pkg.cli:main"\nother = "pkg.cli:other"\n'
        cli = "def main():\n    return 0\n\n\ndef other():\n    return 1\n"
        root = make_tree(tmp_path / "snap", {"pyproject.toml": pyproject, "pkg/__init__.py": "", "pkg/cli.py": cli})
        found = by_name(of_kind(discover(root), "project_script"))
        assert set(found) == {"tool", "other"}
        for name, symbol in (("tool", "main"), ("other", "other")):
            e = found[name]
            key_line = line_of(pyproject, f"{name} = ")
            def_line = line_of(cli, f"def {symbol}(")
            assert (e.location.path, e.location.line) == ("pyproject.toml", key_line)
            assert handler(e) == ("pkg/cli.py", def_line, symbol)
            assert ("pyproject.toml", key_line, "pkg/cli.py", def_line) in edges(e)
            assert e.invocation is None

    def test_src_layout(self, tmp_path):
        cli = "import sys\n" + "\n" * 10 + "def main():\n    return 0\n"
        assert line_of(cli, "def main(") == 12
        pyproject = PYPROJECT_HEAD + 'mytool = "mytool.cli:main"\n'
        root = make_tree(tmp_path / "snap", {
            "pyproject.toml": pyproject, "src/mytool/__init__.py": "", "src/mytool/cli.py": cli})
        (e,) = of_kind(discover(root), "project_script")
        assert e.name == "mytool"
        assert handler(e) == ("src/mytool/cli.py", 12, "main")
        assert edges(e) == {("pyproject.toml", line_of(pyproject, "mytool ="), "src/mytool/cli.py", 12)}

    def test_external_target_is_unresolved_target_at_the_key_line(self, tmp_path):
        pyproject = PYPROJECT_HEAD + 'ext = "thirdparty.cli:main"\ntool = "pkg.cli:main"\nnodef = "pkg.cli:missing"\n'
        root = make_tree(tmp_path / "snap", {
            "pyproject.toml": pyproject, "pkg/__init__.py": "", "pkg/cli.py": "def main():\n    pass\n"})
        disc = discover(root)
        assert [e.name for e in of_kind(disc, "project_script")] == ["tool"]
        assert sorted(diags(disc, "unresolved_target")) == [
            ("pyproject.toml", line_of(pyproject, "ext =")), ("pyproject.toml", line_of(pyproject, "nodef ="))]

    def test_invalid_toml_is_a_parse_error_and_other_files_still_count(self, tmp_path):
        good = PYPROJECT_HEAD + 'run = "tool.cli:main"\n'
        root = make_tree(tmp_path / "snap", {
            "repo_a/pyproject.toml": "[project\nname = 'broken'\n",
            "repo_b/pyproject.toml": good,
            "repo_b/tool/__init__.py": "", "repo_b/tool/cli.py": "def main():\n    pass\n"})
        disc = discover(root)
        assert ("repo_a/pyproject.toml", None) in diags(disc, "parse_error")
        assert [(e.name, e.location.path) for e in of_kind(disc, "project_script")] == [
            ("run", "repo_b/pyproject.toml")]

    def test_two_repositories_report_snapshot_relative_paths(self, tmp_path):
        files = {}
        for repo in ("repo_a", "repo_b"):
            files[f"{repo}/pyproject.toml"] = PYPROJECT_HEAD + 'run = "tool.cli:main"\n'
            files[f"{repo}/tool/__init__.py"] = ""
            files[f"{repo}/tool/cli.py"] = "def main():\n    pass\n"
        root = make_tree(tmp_path / "snap", files)
        found = sorted(of_kind(discover(root), "project_script"), key=lambda e: e.location.path)
        assert [(e.location.path, e.handler.path) for e in found] == [
            ("repo_a/pyproject.toml", "repo_a/tool/cli.py"), ("repo_b/pyproject.toml", "repo_b/tool/cli.py")]

    def test_resolution_never_crosses_into_another_repository(self, tmp_path):
        root = make_tree(tmp_path / "snap", {
            "repo_a/pyproject.toml": PYPROJECT_HEAD + 'run = "tool.cli:main"\n',
            "repo_b/pyproject.toml": '[project]\nname = "b"\n',
            "repo_b/tool/__init__.py": "", "repo_b/tool/cli.py": "def main():\n    pass\n"})
        disc = discover(root)
        assert of_kind(disc, "project_script") == []
        assert [p for p, _ in diags(disc, "unresolved_target")] == ["repo_a/pyproject.toml"]


# --- __main__ modules ------------------------------------------------------------------------------


class TestMainModule:
    def test_package_main_without_guard_is_anchored_at_line_1(self, tmp_path):
        cli = "def main():\n    return 0\n"
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": cli,
            "pkg/__main__.py": "from pkg.cli import main\n\nmain()\n"})
        (e,) = of_kind(discover(root), "main_module")
        assert (e.name, e.location.path, e.location.line) == ("pkg", "pkg/__main__.py", 1)
        assert handler(e) == ("pkg/cli.py", 1, "main")
        assert edges(e) == {("pkg/__main__.py", 1, "pkg/cli.py", 1)}

    def test_package_main_with_guard_is_anchored_at_the_guard(self, tmp_path):
        src = "import sys\n\nfrom pkg.cli import main\n\nif __name__ == \"__main__\":\n    sys.exit(main(sys.argv[1:]))\n"
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": "def main(argv):\n    return 0\n", "pkg/__main__.py": src})
        (e,) = of_kind(discover(root), "main_module")
        guard = line_of(src, "if __name__")
        assert (e.name, e.location.line) == ("pkg", guard)
        assert handler(e) == ("pkg/cli.py", 1, "main")
        assert edges(e) == {("pkg/__main__.py", guard, "pkg/cli.py", 1)}

    def test_script_guard_in_a_module(self, tmp_path):
        src = "def run():\n    return 0\n\n\nif __name__ == \"__main__\":\n    run()\n"
        root = make_tree(tmp_path / "snap", {"tools/__init__.py": "", "tools/report.py": src})
        (e,) = of_kind(discover(root), "main_module")
        assert (e.name, e.location.path, e.location.line) == ("tools.report", "tools/report.py",
                                                              line_of(src, "if __name__"))
        assert handler(e) == ("tools/report.py", 1, "run")

    @pytest.mark.parametrize("body", [
        "sys.exit(main(sys.argv[1:]))",
        "exit(main())",
        "raise SystemExit(main())",
        "asyncio.run(main())",
        "print('starting')\n    main()",
    ])
    def test_transparent_wrappers_and_unresolvable_callees_are_looked_through(self, tmp_path, body):
        src = ("import asyncio\nimport sys\n\n\nasync def helper():\n    pass\n\n\n"
               f"def main(argv=None):\n    return 0\n\n\nif __name__ == \"__main__\":\n    {body}\n")
        root = make_tree(tmp_path / "snap", {"tool.py": src})
        (e,) = of_kind(discover(root), "main_module")
        assert handler(e) == ("tool.py", line_of(src, "def main("), "main")

    @pytest.mark.parametrize("body", ["first()\n    second()", "pass", "value = 1"])
    def test_zero_or_several_resolved_callees_leave_the_handler_null(self, tmp_path, body):
        src = ("def first():\n    pass\n\n\ndef second():\n    pass\n\n\n"
               f"if __name__ == \"__main__\":\n    {body}\n")
        root = make_tree(tmp_path / "snap", {"tool.py": src})
        (e,) = of_kind(discover(root), "main_module")
        assert e.location.line == line_of(src, "if __name__")
        assert e.handler is None and e.known_edges == []

    def test_test_files_are_excluded(self, tmp_path):
        guard = "def run():\n    pass\n\n\nif __name__ == \"__main__\":\n    run()\n"
        root = make_tree(tmp_path / "snap", {
            "tests/test_report.py": guard, "tests/helpers.py": guard, "pkg/tests/fixture_tool.py": guard,
            "report_test.py": guard, "test_top.py": guard, "conftest.py": guard,
            "tools/report.py": guard})
        assert [e.location.path for e in of_kind(discover(root), "main_module")] == ["tools/report.py"]

    def test_main_module_handler_directly(self, tmp_path):
        src = "import sys\n\nfrom pkg.cli import main\n\nif __name__ == \"__main__\":\n    sys.exit(main())\n"
        root = make_tree(tmp_path / "snap", {
            "pkg/__init__.py": "", "pkg/cli.py": "def main():\n    pass\n", "pkg/__main__.py": src,
            "pkg/bare/__init__.py": "", "pkg/bare/__main__.py": "print('hi')\n"})
        index = lib("PyIndex").build(root)
        location, found = entries_mod().main_module_handler(index, "pkg/__main__.py")
        assert (location.path, location.line) == ("pkg/__main__.py", line_of(src, "if __name__"))
        assert (found.path, found.line, found.symbol) == ("pkg/cli.py", 1, "main")
        location, found = entries_mod().main_module_handler(index, "pkg/bare/__main__.py")
        assert (location.path, location.line, found) == ("pkg/bare/__main__.py", 1, None)


# --- no execution --------------------------------------------------------------------------------


def test_discovery_never_runs_main_or_click_modules(tmp_path):
    sentinel = tmp_path / "sentinel"
    root = make_tree(tmp_path / "snap", {
        "pkg/__init__.py": "",
        "pkg/cli.py": "def main():\n    pass\n",
        "pkg/__main__.py": sentinel_prelude(sentinel) + "from pkg.cli import main\nmain()\n",
        "app.py": sentinel_prelude(sentinel) + CLICK,
    })
    disc = discover(root)
    assert of_kind(disc, "main_module") and of_kind(disc, "click_command")
    assert not sentinel.exists()


def test_repeat_and_reordered_seed_runs_are_byte_identical(tmp_path):
    root = make_tree(tmp_path / "snap", {
        "pyproject.toml": PYPROJECT_HEAD + 'tool = "app.cli:cli"\n', "app/__init__.py": "",
        "app/cli.py": CLICK, "app/typer_cli.py": TYPER,
        "tools/report.py": "def run():\n    pass\n\n\nif __name__ == \"__main__\":\n    run()\n"})
    seeds = [lib("Seed")(path="app/typer_cli.py", symbol="add_user"), lib("Seed")(path="tools/report.py", line=1)]
    first = discover(root, seeds).model_dump_json(by_alias=True)
    assert {"click_command", "typer_command", "main_module"} <= {e["kind"] for e in json.loads(first)["entries"]}
    assert discover(root, list(reversed(seeds))).model_dump_json(by_alias=True) == first


# --- merging ---------------------------------------------------------------------------------------


def make_entry(kind, name, path, line, h=None, edges_=()):
    m = entries_mod()
    return m.Entry(kind=kind, name=name, location=lib("Location")(path=path, line=line),
                   handler=None if h is None else lib("Handler")(path=h[0], line=h[1], symbol=h[2]),
                   invocation=None,
                   known_edges=[m.KnownEdge(from_=lib("Location")(path=a, line=b), to=lib("Location")(path=c, line=d))
                                for a, b, c, d in edges_],
                   also_invoked_by=[])


def merge(entries):
    return sorted(entries_mod().merge_entries(entries), key=lambda e: (e.location.path, e.location.line, e.kind))


def invokers(entry):
    return [(i.kind, i.name, i.location.path, i.location.line) for i in entry.also_invoked_by]


class TestMergeEntries:
    H = ("pkg/cli.py", 4, "main")

    def test_best_rank_wins_and_the_rest_become_sorted_invokers(self):
        script = make_entry("project_script", "tool", "pyproject.toml", 6, self.H,
                            [("pyproject.toml", 6, "pkg/cli.py", 4)])
        main = make_entry("main_module", "pkg", "pkg/__main__.py", 5, self.H, [("pkg/__main__.py", 5, "pkg/cli.py", 4)])
        seed = make_entry("seed", "pkg/cli.py:main", "pkg/cli.py", 4, self.H)
        (kept,) = merge([seed, script, main])
        assert (kept.kind, kept.name, kept.location.line) == ("main_module", "pkg", 5)
        assert invokers(kept) == [("seed", "pkg/cli.py:main", "pkg/cli.py", 4),
                                  ("project_script", "tool", "pyproject.toml", 6)]
        assert edges(kept) == {("pyproject.toml", 6, "pkg/cli.py", 4), ("pkg/__main__.py", 5, "pkg/cli.py", 4)}

    def test_ties_go_to_the_smallest_location_and_edges_are_not_duplicated(self):
        shared = ("pkg/__main__.py", 1, "pkg/cli.py", 4)
        a = make_entry("project_script", "zz", "b/pyproject.toml", 2, self.H, [shared])
        b = make_entry("project_script", "aa", "a/pyproject.toml", 9, self.H, [shared])
        (kept,) = merge([a, b])
        assert (kept.name, kept.location.path) == ("aa", "a/pyproject.toml")
        assert invokers(kept) == [("project_script", "zz", "b/pyproject.toml", 2)]
        assert len(kept.known_edges) == 1

    def test_null_handlers_at_different_lines_stay_separate(self):
        a = make_entry("argparse_subcommand", "open", "cli.py", 10)
        b = make_entry("argparse_subcommand", "close", "cli.py", 11)
        assert [e.name for e in merge([a, b])] == ["open", "close"]

    def test_null_handlers_at_the_same_kind_and_location_merge(self):
        a = make_entry("hook_command", "Stop", "hooks.json", 3)
        b = make_entry("hook_command", "SubagentStop", "hooks.json", 3)
        assert len(merge([a, b])) == 1

    def test_non_def_handlers_merge_only_on_kind_and_location(self):
        launcher = ("hooks/run.sh", 1, None)
        a = make_entry("hook_command", "PreToolUse:Bash", "hooks/hooks.json", 5, launcher)
        b = make_entry("hook_command", "Stop", "hooks/hooks.json", 9, launcher)
        c = make_entry("skill_command", "x: run", "hooks/hooks.json", 5, launcher)
        assert sorted((e.kind, e.location.line) for e in merge([a, b, c])) == [
            ("hook_command", 5), ("hook_command", 9), ("skill_command", 5)]
        assert all(e.also_invoked_by == [] for e in merge([a, b, c]))

    def test_paths_entry_publishes_invoker_kinds_as_output_kinds(self):
        script = make_entry("project_script", "tool", "pyproject.toml", 6, self.H)
        seed = make_entry("seed", "s", "pkg/cli.py", 4, self.H)
        main = make_entry("main_module", "pkg", "pkg/__main__.py", 1, self.H)
        (kept,) = merge([script, seed, main])
        published = entries_mod().to_path_entry(kept)
        assert published.kind == "main"
        assert [i.kind for i in published.also_invoked_by] == ["seed", "script"]


class TestMergeInDiscovery:
    def test_seed_duplicating_a_click_handler_is_folded_in(self, tmp_path):
        root = make_tree(tmp_path / "snap", {"cli.py": CLICK})
        disc = discover(root, [lib("Seed")(path="cli.py", symbol="build_all")])
        assert of_kind(disc, "seed") == []
        e = by_name(of_kind(disc, "click_command"))["cli build-all"]
        assert invokers(e) == [("seed", "cli.py:build_all", "cli.py", line_of(CLICK, "def build_all("))]


# --- through map_paths ------------------------------------------------------------------------------

SCRIPT_PYPROJECT = PYPROJECT_HEAD + 'tool = "pkg.cli:main"\n'
SCRIPT_CLI = "def main(argv=None):\n    return run(argv)\n\n\ndef run(argv):\n    return 0\n"
SCRIPT_MAIN = "import sys\n\nfrom pkg.cli import main\n\nif __name__ == \"__main__\":\n    sys.exit(main(sys.argv[1:]))\n"


def run_map(snap, call):
    return asyncio.run(lib("map_paths")(root=snap.root, commit=snap.head, resolve=snap.resolve, call=call,
                                        fallback=False))


def written(result, tmp_path):
    out = tmp_path / "paths.json"
    lib("write_paths_file")(result, out)
    return json.loads(out.read_text())


def test_script_and_package_main_produce_one_main_path(tmp_path):
    snap = commit_snapshot(tmp_path / "repo", {
        "pyproject.toml": SCRIPT_PYPROJECT, "pkg/__init__.py": "", "pkg/cli.py": SCRIPT_CLI,
        "pkg/__main__.py": SCRIPT_MAIN})
    key_line = line_of(SCRIPT_PYPROJECT, "tool =")
    call = FakeCall(trace=lambda _: {"hops": [snap.hop("pkg/cli.py", "main"), snap.hop("pkg/cli.py", "run")]})
    result = run_map(snap, call)

    doc = written(result, tmp_path)
    (path,) = doc["paths"]
    assert path["entry"]["kind"] == "main"
    assert path["entry"]["location"] == {"path": "pkg/__main__.py", "line": line_of(SCRIPT_MAIN, "if __name__")}
    assert path["entry"]["also_invoked_by"] == [
        {"kind": "script", "name": "tool", "location": {"path": "pyproject.toml", "line": key_line}}]
    assert doc["dropped"] == []
    (trace,) = [c for c in call.calls if c.stage == "trace"]
    assert f"pyproject.toml:{key_line} -> pkg/cli.py:1" in trace.prompt


def test_seed_on_a_click_handler_is_listed_as_an_invoker(tmp_path):
    snap = commit_snapshot(tmp_path / "repo", {"cli.py": CLICK})
    answer = lambda _: {"hops": [snap.hop("cli.py", "build_all")]}  # noqa: E731
    call = FakeCall(trace=answer, reask=answer)
    result = asyncio.run(lib("map_paths")(
        root=snap.root, commit=snap.head, resolve=snap.resolve, call=call, fallback=False,
        seeds=[lib("Seed")(path="cli.py", symbol="build_all", name="my seed")]))
    doc = written(result, tmp_path)
    (path,) = [p for p in doc["paths"] if p["entry"]["name"] == "cli build-all"]
    assert path["entry"]["kind"] == "click"
    assert path["entry"]["also_invoked_by"] == [
        {"kind": "seed", "name": "my seed", "location": {"path": "cli.py", "line": line_of(CLICK, "def build_all(")}}]
    assert not any(p["entry"]["kind"] == "seed" for p in doc["paths"])
    assert not any(d["entry"]["kind"] == "seed" for d in doc["dropped"])


def test_null_handler_argparse_entries_stay_separate_paths(tmp_path):
    src = ("import argparse\n\n\ndef main(argv=None):\n    parser = argparse.ArgumentParser()\n"
           "    sub = parser.add_subparsers(dest='command')\n    sub.add_parser('opal')\n    sub.add_parser('ruby')\n"
           "    args = parser.parse_args(argv)\n    if args.command == 'opal':\n        return 1\n    return 0\n")
    snap = commit_snapshot(tmp_path / "repo", {"cli.py": src})

    def answer(name):
        return {"hops": [snap.hop("cli.py", "main", line=snap.line("cli.py", f"add_parser('{name}')"))]}

    result = run_map(snap, FakeCall(trace=answer, names=["opal", "ruby"]))
    entries = sorted((p.entry.name, p.entry.location.line, p.entry.handler, p.entry.also_invoked_by)
                     for p in result.paths)
    assert entries == [("opal", snap.line("cli.py", "add_parser('opal')"), None, []),
                       ("ruby", snap.line("cli.py", "add_parser('ruby')"), None, [])]
