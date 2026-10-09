"""`pipeline.py render` publishes the output only once ffmpeg has finished.

The review page lists every video in its folder every 5 s, so the render
goes to a hidden partial file that is renamed into place on success and
removed on failure. composite.render is replaced, so no ffmpeg runs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402


def _args(tmp_path: Path) -> argparse.Namespace:
    edl = tmp_path / "edl.json"
    edl.write_text(json.dumps({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 5}]}))
    return argparse.Namespace(edl=str(edl), out=str(tmp_path / "out" / "clip-01.v2.mp4"),
                              work_dir=None, music_file=None, music_url=None,
                              music_synth=None, music_volume_db=None)


def test_render_writes_a_hidden_partial_then_renames_it(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def fake_render(edl, out_path, work_dir=None):
        seen.append(out_path)
        assert not (tmp_path / "out" / "clip-01.v2.mp4").exists()
        out_path.write_bytes(b"video")
        return out_path

    monkeypatch.setattr(pipeline.composite, "render", fake_render)
    assert pipeline.cmd_render(_args(tmp_path)) == 0
    assert seen == [tmp_path / "out" / ".clip-01.v2.partial.mp4"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["clip-01.v2.mp4"]


def test_failed_render_leaves_no_output(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_render(edl, out_path, work_dir=None):
        out_path.write_bytes(b"half")
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(pipeline.composite, "render", fake_render)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.cmd_render(_args(tmp_path))
    assert list((tmp_path / "out").iterdir()) == []
