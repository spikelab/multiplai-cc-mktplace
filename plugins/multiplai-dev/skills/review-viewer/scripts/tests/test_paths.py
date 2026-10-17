"""paths.json beside findings.json: which file the viewer accepts, what it logs, what it serves."""

from __future__ import annotations

import json
import logging
import pathlib
import tomllib
import types
import typing
from pathlib import Path

import pytest

from conftest import FIXTURE

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
REPO = SCRIPTS.parents[4]
PATHS_FIXTURE = TESTS / "fixtures" / "paths.example.json"
FIXTURE_DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
HEAD = FIXTURE_DATA["target"]["head_sha"]
BASE = FIXTURE_DATA["target"]["base_sha"]
PATHS_TOP_KEYS = ["schema_version", "commit", "scoped", "cap", "fallback", "paths", "dropped",
                  "diagnostics", "test_listing_skips"]


def paths_doc() -> dict:
    return json.loads(PATHS_FIXTURE.read_text(encoding="utf-8"))


def paths_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "paths.json" in r.getMessage()]


def payload(live) -> dict:
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200
    return detail


# --- the outcome table --------------------------------------------------------------------


def _set(doc: dict, **changes) -> dict:
    doc.update(changes)
    return doc


def _drop(doc: dict, key: str) -> dict:
    del doc[key]
    return doc


def _no_invoked_by(doc: dict) -> dict:
    del doc["paths"][0]["entry"]["invoked_by"]
    return doc


def _hops_as_string(doc: dict) -> dict:
    doc["paths"][0]["hops"] = "app/service.py:8"
    return doc


def _escaping_hop(doc: dict) -> dict:
    doc["paths"][0]["hops"][0]["path"] = "../../etc/passwd"
    return doc


# (id, what goes in paths.json, status, a substring the detail must contain)
CASES = [
    ("exact-match", lambda d: d, "accepted", None),
    ("seven-char-prefix", lambda d: _set(d, commit=HEAD[:7]), "stale", f"commit={HEAD[:7]} head_sha={HEAD}"),
    ("head-is-prefix-of-commit", lambda d: _set(d, commit=HEAD + "0f"), "stale", f"commit={HEAD}0f head_sha={HEAD}"),
    ("commit-is-base-sha", lambda d: _set(d, commit=BASE), "stale", f"commit={BASE} head_sha={HEAD}"),
    ("case-only-difference", lambda d: _set(d, commit=HEAD.upper()), "stale",
     f"commit={HEAD.upper()} head_sha={HEAD}"),
    ("missing-commit", lambda d: _drop(d, "commit"), "invalid", "no commit"),
    ("non-string-commit", lambda d: _set(d, commit=12345), "invalid", "no commit"),
    ("top-level-not-an-object", lambda d: "[]", "invalid", "no commit"),
    ("malformed-json", lambda d: '{"schema_version": "paths.v1", "commit": ', "invalid", "parse failure: "),
    ("missing-required-field", _no_invoked_by, "invalid", "invoked_by"),
    ("hops-as-string", _hops_as_string, "invalid", "hops"),
    ("schema-id-not-paths-v1", lambda d: _set(d, schema_version="paths.v2"), "invalid", "schema_version"),
    ("hop-escapes-the-repo", _escaping_hop, "invalid", "hops"),
    # The commit is checked before validation, so a stale file never reaches PathsResult.
    ("stale-and-invalid", lambda d: _set(d, commit=BASE, schema_version="paths.v2"), "stale",
     f"commit={BASE} head_sha={HEAD}"),
    ("zero-paths", lambda d: _set(d, paths=[]), "accepted", None),
]


def _write(review_dir: Path, content) -> Path:
    path = review_dir / "paths.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


@pytest.mark.parametrize("make,status,detail", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_paths_json_is_accepted_only_when_valid_and_on_the_exact_head_sha(
        make, status, detail, start_live, findings_path, caplog):
    from review_viewer.server import inspect_paths

    content = make(paths_doc())
    path = _write(findings_path.parent, content)

    got = inspect_paths(findings_path.parent, HEAD)
    assert got.status == status
    if detail is not None:
        assert detail in got.detail

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    served = payload(start_live())["paths"]
    records = paths_records(caplog)
    if status == "accepted":
        assert got.result.model_dump(mode="json") == content
        assert served == content
        assert list(served) == PATHS_TOP_KEYS
        assert records == []
    else:
        assert got.result is None
        assert served is None
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.WARNING, f"paths.json ignored ({status}): {path} {got.detail}")]


def test_a_validation_failure_reports_only_its_first_error(findings_path):
    from review_viewer.server import inspect_paths

    doc = paths_doc()
    for p in doc["paths"][:2]:
        del p["entry"]["invoked_by"]
    _write(findings_path.parent, doc)
    got = inspect_paths(findings_path.parent, HEAD)
    assert got.status == "invalid"
    assert got.detail.count("invoked_by") == 1


def test_an_unreadable_paths_json_is_logged_with_the_error(start_live, findings_path, caplog, monkeypatch):
    from review_viewer.server import inspect_paths

    path = _write(findings_path.parent, paths_doc())
    real = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "paths.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    got = inspect_paths(findings_path.parent, HEAD)
    assert got.status == "invalid" and got.result is None
    assert "Permission denied" in got.detail

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    assert payload(start_live())["paths"] is None
    assert [r.getMessage() for r in paths_records(caplog)] == [
        f"paths.json ignored (invalid): {path} {got.detail}"]


def test_paths_json_that_is_not_utf8_is_ignored_without_raising(findings_path, caplog):
    from review_viewer.server import paths_beside

    (findings_path.parent / "paths.json").write_bytes(b'{"commit": "\xff\xfe"}')
    caplog.set_level(logging.DEBUG)
    assert paths_beside(findings_path.parent, HEAD) is None
    assert len(paths_records(caplog)) == 1


@pytest.mark.parametrize("head_sha", [None, ""], ids=["missing", "empty"])
def test_a_review_without_a_head_sha_never_accepts_paths_json(head_sha, findings_path, caplog):
    from review_viewer.server import inspect_paths, paths_beside

    _write(findings_path.parent, paths_doc())
    got = inspect_paths(findings_path.parent, head_sha)
    assert (got.status, got.result) == ("stale", None)

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    assert paths_beside(findings_path.parent, head_sha) is None
    records = paths_records(caplog)
    assert len(records) == 1 and records[0].getMessage().startswith("paths.json ignored (stale): ")


def test_an_absent_paths_json_serves_null_and_logs_nothing(start_live, findings_path, caplog):
    from review_viewer.server import inspect_paths

    got = inspect_paths(findings_path.parent, HEAD)
    assert (got.status, got.result) == ("absent", None)

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    detail = payload(start_live())
    assert "paths" in detail and detail["paths"] is None
    assert paths_records(caplog) == []


def test_paths_json_elsewhere_is_never_read(start_live, findings_path, tmp_path, caplog):
    """Only `<review_dir>/paths.json` counts: not its parent, not the mailbox, not a sibling review."""
    doc = paths_doc()
    sibling = tmp_path / "other-review"
    sibling.mkdir()
    for where in (sibling, findings_path.parent / "viewer", tmp_path):
        where.mkdir(exist_ok=True)
        _write(where, doc)
    caplog.set_level(logging.DEBUG)
    assert payload(start_live())["paths"] is None
    assert paths_records(caplog) == []


# --- the payload ------------------------------------------------------------------------


def _checks(findings_path: Path) -> dict:
    from test_checks import checks_for
    return checks_for(findings_path)


def _walkthrough() -> dict:
    """A stored walkthrough that names no path, in the form the walkthrough route serves it."""
    from review_viewer.models import Walkthrough

    raw = {
        "schema_version": 1, "generated_at": "2026-09-26T10:00:00Z", "base_sha": BASE, "head_sha": HEAD,
        "overview_md": "Totals now multiply by quantity; refunds are logged.",
        "steps": [
            {"id": "refund-logging", "title": "refund logging", "body_md": "Logs the refund.",
             "anchors": [{"path": "app/service.py", "side": "head", "line_start": 17, "line_end": 20}],
             "diagram": None, "finding_ids": []},
            {"id": "totals", "title": "totals", "body_md": "Multiplies by qty.",
             "anchors": [{"path": "app/service.py", "side": "head", "line_start": 7, "line_end": 9}],
             "diagram": None, "finding_ids": []},
        ],
        "complete": False,
    }
    return Walkthrough.model_validate(raw).model_dump(mode="json")


def _review(dir_: Path, findings_path: Path, paths_content=None) -> Path:
    dir_.mkdir()
    out = dir_ / "findings.json"
    out.write_text(findings_path.read_text(encoding="utf-8"), encoding="utf-8")
    (dir_ / "checks.json").write_text(json.dumps(_checks(findings_path)), encoding="utf-8")
    (dir_ / "walkthrough.json").write_text(json.dumps(_walkthrough()), encoding="utf-8")
    if paths_content is not None:
        _write(dir_, paths_content)
    return out


@pytest.mark.parametrize("rejected", [_set(paths_doc(), commit=HEAD[:7]), _no_invoked_by(paths_doc())],
                         ids=["stale", "invalid"])
def test_a_rejected_paths_json_leaves_the_payload_as_without_one(rejected, start_live, findings_path, tmp_path):
    without = start_live(path=_review(tmp_path / "without", findings_path))
    with_rejected = start_live(path=_review(tmp_path / "rejected", findings_path, rejected))

    a, b = payload(without), payload(with_rejected)
    assert a["checks"] is not None and a["paths"] is None
    assert b == a
    route = f"/api/targets/{without.slug}/walkthrough"
    assert with_rejected.request("GET", route) == without.request("GET", route) == (200, _walkthrough())


def test_accepted_paths_arrive_in_the_same_response_as_findings_and_checks(start_live, findings_path, tmp_path):
    doc = paths_doc()
    live = start_live(path=_review(tmp_path / "accepted", findings_path, doc))
    detail = payload(live)
    assert len(detail["findings"]["findings"]) == len(FIXTURE_DATA["findings"])
    assert [a["stage"] for a in detail["checks"]["agents"]] == ["find", "verify"]
    assert detail["paths"] == doc
    assert [p["id"] for p in detail["paths"]["paths"]] == ["p1", "p2", "p3", "p4", "p5", "p6"]
    assert live.viewer.targets[live.slug].paths.commit == HEAD


def test_loading_paths_calls_no_model_and_serves_the_walkthrough_as_stored(
        start_live, findings_path, tmp_path, monkeypatch):
    import multiplai_core

    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the viewer called a model")

    monkeypatch.setattr(multiplai_core, "run_agent", refuse, raising=False)
    runner = getattr(multiplai_core, "agent_runner", None)
    if isinstance(runner, types.ModuleType):
        monkeypatch.setattr(runner, "run_agent", refuse, raising=False)

    doc = paths_doc()
    live = start_live(path=_review(tmp_path / "accepted", findings_path, doc))
    assert payload(live)["paths"] == doc
    status, served = live.request("GET", f"/api/targets/{live.slug}/walkthrough")
    assert status == 200 and served == _walkthrough()
    assert [s["id"] for s in served["steps"]] == ["refund-logging", "totals"]
    assert calls == []


# --- the fixtures and the contract ------------------------------------------------------


def test_the_paths_fixture_is_valid_paths_v1_and_matches_the_findings_fixture():
    from code_facts.paths import PathsResult

    doc = paths_doc()
    result = PathsResult.model_validate(doc)
    assert result.commit == HEAD and len(HEAD) == 40
    by_id = {p.id: p for p in result.paths}
    assert list(by_id) == ["p1", "p2", "p3", "p4", "p5", "p6"]

    p1, p2, p3, p4, p5, p6 = result.paths
    assert p1.entry.handler is not None and p1.stops_at is None
    assert [t.node_id for t in p1.tests.e2e] == ["tests/e2e/test_run.py::test_run_cli"]
    assert [t.node_id for t in p1.tests.mocks_hop_2] == ["tests/e2e/test_run.py::test_run_cli_mocked"]
    assert p2.stops_at == 2 and len(p2.hops) == 2 and p2.break_reason is not None and len(p2.tests.e2e) == 1
    assert not p3.tests.e2e and p3.tests.mocks_hop_2
    assert not p4.tests.e2e and not p4.tests.mocks_hop_2
    assert p5.entry.handler is None and len(p5.tests.e2e) == 1
    assert len(p6.tests.e2e) == 1
    assert result.test_listing_skips

    findings = FIXTURE_DATA["findings"]
    for f in findings:
        ids = f.get("paths", [])
        assert all(i in by_id for i in ids), f"{f['id']} names a path the fixture lacks"
        assert ids == sorted(ids, key=list(by_id).index) and len(set(ids)) == len(ids)
    label = lambda f: (f.get("assessment") or {}).get("label")  # noqa: E731
    on = {(f["status"], label(f)): f["paths"] for f in findings if "paths" in f}
    assert on == {("confirmed", None): ["p1", "p4"], ("unverifiable", None): ["p1"],
                  ("refuted", None): ["p6"], ("confirmed", "low-value"): ["p2"]}
    assert any("paths" not in f for f in findings)


def _model_of(annotation):
    """The pydantic model inside `X`, `list[X]` or `X | None`, else None."""
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _model_of(arg)
        if found is not None:
            return found
    return None


def _is_list(annotation) -> bool:
    if typing.get_origin(annotation) is list:
        return True
    return any(_is_list(a) for a in typing.get_args(annotation)
               if typing.get_origin(annotation) in (typing.Union, types.UnionType))


@pytest.mark.parametrize("dotted", [
    "commit", "paths[].id",
    "paths[].entry.name", "paths[].entry.kind", "paths[].entry.location.path", "paths[].entry.location.line",
    "paths[].entry.invoked_by", "paths[].entry.also_invoked_by",
    "paths[].entry.handler.path", "paths[].entry.handler.line",
    "paths[].hops[].path", "paths[].hops[].line_start", "paths[].hops[].line_end",
    "paths[].stops_at", "paths[].break_reason",
    "paths[].tests.e2e[].node_id", "paths[].tests.e2e[].file", "paths[].tests.e2e[].line",
    "paths[].tests.mocks_hop_2[].node_id",
    "test_listing_skips",
])
def test_the_fields_the_viewer_reads_exist_in_paths_v1(dotted):
    from code_facts.paths import PathsResult

    model, seen = PathsResult, "PathsResult"
    for part in dotted.split("."):
        name = part.removesuffix("[]")
        assert model is not None, f"{seen} is not a model, so it has no field {name!r}"
        assert name in model.model_fields, f"{seen} has no field {name!r}"
        annotation = model.model_fields[name].annotation
        if part.endswith("[]"):
            assert _is_list(annotation), f"{seen}.{name} is not a list"
        model, seen = _model_of(annotation), f"{seen}.{part}"


def test_review_viewer_declares_code_facts_and_the_lock_resolves_it():
    pyproject_text = (SCRIPTS / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    assert "code-facts" in pyproject["project"]["dependencies"]
    deps_block = pyproject_text.split("dependencies = [", 1)[1].split("\n]", 1)[0]
    assert "paths.v1" in deps_block, "the dependency's comment says the viewer reads code_facts' paths.v1"
    assert pyproject["tool"]["uv"]["sources"]["code-facts"] == {"path": "../../../lib/code_facts", "editable": True}

    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    viewer = next(p for p in lock["package"] if p["name"] == "review-viewer")
    assert "code-facts" in {d["name"] for d in viewer["dependencies"]}


def test_the_viewer_imports_only_paths_result_from_code_facts():
    lines = [line.strip() for src in (SCRIPTS / "review_viewer").glob("*.py")
             for line in src.read_text(encoding="utf-8").splitlines() if "code_facts" in line]
    imports = [line for line in lines if line.startswith(("import ", "from "))]
    assert imports and set(imports) == {"from code_facts.paths import PathsResult"}
    for src in (SCRIPTS / "review_viewer").glob("*.py"):
        assert "serialise_paths" not in src.read_text(encoding="utf-8"), src.name


def test_paths_status_is_a_named_tuple_of_status_result_and_detail(findings_path):
    from review_viewer.server import PathsStatus, inspect_paths

    _write(findings_path.parent, paths_doc())
    got = inspect_paths(findings_path.parent, HEAD)
    assert isinstance(got, PathsStatus) and PathsStatus._fields == ("status", "result", "detail")
    status, result, detail = got
    assert status == "accepted" and result.commit == HEAD and isinstance(detail, str)
