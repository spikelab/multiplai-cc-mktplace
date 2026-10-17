"""A two-commit git repo the citation checks re-read from. No test calls a model."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BASE_APP = "def greet(name):\n    return 'hi ' + name\n"
HEAD_APP = "import logging\n\n\ndef greet(name):\n    logging.info('greeting')\n    return 'hello ' + name\n"


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(repo path, base sha, head sha): app/main.py changes between the commits."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "app" / "main.py").write_text(BASE_APP)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app" / "main.py").write_text(HEAD_APP)
    (root / "app" / "util.py").write_text("X = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "head")
    head = _git(root, "rev-parse", "HEAD")
    return str(root), base, head


@pytest.fixture
def resolve(repo):
    """Repo-relative paths; the side picks the commit."""
    path, base, head = repo

    def _resolve(p: str, side: str):
        if p.startswith("elsewhere/"):
            return None
        return path, (base if side == "base" else head), p

    return _resolve


# --- paths.v1 fixtures ----------------------------------------------------------------

import copy  # noqa: E402
import importlib  # noqa: E402


def paths_name(name: str):
    """A paths.v1 name from wherever block 1 put it: the package, `paths` or `models`.

    Looked up at call time so a missing implementation fails the test instead of
    breaking collection.
    """
    for mod in ("code_facts", "code_facts.paths", "code_facts.models"):
        try:
            module = importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"code_facts does not define {name!r}")


def _loc(path: str, line: int) -> dict:
    return {"path": path, "line": line}


def _entry(kind: str, name: str, path: str, line: int, *, handler=None, invocation=None, invokers=()) -> dict:
    return {"kind": kind, "name": name, "location": _loc(path, line), "handler": handler,
            "invocation": invocation, "also_invoked_by": list(invokers)}


def _hop(symbol: str, path: str, start: int, end: int, quote: str) -> dict:
    return {"symbol_called": symbol, "path": path, "line_start": start, "line_end": end, "quote": quote,
            "note": "", "side": "head"}


def _full_paths_doc() -> dict:
    """A scoped paths.v1 document using every enum value and both arms of every nullable field."""
    open_entry = _entry(
        "argparse", "open", "pkg/__main__.py", 41,
        handler={"path": "pkg/commands.py", "line": 180, "symbol": "cmd_open"},
        invokers=[{"kind": "hook", "name": "PreToolUse:Bash", "location": _loc("hooks/hooks.json", 7)}])
    skill_entry = _entry("skill_command", "café: pkg run", "skills/café/SKILL.md", 12,
                         invocation="python -m pkg run")
    p1 = {
        "id": "p1", "entry": open_entry,
        "hops": [_hop("main", "pkg/__main__.py", 40, 42, "sub.add_parser('open')"),
                 _hop("cmd_open", "pkg/commands.py", 180, 181, "def cmd_open(args):")],
        "stops_at": 2, "break_reason": "text_mismatch",
        "changed_lines_covered": [_loc("pkg/commands.py", 181)],
        "tests": {
            "e2e": [
                {"node_id": "tests/test_cli.py::test_a", "file": "tests/test_cli.py", "line": 3,
                 "match": "direct_call", "invocation_line": 4},
                {"node_id": "tests/test_cli.py::test_b", "file": "tests/test_cli.py", "line": 8,
                 "match": "cli_argv", "invocation_line": 9},
                {"node_id": "tests/test_cli.py::TestX::test_c", "file": "tests/test_cli.py", "line": 14,
                 "match": "cli_runner", "invocation_line": 15},
            ],
            "mocks_hop_2": [
                {"node_id": "tests/test_run.py::test_d", "file": "tests/test_run.py", "line": 10,
                 "match": "subprocess", "invocation_line": 12, "patch_file": "tests/conftest.py",
                 "patch_line": 5},
                {"node_id": "tests/test_run.py::test_e", "file": "tests/test_run.py", "line": 20,
                 "match": "runpy", "invocation_line": 21, "patch_file": "tests/test_run.py",
                 "patch_line": 19},
            ],
        },
    }
    p2 = {
        "id": "p2", "entry": skill_entry,
        "hops": [_hop("pkg", "skills/café/SKILL.md", 12, 12, "python -m pkg run")],
        "stops_at": None, "break_reason": None,
        "changed_lines_covered": [], "tests": {"e2e": [], "mocks_hop_2": []},
    }
    drops = [
        ("model_fallback_citation", "citation_unverified", _entry("model", "GET /items", "app/routes.py", 12)),
        ("model_fallback_duplicate", "duplicate", _entry("model", "dup", "pkg/commands.py", 180)),
        ("import_reachability", "unreachable", _entry("click", "cli sync", "cli/app.py", 5,
                                                      handler={"path": "cli/app.py", "line": 6,
                                                               "symbol": None})),
        ("trace_limit", "over_trace_limit", _entry("typer", "users add-user", "cli/users.py", 9)),
        ("trace", "call_failed", _entry("main", "tools.report", "tools/report.py", 30)),
        ("no_changed_line", "no_changed_line_covered", _entry("script", "mytool", "pyproject.toml", 8)),
        ("cap", "over_cap", _entry("seed", "lib/api.py:handle_request", "lib/api.py", 30)),
    ]
    return {
        "schema_version": "paths.v1",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "scoped": True,
        "cap": 10,
        "fallback": {"status": "failed", "reason": "1 validation error for FallbackAnswer"},
        "paths": [p1, p2],
        "dropped": [{"stage": s, "reason": r, "detail": "detail text", "entry": e} for s, r, e in drops],
        "diagnostics": [
            {"path": "broken.py", "line": None, "reason": "parse_error", "detail": "invalid syntax"},
            {"path": "cli.py", "line": 3, "reason": "dynamic_name", "detail": "add_parser(name)"},
            {"path": "cli.py", "line": 9, "reason": "unresolved_handler", "detail": "x"},
            {"path": "lib/missing.py", "line": None, "reason": "seed_not_found", "detail": "no such file"},
            {"path": "pyproject.toml", "line": 8, "reason": "unresolved_target", "detail": "ext"},
        ],
        "test_listing_skips": [
            {"file": "tests/test_bad_utf8.py", "reason": "decode_error"},
            {"file": "tests/test_broken.py", "reason": "syntax_error"},
        ],
    }


def unscoped_variant(doc: dict) -> dict:
    """The same document as an unscoped run: cap null, only fallback and trace drops, no coverage."""
    doc = copy.deepcopy(doc)
    doc["scoped"] = False
    doc["cap"] = None
    doc["fallback"] = {"status": "skipped", "reason": None}
    doc["dropped"] = [d for d in doc["dropped"] if d["stage"] in ("model_fallback_citation", "trace")]
    for p in doc["paths"]:
        p["changed_lines_covered"] = []
    return doc


@pytest.fixture
def full_paths_doc() -> dict:
    return _full_paths_doc()


@pytest.fixture
def empty_snapshot(tmp_path) -> Path:
    """A snapshot with Python code but no entry point of any kind."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "README.md").write_text("# nothing to run\n")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text("def run(x):\n    return x + 1\n")
    return root
