"""Unit tests for the slack skill engine (slack_client.py).

Covers Fix 3 (incremental sync must not advance the read-since marker on an
early exit), Fix 9 (the default-command / --data-dir re-parse no longer crashes),
and Fix 1 (send is audited via log_event). A fake WebClient stands in for Slack;
the SQLite store is real (a temp file).
"""
from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeClient:
    """Yields pre-canned conversations_history pages, newest-first.

    ``error_on_page`` (1-indexed) makes that page raise SlackApiError.
    """

    def __init__(self, pages, error_on_page=None):
        self.pages = pages
        self.error_on_page = error_on_page
        self.calls = 0

    def conversations_history(self, channel, oldest, limit, cursor=None):
        self.calls += 1
        page = self.calls  # 1-indexed
        if self.error_on_page and page == self.error_on_page:
            raise SlackApiError("boom", {"error": "internal_error"})
        msgs = self.pages[page - 1]
        has_more = page < len(self.pages)
        return {
            "messages": msgs,
            "response_metadata": {"next_cursor": f"c{page}" if has_more else ""},
        }


def _msgs(*tss):
    return [{"ts": t, "type": "message", "text": f"m{t}"} for t in tss]


@pytest.fixture
def reader_factory(slack, tmp_path):
    """Build a SlackReader wired to a real Store and a fake client."""

    def make(pages, error_on_page=None, preset_marker=None, cid="C1"):
        store = slack.Store(tmp_path / f"{cid}.db")
        store.upsert_channel(cid, "#test", False, False)
        if preset_marker is not None:
            store.set_marker(cid, preset_marker)
        reader = slack.SlackReader("xoxp-test", store, tmp_path / "assets")
        reader.client = FakeClient(pages, error_on_page=error_on_page)
        return reader, store, cid

    return make


# --------------------------------------------------------------------------- #
# Fix 3 — marker only advances on a clean, complete pass
# --------------------------------------------------------------------------- #
class TestSyncMarker:
    def test_limit_stop_does_not_advance_marker(self, reader_factory):
        reader, store, cid = reader_factory(
            [_msgs("300.0", "200.0", "150.0")], preset_marker="100.0")
        res = reader.sync_channel(cid, "#test", limit=2, fetch_files=False,
                                  fetch_threads=False)
        assert res["completed"] is False
        assert store.get_last_ts(cid) == "100.0"  # unchanged

    def test_api_error_mid_pagination_does_not_advance_marker(self, reader_factory):
        reader, store, cid = reader_factory(
            [_msgs("300.0", "250.0"), _msgs("200.0")],
            error_on_page=2, preset_marker="100.0")
        res = reader.sync_channel(cid, "#test", fetch_files=False,
                                  fetch_threads=False)
        assert res["completed"] is False
        assert store.get_last_ts(cid) == "100.0"  # unchanged despite page-1 rows

    def test_full_clean_run_advances_marker_to_newest_ts(self, reader_factory):
        reader, store, cid = reader_factory(
            [_msgs("300.0", "250.0"), _msgs("200.0", "150.0")],
            preset_marker="100.0")
        res = reader.sync_channel(cid, "#test", fetch_files=False,
                                  fetch_threads=False)
        assert res["completed"] is True
        assert store.get_last_ts(cid) == "300.0"  # newest ts across all pages

    def test_rerun_after_incomplete_refetches_and_completes(self, reader_factory):
        # First run errors on page 2 → marker stays put, page-1 rows are stored.
        reader, store, cid = reader_factory(
            [_msgs("300.0", "250.0"), _msgs("200.0")],
            error_on_page=2, preset_marker="100.0")
        reader.sync_channel(cid, "#test", fetch_files=False, fetch_threads=False)
        assert store.get_last_ts(cid) == "100.0"
        stored_after_fail = store.stats()["messages"]
        assert stored_after_fail == 2  # only page 1 made it in

        # Re-run cleanly: the whole window is refetched (INSERT OR IGNORE dedups),
        # the older message now arrives, and the marker finally advances.
        reader.client = FakeClient([_msgs("300.0", "250.0", "200.0")])
        reader.sync_channel(cid, "#test", fetch_files=False, fetch_threads=False)
        assert store.get_last_ts(cid) == "300.0"
        assert store.stats()["messages"] == 3  # no duplicates, older one added


# --------------------------------------------------------------------------- #
# Fix 9 — default-command / --data-dir re-parse
# --------------------------------------------------------------------------- #
class TestParserDefaultCommand:
    def test_bare_data_dir_reparses_as_sync_without_crash(self, slack):
        parser = slack.build_parser()
        # First parse: --data-dir with NO subcommand (the reproduced crash input).
        args = parser.parse_args(["--data-dir", "/some/where"])
        assert args.cmd is None
        assert args.data_dir == "/some/where"
        # main() re-parses with "sync" prepended — must NOT raise now that
        # --data-dir lives on a parent parser shared with the sync subparser.
        args2 = parser.parse_args(["sync", "--data-dir", "/some/where"])
        assert args2.cmd == "sync"
        assert args2.data_dir == "/some/where"
        # sync's own defaults are present.
        assert args2.full is False
        assert args2.channels is None

    def test_no_args_defaults_to_sync(self, slack):
        parser = slack.build_parser()
        args = parser.parse_args([])
        assert args.cmd is None
        assert parser.parse_args(["sync"]).cmd == "sync"


# --------------------------------------------------------------------------- #
# Fix 1 — send is audited
# --------------------------------------------------------------------------- #
class TestSendAudit:
    def test_cmd_send_logs_event(self, slack, monkeypatch):
        calls = []
        monkeypatch.setattr(slack, "log_event", lambda *a, **k: calls.append((a, k)))

        class FakeReader:
            def sync_users(self, *a, **k):
                return 0

            def resolve_target(self, to):
                return "C999"

            def post(self, channel, text, thread_ts=None):
                return {"channel": channel, "ts": "1720000000.000100"}

        class Args:
            to = "#eng"
            text = "hello"
            thread_ts = None

        slack.cmd_send(Args(), FakeReader(), store=None)
        assert calls, "expected a send audit log_event"
        (args, kwargs) = calls[0]
        assert args[0] == "slack" and args[1] == "send"
        assert kwargs.get("channel_id") == "C999"
        assert kwargs.get("ts") == "1720000000.000100"


# --------------------------------------------------------------------------- #
# MSG-4 — _safe() never emits an all-dot path component
# --------------------------------------------------------------------------- #
class TestSafe:
    def test_all_dot_labels_collapse(self, slack):
        assert slack._safe("..") == "_"
        assert slack._safe(".") == "_"

    def test_normal_dotted_names_preserved(self, slack):
        assert slack._safe("report.v2.pdf") == "report.v2.pdf"
