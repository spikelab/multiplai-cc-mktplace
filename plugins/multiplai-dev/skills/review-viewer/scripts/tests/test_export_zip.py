"""GET /api/targets/<t>/export: the zip's exact contents, the gitleaks refusals, the temp directory.

Row S8 of the plan's stop-and-resume table: a build that dies part way leaves the review folder
as it was and its temporary directory removed (`test_a_build_that_dies_leaves_the_review_folder_untouched`).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest
from review_viewer import export_zip

FAKE_GITLEAKS = """#!/bin/sh
if [ "$1" = version ]; then echo 8.29.0; exit 0; fi
report=""
while [ $# -gt 0 ]; do
  if [ "$1" = --report-path ]; then report="$2"; fi
  shift
done
if [ "$FAKE_GITLEAKS_EXIT" = 1 ]; then
  printf '[{"File": "%s/transcripts/sess-1.jsonl", "StartLine": 3, "RuleID": "aws-access-token", "Secret": "REDACTED"}]' "$FAKE_STAGING" > "$report"
fi
exit "${FAKE_GITLEAKS_EXIT:-0}"
"""

EXPECTED = [
    "MANIFEST.txt",
    "diff.patch",
    "findings.json",
    "progress.log",
    "review-state.json",
    "rounds/01-aaaaaaaaaaaa/findings.json",
    "rounds/01-aaaaaaaaaaaa/progress.log",
    "transcripts/sess-1.jsonl",
    "transcripts/sess-1/subagents/agent-a.jsonl",
    "transcripts/sess-1/tool-results/t1.txt",
    "viewer/decisions.json",
    "viewer/inbox.jsonl",
    "viewer/outbox.jsonl",
]


@pytest.fixture
def review(findings_path, tmp_path, monkeypatch) -> Path:
    """A review folder with everything the zip takes and everything it must leave out."""
    rd = findings_path.parent
    (rd / "diff.patch").write_text("diff --git a/a.py b/a.py\n")
    (rd / "progress.log").write_text("STARTED multiplai-dev:review 0.114.0\n")
    (rd / "review-state.json").write_text(
        json.dumps(
            {"runs": [{"producer": "p", "session_id": "sess-1", "started_at": "t"}]}
        )
    )
    (rd / ".DS_Store").write_bytes(b"\0")
    (rd / ".gitleaksignore").write_text("*\n")
    for name, text in {
        "tree/a.py": "x = 1\n",
        "paths-cache/k.json": "{}",
        "rounds/01-aaaaaaaaaaaa/findings.json": "{}",
        "rounds/01-aaaaaaaaaaaa/progress.log": "old\n",
        "rounds/01-aaaaaaaaaaaa/.DS_Store": "",
        "rounds/.partial-02-bbbbbbbbbbbb/findings.json": "{}",
        "viewer/decisions.json": "{}",
        "viewer/inbox.jsonl": "",
        "viewer/outbox.jsonl": "",
        "viewer/server.json": "{}",
        "viewer/open.html": "token here",
        "viewer/target.json": "{}",
        "viewer/viewed.json": "{}",
    }.items():
        (rd / name).parent.mkdir(parents=True, exist_ok=True)
        (rd / name).write_text(text)
    # Transcripts: sess-1 from the state's runs, sess-2 from the ledger (not on disk), sess-3 another
    # folder's, and a line with an id that must never reach a glob.
    cfg = tmp_path / "claude-config"
    proj = cfg / "projects" / "-some-project"
    (proj / "sess-1" / "subagents").mkdir(parents=True)
    (proj / "sess-1" / "tool-results").mkdir()
    (proj / "sess-1" / "other").mkdir()
    (proj / "sess-1.jsonl").write_text('{"type": "user"}\n')
    (proj / "sess-1" / "subagents" / "agent-a.jsonl").write_text("{}\n")
    (proj / "sess-1" / "tool-results" / "t1.txt").write_text("out\n")
    (proj / "sess-1" / "other" / "x.txt").write_text("not taken\n")
    (proj / "sess-3.jsonl").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"event": "started", "target_dir": str(rd), "session_id": "sess-2"},
                {
                    "event": "started",
                    "target_dir": str(tmp_path / "other"),
                    "session_id": "sess-3",
                },
                {
                    "event": "started",
                    "target_dir": str(rd),
                    "session_id": "../../etc/x",
                },
            )
        )
        + "\nnot json\n"
    )
    monkeypatch.setattr(export_zip, "default_ledger", lambda: ledger)
    return rd


@pytest.fixture
def temp_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def fake_gitleaks(tmp_path, monkeypatch, temp_root):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "gitleaks"
    exe.write_text(FAKE_GITLEAKS)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin:/bin")
    return exe


def emptied(root: Path, wait_s: float = 5.0) -> bool:
    """The server deletes its temp directory after the last byte is sent, so the client can read
    the response first; give it a moment."""
    import time

    end = time.monotonic() + wait_s
    while list(root.iterdir()) and time.monotonic() < end:
        time.sleep(0.02)
    return list(root.iterdir()) == []


def snapshot(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name not in ("server.token", "open.html", "server.json")
    }


def test_the_zip_holds_exactly_the_review_its_rounds_and_its_sessions(
    review, start_live, fake_gitleaks, temp_root
):
    live = start_live(path=review / "findings.json")
    status, body = live.request("GET", f"/api/targets/{live.slug}/export")
    assert status == 200, body
    names = sorted(zipfile.ZipFile(io.BytesIO(body)).namelist())
    assert names == EXPECTED
    manifest = zipfile.ZipFile(io.BytesIO(body)).read("MANIFEST.txt").decode()
    assert "scanned by: gitleaks 8.29.0" in manifest
    assert "sessions whose transcript was not found:\nsess-2" in manifest
    assert "sess-3" not in manifest and "etc" not in manifest
    assert emptied(temp_root)


def test_the_response_names_the_zip_and_is_offered_on_the_target(
    review, start_live, fake_gitleaks
):
    live = start_live(path=review / "findings.json")
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{live.port}/api/targets/{live.slug}/export",
        headers={"X-Review-Token": live.token},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        head = live.viewer.targets[live.slug].findings.target.head_sha
        assert resp.headers["Content-Type"] == "application/zip"
        assert resp.headers["Content-Disposition"] == (
            f'attachment; filename="{export_zip.zip_name(live.slug, head)}"'
        )
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert detail["export"] is True


def test_a_gitleaks_hit_refuses_with_where_and_never_what(
    review, start_live, fake_gitleaks, temp_root, monkeypatch
):
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", "1")
    live = start_live(path=review / "findings.json")
    # The fake reports a path under the staging directory, which only exists during the call.
    real_build = export_zip.build

    def build(review_dir, work, **kw):
        monkeypatch.setenv("FAKE_STAGING", str(work / "staging"))
        return real_build(review_dir, work, **kw)

    monkeypatch.setattr(export_zip, "build", build)
    status, body = live.request("GET", f"/api/targets/{live.slug}/export")
    assert status == 409
    assert body["leaks"] == [
        {"file": "transcripts/sess-1.jsonl", "line": 3, "rule": "aws-access-token"}
    ]
    assert "Secret" not in json.dumps(body) and "REDACTED" not in json.dumps(body)
    assert emptied(temp_root)


def test_without_gitleaks_the_export_is_refused_with_how_to_install_it(
    review, start_live, tmp_path, monkeypatch, temp_root
):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    live = start_live(path=review / "findings.json")
    status, body = live.request("GET", f"/api/targets/{live.slug}/export")
    assert status == 503 and body == {
        "error": "install gitleaks (brew install gitleaks)"
    }
    assert emptied(temp_root)


def test_a_build_that_dies_leaves_the_review_folder_untouched(
    review, start_live, fake_gitleaks, temp_root, monkeypatch
):
    """S8: killed during build or scan → nothing in the review folder changes."""
    live = start_live(path=review / "findings.json")
    before = snapshot(review)

    def dies(*a, **kw):
        raise RuntimeError("killed")

    monkeypatch.setattr(export_zip, "scan", dies)
    status, _ = live.request("GET", f"/api/targets/{live.slug}/export")
    assert status == 500
    assert snapshot(review) == before
    assert emptied(temp_root)


def test_the_export_needs_the_token(review, start_live, fake_gitleaks):
    live = start_live(path=review / "findings.json")
    status, _ = live.request("GET", f"/api/targets/{live.slug}/export", token="wrong")
    assert status == 401


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_the_real_gitleaks_refuses_a_planted_key(
    review, start_live, temp_root, monkeypatch
):
    # Built at run time so this file itself holds no key a scanner would flag.
    key = "AKIA" + "QYLPMN5HHHFPZAM2"
    transcript = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-some-project"
        / "sess-1.jsonl"
    )
    transcript.write_text(
        transcript.read_text()
        + json.dumps({"text": f"aws_access_key_id = {key}"})
        + "\n"
    )
    live = start_live(path=review / "findings.json")
    status, body = live.request("GET", f"/api/targets/{live.slug}/export")
    assert status == 409, body
    assert any(h["file"] == "transcripts/sess-1.jsonl" for h in body["leaks"])
    assert key not in json.dumps(body)
    assert emptied(temp_root)
