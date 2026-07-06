#!/usr/bin/env python3
"""screen-demo pipeline entry point.

Subcommands:
  render <edl.json>             Deterministic render (walking skeleton).
  make <source> --prompt TEXT   Natural-language → EDL → render (not yet implemented).
"""
from __future__ import annotations
import argparse
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
    composite.render(edl, out, work_dir=Path(args.work_dir) if args.work_dir else None)
    print(f"✓ wrote {out} ({out.stat().st_size/1e6:.1f} MB)")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    result = prep_stage.prep(args.source, prompt_hint=args.prompt_hint or "")
    print(f"\nCONTEXT: {result.context_path}")
    print(f"DURATION: {result.src_duration:.1f}")
    print(f"PROXY: {result.proxy_path}")
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
    print(f"     → writes ~/.cache/screen-demo/<key>/context.md")
    print(f"  2. (orchestrator) read context.md, author edl.json per user prompt: {args.prompt!r}")
    print(f"  3. python scripts/pipeline.py render edl.json --out {args.out}",
          f"--music-file FILE" if args.music_file else "--music-url URL" if args.music_url else "")
    print()
    print("See SKILL.md → 'make workflow' for the orchestrator's playbook.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="screen-demo")
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
    pp.set_defaults(func=cmd_prep)

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
