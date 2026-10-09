#!/usr/bin/env python3
"""video-edit pipeline entry point.

Subcommands:
  render <edl.json>             Deterministic render (walking skeleton).
  make <source> --prompt TEXT   Natural-language → EDL → render (not yet implemented).
"""
from __future__ import annotations
import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stages.edl import EDL, Music  # noqa: E402
from stages import composite, prep as prep_stage, music as music_stage  # noqa: E402


def _resolve_music_arg(file: str | None, url: str | None, synth: str | None,
                       volume_db: float | None) -> Music | None:
    if not file and not url and not synth:
        return None
    default_vol = -18.0 if volume_db is None else volume_db
    if file:
        resolved = music_stage.resolve(file, None)
        return Music(file=str(resolved), volume_db=default_vol)
    if url:
        resolved = music_stage.resolve(None, url)
        return Music(file=str(resolved), volume_db=default_vol)
    return Music(synth=synth, volume_db=default_vol)


def cmd_render(args: argparse.Namespace) -> int:
    edl = EDL.load(args.edl)
    cli_music = _resolve_music_arg(args.music_file, args.music_url, args.music_synth, args.music_volume_db)
    if cli_music:
        edl.music = cli_music
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"→ rendering {len(edl.segments)} segments → {out}")
    if edl.music and edl.music.file:
        print(f"→ music bed: {edl.music.file} @ {edl.music.volume_db} dB")
    # ffmpeg writes a hidden partial file that is renamed only once it
    # finishes, so a review page watching the folder never offers a
    # half-written render.
    part = out.with_name(f".{out.stem}.partial{out.suffix}")
    try:
        composite.render(edl, part, work_dir=Path(args.work_dir) if args.work_dir else None)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(out)
    print(f"✓ wrote {out} ({out.stat().st_size/1e6:.1f} MB)")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    result = prep_stage.prep(
        args.source,
        prompt_hint=args.prompt_hint or "",
        language=args.language,
        model=args.model,
    )
    print(f"\nCONTEXT: {result.context_path}")
    print(f"TRANSCRIPT: {result.transcript_json_path} ({result.transcript_engine})")
    print(f"DURATION: {result.src_duration:.1f}")
    print(f"PROXY: {result.proxy_path}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """Print the transcript in output time: what the render will say, and when."""
    from stages import timeline, transcript as tx
    edl = EDL.load(args.edl)
    path = Path(args.transcript) if args.transcript else composite.transcript_path(edl)
    words = timeline.map_words(tx.load(path)["words"], timeline.place_segments(edl))
    line: list[dict] = []
    for i, w in enumerate(words):
        line.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or len(line) >= 12 or w["text"].endswith((".", "?", "!")) \
                or nxt["start"] - w["end"] > 0.6:
            who = f" {line[0]['speaker']}:" if line[0].get("speaker") else ""
            print(f"[{line[0]['start']:7.2f}–{line[-1]['end']:7.2f}] (src {line[0]['src_start']:.2f})"
                  f"{who} {' '.join(x['text'] for x in line)}")
            line = []
    print(f"\n{len(words)} words; output duration {edl.total_duration():.2f}s")
    return 0


def grid_filter(width: int, height: int, step: int = 100) -> str:
    """drawgrid every `step` px plus a pixel label on each line, for reading
    panel rectangles off one frame."""
    font = composite._find_font(bold=True)
    parts = [f"drawgrid=w={step}:h={step}:t=1:c=yellow@0.7"]
    for x in range(step, width, step):
        parts.append(f"drawtext=fontfile={font}:text={x}:x={x + 3}:y=3:fontsize=16:"
                     "fontcolor=yellow:box=1:boxcolor=black@0.6")
    for y in range(step, height, step):
        parts.append(f"drawtext=fontfile={font}:text={y}:x=3:y={y + 3}:fontsize=16:"
                     "fontcolor=yellow:box=1:boxcolor=black@0.6")
    return ",".join(parts)


def cmd_frame(args: argparse.Namespace) -> int:
    """Write one frame as PNG, optionally with a labelled grid in source pixels."""
    import subprocess
    out = Path(args.out).resolve() if args.out else Path.cwd() / f"frame-{args.at:g}s.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at),
           "-i", args.source, "-frames:v", "1"]
    if args.grid:
        w, h = composite.probe_size(args.source)
        cmd += ["-vf", grid_filter(w, h)]
    subprocess.run(cmd + [str(out)], check=True)
    print(f"FRAME: {out}")
    return 0


def cmd_snap(args: argparse.Namespace) -> int:
    """Snap a proposed clip to whole sentences, then to the nearest pause."""
    import json
    from stages import clips
    cache = prep_stage.cache_dir_for(args.source)
    sent_path, sil_path = cache / "sentences.json", cache / "silences.json"
    for f in (sent_path, sil_path):
        if not f.exists():
            raise SystemExit(f"{f} not found — run `pipeline.py prep {args.source}` first.")
    sentences = clips.sentences_from_json(json.loads(sent_path.read_text()))
    silences = [tuple(x) for x in json.loads(sil_path.read_text())]
    from stages import transcript as tx
    words = tx.load(cache / "transcript.json")["words"]
    start, end = clips.snap(args.start, args.end, sentences, silences, words)
    print(f"SNAPPED: {start:.3f} {end:.3f} ({end - start:.1f}s)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Spec-check a rendered MP4 (and optionally its EDL's text placement)."""
    from stages import platform
    results = platform.evaluate(platform.probe(args.mp4))
    if args.edl:
        results += platform.lint_text_boxes(EDL.load(args.edl))
    for r in results:
        print(f"{r.status:4}  {r.name}: {r.detail}")
    failed = [r for r in results if r.status == platform.FAIL]
    print(f"\n{'FAIL' if failed else 'PASS'}: {args.mp4} ({len(failed)} failing checks, preset {args.preset})")
    return 1 if failed else 0


def cmd_outdir(args: argparse.Namespace) -> int:
    """Print (and create) the output directory for a job."""
    from stages import outdir
    d = outdir.job_dir(args.job)
    (d / "edl").mkdir(parents=True, exist_ok=True)
    print(f"OUTDIR: {d}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Serve the review page for every render in a directory until stopped."""
    from stages import review_server as rs
    video_dir = Path(args.dir)
    if args.next_version:
        print(f"NEXT: {rs.next_version_path(video_dir, args.next_version)}")
        return 0
    mailbox = Path(args.mailbox) if args.mailbox else video_dir / "review"
    try:
        httpd, _token, urls = rs.serve(video_dir, mailbox, port=args.port)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    box = rs.Mailbox(mailbox.resolve())
    # The token is in open.html only; never print it (stdout is the transcript).
    print(f"REVIEW: serving {len(rs.list_videos(video_dir.resolve()))} clip(s) from {video_dir.resolve()}")
    for u in urls:
        print(f"URL: {u}  (needs the token: open {box.open_html} or append ?t=<token from {box.token_file}>)")
    print(f"OPEN: {box.open_html}")
    print(f"MAILBOX: {box.comments}", flush=True)

    def _stop(_signum, _frame):
        raise KeyboardInterrupt
    # Stopping a background task sends SIGTERM (or SIGHUP), not Ctrl-C;
    # without this the token files would outlive the server.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        rs.unpublish(mailbox.resolve())
    return 0


def cmd_make(args: argparse.Namespace) -> int:
    """make is a thin wrapper for orchestrators. The skill's SKILL.md instructs
    the consuming Claude to run prep, author the EDL, then render — this entry
    just prints the workflow so a human invoking it directly knows what's up.
    """
    print("make: this is a multi-step flow driven by the orchestrating Claude.")
    print()
    print("Steps:")
    print(f"  1. python scripts/pipeline.py prep {args.source}")
    print(f"     → writes ~/.cache/video-edit/<key>/context.md")
    print(f"  2. (orchestrator) read context.md, author edl.json per user prompt: {args.prompt!r}")
    print(f"  3. python scripts/pipeline.py render edl.json --out {args.out}",
          f"--music-file FILE" if args.music_file else "--music-url URL" if args.music_url else "")
    print()
    print("See SKILL.md → 'make workflow' for the orchestrator's playbook.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="video-edit")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="render a hand-written EDL to mp4")
    r.add_argument("edl", help="path to EDL JSON")
    r.add_argument("--out", default="reel.mp4", help="output mp4 path")
    r.add_argument("--work-dir", default=None, help="scratch directory for intermediate files")
    r.add_argument("--music-file", default=None, help="audio file to use as background bed")
    r.add_argument("--music-url", default=None, help="audio URL (yt-dlp): YouTube Audio Library, Pixabay Music, etc.")
    r.add_argument("--music-synth", default=None, choices=["calm", "warm", "bright"],
                   help="synthesize an ambient bed via ffmpeg (no model, container-native fallback)")
    r.add_argument("--music-volume-db", type=float, default=None, help="bed volume in dB (default -18)")
    r.set_defaults(func=cmd_render)

    pp = sub.add_parser("prep", help="detect cuts + transcribe + write context bundle")
    pp.add_argument("source", help="path to screen recording")
    pp.add_argument("--prompt-hint", default=None,
                    help="hint string passed to whisper (proper nouns) e.g. 'My App, Claude'")
    pp.add_argument("--language", default=None,
                    help="ISO code of the spoken language (e.g. 'it', 'es'). Omit to auto-detect. "
                         "Transcription is always multilingual — never English-only.")
    pp.add_argument("--model", default=None,
                    help="override the mlx_whisper model (default: mlx-community/whisper-large-v3-mlx "
                         "for a non-English --language, else mlx-community/whisper-medium-mlx)")
    pp.set_defaults(func=cmd_prep)

    tl = sub.add_parser("timeline", help="print the transcript in output time for an EDL")
    tl.add_argument("edl", help="path to EDL JSON")
    tl.add_argument("--transcript", default=None,
                    help="transcript.json (default: the EDL's `transcript`, else the prep cache for its source)")
    tl.set_defaults(func=cmd_timeline)

    fr = sub.add_parser("frame", help="write one frame as PNG (with --grid: labelled 100 px grid)")
    fr.add_argument("source", help="path to the source video")
    fr.add_argument("--at", type=float, required=True, help="time in seconds")
    fr.add_argument("--grid", action="store_true", help="overlay a 100 px grid labelled in source pixels")
    fr.add_argument("--out", default=None, help="output PNG (default: ./frame-<t>s.png)")
    fr.set_defaults(func=cmd_frame)

    sn = sub.add_parser("snap", help="snap clip edges to sentences, then to the nearest pause")
    sn.add_argument("source", help="the source recording prep ran on")
    sn.add_argument("start", type=float)
    sn.add_argument("end", type=float)
    sn.set_defaults(func=cmd_snap)

    ck = sub.add_parser("check", help="spec-check a rendered MP4 for a platform preset")
    ck.add_argument("mp4", help="rendered video")
    ck.add_argument("--preset", default="reels", choices=["reels"],
                    help="reels: 1080x1920 H.264/AAC for Instagram, Facebook and TikTok")
    ck.add_argument("--edl", default=None, help="also lint the EDL's caption and headline placement")
    ck.set_defaults(func=cmd_check)

    od = sub.add_parser("outdir", help="print and create the output directory for a job")
    od.add_argument("job", help="job name, e.g. the show and episode")
    od.set_defaults(func=cmd_outdir)

    rv = sub.add_parser("review", help="serve a page to watch renders and comment on them by time and position")
    rv.add_argument("dir", help="directory of rendered videos (clip.mp4, clip.v2.mp4, …)")
    rv.add_argument("--mailbox", default=None, help="where comments.jsonl goes (default: <dir>/review)")
    rv.add_argument("--port", type=int, default=8765, help="first port to try (20 tried)")
    rv.add_argument("--next-version", metavar="CLIP", default=None,
                    help="print the path for CLIP's next version and exit")
    rv.set_defaults(func=cmd_review)

    m = sub.add_parser("make", help="natural-language → reel (orchestrator workflow)")
    m.add_argument("source", help="path to screen recording (.mov/.mp4)")
    m.add_argument("--prompt", required=True, help="prose description of the cut")
    m.add_argument("--logo", default=None)
    m.add_argument("--music-file", default=None)
    m.add_argument("--music-url", default=None)
    m.add_argument("--out", default="reel.mp4")
    m.set_defaults(func=cmd_make)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
