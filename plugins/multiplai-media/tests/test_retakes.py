"""Retake marking (stages/retakes.py): a sentence said again within 30 s."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages.retakes import find_retakes  # noqa: E402


def seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def test_exact_repeat_is_flagged() -> None:
    r = find_retakes([
        seg(0, 3, "We built the booking engine in six weeks."),
        seg(4, 7, "We built the booking engine in six weeks."),
    ])
    assert len(r) == 1
    assert (r[0]["start"], r[0]["retake_start"]) == (0, 4)
    assert r[0]["ratio"] == 1.0


def test_restart_after_a_stumble_is_flagged() -> None:
    # The first take breaks off; the second starts with the same words.
    r = find_retakes([
        seg(10, 11.5, "So the main problem"),
        seg(12, 16, "So the main problem with channel managers is latency."),
    ])
    assert len(r) == 1
    assert r[0]["text"] == "So the main problem"


def test_two_different_sentences_are_not_flagged() -> None:
    assert find_retakes([
        seg(0, 3, "We built the booking engine in six weeks."),
        seg(4, 7, "Then we spent a year on the channel manager."),
    ]) == []


def test_repeat_sixty_seconds_later_is_not_flagged() -> None:
    assert find_retakes([
        seg(0, 3, "We built the booking engine in six weeks."),
        seg(63, 66, "We built the booking engine in six weeks."),
    ]) == []


def test_punctuation_and_case_are_ignored() -> None:
    r = find_retakes([seg(0, 2, "Perché no?"), seg(3, 5, "perche NO")])
    assert len(r) == 1
