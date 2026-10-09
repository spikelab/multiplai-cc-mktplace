"""What render() adds to the ffmpeg commands for a reel EDL.

Captions and a headline burn in as a subtitles filter on the final video
label; a caption or headline character the font cannot draw stops the render;
output.audio_rate resamples the final audio; a brand file supplies the logo
and the background. subprocess.run and ffprobe are replaced, so no ffmpeg
runs, and the font lookups are pinned so the tests do not depend on the
machine's fonts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import captions, composite  # noqa: E402
from stages.edl import EDL  # noqa: E402

_LOGO = Path(__file__).resolve().parent / "fixtures" / "brand" / "logo.png"
_FONT = "/fonts/Caption-Bold.ttf"


class _Done:
    returncode = 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    got: list[list[str]] = []
    monkeypatch.setattr(composite.subprocess, "run",
                        lambda cmd, *a, **k: got.append([str(c) for c in cmd]) or _Done())
    monkeypatch.setattr(composite, "probe_size", lambda path: (1920, 1080))
    monkeypatch.setattr(composite, "_find_font", lambda bold: _FONT)
    monkeypatch.setattr(captions, "font_family", lambda font_file: "Caption")
    # The font draws Latin only (code points below U+0250).
    monkeypatch.setattr(captions, "_charset", lambda font_file: set(range(0x250)))
    return got


def _transcript(tmp_path: Path, texts: list[str]) -> str:
    words = [{"text": t, "start": 1.0 + i * 0.4, "end": 1.3 + i * 0.4} for i, t in enumerate(texts)]
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps({"language": "it", "engine": "test", "words": words}))
    return str(p)


def _reel(tmp_path: Path, **extra) -> EDL:
    d = {"source": "/rec/interview.mp4",
         "segments": [{"src_start": 0, "src_end": 5}, {"src_start": 10, "src_end": 15}],
         "output": {"width": 1080, "height": 1920, "fit": "blur"}}
    d.update(extra)
    return EDL.from_dict(d)


def _final(calls: list[list[str]]) -> list[str]:
    assert calls and calls[-1][0] == "ffmpeg"
    return calls[-1]


def _maps(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, c in enumerate(cmd) if c == "-map"]


def test_captions_and_headline_burn_into_the_mapped_video(tmp_path, calls) -> None:
    edl = _reel(tmp_path, transcript=_transcript(tmp_path, ["Ciao", "a", "tutti."]),
                captions={}, headline={"text": "Hook"})
    work = tmp_path / "work"
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=work)

    final = _final(calls)
    fc = final[final.index("-filter_complex") + 1]
    # Two segments, no title: build_filter_complex's video ends at [vx1].
    assert fc.endswith(f"; [vx1]subtitles=filename='{work}/subs.ass':fontsdir='/fonts'[vsub]")
    assert _maps(final)[0] == "[vsub]"
    ass = (work / "subs.ass").read_text()
    assert "Ciao" in ass and "tutti." in ass
    assert ",Headline,,0,0,0,," in ass and ass.rstrip().endswith("Hook")


def test_without_captions_or_headline_no_subtitles_filter(tmp_path, calls) -> None:
    composite.render(_reel(tmp_path), Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    final = _final(calls)
    assert "subtitles=" not in final[final.index("-filter_complex") + 1]
    assert _maps(final)[0] == "[vx1]"


def test_caption_glyph_the_font_lacks_stops_the_render(tmp_path, calls) -> None:
    edl = _reel(tmp_path, transcript=_transcript(tmp_path, ["你好", "Ciao"]), captions={})
    with pytest.raises(ValueError, match="no glyph for: 你 好"):
        composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    assert calls == []          # nothing was cut or encoded


def test_headline_glyph_the_font_lacks_stops_the_render(tmp_path, calls) -> None:
    edl = _reel(tmp_path, headline={"text": "Привет"})
    with pytest.raises(ValueError, match="no glyph for: .*П"):
        composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    assert calls == []


def test_audio_rate_resamples_the_final_audio(tmp_path, calls) -> None:
    edl = _reel(tmp_path)
    edl.output.audio_rate = 48000
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    final = _final(calls)
    assert final[final.index("-ar") + 1] == "48000"
    assert final.index("-ar") < final.index("<OUT>/reel.mp4")


def test_no_audio_rate_leaves_the_sample_rate_alone(tmp_path, calls) -> None:
    composite.render(_reel(tmp_path), Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    assert "-ar" not in _final(calls)


def test_brand_supplies_the_logo_and_the_stack_background(tmp_path, calls) -> None:
    brand_file = tmp_path / "brand.json"
    brand_file.write_text(json.dumps({
        "background": "#1B2A41",
        "logo": {"path": str(_LOGO), "position": "tl", "scale": 0.18},
    }))
    edl = _reel(tmp_path, brand=str(brand_file),
                layout={"panels": {"A": {"x": 36, "y": 200, "w": 920, "h": 744},
                                   "B": {"x": 966, "y": 200, "w": 918, "h": 744}}},
                segments=[{"src_start": 0, "src_end": 5, "frame": "stack"},
                          {"src_start": 10, "src_end": 15}])
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")

    seg0_fc = calls[0][calls[0].index("-filter_complex") + 1]
    assert "vstack=inputs=2,pad=1080:1920:" in seg0_fc
    assert seg0_fc.split("vstack=inputs=2,")[1].split("[vfit]")[0].endswith("color=#1B2A41,setsar=1")
    final = _final(calls)
    inputs = [final[i + 1] for i, c in enumerate(final) if c == "-i"]
    assert inputs[-1] == str(_LOGO.resolve())
    fc = final[final.index("-filter_complex") + 1]
    assert "[2:v]format=rgba,scale=194:-1[lg]" in fc
    assert "[vx1][lg]overlay=" in fc
    assert _maps(final)[0] == "[vout]"


def test_an_edl_logo_wins_over_the_brand_logo(tmp_path, calls) -> None:
    brand_file = tmp_path / "brand.json"
    brand_file.write_text(json.dumps({"logo": {"path": str(_LOGO), "position": "tl"}}))
    other = tmp_path / "other.png"
    other.write_bytes(_LOGO.read_bytes())
    edl = _reel(tmp_path, brand=str(brand_file), logo={"path": str(other)})
    composite.render(edl, Path("<OUT>/reel.mp4"), work_dir=tmp_path / "work")
    final = _final(calls)
    inputs = [final[i + 1] for i, c in enumerate(final) if c == "-i"]
    assert inputs[-1] == str(other)
    assert str(_LOGO.resolve()) not in inputs
