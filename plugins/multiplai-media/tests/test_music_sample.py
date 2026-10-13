"""`pipeline.py music-sample`: a short sample of a synth bed, for music.json."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
PIPELINE = _SCRIPTS / "pipeline.py"
sys.path.insert(0, str(_SCRIPTS))

from stages import music  # noqa: E402


def _fake_ffmpeg(bin_dir: Path, log: Path) -> None:
    """An ffmpeg that records its argv and writes its last argument (the output)."""
    bin_dir.mkdir()
    f = bin_dir / "ffmpeg"
    f.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\nfor a; do out="$a"; done\n: > "$out"\n')
    f.chmod(0o755)


def _run(args: list[str], path: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PIPELINE), "music-sample", *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": path}, timeout=60)


def test_writes_the_preset_a_render_uses_for_the_sample_length(tmp_path: Path) -> None:
    log = tmp_path / "argv"
    _fake_ffmpeg(tmp_path / "bin", log)
    out = tmp_path / "job" / "samples" / "warm.wav"
    proc = _run(["warm", "--out", str(out)], f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"SAMPLE: {out}"
    argv = log.read_text().splitlines()
    assert "anoisesrc=color=pink:duration=15.00:amplitude=0.35" in argv
    assert argv[argv.index("-af") + 1] == music.SYNTH_PRESETS["warm"]
    assert argv[-1] == str(out) and out.exists()            # the samples/ folder is created


def test_seconds_must_be_positive_and_the_preset_known(tmp_path: Path) -> None:
    _fake_ffmpeg(tmp_path / "bin", tmp_path / "argv")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    proc = _run(["calm", "--out", str(tmp_path / "x.wav"), "--seconds", "0"], path)
    assert proc.returncode == 1 and "seconds must be positive" in proc.stderr
    proc = _run(["calm", "--out", str(tmp_path / "x.wav"), "--seconds", "inf"], path)
    assert proc.returncode == 1 and "seconds must be positive and finite" in proc.stderr
    proc = _run(["jazz", "--out", str(tmp_path / "x.wav")], path)
    assert proc.returncode == 2 and "invalid choice" in proc.stderr


def test_a_missing_or_failing_ffmpeg_is_one_line_and_exit_1(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _run(["calm", "--out", str(tmp_path / "x.wav")], str(empty))
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert proc.stderr.strip() == "✗ ffmpeg not found on PATH. Install it (brew install ffmpeg, or apt install ffmpeg) and try again."
    failing = tmp_path / "failing"
    failing.mkdir()
    (failing / "ffmpeg").write_text("#!/bin/sh\nexit 3\n")
    (failing / "ffmpeg").chmod(0o755)
    proc = _run(["calm", "--out", str(tmp_path / "x.wav")], f"{failing}:{os.environ['PATH']}")
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert proc.stderr.strip() == "✗ ffmpeg could not write the sample (exit 3)"


def test_a_render_still_synthesizes_its_bed_five_seconds_long(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(music, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(music.subprocess, "run", lambda argv, check: calls.append(argv))
    music._synthesize_bed("calm", 30.0)
    assert "anoisesrc=color=pink:duration=35.00:amplitude=0.35" in calls[0]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe")
def test_with_real_ffmpeg_the_sample_is_as_long_as_asked(tmp_path: Path) -> None:
    out = tmp_path / "calm.wav"
    proc = _run(["calm", "--out", str(out), "--seconds", "2"], os.environ["PATH"])
    assert proc.returncode == 0, proc.stderr
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True, check=True).stdout
    assert abs(float(dur) - 2.0) < 0.1
