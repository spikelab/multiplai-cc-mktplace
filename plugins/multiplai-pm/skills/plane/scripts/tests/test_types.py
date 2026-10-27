"""Tests for work item types: listing them and setting one on create/update.

Types are a Plane Pro feature; a server without them answers the types route
with a 404, which the tool turns into one message saying where to turn them on.
The write field is `type_id`, the one Plane's issue serializer declares.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import plane
import pytest
from conftest import CFG, OK, Args

T1 = "eeee1111-2222-3333-4444-555566667777"
T2 = "eeee1111-9999-8888-7777-666655554444"
T3 = "ffff2222-3333-4444-5555-666677778888"

TYPES = [
    {"id": T1, "name": "Task", "is_default": True, "description": "default"},
    {"id": T2, "name": "Bug", "is_default": False, "description": ""},
    {"id": T3, "name": "Story", "is_default": False, "description": "user story"},
]

PROJECT = {"id": OK, "name": "Mine", "identifier": "SPK"}
ISSUE = {"id": "i-12", "sequence_id": 12, "name": "Thing", "project": OK}


def stub(monkeypatch, *, types=TYPES, types_error=None):
    calls: list[tuple] = []

    def paginate(path, cfg, **kw):
        if path.endswith("/work-item-types/"):
            if types_error:
                raise plane.PlaneError(types_error)
            return iter([dict(t) for t in types])
        return iter([])

    def request(method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        calls.append((method, path, body))
        if path.startswith("/work-items/"):
            return dict(ISSUE)
        return {"id": "new", "sequence_id": 13, "name": "made"}

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "_paginate", paginate)
    monkeypatch.setattr(plane, "_request", request)
    return calls


def issue_args(**kw):
    base = {
        "project": None,
        "title": None,
        "body": None,
        "body_file": None,
        "priority": None,
        "state": None,
        "target_date": None,
        "assignee": None,
        "label": None,
        "create_labels": False,
        "estimate": None,
        "type": None,
        "cycle": None,
        "dry_run": False,
    }
    base.update(kw)
    return Args(**base)


def quiet(fn, args):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(CFG, args)
    return out.getvalue()


def test_types_lists_name_default_and_id(monkeypatch):
    stub(monkeypatch)
    out = quiet(plane.cmd_types, Args(project=None))
    assert "Bug" in out and T2 in out and "user story" in out
    assert out.splitlines()[0].split() == ["NAME", "DEFAULT", "ID", "DESCRIPTION"]


@pytest.mark.parametrize("ref", ["bug", T2, "eeee1111-9"])
def test_type_by_name_uuid_or_prefix_goes_into_create(monkeypatch, ref):
    calls = stub(monkeypatch)
    quiet(plane.cmd_create, issue_args(title="x", type=ref))
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["type_id"] == T2


def test_type_goes_into_update_and_alone_is_enough(monkeypatch):
    calls = stub(monkeypatch)
    out = quiet(plane.cmd_update, issue_args(ref="SPK-12", type="Story"))
    patch = next(c for c in calls if c[0] == "PATCH")
    assert patch[2] == {"type_id": T3}
    assert "updated" in out


def test_an_unknown_type_lists_what_exists_and_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="Types here: Task, Bug, Story"):
        plane.cmd_create(CFG, issue_args(title="x", type="Epic"))
    assert calls == []


def test_an_ambiguous_type_prefix_is_refused(monkeypatch):
    stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="2 work item types match"):
        plane.cmd_create(CFG, issue_args(title="x", type="eeee1111"))


def test_types_not_available_becomes_one_message(monkeypatch):
    stub(
        monkeypatch,
        types_error='GET x -> 404 {"error": "Page not found."}  (check ...)',
    )
    with pytest.raises(plane.PlaneError, match="Project settings -> Work item types"):
        plane.cmd_types(CFG, Args(project=None))
    with pytest.raises(plane.PlaneError, match="Plane Pro feature"):
        plane.cmd_create(CFG, issue_args(title="x", type="Bug"))


def test_other_type_errors_surface_unchanged(monkeypatch):
    stub(monkeypatch, types_error="GET x -> 401 nope")
    with pytest.raises(plane.PlaneError, match="401 nope"):
        plane.cmd_types(CFG, Args(project=None))


def test_get_prints_the_type(monkeypatch):
    stub(monkeypatch)
    monkeypatch.setattr(plane, "list_projects", lambda cfg: [dict(PROJECT)])
    monkeypatch.setattr(
        plane, "resolve_issue", lambda cfg, ref, p=None: {**ISSUE, "type_id": T2}
    )
    out = quiet(plane.cmd_get, Args(ref="SPK-12", project=None))
    assert f"type     : {T2}" in out


def test_slim_shows_an_expanded_type_by_name():
    assert plane.slim({"type": {"id": T2, "name": "Bug"}})["type"] == "Bug"
    assert plane.slim({})["type"] is None


def test_create_and_update_parsers_take_type():
    p = plane.build_parser()
    assert p.parse_args(["create", "--title", "x", "--type", "Bug"]).type == "Bug"
    assert p.parse_args(["update", "SPK-1", "--type", "Bug"]).type == "Bug"
