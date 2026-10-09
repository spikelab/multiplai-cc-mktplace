"""Word-timed captions and a headline, written as one ASS subtitle file.

Captions show a few words at a time and colour the word being spoken. The
rules follow what short-form editors converge on: at most 3 words / 22
characters on screen, a new caption at every pause, never one caption across
a silence longer than 0.3 s. whisper's word timings sometimes start a word
before the previous one ended; a forward-only pass makes every event start
no earlier than the one before it ends, so two captions never overlap.

The words passed in are already in output time (stages/timeline.map_words).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from stages.brand import Brand, ass_colour
from stages.edl import Captions, Headline

MAX_GAP_S = 0.3
MIN_EVENT_S = 0.05
SENTENCE_END = (".", "?", "!", "…")


def group_words(words: list[dict], words_per_line: int = 3, max_chars: int = 22,
                max_gap: float = MAX_GAP_S) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            text_len = len(" ".join(x["text"] for x in cur + [w]))
            if (len(cur) >= words_per_line or text_len > max_chars
                    or w["start"] - cur[-1]["end"] > max_gap
                    or cur[-1]["text"].endswith(SENTENCE_END)):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_events(groups: list[list[dict]], highlight: bool, accent: str,
                   primary: str) -> list[tuple[float, float, str]]:
    """(start, end, ASS text) events. With highlight, one event per word: the
    group's text with that word in the accent colour, shown until the next
    word starts. Without, one event per group."""
    events = []
    hi, base = ass_colour(accent), ass_colour(primary)
    for g in groups:
        if not highlight:
            events.append((g[0]["start"], g[-1]["end"], " ".join(_esc(w["text"]) for w in g)))
            continue
        for i, w in enumerate(g):
            end = g[i + 1]["start"] if i + 1 < len(g) else w["end"]
            parts = []
            for j, x in enumerate(g):
                t = _esc(x["text"])
                parts.append(f"{{\\1c{hi}}}{t}{{\\1c{base}}}" if j == i else t)
            events.append((w["start"], end, " ".join(parts)))
    return events


def forward_only(events: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Each event starts no earlier than the previous one ends."""
    out = []
    prev_end = float("-inf")
    for s, e, text in events:
        s = max(s, prev_end)
        e = max(e, s + MIN_EVENT_S)
        out.append((round(s, 3), round(e, 3), text))
        prev_end = e
    return out


def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def layout_metrics(W: int, H: int, captions: Captions | None) -> dict:
    """Sizes and positions in output pixels, shared with the safe-zone lint."""
    size = (captions.size if captions and captions.size else round(H * 0.036))
    margin = round(W * 0.12)
    return {
        "caption_size": size,
        "caption_y": round(H * (captions.position_y if captions else 0.68)),
        "headline_size": round(H * 0.04),
        "headline_y": round(H * 0.125),
        "margin": margin,
    }


def build_ass(words: list[dict], captions: Captions | None, headline: Headline | None,
              W: int, H: int, font_name: str, brand: Brand) -> str:
    m = layout_metrics(W, H, captions)
    outline = max(2, round(m["caption_size"] * 0.07))
    styles = [
        f"Style: Caption,{font_name},{m['caption_size']},{ass_colour(brand.primary)},"
        f"{ass_colour(brand.primary)},{ass_colour(brand.caption_outline)},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,5,{m['margin']},{m['margin']},0,1",
        f"Style: Headline,{font_name},{m['headline_size']},{ass_colour(brand.background)},"
        f"{ass_colour(brand.background)},{ass_colour(brand.accent)},{ass_colour(brand.accent)},"
        f"-1,0,0,0,100,100,0,0,3,{round(m['headline_size'] * 0.25)},0,8,{m['margin']},{m['margin']},0,1",
    ]
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if captions is not None:
        groups = group_words(words, captions.words_per_line, captions.max_chars)
        events = forward_only(caption_events(groups, captions.highlight, brand.accent, brand.primary))
        pos = f"{{\\an5\\pos({W // 2},{m['caption_y']})}}"
        for s, e, text in events:
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Caption,,0,0,0,,{pos}{text}")
    if headline is not None:
        pos = f"{{\\an8\\pos({W // 2},{m['headline_y']})}}"
        lines.append(f"Dialogue: 1,{_ts(headline.start)},{_ts(headline.end)},Headline,,0,0,0,,"
                     f"{pos}{_esc(headline.text)}")
    return "\n".join(lines) + "\n"


# --- fonts -------------------------------------------------------------------

def font_family(font_file: str) -> str:
    """The family name libass matches against (fontTools, else fc-query)."""
    try:
        from fontTools.ttLib import TTFont  # type: ignore[import-not-found]
        name = TTFont(font_file, fontNumber=0)["name"].getDebugName(1)
        if name:
            return name
    except Exception:
        pass
    if shutil.which("fc-query"):
        out = subprocess.run(["fc-query", "--format=%{family[0]}", font_file],
                             capture_output=True, text=True).stdout.strip()
        if out:
            return out
    return Path(font_file).stem


def _charset(font_file: str) -> set[int] | None:
    try:
        from fontTools.ttLib import TTFont  # type: ignore[import-not-found]
        return set(TTFont(font_file, fontNumber=0).getBestCmap())
    except ImportError:
        pass
    if not shutil.which("fc-query"):
        return None
    out = subprocess.run(["fc-query", "--format=%{charset}", font_file],
                         capture_output=True, text=True).stdout
    return parse_fc_charset(out)


def parse_fc_charset(text: str) -> set[int]:
    cps: set[int] = set()
    for part in text.split():
        lo, _, hi = part.partition("-")
        a = int(lo, 16)
        cps.update(range(a, int(hi, 16) + 1 if hi else a + 1))
    return cps


def missing_glyphs(font_file: str, text: str) -> list[str] | None:
    """Characters of `text` the font cannot draw; None when no checker is available."""
    cs = _charset(font_file)
    if cs is None:
        return None
    return sorted({c for c in text if not c.isspace() and ord(c) not in cs})
