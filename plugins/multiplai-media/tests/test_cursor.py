"""Finding the pointer's clicks in a screen recording (stages/cursor.py), on a
synthetic recording: a pointer crosses a white page, rests, and a button
under it changes. Skipped where OpenCV is not installed (the media suite
runs without the workspace environment)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import cursor

W, H, FPS = 1280, 720, 10  # proxy size: the pointer must be a small part of the frame
ARROW = np.array(
    [[0, 0], [0, 14], [4, 10], [7, 16], [9, 15], [6, 9], [11, 9]], np.int32
)


def _pointer(img, x: int, y: int) -> None:
    pts = ARROW + [x, y]
    cv2.fillPoly(img, [pts], 255)
    cv2.polylines(img, [pts], True, 255, 3)  # the light border
    cv2.fillPoly(img, [pts], 0)


def _recording(path: Path) -> tuple[int, int]:
    """3 s: the pointer moves for 2 s, rests on a button, and the button
    turns grey 0.5 s later. Returns where it rests."""
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    rest = (420, 200)
    for i in range(30):
        img = np.full((H, W), 245, np.uint8)
        if i >= 25:
            cv2.rectangle(img, (380, 185), (480, 225), 150, -1)
        x = 60 + i * 18 if i < 20 else rest[0]
        y = 60 + i * 7 if i < 20 else rest[1]
        _pointer(img, x, y)
        out.write(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    out.release()
    return rest


def test_a_click_is_found_where_the_pointer_rests(tmp_path: Path) -> None:
    rest = _recording(tmp_path / "rec.mp4")
    found = cursor.find(tmp_path / "rec.mp4")
    assert found["pictures"] >= 1
    assert len(found["clicks"]) == 1
    c = found["clicks"][0]
    assert 2.0 <= c["t"] <= 3.0
    assert abs(c["x"] * W - (rest[0] + 5)) < 15 and abs(c["y"] * H - (rest[1] + 8)) < 15
    assert c["sure"]


def test_a_marked_pointer_becomes_a_picture(tmp_path: Path) -> None:
    rest = _recording(tmp_path / "rec.mp4")
    looks = cursor.marked(
        tmp_path / "rec.mp4", [(2.2, (rest[0] + 8) / W, (rest[1] + 10) / H)]
    )
    assert len(looks) == 1
    picture, outline = looks[0]
    assert picture.shape == outline.shape == (cursor.PATCH, cursor.PATCH)
