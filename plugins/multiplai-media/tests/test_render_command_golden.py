"""The ffmpeg commands render() builds for a screencast EDL must not drift.

`fixtures/demo-narrated.ffmpeg.json` was captured from `main` (0.2.5) before
the skill took the name video-edit in 0.3.0. Every later change
to composite.py adds features behind new EDL keys; an EDL that uses none of
them must still produce byte-for-byte the same ffmpeg argv. subprocess.run is
replaced, so no ffmpeg runs; the font lookup is pinned so the test does not
depend on which fonts the machine has.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite  # noqa: E402
from stages.edl import EDL  # noqa: E402

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "demo-narrated.ffmpeg.json"


class _Done:
    returncode = 0


def test_demo_narrated_ffmpeg_commands_unchanged(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        return _Done()

    monkeypatch.setattr(composite.subprocess, "run", fake_run)
    monkeypatch.setattr(composite, "_find_font",
                        lambda bold: "<FONT_BOLD>" if bold else "<FONT_REGULAR>")
    edl = EDL.load(_SCRIPTS.parent / "examples" / "demo-narrated.edl.json")
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path)

    got = [[c.replace(str(tmp_path), "<WORK>") for c in cmd] for cmd in calls]
    assert got == json.loads(_GOLDEN.read_text())


def test_single_segment_without_title_maps_the_audio_stream(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One input and no crossfade: the audio is the input's own stream, which
    # -map must name as 0:a, not as the filter label [0:a].
    calls: list[list[str]] = []
    monkeypatch.setattr(composite.subprocess, "run", lambda cmd, *a, **k: calls.append(list(cmd)) or _Done())
    edl = EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 5}]})
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path)
    final = calls[-1]
    assert final[final.index("-map", final.index("-map") + 1) + 1] == "0:a"
