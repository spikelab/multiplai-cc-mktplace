"""Tests for parents and relations between issues.

A parent is a field on the issue (`parent`), and Plane only accepts one from
the same project. Relations (blocked_by, relates_to, ...) are their own
endpoint and may cross projects in Plane, so every issue named here goes
through `resolve_issue`, which refuses one outside the allowlist before
anything is sent.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import plane
import pytest
from conftest import BAD, CFG, OK, OK2, Args

ALLOWED_TWO_CFG = {**CFG, "allowed": {OK: "Mine", OK2: "Also mine"}}

PROJECTS = [
    {"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True},
    {"id": OK2, "identifier": "WEB", "name": "Also mine", "_allowed": True},
    {"id": BAD, "identifier": "OPS", "name": "Ops", "_allowed": False},
]


def issue(ident, seq):
    pid = {"SPK": OK, "WEB": OK2, "OPS": BAD}[ident]
    return {
        "id": f"{ident.lower()}-{seq}-id",
        "sequence_id": seq,
        "name": f"{ident} {seq}",
        "project": pid,
    }


def stub(monkeypatch, *, relations=None, error=None):
    """resolve_issue answers from refs; _request records writes and reads."""
    calls: list[tuple] = []

    def resolve(cfg, ref, project_ref=None):
        ref = ref.strip()
        if ref.isdigit():
            ident = next(p["identifier"] for p in PROJECTS if p["id"] == project_ref)
            return issue(ident, int(ref))
        ident, _, seq = ref.partition("-")
        if ident == "OPS":
            raise plane.GuardError("project 'Ops' (OPS) is not in the allowlist")
        return issue(ident, int(seq))

    def request(method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        calls.append((method, path, body, dry_run))
        if error:
            raise plane.PlaneError(error)
        if method == "GET" and path.endswith("/relations/"):
            return relations
        if method == "GET":
            iid = path.rstrip("/").rsplit("/", 1)[-1]
            ident, seq, _ = iid.split("-")
            found = issue(ident.upper(), int(seq))
            if f"/projects/{found['project']}/" not in path:
                raise plane.PlaneError(f"GET {path} -> 404: Work item not found")
            return found
        return {"id": "x"}

    monkeypatch.setattr(plane, "resolve_issue", resolve)
    monkeypatch.setattr(plane, "list_projects", lambda cfg: [dict(p) for p in PROJECTS])
    monkeypatch.setattr(plane, "_request", request)
    return calls


def run(fn, args, cfg=CFG):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fn(cfg, args)
    return out.getvalue(), err.getvalue()


def issue_args(**kw):
    base = {
        "project": None,
        "ref": "SPK-12",
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
        "parent": None,
        "cycle": None,
        "dry_run": False,
    }
    base.update(kw)
    return Args(**base)


# --- parent -------------------------------------------------------------------


def test_update_parent_by_ref_patches_the_parent_id(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_update, issue_args(parent="SPK-3"))
    assert calls == [
        ("PATCH", f"/projects/{OK}/issues/spk-12-id/", {"parent": "spk-3-id"}, False)
    ]


def test_a_bare_parent_number_is_in_the_same_project(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_update, issue_args(parent="3"))
    assert calls[0][2] == {"parent": "spk-3-id"}


def test_parent_none_clears_it(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_update, issue_args(parent="none"))
    assert calls[0][2] == {"parent": None}


def test_a_parent_in_another_project_is_refused_before_any_write(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="same project"):
        plane.cmd_update(CFG, issue_args(parent="WEB-3"))
    assert calls == []


def test_an_issue_cannot_be_its_own_parent(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="its own parent"):
        plane.cmd_update(CFG, issue_args(parent="SPK-12"))
    assert calls == []


def test_create_takes_a_parent(monkeypatch):
    calls = stub(monkeypatch)
    monkeypatch.setattr(
        plane, "resolve_project", lambda cfg, ref=None: dict(PROJECTS[0])
    )
    run(plane.cmd_create, issue_args(title="child", parent="SPK-3", upload=None))
    assert calls[0][0] == "POST" and calls[0][2]["parent"] == "spk-3-id"


def test_slim_and_get_show_an_expanded_parent_as_a_ref():
    got = plane.slim({"parent": {"id": "p", "sequence_id": 3, "project_id": OK}}, "SPK")
    assert got["parent"] == "SPK-3"
    assert plane.slim({"parent": "raw-id"}, "SPK")["parent"] == "raw-id"


def test_the_parser_takes_parent_on_create_and_update():
    p = plane.build_parser()
    assert (
        p.parse_args(["create", "--title", "x", "--parent", "SPK-3"]).parent == "SPK-3"
    )
    assert p.parse_args(["update", "SPK-1", "--parent", "none"]).parent == "none"


# --- relate / unrelate --------------------------------------------------------


def relate_args(relation="blocked_by", other=("SPK-3",), **kw):
    base = {
        "project": None,
        "ref": "SPK-12",
        "relation": relation,
        "other": list(other),
    }
    base.update({"dry_run": False}, **kw)
    return Args(**base)


def test_relate_posts_the_type_and_every_other_id(monkeypatch):
    calls = stub(monkeypatch)
    out, _ = run(plane.cmd_relate, relate_args(other=["SPK-3", "4"]), ALLOWED_TWO_CFG)
    assert calls == [
        (
            "POST",
            f"/projects/{OK}/work-items/spk-12-id/relations/",
            {"relation_type": "blocked_by", "issues": ["spk-3-id", "spk-4-id"]},
            False,
        )
    ]
    assert "SPK-12 blocked_by SPK-3, SPK-4" in out


def test_relate_crosses_into_another_allowed_project(monkeypatch):
    calls = stub(monkeypatch)
    run(plane.cmd_relate, relate_args("relates_to", ["WEB-7"]), ALLOWED_TWO_CFG)
    assert calls[0][2] == {"relation_type": "relates_to", "issues": ["web-7-id"]}


def test_relate_refuses_an_issue_outside_the_allowlist_with_nothing_sent(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.GuardError):
        plane.cmd_relate(CFG, relate_args(other=["SPK-3", "OPS-1"]))
    assert calls == []


def test_relate_refuses_the_issue_itself(monkeypatch):
    calls = stub(monkeypatch)
    with pytest.raises(plane.PlaneError, match="itself"):
        plane.cmd_relate(CFG, relate_args(other=["12"]))
    assert calls == []


def test_relate_dry_run_sends_nothing(monkeypatch):
    calls = stub(monkeypatch)
    out, _ = run(plane.cmd_relate, relate_args(dry_run=True))
    assert calls[0][3] is True and "blocked_by" not in out


def test_the_parser_accepts_only_plane_relation_types():
    p = plane.build_parser()
    for rtype in plane._RELATION_TYPES:
        assert p.parse_args(["relate", "SPK-1", rtype, "SPK-2"]).relation == rtype
    with pytest.raises(SystemExit):
        p.parse_args(["relate", "SPK-1", "depends_on", "SPK-2"])


def test_unrelate_posts_one_remove_per_other(monkeypatch):
    calls = stub(monkeypatch)
    out, _ = run(
        plane.cmd_unrelate,
        Args(project=None, ref="SPK-12", other=["3", "SPK-4"], dry_run=False),
    )
    path = f"/projects/{OK}/work-items/spk-12-id/relations/remove/"
    assert calls == [
        ("POST", path, {"related_issue": "spk-3-id"}, False),
        ("POST", path, {"related_issue": "spk-4-id"}, False),
    ]
    assert "between SPK-12 and SPK-3" in out


def test_a_server_without_the_relations_api_says_so(monkeypatch):
    stub(monkeypatch, error='POST x -> 404 {"error": "Page not found."}  (check …)')
    with pytest.raises(plane.PlaneError, match="no work item relations API"):
        plane.cmd_relate(CFG, relate_args())


def test_other_relation_errors_surface_unchanged(monkeypatch):
    stub(monkeypatch, error='POST x -> 404 {"error": "Issue relation not found"}')
    with pytest.raises(plane.PlaneError, match="Issue relation not found"):
        plane.cmd_unrelate(
            CFG, Args(project=None, ref="SPK-12", other=["3"], dry_run=False)
        )


# --- relations ----------------------------------------------------------------


def test_relations_lists_refs_and_withholds_issues_outside_the_allowlist(monkeypatch):
    calls = stub(
        monkeypatch,
        relations={
            "blocked_by": [{"project_id": OK, "issue_id": "spk-3-id"}],
            "blocking": [{"project_id": OK2, "issue_id": "web-7-id"}],
            "relates_to": [{"project_id": BAD, "issue_id": "ops-1-id"}],
            "duplicate": [],
        },
    )
    out, err = run(
        plane.cmd_relations, Args(project=None, ref="SPK-12"), ALLOWED_TWO_CFG
    )
    lines = out.splitlines()
    assert lines[0].split() == ["RELATION", "REF", "NAME", "ID"]
    assert any(line.startswith("blocked_by") and "SPK-3" in line for line in lines)
    assert any(line.startswith("blocking") and "WEB-7" in line for line in lines)
    assert "OPS" not in out and "ops-1-id" not in out
    assert "1 relation(s) to issues outside the allowlist withheld" in err
    assert not any(BAD in c[1] for c in calls)


def test_relations_reads_bare_issue_ids(monkeypatch):
    # Some Plane servers answer with a list of issue ids per type, with no
    # project id, so each id is looked up in the allowed projects instead.
    calls = stub(
        monkeypatch,
        relations={
            "blocked_by": ["spk-3-id", "web-7-id"],
            "relates_to": ["ops-1-id"],
        },
    )
    out, err = run(
        plane.cmd_relations, Args(project=None, ref="SPK-12"), ALLOWED_TWO_CFG
    )
    lines = out.splitlines()
    assert any(line.startswith("blocked_by") and "SPK-3" in line for line in lines)
    assert any(line.startswith("blocked_by") and "WEB-7" in line for line in lines)
    assert "OPS" not in out and "ops-1-id" not in out
    assert "1 relation(s) to issues outside the allowlist withheld" in err
    assert not any(BAD in c[1] for c in calls)
    # The issue's own project is tried first.
    gets = [c[1] for c in calls if c[1].endswith("/spk-3-id/")]
    assert gets == [f"/projects/{OK}/issues/spk-3-id/"]


def test_relations_surfaces_errors_other_than_404_for_bare_ids(monkeypatch):
    stub(monkeypatch, relations={"blocked_by": ["spk-3-id"]})
    real = plane._request

    def request(method, path, cfg, **kw):
        if path.endswith("/spk-3-id/"):
            raise plane.PlaneError(f"GET {path} -> 401: bad token")
        return real(method, path, cfg, **kw)

    monkeypatch.setattr(plane, "_request", request)
    with pytest.raises(plane.PlaneError, match="401"):
        run(plane.cmd_relations, Args(project=None, ref="SPK-12"), ALLOWED_TWO_CFG)
