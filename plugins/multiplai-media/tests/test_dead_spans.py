"""Tests for prep's dead-span classification — the "video sits there" detector.

_dead_spans() is pure Python (no ffmpeg, no I/O): it takes the per-second
motion profile, the blackdetect spans, and the container duration, and returns
the spans an EDL author must cut around. These tests lock in the calibrated
thresholds (static < 0.02, low < 1.0), the minimum span length, black-span
precedence, and the no-video-frames tail rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "screen-demo" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages.prep import DeadSpan, _dead_spans  # noqa: E402

ACTIVE = 8.0    # scroll/navigation-level motion
TYPING = 0.4    # cursor blink / characters landing
FROZEN = 0.0


def _kinds(spans: list[DeadSpan]) -> list[tuple[float, float, str]]:
    return [(s.start, s.end, s.kind) for s in spans]


def test_frozen_run_is_static() -> None:
    profile = [ACTIVE] * 5 + [FROZEN] * 10 + [ACTIVE] * 5
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(5.0, 15.0, "static")]


def test_typing_wait_is_low() -> None:
    profile = [ACTIVE] * 5 + [TYPING] * 6 + [ACTIVE] * 5
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(5.0, 11.0, "low")]


def test_isolated_short_pause_is_not_reported() -> None:
    profile = [ACTIVE] * 4 + [FROZEN] * 2 + [ACTIVE] * 4
    assert _dead_spans(profile, [], float(len(profile))) == []


def test_keystroke_blips_merge_into_one_low_span() -> None:
    # The form-typing signature: frozen stretches separated by single-second
    # keystroke blips must read as ONE dead span, not a fragmented list.
    profile = [ACTIVE] * 3 + ([FROZEN] * 3 + [ACTIVE]) * 4 + [ACTIVE] * 3
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(3.0, 18.0, "low")]


def test_page_level_burst_blocks_merge() -> None:
    # A 1s navigation/results-render burst (motion >= 30) is a money-shot
    # anchor — it must split dead spans, not be swallowed like a keystroke.
    BURST = 50.0
    profile = [ACTIVE] * 3 + [FROZEN] * 4 + [BURST] + [FROZEN] * 4 + [ACTIVE] * 3
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(3.0, 7.0, "static"), (8.0, 12.0, "static")]


def test_no_merge_across_sustained_activity() -> None:
    profile = [ACTIVE] * 3 + [FROZEN] * 4 + [ACTIVE] * 5 + [FROZEN] * 4 + [ACTIVE] * 3
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(3.0, 7.0, "static"), (12.0, 16.0, "static")]


def test_no_merge_across_a_black_gap() -> None:
    # Static runs on either side of a black gap must stay separate spans —
    # merging across would double-report the black stretch.
    profile = [ACTIVE] * 3 + [FROZEN] * 4 + [FROZEN] * 2 + [FROZEN] * 4 + [ACTIVE] * 3
    black = [DeadSpan(start=7.0, end=9.0, kind="black")]
    spans = _dead_spans(profile, black, float(len(profile)))
    assert _kinds(spans) == [(3.0, 7.0, "static"), (7.0, 9.0, "black"), (9.0, 13.0, "static")]


def test_black_spans_take_precedence_over_motion() -> None:
    # A black screen also measures as zero motion; it must be reported as
    # black (from blackdetect), not double-reported as static.
    profile = [ACTIVE] * 5 + [FROZEN] * 10 + [ACTIVE] * 5
    black = [DeadSpan(start=5.0, end=15.0, kind="black")]
    spans = _dead_spans(profile, black, float(len(profile)))
    assert _kinds(spans) == [(5.0, 15.0, "black")]


def test_tail_beyond_last_video_frame_is_static() -> None:
    # Screen recordings often carry audio past the last video frame; the
    # profile ends where frames end but the container keeps going.
    profile = [ACTIVE] * 10
    spans = _dead_spans(profile, [], 30.0)
    assert _kinds(spans) == [(10.0, 30.0, "static")]


def test_adjacent_static_and_low_runs_merge_as_low() -> None:
    profile = [ACTIVE] * 3 + [FROZEN] * 5 + [TYPING] * 5 + [ACTIVE] * 3
    spans = _dead_spans(profile, [], float(len(profile)))
    assert _kinds(spans) == [(3.0, 13.0, "low")]


def test_fully_active_recording_reports_nothing() -> None:
    profile = [ACTIVE] * 60
    assert _dead_spans(profile, [], 60.0) == []
