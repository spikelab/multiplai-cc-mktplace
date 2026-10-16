"""A new review never deletes a progress.log that no kept round holds."""

from __future__ import annotations

import json

from review_pipeline import orchestrator, rounds


def test_a_log_a_kept_round_holds_is_deleted(tmp_path):
    (tmp_path / "rounds" / "01-aaaaaaaaaaaa").mkdir(parents=True)
    (tmp_path / "rounds" / "01-aaaaaaaaaaaa" / "progress.log").write_text("round one\n")
    (tmp_path / "progress.log").write_text("round one\n")
    orchestrator._set_aside_progress(tmp_path)
    assert not (tmp_path / "progress.log").exists()
    assert not list(tmp_path.glob("progress-*.log"))


def test_a_log_no_kept_round_holds_moves_to_a_dated_name(tmp_path):
    (tmp_path / "progress.log").write_text("died before it saved state\n")
    orchestrator._set_aside_progress(tmp_path)
    assert not (tmp_path / "progress.log").exists()
    moved = list(tmp_path.glob("progress-*.log"))
    assert [p.read_text() for p in moved] == ["died before it saved state\n"]


def test_no_log_is_a_no_op(tmp_path):
    orchestrator._set_aside_progress(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_kept_round_does_not_copy_set_aside_logs(tmp_path):
    h = "e" * 40
    state = {"target": {"head_sha": h, "slug": "t"}, "timings": {"run": [{"started_at": "2026-01-01T00:00:00.100Z"}]}}
    (tmp_path / "review-state.json").write_text(json.dumps(state))
    (tmp_path / "progress.log").write_text("stopped\n")
    (tmp_path / "progress-20260101T000000Z.log").write_text("earlier\n")
    kept = rounds.keep_round(tmp_path, "t", h)
    assert (kept / "progress.log").read_text() == "stopped\n"
    assert not list(kept.glob("progress-*.log"))
