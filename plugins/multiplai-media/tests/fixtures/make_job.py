"""Build a synthetic two-speaker reels job for the review page.

    python tests/fixtures/make_job.py <dir> [--no-video]

Writes into <dir>:

    source.mp4                ~40 s, 1280x720: the left half is SPEAKER_0, the
                              right half SPEAKER_1, over a 440 Hz tone
    cache/transcript.json     word timings with SPEAKER_0 / SPEAKER_1 labels
    cache/sentences.json      built from those words by stages/clips.py, as prep does
    job.json                  {source, transcript, sentences, kind: "reels"}
    candidates.json           four candidates on sentence edges
    looks.json                the two shipped styles, each with its Example page as
                              the sample (looks/<style>.html), two brands
                              (brands/*.json) and two caption options

`--no-video` (and `build(..., video=False)`, which the tests use) writes a
few stand-in bytes instead of running ffmpeg.
`--no-offers` (`offers=False`) leaves out looks.json.
Generated media goes wherever <dir> is; never into the repository.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "video-edit" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from stages import clips  # noqa: E402

STYLES_DIR = _SCRIPTS.parent / "references" / "styles"
BRANDS = {
    "house": {"primary": "#FFFFFF", "accent": "#3DDC97", "background": "#0B1220", "caption_outline": "#000000"},
    "warm": {"primary": "#FFF8EC", "accent": "#FF6B4A", "background": "#1F2A44", "caption_outline": "#1F2A44"},
}
CAPTION_OPTIONS = [
    {"name": "Bold, 2 words", "values": {"size": 84, "position_y": 0.66, "words_per_line": 2, "highlight": True}},
    {"name": "Classic, 5 words", "values": {"size": 64, "position_y": 0.68, "words_per_line": 5, "highlight": False}},
]

TURNS = [
    ("SPEAKER_0", "Welcome back to the show. Today we talk about shipping small."),
    ("SPEAKER_1", "Thanks for having me. Small releases changed how my team works."),
    ("SPEAKER_0", "What was the biggest surprise?"),
    ("SPEAKER_1", "We stopped fearing Fridays. Every change was tiny, so every rollback was easy."),
    ("SPEAKER_0", "Did anything get worse?"),
    ("SPEAKER_1", "Planning got harder at first. But our deploys doubled in three months."),
    ("SPEAKER_0", "That is a strong claim. How did you measure it?"),
    ("SPEAKER_1", "We counted deploys per week. It went from two to forty."),
    ("SPEAKER_0", "Forty deploys a week sounds wild."),
    ("SPEAKER_1", "It sounds wild until you try it. Then going back feels impossible."),
]
WORD_S, WORD_GAP_S, SENTENCE_GAP_S, TURN_GAP_S = 0.24, 0.05, 0.45, 0.8
LEAD_IN_S, DURATION_S = 0.6, 40.0
WIDTH, HEIGHT = 1280, 720


def words() -> list[dict]:
    out, t = [], LEAD_IN_S
    for speaker, text in TURNS:
        for i, w in enumerate(text.split()):
            out.append({"text": w, "start": round(t, 3), "end": round(t + WORD_S, 3), "speaker": speaker})
            t += WORD_S + (SENTENCE_GAP_S if w.endswith((".", "?")) else WORD_GAP_S)
        t += TURN_GAP_S - SENTENCE_GAP_S
    assert out[-1]["end"] < DURATION_S - 0.5, "the dialogue no longer fits the video"
    return out


def candidates(sentences: list[dict]) -> list[dict]:
    """Four candidates, each a run of whole sentences (indexes into `sentences`)."""
    def span(cid, first, last, scores, why):
        ss = sentences[first:last + 1]
        speakers = [s.get("speaker") for s in ss]
        return {"id": cid, "start": ss[0]["start"], "end": ss[-1]["end"],
                "speaker": max(set(speakers), key=speakers.count), "hook": ss[0]["text"],
                "excerpt": " ".join(s["text"] for s in ss),
                "scores": dict(zip(("hook", "payoff", "quotable", "standalone", "density"), scores)),
                "why": why}
    return [
        span("c1", 4, 6, (3, 4, 3, 4, 4), "A concrete surprise with an easy payoff."),
        span("c2", 9, 13, (5, 5, 4, 4, 5), "A number that grows tenfold; strong quote at the end."),
        span("c3", 0, 1, (1, 1, 1, 2, 2), "The intro: no hook, needs context."),
        span("c4", 14, 16, (4, 4, 5, 3, 4), "Quotable close, but leans on the earlier answer."),
    ]


def _video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH; pass --no-video for a stand-in source")
    half = f"s={WIDTH // 2}x{HEIGHT}:d={DURATION_S}:r=25"
    subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0xc0392b:{half}",
        "-f", "lavfi", "-i", f"color=c=0x2471a3:{half}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={DURATION_S}",
        "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-shortest", str(path),
    ], check=True)


def example_page(style_md: Path) -> str:
    """The `## Example` page of a style file (references/styles/README.md)."""
    text = style_md.read_text(encoding="utf-8")
    body = text.split("## Example", 1)[1]
    return body.split("```html", 1)[1].split("```", 1)[0].strip() + "\n"


def looks(out: Path) -> dict:
    styles = []
    for md in sorted(STYLES_DIR.glob("*.md")):
        if md.name == "README.md":
            continue
        page = out / "looks" / f"{md.stem}.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(example_page(md), encoding="utf-8")
        styles.append({"name": md.stem, "file": str(md), "sample_html": str(page)})
    brands = []
    for name, data in BRANDS.items():
        f = out / "brands" / f"{name}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=1) + "\n")
        brands.append({"name": name, "file": str(f)})
    return {"styles": styles, "brands": brands, "captions": CAPTION_OPTIONS}


def build(out: Path, video: bool = True, offers: bool = True) -> dict[str, Path]:
    out = out.resolve()
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    source = out / "source.mp4"
    if video:
        _video(source)
    else:
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42 stand-in")
    ws = words()
    transcript = cache / "transcript.json"
    transcript.write_text(json.dumps({"language": "en", "engine": "fixture", "words": ws}, indent=1) + "\n")
    sentences = clips.sentences_to_json(clips.build_sentences(ws))
    sentences_path = cache / "sentences.json"
    sentences_path.write_text(json.dumps(sentences, indent=1) + "\n")
    job = out / "job.json"
    job.write_text(json.dumps({"source": str(source), "transcript": str(transcript),
                               "sentences": str(sentences_path), "kind": "reels"}, indent=1) + "\n")
    cands = out / "candidates.json"
    cand_list = candidates(sentences)
    cands.write_text(json.dumps(cand_list, indent=1) + "\n")
    paths = {"dir": out, "source": source, "transcript": transcript, "sentences": sentences_path,
             "job": job, "candidates": cands}
    if offers:
        paths["looks"] = out / "looks.json"
        paths["looks"].write_text(json.dumps(looks(out), indent=1) + "\n")
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir", help="directory to build the job in (created)")
    ap.add_argument("--no-video", action="store_true", help="write stand-in bytes instead of an ffmpeg video")
    ap.add_argument("--no-offers", action="store_true", help="leave out looks.json")
    args = ap.parse_args()
    paths = build(Path(args.dir), video=not args.no_video, offers=not args.no_offers)
    print(f"JOB: {paths['dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
