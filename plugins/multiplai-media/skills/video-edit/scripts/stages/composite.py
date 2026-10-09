from __future__ import annotations
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from stages import brand as brand_mod, layouts
from stages.edl import EDL, Logo, Overlay


def _find_font(bold: bool) -> str:
    """Locate a usable TTF/TTC for ffmpeg drawtext across platforms.

    Honors $VIDEO_EDIT_FONT (applied to both weights; the old name
    $SCREEN_DEMO_FONT is still read). Otherwise probes a candidate list
    covering Linux (DejaVu) and macOS (Helvetica/Arial).
    """
    override = os.environ.get("VIDEO_EDIT_FONT") or os.environ.get("SCREEN_DEMO_FONT")
    if override:
        return override
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    )
    for c in candidates:
        if Path(c).exists():
            return c
    raise RuntimeError(
        "No usable font found for title cards. Set $VIDEO_EDIT_FONT to a "
        ".ttf/.ttc path (checked DejaVu on Linux and Helvetica/Arial on macOS)."
    )

# Segment/title clips are re-encoded by the final composite pass; near-lossless
# intermediates keep the double encode from compounding into visible grain.
INTERMEDIATE_CRF = 14

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
    # Crop a window at the OUTPUT aspect ratio so the zoom fills the frame with
    # no distortion (the old ih/s crop kept the source AR, then a hard scale=W:H
    # stretched 16:10 Mac recordings). Sizing to W×H is left to the shared
    # scale+pad stage that follows.
    s = zoom.scale
    crop_w = f"iw/{s}"
    crop_h = f"min(ih\\,iw/{s}*{H}/{W})"
    x = f"(iw-ow)*{zoom.x}"
    y = f"(ih-oh)*{zoom.y}"
    return f"crop={crop_w}:{crop_h}:{x}:{y}"


def _segment_video(edl: EDL, seg, words: list[dict] | None, bg: str) -> str:
    """The segment's video filtergraph, from [0:v] to [v].

    A framing that is a single chain stays one linear chain (so a screencast
    EDL builds exactly the command it always did); one that needs several
    copies of the frame becomes a graph between [vin] and [vfit].
    """
    W, H, fps = edl.output.width, edl.output.height, edl.output.fps
    pre = [f"trim=duration={seg.src_duration}", "setpts=PTS-STARTPTS"]
    if seg.zoom:
        pre.append(_zoom_filter(seg.zoom, W, H))
    post = []
    if seg.speed != 1.0:
        post.append(f"setpts=PTS/{seg.speed}")
    post.append(f"fps={fps}")
    post.append(f"trim=duration={seg.duration}")

    fx, fy = (seg.focus.x, seg.focus.y) if seg.focus else (0.5, 0.5)
    panels = {k: layouts.Rect(p.x, p.y, p.w, p.h)
              for k, p in (edl.layout.panels.items() if edl.layout else [])}
    chain = graph = None
    fit = seg.fit or edl.output.fit
    if seg.frame is None:
        if fit == "crop":
            chain = layouts.crop_chain(W, H, fx, fy)
        elif fit == "blur":
            graph = layouts.blur_graph(W, H)
        else:
            chain = layouts.pad_chain(W, H, bg)
    elif seg.frame == "stack":
        graph = layouts.stack_graph(panels["A"], panels["B"], W, H, bg, fx, fx)
    elif seg.frame == "speaker":
        runs = layouts.speaker_runs(words or [], seg.src_start, seg.src_end,
                                    edl.layout.speakers if edl.layout else {})
        if not runs:
            runs = [(0.0, seg.src_duration, sorted(panels)[0])]
        graph = layouts.speaker_graph(runs, panels, W, H, (fx, fy))
    else:
        chain = layouts.panel_chain(panels[seg.frame], W, H, fx, fy)

    if chain is not None:
        return f"[0:v]{','.join(pre + [chain] + post)}[v]"
    return f"[0:v]{','.join(pre)}[vin];{graph};[vfit]{','.join(post)}[v]"


def _cut_segments(edl: EDL, work: Path, words: list[dict] | None = None,
                  bg: str = layouts.DEFAULT_BG) -> list[Path]:
    out = []
    for i, seg in enumerate(edl.segments):
        p = work / f"seg{i:02d}.mp4"
        mute = seg.mute or seg.speed > 4.0
        out_dur = seg.duration
        video = _segment_video(edl, seg, words, bg)

        if mute:
            filter_complex = (
                f"{video};"
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
            filter_complex = f"{video}; [0:a]{achain}[a]"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(seg.src_start), "-t", str(seg.src_duration + 1),
            "-i", edl.source,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            # Intermediates get re-encoded by the composite pass — keep them
            # near-lossless (CRF 14) so quality is only lost once, at the end.
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(INTERMEDIATE_CRF),
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
    font_bold = _find_font(bold=True)
    font_reg = _find_font(bold=False)
    drawtext = [
        f"drawtext=fontfile={font_bold}:text={_esc(edl.title.line1)}:fontcolor=white:"
        f"fontsize={int(h*0.08)}:x=(w-tw)/2:y=(h-th)/2-{int(h*0.05)}",
    ]
    if edl.title.line2:
        drawtext.append(
            f"drawtext=fontfile={font_reg}:text={_esc(edl.title.line2)}:fontcolor=#9ca3af:"
            f"fontsize={int(h*0.036)}:x=(w-tw)/2:y=(h-th)/2+{int(h*0.055)}"
        )
    vf = ",".join(drawtext)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=#0a0a0a:s={w}x{h}:r={fps}:d={edl.title.duration}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", str(edl.title.duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(INTERMEDIATE_CRF),
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
        if xfade_dur == 0:
            # A hard cut: one continuous take, so the frames simply follow on.
            parts.append(f"[{cur_v}][{next_label}]concat=n=2:v=1:a=0[{out}]")
        else:
            parts.append(f"[{cur_v}][{next_label}]xfade=transition=fade:duration={xfade_dur}:offset={offset}[{out}]")
        cur_v = out
        cur_off = offset + seg.duration

    audio_parts = []
    last_a = "0:a"
    for i in range(n - 1):
        # Match the audio crossfade to the corresponding video transition. When a
        # title card leads the inputs, audio input i aligns with video segment i;
        # otherwise it aligns with segment i+1.
        seg_idx = i if title else i + 1
        xfade_dur = _xfade_duration_for(edl, seg_idx)
        a_out = f"ax{i}"
        a_in = "0:a" if i == 0 else last_a
        if xfade_dur == 0:
            audio_parts.append(f"[{a_in}][{i+1}:a]concat=n=2:v=0:a=1[{a_out}]")
        else:
            audio_parts.append(f"[{a_in}][{i+1}:a]acrossfade=d={xfade_dur}[{a_out}]")
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


def _map_label(label: str) -> str:
    """-map argument for a filter output ("[ax3]") or, when one input passes
    straight through with no crossfade, its stream ("0:a" — "[0:a]" names a
    filter output that does not exist and ffmpeg refuses it)."""
    return label if ":" in label else f"[{label}]"


def _xfade_duration_for(edl: EDL, segment_index: int) -> float:
    return edl.xfade_before(segment_index)


def _needs_source_size(edl: EDL) -> bool:
    return (bool(edl.layout) or edl.output.fit != "pad" or edl.output.height > edl.output.width
            or any(s.fit for s in edl.segments))


def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path], text=True).strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def _needs_words(edl: EDL) -> bool:
    return edl.captions is not None or any(s.frame == "speaker" for s in edl.segments)


def transcript_path(edl: EDL) -> Path:
    if edl.transcript:
        return Path(edl.transcript)
    from stages.prep import cache_dir_for
    return cache_dir_for(edl.source) / "transcript.json"


def _load_words(edl: EDL) -> list[dict]:
    path = transcript_path(edl)
    if not path.exists():
        raise FileNotFoundError(
            f"captions and speaker framing need the word transcript, and {path} does not "
            "exist. Run `pipeline.py prep <source>` first, or set `transcript` in the EDL.")
    from stages import transcript as tx
    return tx.load(path)["words"]


def _filter_path(p: Path) -> str:
    s = str(p)
    if "'" in s:
        raise ValueError(f"cannot pass a path containing a quote to ffmpeg's subtitles filter: {s}")
    return "'" + s + "'"


def _burn_subtitles(edl: EDL, words: list[dict] | None, brand, work: Path) -> str | None:
    """Write subs.ass and return the subtitles filter, or None when the EDL has
    no captions and no headline. Fails when the font cannot draw a character."""
    if edl.captions is None and edl.headline is None:
        return None
    from stages import captions as cap
    from stages import timeline
    out_words = timeline.map_words(words or [], timeline.place_segments(edl)) if edl.captions else []
    font_file = brand.caption_font() or _find_font(bold=True)
    text = " ".join(w["text"] for w in out_words) + " " + (edl.headline.text if edl.headline else "")
    missing = cap.missing_glyphs(font_file, text)
    if missing is None:
        print(f"⚠ could not check that {font_file} has every character (no fontTools, no "
              "fc-query); captions may show boxes for missing glyphs.")
    elif missing:
        raise ValueError(f"the caption font {font_file} has no glyph for: {' '.join(missing)} — "
                         "pick a font that covers the transcript's language (brand font_bold_file).")
    ass = work / "subs.ass"
    ass.write_text(cap.build_ass(out_words, edl.captions, edl.headline, edl.output.width,
                                 edl.output.height, cap.font_family(font_file), brand))
    return f"subtitles=filename={_filter_path(ass)}:fontsdir={_filter_path(Path(font_file).parent)}"


KEY_SIMILARITY = 0.30          # colorkey: how close to the key colour counts as background
KEY_BLEND = 0.08               # colorkey: soft edge width


def _despill_type(hex_colour: str) -> str | None:
    """despill handles green and blue screens only."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    if g > r and g > b:
        return "green"
    if b > r and b > g:
        return "blue"
    return None


def overlay_graph(overlays: list[Overlay], movs: list[Path], first_input: int,
                  vlabel: str, width: int, height: int) -> tuple[str, str]:
    """Filter graph that lays each rendered overlay over [vlabel] during its span.

    Input `first_input + i` is overlays[i]'s clip; returns (graph, new label).
    """
    parts = []
    for i, (o, _mov) in enumerate(zip(overlays, movs)):
        r = o.rect(width, height)
        chain = f"[{first_input + i}:v]setpts=PTS-STARTPTS+{o.start}/TB"
        if o.mode == "keyed":
            chain += f",colorkey=0x{o.key_color[1:].upper()}:{KEY_SIMILARITY}:{KEY_BLEND}"
            spill = _despill_type(o.key_color)
            if spill:
                chain += f",despill=type={spill}"
        parts.append(f"{chain}[ov{i}]")
        parts.append(f"[{vlabel}][ov{i}]overlay={r.x}:{r.y}:enable='between(t,{o.start},{o.end})':"
                     f"eof_action=pass[vov{i}]")
        vlabel = f"vov{i}"
    return "; ".join(parts), vlabel


def _render_overlays(edl: EDL) -> list[Path]:
    from stages import overlay as overlay_mod
    from stages.prep import cache_dir_for
    cache = cache_dir_for(edl.source)
    movs = []
    for o in edl.overlays:
        r = o.rect(edl.output.width, edl.output.height)
        movs.append(overlay_mod.render_overlay(Path(o.html), r.w, r.h, edl.output.fps,
                                               o.end - o.start, cache))
    return movs


def render(edl: EDL, out_path: Path, work_dir: Path | None = None) -> Path:
    brand = brand_mod.load(edl.brand) if edl.brand else brand_mod.default()
    words = _load_words(edl) if _needs_words(edl) else None
    source_size = probe_size(edl.source) if _needs_source_size(edl) else None
    for w in edl.validate(source_size=source_size, words=words):
        print(f"⚠ EDL warning: {w}")
    if brand.logo and not edl.logo:
        edl.logo = Logo(path=brand.logo.path, position=brand.logo.position,
                        scale=brand.logo.scale, start_at=0.0)

    work = work_dir or Path(tempfile.mkdtemp(prefix="video-edit-work-"))
    work.mkdir(parents=True, exist_ok=True)

    subs = _burn_subtitles(edl, words, brand, work)
    clips = _cut_segments(edl, work, words, bg=brand.background if edl.brand else layouts.DEFAULT_BG)
    title = _render_title(edl, work)
    filter_complex, cmd_inputs, vlabel, alabel = build_filter_complex(edl, clips, title)
    if edl.overlays:
        movs = _render_overlays(edl)
        graph, vlabel = overlay_graph(edl.overlays, movs, len(cmd_inputs) // 2, vlabel,
                                      edl.output.width, edl.output.height)
        for mov in movs:
            cmd_inputs.extend(["-i", str(mov)])
        filter_complex += "; " + graph
    if subs:
        filter_complex += f"; [{vlabel}]{subs}[vsub]"
        vlabel = "vsub"

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
        "-map", f"[{vlabel}]", "-map", _map_label(alabel),
        "-c:v", "libx264", "-preset", "medium", "-crf", str(edl.output.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", edl.output.audio_bitrate,
        *(["-ar", str(edl.output.audio_rate)] if edl.output.audio_rate else []),
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
