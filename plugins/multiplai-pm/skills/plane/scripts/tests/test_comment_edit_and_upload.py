"""Tests for editing a comment and uploading an attachment.

Two behaviours added together because they share one property worth pinning
down: both name a *specific* remote object — a comment to overwrite, a
presigned URL to POST a file to — and getting either wrong is silent. So the
tests here are mostly about refusals: an ambiguous comment id, a host that is
not Plane's, a file over the cap, a credentials response whose shape is not the
one the code expects.

The upload path is also the second network call in the script that does not go
through `_request`. `fetch_asset`'s protections are tested in
test_adversarial_round2.py; the ones below are the same five, asserted against
`push_asset`.
"""

from __future__ import annotations

import io
import urllib.error
from contextlib import redirect_stdout

import pytest

import plane
from conftest import ALLOWED, CFG, OK, Args

ISSUE = "792a89b1-37db-47e1-a947-6fb1b79198d6"
C1 = "aaaa1111-2222-3333-4444-555566667777"
C2 = "aaaa1111-9999-8888-7777-666655554444"
C3 = "bbbb2222-3333-4444-5555-666677778888"
ATT = "cccc3333-4444-5555-6666-777788889999"

COMMENTS = [
    {"id": C1, "comment_html": "<p>first</p>", "created_at": "2026-08-18T10:00:00Z",
     "actor_detail": {"display_name": "alice"}},
    {"id": C2, "comment_html": "<p>second</p>", "created_at": "2026-08-18T11:00:00Z",
     "actor_detail": {"display_name": "bob"}},
    {"id": C3, "comment_html": "<p>third</p>", "created_at": "2026-08-18T12:00:00Z",
     "actor_detail": {"display_name": "alice"}},
]


class Calls:
    """Stands in for _request: records every call, answers from a route map."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple] = []

    def __call__(self, method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        bare = path.split("?")[0]
        self.calls.append((method, bare, body, dry_run))
        payload = self.routes.get(bare)
        return payload() if callable(payload) else payload

    def paths(self):
        return [(c[0], c[1]) for c in self.calls]


def issue_stub(monkeypatch):
    monkeypatch.setattr(
        plane, "resolve_issue",
        lambda cfg, ref, project=None: {"id": ISSUE, "project": OK, "name": "A ticket"},
    )


# --- comments now print the id, which is the only way to get one -------------


def test_comments_text_output_carries_the_comment_id(monkeypatch):
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter(COMMENTS))
    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_comments(CFG, Args(ref="SPK-1", project=None))
    out = buf.getvalue()
    for cid in (C1, C2, C3):
        assert f"[{cid}]" in out, out
    assert "first" in out and "alice" in out


# --- comment-edit ------------------------------------------------------------


def test_comment_edit_patches_the_comment_under_its_project_and_issue(monkeypatch):
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter(COMMENTS))
    rec = Calls()
    monkeypatch.setattr(plane, "_request", rec)

    with redirect_stdout(io.StringIO()):
        plane.cmd_comment_edit(
            CFG, Args(ref="SPK-1", project=None, comment_id=C2, text="**fixed**",
                      body=None, body_file=None, dry_run=False),
        )

    assert rec.paths() == [
        ("PATCH", f"/projects/{OK}/issues/{ISSUE}/comments/{C2}/")
    ]
    body = rec.calls[0][2]
    assert body == {"comment_html": "<p><strong>fixed</strong></p>"}
    # The path the guard sees must be the one that was sent.
    plane._guard("PATCH", rec.calls[0][1], ALLOWED)


def test_comment_edit_accepts_a_unique_id_prefix(monkeypatch):
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter(COMMENTS))
    rec = Calls()
    monkeypatch.setattr(plane, "_request", rec)

    with redirect_stdout(io.StringIO()):
        plane.cmd_comment_edit(
            CFG, Args(ref="SPK-1", project=None, comment_id="BBBB2222", text="x",
                      body=None, body_file=None, dry_run=False),
        )
    assert rec.calls[0][1].endswith(f"/comments/{C3}/")


def test_comment_edit_refuses_an_ambiguous_prefix(monkeypatch):
    """C1 and C2 share the first eight characters. Picking one silently
    overwrites text nobody kept a copy of."""
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter(COMMENTS))
    rec = Calls()
    monkeypatch.setattr(plane, "_request", rec)

    with pytest.raises(plane.PlaneError, match="2 comments match"):
        plane.cmd_comment_edit(
            CFG, Args(ref="SPK-1", project=None, comment_id="aaaa1111", text="x",
                      body=None, body_file=None, dry_run=False),
        )
    assert rec.calls == [], "an ambiguous id must not send a write"


@pytest.mark.parametrize(
    "ref,message",
    [
        ("aaa", "too short"),
        ("", "too short"),
        ("deadbeef", "no comment"),
        ("00000000-0000-0000-0000-000000000000", "no comment"),
    ],
)
def test_resolve_comment_id_refuses_rather_than_guessing(ref, message):
    with pytest.raises(plane.PlaneError, match=message):
        plane._resolve_comment_id(COMMENTS, ref)


def test_comment_edit_needs_text():
    with pytest.raises(plane.PlaneError, match="no comment text"):
        plane.cmd_comment_edit(
            CFG, Args(ref="SPK-1", project=None, comment_id=C1, text=None,
                      body=None, body_file=None, dry_run=False),
        )


def test_comment_edit_dry_run_opens_no_socket(monkeypatch):
    """Through the real `_request`, so the dry-run branch itself is what runs."""
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_paginate", lambda *a, **k: iter(COMMENTS))

    def explode(*a, **k):
        raise AssertionError("--dry-run reached the network")

    monkeypatch.setattr(plane.urllib.request, "urlopen", explode)

    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_comment_edit(
            CFG, Args(ref="SPK-1", project=None, comment_id=C3, text="x",
                      body=None, body_file=None, dry_run=True),
        )
    out = buf.getvalue()
    assert "[dry-run] PATCH" in out and f"/comments/{C3}/" in out
    assert "edited comment" not in out


# --- push_asset: the same five protections as fetch_asset --------------------


class _Opener:
    """Captures the request instead of sending it."""

    def __init__(self, log, fail=True):
        self.log, self.fail = log, fail

    def open(self, req, timeout=None):  # noqa: ARG002
        self.log.append(req)
        if self.fail:
            raise urllib.error.URLError("stopped before the socket")
        return _Resp()


class _Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.amazonaws.com@evil.com/x",
        "https://api.plane.so@evil.com/x",
        "https://notamazonaws.com/x",
        "https://x.amazonaws.com.evil.net/x",
        "http://api.plane.so/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "//api.plane.so/x",
    ],
)
def test_push_asset_refuses_a_host_plane_does_not_upload_to(url):
    with pytest.raises(plane.PlaneError, match="not a host"):
        plane.push_asset(url, {"key": "k"}, "f.txt", "text/plain", b"x",
                         "https://api.plane.so")


def test_push_asset_sends_no_plane_credential(monkeypatch):
    """Same trap as the download: urllib copies custom headers across a
    cross-host redirect, so an X-API-Key here would reach amazonaws.com."""
    seen = []
    monkeypatch.setattr(plane.urllib.request, "build_opener", lambda *a: _Opener(seen))
    with pytest.raises(plane.PlaneError):
        plane.push_asset("https://uploads.s3.amazonaws.com/", {"key": "k"},
                         "f.txt", "text/plain", b"x", "https://api.plane.so")
    keys = {k.lower() for k in dict(seen[0].header_items())}
    assert keys == {"user-agent", "content-type"}, keys


def test_push_asset_refuses_redirects(monkeypatch):
    built = []
    monkeypatch.setattr(
        plane.urllib.request, "build_opener",
        lambda *handlers: built.append(handlers) or _Opener([]),
    )
    with pytest.raises(plane.PlaneError):
        plane.push_asset("https://uploads.s3.amazonaws.com/", {}, "f.txt",
                         "text/plain", b"x", "https://api.plane.so")
    assert plane._NoRedirect in built[0]
    assert plane._NoRedirect().redirect_request(None, None, 302, "m", {}, "u") is None


def test_push_asset_refuses_a_blob_over_the_cap(monkeypatch):
    monkeypatch.setattr(plane, "_ASSET_MAX_BYTES", 4)
    with pytest.raises(plane.PlaneError, match="refusing to upload more than"):
        plane.push_asset("https://uploads.s3.amazonaws.com/", {}, "f.txt",
                         "text/plain", b"12345", "https://api.plane.so")


def test_multipart_puts_the_file_part_last_and_keeps_the_bytes_intact(monkeypatch):
    """S3 ignores every field written after the file part, so `file` last is a
    requirement of the presigned POST, not a style choice."""
    seen = []
    monkeypatch.setattr(plane.urllib.request, "build_opener", lambda *a: _Opener(seen))
    fields = {"key": "uploads/1", "policy": "POLICY", "x-amz-signature": "SIG"}
    blob = b"\x00\x01binary\xff payload"
    with pytest.raises(plane.PlaneError):
        plane.push_asset("https://uploads.s3.amazonaws.com/", fields, 'a "b".txt',
                         "text/plain", blob, "https://api.plane.so")

    req = seen[0]
    body = req.data
    boundary = req.get_header("Content-type").split("boundary=")[1]
    assert boundary.encode() not in blob

    parts = body.split(f"--{boundary}".encode())
    # First is empty (leading delimiter), last is the closing "--\r\n".
    named = [p for p in parts if b"Content-Disposition" in p]
    assert len(named) == len(fields) + 1
    assert b'name="file"' in named[-1], "the file part must be last"
    assert [b'name="key"' in named[0], b'name="policy"' in named[1]] == [True, True]
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
    # The binary is passed through untouched, and the filename is sanitised —
    # a quote in it would otherwise close the Content-Disposition header early.
    assert blob in body
    assert b'filename="a__b_.txt"' in named[-1]
    assert b'"' not in named[-1].split(b"\r\n")[1].replace(b'name="file"', b"").replace(
        b'filename="a__b_.txt"', b""
    )
    assert b"Content-Type: text/plain" in named[-1]


# --- the credentials response, whose shape is the risky part -----------------


def test_upload_credentials_reads_the_documented_shape():
    url, fields, aid = plane._upload_credentials(
        {"upload_data": {"url": "https://u/", "fields": {"key": "k", "size": 12}},
         "asset_id": ATT}
    )
    assert (url, aid) == ("https://u/", ATT)
    assert fields == {"key": "k", "size": "12"}, "every field is sent as text"


def test_upload_credentials_reads_a_top_level_form_and_a_nested_id():
    url, _fields, aid = plane._upload_credentials(
        {"url": "https://u/", "fields": {}, "attachment": {"id": ATT}}
    )
    assert (url, aid) == ("https://u/", ATT)


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"detail": "nope"}, "no presigned upload form"),
        ({"upload_data": {"url": "https://u/"}}, "no presigned upload form"),
        ([], "not an object"),
        ({"upload_data": {"url": "https://u/", "fields": {}}}, "no attachment id"),
        ({"url": "https://u/", "fields": {}, "asset_id": "not-a-uuid"},
         "no attachment id"),
    ],
)
def test_upload_credentials_refuses_an_unrecognised_shape(payload, message):
    with pytest.raises(plane.PlaneError, match=message):
        plane._upload_credentials(payload)


def test_upload_credentials_error_names_keys_but_never_values():
    """`fields` holds the policy and the signature; an error message is the
    easiest place to leak them."""
    with pytest.raises(plane.PlaneError) as exc:
        plane._upload_credentials({"detail": "x", "signature": "SECRET-SIG"})
    assert "signature" in str(exc.value) and "SECRET-SIG" not in str(exc.value)


# --- the three-step upload, end to end ---------------------------------------


def credentials_route(project=OK, issue=ISSUE):
    return {
        f"/projects/{project}/issues/{issue}/issue-attachments/": {
            "upload_data": {"url": "https://uploads.s3.amazonaws.com/",
                            "fields": {"key": "uploads/x"}},
            "asset_id": ATT,
        }
    }


def test_upload_runs_credentials_then_s3_then_completion(monkeypatch, tmp_path):
    issue_stub(monkeypatch)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)
    pushed = []
    monkeypatch.setattr(
        plane, "push_asset",
        lambda url, fields, name, mime, blob, base: pushed.append((url, name, mime, blob)),
    )
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello")

    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=False),
        )

    endpoint = f"/projects/{OK}/issues/{ISSUE}/issue-attachments/"
    assert rec.paths() == [("POST", endpoint), ("PATCH", f"{endpoint}{ATT}/")]
    assert rec.calls[0][2] == {"name": "report.txt", "size": 5, "type": "text/plain"}
    assert rec.calls[1][2] == {"is_uploaded": True}
    assert pushed == [("https://uploads.s3.amazonaws.com/", "report.txt",
                       "text/plain", b"hello")]
    assert "uploaded report.txt  (5 bytes)  attachment " + ATT in buf.getvalue()
    for method, path in rec.paths():
        plane._guard(method, path, ALLOWED)


def test_upload_of_an_unknown_type_falls_back_to_octet_stream(monkeypatch, tmp_path):
    issue_stub(monkeypatch)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)
    monkeypatch.setattr(plane, "push_asset", lambda *a, **k: None)
    f = tmp_path / "thing.zzzz"
    f.write_bytes(b"\x00\x01")
    with redirect_stdout(io.StringIO()):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=False),
        )
    assert rec.calls[0][2]["type"] == "application/octet-stream"


def test_upload_says_so_when_the_record_exists_but_the_file_did_not_land(
    monkeypatch, tmp_path
):
    issue_stub(monkeypatch)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)

    def boom(*a, **k):
        raise plane.PlaneError("asset upload -> 403 SignatureDoesNotMatch")

    monkeypatch.setattr(plane, "push_asset", boom)
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello")

    with pytest.raises(plane.PlaneError, match="half-uploaded attachment") as exc:
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=False),
        )
    # The id is the thing the user has to go and delete, so it must be in the
    # message — and the completion PATCH must not have run.
    assert ATT in str(exc.value) and "SignatureDoesNotMatch" in str(exc.value)
    assert [m for m, _ in rec.paths()] == ["POST"]


def test_upload_says_so_when_only_the_completion_patch_fails(monkeypatch, tmp_path):
    issue_stub(monkeypatch)

    def request(method, path, cfg, **kw):
        if method == "PATCH":
            raise plane.PlaneError("PATCH -> 500")
        return credentials_route()[path.split("?")[0]]

    monkeypatch.setattr(plane, "_request", request)
    monkeypatch.setattr(plane, "push_asset", lambda *a, **k: None)
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello")

    with pytest.raises(plane.PlaneError, match="will not show in"):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=False),
        )


def test_upload_refuses_a_file_over_the_cap_before_creating_a_record(
    monkeypatch, tmp_path
):
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_ASSET_MAX_BYTES", 8)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)
    f = tmp_path / "big.bin"
    f.write_bytes(b"0123456789")

    with pytest.raises(plane.PlaneError, match="larger than 8 bytes"):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=False),
        )
    assert rec.calls == [], "an oversize file must not create an attachment record"


def test_upload_refuses_a_missing_file(monkeypatch, tmp_path):
    issue_stub(monkeypatch)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)
    with pytest.raises(plane.PlaneError, match="no file to upload"):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None,
                      upload=[str(tmp_path / "nope.txt")], dry_run=False),
        )
    assert rec.calls == []


def test_upload_dry_run_stops_after_the_credentials_step(monkeypatch, tmp_path):
    """Through the real `_request` and a poisoned opener: a dry run must not
    ask Plane for a presigned form, and must not invent one either."""
    issue_stub(monkeypatch)

    def explode(*a, **k):
        raise AssertionError("--dry-run reached the network")

    monkeypatch.setattr(plane.urllib.request, "urlopen", explode)
    monkeypatch.setattr(plane.urllib.request, "build_opener", explode)
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello")

    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=[str(f)],
                      dry_run=True),
        )
    out = buf.getvalue()
    assert "[dry-run] POST" in out and "issue-attachments/" in out
    assert '"name": "report.txt"' in out and '"size": 5' in out
    assert "skipped" in out
    assert "uploaded" not in out


def test_upload_and_download_are_mutually_exclusive_in_the_parser():
    with pytest.raises(SystemExit):
        plane.build_parser().parse_args(
            ["attachments", "SPK-1", "--upload", "a.txt", "--download", "/tmp/x"]
        )


def test_upload_and_download_are_refused_together_before_any_request(monkeypatch):
    """The parser catches the CLI case; this catches a programmatic one, and
    does it before the issue lookup so nothing is spent on a contradiction."""
    def explode(*a, **k):
        raise AssertionError("resolved an issue for a request that cannot run")

    monkeypatch.setattr(plane, "resolve_issue", explode)
    with pytest.raises(plane.PlaneError, match="opposite directions"):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download="/tmp/x",
                      upload=["a.txt"], dry_run=False),
        )


# --- what the live round-trip found: the record's asset field is not an id ---


# Verbatim from Plane Cloud, 2026-08-18, for a file this tool uploaded to
# SPK-37. `asset` is the storage key; `id` is what /assets/ answers to.
CLOUD_RECORD = {
    "id": "d269e4b7-d480-4dcd-ab54-a95f37193ac9",
    "attributes": {"name": "marker.png", "size": 118, "type": "image/png"},
    "asset": "a2667d8d-0a51-46c4-ace0-da990f0f0a07/4ffc08aa-marker.png",
    "is_uploaded": True,
}


def test_a_cloud_attachment_record_lists_a_fetchable_asset_id(monkeypatch):
    """`--download` skipped every real attachment before this: it read `asset`,
    found a storage key rather than a UUID, and never reached the record id."""
    issue_stub(monkeypatch)
    monkeypatch.setattr(plane, "_request", lambda *a, **k: {"description_html": ""})
    monkeypatch.setattr(
        plane, "_paginate", lambda path, *a, **k: iter([CLOUD_RECORD])
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=None,
                      dry_run=False, json=True),
        )
    row = plane.json.loads(buf.getvalue())[0]
    assert row["asset"] == CLOUD_RECORD["id"]
    assert row["name"] == "marker.png"


def test_an_asset_uuid_in_the_asset_field_is_still_preferred(monkeypatch):
    """Self-hosted builds have put the asset UUID there; that spelling still
    wins over the record id."""
    issue_stub(monkeypatch)
    asset_uuid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(plane, "_request", lambda *a, **k: {"description_html": ""})
    monkeypatch.setattr(
        plane, "_paginate",
        lambda path, *a, **k: iter([dict(CLOUD_RECORD, asset=asset_uuid)]),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None, upload=None,
                      dry_run=False, json=True),
        )
    assert plane.json.loads(buf.getvalue())[0]["asset"] == asset_uuid


def test_several_uploads_run_in_order(monkeypatch, tmp_path):
    issue_stub(monkeypatch)
    rec = Calls(credentials_route())
    monkeypatch.setattr(plane, "_request", rec)
    names = []
    monkeypatch.setattr(
        plane, "push_asset",
        lambda url, fields, name, mime, blob, base: names.append(name),
    )
    for n in ("one.txt", "two.png"):
        (tmp_path / n).write_bytes(b"x")

    with redirect_stdout(io.StringIO()):
        plane.cmd_attachments(
            CFG, Args(ref="SPK-1", project=None, download=None,
                      upload=[str(tmp_path / "one.txt"), str(tmp_path / "two.png")],
                      dry_run=False),
        )
    assert names == ["one.txt", "two.png"]
    assert [m for m, _ in rec.paths()] == ["POST", "PATCH", "POST", "PATCH"]
