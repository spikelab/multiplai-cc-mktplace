"""The CLI: serve in a subprocess, invite, reply, pending, record decided."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from helpers import git
from pipeline_viewer.__main__ import main
from viewer_kit.mailbox import Mailbox

SCRIPTS = Path(__file__).resolve().parents[1]


def _start(fx, *extra):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline_viewer", "serve", "--repo", str(fx["repo"]),
         "--box", str(fx["box"]), "--worktrees-root", str(fx["wt"]), "--port", "18900", *extra],
        cwd=SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )  # fmt: skip
    lines = []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip("\n"))
        if line.startswith("pending:"):
            return proc, lines
    proc.kill()
    raise AssertionError(f"serve did not start: {lines} {proc.stderr.read()}")


def _invite(fx, capsys, name, email, role, slack=None, url=None):
    args = [
        "invite",
        "--box",
        str(fx["box"]),
        "--name",
        name,
        "--email",
        email,
        "--role",
        role,
    ]
    if slack:
        args += ["--slack-id", slack]
    if url:
        args += ["--url", url]
    assert main(args) == 0
    out = capsys.readouterr().out.strip()
    path = Path(out.removeprefix("invite: "))
    return out, path


def test_serve_invite_and_curl(fx, capsys):
    proc, lines = _start(fx)
    try:
        url = next(line.split()[1] for line in lines if line.startswith("url:"))
        assert any(line.startswith("monitor: Monitor(command=") for line in lines)
        record = json.loads((fx["box"] / "server.json").read_text())
        assert record["pid"] == proc.pid
        out, path = _invite(fx, capsys, "Pat Planner", "pat@example.org", "pm", "U0PAT")
        assert out == f"invite: {path}" and "?t=" not in out
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        link = path.read_text().strip()
        assert link.startswith(url.rstrip("/") + "/?t=")
        token = link.split("?t=", 1)[1]

        def call(route, body=None):
            req = urllib.request.Request(
                url.rstrip("/") + route,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Review-Token": token, "Content-Type": "application/json"},
                method="POST" if body is not None else "GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        status, body = call("/api/changes")
        assert status == 200
        assert {c["name"]: c["waiting_on"] for c in body["changes"]}["export-csv"] == [
            "Pat Planner"
        ]
        status, body = call("/api/approve", {"name": "export-csv", "stop": 1})
        assert status == 200, body
        assert git(fx["export-csv"], "log", "-1", "--format=%an") == "Pat Planner"
        assert call("/api/doc?name=export-csv&path=../../etc/passwd")[0] == 403

        # S2: a second serve while this one lives refuses.
        res = subprocess.run(
            [sys.executable, "-m", "pipeline_viewer", "serve", "--repo", str(fx["repo"]),
             "--box", str(fx["box"]), "--port", "18900"],
            check=False, cwd=SCRIPTS, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert res.returncode == 3
        assert (
            res.stdout.strip()
            == f"pipeline-viewer already serving {record['url']} (pid {proc.pid})"
        )
        assert token not in res.stdout + res.stderr + "\n".join(lines)
    finally:
        proc.terminate()
        proc.wait(timeout=60)
    assert not (fx["box"] / "server.json").exists()
    for f in fx["box"].glob("*.log"):
        assert token not in f.read_text()


def test_invite_again_revokes_the_old_link(fx, capsys):
    _, path = _invite(
        fx, capsys, "Pat Planner", "pat@example.org", "pm", url="http://h:1"
    )
    first = path.read_text()
    assert main(["invite", "--box", str(fx["box"]), "--url", "http://h:1",
                 "--name", "Pat Planner", "--email", "pat@example.org", "--role", "pm"]) == 0  # fmt: skip
    capsys.readouterr()
    people = json.loads((fx["box"] / "people.json").read_text())
    assert first.split("?t=")[1].strip() not in people
    assert sum(1 for p in people.values() if p["name"] == "Pat Planner") == 1


def test_reply_and_pending(fx, capsys, tmp_path):
    from pipeline_viewer.models import InboxRow

    box = Mailbox(fx["box"])
    box.append_inbox(
        InboxRow(id="q-1", ts="t", kind="ask", change="export-csv", text="?")
    )
    assert main(["pending", "--box", str(fx["box"])]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "q-1"
    answer = tmp_path / "a.md"
    answer.write_text("Because.\n")
    assert (
        main(
            [
                "reply",
                "--box",
                str(fx["box"]),
                "--to",
                "q-1",
                "--file",
                str(answer),
                "--commit",
                "abc1234",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert box.read_outbox(0)[0][0]["commit"] == "abc1234"
    assert main(["pending", "--box", str(fx["box"])]) == 0
    assert capsys.readouterr().out == ""
    assert (
        main(["reply", "--box", str(fx["box"]), "--to", "q-404", "--file", str(answer)])
        == 2
    )


def test_record_decided_cli(fx, capsys):
    args = ["record", "decided", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
            "--worktrees-root", str(fx["wt"]), "--change", "export-csv", "--stop", "1",
            "--reason", "no reply in 240 minutes"]  # fmt: skip
    assert main(args) == 0
    out = capsys.readouterr().out
    assert re.match(r"DECIDED:[0-9a-f]{40}\n", out)
    assert "Decided-by: agent" in git(
        fx["export-csv"], "log", "-1", "--format=%(trailers)"
    )
