"""Tests for fetching a ticket by its ref with one request.

`SPK-12` is fetched from the workspace-scoped /work-items/<IDENT>-<N>/ route.
The project is resolved first (that is what refuses a non-allowlisted
identifier), the URL is built from the resolved identifier, and the answer's
project is checked against it, because the route itself names no project UUID
for the guard to judge.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import plane
import pytest
from conftest import CFG, OK, OK2

PROJECT = {"id": OK, "identifier": "DB", "name": "Dolce"}
ISSUE = {"id": "i-2291", "sequence_id": 2291, "name": "Thing", "project": OK}

VIEW_404 = 'GET x -> 404 {"error": "The requested resource does not exist."}'
ROUTE_404 = 'GET x -> 404 {"error": "Page not found."}  (check PLANE_WORKSPACE)'


def stub(monkeypatch, *, answer=ISSUE, error=None, pages=()):
    requests: list[tuple] = []
    scans: list[str] = []

    def request(method, path, cfg, *, params=None, **kw):
        requests.append((method, path, params))
        if error:
            raise plane.PlaneError(error)
        return dict(answer) if isinstance(answer, dict) else answer

    def paginate(path, cfg, **kw):
        scans.append(path)
        return iter([dict(p) for p in pages])

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "_request", request)
    monkeypatch.setattr(plane, "_paginate", paginate)
    return requests, scans


def test_a_ref_is_one_get_and_no_issue_pages(monkeypatch):
    requests, scans = stub(monkeypatch)
    assert plane.resolve_issue(dict(CFG), "DB-2291")["id"] == "i-2291"
    assert requests == [
        ("GET", "/work-items/DB-2291/", {"expand": "state,assignees,labels"})
    ]
    assert scans == []


@pytest.mark.parametrize("ref", ["db-2291", "DB2291", "db 2291"])
def test_other_spellings_build_the_same_url(monkeypatch, ref):
    requests, _ = stub(monkeypatch)
    plane.resolve_issue(dict(CFG), ref)
    assert requests[0][1] == "/work-items/DB-2291/"


def test_a_bare_number_uses_the_given_project(monkeypatch):
    requests, _ = stub(monkeypatch)
    plane.resolve_issue(dict(CFG), "2291", "DB")
    assert requests[0][1] == "/work-items/DB-2291/"


def test_a_non_allowlisted_identifier_is_refused_with_no_request(monkeypatch):
    requests: list = []
    monkeypatch.setattr(
        plane,
        "list_projects",
        lambda cfg: [
            {"id": OK2, "identifier": "OPS", "name": "Ops", "_allowed": False}
        ],
    )
    monkeypatch.setattr(plane, "_request", lambda *a, **k: requests.append(a))
    with pytest.raises(plane.GuardError, match="not in the allowlist"):
        plane.resolve_issue(dict(CFG), "OPS-1")
    assert requests == []


def test_an_answer_from_another_project_is_refused(monkeypatch):
    stub(monkeypatch, answer={**ISSUE, "project": OK2})
    with pytest.raises(plane.GuardError, match=OK2):
        plane.resolve_issue(dict(CFG), "DB-2291")


def test_an_expanded_project_object_is_checked_too(monkeypatch):
    stub(monkeypatch, answer={**ISSUE, "project": {"id": OK.upper()}})
    assert plane.resolve_issue(dict(CFG), "DB-2291")["id"] == "i-2291"


def test_a_404_from_the_view_is_not_found_without_scanning(monkeypatch):
    requests, scans = stub(monkeypatch, error=VIEW_404)
    with pytest.raises(plane.PlaneError, match="no issue with sequence 2291"):
        plane.resolve_issue(dict(CFG), "DB-2291")
    assert len(requests) == 1 and scans == []


def test_a_non_404_error_surfaces(monkeypatch):
    stub(monkeypatch, error="GET x -> 401 bad token  (check PLANE_API_TOKEN)")
    with pytest.raises(plane.PlaneError, match="401"):
        plane.resolve_issue(dict(CFG), "DB-2291")


def test_an_unknown_route_falls_back_to_the_scan_and_says_so(monkeypatch):
    """A Plane older than the /work-items/ route answers with its catch-all 404;
    only then does the lookup page through the project's issues."""
    _, scans = stub(
        monkeypatch,
        error=ROUTE_404,
        pages=[{"sequence_id": 7}, {"sequence_id": 2291, "id": "found"}],
    )
    err = io.StringIO()
    with redirect_stderr(err):
        assert plane.resolve_issue(dict(CFG), "DB-2291")["id"] == "found"
    assert scans == [f"/projects/{OK}/issues/"]
    assert "scanning DB's issues instead" in err.getvalue()


def test_the_fallback_scan_reports_a_miss_the_same_way(monkeypatch):
    stub(monkeypatch, error=ROUTE_404, pages=[{"sequence_id": 7}])
    with (
        redirect_stderr(io.StringIO()),
        pytest.raises(plane.PlaneError, match="no issue with sequence 2291"),
    ):
        plane.resolve_issue(dict(CFG), "DB-2291")


def test_the_guard_still_refuses_a_write_through_the_lookup_route():
    with pytest.raises(plane.GuardError, match="workspace-scoped"):
        plane._guard("PATCH", "/work-items/DB-2291/", CFG["allowed"])
    plane._guard("GET", "/work-items/DB-2291/?expand=state", CFG["allowed"])
