#!/usr/bin/env python3
"""video-edit pipeline entry point.

Subcommands (see --help for all):
  render <edl.json>             Deterministic render (walking skeleton).
  correct <cache> <fixes.json>  Fix misheard words before they become captions.
  proof <mp4> --edl E --out P   One PNG of the reel, moment by moment, to check before reporting.
  lint-overlay <page.html>      Check an overlay page for what breaks a seeked render.
  sfx-suggest <edl.json>        A sound on each follow target and each overlay start, as an `sfx` list.
  capture <url> --highlight T   Screenshot a real web page and record where a line of it is.
  proof-card <shot-dir>         An alpha overlay page showing that screenshot, moving to the line.
  shots <cache> --from --to     The shots in a range, 3 frames each, for framing a broadcast feed.
  make <source> --prompt TEXT   Natural-language → EDL → render (not yet implemented).
"""
from __future__ import annotations
import argparse
import copy
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stages.edl import EDL, Music  # noqa: E402
from stages import composite, prep as prep_stage, music as music_stage, render_sidecar  # noqa: E402


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
    sidecar = render_sidecar.path_for(out)
    # samefile, not ==: on a case-insensitive disk (macOS) Clip-01.edl.json is clip-01.edl.json.
    if sidecar == Path(args.edl).resolve() or (sidecar.exists() and sidecar.samefile(args.edl)):
        print(f"✗ the EDL is {sidecar}, where render writes its record of this render (the review page's "
              "timeline) — move the EDL (e.g. into edl/) or pick another --out", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"→ rendering {len(edl.segments)} segments → {out}")
    if edl.music and edl.music.file:
        print(f"→ music bed: {edl.music.file} @ {edl.music.volume_db} dB")
    # ffmpeg writes a hidden partial file that is renamed only once it
    # finishes, so a review page watching the folder never offers a
    # half-written render.
    part = out.with_name(f".{out.stem}.partial{out.suffix}")
    loaded = copy.deepcopy(edl)     # render fills in parts of the EDL; the sidecar records it as loaded
    try:
        composite.render(edl, part, work_dir=Path(args.work_dir) if args.work_dir else None)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    # The sidecar (<stem>.edl.json) is what the review page draws the timeline
    # from. An old one is removed before the new mp4 takes its name, so a
    # render is never paired with another render's sidecar; the new one is
    # written after the mp4, and a render without one still counts.
    try:
        record = render_sidecar.build(loaded, Path(args.edl), out.name)
    except Exception as e:  # noqa: BLE001 — the mp4 is good; only its timeline is lost
        record = None
        print(f"⚠ no {sidecar.name}: {e} (the review page shows no timeline for this render)")
    try:
        sidecar.unlink(missing_ok=True)
    except OSError as e:
        print(f"⚠ could not remove the old {sidecar.name}: {e} (it describes an earlier render; delete it)")
    part.replace(out)
    print(f"✓ wrote {out} ({out.stat().st_size/1e6:.1f} MB)")
    if record is not None:
        try:
            render_sidecar.write(out, record)
        except OSError as e:
            print(f"⚠ could not write {sidecar.name}: {e} (the review page shows no timeline for this render)")
    return 0


def cmd_lint_overlay(args: argparse.Namespace) -> int:
    from stages import overlay_lint
    page = Path(args.page)
    findings = overlay_lint.lint(page.read_text(encoding="utf-8"), mode=args.mode,
                                 key_color=args.key_color,
                                 key_reach=composite.KEYED_OUT_BELOW)
    for f in findings:
        print(f"{page.name}: {f}")
    if not findings:
        print(f"✓ {page.name}: no problems found")
    return 1 if overlay_lint.errors(findings) else 0


def cmd_sfx_suggest(args: argparse.Namespace) -> int:
    """Print an `sfx` list for an EDL: a sound at each follow target and at
    each overlay's start, in output time. Nothing is written; the list goes
    to stdout as JSON, and what was skipped or dropped goes to stderr."""
    import json
    from stages import sfx
    try:
        clicks, overlays = str(sfx.resolve(args.clicks)), str(sfx.resolve(args.overlays))
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    kept, notes = sfx.suggest(EDL.load(args.edl), clicks=clicks, overlays=overlays)
    print(json.dumps(kept, indent=1))
    for n in notes:
        print(f"⚠ {n}", file=sys.stderr)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Screenshot a web page (or take the user's own screenshot) into a shot
    directory. Exit codes are capture.py's: 2 when the page cannot be
    captured as it is, 3 when the highlight text is not in the shot."""
    from stages import capture
    try:
        if args.box and not args.from_file:
            raise capture.CaptureError("--box goes with --from-file; on a live page use --highlight TEXT")
        if args.from_file and args.highlight and not args.box:
            raise capture.CaptureError("with --from-file, give the line to highlight as --box X,Y,W,H "
                                       "(in the screenshot's pixels); --highlight is then only its label")
        out = Path(args.out) if args.out else capture.default_dir(args.url, args.job)
        if args.from_file:
            box = capture.parse_box(args.box) if args.box else None
            shot = capture.from_file(args.url, Path(args.from_file), out, box, args.highlight)
        else:
            shot = capture.capture(args.url, out, highlight=args.highlight, width=args.width,
                                   height=args.height, full_page=args.full_page)
    except capture.CaptureError as e:
        print(f"✗ {e}", file=sys.stderr)
        return e.code
    meta = shot.meta
    print(f"SHOT: {shot.dir / 'shot.png'} ({meta['width']}x{meta['height']}, captured {meta['captured_at']})")
    print(f"META: {shot.dir / 'shot.json'}")
    hl = meta["highlight"]
    if hl:
        print(f"HIGHLIGHT: x={hl['x']} y={hl['y']} w={hl['w']} h={hl['h']} (page pixels)")
    if shot.matched:
        print("MATCHED (the whole text of the element that contains the highlight):")
        for line in capture.fenced(shot.matched, meta["url"]):
            print(line)
    for w in shot.warnings or []:
        print(f"⚠ {w}", file=sys.stderr)
    return capture.EXIT_NO_MATCH if args.highlight and not hl else 0


def cmd_proof_card(args: argparse.Namespace) -> int:
    """Write the overlay page for a shot directory, then lint it as render will."""
    from stages import overlay_lint, proof_card
    try:
        style = proof_card.load_style(args.style)
        page, warnings = proof_card.build(Path(args.shot_dir), args.motion, style,
                                          width=args.width, height=args.height, duration=args.duration)
    except proof_card.ProofCardError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    findings = overlay_lint.lint(page, mode="alpha")
    for f in findings:
        print(f"{out.name}: {f}")
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)
    print(f"CARD: {out.resolve()} ({args.width}x{args.height}, {args.duration:g} s, motion {args.motion})")
    print(f'EDL: {{"html": "{out.resolve()}", "style": "{args.style}", "mode": "alpha", '
          f'"start": <s>, "end": <s + {args.duration:g}>}}')
    return 1 if overlay_lint.errors(findings) else 0


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


def cmd_correct(args: argparse.Namespace) -> int:
    """Apply a corrections file to a prep cache's transcript, then rebuild
    what prep derives from it (sentences, retakes, context.md)."""
    from stages import corrections
    cache = Path(args.cache)
    try:
        source = corrections.source_of(cache)
        path = corrections.correct(cache, Path(args.corrections))
    except (corrections.CorrectionError, FileNotFoundError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"CORRECTED: {path} (original kept in {path.with_name('transcript.raw.json')})")
    result = prep_stage.prep(source)
    print(f"CONTEXT: {result.context_path}")
    return 0


def cmd_proof(args: argparse.Namespace) -> int:
    """Tile a rendered reel's frames, labelled with time, framing, caption and speaker."""
    import subprocess
    from stages import proof, transcript as tx
    edl = EDL.load(args.edl)
    tpath = composite.transcript_path(edl)
    words = tx.load(tpath)["words"] if tpath.exists() else None
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", args.mp4],
        text=True).strip())
    out = Path(args.out)
    shown = proof.make(args.mp4, edl, words, out, composite._find_font(bold=True), duration=dur)
    for tile in shown:
        print(" | ".join(proof.label_lines(tile)))
    print(f"PROOF: {out.resolve()} ({len(shown)} tiles"
          f"{'' if words else '; no transcript, so no captions or speakers in the labels'})")
    return 0


def cmd_shots(args: argparse.Namespace) -> int:
    """List the shots prep detected in a time range and draw 3 frames of each."""
    from stages import shots
    cache = Path(args.cache)
    scenes_csv, proxy = cache / "scenes.csv", cache / "proxy_720p.mp4"
    for f in (scenes_csv, proxy):
        if not f.exists():
            print(f"✗ {f} not found — run `pipeline.py prep <source>` first.", file=sys.stderr)
            return 1
    found = shots.shots_in(shots.read_scenes(scenes_csv), args.start, args.end)
    if not found:
        print(f"✗ no shots between {args.start} and {args.end}", file=sys.stderr)
        return 1
    for k, (s, e) in enumerate(found, start=1):
        print(f"SHOT {k}: {s:.2f}–{e:.2f} ({e - s:.1f}s)")
    out = Path(args.out)
    shots.make(proxy, found, out, composite._find_font(bold=True))
    print(f"SHEET: {out.resolve()} (frames just after the start, the middle, just before the end)")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """Print each segment's place in the output and its join, then the
    transcript in output time: what the render will say, and when."""
    from stages import timeline, transcript as tx
    edl = EDL.load(args.edl)
    path = Path(args.transcript) if args.transcript else composite.transcript_path(edl)
    placed = timeline.place_segments(edl)
    for line in timeline.segment_lines(edl, placed):
        print(line)
    print()
    words = timeline.map_words(tx.load(path)["words"], placed)
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


def _mark(text: str) -> tuple[float, float, float]:
    try:
        t, x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--mark wants T,X,Y (seconds, then 0..1), got {text!r}") from None
    if t < 0 or not (0 <= x <= 1 and 0 <= y <= 1):
        raise argparse.ArgumentTypeError(f"--mark {text!r}: T must be >= 0 and X, Y within 0..1")
    return t, x, y


def cmd_cursor(args: argparse.Namespace) -> int:
    """Write the clicks found in a screen recording, for segment `follow` targets."""
    import json
    from stages import cursor
    proxy = prep_stage.cache_dir_for(args.source) / "proxy_720p.mp4"
    if not proxy.exists():
        raise SystemExit(f"{proxy} not found — run `pipeline.py prep {args.source}` first.")
    found = cursor.find(proxy, args.mark)
    out = Path(args.out).resolve() if args.out else proxy.parent / "clicks.json"
    out.write_text(json.dumps(found, indent=1))
    sure = sum(c["sure"] for c in found["clicks"])
    print(f"CLICKS: {out} ({len(found['clicks'])} clicks, {sure} matched a pointer picture; "
          f"{found['pictures']} pictures)")
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
    """Serve the job page for a job's output directory until stopped."""
    from stages import job_files
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
    job = job_files.load(video_dir.resolve(), "job")
    source = {Path(job["data"]["source"]).resolve()} if job["status"] == "ok" else set()
    print(f"REVIEW: serving {len(rs.list_videos(video_dir.resolve(), source))} clip(s) from {video_dir.resolve()}")
    states = {name: job_files.load(video_dir.resolve(), name) for name in job_files.INPUTS}
    print("INPUTS: " + ", ".join(f"{job_files.INPUTS[n]} {st['status']}" for n, st in states.items()))
    for n, st in states.items():
        if st["status"] == "invalid":
            print(f"✗ {job_files.INPUTS[n]}: " + st["error"].replace("\n", "; "))
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


def cmd_music_sample(args: argparse.Namespace) -> int:
    """Write a short sample of a synth bed, for the job page's Music tab."""
    out = Path(args.out).resolve()
    try:
        music_stage.write_synth(args.preset, args.seconds, out)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("✗ ffmpeg not found on PATH. Install it (brew install ffmpeg, or apt install ffmpeg) and try again.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"✗ ffmpeg could not write the sample (exit {e.returncode})", file=sys.stderr)
        return 1
    print(f"SAMPLE: {out}")
    return 0


def cmd_contact_sheet(args: argparse.Namespace) -> int:
    """Tile frames of a video (or a URL) into 4x4 sheets for deriving a style."""
    from stages import contact_sheet
    try:
        sheets = contact_sheet.make(args.source, Path(args.out), every=args.every,
                                    max_seconds=args.max_seconds or None)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    for s in sheets:
        print(f"SHEET: {s}")
    print(f"TIMES: {Path(args.out) / 'sheets.json'}")
    return 0 if sheets else 1


def cmd_prefs(args: argparse.Namespace) -> int:
    """Show the preferences file, or append lines the user approved."""
    from stages import preferences
    if args.action == "add":
        try:
            p = preferences.append(args.text)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"PREFS: appended {len(args.text)} line(s) to {p}")
        return 0
    text = preferences.read()
    print(f"PREFS: {preferences.path()}" + ("" if text else " (none yet)"))
    if text:
        print(text, end="")
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
    r.add_argument("--out", default="reel.mp4",
                   help="output mp4 path; <stem>.edl.json beside it records the EDL for the review page")
    r.add_argument("--work-dir", default=None, help="scratch directory for intermediate files")
    r.add_argument("--music-file", default=None, help="audio file to use as background bed")
    r.add_argument("--music-url", default=None, help="audio URL (yt-dlp): YouTube Audio Library, Pixabay Music, etc.")
    r.add_argument("--music-synth", default=None, choices=["calm", "warm", "bright"],
                   help="synthesize an ambient bed via ffmpeg (no model, container-native fallback)")
    r.add_argument("--music-volume-db", type=float, default=None, help="bed volume in dB (default -18)")
    r.set_defaults(func=cmd_render)

    lo = sub.add_parser("lint-overlay", help="check an overlay page before rendering it (render runs this too)")
    lo.add_argument("page", help="the overlay's HTML file")
    lo.add_argument("--mode", default="keyed", choices=["alpha", "panel", "keyed"],
                    help="the overlay's EDL mode (default keyed, as in the EDL)")
    lo.add_argument("--key-color", default="#00FF00", help="keyed mode: the key colour (default #00FF00)")
    lo.set_defaults(func=cmd_lint_overlay)

    sx = sub.add_parser("sfx-suggest", help="print an sfx list: a sound on each follow target and each overlay start")
    sx.add_argument("edl", help="path to EDL JSON")
    sx.add_argument("--clicks", required=True, help="audio file for follow targets (a click)")
    sx.add_argument("--overlays", required=True, help="audio file for overlay starts (a pop)")
    sx.set_defaults(func=cmd_sfx_suggest)

    cp = sub.add_parser("capture", help="screenshot a real web page and record where a line of it is (uses the network)")
    cp.add_argument("url", help="the page's address (with --from-file: the address the screenshot shows)")
    cp.add_argument("--highlight", default=None, metavar="TEXT",
                    help="record the box of the first visible element containing TEXT")
    cp.add_argument("--width", type=int, default=1280, help="viewport width in pixels (default 1280)")
    cp.add_argument("--height", type=int, default=800, help="viewport height in pixels (default 800)")
    cp.add_argument("--full-page", action="store_true", help="capture the whole page, not just the viewport")
    cp.add_argument("--from-file", default=None, metavar="PNG",
                    help="use a screenshot you saved yourself instead of loading the page")
    cp.add_argument("--box", default=None, metavar="X,Y,W,H",
                    help="with --from-file: the line to highlight, in the screenshot's pixels")
    cp.add_argument("--out", default=None, help="directory for shot.png and shot.json "
                    "(default: the job's output directory, under proof/)")
    cp.add_argument("--job", default=None, help="job name for the default directory (default: the domain)")
    cp.set_defaults(func=cmd_capture)

    pc = sub.add_parser("proof-card", help="an alpha overlay page that shows a captured page and its highlight")
    pc.add_argument("shot_dir", help="the directory capture wrote (shot.png, shot.json)")
    pc.add_argument("--motion", default="scroll", choices=["scroll", "zoom", "none"],
                    help="scroll down to the highlight, zoom in on it, or stay still (default scroll)")
    pc.add_argument("--style", required=True, help="a style name (references/styles/) or a style file")
    pc.add_argument("--width", type=int, default=1920, help="the overlay region's width (default 1920)")
    pc.add_argument("--height", type=int, default=1080, help="the overlay region's height (default 1080)")
    pc.add_argument("--duration", type=float, default=6.0,
                    help="the overlay's length in seconds, end minus start in the EDL (default 6)")
    pc.add_argument("--out", required=True, help="HTML file to write")
    pc.set_defaults(func=cmd_proof_card)

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

    co = sub.add_parser("correct", help="fix misheard words in a prep cache's transcript before captioning")
    co.add_argument("cache", help="the prep cache directory (prep prints it)")
    co.add_argument("corrections", help='JSON list of {"at": <source s>, "from": "...", "to": "..."}')
    co.set_defaults(func=cmd_correct)

    pr = sub.add_parser("proof", help="tile a rendered reel's frames with time, framing, caption and speaker")
    pr.add_argument("mp4", help="the rendered reel")
    pr.add_argument("--edl", required=True, help="the EDL it was rendered from")
    pr.add_argument("--out", required=True, help="PNG to write")
    pr.set_defaults(func=cmd_proof)

    sh = sub.add_parser("shots", help="list the shots in a time range and draw 3 frames of each")
    sh.add_argument("cache", help="the prep cache directory (prep prints it)")
    sh.add_argument("--from", dest="start", type=float, required=True, help="source seconds")
    sh.add_argument("--to", dest="end", type=float, required=True, help="source seconds")
    sh.add_argument("--out", required=True, help="PNG to write")
    sh.set_defaults(func=cmd_shots)

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

    cu = sub.add_parser("cursor", help="find where the pointer clicks in a screen recording (for follow)")
    cu.add_argument("source", help="the source recording prep ran on")
    cu.add_argument("--mark", type=_mark, action="append", default=[], metavar="T,X,Y",
                    help="the pointer at T seconds is at X,Y (0..1), e.g. from a review-page comment; repeat")
    cu.add_argument("--out", default=None, help="output JSON (default: clicks.json in the prep cache)")
    cu.set_defaults(func=cmd_cursor)

    ck = sub.add_parser("check", help="spec-check a rendered MP4 for a platform preset")
    ck.add_argument("mp4", help="rendered video")
    ck.add_argument("--preset", default="reels", choices=["reels"],
                    help="reels: 1080x1920 H.264/AAC for Instagram, Facebook and TikTok")
    ck.add_argument("--edl", default=None, help="also lint the EDL's caption and headline placement")
    ck.set_defaults(func=cmd_check)

    od = sub.add_parser("outdir", help="print and create the output directory for a job")
    od.add_argument("job", help="job name, e.g. the show and episode")
    od.set_defaults(func=cmd_outdir)

    rv = sub.add_parser("review", help="serve the job page: pick clips, draw speaker boxes, comment on renders")
    rv.add_argument("dir", help="the job's output directory: renders (clip.mp4, clip.v2.mp4, …) and the "
                               "job.json, candidates.json, looks.json, music.json the session writes")
    rv.add_argument("--mailbox", default=None, help="where comments.jsonl goes (default: <dir>/review)")
    rv.add_argument("--port", type=int, default=8765, help="first port to try (20 tried)")
    rv.add_argument("--next-version", metavar="CLIP", default=None,
                    help="print the path for CLIP's next version and exit")
    rv.set_defaults(func=cmd_review)

    ms = sub.add_parser("music-sample", help="write a short sample of a synth music bed (for music.json)")
    ms.add_argument("preset", choices=sorted(music_stage.SYNTH_PRESETS), help="the synth bed: what EDL music.synth names")
    ms.add_argument("--out", required=True, help="the .wav to write, e.g. <outdir>/samples/calm.wav")
    ms.add_argument("--seconds", type=float, default=15.0, help="sample length (default 15)")
    ms.set_defaults(func=cmd_music_sample)

    cs = sub.add_parser("contact-sheet", help="tile a reference video's frames into 4x4 sheets")
    cs.add_argument("source", help="video file or URL (fetched with yt-dlp)")
    cs.add_argument("--out", required=True, help="directory for the sheets")
    cs.add_argument("--every", type=float, default=5.0, help="seconds between interval frames (default 5)")
    cs.add_argument("--max-seconds", type=int, default=600,
                    help="only the first N seconds (default 600; 0 = all)")
    cs.set_defaults(func=cmd_contact_sheet)

    pf = sub.add_parser("prefs", help="show the user's video-edit preferences, or append approved ones")
    pf.add_argument("action", choices=["show", "add"])
    pf.add_argument("text", nargs="*", help="with add: one preference per argument (only after the user approves)")
    pf.set_defaults(func=cmd_prefs)

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
