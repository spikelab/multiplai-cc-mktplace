"""Scoping a run to changed lines: import reachability, trace limit, coverage, order, cap and drop records."""

from __future__ import annotations

import json
import random

import pytest

from conftest import make_tree
from trace_fixtures import FakeCall, commit_snapshot, lib, run_map

CORE = "pkg/core.py"
SERVICE = "pkg/service.py"
CLI = "pkg/cli.py"
TOOLS = "pkg/tools.py"

CORE_SRC = """\
def run_core():
    return 42


def other_core():
    return 7
"""

SERVICE_SRC = """\
from pkg.core import other_core, run_core


def serve():
    return run_core()


def serve_other():
    return other_core()
"""

CLI_SRC = """\
import argparse

from pkg.service import serve, serve_other


def cmd_opal(args):
    return serve()


def cmd_ruby(args):
    return serve_other()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tool')
    sub = parser.add_subparsers(dest='command', required=True)
    p_opal = sub.add_parser('opal')
    p_opal.set_defaults(func=cmd_opal)
    p_ruby = sub.add_parser('ruby')
    p_ruby.set_defaults(func=cmd_ruby)
    args = parser.parse_args(argv)
    return args.func(args)
"""

TOOLS_SRC = """\
import argparse

import requests


def cmd_jade(args):
    return 0


def hidden_job():
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tools')
    sub = parser.add_subparsers(dest='command', required=True)
    p_jade = sub.add_parser('jade')
    p_jade.set_defaults(func=cmd_jade)
    args = parser.parse_args(argv)
    return args.func(args)
"""


def cycle_src(name: str, other: str) -> str:
    return (f"import argparse\n\nimport pkg.{other}\n\n\ndef cmd_{name}(args):\n    return 0\n\n\n"
            "def main(argv=None):\n    parser = argparse.ArgumentParser(prog='c')\n"
            "    sub = parser.add_subparsers(dest='command', required=True)\n"
            f"    p = sub.add_parser({name!r})\n    p.set_defaults(func=cmd_{name})\n"
            "    args = parser.parse_args(argv)\n    return args.func(args)\n")


FILES = {
    "pkg/__init__.py": "", CORE: CORE_SRC, SERVICE: SERVICE_SRC, CLI: CLI_SRC, TOOLS: TOOLS_SRC,
    "pkg/x.py": cycle_src("onyx", "y"), "pkg/y.py": cycle_src("topaz", "x"),
    "pkg/broken.py": "def oops(:\n",
}
ALL = {"opal", "ruby", "jade", "onyx", "topaz"}


@pytest.fixture
def snap(tmp_path):
    return commit_snapshot(tmp_path / "repo", FILES)


def opal_hops(snap, *, core_end=None):
    return [snap.hop(CLI, "cmd_opal"), snap.hop(SERVICE, "serve"), snap.hop(CORE, "run_core", end=core_end)]


def ruby_hops(snap, *, core_end=None, core_quote=None):
    return [snap.hop(CLI, "cmd_ruby"), snap.hop(SERVICE, "serve_other"),
            snap.hop(CORE, "other_core", end=core_end, quote=core_quote)]


def responder(snap, **overrides):
    hops = {"opal": opal_hops(snap), "ruby": ruby_hops(snap), "jade": [snap.hop(TOOLS, "cmd_jade")],
            "onyx": [snap.hop("pkg/x.py", "cmd_onyx")], "topaz": [snap.hop("pkg/y.py", "cmd_topaz")],
            "amber": [snap.hop(CORE, "run_core")], **overrides}

    def answer(name):
        if name not in hops:
            raise AssertionError(f"no trace request was expected for entry {name!r}")
        return {"hops": hops[name]}
    return answer


def traced(call):
    return sorted(c.entry for c in call.calls if c.stage == "trace")


def drops(result):
    return {d.entry.name: (d.stage, d.reason) for d in result.dropped}


def run_core_line(snap):
    return snap.line(CORE, "def run_core(")


def other_core_line(snap):
    return snap.line(CORE, "def other_core(")


# --- the import graph ----------------------------------------------------------------------------


def index_of(root):
    return lib("PyIndex").build(root)


class TestImportEdges:
    def test_dotted_import_adds_every_parent_package(self, tmp_path):
        root = make_tree(tmp_path / "s", {"a/__init__.py": "", "a/b/__init__.py": "", "a/b/c.py": "",
                                          "main.py": "import a.b.c\n"})
        assert index_of(root).import_edges("main.py") == {"a/__init__.py", "a/b/__init__.py", "a/b/c.py"}

    def test_from_import_adds_the_package_and_the_submodule(self, tmp_path):
        root = make_tree(tmp_path / "s", {"pkg/__init__.py": "", "pkg/util.py": "",
                                          "main.py": "from pkg import util\n"})
        assert index_of(root).import_edges("main.py") == {"pkg/__init__.py", "pkg/util.py"}

    def test_relative_imports_resolve_by_package_path(self, tmp_path):
        root = make_tree(tmp_path / "s", {"pkg/__init__.py": "", "pkg/a.py": "from . import b\n",
                                          "pkg/b.py": "from .sub import c\n", "pkg/sub/__init__.py": "",
                                          "pkg/sub/c.py": ""})
        index = index_of(root)
        assert index.import_edges("pkg/a.py") == {"pkg/__init__.py", "pkg/b.py"}
        assert index.import_edges("pkg/b.py") == {"pkg/sub/__init__.py", "pkg/sub/c.py"}

    def test_function_local_and_try_imports_count_but_type_checking_imports_do_not(self, tmp_path):
        src = ("from typing import TYPE_CHECKING\n"
               "if TYPE_CHECKING:\n    import pkg.tc\n"
               "try:\n    import pkg.opt\nexcept ImportError:\n    pass\n"
               "def f():\n    import pkg.local\n")
        root = make_tree(tmp_path / "s", {"pkg/__init__.py": "", "pkg/tc.py": "", "pkg/opt.py": "",
                                          "pkg/local.py": "", "main.py": src})
        assert index_of(root).import_edges("main.py") == {"pkg/__init__.py", "pkg/opt.py", "pkg/local.py"}

    def test_third_party_import_and_syntax_error_add_no_edges(self, tmp_path):
        root = make_tree(tmp_path / "s", {"lib.py": "", "third.py": "import requests\nimport yaml.loader\n",
                                          "broken.py": "import lib\ndef oops(:\n"})
        index = index_of(root)
        assert index.import_edges("third.py") == frozenset()
        assert index.import_edges("broken.py") == frozenset()

    def test_absolute_imports_fall_back_to_other_repositories_in_sorted_order(self, tmp_path):
        root = make_tree(tmp_path / "s", {
            "repo_a/pyproject.toml": "[project]\nname = 'a'\n",
            "repo_a/tool.py": "import mod\nimport shared\nimport helper\n",
            "repo_a/src/shared.py": "", "repo_a/helper.py": "",
            "repo_b/pyproject.toml": "[project]\nname = 'b'\n",
            "repo_b/src/mod.py": "", "repo_b/shared.py": "", "repo_b/helper.py": "",
            "repo_c/pyproject.toml": "[project]\nname = 'c'\n",
            "repo_c/src/mod.py": "",
        })
        index = index_of(root)
        # Own repository first (root, then src/); other repositories only for what it lacks, repo_b before repo_c.
        assert index.import_edges("repo_a/tool.py") == {"repo_b/src/mod.py", "repo_a/src/shared.py",
                                                         "repo_a/helper.py"}
        # Discovery handler resolution stays inside the repository.
        assert index.resolve_module("repo_a/tool.py", "mod") is None


class TestImportDistance:
    def test_shortest_chain_length_to_any_target(self, snap):
        index = index_of(snap.root)
        assert index.import_distance(CLI, frozenset({CORE})) == 2
        assert index.import_distance(SERVICE, frozenset({CORE})) == 1
        assert index.import_distance(CLI, frozenset({CORE, SERVICE})) == 1
        assert index.import_distance(CORE, frozenset({CORE})) == 0

    def test_no_path_cycle_and_empty_targets_give_none(self, snap):
        index = index_of(snap.root)
        assert index.import_distance(TOOLS, frozenset({CORE})) is None
        assert index.import_distance("pkg/x.py", frozenset({CORE})) is None
        assert index.import_distance(CLI, frozenset()) is None
        assert index.import_distance(CLI, frozenset({"pkg/deleted.py"})) is None

    def test_relative_import_chain(self, tmp_path):
        root = make_tree(tmp_path / "s", {"pkg/__init__.py": "", "pkg/a.py": "from . import b\n",
                                          "pkg/b.py": "from .sub import c\n", "pkg/sub/__init__.py": "",
                                          "pkg/sub/c.py": ""})
        assert index_of(root).import_distance("pkg/a.py", frozenset({"pkg/sub/c.py"})) == 2


# --- entry distance ------------------------------------------------------------------------------


def entry(path, line, *, handler=None, invokers=(), kind="argparse_subcommand"):
    Location = lib("Location")
    h = None if handler is None else lib("Handler")(path=handler[0], line=handler[1], symbol=handler[2])
    inv = [lib("EntryInvoker")(kind="skill_command", name="inv", location=Location(path=p, line=n))
           for p, n in invokers]
    return lib("Entry")(kind=kind, name="e", location=Location(path=path, line=line), handler=h,
                        invocation=None, invoked_by="the caller (seed)", known_edges=[], also_invoked_by=inv)


class TestEntryDistance:
    def distance(self, snap, ent, changed):
        changed = {p: frozenset(lines) for p, lines in changed.items()}
        return lib("entry_distance")(ent, index_of(snap.root), changed)

    def test_zero_when_location_invoker_or_handler_file_changed(self, snap):
        changed = {"skills/x/SKILL.md": {12}}
        assert self.distance(snap, entry("skills/x/SKILL.md", 12, kind="skill_command"), changed) == 0
        assert self.distance(snap, entry(TOOLS, 15, invokers=[("skills/x/SKILL.md", 3)]), changed) == 0
        assert self.distance(snap, entry("hooks/hooks.json", 2, handler=(CORE, 1, "run_core"),
                                         kind="hook_command"), {CORE: {5}}) == 0

    def test_measured_from_the_handler_module_not_the_location(self, snap):
        # The location's module (cli) is 2 imports from core; the handler's (service) is 1.
        assert self.distance(snap, entry(CLI, 18, handler=(SERVICE, 4, "serve")), {CORE: {1}}) == 1

    def test_location_module_when_there_is_no_handler(self, snap):
        assert self.distance(snap, entry(CLI, 18), {CORE: {1}}) == 2

    def test_none_when_unreachable_or_not_python(self, snap):
        assert self.distance(snap, entry(TOOLS, 15, handler=(TOOLS, 6, "cmd_jade")), {CORE: {1}}) is None
        assert self.distance(snap, entry("skills/x/SKILL.md", 12, kind="skill_command"), {CORE: {1}}) is None
        assert self.distance(snap, entry(CLI, 18, handler=(CLI, 6, "cmd_opal")), {}) is None


# --- coverage, order, drop sort ------------------------------------------------------------------


def hop(path, start, end):
    return lib("PathHop")(symbol_called="f", path=path, line_start=start, line_end=end, quote="x")


def locs(items):
    return [(loc.path, loc.line) for loc in items]


class TestCoveredLines:
    def test_distinct_sorted_inclusive_and_path_exact(self):
        hops = [hop("b.py", 10, 12), hop("a.py", 5, 5), hop("b.py", 12, 14), hop("c.py", 1, 99)]
        changed = {"a.py": frozenset({4, 5, 6}), "b.py": frozenset({9, 10, 12, 14, 15}), "z.py": frozenset({1})}
        assert locs(lib("covered_lines")(hops, changed)) == [("a.py", 5), ("b.py", 10), ("b.py", 12),
                                                              ("b.py", 14)]

    def test_nothing_covered(self):
        assert lib("covered_lines")([hop("a.py", 40, 45)], {"a.py": frozenset({10})}) == []


def traced_path(name, *, hops=1, covered=(), kind="argparse", path="a/cli.py", line=1):
    L, E = lib("Location"), lib("PathEntry")
    ent = E(kind=kind, name=name, location=L(path=path, line=line), handler=None, invocation=None,
            invoked_by="the caller (seed)", also_invoked_by=[])
    return lib("TracedPath")(
        id="p1", entry=ent, hops=[hop(path, n, n) for n in range(1, hops + 1)], stops_at=None, break_reason=None,
        changed_lines_covered=[L(path="core.py", line=n) for n in covered],
        tests=lib("E2EListing")(e2e=[], mocks_hop_2=[]))


class TestOrderPaths:
    def test_coverage_then_hops_then_kind_then_location(self):
        paths = [
            traced_path("few-cover", covered=[10], hops=1),
            traced_path("long", covered=[10, 11], hops=6),
            traced_path("short", covered=[10, 11], hops=4, kind="seed"),
            traced_path("short-click", covered=[10, 11], hops=4, kind="click", path="z.py", line=5),
            traced_path("short-click-earlier-line", covered=[10, 11], hops=4, kind="click", path="z.py", line=1),
            traced_path("short-click-earlier-path", covered=[10, 11], hops=4, kind="click", path="a.py", line=30),
        ]
        want = ["short-click-earlier-path", "short-click-earlier-line", "short-click", "short", "long",
                "few-cover"]
        for seed in range(5):
            shuffled = list(paths)
            random.Random(seed).shuffle(shuffled)
            assert [p.entry.name for p in lib("order_paths")(shuffled)] == want


def drop(stage, reason, path, line, kind="argparse"):
    L, E = lib("Location"), lib("PathEntry")
    ent = E(kind=kind, name=f"{stage}:{path}:{line}:{kind}", location=L(path=path, line=line), handler=None,
            invocation=None, invoked_by="the caller (seed)", also_invoked_by=[])
    return lib("PathDrop")(stage=stage, reason=reason, detail="d", entry=ent)


def test_sort_drops_by_stage_order_then_location_then_kind():
    want = [
        drop("model_fallback_citation", "missing_citation", "z.py", 1, "model"),
        drop("model_fallback_duplicate", "duplicate", "a.py", 1, "model"),
        drop("import_reachability", "unreachable", "a.py", 2),
        drop("import_reachability", "unreachable", "a.py", 10),
        drop("import_reachability", "unreachable", "b.py", 1, "click"),
        drop("import_reachability", "unreachable", "b.py", 1, "hook"),
        drop("trace_limit", "over_trace_limit", "a.py", 1),
        drop("trace", "call_failed", "a.py", 1),
        drop("no_changed_line", "no_changed_line_covered", "a.py", 1),
        drop("cap", "over_cap", "a.py", 1),
    ]
    got = lib("sort_drops")(list(reversed(want)))
    assert [d.entry.name for d in got] == [d.entry.name for d in want]


# --- map_paths, scoped ---------------------------------------------------------------------------


def test_reachable_entries_are_traced_and_others_dropped_without_a_call(snap):
    call = FakeCall(trace=responder(snap))
    result = run_map(snap, call, changed_lines={CORE: {run_core_line(snap)}})
    assert traced(call) == ["opal", "ruby"]
    assert [p.entry.name for p in result.paths] == ["opal"]
    assert result.paths[0].changed_lines_covered == [lib("Location")(path=CORE, line=run_core_line(snap))]
    assert drops(result) == {
        "ruby": ("no_changed_line", "no_changed_line_covered"),
        "jade": ("import_reachability", "unreachable"),
        "onyx": ("import_reachability", "unreachable"),
        "topaz": ("import_reachability", "unreachable"),
    }
    assert len(result.paths) + len(result.dropped) == len(lib("discover_entries")(snap.root).entries)


@pytest.mark.parametrize("changed", [{}, {"pkg/deleted.py": [4]}])
def test_empty_or_missing_scope_drops_every_entry_as_unreachable(snap, changed):
    call = FakeCall(trace=responder(snap))
    result = run_map(snap, call, changed_lines=changed)
    assert call.calls == []
    assert result.paths == []
    assert drops(result) == {n: ("import_reachability", "unreachable") for n in ALL}


def test_entry_whose_definition_file_changed_is_traced_first(snap):
    call = FakeCall(trace=responder(snap), names=[*ALL, "amber"])
    seed = lib("Seed")(path=CORE, symbol="run_core", name="amber")
    result = run_map(snap, call, seeds=[seed], changed_lines={CORE: {run_core_line(snap)}}, trace_limit=1)
    assert traced(call) == ["amber"]
    assert [p.entry.name for p in result.paths] == ["amber"]
    got = drops(result)
    assert got["opal"] == got["ruby"] == ("trace_limit", "over_trace_limit")
    # An unreachable entry has its one drop record and does not use a trace_limit slot.
    assert got["jade"] == ("import_reachability", "unreachable")
    assert sum(d.entry.name == "jade" for d in result.dropped) == 1


def test_trace_limit_keeps_the_first_in_location_order_on_a_distance_tie(snap):
    call = FakeCall(trace=responder(snap))
    lines = {run_core_line(snap), other_core_line(snap)}
    result = run_map(snap, call, changed_lines={CORE: lines}, trace_limit=1)
    assert traced(call) == ["opal"]
    assert drops(result)["ruby"] == ("trace_limit", "over_trace_limit")


def test_more_coverage_orders_first_and_cap_drops_the_rest(snap):
    other = other_core_line(snap)
    changed = {CORE: {run_core_line(snap), other, other + 1}}
    call = FakeCall(trace=responder(snap, ruby=ruby_hops(snap, core_end=other + 1)))
    full = run_map(snap, call, changed_lines=changed)
    assert [(p.id, p.entry.name, len(p.changed_lines_covered)) for p in full.paths] == [("p1", "ruby", 2),
                                                                                        ("p2", "opal", 1)]
    capped = run_map(snap, FakeCall(trace=responder(snap, ruby=ruby_hops(snap, core_end=other + 1))),
                     changed_lines=changed, cap=1)
    assert [(p.id, p.entry.name) for p in capped.paths] == [("p1", "ruby")]
    assert drops(capped)["opal"] == ("cap", "over_cap")
    assert not any(d.stage == "cap" for d in full.dropped)


def test_hops_cut_by_the_gate_do_not_count(snap):
    # The third hop's quote is wrong in both answers: the path keeps hops 1-2, which miss the change.
    bad = ruby_hops(snap, core_quote="def nothing_like_it():")
    call = FakeCall(trace=responder(snap, ruby=bad), reask=responder(snap, ruby=bad))
    result = run_map(snap, call, changed_lines={CORE: {other_core_line(snap)}})
    assert drops(result)["ruby"] == ("no_changed_line", "no_changed_line_covered")
    assert len(call.of("ruby")) == 2


def test_drop_records_are_sorted_by_stage_then_location(snap):
    call = FakeCall(trace=responder(snap))
    result = run_map(snap, call, changed_lines={CORE: {run_core_line(snap)}})
    assert [(d.stage, d.entry.location.path) for d in result.dropped] == [
        ("import_reachability", "pkg/tools.py"), ("import_reachability", "pkg/x.py"),
        ("import_reachability", "pkg/y.py"), ("no_changed_line", CLI)]


def test_fallback_entries_go_through_the_same_scope_filter_and_accounting(snap):
    line = snap.line(TOOLS, "def hidden_job(")
    listed = [
        {"kind": "route", "name": "nightly job", "reason": "r",
         "citation": {"path": TOOLS, "line_start": line, "line_end": line, "quote": "def hidden_job():"}},
        {"kind": "route", "name": "uncited", "reason": "r", "citation": None},
    ]
    call = FakeCall(trace=responder(snap), fallback=lambda _: {"entries": listed})
    result = run_map(snap, call, fallback=True, changed_lines={CORE: {run_core_line(snap)}})
    got = drops(result)
    assert got["nightly job"] == ("import_reachability", "unreachable")
    assert got["uncited"] == ("model_fallback_citation", "missing_citation")
    assert traced(call) == ["opal", "ruby"]
    discovered = len(lib("discover_entries")(snap.root).entries)
    assert len(result.paths) + len(result.dropped) == discovered + len(listed)


def test_multi_repository_paths_stay_relative_to_the_snapshot_root(tmp_path):
    mod = "".join(f"# line {n}\n" for n in range(1, 7)) + "def work():\n    return 1\n"
    cli = CLI_SRC.replace("from pkg.service import serve, serve_other", "import mod").replace(
        "return serve()", "return mod.work()").replace("return serve_other()", "return 0")
    snap = commit_snapshot(tmp_path / "snap", {
        "repo_a/pyproject.toml": "[project]\nname = 'a'\n", "repo_a/tools.py": TOOLS_SRC,
        "repo_b/pyproject.toml": "[project]\nname = 'b'\n", "repo_b/src/mod.py": mod, "repo_b/cli.py": cli,
    })
    hops = {"opal": [snap.hop("repo_b/cli.py", "cmd_opal"), snap.hop("repo_b/src/mod.py", "work")],
            "ruby": [snap.hop("repo_b/cli.py", "cmd_ruby")], "jade": [snap.hop("repo_a/tools.py", "cmd_jade")]}
    call = FakeCall(trace=lambda name: {"hops": hops[name]})
    result = run_map(snap, call, changed_lines={"repo_b/src/mod.py": {7}})
    assert [p.entry.name for p in result.paths] == ["opal"]
    assert result.paths[0].changed_lines_covered == [lib("Location")(path="repo_b/src/mod.py", line=7)]
    assert drops(result)["jade"] == ("import_reachability", "unreachable")
    assert "jade" not in traced(call)


def test_equivalent_spelling_and_completion_order_give_identical_bytes(snap, tmp_path):
    def written(changed, concurrency, name):
        rng = random.Random(concurrency)
        call = FakeCall(trace=responder(snap), delay=lambda: rng.random() / 200)
        out = tmp_path / name
        lib("write_paths_file")(run_map(snap, call, changed_lines=changed, concurrency=concurrency), out)
        return out.read_bytes()

    lines = {run_core_line(snap), other_core_line(snap)}
    first = written({CORE: lines}, 1, "a.json")
    assert first == written({f"./{CORE}": lines}, 8, "b.json")
    doc = json.loads(first)
    assert [p["entry"]["name"] for p in doc["paths"]] == ["opal", "ruby"]
    assert [d["stage"] for d in doc["dropped"]] == ["import_reachability"] * 3


# --- map_paths, unscoped -------------------------------------------------------------------------


def test_unscoped_run_traces_and_keeps_everything(snap):
    call = FakeCall(trace=responder(snap))
    result = run_map(snap, call, cap=1, trace_limit=1)
    assert traced(call) == sorted(ALL)
    assert [p.id for p in result.paths] == ["p1", "p2", "p3", "p4", "p5"]
    assert all(p.changed_lines_covered == [] for p in result.paths)
    assert result.dropped == []
