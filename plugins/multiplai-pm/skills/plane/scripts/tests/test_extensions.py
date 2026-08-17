"""Tests for the defects closed on the baseline and for the capabilities added.

The fixtures are trimmed copies of real responses from a live Plane Cloud
workspace (projects DB and DFT, 2026-08-02), so the shapes here are the shapes
the API actually returns — including the two that read like the opposite of
what they mean: an issue listed under `cycle-issues/` with `cycle_id: null`,
and `/estimates/` answering a bare object where every neighbouring endpoint
answers a paginated envelope.
"""

from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plane  # noqa: E402

OK = "1fa4d2f6-e016-428a-aca7-5ebb1c8bca4f"
BAD = "b996f98b-0bdc-4bea-9ec0-92da5268f054"
ALLOWED = {OK: "Mine"}
CFG = {"base": "https://api.plane.so", "workspace": "ws", "token": "t", "allowed": ALLOWED}

ISSUE = "792a89b1-37db-47e1-a947-6fb1b79198d6"

# The four cycles of project DB, verbatim bounds. Note 2026-08-03: S16 ends at
# 21:59:00Z and S17 starts at 22:00:01Z on the same calendar day.
DB_CYCLES = [
    {"id": "b001eecb-d718-4890-8198-1dbe2fbe124c", "name": "S18 ~ AUG26",
     "start_date": "2026-08-17T22:00:01Z", "end_date": "2026-08-31T21:59:00Z"},
    {"id": "7e7899c4-975f-43c6-9e60-2c8edeb07835", "name": "S17 ~ AUG26",
     "start_date": "2026-08-03T22:00:01Z", "end_date": "2026-08-17T21:59:00Z"},
    {"id": "a476a18d-d56a-4835-8ddd-0482c9a9b46a", "name": "S16 ~ JUL26",
     "start_date": "2026-07-21T10:31:41.143422Z", "end_date": "2026-08-03T21:59:00Z"},
    {"id": "137a3296-12b8-466c-9b40-3ed8666e35eb", "name": "S15 ~ JUL26",
     "start_date": "2026-07-06T22:00:01Z", "end_date": "2026-07-20T21:59:00Z"},
]

DFT_POINTS = [
    {"id": "d02f065a-04d7-4d74-a159-2a4142b9cd0d", "key": 1, "value": "1"},
    {"id": "082021d6-4dcc-487d-87f3-b5b9e59b5a41", "key": 2, "value": "2"},
    {"id": "269f1d46-c7fa-4aa8-bd99-9e6fa2ee603d", "key": 3, "value": "3"},
    {"id": "1b0b6330-c61a-4b04-9343-7fcb7c80837d", "key": 4, "value": "5"},
]

MEMBERS = [
    {"id": "2fd6e2a0-83a4-4415-8475-90d5d8953085", "display_name": "Slack",
     "first_name": "Slack", "last_name": "", "email": "bot@plane.so",
     "is_active": True, "is_bot": True},
    {"id": "f5dab78f-fa50-4055-b415-0b1bfa98652c", "display_name": "alice",
     "first_name": "Alice", "last_name": "Anders",
     "email": "alice@example.com", "is_active": True, "is_bot": False},
    {"id": "093da523-f308-457e-a03f-b681ecba5b5f", "display_name": "bob",
     "first_name": "BOB", "last_name": "", "email": "robert@example.com",
     "is_active": True, "is_bot": False},
]


class Recorder:
    """Stands in for _request, answering by path and recording every write."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple] = []

    def __call__(self, method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        self.calls.append((method, path.split("?")[0], body))
        for prefix, payload in self.routes.items():
            if path.split("?")[0] == prefix:
                return payload() if callable(payload) else payload
        return None

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def install(monkeypatch, routes):
    rec = Recorder(routes)
    monkeypatch.setattr(plane, "_request", rec)
    return rec


def envelope(items):
    return {"results": items, "next_cursor": "", "next_page_results": False}


# --- the five baseline defects, from the other side ---------------------------


def test_guard_refuses_dot_segments_however_they_are_spelled():
    """The allowlisted UUID must not work as a prefix that launders the rest."""
    for path in (
        f"/projects/{OK}/../{BAD}/issues/",
        f"/projects/{OK}/%2e%2e/{BAD}/issues/",
        f"/projects/{OK}/%252e%252e/{BAD}/issues/",
        f"/projects/{OK}/./issues/",
    ):
        with pytest.raises(plane.GuardError, match="dot segment"):
            plane._guard("POST", path, ALLOWED)


def test_guard_still_reads_an_encoded_project_uuid_as_a_project():
    """Decoding first makes the encoded variants stricter, not looser."""
    for path in (f"/projects%2f{BAD}/issues/", f"/projects/{BAD}%2fissues/"):
        with pytest.raises(plane.GuardError, match="allowlist"):
            plane._guard("GET", path, ALLOWED)


def test_check_fails_when_the_chokepoint_is_disconnected(monkeypatch):
    """The mutation the old self-test could not see: delete the guard call in
    `_request` and `check` must go red, because a self-test that only exercises
    `_guard` certifies a function nobody has to call."""
    src = Path(plane.__file__).read_text(encoding="utf-8")
    mutated = src.replace('    _guard(method, path, cfg["allowed"])\n', "", 1)
    assert mutated != src, "the guard call moved - update this mutation"
    mod = types.ModuleType("plane_mutant")
    mod.__file__ = plane.__file__
    exec(compile(mutated, "plane_mutant", "exec"), mod.__dict__)

    projects = [{"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True}]
    mod.list_projects = lambda _cfg: projects
    cfg = dict(CFG, allowed=dict(ALLOWED), _projects=projects)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.cmd_check(cfg, None)
    assert rc == 1 and "LEAKED" in buf.getvalue()


def test_check_is_green_and_covers_query_params_and_traversal(monkeypatch):
    projects = [{"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True}]
    monkeypatch.setattr(plane, "list_projects", lambda _cfg: projects)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plane.cmd_check(dict(CFG, allowed=dict(ALLOWED)), None)
    out = buf.getvalue()
    assert rc == 0 and "PASS" in out and "LEAKED" not in out
    assert "?project_id=" in out and "/../" in out
    assert "chokepoint self-test" in out


def test_search_asks_for_a_limit_and_says_when_it_hits_it(monkeypatch, capsys):
    """Observed: `search=calendario` returns 10, `&limit=100` returns 29."""
    hits = [{"id": str(n), "name": f"h{n}", "project_id": OK, "sequence_id": n}
            for n in range(5)]
    rec = install(monkeypatch, {"/issues/search/": {"issues": hits}})
    plane.cmd_search(dict(CFG), types.SimpleNamespace(query="x", json=True, limit=5))
    assert "TRUNCATED" in capsys.readouterr().err
    assert rec.calls  # the limit is in the query string, which Recorder strips
    plane.cmd_search(dict(CFG), types.SimpleNamespace(query="x", json=True, limit=50))
    assert "TRUNCATED" not in capsys.readouterr().err


def test_search_limit_is_actually_sent(monkeypatch):
    seen = {}

    def fake(method, path, cfg, *, params=None, **kw):  # noqa: ARG001
        seen.update(params or {})
        return {"issues": []}

    monkeypatch.setattr(plane, "_request", fake)
    plane.cmd_search(dict(CFG), types.SimpleNamespace(query="x", json=True, limit=None))
    assert seen == {"search": "x", "limit": 100}


def test_nested_fences_and_tilde_fences_survive_sanitising():
    nested = "````\nouter - a \u2014 b\n```\ninner \u2014 c\n```\nouter \u2014 d\n````"
    assert plane._sanitize_prose(nested) == nested
    tilde = "~~~\na \u2014 b\n~~~"
    assert plane._sanitize_prose(tilde) == tilde
    # ...and the inner block stays one code block downstream, not prose.
    html = plane.md_to_html(nested)
    assert html.count("<pre>") == 1 and "<p>inner" not in html


def test_md_to_html_survives_a_nul_and_renders_images():
    assert plane.md_to_html("\x000\x00") == "<p>0</p>"
    assert plane.md_to_html("ciao \x00 0\x00 mondo") == "<p>ciao  0 mondo</p>"
    assert plane.md_to_html("![alt](https://x/y.png)") == (
        '<p><img src="https://x/y.png" alt="alt"></p>'
    )
    # An unsafe target is text for images too, never a src.
    assert "img" not in plane.md_to_html("![x](javascript:alert(1))")


def test_nested_list_items_degrade_to_a_paragraph():
    assert plane.md_to_html("- a\n    - b\n        - c") == (
        "<ul><li>a</li></ul><p>- b - c</p>"
    )
    # Up to three spaces is still the same level, per CommonMark.
    assert plane.md_to_html("- a\n  - b").count("<li>") == 2


def test_not_found_says_what_it_actually_checked(monkeypatch):
    monkeypatch.setattr(plane, "_request", lambda *a, **k: envelope([]))
    monkeypatch.setattr(
        plane, "resolve_project",
        lambda cfg, ref: {"id": OK, "identifier": "SPK", "name": "Mine"},
    )
    with pytest.raises(plane.PlaneError, match="listed by the API"):
        plane.resolve_issue(dict(CFG), "SPK-1974")


# --- cycles -------------------------------------------------------------------


def test_active_cycle_is_decided_on_instants_not_on_dates():
    """2026-08-03 is the changeover day: comparing the first ten characters
    matches both S16 and S17, comparing instants matches exactly one."""
    truncating = [
        c for c in DB_CYCLES
        if c["start_date"][:10] <= "2026-08-03" <= c["end_date"][:10]
    ]
    assert {c["name"] for c in truncating} == {"S16 ~ JUL26", "S17 ~ AUG26"}

    noon = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    live = [c for c in DB_CYCLES if plane.cycle_is_live(c, noon)]
    assert [c["name"] for c in live] == ["S16 ~ JUL26"]

    late = datetime(2026, 8, 3, 23, 0, tzinfo=timezone.utc)
    assert [c["name"] for c in DB_CYCLES if plane.cycle_is_live(c, late)] == [
        "S17 ~ AUG26"
    ]


def test_overlapping_cycles_are_refused_rather_than_picked(monkeypatch):
    twins = [dict(c, id=c["id"], name=f"twin {n}") for n, c in enumerate(DB_CYCLES[:2])]
    for c in twins:
        c["start_date"] = "2000-01-01T00:00:00Z"
        c["end_date"] = "2100-01-01T00:00:00Z"
    monkeypatch.setattr(plane, "list_cycles", lambda cfg, pid: twins)
    with pytest.raises(plane.PlaneError, match="active at the same time"):
        plane.active_cycle(dict(CFG), OK)


def test_no_cycles_listed_is_not_reported_as_no_cycles_existing(monkeypatch):
    """Verified on Plane Cloud: a non-member of the project gets 200 and zero
    results on a project that has four cycles."""
    monkeypatch.setattr(plane, "list_cycles", lambda cfg, pid: [])
    with pytest.raises(plane.PlaneError, match="not proof that none exist"):
        plane.active_cycle(dict(CFG), OK)


def test_membership_is_read_from_the_issue_not_from_cycle_issues():
    """cycle-issues/ lists the members of a cycle with `cycle_id: null`."""
    from_cycle_list = {"id": ISSUE, "cycle_id": None, "name": "in the cycle"}
    from_issue = {"id": ISSUE, "cycle_id": DB_CYCLES[2]["id"]}
    assert plane.issue_cycle_id(from_cycle_list) is None
    assert plane.issue_cycle_id(from_issue) == DB_CYCLES[2]["id"]
    # There is no `cycle` key at all, so the obvious read is always wrong.
    assert "cycle" not in from_issue


def test_adding_to_a_cycle_posts_once_and_not_twice(monkeypatch, capsys):
    cid = DB_CYCLES[2]["id"]
    rec = install(monkeypatch, {f"/projects/{OK}/cycles/": envelope(DB_CYCLES)})
    plane._place_in_cycle(dict(CFG), OK, {"id": ISSUE, "cycle_id": None}, cid)
    assert rec.writes() == [
        ("POST", f"/projects/{OK}/cycles/{cid}/cycle-issues/", {"issues": [ISSUE]})
    ]
    rec.calls.clear()
    plane._place_in_cycle(dict(CFG), OK, {"id": ISSUE, "cycle_id": cid}, cid)
    assert rec.writes() == []
    assert "already in cycle" in capsys.readouterr().out


# --- estimates ----------------------------------------------------------------


def test_estimate_points_take_two_calls_and_survive_the_bare_object(monkeypatch):
    """`/estimates/` is a single object: _paginate would yield nothing at all."""
    eid = "2aa00637-e275-4849-a68c-592c26145996"
    rec = install(monkeypatch, {
        f"/projects/{OK}/estimates/": {"id": eid, "name": "Points", "type": "points"},
        f"/projects/{OK}/estimates/{eid}/estimate-points/": list(reversed(DFT_POINTS)),
    })
    points = plane.estimate_points(dict(CFG), OK)
    assert [p["value"] for p in points] == ["1", "2", "3", "5"]
    assert len(rec.calls) == 2


def test_estimate_value_resolves_on_the_string_not_the_number(monkeypatch):
    eid = "e"
    install(monkeypatch, {
        f"/projects/{OK}/estimates/": {"id": eid},
        f"/projects/{OK}/estimates/{eid}/estimate-points/": DFT_POINTS,
    })
    assert plane.resolve_estimate_point(dict(CFG), OK, 3) == DFT_POINTS[2]["id"]
    assert plane.resolve_estimate_point(dict(CFG), OK, "5") == DFT_POINTS[3]["id"]
    with pytest.raises(plane.PlaneError, match="Available: 1, 2, 3, 5"):
        plane.resolve_estimate_point(dict(CFG), OK, "4")


def test_a_project_without_an_estimate_set_says_so(monkeypatch):
    def fake(method, path, cfg, **kw):  # noqa: ARG001
        raise plane.PlaneError("GET https://x -> 404 {\"error\":\"Estimate not found\"}")

    monkeypatch.setattr(plane, "_request", fake)
    with pytest.raises(plane.PlaneError, match="no estimate set"):
        plane.estimate_points(dict(CFG), OK)


# --- assignees and labels -----------------------------------------------------


def test_member_resolution_takes_names_emails_and_uuids(monkeypatch):
    monkeypatch.setattr(plane, "project_members", lambda cfg, pid: MEMBERS)
    for ref in ("alice", "Alice Anders", "ALICE@EXAMPLE.COM", MEMBERS[1]["id"]):
        assert plane.resolve_member(dict(CFG), OK, ref) == MEMBERS[1]["id"]


def test_member_resolution_skips_bots_and_refuses_ambiguity(monkeypatch):
    twins = MEMBERS + [dict(MEMBERS[1], id=OK, email="alice@example.org")]
    monkeypatch.setattr(plane, "project_members", lambda cfg, pid: twins)
    with pytest.raises(plane.PlaneError, match="2 members match"):
        plane.resolve_member(dict(CFG), OK, "alice")
    with pytest.raises(plane.PlaneError, match="no project member matching"):
        plane.resolve_member(dict(CFG), OK, "Slack")


def test_a_missing_label_is_only_created_when_asked(monkeypatch):
    label = {"id": "6d4c3ab3-005a-4ef8-9bad-2037fe8d8bfa", "name": "plane-skill-test"}
    rec = install(monkeypatch, {
        f"/projects/{OK}/labels/": envelope([label]),
    })
    assert plane.resolve_label(dict(CFG), OK, "PLANE-SKILL-TEST") == label["id"]
    assert rec.writes() == []
    with pytest.raises(plane.PlaneError, match="--create-labels"):
        plane.resolve_label(dict(CFG), OK, "nuova")

    rec = install(monkeypatch, {
        f"/projects/{OK}/labels/": envelope([]),
    })
    rec.routes[f"/projects/{OK}/labels/"] = envelope([])
    with pytest.raises(plane.PlaneError, match="returned no id"):
        # The POST goes through the same path, and Recorder answers the GET
        # shape; what matters here is that a create was attempted at all.
        plane.resolve_label(dict(CFG), OK, "nuova", create=True)
    assert rec.writes() == [("POST", f"/projects/{OK}/labels/", {"name": "nuova"})]


def test_dry_run_does_not_invent_an_id_for_a_label_it_would_create(monkeypatch, capsys):
    install(monkeypatch, {f"/projects/{OK}/labels/": envelope([])})
    assert plane.resolve_label(dict(CFG), OK, "nuova", create=True, dry_run=True) is None
    assert "[dry-run]" in capsys.readouterr().out


def test_assignment_flags_become_whole_list_writes(monkeypatch):
    monkeypatch.setattr(plane, "project_members", lambda cfg, pid: MEMBERS)
    monkeypatch.setattr(plane, "resolve_estimate_point", lambda cfg, pid, v: "point-3")
    monkeypatch.setattr(
        plane, "resolve_label", lambda cfg, pid, n, **kw: f"label-{n}"
    )
    payload: dict = {}
    args = types.SimpleNamespace(
        assignee=["alice", "bob"], label=["a", "b"],
        create_labels=False, estimate="3", dry_run=False,
    )
    plane._apply_assignment_flags(dict(CFG), args, OK, payload)
    assert payload == {
        "assignees": [MEMBERS[1]["id"], MEMBERS[2]["id"]],
        "labels": ["label-a", "label-b"],
        "estimate_point": "point-3",
    }


# --- attachments and the one call outside the chokepoint ----------------------


DESCRIPTION = (
    '<div><p>x</p><image-component data-id="42f889ff-ea4e-44ce-b6ee-e3c96d34f2d4" '
    'src="0a147e39-2e55-4a18-9e13-f35279a13ee2" width="331px"></image-component>'
    '<img src="0a147e39-2e55-4a18-9e13-f35279a13ee2">'
    '<img src="https://example.com/external.png"></div>'
)


def test_inline_assets_are_found_deduplicated_and_url_srcs_ignored():
    assert plane.inline_asset_ids({"description_html": DESCRIPTION}) == [
        "0a147e39-2e55-4a18-9e13-f35279a13ee2"
    ]
    assert plane.inline_asset_ids({}) == []


def test_fetch_asset_refuses_a_host_plane_does_not_serve_assets_from():
    for url in (
        "https://evil.example.com/x.png",
        "http://planefs-uploads.s3.amazonaws.com/x.png",
        "https://amazonaws.com.evil.test/x.png",
    ):
        with pytest.raises(plane.PlaneError, match="refusing to download"):
            plane.fetch_asset(url, "https://api.plane.so")


def test_fetch_asset_sends_no_credentials_and_does_not_follow_redirects(monkeypatch):
    """The whole point of the separate function: the Plane token must not reach
    S3. urllib copies custom headers across a cross-host redirect, so both the
    header and the redirect have to be off."""
    captured = {}

    class FakeResponse:
        def read(self, _n=None):
            return b"\x89PNG\r\n\x1a\n"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class FakeOpener:
        def open(self, req, timeout=None):  # noqa: ARG002
            captured["headers"] = dict(req.headers)
            captured["url"] = req.full_url
            return FakeResponse()

    monkeypatch.setattr(plane.urllib.request, "build_opener", lambda *h: FakeOpener())
    blob = plane.fetch_asset(
        "https://planefs-uploads.s3.amazonaws.com/a?X-Amz-Signature=x",
        "https://api.plane.so",
    )
    assert blob.startswith(b"\x89PNG")
    assert not any("api-key" in k.lower() or "authorization" in k.lower()
                   for k in captured["headers"])
    assert plane._NoRedirect().redirect_request(None, None, 302, "", {}, "") is None


def test_attachments_lists_records_and_inline_assets(monkeypatch, capsys):
    asset = "0a147e39-2e55-4a18-9e13-f35279a13ee2"
    record = {"id": "rec-1", "asset": "11111111-1111-1111-1111-111111111111",
              "attributes": {"name": "spec.pdf", "type": "application/pdf"}}
    install(monkeypatch, {
        f"/projects/{OK}/issues/{ISSUE}/": {"id": ISSUE, "description_html": DESCRIPTION},
        f"/projects/{OK}/issues/{ISSUE}/issue-attachments/": [record],
        f"/assets/{asset}/": {"asset_id": asset, "asset_name": "image.png",
                              "asset_type": "image/png", "asset_url": "https://s3/x"},
    })
    monkeypatch.setattr(
        plane, "resolve_issue", lambda cfg, ref, p=None: {"id": ISSUE, "project": OK}
    )
    plane.cmd_attachments(
        dict(CFG), types.SimpleNamespace(ref="SPK-1", project=None, json=False, download=None)
    )
    out = capsys.readouterr().out
    assert "spec.pdf" in out and "image.png" in out
    assert "record" in out and "inline" in out


def test_downloaded_filenames_cannot_escape_the_target_directory():
    assert plane._safe_filename("../../etc/passwd", "x") == "passwd"
    assert plane._safe_filename("C:\\Windows\\system32\\a.dll", "x") == "a.dll"
    assert plane._safe_filename("", "fallback.bin") == "fallback.bin"
    assert plane._safe_filename("..", "fallback.bin") == "fallback.bin"
