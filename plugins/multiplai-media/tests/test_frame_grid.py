"""pipeline.py frame --grid: a 100 px grid labelled in source pixels."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402


def test_grid_labels_every_100px(monkeypatch) -> None:
    monkeypatch.setattr(pipeline.composite, "_find_font", lambda bold: "/f.ttf")
    vf = pipeline.grid_filter(1920, 1080)
    assert vf.startswith("drawgrid=w=100:h=100:t=1:c=yellow@0.7,")
    assert vf.count("drawtext=") == 19 + 10          # x = 100..1900, y = 100..1000
    assert "text=1900:x=1903:y=3" in vf and "text=1000:x=3:y=1003" in vf
