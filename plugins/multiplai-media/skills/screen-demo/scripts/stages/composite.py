from __future__ import annotations
import shlex
import subprocess
from pathlib import Path
from stages.edl import EDL

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

POSITIONS = {
    "br": "x=W-w-{m}:y=H-h-{m}",
    "bl": "x={m}:y=H-h-{m}",
    "tr": "x=W-w-{m}:y={m}",
    "tl": "x={m}:y={m}",
}


def _atempo_chain(speed: float) -> str:
    if speed == 1.0:
        return "anull"
    parts: list[str] = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.6f}")
    return ",".join(parts)


def _zoom_filter(zoom, W: int, H: int) -> str:
    s = zoom.scale
    crop_w = f"iw/{s}"
    crop_h = f"ih/{s}"
    x = f"(iw-iw/{s})*{zoom.x}"
    y = f"(ih-ih/{s})*{zoom.y}"
    return f"crop={crop_w}:{crop_h}:{x}:{y},scale={W}:{H}"


def _cut_segments(edl: EDL, work: Path) -> list[Path]:
    out = []
    W, H, fps = edl.output.width, edl.output.height, edl.output.fps
    for i, seg in enumerate(edl.segments):
        p = work / f"seg{i:02d}.mp4"
        mute = seg.mute or seg.speed > 4.0
        out_dur = seg.duration

        vfilters = [
            f"trim=duration={seg.src_duration}",
            "setpts=PTS-STARTPTS",
        ]
        if seg.zoom:
            vfilters.append(_zoom_filter(seg.zoom, W, H))
        vfilters.append(
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#0a0a0a,setsar=1"
        )
        if seg.speed != 1.0:
            vfilters.append(f"setpts=PTS/{seg.speed}")
        vfilters.append(f"fps={fps}")
        vfilters.append(f"trim=duration={out_dur}")
        vchain = ",".join(vfilters)

        if mute:
            filter_complex = (
                f"[0:v]{vchain}[v];"
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={out_dur},asetpts=PTS-STARTPTS[a]"
            )
        else:
            afilters = [
                f"atrim=duration={seg.src_duration}",
                "asetpts=PTS-STARTPTS",
            ]
            if seg.speed != 1.0:
                afilters.append(_atempo_chain(seg.speed))
            afilters.append(f"atrim=duration={out_dur}")
            achain = ",".join(afilters)
            filter_complex = f"[0:v]{vchain}[v]; [0:a]{achain}[a]"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(seg.src_start), "-t", str(seg.src_duration + 1),
            "-i", edl.source,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(edl.output.crf),
            "-c:a", "aac", "-b:a", edl.output.audio_bitrate,
            "-pix_fmt", "yuv420p", str(p),
        ]
        subprocess.run(cmd, check=True)
        out.append(p)
    return out


def _render_title(edl: EDL, work: Path) -> Path | None:
    if not edl.title:
        return None
    p = work / "title.mp4"
    w, h, fps = edl.output.width, edl.output.height, edl.output.fps
    drawtext = [
        f"drawtext=fontfile={FONT_BOLD}:text={_esc(edl.title.line1)}:fontcolor=white:"
        f"fontsize={int(h*0.08)}:x=(w-tw)/2:y=(h-th)/2-{int(h*0.05)}",
    ]
    if edl.title.line2:
        drawtext.append(
            f"drawtext=fontfile={FONT_REG}:text={_esc(edl.title.line2)}:fontcolor=#9ca3af:"
            f"fontsize={int(h*0.036)}:x=(w-tw)/2:y=(h-th)/2+{int(h*0.055)}"
        )
    vf = ",".join(drawtext)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=#0a0a0a:s={w}x{h}:r={fps}:d={edl.title.duration}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", str(edl.title.duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(edl.output.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", edl.output.audio_bitrate,
        str(p),
    ]
    subprocess.run(cmd, check=True)
    return p


def _esc(s: str) -> str:
    return "'" + s.replace("'", "\\'").replace(":", "\\:") + "'"


def build_filter_complex(
    edl: EDL, clips: list[Path], title: Path | None
) -> tuple[str, list[str], str, str]:
    inputs: list[Path] = []
    if title:
        inputs.append(title)
    inputs.extend(clips)

    n = len(inputs)
    parts = []
    for i in range(n):
        parts.append(f"[{i}:v]fps={edl.output.fps},settb=AVTB,format=yuv420p[v{i}]")

    cur_v = "v0"
    cur_off = 0.0
    if title:
        cur_off = edl.title.duration  # type: ignore[union-attr]

    seg_offset_base = 1 if title else 0
    for i, seg in enumerate(edl.segments):
        next_label = f"v{i+seg_offset_base}"
        if i == 0 and not title:
            cur_v = next_label
            cur_off = seg.duration
            continue
        xfade_dur = _xfade_duration_for(edl, i)
        offset = cur_off - xfade_dur
        out = f"vx{i}"
        parts.append(f"[{cur_v}][{next_label}]xfade=transition=fade:duration={xfade_dur}:offset={offset}[{out}]")
        cur_v = out
        cur_off = offset + seg.duration

    audio_parts = []
    last_a = "0:a"
    for i in range(n - 1):
        xfade_dur = 0.5
        a_out = f"ax{i}"
        if i == 0:
            audio_parts.append(f"[0:a][{i+1}:a]acrossfade=d={xfade_dur}[{a_out}]")
        else:
            audio_parts.append(f"[{last_a}][{i+1}:a]acrossfade=d={xfade_dur}[{a_out}]")
        last_a = a_out
    if not audio_parts:
        last_a = "0:a"

    filter_lines = parts + audio_parts
    video_label = cur_v
    audio_label = last_a

    if edl.logo:
        logo_index = len(inputs)
        start_at = edl.logo.start_at if edl.logo.start_at is not None else (edl.title.duration if edl.title else 0.0)
        target_w = int(edl.output.width * edl.logo.scale)
        pos = POSITIONS[edl.logo.position].format(m=int(edl.output.width * 0.02))
        filter_lines.append(f"[{logo_index}:v]format=rgba,scale={target_w}:-1[lg]")
        filter_lines.append(f"[{video_label}][lg]overlay={pos}:enable='gt(t,{start_at})'[vout]")
        video_label = "vout"

    filter_complex = "; ".join(filter_lines)

    cmd_inputs: list[str] = []
    for p in inputs:
        cmd_inputs.extend(["-i", str(p)])
    if edl.logo:
        cmd_inputs.extend(["-i", edl.logo.path])

    return filter_complex, cmd_inputs, video_label, audio_label


def _xfade_duration_for(edl: EDL, segment_index: int) -> float:
    for t in edl.transitions:
        if t.after == segment_index - 1:
            return t.duration
    return 0.5


def render(edl: EDL, out_path: Path, work_dir: Path | None = None) -> Path:
    work = work_dir or Path("/tmp/screen-demo-work")
    work.mkdir(parents=True, exist_ok=True)

    clips = _cut_segments(edl, work)
    title = _render_title(edl, work)
    filter_complex, cmd_inputs, vlabel, alabel = build_filter_complex(edl, clips, title)

    music_path = _resolve_music(edl, work)
    if music_path:
        total_dur = edl.total_duration()
        vol = 10 ** (edl.music.volume_db / 20)  # type: ignore[union-attr]
        music_idx = len(cmd_inputs) // 2          # cmd_inputs is [-i, path] pairs
        cmd_inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
        fade_in = 1.0
        fade_out = 2.0
        # Bed: loudnorm so any source lands at predictable RMS, then attenuate.
        # Sidechain-compress the bed against the narration so it ducks under speech
        # and rises during silent (sped-up) sections.
        bed = (
            f"[{music_idx}:a]atrim=duration={total_dur},asetpts=PTS-STARTPTS,"
            f"loudnorm=I=-16:TP=-1.5:LRA=9,"
            f"afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={total_dur-fade_out}:d={fade_out},"
            f"volume={vol:.4f},aformat=channel_layouts=stereo:sample_rates=48000[bed_pre]"
        )
        sidechain = (
            f"[{alabel}]asplit=2[narr_trigger][narr_mix]; "
            f"[bed_pre][narr_trigger]sidechaincompress="
            f"threshold=0.04:ratio=8:attack=8:release=400:makeup=4[bed_ducked]"
        )
        mix = (
            f"[narr_mix][bed_ducked]amix=inputs=2:duration=first:"
            f"dropout_transition=0:normalize=0[amix]"
        )
        filter_complex = filter_complex + "; " + bed + "; " + sidechain + "; " + mix
        alabel = "amix"

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *cmd_inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(edl.output.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", edl.output.audio_bitrate,
        "-movflags", "+faststart",
        str(out_path),
    ]
    print("→ ffmpeg composite:", " ".join(shlex.quote(c) for c in cmd[:8]), "...")
    subprocess.run(cmd, check=True)
    return out_path


def _resolve_music(edl: EDL, _work: Path) -> Path | None:
    if not edl.music:
        return None
    from stages import music
    if edl.music.file:
        return music.resolve(edl.music.file, None)
    if edl.music.url:
        return music.resolve(None, edl.music.url)
    if edl.music.synth:
        return music.resolve(None, None, synth=edl.music.synth,
                             synth_duration=edl.total_duration())
    if edl.music.prompt:
        print("⚠ music prompt → ACE-Step generation not available in this container.")
        print(music.generate_note(edl.music.prompt))
        print("→ falling back to synth='calm' (ffmpeg pink-noise bed)")
        return music.resolve(None, None, synth="calm",
                             synth_duration=edl.total_duration())
    return None
