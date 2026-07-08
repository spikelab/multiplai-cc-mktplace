"""Unit tests for the gmail skill engine (gmail.py).

Covers Fix 8 (_extract_body single-DFS, text/plain preferred) and Fix 6 (reply
drafts set the recipient explicitly). No network: a fake Gmail service stands in
for the discovery client.
"""
from __future__ import annotations

import base64
import email
import json

import pytest


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


def _text_part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def _multipart(mime: str, *parts: dict) -> dict:
    return {"mimeType": mime, "parts": list(parts)}


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Messages:
    def __init__(self, get_result):
        self._get_result = get_result

    def get(self, **_kw):
        return _Exec(self._get_result)


class _Drafts:
    def __init__(self, sink):
        self._sink = sink

    def create(self, userId, body):  # noqa: N803 - matches google client kwarg
        self._sink["body"] = body
        return _Exec({"id": "draft-xyz"})


class _Users:
    def __init__(self, get_result, sink):
        self._messages = _Messages(get_result)
        self._drafts = _Drafts(sink)

    def messages(self):
        return self._messages

    def drafts(self):
        return self._drafts


class FakeService:
    def __init__(self, get_result=None, sink=None):
        self._users = _Users(get_result, sink if sink is not None else {})

    def users(self):
        return self._users


def _decode_created_to(sink: dict) -> str:
    raw = sink["body"]["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw.encode("utf-8")))
    return msg["To"] or ""


# --------------------------------------------------------------------------- #
# Fix 8 — _extract_body
# --------------------------------------------------------------------------- #
class TestExtractBody:
    def test_flat_plain(self, gmail):
        payload = _text_part("text/plain", "hello plain")
        assert gmail._extract_body(payload) == "hello plain"

    def test_flat_html_only(self, gmail):
        payload = _text_part("text/html", "<p>hi html</p>")
        assert gmail._extract_body(payload) == "<p>hi html</p>"

    def test_prefers_plain_over_html_simple_alternative(self, gmail):
        payload = _multipart(
            "multipart/alternative",
            _text_part("text/html", "<p>HTML</p>"),
            _text_part("text/plain", "PLAIN"),
        )
        assert gmail._extract_body(payload) == "PLAIN"

    def test_html_sibling_before_nested_plain(self, gmail):
        # The confirmed failure case: a top-level text/html sibling appears
        # BEFORE a multipart/alternative that contains the text/plain body.
        # The plain body must still win.
        payload = _multipart(
            "multipart/mixed",
            _text_part("text/html", "<p>outer html</p>"),
            _multipart(
                "multipart/alternative",
                _text_part("text/html", "<p>inner html</p>"),
                _text_part("text/plain", "the real plain body"),
            ),
        )
        assert gmail._extract_body(payload) == "the real plain body"

    def test_deeply_nested_plain(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            _multipart(
                "multipart/related",
                _multipart(
                    "multipart/alternative",
                    _text_part("text/plain", "deep plain"),
                    _text_part("text/html", "<p>deep html</p>"),
                ),
            ),
        )
        assert gmail._extract_body(payload) == "deep plain"

    def test_html_fallback_when_no_plain(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            _text_part("text/html", "<p>only html</p>"),
        )
        assert gmail._extract_body(payload) == "<p>only html</p>"

    def test_empty_payload(self, gmail):
        assert gmail._extract_body({"mimeType": "multipart/mixed", "parts": []}) == ""

    def test_ignores_non_text_parts(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            {"mimeType": "image/png", "body": {"attachmentId": "a1"}},
            _text_part("text/plain", "body after image"),
        )
        assert gmail._extract_body(payload) == "body after image"


# --------------------------------------------------------------------------- #
# Fix 6 — reply recipient resolution
# --------------------------------------------------------------------------- #
def _orig_msg(headers: list[tuple[str, str]], thread="T1") -> dict:
    return {
        "threadId": thread,
        "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": n, "value": v} for n, v in headers]},
    }


class TestReplyRecipient:
    def _payload_file(self, tmp_path, payload: dict):
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_reply_without_to_uses_from_header(self, gmail, tmp_path, capsys):
        sink: dict = {}
        orig = _orig_msg([
            ("Message-ID", "<abc@x>"),
            ("Subject", "Contract"),
            ("From", "Marco Rossi <marco@example.com>"),
        ])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "Thanks Marco"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "Marco Rossi <marco@example.com>"
        out = capsys.readouterr().out
        assert "To: Marco Rossi <marco@example.com>" in out
        assert "inherited from thread" not in out

    def test_reply_prefers_reply_to_over_from(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([
            ("Message-ID", "<abc@x>"),
            ("From", "Marco <marco@example.com>"),
            ("Reply-To", "list@example.com"),
        ])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "hi"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "list@example.com"

    def test_explicit_to_overrides_headers(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("From", "marco@example.com")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"to": "someone@else.com", "body": "hi"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "someone@else.com"

    def test_reply_with_no_recipient_headers_dies(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("Subject", "no sender")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "hi"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" not in sink  # never reached drafts.create

    def test_new_draft_without_to_dies(self, gmail, tmp_path):
        svc = FakeService(sink={})
        inp = self._payload_file(tmp_path, {"body": "hi"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="none", input_file=inp)

    def test_draft_logs_audit_event(self, gmail, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(gmail, "log_event", lambda *a, **k: calls.append((a, k)))
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("From", "marco@example.com")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = tmp_path / "p.json"
        inp.write_text(json.dumps({"body": "hi"}), encoding="utf-8")
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=str(inp))
        assert calls, "expected a draft audit log_event"
        (args, kwargs) = calls[0]
        assert args[0] == "gmail" and args[1] == "draft"
        assert kwargs.get("mode") == "reply"
        assert kwargs.get("draft_id") == "draft-xyz"
