"""Motion graphics (stages/overlay.py, EDL overlays, composite.overlay_graph).

Backend selection and the browser conversation run against fake `ab` /
`agent-browser` executables on PATH that log every call and write the
screenshot files; no browser starts and no ffmpeg runs.
"""
from __future__ import annotations

import base64
import json
import stat
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite, overlay  # noqa: E402
from stages.edl import EDL, Overlay, Region  # noqa: E402

PAGE = "<!doctype html><style>div{animation:a 1s forwards}@keyframes a{to{opacity:1}}</style><div>Prezzo €</div>"

# Logs argv (one JSON line per call, with stdin) and creates screenshot files.
# FAKE_MODE=denied answers every call with the host-browser refusal;
# FAKE_MODE=fail_at_screenshot_3 fails the third screenshot.
_FAKE = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "--stdin" in args else None
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"prog": os.path.basename(sys.argv[0]), "args": args, "stdin": stdin}) + "\n")
mode = os.environ.get("FAKE_MODE", "")
if mode == "denied":
    print("DENIED: host browser is not enabled on this host"); sys.exit(1)
if mode == "denied_other":
    print("DENIED: command not in allowlist"); sys.exit(1)
cmd = args[2] if len(args) > 2 else ""
if cmd == "screenshot":
    n = sum(1 for line in open(log) if '"screenshot"' in line)
    if mode == "fail_at_screenshot_3" and n == 3:
        print("✗ screenshot failed"); sys.exit(1)
    open(args[3], "wb").write(b"PNG")
    print("✓ Screenshot saved")
elif cmd == "eval":
    print('"function"' if stdin and "__seek" in stdin else "1")
else:
    print("✓ Done")
"""


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ab", "agent-browser"):
        p = bin_dir / name
        p.write_text(_FAKE)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    return {"bin": bin_dir, "log": log, "root": tmp_path}


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- backend selection -------------------------------------------------------------

def test_container_uses_ab_with_a_per_process_session(fakes, monkeypatch) -> None:
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    b = overlay.choose_backend(pid=4242)
    assert b.name == "ab"
    assert b.argv == [str(fakes["bin"] / "ab"), "--session", "video-edit-4242"]
    assert b.shared_root == fakes["root"].resolve()


def test_mac_uses_agent_browser(fakes, monkeypatch) -> None:
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    monkeypatch.setattr(overlay.sys, "platform", "darwin")
    b = overlay.choose_backend(pid=7)
    assert b.name == "agent-browser"
    assert b.argv == [str(fakes["bin"] / "agent-browser"), "--session", "video-edit-7"]
    assert b.shared_root is None


def test_other_platforms_name_agent_browser_and_its_install(fakes, monkeypatch) -> None:
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    monkeypatch.setattr(overlay.sys, "platform", "linux")
    with pytest.raises(overlay.OverlayError, match=r"agent-browser.*npm i -g agent-browser"):
        overlay.choose_backend()


def test_mac_without_agent_browser_says_how_to_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    monkeypatch.setattr(overlay.sys, "platform", "darwin")
    with pytest.raises(overlay.OverlayError, match="npm i -g agent-browser"):
        overlay.choose_backend()


def test_container_without_ab_does_not_mention_the_container(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    with pytest.raises(overlay.OverlayError) as e:
        overlay.choose_backend()
    assert "agent-browser" in str(e.value)
    assert "container" not in str(e.value).lower() and "kit" not in str(e.value).lower()


# --- the browser conversation --------------------------------------------------------

def _render(fakes, monkeypatch, duration=0.1, fps=30, **kw):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    frames = fakes["root"] / "out" / "frames"
    n = overlay.render_frames(PAGE, 640, 360, fps, duration, frames,
                              backend=overlay.choose_backend(pid=99), log=lambda s: None, **kw)
    return n, frames


def test_render_frames_drives_the_page_then_closes(fakes, monkeypatch) -> None:
    n, frames = _render(fakes, monkeypatch)
    assert n == 3
    calls = _calls(fakes["log"])
    assert all(c["prog"] == "ab" and c["args"][:2] == ["--session", "video-edit-99"] for c in calls)
    cmds = [c["args"][2:] for c in calls]
    assert cmds[0] == ["set", "viewport", "640", "360"]
    assert cmds[1] == ["open", "about:blank"]
    assert cmds[2] == ["eval", "--stdin"]
    page_b64 = base64.b64encode(PAGE.encode()).decode()
    assert page_b64 in calls[2]["stdin"] and "TextDecoder" in calls[2]["stdin"]
    assert cmds[3] == ["eval", "--stdin"] and "document.fonts.ready" in calls[3]["stdin"]
    assert cmds[4:10] == [
        ["eval", "__seek(0.000000)"], ["screenshot", str(frames / "f_00001.png")],
        ["eval", "__seek(0.033333)"], ["screenshot", str(frames / "f_00002.png")],
        ["eval", "__seek(0.066667)"], ["screenshot", str(frames / "f_00003.png")],
    ]
    assert cmds[-1] == ["close"]
    assert sorted(p.name for p in frames.iterdir()) == ["f_00001.png", "f_00002.png", "f_00003.png"]


def test_never_attaches_to_a_running_browser(fakes, monkeypatch) -> None:
    _render(fakes, monkeypatch)
    flat = [a for c in _calls(fakes["log"]) for a in c["args"]]
    for forbidden in ("connect", "9222", "--profile", "--cdp", "hb-connect.sh"):
        assert forbidden not in flat
    b = overlay.Browser(overlay.choose_backend())
    with pytest.raises(overlay.OverlayError, match="never attach"):
        b("connect", "9222")


def test_host_browser_refusal_is_passed_through_with_the_enable_command(fakes, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "denied")
    with pytest.raises(overlay.OverlayError) as e:
        _render(fakes, monkeypatch)
    msg = str(e.value)
    assert msg.startswith("DENIED: host browser is not enabled on this host")
    assert "touch ~/.local/state/multiplai/host-browser-enabled" in msg


def test_other_refusals_are_passed_through_verbatim(fakes, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "denied_other")
    with pytest.raises(overlay.OverlayError, match="^DENIED: command not in allowlist$"):
        _render(fakes, monkeypatch)


def test_session_is_closed_when_a_frame_fails(fakes, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "fail_at_screenshot_3")
    with pytest.raises(overlay.OverlayError, match="screenshot"):
        _render(fakes, monkeypatch, duration=1.0)
    assert _calls(fakes["log"])[-1]["args"][2:] == ["close"]


def test_frames_outside_the_shared_workspace_are_refused(fakes, monkeypatch, tmp_path_factory) -> None:
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(overlay.OverlayError, match="outside the shared workspace"):
        overlay.render_frames(PAGE, 640, 360, 30, 0.1, elsewhere, backend=overlay.choose_backend(),
                              log=lambda s: None)
    assert _calls(fakes["log"]) == []


def test_estimate_is_printed_before_rendering(fakes, monkeypatch) -> None:
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    lines: list[str] = []
    overlay.render_frames(PAGE, 640, 360, 30, 2.0, fakes["root"] / "f", backend=overlay.choose_backend(),
                          log=lines.append)
    assert lines[0] == "→ overlay: 60 frames at 640x360, about 19s"     # 60 × 0.32


def test_frame_count() -> None:
    assert overlay.frame_count(1.0, 30) == 30
    assert overlay.frame_count(0.1, 30) == 3
    assert overlay.frame_count(0.0, 30) == 1


def test_content_hash_covers_page_and_parameters() -> None:
    base = overlay.content_hash(PAGE, 640, 360, 30, 2.0)
    assert base == overlay.content_hash(PAGE, 640, 360, 30, 2.0)
    assert len({base,
                overlay.content_hash(PAGE + " ", 640, 360, 30, 2.0),
                overlay.content_hash(PAGE, 642, 360, 30, 2.0),
                overlay.content_hash(PAGE, 640, 360, 25, 2.0),
                overlay.content_hash(PAGE, 640, 360, 30, 2.5)}) == 5


def test_cached_overlay_is_not_rendered_again(tmp_path) -> None:
    html = tmp_path / "card.html"
    html.write_text(PAGE)
    key = overlay.content_hash(PAGE, 640, 360, 30, 2.0)
    cached = tmp_path / "cache" / "overlays" / key / "overlay.mov"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"mov")

    def explode(*a, **k):
        raise AssertionError("must not run anything on a cache hit")
    got = overlay.render_overlay(html, 640, 360, 30, 2.0, tmp_path / "cache",
                                 backend=overlay.Backend("ab", ["ab"], None), run=explode, log=lambda s: None)
    assert got == cached


def test_encode_is_prores_4444_with_alpha(tmp_path) -> None:
    calls = []
    overlay.encode_prores(tmp_path, 30, tmp_path / "o.mov", run=lambda cmd, **k: calls.append(cmd))
    cmd = calls[0]
    assert cmd[cmd.index("-c:v") + 1] == "prores_ks"
    assert cmd[cmd.index("-profile:v") + 1] == "4444"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuva444p10le"
    assert cmd[cmd.index("-i") + 1] == str(tmp_path / "f_%05d.png")


# --- EDL overlays ------------------------------------------------------------------------

def _edl(overlays: list[dict], **out) -> EDL:
    return EDL.from_dict({"source": "/rec/x.mp4", "segments": [{"src_start": 0, "src_end": 20}],
                          "output": {"width": 1080, "height": 1920, **out}, "overlays": overlays})


def _ov(**kw) -> dict:
    return {"html": "/o/card.html", "style": "clean-tech", "start": 2, "end": 6, **kw}


def test_overlays_parse() -> None:
    e = _edl([_ov(mode="panel", region={"x": 90, "y": 520, "w": 900, "h": 560}),
              _ov(start=7, end=9, key_color="#0000FF")])
    assert e.overlays[0] == Overlay("/o/card.html", 2, 6, "panel", Region(90, 520, 900, 560), "#00FF00", "clean-tech")
    assert e.overlays[1].rect(1080, 1920) == Region(0, 0, 1080, 1920)
    assert e.validate() == []


@pytest.mark.parametrize("ov,match", [
    (_ov(start=15, end=21), "within the video"),
    (_ov(start=-1, end=3), "within the video"),
    (_ov(start=5, end=5), "within the video"),
    (_ov(mode="floating"), "mode"),
    (_ov(region={"x": 0, "y": 1800, "w": 1080, "h": 200}), "not inside"),
    (_ov(region={"x": 0, "y": 0, "w": 1081, "h": 200}), "not inside"),
    (_ov(region={"x": 0, "y": 0, "w": 501, "h": 200}), "even"),
    (_ov(key_color="green"), "key_color"),
])
def test_invalid_overlays_are_rejected(ov, match) -> None:
    with pytest.raises(ValueError, match=match):
        _edl([ov]).validate()


def test_overlays_overlapping_in_time_and_place_are_rejected() -> None:
    a = _ov(region={"x": 0, "y": 0, "w": 1080, "h": 600})
    b = _ov(start=5, end=9, region={"x": 0, "y": 500, "w": 1080, "h": 600})
    with pytest.raises(ValueError, match="overlays 0 and 1 cover the same part of the frame.*5.00–6.00s"):
        _edl([a, b]).validate()


def test_overlays_in_separate_regions_or_times_are_fine() -> None:
    top = _ov(region={"x": 0, "y": 0, "w": 1080, "h": 600})
    bottom = _ov(region={"x": 0, "y": 1400, "w": 1080, "h": 300})
    later = _ov(start=6, end=9, region={"x": 0, "y": 0, "w": 1080, "h": 600})
    assert _edl([top, bottom, later]).validate() == []


def test_overlay_end_uses_the_rendered_duration_with_title_and_crossfades() -> None:
    e = EDL.from_dict({"source": "/rec/x.mp4", "title": {"line1": "T", "duration": 3},
                       "segments": [{"src_start": 0, "src_end": 10}, {"src_start": 20, "src_end": 30}],
                       "overlays": [_ov(start=0, end=22.0)]})
    # 3 s title + 10 + 10, minus a 0.5 s crossfade at each of the two joins
    assert e.output_duration() == pytest.approx(22.0)
    e.validate()
    e.overlays[0].end = 22.1
    with pytest.raises(ValueError, match="within the video"):
        e.validate()


def test_overlay_without_style_warns() -> None:
    o = _ov()
    del o["style"]
    assert any("names no style" in w for w in _edl([o]).validate())


# --- filter graph --------------------------------------------------------------------------

def test_panel_and_keyed_filter_strings() -> None:
    e = _edl([_ov(start=10, end=17, mode="panel", region={"x": 90, "y": 520, "w": 900, "h": 560}),
              _ov(start=11, end=16, mode="keyed", region={"x": 0, "y": 1500, "w": 1080, "h": 300})])
    graph, label = composite.overlay_graph(e.overlays, [Path("p.mov"), Path("k.mov")], 1, "vsub0", 1080, 1920)
    assert graph == (
        "[1:v]setpts=PTS-STARTPTS+10/TB[ov0]; "
        "[vsub0][ov0]overlay=90:520:enable='between(t,10,17)':eof_action=pass[vov0]; "
        "[2:v]setpts=PTS-STARTPTS+11/TB,colorkey=0x00FF00:0.3:0.08,despill=type=green[ov1]; "
        "[vov0][ov1]overlay=0:1500:enable='between(t,11,16)':eof_action=pass[vov1]")
    assert label == "vov1"


@pytest.mark.parametrize("key,spill", [("#00FF00", "green"), ("#0047BB", "blue"), ("#FF00FF", None)])
def test_despill_follows_the_key_colour(key, spill) -> None:
    assert composite._despill_type(key) == spill
    e = _edl([_ov(key_color=key)])
    graph, _ = composite.overlay_graph(e.overlays, [Path("k.mov")], 1, "v", 1080, 1920)
    assert (f"despill=type={spill}" in graph) if spill else ("despill" not in graph)


class _Done:
    returncode = 0


def test_render_adds_overlay_inputs_after_the_segments(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(composite.subprocess, "run", lambda cmd, *a, **k: calls.append([str(c) for c in cmd]) or _Done())
    monkeypatch.setattr(composite, "_render_overlays", lambda edl: [Path("/c/p.mov"), Path("/c/k.mov")])
    e = EDL.from_dict({"source": "/rec/x.mp4",
                       "segments": [{"src_start": 0, "src_end": 10}, {"src_start": 20, "src_end": 30}],
                       "overlays": [_ov(mode="panel", region={"x": 0, "y": 0, "w": 640, "h": 360}),
                                    _ov(start=7, end=9)]})
    composite.render(e, Path("<OUT>/o.mp4"), work_dir=tmp_path)
    final = calls[-1]
    inputs = [final[i + 1] for i, a in enumerate(final) if a == "-i"]
    assert inputs[-2:] == ["/c/p.mov", "/c/k.mov"]
    fc = final[final.index("-filter_complex") + 1]
    assert "[2:v]setpts=PTS-STARTPTS+2/TB[ov0]" in fc and "[3:v]setpts=PTS-STARTPTS+7/TB,colorkey" in fc
    assert final[final.index("-map") + 1] == "[vov1]"
