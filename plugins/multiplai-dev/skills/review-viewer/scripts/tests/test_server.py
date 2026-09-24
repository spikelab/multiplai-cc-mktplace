from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from review_viewer.__main__ import main
from review_viewer.mailbox import read_rows

HIGH = "b561bd34ce"


def test_rejects_missing_token(start_live):
    live = start_live()
    assert live.request("GET", "/api/whoami", token=None)[0] == 401
    assert live.request("GET", "/api/whoami", token="wrong")[0] == 401


def test_rejects_foreign_origin(start_live):
    live = start_live()
    status, _ = live.request("POST", "/api/ask", {"target": live.slug, "text": "hi"},
                             headers={"Origin": "https://evil.example"})
    assert status == 403
    assert not read_rows(live.box / "inbox.jsonl")
    # The page's own origin is fine.
    own = f"http://127.0.0.1:{live.port}"
    assert live.request("GET", "/api/whoami", headers={"Origin": own})[0] == 200


def test_rejects_text_plain_post(start_live):
    live = start_live()
    status, _ = live.request("POST", "/api/ask", b'{"target":"x","text":"hi"}', ctype="text/plain")
    assert status == 415
    assert not read_rows(live.box / "inbox.jsonl")


def test_page_needs_no_token_and_holds_no_review_data(start_live):
    live = start_live()
    status, body = live.request("GET", "/", token=None)
    assert status == 200 and b"<html" in body.lower()
    assert b"refunded" not in body and b"KeyError" not in body


def test_static_traversal_refused(start_live):
    live = start_live()
    assert live.request("GET", "/static/../server.py", token=None)[0] == 404
    assert live.request("GET", "/static/%2e%2e%2fserver.py", token=None)[0] == 404


def test_whoami_and_targets(start_live):
    live = start_live()
    status, who = live.request("GET", "/api/whoami")
    assert status == 200 and who["agent"] == "Claude Test" and who["session_id"] == "sess-A"
    status, t = live.request("GET", "/api/targets")
    assert t["targets"][0]["counts"]["severity"] == {"HIGH": 1, "MEDIUM": 1, "LOW": 1}
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and len(detail["findings"]["findings"]) == 3


def test_file_route(start_live):
    live = start_live()
    status, view = live.request("GET", f"/api/targets/{live.slug}/file?path=app/service.py")
    assert status == 200 and view["language"] == "python"
    assert live.request("GET", f"/api/targets/{live.slug}/file?path=setup.py")[0] == 404
    assert live.request("GET", "/api/targets/nope/file?path=app/service.py")[0] == 404


def test_ask_validation(start_live):
    live = start_live()
    assert live.request("POST", "/api/ask", {"target": "nope", "text": "hi"})[0] == 404
    assert live.request("POST", "/api/ask", {"target": live.slug, "text": ""})[0] == 400
    assert live.request("POST", "/api/ask", {"target": live.slug, "text": "x" * 8001})[0] == 400
    assert live.request("POST", "/api/ask", {"target": live.slug, "text": "hi",
                                              "finding_id": "0000000000"})[0] == 404


def test_ask_round_trip(start_live):
    live = start_live()
    status, res = live.request("POST", "/api/ask", {
        "target": live.slug, "finding_id": HIGH, "text": "Is qty ever missing?"})
    assert status == 200
    qid = res["id"]
    rows = read_rows(live.box / "inbox.jsonl")
    assert rows[-1]["id"] == qid and rows[-1]["kind"] == "question"
    assert rows[-1]["finding_id"] == HIGH and rows[-1]["text"] == "Is qty ever missing?"

    proc = subprocess.run([sys.executable, "-m", "review_viewer", "reply", "--box", str(live.box),
                           "--to", qid], input="Yes — the **legacy** checkout.", text=True,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr
    status, poll = live.request("GET", f"/api/poll?target={live.slug}&since=0")
    assert status == 200 and poll["n"] == 1
    assert poll["answers"][0] == {**poll["answers"][0], "reply_to": qid, "done": True,
                                  "text": "Yes — the **legacy** checkout."}


def test_anchor_question_without_finding(start_live):
    live = start_live()
    status, _ = live.request("POST", "/api/ask", {
        "target": live.slug, "finding_id": None, "text": "what is this?",
        "anchor": {"path": "app/service.py", "line_start": 3, "line_end": 5}})
    assert status == 200
    row = read_rows(live.box / "inbox.jsonl")[-1]
    assert row["anchor"] == {"path": "app/service.py", "line_start": 3, "line_end": 5}


def test_decision_writes_decisions_json(start_live):
    live = start_live()
    status, _ = live.request("POST", "/api/decision",
                             {"target": live.slug, "finding_id": HIGH, "decision": "reject",
                              "note": "legacy checkout is gone"})
    assert status == 200
    data = json.loads((live.box / "decisions.json").read_text())
    assert data[HIGH]["decision"] == "reject" and data[HIGH]["note"] == "legacy checkout is gone"
    row = read_rows(live.box / "inbox.jsonl")[-1]
    assert row["kind"] == "decision" and row["decision"] == "reject"
    assert live.request("POST", "/api/decision", {"target": live.slug, "finding_id": HIGH,
                                                   "decision": "maybe"})[0] == 400
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert detail["decisions"][HIGH]["decision"] == "reject"


def test_idle_watchdog_stops_server(start_live):
    live = start_live(idle=0.02)  # 1.2 s
    live.thread.join(10)
    assert not live.thread.is_alive()
    assert live.viewer.stop_reason == "idle"
    for name in ("server.token", "open.html", "server.json"):
        assert not (live.box / name).exists(), name


def test_heartbeat_keeps_server_alive(start_live):
    live = start_live(idle=0.03)  # 1.8 s
    for _ in range(8):
        time.sleep(0.4)
        assert live.request("GET", "/api/alive")[0] == 200
    assert live.thread.is_alive()


def test_shutdown_route(start_live):
    live = start_live()
    assert live.request("POST", "/api/shutdown", {})[0] == 200
    live.thread.join(5)
    assert not live.thread.is_alive()


def _serve_in_process(*args) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(args))
    return code, out.getvalue()


def test_second_serve_reuses_or_refuses(start_live):
    live = start_live(session_id="sess-A")
    port = str(live.port)
    code, out = _serve_in_process("--session-id", "sess-A", "serve", str(live.findings),
                                  "--port", port)
    assert code == 0 and "reusing" in out
    assert f"open: file://{live.box / 'open.html'}" in out
    code, out = _serve_in_process("--session-id", "sess-B", "serve", str(live.findings),
                                  "--port", port)
    assert code == 3
    assert "owned by another session (sess-A)" in out and "stop --box" in out
    assert live.thread.is_alive()


def test_stale_server_json_is_not_trusted(findings_path):
    box = findings_path.parent / "viewer"
    box.mkdir()
    (box / "server.json").write_text(json.dumps({"port": 1, "session_id": "x"}))
    (box / "server.token").write_text("stale")
    from review_viewer import registry
    assert registry.existing(box, 1, 1) is None
    assert not (box / "server.json").exists()


def test_token_stays_out_of_stdout_logs_and_server_json(findings_path, tmp_path):
    from conftest import free_port
    ws = tmp_path / "ws"
    env = dict(os.environ, WORKSPACE=str(ws), PYTHONUNBUFFERED="1", MULTIPLAI_DEBUG="1")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "review_viewer", "--session-id", "sess-T", "serve",
         str(findings_path), "--port", str(port), "--idle", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("monitor:"):
                break
        box = findings_path.parent / "viewer"
        token = (box / "server.token").read_text().strip()
        assert len(token) >= 40
        for name in ("server.token", "open.html"):
            assert stat.S_IMODE((box / name).stat().st_mode) == 0o600, name
        assert token in (box / "open.html").read_text()
        # Exercise the paths that log: a question, a bad token, a decision.
        import urllib.request
        def call(route, body, tok):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "X-Review-Token": tok})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        slug = json.loads(findings_path.read_text())["target"]["slug"]
        assert call("/api/ask", {"target": slug, "text": "secret question"}, token) == 200
        assert call("/api/ask", {"target": slug, "text": "x"}, "bad") == 401
        assert call("/api/decision", {"target": slug, "finding_id": HIGH, "decision": "defer"},
                    token) == 200
        server_json = (box / "server.json").read_text()
        assert call("/api/shutdown", {}, token) == 200
        rest_out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    stdout = "".join(lines) + rest_out
    assert stdout.startswith(f"open: file://{box.resolve() / 'open.html'}")
    assert token not in stdout and token not in err
    assert token not in server_json
    logs = ws / ".multiplai" / "data" / "logs"
    files = [p for p in logs.rglob("*") if p.is_file()]
    assert any(p.name == "review-viewer.log" for p in files)
    for p in files:
        assert token not in p.read_text(errors="replace"), p
    assert not (box / "server.token").exists() and not (box / "open.html").exists()
