"""Sound effects: sound files, EDL checks, the mix, and sfx-suggest.

The mix tests replace subprocess.run, as the golden test does, so no ffmpeg
runs; they read the final composite command render() would have run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402
from stages import composite, sfx  # noqa: E402
from stages.edl import EDL, SFX_MIN_GAP_S  # noqa: E402


class _Done:
    returncode = 0


def _edl(**extra) -> EDL:
    return EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 30}], **extra})


@pytest.fixture(autouse=True)
def _sounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """click.wav, pop.wav, tick.wav and whoosh.wav in the working directory:
    no sounds ship with the skill, so every test supplies its own files."""
    for name in ("click", "pop", "tick", "whoosh"):
        (tmp_path / f"{name}.wav").write_bytes(b"RIFF")
    monkeypatch.chdir(tmp_path)


# --- sound files --------------------------------------------------------------

def test_no_sounds_ship_with_the_skill() -> None:
    assert not (_SCRIPTS.parent / "assets" / "sfx").exists()


def test_a_users_own_file_resolves_to_its_path(tmp_path: Path) -> None:
    own = tmp_path / "ding.wav"
    own.write_bytes(b"RIFF")
    assert sfx.resolve(str(own)) == own.resolve()


def test_a_bare_name_is_not_a_sound() -> None:
    with pytest.raises(FileNotFoundError, match="click.*a sound is a path to an audio file"):
        sfx.resolve("click")


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.wav"):
        sfx.resolve(str(tmp_path / "nope.wav"))


# --- EDL checks ---------------------------------------------------------------

def test_sounds_inside_the_video_and_apart_are_accepted() -> None:
    edl = _edl(sfx=[{"at": 1.0, "sound": "click.wav"}, {"at": 1.5, "sound": "pop.wav", "volume_db": -20}])
    assert edl.validate() == []
    assert edl.sfx[0].volume_db == -14.0 and edl.sfx[1].volume_db == -20


@pytest.mark.parametrize("at", [-0.1, 30.0, 45.0])
def test_a_sound_outside_the_video_is_refused(at: float) -> None:
    with pytest.raises(ValueError, match=r"sfx 0 \(click.wav\).*within the video \(0–30.00s\)"):
        _edl(sfx=[{"at": at, "sound": "click.wav"}]).validate()


def test_two_sounds_too_close_name_both_times() -> None:
    edl = _edl(sfx=[{"at": 5.0, "sound": "click.wav"}, {"at": 2.0, "sound": "pop.wav"}, {"at": 5.3, "sound": "tick.wav"}])
    with pytest.raises(ValueError, match=r"at 5.0s and 5.3s are 0.30s apart.*at least 0.5s"):
        edl.validate()


def test_a_missing_sound_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sfx 0: sound file not found"):
        _edl(sfx=[{"at": 1.0, "sound": str(tmp_path / "gone.wav")}]).validate()


def test_more_than_one_sound_every_three_seconds_warns() -> None:
    busy = _edl(sfx=[{"at": float(t), "sound": "click.wav"} for t in range(0, 30, 2)])   # 15 in 30 s
    calm = _edl(sfx=[{"at": float(t), "sound": "click.wav"} for t in range(0, 30, 3)])   # 10 in 30 s
    assert any("sound busy" in w for w in busy.validate())
    assert calm.validate() == []


# --- the mix ------------------------------------------------------------------

def _final_command(edl: EDL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[list[str]] = []
    monkeypatch.setattr(composite.subprocess, "run", lambda cmd, *a, **k: calls.append(list(cmd)) or _Done())
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path)
    return calls[-1]


def test_sounds_are_mixed_after_the_ducked_music(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bed = tmp_path / "bed.wav"
    bed.write_bytes(b"RIFF")
    edl = EDL.from_dict({
        "source": "/rec/x.mp4",
        "segments": [{"src_start": 0, "src_end": 10}, {"src_start": 20, "src_end": 30}],
        "music": {"file": str(bed)},
        "sfx": [{"at": 1.25, "sound": "click.wav"}, {"at": 12.0, "sound": "pop.wav", "volume_db": -20}],
    })
    cmd = _final_command(edl, tmp_path, monkeypatch)
    graph = cmd[cmd.index("-filter_complex") + 1]
    inputs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-i"]
    # two segments, the music bed, then the two sounds
    assert inputs[2] == str(bed)
    assert inputs[3:] == [str(sfx.resolve("click.wav")), str(sfx.resolve("pop.wav"))]
    assert "[3:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay=delays=1250:all=1,volume=-14.0dB[sfx0]" in graph
    assert "[4:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay=delays=12000:all=1,volume=-20dB[sfx1]" in graph
    assert "[amix][sfx0][sfx1]amix=inputs=3:duration=first:dropout_transition=0:normalize=0," \
           "alimiter=limit=0.95:level=0[afx]" in graph
    assert graph.index("sidechaincompress") < graph.index("adelay")
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[afx]"


def test_without_music_the_sounds_mix_into_the_narration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _final_command(_edl(sfx=[{"at": 2.0, "sound": "tick.wav"}]), tmp_path, monkeypatch)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a][sfx0]amix=inputs=2" in graph and "[1:a]aformat" in graph
    assert "sidechaincompress" not in graph


def test_an_edl_without_sfx_builds_no_sound_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The byte-for-byte check is test_render_command_golden.py; this one says why it holds.
    edl = EDL.load(_SCRIPTS.parent / "examples" / "demo-narrated.edl.json")
    monkeypatch.setattr(composite, "_find_font", lambda bold: "<FONT>")
    assert edl.sfx == []
    graph = " ".join(_final_command(edl, tmp_path, monkeypatch))
    assert "adelay" not in graph and "alimiter" not in graph


# --- sfx-suggest --------------------------------------------------------------

def test_suggest_maps_a_sped_up_target_and_drops_close_neighbours() -> None:
    edl = EDL.from_dict({
        "source": "/rec/x.mp4",
        "segments": [
            {"src_start": 0, "src_end": 10, "follow": {"targets": [{"t": 2.0}, {"t": 9.8}]}},
            # 0.5 s crossfade, so this segment starts at 9.5 s; 2x, so source 24 is 9.5 + 4/2
            {"src_start": 20, "src_end": 30, "speed": 2.0, "follow": {"targets": [{"t": 24.0}]}},
            # muted (above 4x): its target has no output time
            {"src_start": 40, "src_end": 80, "speed": 8.0, "follow": {"targets": [{"t": 50.0}]}},
        ],
        "overlays": [{"html": "a.html", "start": 11.7, "end": 13}, {"html": "b.html", "start": 5, "end": 6}],
    })
    kept, notes = sfx.suggest(edl, "click.wav", "pop.wav")
    assert kept == [{"at": 2.0, "sound": "click.wav"}, {"at": 5, "sound": "pop.wav"},
                    {"at": 9.8, "sound": "click.wav"}, {"at": 11.5, "sound": "click.wav"}]
    assert any("skipped click.wav for segment 2 target at source 50.0s" in n for n in notes)
    assert any(n.startswith("dropped pop.wav at 11.700s (overlay 0 (a.html))") and "click.wav at 11.500s" in n
               for n in notes)
    assert all(b["at"] - a["at"] >= SFX_MIN_GAP_S for a, b in zip(kept, kept[1:]))


def test_sfx_suggest_prints_a_pasteable_list(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "edl.json"
    path.write_text(json.dumps({
        "source": "/rec/x.mp4",
        "segments": [{"src_start": 100, "src_end": 130,
                      "follow": {"targets": [{"t": 101}, {"t": 110}, {"t": 120}]}}],
        "overlays": [{"html": "a.html", "start": 4, "end": 6}, {"html": "b.html", "start": 15, "end": 17}],
    }))
    args = argparse.Namespace(edl=str(path), clicks="tick.wav", overlays="whoosh.wav")
    assert pipeline.cmd_sfx_suggest(args) == 0
    out = json.loads(capsys.readouterr().out)
    tick, whoosh = str((tmp_path / "tick.wav").resolve()), str((tmp_path / "whoosh.wav").resolve())
    assert out == [{"at": 1.0, "sound": tick}, {"at": 4, "sound": whoosh}, {"at": 10.0, "sound": tick},
                   {"at": 15, "sound": whoosh}, {"at": 20.0, "sound": tick}]
    edl = EDL.from_dict({**json.loads(path.read_text()), "overlays": [], "sfx": out})
    assert edl.validate() == []


def test_sfx_suggest_refuses_a_missing_sound_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "edl.json"
    path.write_text(json.dumps({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 30}]}))
    args = argparse.Namespace(edl=str(path), clicks="click", overlays="pop.wav")
    assert pipeline.cmd_sfx_suggest(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "sound file not found" in captured.err
