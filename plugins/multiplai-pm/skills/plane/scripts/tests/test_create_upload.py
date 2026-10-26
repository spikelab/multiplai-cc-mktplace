"""Tests for `create --upload`: create a ticket and attach files in one command.

Every file is checked before the issue POST, so a bad path never leaves a card
behind. After the create, files upload in order; the first failure stops the
run, and the error names the new ref, what went up, what failed, what was not
tried, and the `attachments` command that attaches the rest (stop rows
S10-S12 of the plan). The create is never retried.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import plane
import pytest
from conftest import CFG, OK, Args

PROJECT = {"id": OK, "name": "Mine", "identifier": "SPK"}
CREATED = {"id": "new-issue", "sequence_id": 12, "name": "Fix it"}


def stub(monkeypatch, *, created=CREATED, fail_on=None):
    calls: list[tuple] = []
    uploads: list[tuple] = []

    def request(method, path, cfg, *, params=None, body=None, dry_run=False, **kw):
        calls.append((method, path, dry_run))
        if dry_run:
            print(f"[dry-run] {method} {path}")
            return None
        return dict(created) if isinstance(created, dict) else created

    def upload(cfg, project_id, issue_id, path, *, dry_run=False):
        plane._check_upload(path)
        uploads.append((issue_id, path))
        if fail_on and path.endswith(fail_on):
            raise plane.PlaneError(f"{path}: Invalid file type")
        print(f"uploaded {path}")
        return f"att-{len(uploads)}"

    monkeypatch.setattr(plane, "resolve_project", lambda cfg, ref=None: dict(PROJECT))
    monkeypatch.setattr(plane, "_request", request)
    monkeypatch.setattr(plane, "upload_attachment", upload)
    return calls, uploads


def files(tmp_path, *names):
    paths = []
    for name in names:
        f = tmp_path / name
        f.write_bytes(b"x")
        paths.append(str(f))
    return paths


def create_args(upload, **kw):
    base = {
        "project": None,
        "title": "Fix it",
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
        "upload": upload,
        "dry_run": False,
    }
    base.update(kw)
    return Args(**base)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plane.cmd_create(CFG, args)
    return code, out.getvalue(), err.getvalue()


def test_s10_a_missing_file_raises_before_any_request(monkeypatch, tmp_path):
    """S10: a file check fails; nothing reaches Plane."""
    calls, _ = stub(monkeypatch)
    good = files(tmp_path, "a.png")
    with pytest.raises(plane.PlaneError, match="no file to upload"):
        plane.cmd_create(CFG, create_args(good + [str(tmp_path / "gone.pdf")]))
    assert calls == []


def test_s10_a_file_over_the_cap_raises_before_any_request(monkeypatch, tmp_path):
    calls, _ = stub(monkeypatch)
    monkeypatch.setattr(plane, "_ASSET_MAX_BYTES", 3)
    big = tmp_path / "big.bin"
    big.write_bytes(b"xxxx")
    with pytest.raises(plane.PlaneError, match="larger than 3 bytes"):
        plane.cmd_create(CFG, create_args([str(big)]))
    assert calls == []


def test_a_successful_run_posts_then_uploads_each_file_in_order(monkeypatch, tmp_path):
    calls, uploads = stub(monkeypatch)
    paths = files(tmp_path, "a.png", "b.pdf")
    code, out, _ = run(create_args(paths))
    assert code is None
    assert calls == [("POST", f"/projects/{OK}/issues/", False)]
    assert uploads == [("new-issue", paths[0]), ("new-issue", paths[1])]
    assert "created SPK-12" in out


def test_s12_a_failure_stops_and_prints_the_command_for_the_rest(monkeypatch, tmp_path):
    """S12: the second of three uploads fails. The third is not tried, nothing
    is retried, and the error lists exactly the files still to attach."""
    calls, uploads = stub(monkeypatch, fail_on="b.pdf")
    a, b, c = files(tmp_path, "a.png", "b.pdf", "c.txt")
    with pytest.raises(plane.PlaneError) as exc:
        run(create_args([a, b, c]))
    text = str(exc.value)
    assert [u[1] for u in uploads] == [a, b]
    assert [m for m, *_ in calls].count("POST") == 1
    assert "created SPK-12 [new-issue]" in text
    assert f"uploaded : {a}" in text
    assert f"failed   : {b} — {b}: Invalid file type" in text
    assert f"not tried: {c}" in text
    assert text.splitlines()[-1] == (
        f"  plane.py attachments SPK-12 --upload {b} --upload {c}"
    )


def test_main_exits_1_on_a_partial_upload(monkeypatch, tmp_path):
    stub(monkeypatch, fail_on="b.pdf")
    a, b = files(tmp_path, "a.png", "b.pdf")
    monkeypatch.setattr(plane, "_cfg", lambda: dict(CFG))
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        code = plane.main(["create", "--title", "x", "--upload", a, "--upload", b])
    assert code == 1
    assert "attachments SPK-12 --upload" in err.getvalue()


def test_the_resume_command_quotes_paths_with_spaces(monkeypatch, tmp_path):
    stub(monkeypatch, fail_on="my file.pdf")
    (spaced,) = files(tmp_path, "my file.pdf")
    with pytest.raises(plane.PlaneError) as exc:
        run(create_args([spaced]))
    assert f"--upload '{spaced}'" in str(exc.value)


def test_s11_a_create_with_no_body_uploads_nothing(monkeypatch, tmp_path):
    """S11: the issue exists but there is no id to attach to."""
    _, uploads = stub(monkeypatch, created=None)
    paths = files(tmp_path, "a.png")
    code, _, err = run(create_args(paths))
    assert code == 1
    assert uploads == []
    assert "not attached" in err and "do not re-run `create`" in err


def test_dry_run_sends_nothing_and_previews_issue_and_each_file(monkeypatch, tmp_path):
    calls, uploads = stub(monkeypatch)
    paths = files(tmp_path, "a.png", "b.pdf")
    run(create_args(paths, dry_run=True))
    assert all(dry for *_, dry in calls)
    assert uploads == [("<new-issue-id>", paths[0]), ("<new-issue-id>", paths[1])]


def test_json_carries_the_attachment_lists(monkeypatch, tmp_path):
    stub(monkeypatch, fail_on="b.pdf")
    a, b, c = files(tmp_path, "a.png", "b.pdf", "c.txt")
    out = io.StringIO()
    with (
        redirect_stdout(out),
        redirect_stderr(io.StringIO()),
        pytest.raises(plane.PlaneError),
    ):
        plane.cmd_create(CFG, create_args([a, b, c], json=True))
    got = json.loads(out.getvalue())
    assert got["ref"] == "SPK-12"
    assert got["attachments"] == [{"file": a, "attachment_id": "att-1"}]
    assert got["failed"][0]["file"] == b
    assert got["not_tried"] == [c]


def test_update_does_not_take_upload():
    with pytest.raises(SystemExit):
        plane.build_parser().parse_args(["update", "SPK-1", "--upload", "a.png"])
    ok = plane.build_parser().parse_args(
        ["create", "--title", "x", "--upload", "a", "--upload", "b"]
    )
    assert ok.upload == ["a", "b"]
