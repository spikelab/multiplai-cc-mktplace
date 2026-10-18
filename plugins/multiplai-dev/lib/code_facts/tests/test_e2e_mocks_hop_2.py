"""Tests that patch hop 2 move to `mocks_hop_2`; tests that patch out the handler are not listed.

Every snapshot is written into `tmp_path`; nothing in it is imported or run. The
paths are built by hand (hop 1 at the handler, hop 2 at `pkg.core.run`) except in
the `map_paths` tests at the end, where the trace gate decides the retained hops.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path
from typing import Any

import pytest
from conftest import line_of, make_tree
from trace_fixtures import CLI as TRACE_CLI
from trace_fixtures import FakeCall, chain_hops, cli_snapshot, commit_snapshot, run_map


def lib(name: str) -> Any:
    """A code_facts name from whichever module defines it, looked up at call time."""
    for mod in ("code_facts", "code_facts.e2e_tests", "code_facts.paths", "code_facts.entries",
                "code_facts.pyindex"):
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

CORE = src('''
    def run(args):
        return 0


    def log(message):
        return None
    ''')

# Hop 1's module: `run` is bound at module level, and as `go` inside handle_render only.
CLI = src('''
    from pkg.core import log, run


    def handle_open(args):
        log("open")
        return run(args)


    def handle_render(args):
        from pkg.core import run as go
        return go(args)
    ''')

# The known-edge `from` lines of both subcommands are in main().
MAIN = src('''
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
    ''')

BASE = {"pyproject.toml": '[project]\nname = "x"\nversion = "0"\n', "pkg/__init__.py": "",
        "pkg/core.py": CORE, "pkg/cli.py": CLI, "pkg/__main__.py": MAIN}

IMPORTS = src('''
    import unittest.mock
    from unittest import mock

    import pytest

    import pkg.core
    from pkg import core
    from pkg.__main__ import main
    ''') + "\n\n"


def snapshot(tmp_path: Path, files: dict[str, str]) -> Path:
    return make_tree(tmp_path / "snap", {**BASE, **files})


# --- listing -------------------------------------------------------------------------------


def def_hop(path: str, text: str, symbol: str, lines: int = 1) -> Any:
    """A hop at `def <symbol>(` of `path`, spanning `lines` lines."""
    start = line_of(text, f"def {symbol}(")
    return lib("PathHop")(symbol_called=symbol, path=path, line_start=start, line_end=start + lines - 1,
                          quote=f"def {symbol}(")


# Retained hops per subcommand: the handler (its whole body), then pkg.core.run.
HOPS = {
    "open": [def_hop("pkg/cli.py", CLI, "handle_open", 3), def_hop("pkg/core.py", CORE, "run")],
    "render": [def_hop("pkg/cli.py", CLI, "handle_render", 3), def_hop("pkg/core.py", CORE, "run")],
}


def traced(pid: str, entry: Any, hops: list[Any], *, stops_at: int | None = None,
           break_reason: str | None = None) -> Any:
    return lib("TracedPath")(id=pid, entry=lib("to_path_entry")(entry), hops=hops, stops_at=stops_at,
                             break_reason=break_reason, changed_lines_covered=[],
                             tests=lib("E2EListing")(e2e=[], mocks_hop_2=[]))


def entries_named(root: Path, *names: str) -> dict[str, Any]:
    found = {e.name: e for e in lib("discover_entries")(root).entries}
    missing = [n for n in names if n not in found]
    assert not missing, f"not discovered: {missing}; have {sorted(found)}"
    return {n: found[n] for n in names}


def listing(root: Path, hops: dict[str, list[Any]] = HOPS, **cut: Any) -> dict[str, Any]:
    """E2EListing per subcommand name, from `list_e2e_tests` over one path per name in `hops`."""
    entries = entries_named(root, *hops)
    by_id = {f"p{n}": entries[name] for n, name in enumerate(hops, start=1)}
    paths = [traced(pid, e, hops[e.name], **cut) for pid, e in by_id.items()]
    out, _ = lib("list_e2e_tests")(root, paths, by_id, index=lib("PyIndex").build(root))
    return {e.name: out[pid] for pid, e in by_id.items()}


def e2e_ids(listed: Any) -> list[str]:
    return [t.node_id for t in listed.e2e]


def mocked(listed: Any) -> list[tuple[str, str, int]]:
    return [(t.node_id, t.patch_file, t.patch_line) for t in listed.mocks_hop_2]


# --- patch forms ---------------------------------------------------------------------------


MONKEY = 'def test_x(monkeypatch):\n    monkeypatch.setattr({})\n    main(["open"])\n'

FORMS = {
    "mock-decorator": ('@mock.patch("pkg.core.run")\ndef test_x(fake):\n    main(["open"])\n',
                       '@mock.patch("pkg.core.run")'),
    "unittest-mock-decorator": ('@unittest.mock.patch("pkg.core.run")\ndef test_x(fake):\n    main(["open"])\n',
                                '@unittest.mock.patch'),
    "with": ('def test_x():\n    with mock.patch("pkg.core.run"):\n        main(["open"])\n', "with mock.patch"),
    "start": ('def test_x():\n    mock.patch("pkg.core.run").start()\n    main(["open"])\n', ".start()"),
    "patch-object": ('def test_x():\n    with mock.patch.object(core, "run"):\n        main(["open"])\n',
                     "mock.patch.object"),
    "monkeypatch-dotted": (MONKEY.format('"pkg.core.run", lambda a: 0'), "monkeypatch.setattr"),
    "monkeypatch-object": (MONKEY.format('core, "run", lambda a: 0'), "monkeypatch.setattr"),
    "monkeypatch-imported-module": (MONKEY.format('pkg.core, "run", lambda a: 0'), "monkeypatch.setattr"),
}


@pytest.mark.parametrize("form", FORMS)
def test_each_recognised_patch_form_of_hop_2_moves_the_test(tmp_path, form):
    test, patch_needle = FORMS[form]
    body = IMPORTS + test
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert out["open"].e2e == []
    (record,) = out["open"].mocks_hop_2
    assert (record.node_id, record.file, record.line, record.match, record.invocation_line, record.patch_file,
            record.patch_line) == ("tests/test_p.py::test_x", "tests/test_p.py", line_of(body, "def test_x"),
                                   "cli_argv", line_of(body, 'main(["open"])'), "tests/test_p.py",
                                   line_of(body, patch_needle))


def test_patch_imported_from_unittest_mock_by_name_is_recognised(tmp_path):
    body = 'from unittest.mock import patch\n\nfrom pkg.__main__ import main\n\n\n' \
           '@patch("pkg.core.run")\ndef test_x(fake):\n    main(["open"])\n'
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert mocked(out["open"]) == [("tests/test_p.py::test_x", "tests/test_p.py", line_of(body, "@patch"))]


def test_record_fields_for_a_decorated_test(tmp_path):
    # Decorator on line 9, `def` on line 10, the call on line 12.
    body = src('''
        from unittest import mock

        from pkg.__main__ import main


        # Padding so the decorator lands on line 9,
        # the test on line 10 and the call on line 12.

        @mock.patch("pkg.core.run")
        def test_open(fake_run):
            fake_run.return_value = 0
            main(["open"])
        ''')
    assert line_of(body, "@mock.patch") == 9 and line_of(body, 'main(["open"])') == 12
    out = listing(snapshot(tmp_path, {"tests/test_open.py": body}))
    (record,) = out["open"].mocks_hop_2
    assert record.model_dump() == {"node_id": "tests/test_open.py::test_open", "file": "tests/test_open.py",
                                   "line": 10, "match": "cli_argv", "invocation_line": 12,
                                   "patch_file": "tests/test_open.py", "patch_line": 9}
    assert out["open"].e2e == []


# --- which target names hop 2 --------------------------------------------------------------


def test_patch_at_the_importing_module_moves_the_test(tmp_path):
    # pkg/cli.py (hop 1) does `from pkg.core import run`.
    body = IMPORTS + 'def test_x():\n    with mock.patch("pkg.cli.run"):\n        main(["open"])\n'
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert mocked(out["open"]) == [("tests/test_p.py::test_x", "tests/test_p.py", line_of(body, "mock.patch"))]
    assert out["open"].e2e == []


def test_a_function_local_import_in_hop_1_binds_the_name_too(tmp_path):
    # handle_render (hop 1) does `from pkg.core import run as go` inside its body.
    body = IMPORTS + 'def test_x():\n    with mock.patch("pkg.cli.go"):\n        main(["render"])\n'
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert mocked(out["render"]) == [("tests/test_p.py::test_x", "tests/test_p.py", line_of(body, "mock.patch"))]


def test_patching_another_symbol_leaves_the_test_in_e2e(tmp_path):
    body = IMPORTS + src('''
        @mock.patch("pkg.core.log")
        def test_log(fake):
            main(["open"])


        def test_attr(monkeypatch):
            monkeypatch.setattr(core, "log", lambda m: None)
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert e2e_ids(out["open"]) == ["tests/test_p.py::test_log", "tests/test_p.py::test_attr"]
    assert out["open"].mocks_hop_2 == []


def test_patch_line_is_the_hop_2_patch_when_other_symbols_are_patched_too(tmp_path):
    body = IMPORTS + src('''
        @mock.patch("pkg.core.log")
        @mock.patch("pkg.core.run")
        def test_x(fake_run, fake_log):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}))
    assert mocked(out["open"]) == [("tests/test_p.py::test_x", "tests/test_p.py",
                                    line_of(body, '@mock.patch("pkg.core.run")'))]


def test_a_single_hop_path_never_has_mocked_tests(tmp_path):
    body = IMPORTS + '@mock.patch("pkg.core.run")\ndef test_x(fake):\n    main(["open"])\n'
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}), {"open": HOPS["open"][:1]})
    assert e2e_ids(out["open"]) == ["tests/test_p.py::test_x"]
    assert out["open"].mocks_hop_2 == []


def test_the_same_test_is_mocked_on_one_path_and_e2e_on_another(tmp_path):
    # Hop 2 of a path through log() is pkg.core.log; patching run mocks only the run path.
    body = IMPORTS + '@mock.patch("pkg.core.run")\ndef test_x(fake):\n    main(["open"])\n    main(["render"])\n'
    hops = {"open": HOPS["open"], "render": [HOPS["render"][0], def_hop("pkg/core.py", CORE, "log")]}
    out = listing(snapshot(tmp_path, {"tests/test_p.py": body}), hops)
    assert [t.node_id for t in out["open"].mocks_hop_2] == ["tests/test_p.py::test_x"]
    assert e2e_ids(out["render"]) == ["tests/test_p.py::test_x"] and out["render"].mocks_hop_2 == []
    for listed in out.values():
        assert not set(e2e_ids(listed)) & {t.node_id for t in listed.mocks_hop_2}


# --- patches applied by fixtures -----------------------------------------------------------


CONFTEST = src('''
    import pytest


    @pytest.fixture
    def fake_run(monkeypatch):
        monkeypatch.setattr("pkg.core.run", lambda args: 0)


    @pytest.fixture
    def app_env(fake_run):
        return {"env": "test"}
    ''')


def test_requested_conftest_fixture_moves_the_test_and_unrequested_leaves_it(tmp_path):
    body = src('''
        from pkg.__main__ import main


        def test_requested(fake_run):
            main(["open"])


        def test_unrequested():
            main(["open"])


        def test_through_another_fixture(app_env):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_f.py": body}))
    patch_line = line_of(CONFTEST, "monkeypatch.setattr")
    assert mocked(out["open"]) == [("tests/test_f.py::test_requested", "tests/conftest.py", patch_line),
                                   ("tests/test_f.py::test_through_another_fixture", "tests/conftest.py",
                                    patch_line)]
    assert e2e_ids(out["open"]) == ["tests/test_f.py::test_unrequested"]


def test_requested_fixture_in_the_test_module_moves_the_test(tmp_path):
    body = IMPORTS + src('''
        @pytest.fixture
        def quiet():
            with mock.patch("pkg.core.run"):
                yield


        def test_x(quiet):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/test_f.py": body}))
    assert mocked(out["open"]) == [("tests/test_f.py::test_x", "tests/test_f.py", line_of(body, "mock.patch"))]


def test_autouse_fixture_applies_to_its_module_only(tmp_path):
    patched = IMPORTS + src('''
        @pytest.fixture(autouse=True)
        def no_run(monkeypatch):
            monkeypatch.setattr("pkg.core.run", lambda args: 0)


        def test_a():
            main(["open"])
        ''')
    plain = "from pkg.__main__ import main\n\n\ndef test_b():\n    main([\"open\"])\n"
    out = listing(snapshot(tmp_path, {"tests/test_a.py": patched, "tests/test_b.py": plain}))
    assert mocked(out["open"]) == [("tests/test_a.py::test_a", "tests/test_a.py",
                                    line_of(patched, "monkeypatch.setattr"))]
    assert e2e_ids(out["open"]) == ["tests/test_b.py::test_b"]


def test_autouse_conftest_applies_below_its_directory_and_not_beside_it(tmp_path):
    conftest = src('''
        import pytest


        @pytest.fixture(autouse=True)
        def no_run(monkeypatch):
            monkeypatch.setattr("pkg.core.run", lambda args: 0)
        ''')
    plain = "from pkg.__main__ import main\n\n\ndef test_x():\n    main([\"open\"])\n"
    out = listing(snapshot(tmp_path, {"tests/unit/conftest.py": conftest, "tests/unit/deep/test_u.py": plain,
                                      "tests/other/test_o.py": plain}))
    assert mocked(out["open"]) == [("tests/unit/deep/test_u.py::test_x", "tests/unit/conftest.py",
                                    line_of(conftest, "monkeypatch.setattr"))]
    assert e2e_ids(out["open"]) == ["tests/other/test_o.py::test_x"]


def test_a_conftest_at_the_snapshot_root_counts(tmp_path):
    conftest = src('''
        import pytest


        @pytest.fixture
        def fake_run(monkeypatch):
            monkeypatch.setattr("pkg.core.run", lambda args: 0)
        ''')
    body = "from pkg.__main__ import main\n\n\ndef test_x(fake_run):\n    main([\"open\"])\n"
    out = listing(snapshot(tmp_path, {"conftest.py": conftest, "tests/test_r.py": body}))
    assert mocked(out["open"]) == [("tests/test_r.py::test_x", "conftest.py", line_of(conftest, "setattr"))]


AUTOUSE_CONFTEST = src('''
    import pytest


    @pytest.fixture(autouse=True)
    def no_run(monkeypatch):
        monkeypatch.setattr("{target}", lambda args: 0)
    ''')

PLAIN_NO_RUN = src('''
    import pytest

    from pkg.__main__ import main


    @pytest.fixture
    def no_run():
        return None


    def test_x():
        main(["open"])
    ''')


@pytest.mark.parametrize("target", ["pkg.core.run", "pkg.cli.handle_open"])
def test_a_plain_module_fixture_hides_an_autouse_conftest_fixture_of_the_same_name(tmp_path, target):
    # Only the module's no_run runs, so the conftest patch neither moves nor drops test_x. test_y,
    # beside it, still gets the conftest patch: moved for hop 2, not listed for the handler.
    plain = "from pkg.__main__ import main\n\n\ndef test_y():\n    main([\"open\"])\n"
    conftest = AUTOUSE_CONFTEST.format(target=target)
    out = listing(snapshot(tmp_path, {"tests/conftest.py": conftest, "tests/test_hidden.py": PLAIN_NO_RUN,
                                      "tests/test_seen.py": plain}))
    assert e2e_ids(out["open"]) == ["tests/test_hidden.py::test_x"]
    seen = [("tests/test_seen.py::test_y", "tests/conftest.py", line_of(conftest, "monkeypatch.setattr"))]
    assert mocked(out["open"]) == (seen if target == "pkg.core.run" else [])


def test_a_module_fixture_hides_a_conftest_fixture_of_the_same_name(tmp_path):
    hiding = src('''
        import pytest

        from pkg.__main__ import main


        @pytest.fixture
        def fake_run():
            return None


        def test_x(fake_run):
            main(["open"])
        ''')
    seen = "from pkg.__main__ import main\n\n\ndef test_y(fake_run):\n    main([\"open\"])\n"
    out = listing(snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_hidden.py": hiding,
                                      "tests/test_seen.py": seen}))
    assert e2e_ids(out["open"]) == ["tests/test_hidden.py::test_x"]
    assert mocked(out["open"]) == [("tests/test_seen.py::test_y", "tests/conftest.py",
                                    line_of(CONFTEST, "monkeypatch.setattr"))]


def test_a_fixture_requesting_its_own_name_gets_the_one_it_hides(tmp_path):
    body = src('''
        import pytest

        from pkg.__main__ import main


        @pytest.fixture
        def fake_run(fake_run):
            return fake_run


        def test_x(fake_run):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_s.py": body}))
    assert mocked(out["open"]) == [("tests/test_s.py::test_x", "tests/conftest.py",
                                    line_of(CONFTEST, "monkeypatch.setattr"))]
    assert out["open"].e2e == []


def test_a_fixture_is_requested_by_its_name_argument_not_its_function_name(tmp_path):
    body = IMPORTS + src('''
        @pytest.fixture(name="quiet")
        def _quiet_fixture():
            with mock.patch("pkg.core.run"):
                yield


        def test_by_name(quiet):
            main(["open"])


        def test_by_function_name(_quiet_fixture):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/test_n.py": body}))
    assert mocked(out["open"]) == [("tests/test_n.py::test_by_name", "tests/test_n.py",
                                    line_of(body, "mock.patch"))]
    assert e2e_ids(out["open"]) == ["tests/test_n.py::test_by_function_name"]


def test_a_mock_decorator_given_a_replacement_fills_no_parameter(tmp_path):
    # fake_run is the conftest fixture unless a decorator passes a mock into that parameter.
    body = IMPORTS + src('''
        @mock.patch("pkg.core.log", lambda m: None)
        def test_positional_new(fake_run):
            main(["open"])


        @mock.patch("pkg.core.log", new=lambda m: None)
        def test_keyword_new(fake_run):
            main(["open"])


        @mock.patch.object(core, "log", lambda m: None)
        def test_object_new(fake_run):
            main(["open"])


        @mock.patch("pkg.core.log")
        def test_mock_passed(fake_run):
            main(["open"])


        @mock.patch.object(core, "log")
        def test_object_mock_passed(fake_run):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_d.py": body}))
    patch_line = line_of(CONFTEST, "monkeypatch.setattr")
    assert mocked(out["open"]) == [(f"tests/test_d.py::{name}", "tests/conftest.py", patch_line)
                                   for name in ("test_positional_new", "test_keyword_new", "test_object_new")]
    assert e2e_ids(out["open"]) == ["tests/test_d.py::test_mock_passed", "tests/test_d.py::test_object_mock_passed"]


def test_test_class_methods_skip_self_and_apply_class_decorators(tmp_path):
    # conftest's fake_log patches hop 2, so a parameter wrongly read as a request would move the test.
    conftest = src('''
        import pytest


        @pytest.fixture
        def fake_log(monkeypatch):
            monkeypatch.setattr("pkg.core.run", lambda args: 0)
        ''')
    body = IMPORTS + src('''
        @mock.patch("pkg.core.log")
        class TestLogged:
            def test_m(self, fake_log):
                main(["open"])


        @mock.patch("pkg.core.run")
        class TestRunPatched:
            def test_n(self, fake):
                main(["open"])


        class TestPlain:
            def test_o(self, fake_log):
                main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/conftest.py": conftest, "tests/test_c.py": body}))
    assert e2e_ids(out["open"]) == ["tests/test_c.py::TestLogged::test_m"]
    assert mocked(out["open"]) == [
        ("tests/test_c.py::TestRunPatched::test_n", "tests/test_c.py", line_of(body, '@mock.patch("pkg.core.run")')),
        ("tests/test_c.py::TestPlain::test_o", "tests/conftest.py", line_of(conftest, "monkeypatch.setattr"))]


# --- handler-patch exclusion (D15) ---------------------------------------------------------


def test_a_test_that_patches_the_handler_is_not_listed_for_that_path(tmp_path):
    body = IMPORTS + src('''
        @mock.patch("pkg.cli.handle_open")
        def test_x(fake):
            main(["open"])
            main(["render"])


        @pytest.fixture
        def no_handler(monkeypatch):
            monkeypatch.setattr("pkg.cli.handle_open", lambda a: 0)


        def test_y(no_handler):
            main(["open"])
        ''')
    out = listing(snapshot(tmp_path, {"tests/test_h.py": body}))
    assert out["open"].e2e == [] and out["open"].mocks_hop_2 == []
    # test_x still runs render's handler; test_y never invokes render.
    assert e2e_ids(out["render"]) == ["tests/test_h.py::test_x"]


def test_a_test_that_patches_the_function_holding_a_known_edge_is_not_listed(tmp_path):
    # main() holds the known-edge `from` line (dispatch call or set_defaults) of both subcommands.
    body = IMPORTS + src('''
        import subprocess
        import sys


        @mock.patch("pkg.__main__.main")
        def test_x(fake):
            subprocess.run([sys.executable, "-m", "pkg", "open"])
            subprocess.run([sys.executable, "-m", "pkg", "render"])
        ''')
    root = snapshot(tmp_path, {"tests/test_h.py": body})
    main_span = range(line_of(MAIN, "def main("), len(MAIN.splitlines()) + 1)
    for entry in entries_named(root, "open", "render").values():
        assert entry.known_edges and all(e.from_.path == "pkg/__main__.py" and e.from_.line in main_span
                                         for e in entry.known_edges)
    out = listing(root)
    assert {name: (listed.e2e, listed.mocks_hop_2) for name, listed in out.items()} == {
        "open": ([], []), "render": ([], [])}


# --- applied_patches -----------------------------------------------------------------------


def test_applied_patches_resolves_targets_and_follows_requested_fixtures(tmp_path):
    body = IMPORTS + src('''
        @mock.patch("pkg.core.log")
        def test_x(fake, fake_run):
            with mock.patch.object(core, "run"):
                main(["open"])


        def test_y(monkeypatch):
            monkeypatch.setattr(core, "log", None)
            mock.patch("pkg.cli.run").start()
        ''')
    root = snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_a.py": body})
    e2e_index = lib("E2EIndex").build(root, lib("PyIndex").build(root))
    applied = lib("applied_patches")
    f = "tests/test_a.py"
    assert sorted(applied(e2e_index, f"{f}::test_x", root=root)) == sorted([
        ("pkg.core.log", f, line_of(body, '@mock.patch("pkg.core.log")')),
        ("pkg.core.run", f, line_of(body, "mock.patch.object")),
        ("pkg.core.run", "tests/conftest.py", line_of(CONFTEST, "monkeypatch.setattr")),
    ])
    assert sorted(applied(e2e_index, f"{f}::test_y", root=root)) == sorted([
        ("pkg.core.log", f, line_of(body, "monkeypatch.setattr")),
        ("pkg.cli.run", f, line_of(body, ".start()")),
    ])


def test_applied_patches_ignores_non_literal_targets_and_unrequested_fixtures(tmp_path):
    body = IMPORTS + src('''
        TARGET = "pkg.core.run"


        def test_x():
            with mock.patch(TARGET):
                main(["open"])
        ''')
    root = snapshot(tmp_path, {"tests/conftest.py": CONFTEST, "tests/test_a.py": body})
    e2e_index = lib("E2EIndex").build(root, lib("PyIndex").build(root))
    assert lib("applied_patches")(e2e_index, "tests/test_a.py::test_x", root=root) == []
    out = listing(root)
    assert e2e_ids(out["open"]) == ["tests/test_a.py::test_x"] and out["open"].mocks_hop_2 == []


# --- through map_paths: hop 2 comes from the retained hops ---------------------------------


TEST_OPAL = src('''
    from unittest import mock

    from pkg.cli import main


    @mock.patch("pkg.cli.step1_opal")
    def test_opal(fake):
        main(["opal"])
    ''')


def test_map_paths_moves_a_test_patching_the_traced_hop_2(tmp_path):
    base = cli_snapshot(tmp_path / "base", ["opal", "ruby"])
    snap = commit_snapshot(tmp_path / "snap", {**base.files, "tests/test_cli.py": TEST_OPAL})
    result = run_map(snap, FakeCall(trace=lambda name: {"hops": chain_hops(snap, name)}))
    opal = next(p for p in result.paths if p.entry.name == "opal")
    assert [h.symbol_called for h in opal.hops[:2]] == ["cmd_opal", "step1_opal"]
    assert opal.tests.e2e == []
    assert [t.model_dump() for t in opal.tests.mocks_hop_2] == [{
        "node_id": "tests/test_cli.py::test_opal", "file": "tests/test_cli.py",
        "line": line_of(TEST_OPAL, "def test_opal"), "match": "cli_argv",
        "invocation_line": line_of(TEST_OPAL, 'main(["opal"])'), "patch_file": "tests/test_cli.py",
        "patch_line": line_of(TEST_OPAL, "@mock.patch")}]


def test_map_paths_keeps_the_test_in_e2e_when_the_path_is_cut_after_hop_1(tmp_path):
    base = cli_snapshot(tmp_path / "base", ["opal", "ruby"])
    snap = commit_snapshot(tmp_path / "snap", {**base.files, "tests/test_cli.py": TEST_OPAL})

    def broken_at_hop_2(name: str) -> dict:
        # The model's hop 2 is step1_<name>, but its quote is not in the file: the gate cuts it.
        return {"hops": [snap.hop(TRACE_CLI, f"cmd_{name}"),
                         snap.hop(TRACE_CLI, f"step1_{name}", quote="not in the file")]}

    result = run_map(snap, FakeCall(trace=broken_at_hop_2, reask=broken_at_hop_2))
    opal = next(p for p in result.paths if p.entry.name == "opal")
    assert (opal.stops_at, len(opal.hops)) == (1, 1)
    assert [t.node_id for t in opal.tests.e2e] == ["tests/test_cli.py::test_opal"]
    assert opal.tests.mocks_hop_2 == []
