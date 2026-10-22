"""discover_entries with caller seeds: seed entries, seed_not_found, and seed-order independence."""

from __future__ import annotations

import importlib

import pytest
from code_facts import Seed
from conftest import line_of, make_tree

PAD = "".join(f"# filler {i}\n" for i in range(1, 30))
API = (
    PAD
    + """\
def handle_request(req):
    def inner(x):
        return x * 2
    return inner(req)


def other():
    return 1
"""
)
# The broken file, the dynamic add_parser and the missing seed are the only skipped candidates.
DYNAMIC = """\
import argparse

NAME = "go"


def build():
    sub = argparse.ArgumentParser().add_subparsers()
    sub.add_parser(NAME)
"""


def discover(root, seeds=()):
    return importlib.import_module("code_facts.entries").discover_entries(root, seeds)


@pytest.fixture
def root(tmp_path):
    return make_tree(tmp_path / "snap", {"lib/__init__.py": "", "lib/api.py": API})


def seeds_of(disc):
    return [e for e in disc.entries if e.kind == "seed"]


class TestSeedEntries:
    def test_seed_by_symbol(self, root):
        disc = discover(root, [Seed(path="lib/api.py", symbol="handle_request")])
        [e] = seeds_of(disc)
        assert line_of(API, "def handle_request") == 30
        assert (e.handler.path, e.handler.line, e.handler.symbol) == (
            "lib/api.py",
            30,
            "handle_request",
        )
        assert e.name == "lib/api.py:handle_request"
        assert (e.location.path, e.location.line) == ("lib/api.py", 30)
        assert e.invocation is None and e.also_invoked_by == []
        assert disc.diagnostics == []

    def test_seed_by_line_takes_the_enclosing_def(self, root):
        [e] = seeds_of(
            discover(
                root, [Seed(path="lib/api.py", line=line_of(API, "return inner(req)"))]
            )
        )
        assert (e.handler.line, e.handler.symbol) == (30, "handle_request")
        assert e.name == "lib/api.py:handle_request"
        assert e.location.path == "lib/api.py"

    def test_seed_line_in_a_nested_def_takes_the_innermost(self, root):
        [e] = seeds_of(
            discover(root, [Seed(path="lib/api.py", line=line_of(API, "return x * 2"))])
        )
        assert (e.handler.line, e.handler.symbol) == (
            line_of(API, "def inner"),
            "inner",
        )

    def test_given_name_is_kept(self, root):
        [e] = seeds_of(
            discover(
                root, [Seed(path="lib/api.py", symbol="other", name="the other one")]
            )
        )
        assert e.name == "the other one"
        assert e.handler.line == line_of(API, "def other")

    def test_the_seed_name_says_who_invokes_it_else_the_caller(self, root):
        # Entries come in location order: handle_request is above other.
        unnamed, named = seeds_of(
            discover(
                root,
                [
                    Seed(path="lib/api.py", symbol="other", name="the nightly job"),
                    Seed(path="lib/api.py", symbol="handle_request"),
                ],
            )
        )
        assert (named.name, named.invoked_by) == ("the nightly job", "the nightly job")
        assert (unnamed.name, unnamed.invoked_by) == (
            "lib/api.py:handle_request",
            "the caller (seed)",
        )


class TestSeedNotFound:
    @pytest.mark.parametrize(
        "seed",
        [
            Seed(path="lib/missing.py", symbol="x"),
            Seed(path="lib/missing.py", line=3),
            Seed(path="lib/api.py", line=9999),
            Seed(path="lib/api.py", symbol="no_such_function"),
        ],
        ids=[
            "missing-file-symbol",
            "missing-file-line",
            "line-out-of-range",
            "unknown-symbol",
        ],
    )
    def test_invalid_seed_gives_one_diagnostic_and_no_entry(self, root, seed):
        disc = discover(root, [seed])
        assert seeds_of(disc) == []
        assert [(d.path, d.reason) for d in disc.diagnostics] == [
            (seed.path, "seed_not_found")
        ]
        assert disc.diagnostics[0].detail

    def test_valid_seeds_survive_beside_invalid_ones(self, root):
        disc = discover(
            root,
            [
                Seed(path="lib/gone.py", symbol="x"),
                Seed(path="lib/api.py", symbol="other"),
            ],
        )
        assert [e.handler.symbol for e in seeds_of(disc)] == ["other"]
        assert [d.reason for d in disc.diagnostics] == ["seed_not_found"]


class TestSeedPathValidation:
    @pytest.mark.parametrize("path", ["/abs.py", "../x.py", "lib/../../x.py"])
    def test_absolute_or_escaping_seed_path_raises_before_discovery(
        self, root, monkeypatch, path
    ):
        entries = importlib.import_module("code_facts.entries")

        def refuse(cls, root):
            raise AssertionError("discovery ran before the seed paths were checked")

        monkeypatch.setattr(entries.PyIndex, "build", classmethod(refuse))
        with pytest.raises(ValueError, match="absolute|escapes"):
            discover(
                root,
                [Seed(path="lib/api.py", symbol="other"), Seed(path=path, symbol="x")],
            )


class TestMixedFailures:
    def test_one_diagnostic_per_skipped_candidate_and_valid_entries_kept(
        self, tmp_path
    ):
        root = make_tree(
            tmp_path / "snap",
            {"lib/api.py": API, "broken.py": "def broken(:\n", "cli.py": DYNAMIC},
        )
        disc = discover(
            root,
            [Seed(path="nope.py", symbol="x"), Seed(path="lib/api.py", symbol="other")],
        )
        assert [(d.path, d.reason) for d in disc.diagnostics] == [
            ("broken.py", "parse_error"),
            ("cli.py", "dynamic_name"),
            ("nope.py", "seed_not_found"),
        ]
        assert disc.diagnostics[0].line is None
        assert disc.diagnostics[1].line == line_of(DYNAMIC, "add_parser(NAME)")
        assert [e.handler.symbol for e in seeds_of(disc)] == ["other"]


class TestSeedOrder:
    def test_seed_order_does_not_change_the_output(self, root):
        a = Seed(path="lib/api.py", symbol="other")
        b = Seed(path="lib/api.py", line=line_of(API, "return inner(req)"), name="b")
        c = Seed(path="lib/absent.py", symbol="x")
        first = discover(root, [a, b, c]).model_dump_json(by_alias=True)
        assert discover(root, [c, b, a]).model_dump_json(by_alias=True) == first
        keys = [
            (e.location.path, e.location.line, e.kind, e.name)
            for e in discover(root, [b, a]).entries
        ]
        assert keys == sorted(keys) and len(keys) == 2
