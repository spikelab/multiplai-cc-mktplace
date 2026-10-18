"""map_paths on a snapshot with no entries: argument checks, the empty document, and the library surface."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.resources
import inspect
import json
import tomllib
from pathlib import Path

import jsonschema
import pytest

import code_facts
from conftest import paths_name

LIB = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[5]

EXISTING_EXPORTS = [
    "CallFn", "Citation", "Dropped", "ExploreOutput", "Fact", "FilePurpose", "Gated", "PATH_RULE", "Resolve",
    "Scope", "Side", "Span", "Term", "UNRESOLVED", "UNTRUSTED_RULES", "Unit", "ask", "bounded",
    "check_citation", "citation_keeper", "citation_rules", "explore_unit", "gate_answers", "partition",
    "reask_units", "run_tasks", "schema_json", "unit_key", "ws_path",
]
NEW_EXPORTS = ["map_paths", "write_paths_file", "PathsResult", "TracedPath", "PathEntry", "PathHop", "PathDrop",
               "E2ETest", "MockedE2ETest", "E2EListing", "Location", "Seed"]


class RecordingCall:
    """A CallFn that records every call. Nothing in an empty snapshot should reach it."""

    def __init__(self):
        self.calls = []

    async def __call__(self, prompt, schema, **kwargs):
        self.calls.append((prompt, schema, kwargs))
        raise AssertionError("no model call is expected on this snapshot")


def _resolve(path, side):
    return None


def _map(root, call=None, **kw):
    call = call if call is not None else RecordingCall()
    kw.setdefault("fallback", False)
    return asyncio.run(paths_name("map_paths")(root=root, commit="c0ffee", resolve=_resolve, call=call, **kw))


def _tree(base: Path) -> dict[str, tuple]:
    out = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        out[rel] = ("dir",) if p.is_dir() else (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _validate_file(path: Path) -> dict:
    schema = json.loads(importlib.resources.files("code_facts").joinpath("schema", "paths.v1.schema.json")
                        .read_text(encoding="utf-8"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validators.validator_for(schema)(schema).validate(doc)
    return doc


# --- the empty document ---------------------------------------------------------------


def test_empty_snapshot_maps_to_an_empty_valid_document(empty_snapshot, tmp_path):
    call = RecordingCall()
    result = _map(empty_snapshot, call)
    assert isinstance(result, paths_name("PathsResult"))
    assert result.model_dump(mode="json") == {
        "schema_version": "paths.v1", "commit": "c0ffee", "scoped": False, "cap": None,
        "fallback": {"status": "skipped", "reason": None},
        "paths": [], "dropped": [], "diagnostics": [], "test_listing_skips": [],
    }
    assert call.calls == []

    out = tmp_path / "out.json"
    paths_name("write_paths_file")(result, out)
    assert _validate_file(out)["paths"] == []
    data = out.read_bytes()
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n")
    again = tmp_path / "again.json"
    paths_name("write_paths_file")(paths_name("PathsResult").model_validate_json(data), again)
    assert again.read_bytes() == data


def test_scoped_run_records_scope_and_cap(empty_snapshot):
    result = _map(empty_snapshot, changed_lines={"pkg/core.py": {1}}, cap=5)
    assert (result.scoped, result.cap) == (True, 5)
    assert result.paths == [] and result.dropped == []


def test_unscoped_run_writes_null_cap_whatever_cap_was_passed(empty_snapshot):
    result = _map(empty_snapshot, cap=2)
    assert (result.scoped, result.cap) == (False, None)


def test_unscoped_run_does_not_validate_cap_or_trace_limit(empty_snapshot):
    result = _map(empty_snapshot, cap=0, trace_limit=0)
    assert (result.scoped, result.cap) == (False, None)


def test_empty_changed_lines_is_scoped(empty_snapshot):
    result = _map(empty_snapshot, changed_lines={})
    assert (result.scoped, result.cap) == (True, 10)


def test_missing_changed_file_is_tolerated(empty_snapshot):
    result = _map(empty_snapshot, changed_lines={"pkg/deleted.py": [4]})
    assert result.scoped is True and result.paths == []


def test_equivalent_spellings_write_identical_files(empty_snapshot, tmp_path):
    write = paths_name("write_paths_file")
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write(_map(empty_snapshot, changed_lines={"./pkg/core.py": [10]}), a)
    write(_map(empty_snapshot, changed_lines={"pkg/core.py": [10]}), b)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("cache", [True, False])
def test_writes_nothing_outside_the_cache_dir(empty_snapshot, tmp_path, cache):
    cache_dir = tmp_path / "cache"
    before = _tree(tmp_path)
    _map(empty_snapshot, changed_lines={"pkg/core.py": [1]}, cache_dir=cache_dir if cache else None)
    after = {k: v for k, v in _tree(tmp_path).items() if not (k == "cache" or k.startswith("cache/"))}
    assert after == before
    if not cache:
        assert not cache_dir.exists()


# --- argument checks, all before any model call ------------------------------------------


@pytest.mark.parametrize("missing", ["call", "resolve"])
def test_missing_call_or_resolve_raises_type_error(empty_snapshot, missing):
    map_paths = paths_name("map_paths")
    kwargs = {"root": empty_snapshot, "commit": "c", "resolve": _resolve, "call": RecordingCall()}
    del kwargs[missing]
    with pytest.raises(TypeError):
        asyncio.run(map_paths(**kwargs))


@pytest.mark.parametrize("changed", [
    {"/etc/passwd": [1]},
    {"../other/x.py": [3]},
    {"pkg/../../x.py": [3]},
    {"pkg/core.py": [0]},
    {"pkg/core.py": [5, -1]},
])
def test_bad_changed_lines_raise_before_any_call(empty_snapshot, changed):
    call = RecordingCall()
    with pytest.raises(ValueError):
        _map(empty_snapshot, call, changed_lines=changed)
    assert call.calls == []


@pytest.mark.parametrize("kw", [{"cap": 0}, {"cap": -1}, {"trace_limit": 0}])
def test_cap_and_trace_limit_below_one_raise_when_scoped(empty_snapshot, kw):
    call = RecordingCall()
    with pytest.raises(ValueError):
        _map(empty_snapshot, call, changed_lines={"pkg/core.py": [1]}, **kw)
    assert call.calls == []


def test_normalise_changed_lines_strips_dot_and_dedupes(empty_snapshot):
    normalise = paths_name("normalise_changed_lines")
    lines = normalise(empty_snapshot, {"./pkg/core.py": (n for n in [10, 3, 10]), "README.md": [1]})
    assert lines == {"pkg/core.py": frozenset({3, 10}), "README.md": frozenset({1})}
    with pytest.raises(ValueError):
        normalise(empty_snapshot, {"/abs/pkg/core.py": [1]})


# --- library surface ----------------------------------------------------------------------


def test_map_paths_signature_and_defaults():
    sig = inspect.signature(paths_name("map_paths"))
    assert inspect.iscoroutinefunction(paths_name("map_paths"))
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    assert [n for n, p in sig.parameters.items() if p.default is inspect.Parameter.empty] == \
        ["root", "commit", "resolve", "call"]
    defaults = {n: p.default for n, p in sig.parameters.items() if p.default is not inspect.Parameter.empty}
    assert defaults == {"changed_lines": None, "seeds": (), "places": (), "cap": 10, "trace_limit": 30,
                        "concurrency": 4, "cache_dir": None, "fallback": True, "recoverable": (),
                        "call_kwargs": None, "progress": None}


def test_new_names_are_exported_and_old_ones_kept():
    for name in EXISTING_EXPORTS + NEW_EXPORTS:
        assert hasattr(code_facts, name), name
        assert name in code_facts.__all__, name
    assert callable(code_facts.write_paths_file)
    assert not any(n.startswith("Test") for n in code_facts.__all__)


def test_new_modules_do_not_import_walkthrough_pipeline():
    pkg = LIB / "code_facts"
    files = sorted(pkg.rglob("*.py"))
    assert (pkg / "schema" / "paths.v1.schema.json").is_file()
    offenders = []
    for py in files:
        for node in ast.walk(ast.parse(py.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            offenders += [f"{py.name}:{n}" for n in names if n.split(".")[0] == "walkthrough_pipeline"]
    assert offenders == []


def test_jsonschema_is_a_declared_and_locked_runtime_dependency():
    project = tomllib.loads((LIB / "pyproject.toml").read_text())["project"]
    assert "jsonschema>=4" in project["dependencies"]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    entry = next(p for p in lock["package"] if p["name"] == "code-facts")
    assert "jsonschema" in {d["name"] for d in entry["dependencies"]}
