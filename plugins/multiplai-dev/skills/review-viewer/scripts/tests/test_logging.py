"""One real ask/reply/decision cycle through the CLI, then the log files.

Every process in the cycle (the `serve` server, the `reply` command, and this
test) resolves its logs directory from the same WORKSPACE, which lives under
the system temp root, so multiplai_core's pytest guard leaves it alone and
all three write to one place.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from sys import executable as PYTHON

import pytest
from multiplai_core import paths
from multiplai_core.log_utils import _get_logs_dir

from conftest import free_port

HIGH = "b561bd34ce"
QUESTION = "Is the qty field ever missing in production carts?"
ANSWER = "Only from the legacy checkout, which still exists."


@pytest.fixture
def logs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
    paths._reset_cache()
    yield dict(os.environ, PYTHONUNBUFFERED="1")
    paths._reset_cache()


def _post(port, route, body, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Review-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, None


def test_cycle_logs_events_without_text_or_token(findings_path, logs_env, tmp_path):
    port = free_port()
    server = subprocess.Popen(
        [PYTHON, "-m", "review_viewer", "--session-id", "sess-L", "serve", str(findings_path),
         "--port", str(port), "--idle", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=logs_env)
    try:
        for line in server.stdout:
            if line.startswith("monitor:"):
                break
        box = findings_path.parent / "viewer"
        token = (box / "server.token").read_text().strip()
        slug = json.loads(findings_path.read_text())["target"]["slug"]

        status, res = _post(port, "/api/ask", {"target": slug, "finding_id": HIGH,
                                               "text": QUESTION}, token)
        assert status == 200
        answer = tmp_path / "answer.md"
        answer.write_text(ANSWER, encoding="utf-8")
        reply = subprocess.run(
            [PYTHON, "-m", "review_viewer", "--session-id", "sess-L", "reply", "--box", str(box),
             "--to", res["id"], "--file", str(answer)], capture_output=True, text=True,
            env=logs_env)
        assert reply.returncode == 0, reply.stderr
        assert _post(port, "/api/decision", {"target": slug, "finding_id": HIGH,
                                             "decision": "reject"}, token)[0] == 200
        assert _post(port, "/api/ask", {"target": slug, "text": "x"}, "bad-token")[0] == 401
        assert _post(port, "/api/shutdown", {}, token)[0] == 200
        server.wait(timeout=10)
    finally:
        if server.poll() is None:
            server.kill()

    logs = _get_logs_dir()
    assert logs == tmp_path / "ws" / ".multiplai" / "data" / "logs"
    assert (logs / "review-viewer.log").exists()
    records = [json.loads(line) for line in (logs / "activity.jsonl").read_text().splitlines()
               if line.strip()]
    ours = [r for r in records if r.get("component") == "review-viewer"]
    events = [r["event"] for r in ours]
    for expected in ("start", "question", "reply", "decision", "rejected_request", "stop"):
        assert expected in events, (expected, events)
    by_event = {r["event"]: r for r in ours}
    assert by_event["question"]["chars"] == len(QUESTION)
    assert by_event["reply"]["done"] is True and by_event["reply"]["chars"] == len(ANSWER)
    assert by_event["decision"]["decision"] == "reject"
    assert by_event["decision"]["msg"] == f"finding {HIGH} rejected"
    assert by_event["start"]["port"] == port
    assert by_event["rejected_request"]["level"] == "WARNING"

    for path in logs.iterdir():
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert QUESTION not in text and ANSWER not in text, path
            assert token not in text, path


def test_rejected_requests_are_rate_limited(start_live, logs_env):
    live = start_live()
    for _ in range(5):
        assert live.request("GET", "/api/whoami", token="nope")[0] == 401
    records = [json.loads(line) for line in
               (_get_logs_dir() / "activity.jsonl").read_text().splitlines() if line.strip()]
    rejected = [r for r in records if r.get("event") == "rejected_request" and r["status"] == 401]
    assert len(rejected) == 1
