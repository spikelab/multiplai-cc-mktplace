"""Review of PR #224 (this repo) — one test per finding, same convention as
test_review_findings.py: each test asserts the behaviour the plugin claims,
and a failure is proof the claim does not hold on this commit.

Naming: test_g<N>_* maps to finding N of that review, most severe first.
"""

from __future__ import annotations

import pytest

import plane
from conftest import BAD, CFG, OK
from conftest import Args as _Args
from conftest import guard


# --- G1: the project object must stay read-only through slash games ----------


@pytest.mark.parametrize("suffix", ["//", "///", "//./"])
@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
def test_g1_trailing_slash_spellings_cannot_reach_the_project_object(method, suffix):
    """`/projects/<uuid>//` merges to `/projects/<uuid>/` at any slash-merging
    proxy (nginx merge_slashes is on by default), so the guard must judge the
    merged spelling, not the literal one."""
    with pytest.raises(plane.GuardError):
        guard(method, f"/projects/{OK}{suffix}")


# --- G2: a failing token must not read as "issue not found" -------------------


def test_g2_auth_failure_during_uuid_scan_surfaces_instead_of_not_found(monkeypatch):
    def fake_request(method, path, cfg, **kw):
        if path == "/projects/":
            return [{"id": OK, "name": "Mine"}]
        raise plane.PlaneError(f"GET {path} -> 401 (check PLANE_API_TOKEN)")

    monkeypatch.setattr(plane, "_request", fake_request)
    with pytest.raises(plane.PlaneError, match="401"):
        plane.resolve_issue(dict(CFG), "792a89b1-37db-47e1-a947-6fb1b79198d6")


def test_g2_404_during_uuid_scan_still_means_not_found(monkeypatch):
    def fake_request(method, path, cfg, **kw):
        if path == "/projects/":
            return [{"id": OK, "name": "Mine"}]
        raise plane.PlaneError(f"GET {path} -> 404 (check PLANE_WORKSPACE)")

    monkeypatch.setattr(plane, "_request", fake_request)
    with pytest.raises(plane.PlaneError, match="not found in any allowed project"):
        plane.resolve_issue(dict(CFG), "792a89b1-37db-47e1-a947-6fb1b79198d6")


# --- G3: attachment downloads must not silently overwrite each other ----------


def test_g3_colliding_attachment_names_get_distinct_files(monkeypatch, tmp_path):
    issue = {"id": "792a89b1-37db-47e1-a947-6fb1b79198d6", "name": "x"}
    records = [
        {"attributes": {"name": "shot.png"}, "asset": f"aaaaaaa{n}-0000-0000-0000-000000000000"}
        for n in (1, 2)
    ]

    def fake_request(method, path, cfg, **kw):
        if path.endswith("issue-attachments/"):
            return {"results": records, "next_page_results": False}
        return dict(issue, description_html="")

    monkeypatch.setattr(plane, "_request", fake_request)
    monkeypatch.setattr(plane, "resolve_issue", lambda *a, **k: dict(issue, project=OK))
    monkeypatch.setattr(
        plane, "asset_meta",
        lambda cfg, aid: {"asset_name": "shot.png", "asset_url": f"https://api.plane.so/{aid}"},
    )
    monkeypatch.setattr(plane, "fetch_asset", lambda url, base: url.encode())

    plane.cmd_attachments(
        dict(CFG), _Args(ref="SPK-1", project=None, download=str(tmp_path))
    )
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2, f"one download clobbered the other: {files}"


# --- G4: an empty-body 2xx write is not a dry-run ------------------------------


def test_g4_create_with_empty_response_body_does_not_pretend_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref: {"id": OK, "identifier": "SPK"})
    monkeypatch.setattr(plane, "_request", lambda *a, **k: None)  # 201, empty body
    args = _Args(
        project=None, title="t", body=None, body_file=None, priority=None,
        state=None, target_date=None, assignee=None, label=None,
        create_labels=False, estimate=None, cycle=None, dry_run=False,
    )
    plane.cmd_create(dict(CFG), args)
    out = capsys.readouterr().out
    assert "dry-run" not in out
    assert "created" in out


def test_g4_update_with_empty_response_body_still_confirms(monkeypatch, capsys):
    monkeypatch.setattr(
        plane, "resolve_issue",
        lambda *a, **k: {"id": "792a89b1-37db-47e1-a947-6fb1b79198d6",
                         "name": "the issue", "project": OK},
    )
    monkeypatch.setattr(plane, "_request", lambda *a, **k: None)  # 200, empty body
    args = _Args(
        ref="SPK-1", project=None, title="new", body=None, body_file=None,
        priority=None, state=None, target_date=None, assignee=None, label=None,
        create_labels=False, estimate=None, cycle=None, dry_run=False,
    )
    plane.cmd_update(dict(CFG), args)
    assert "updated" in capsys.readouterr().out


# --- G5: a hostile X-RateLimit-Reset header must not crash the retry ----------


def _rate_limited_then_ok(reset_header, sleeps):
    """A urlopen stub: first call 429 with the given header, second call 200."""
    import email.message
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            headers = email.message.Message()
            if reset_header is not None:
                headers["X-RateLimit-Reset"] = reset_header
            raise urllib.error.HTTPError(req.full_url, 429, "throttled", headers, None)

        class _Resp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    return fake_urlopen


@pytest.mark.parametrize(
    "header", ["Mon, 17 Aug 2026 12:00:00 GMT", "30", "1787000000", None]
)
def test_g5_any_reset_header_shape_retries_instead_of_crashing(monkeypatch, header):
    monkeypatch.setattr(
        plane.urllib.request, "urlopen", _rate_limited_then_ok(header, [])
    )
    slept = []
    monkeypatch.setattr(plane.time, "sleep", slept.append)
    assert plane._request("GET", f"/projects/{OK}/issues/", dict(CFG)) == {"ok": True}
    assert len(slept) == 1 and 1 <= slept[0] <= 60


# --- G6: date-only cycle bounds must compare, not crash ------------------------


def test_g6_date_only_cycle_bounds_do_not_raise():
    from datetime import datetime, timezone

    cycle = {"start_date": "2026-08-01", "end_date": "2026-08-31"}
    noon = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    assert plane.cycle_is_live(cycle, noon) is True


# --- G7: an env file written with `export` still loads -------------------------


def test_g7_export_prefixed_env_file_lines_load(monkeypatch, tmp_path):
    f = tmp_path / "plane.env"
    f.write_text('export PLANE_API_TOKEN="tok"\nexport PLANE_WORKSPACE=acme\n')
    for var in ("PLANE_API_TOKEN", "PLANE_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PLANE_ENV_FILE", str(f))
    plane._load_env_file()
    assert plane.os.environ["PLANE_API_TOKEN"] == "tok"
    assert plane.os.environ["PLANE_WORKSPACE"] == "acme"


# --- G8: identifiers containing digits parse ------------------------------------


def test_g8_digit_bearing_identifier_resolves(monkeypatch):
    captured = {}

    def fake_resolve_project(cfg, ref):
        captured["ref"] = ref
        return {"id": OK, "identifier": "WEB3", "name": "Web3"}

    monkeypatch.setattr(plane, "resolve_project", fake_resolve_project)
    monkeypatch.setattr(
        plane, "_paginate", lambda *a, **k: iter([{"sequence_id": 12, "name": "hit"}])
    )
    issue = plane.resolve_issue(dict(CFG), "WEB3-12")
    assert captured["ref"] == "WEB3"
    assert issue["sequence_id"] == 12


def test_g8_separatorless_letters_only_ref_still_parses(monkeypatch):
    monkeypatch.setattr(
        plane, "resolve_project", lambda cfg, ref: {"id": OK, "identifier": "SPK"}
    )
    monkeypatch.setattr(
        plane, "_paginate", lambda *a, **k: iter([{"sequence_id": 12}])
    )
    assert plane.resolve_issue(dict(CFG), "SPK12")["sequence_id"] == 12


# --- G9: `check` must not print any part of the token ---------------------------


def test_g9_check_output_contains_no_token_material(monkeypatch, capsys):
    cfg = dict(CFG, token="plane_api_supersecretvalue", _projects=[
        {"id": OK, "identifier": "SPK", "name": "Mine", "_allowed": True},
    ])
    monkeypatch.setattr(plane, "list_projects", lambda c: cfg["_projects"])
    plane.cmd_check(cfg, None)
    out = capsys.readouterr().out
    for fragment in ("plane_api_s", "supersecret"):
        assert fragment not in out


# --- G10: a still-encoded path is refused, not waved through --------------------


def test_g10_quadruple_encoded_uuid_is_refused_not_treated_as_uuidless():
    encoded = "%25252530" + BAD[1:]  # "0" percent-encoded four times + rest
    with pytest.raises(plane.GuardError):
        guard("GET", f"/projects/{encoded}/issues/")


def test_g10_non_project_query_key_cannot_carry_a_blocked_uuid():
    with pytest.raises(plane.GuardError):
        guard("GET", f"/projects/{OK}/issues/?module={BAD}")
