from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from sys import executable as PYTHON
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

    proc = subprocess.run([PYTHON, "-m", "review_viewer", "reply", "--box", str(live.box),
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
    assert row["anchor"] == {"path": "app/service.py", "side": "head", "line_start": 3, "line_end": 5}


def test_explain_question_needs_an_anchor_and_is_marked(start_live):
    live = start_live()
    anchor = {"path": "app/service.py", "side": "head", "line_start": 7, "line_end": 9}
    status, _ = live.request("POST", "/api/ask", {
        "target": live.slug, "text": "Explain this block.", "anchor": anchor, "explain": True})
    assert status == 200
    row = read_rows(live.box / "inbox.jsonl")[-1]
    assert row["explain"] is True and row["anchor"] == anchor
    status, _ = live.request("POST", "/api/ask", {
        "target": live.slug, "text": "Explain this block.", "explain": True})
    assert status == 400
    # An ordinary question is not marked.
    live.request("POST", "/api/ask", {"target": live.slug, "text": "why?"})
    assert read_rows(live.box / "inbox.jsonl")[-1]["explain"] is False


def test_anchor_on_deleted_lines_carries_side_base(start_live):
    live = start_live()
    status, _ = live.request("POST", "/api/ask", {
        "target": live.slug, "text": "why remove this?",
        "anchor": {"path": "app/old_module.py", "side": "base", "line_start": 1, "line_end": 4}})
    assert status == 200
    assert read_rows(live.box / "inbox.jsonl")[-1]["anchor"]["side"] == "base"
    status, _ = live.request("POST", "/api/ask", {
        "target": live.slug, "text": "x",
        "anchor": {"path": "app/x.py", "side": "left", "line_start": 1, "line_end": 1}})
    assert status == 400


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


def test_viewed_writes_viewed_json(start_live):
    live = start_live()
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert detail["viewed"] == {}
    path = detail["files"][0]
    status, res = live.request("POST", "/api/viewed", {"target": live.slug, "path": path, "viewed": True})
    assert status == 200 and set(res["viewed"]) == {path}
    assert set(json.loads((live.box / "viewed.json").read_text())) == {path}
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert set(detail["viewed"]) == {path}
    assert live.request("POST", "/api/viewed", {"target": live.slug, "path": path,
                                                 "viewed": False})[1]["viewed"] == {}
    # Only changed files, and only a real boolean.
    assert live.request("POST", "/api/viewed", {"target": live.slug, "path": "../etc/passwd",
                                                 "viewed": True})[0] == 404
    assert live.request("POST", "/api/viewed", {"target": live.slug, "path": path,
                                                 "viewed": "yes"})[0] == 400
    # Nothing goes to the session's inbox.
    assert read_rows(live.box / "inbox.jsonl") == []


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
    assert registry.existing(box) is None
    assert not (box / "server.json").exists()


def test_token_stays_out_of_stdout_logs_and_server_json(findings_path, tmp_path):
    from conftest import free_port
    ws = tmp_path / "ws"
    env = dict(os.environ, WORKSPACE=str(ws), PYTHONUNBUFFERED="1", MULTIPLAI_DEBUG="1")
    port = free_port()
    proc = subprocess.Popen(
        [PYTHON, "-m", "review_viewer", "--session-id", "sess-T", "serve",
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


def test_static_assets_and_headers(start_live):
    import urllib.request
    live = start_live()
    for name, ctype in (("app.js", "javascript"), ("logic.js", "javascript"),
                        ("boot.js", "javascript"), ("app.css", "text/css")):
        with urllib.request.urlopen(f"http://127.0.0.1:{live.port}/static/{name}", timeout=5) as r:
            assert r.status == 200 and ctype in r.headers["Content-Type"], name
            assert r.headers["Referrer-Policy"] == "no-referrer"
            assert "connect-src 'self'" in r.headers["Content-Security-Policy"]


# --- regressions from the PR 243 review --------------------------------------------


def test_changed_findings_restart_the_viewer(start_live):
    live = start_live(session_id="sess-A")
    data = json.loads(live.findings.read_text())
    data["findings"][0]["verdict_reason"] = "re-reviewed"
    live.findings.write_text(json.dumps(data))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([PYTHON, "-m", "review_viewer", "--session-id", "sess-A", "serve",
                             str(live.findings), "--idle", "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        out = []
        for line in proc.stdout:
            out.append(line)
            if line.startswith("pending:"):
                break
        assert "a findings file changed; restarted the viewer\n" in out
        live.thread.join(5)
        assert not live.thread.is_alive()
        token = (live.box / "server.token").read_text().strip()
        port = json.loads((live.box / "server.json").read_text())["port"]
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/targets/{live.slug}",
                                     headers={"X-Review-Token": token})
        with urllib.request.urlopen(req, timeout=5) as r:
            served = json.loads(r.read())
        assert served["findings"]["findings"][0]["verdict_reason"] == "re-reviewed"
        from review_viewer import registry
        who = registry.existing(live.box)
        assert registry.shutdown(who, token)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_monitor_command_survives_spaces_and_quotes(tmp_path):
    import shlex
    from review_viewer.__main__ import _monitor_command
    box = tmp_path / "My Reviews" / "pr 12" / "viewer"
    text = _monitor_command([box], 'label with "quotes"')
    command = json.loads(text[len("Monitor(command="):text.index(", description=")])
    assert shlex.split(command)[-1] == str(box / "inbox.jsonl")
    desc = text[text.index("description=") + len("description="):text.index(", timeout_ms")]
    assert json.loads(desc).endswith('label with "quotes"')


def test_pending_lists_rows_without_a_final_reply(tmp_path):
    from review_viewer.mailbox import Mailbox, utc_now
    from review_viewer.models import InboxRow, OutboxRow
    box = Mailbox(tmp_path / "viewer")
    box.create()
    ids = {}
    for name, kind in (("done", "question"), ("partial", "question"), ("none", "question"),
                       ("decided", "decision")):
        row = InboxRow(id=f"q-{name}", ts=utc_now(), target="t", kind=kind, text=name,
                       decision="accept" if kind == "decision" else None)
        box.append_inbox(row)
        ids[name] = row.id
    box.append_outbox(OutboxRow(reply_to="q-done", ts=utc_now(), text="a", done=True))
    box.append_outbox(OutboxRow(reply_to="q-partial", ts=utc_now(), text="b", done=False))
    code, out = _serve_in_process("pending", "--box", str(box.dir))
    assert code == 0
    assert [json.loads(line)["id"] for line in out.splitlines()] == [
        "q-partial", "q-none", "q-decided"]


def test_unanswering_live_process_keeps_its_files(findings_path):
    from conftest import free_port
    from review_viewer import registry
    box = findings_path.parent / "viewer"
    box.mkdir()
    (box / "server.json").write_text(json.dumps({"port": free_port(), "pid": os.getpid()}))
    (box / "server.token").write_text("tok")
    who = registry.existing(box)
    assert who and who["unresponsive"]
    assert (box / "server.json").exists() and (box / "server.token").exists()


def test_whoami_probes_do_not_keep_the_server_alive(start_live):
    live = start_live(idle=0.03)  # 1.8 s
    deadline = time.monotonic() + 8
    while live.thread.is_alive() and time.monotonic() < deadline:
        try:
            live.request("GET", "/api/whoami")
        except OSError:  # the server stopping mid-probe is the expected outcome
            break
        time.sleep(0.2)
    live.thread.join(5)
    assert not live.thread.is_alive()
    assert live.viewer.stop_reason == "idle"


def test_tokens_go_only_to_their_own_port(start_live, findings_path, tmp_path, monkeypatch):
    from review_viewer import registry
    live = start_live()
    registry.register(os.getpid(), [live.box])
    other = tmp_path / "other" / "viewer"
    other.mkdir(parents=True)
    (other / "server.token").write_text("other-token")
    (other / "server.json").write_text(json.dumps({"port": 1, "pid": os.getpid()}))
    registry.register(os.getpid() + 100000, [other])
    calls = []
    real = registry._request

    def spy(port, route, token, **kw):
        calls.append((port, token))
        return real(port, route, token, **kw)

    monkeypatch.setattr(registry, "_request", spy)
    found = registry.scan([live.box, other])
    assert [w["port"] for w in found] == [live.port]
    assert all((port, tok) in {(live.port, live.token), (1, "other-token")}
               for port, tok in calls), calls


def test_session_id_falls_back_to_claude_code_env(start_live, monkeypatch):
    live = start_live(session_id="sess-env")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-env")
    code, out = _serve_in_process("--session-id", "{session_id}", "serve", str(live.findings))
    assert code == 0 and "reusing" in out
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, out = _serve_in_process("serve", str(live.findings))
    assert code == 3 and "cannot identify" in out


def test_negative_content_length_is_refused(start_live):
    import socket
    live = start_live()
    with socket.create_connection(("127.0.0.1", live.port), timeout=5) as s:
        s.sendall((f"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
                   f"X-Review-Token: {live.token}\r\nContent-Type: application/json\r\n"
                   "Content-Length: -1\r\n\r\n").encode())
        assert s.recv(64).startswith(b"HTTP/1.0 400")


def test_walkthrough_route_needs_the_token_and_serves_the_file(start_live):
    from review_viewer.walkthrough import walkthrough_path
    live = start_live()
    route = f"/api/targets/{live.slug}/walkthrough"
    assert live.request("GET", route)[0] == 404
    data = json.loads(live.findings.read_text())["target"]
    walkthrough_path(live.box).write_text(json.dumps({
        "schema_version": 1, "generated_at": "2026-09-26T10:00:00Z",
        "base_sha": data["base_sha"], "head_sha": data["head_sha"], "overview_md": "o",
        "steps": [], "skipped": [], "complete": False}))
    assert live.request("GET", route, token=None)[0] == 401
    status, body = live.request("GET", route)
    assert status == 200 and body["overview_md"] == "o" and body["steps"] == []
    assert live.request("GET", "/api/targets/nope/walkthrough")[0] == 404


def test_pr_refresh_asks_github_at_most_once_a_minute(findings_path, tmp_path, monkeypatch):
    from review_viewer import server as srv
    from review_viewer.mailbox import Mailbox
    from review_viewer.models import load_findings
    from review_viewer.stats import Badge
    calls = []

    def fake_status(repo, number, url, timeout=6.0):
        calls.append((number, url))
        return {"checks": {"total": 3, "passed": 3, "failed": 0, "pending": 0},
                "mergeable": "MERGEABLE", "draft": False, "review_decision": ""}

    monkeypatch.setattr(srv, "pr_status", fake_status)
    pr = {"number": 7, "url": "https://github.com/o/r/pull/7", "body": "x" * 50,
          "checks": {"total": 3, "passed": 1, "failed": 0, "pending": 2}}
    stats = {"badges": [Badge("totals", "1 file", "good", "").to_dict(),
                        Badge("checks", "Checks: 2 running", "note", "").to_dict()]}
    state = srv.TargetState(findings=load_findings(findings_path), mailbox=Mailbox(tmp_path / "box"),
                            allowed=set(), pr=pr, stats=stats)
    # Just served: no second call yet.
    assert state.refresh_pr()["pr"]["checks"]["pending"] == 2 and calls == []
    state._pr_checked -= srv.PR_REFRESH_S
    res = state.refresh_pr()
    assert calls == [(7, "https://github.com/o/r/pull/7")]
    assert res["pr"]["checks"]["pending"] == 0
    assert [b["label"] for b in res["stats"]["badges"]] == ["1 file", "Checks: 3 passing"]
    state.refresh_pr()
    assert len(calls) == 1


def test_pr_refresh_keeps_the_last_answer_when_gh_fails(findings_path, tmp_path, monkeypatch):
    from review_viewer import server as srv
    from review_viewer.gitdata import TargetError
    from review_viewer.mailbox import Mailbox
    from review_viewer.models import load_findings

    def failing(*a, **k):
        raise TargetError("gh pr view 7 failed: offline")

    monkeypatch.setattr(srv, "pr_status", failing)
    pr = {"number": 7, "checks": {"total": 1, "passed": 0, "failed": 0, "pending": 1}}
    state = srv.TargetState(findings=load_findings(findings_path), mailbox=Mailbox(tmp_path / "box"),
                            allowed=set(), pr=pr)
    state._pr_checked -= srv.PR_REFRESH_S
    assert state.refresh_pr()["pr"]["checks"]["pending"] == 1


def test_pr_route_without_a_pr_returns_null(start_live):
    live = start_live()
    status, res = live.request("GET", f"/api/targets/{live.slug}/pr")
    assert status == 200 and res["pr"] is None


RUN = {"started_at": "2026-10-01T10:00:00Z", "ended_at": "2026-10-01T10:01:00Z", "wall_seconds": 60.0,
       "calls": 2, "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4, "total": 10},
       "cost_usd": 0.5, "max_usd": 50.0, "stopped_by_budget": False,
       "stages": [{"name": "verify", "stage": "verify", "calls": 2,
                   "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4, "total": 10},
                   "cost_usd": 0.5, "wall_seconds": 60.0, "model": "session default", "effort": "session default"}],
       "counts": {"found": 3, "rejected": 0, "refuted": 0, "unverifiable": 0, "merged": 0}, "errors": 0}


def test_findings_with_and_without_run_serve(start_live, findings_path, tmp_path):
    live = start_live()
    status, t = live.request("GET", "/api/targets")
    assert status == 200 and t["targets"][0]["run"] is None  # the fixture predates `run`
    assert live.request("GET", f"/api/targets/{live.slug}")[1]["findings"]["run"] is None
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["run"] = RUN
    with_run = tmp_path / "with-run" / "findings.json"
    with_run.parent.mkdir()
    with_run.write_text(json.dumps(data), encoding="utf-8")
    live2 = start_live(path=with_run)
    status, t = live2.request("GET", "/api/targets")
    assert status == 200 and t["targets"][0]["run"]["cost_usd"] == 0.5
    status, detail = live2.request("GET", f"/api/targets/{live2.slug}")
    assert status == 200 and detail["findings"]["run"]["stages"][0]["name"] == "verify"

def test_findings_with_and_without_assessment_both_serve(start_live, findings_path):
    """A findings.json from before the assess stage has no `assessment`; one after has it."""
    plain = start_live()
    status, detail = plain.request("GET", f"/api/targets/{plain.slug}")
    assert status == 200 and all("assessment" not in f or f["assessment"] is None
                                 for f in detail["findings"]["findings"])

    data = json.loads(findings_path.read_text())
    data["findings"][0]["assessment"] = {
        "label": "repeat", "reason": "the same defect as before", "earlier_id": "abcdef0123",
        "earlier_round": "1" * 40, "earlier_decision": "reject", "earlier_note": "by design"}
    data["findings"][1]["assessment"] = {"label": "low-value", "reason": "[speculative] r"}
    labelled = findings_path.parent.parent / "labelled" / "findings.json"
    labelled.parent.mkdir()
    labelled.write_text(json.dumps(data))
    live = start_live(path=labelled)
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200
    got = [f.get("assessment") for f in detail["findings"]["findings"]]
    assert got[0]["label"] == "repeat" and got[0]["earlier_note"] == "by design"
    assert got[1]["label"] == "low-value" and got[1]["earlier_id"] is None


# --- every file at head ---------------------------------------------------------------

def _wide_findings(tmp_path):
    from fixture_repo import build_wide
    from review_viewer.gitdata import diff_findings, diff_target
    repo = tmp_path / "wide"
    base, head = build_wide(repo)
    review = tmp_path / "wide-review"
    review.mkdir()
    path = review / "findings.json"
    path.write_text(diff_findings(diff_target(repo, f"{base}..{head}")).model_dump_json(), encoding="utf-8")
    return path


def test_detail_lists_every_repo_file_and_serves_unchanged_ones(start_live, tmp_path):
    live = start_live(path=_wide_findings(tmp_path))
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["files"] == ["app/main.py"]
    assert "lib/util.py" in detail["repo_files"] and detail["repo_files_total"] == 6
    status, view = live.request("GET", f"/api/targets/{live.slug}/file?path=lib/util.py")
    assert status == 200 and {r["k"] for r in view["rows"]} == {"ctx"}
    for bad in ("../../etc/passwd", "/etc/passwd", "lib/../lib/util.py", "missing.py"):
        assert live.request("GET", f"/api/targets/{live.slug}/file?path={bad}")[0] == 404


def test_repo_files_list_is_cut_at_the_cap(start_live, tmp_path, monkeypatch):
    from review_viewer import server
    monkeypatch.setattr(server, "REPO_FILES_MAX", 2)
    live = start_live(path=_wide_findings(tmp_path))
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert len(detail["repo_files"]) == 2 and detail["repo_files_total"] == 6
    # The cap is on the list the page gets, not on what the file route serves.
    assert live.request("GET", f"/api/targets/{live.slug}/file?path=web/view.ts")[0] == 200


# --- where a name is defined ------------------------------------------------------------

def test_definitions_finds_each_form_in_the_repo(start_live, tmp_path):
    live = start_live(path=_wide_findings(tmp_path))
    url = f"/api/targets/{live.slug}/definitions?name="

    def hits(name):
        status, body = live.request("GET", url + name)
        assert status == 200, body
        return [(h["path"], h["line"]) for h in body["hits"]]

    assert hits("helper") == [("lib/util.py", 6)]          # def, in a file the change leaves alone
    assert hits("Widget") == [("lib/util.py", 10)]          # class
    assert hits("RATE") == [("lib/util.py", 3)]             # NAME = at line start
    assert hits("thing") == [("web/view.ts", 1)]            # export const
    assert hits("render") == [("web/view.ts", 2)]           # export function
    assert hits("counter") == [("web/view.ts", 5)]          # let
    assert hits("Props") == [("web/view.ts", 6)]            # interface
    assert hits("refresh_totals") == [("db/refresh.sql", 1)]  # CREATE OR REPLACE FUNCTION
    assert hits("total") == [("lib/util.py", 11)]           # a method; `total(` calls are not hits
    assert hits("nowhere") == []
    _, body = live.request("GET", url + "helper")
    assert body["hits"][0]["text"] == "def helper(x):"


def test_definitions_refuses_anything_but_an_identifier(start_live, tmp_path):
    live = start_live(path=_wide_findings(tmp_path))
    url = f"/api/targets/{live.slug}/definitions?name="
    for bad in ("", "1abc", "a-b", "a%20b", "a.b", "x" * 101, "%27%3B", "-e", "a*"):
        assert live.request("GET", url + bad)[0] == 400, bad
    assert live.request("GET", url + "helper", token="wrong")[0] == 401


def test_definitions_stop_at_the_cap(start_live, tmp_path, monkeypatch):
    from review_viewer import gitdata
    import subprocess
    from fixture_repo import build_wide
    repo = tmp_path / "wide"
    build_wide(repo)
    many = "".join(f"def dup():\n    return {i}\n\n" for i in range(60))
    (repo / "lib" / "dups.py").write_text(many, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "dups"], check=True)
    target = gitdata.diff_target(repo, "HEAD~1..HEAD")
    listed = gitdata.repo_files(target)
    hits = gitdata.definitions(target, "dup", gitdata.allowed_paths(target, None, listed))
    assert len(hits) == gitdata.DEFINITIONS_MAX == 50
    # A path the file route would not serve is never a hit.
    assert gitdata.definitions(target, "dup", {"app/main.py"}) == []


# --- send a finding to the PR or to Slack -------------------------------------------------

def _share_body(live, **kw):
    return {"target": live.slug, "finding_id": HIGH, "to": "slack", "where": "#reviews",
            "text": "### [HIGH] a finding", **kw}


def test_share_flags_in_the_detail(start_live):
    live = start_live()
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert detail["share"] == {"github": False, "pr": None, "slack": False}
    live.viewer.share_slack = True
    live.viewer.targets[live.slug].pr_number = 7
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert detail["share"] == {"github": True, "pr": 7, "slack": True}


def test_share_is_refused_when_the_destination_is_not_available(start_live):
    live = start_live()
    status, body = live.request("POST", "/api/share", _share_body(live))
    assert status == 403 and "Slack skill" in body["error"]
    status, body = live.request("POST", "/api/share", _share_body(live, to="github", where="pr"))
    assert status == 403 and "not of a PR" in body["error"]
    assert not any(r.get("kind") == "share" for r in live.viewer.targets[live.slug].mailbox.read_inbox())


def test_share_validation(start_live):
    from review_viewer.models import SHARE_TEXT_MAX
    live = start_live()
    live.viewer.share_slack = True
    live.viewer.targets[live.slug].pr_number = 7
    post = lambda **kw: live.request("POST", "/api/share", _share_body(live, **kw))[0]  # noqa: E731
    assert post(to="email") == 400
    assert post(finding_id="0000000000") == 404
    assert post(finding_id=None) == 404
    assert post(text="") == 400 and post(text="   ") == 400
    assert post(text="x" * (SHARE_TEXT_MAX + 1)) in (400, 413)
    assert post(where="") == 400 and post(where="a\nb") == 400 and post(where=None) == 400
    assert post(to="github", where="#reviews") == 400
    assert live.request("POST", "/api/share", _share_body(live), token="bad")[0] == 401


def test_share_writes_one_inbox_row_for_the_session(start_live):
    live = start_live()
    live.viewer.share_slack = True
    live.viewer.targets[live.slug].pr_number = 7
    status, res = live.request("POST", "/api/share", _share_body(live, where="  Marco Rossi "))
    assert status == 200
    status, res2 = live.request("POST", "/api/share", _share_body(live, to="github", where="line"))
    assert status == 200
    rows = [r for r in live.viewer.targets[live.slug].mailbox.read_inbox() if r["kind"] == "share"]
    assert [(r["id"], r["to"], r["where"]) for r in rows] == [
        (res["id"], "slack", "Marco Rossi"), (res2["id"], "github", "line")]
    assert rows[0]["text"] == "### [HIGH] a finding" and rows[0]["finding_id"] == HIGH
    # The page gets them back with its questions, so replies thread under them.
    _, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert {q["id"] for q in detail["questions"]} >= {res["id"], res2["id"]}


def test_pr_number_is_read_from_the_review_state(tmp_path):
    from review_viewer.server import pr_number_of
    assert pr_number_of({"pr": {"number": 12}}, tmp_path) == 12
    assert pr_number_of({}, tmp_path) is None
    (tmp_path / "review-state.json").write_text(json.dumps({"target": {"pr": 278}}), encoding="utf-8")
    assert pr_number_of({}, tmp_path) == 278
    (tmp_path / "review-state.json").write_text(json.dumps({"target": {"pr": None}}), encoding="utf-8")
    assert pr_number_of({}, tmp_path) is None
