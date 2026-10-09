"""Word-timed captions and a headline, written as one ASS subtitle file.

Captions show a few words at a time and colour the word being spoken. The
rules follow what short-form editors converge on: at most 22 characters on
screen, a new caption at every sentence end and every pause, never one
caption across a silence longer than 0.3 s. Inside those limits a line breaks
where the speech does: after a comma, semicolon or colon rather than in the
middle of a phrase, and never leaving one word alone on a line unless that
word ends a sentence or a pause follows it. Only punctuation and timing
decide this, so it works for any language. whisper's word timings sometimes start a word
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


CLAUSE_END = (",", ";", ":")


def _chars(line: list[dict]) -> int:
    return len(" ".join(x["text"] for x in line))


def _runs(words: list[dict], max_gap: float) -> list[list[dict]]:
    """Split at every sentence end and every pause longer than max_gap: no
    caption line crosses one."""
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > max_gap or cur[-1]["text"].endswith(SENTENCE_END)):
            runs.append(cur)
            cur = []
        cur.append(w)
    if cur:
        runs.append(cur)
    return runs


def _clause_end_ahead(run: list[dict], i: int, cur: list[dict], max_chars: int) -> bool:
    """True when the line can grow, within max_chars, to a clause end or to
    the end of the run (a sentence end or a pause)."""
    line = list(cur)
    for j in range(i, len(run)):
        line.append(run[j])
        if _chars(line) > max_chars:
            return False
        if run[j]["text"].endswith(CLAUSE_END) or j == len(run) - 1:
            return True
    return False


def _break_line(cur: list[dict], w: dict, max_chars: int) -> tuple[list[dict], list[dict]]:
    """Where to end the line `cur` now that `w` must start a new one: after
    the last clause end in it, when that leaves at least 2 words on each
    line and the carried words still fit with `w`; else after all of `cur`."""
    for k in range(len(cur) - 2, 0, -1):
        if cur[k]["text"].endswith(CLAUSE_END) and _chars(cur[k + 1:] + [w]) <= max_chars:
            return cur[:k + 1], cur[k + 1:]
    return cur, []


def _split_run(run: list[dict], words_per_line: int, max_chars: int) -> list[list[dict]]:
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(run):
        if cur:
            if _chars(cur + [w]) > max_chars:
                done, cur = _break_line(cur, w, max_chars)
                lines.append(done)
            elif len(cur) >= words_per_line:
                # A soft cap: past it the line still grows to a clause end
                # that fits, and otherwise breaks at the last one in it.
                if cur[-1]["text"].endswith(CLAUSE_END):
                    lines.append(cur)
                    cur = []
                elif not _clause_end_ahead(run, i, cur, max_chars):
                    done, cur = _break_line(cur, w, max_chars)
                    lines.append(done)
        cur.append(w)
    if cur:
        lines.append(cur)
    return _no_lone_words(lines, max_chars)


def _no_lone_words(lines: list[list[dict]], max_chars: int) -> list[list[dict]]:
    """Give a one-word line a neighbour from the same run: join it to the
    line before or after, or borrow one word from it, whichever fits. Joining
    the side its clause belongs to comes first."""
    lines = [list(x) for x in lines]
    i = 0
    while i < len(lines):
        if len(lines) > 1 and len(lines[i]) == 1:
            prev = lines[i - 1] if i > 0 else None
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            word = lines[i][0]
            moves = []
            if prev is not None:
                moves += [("join_prev", prev + [word]), ("borrow_prev", prev[-1:] + [word])]
            if nxt is not None:
                moves += [("join_next", [word] + nxt), ("borrow_next", [word] + nxt[:1])]
            if prev is not None and prev[-1]["text"].endswith(CLAUSE_END):
                moves.sort(key=lambda m: not m[0].endswith("next"))
            for kind, line in moves:
                if _chars(line) > max_chars:
                    continue
                if kind == "borrow_prev" and len(prev) < 3 or kind == "borrow_next" and len(nxt) < 3:
                    continue
                if kind == "join_prev":
                    lines[i - 1:i + 1] = [line]
                    i -= 1
                elif kind == "join_next":
                    lines[i:i + 2] = [line]
                elif kind == "borrow_prev":
                    lines[i - 1:i + 1] = [prev[:-1], line]
                else:
                    lines[i:i + 2] = [line, nxt[1:]]
                break
        i += 1
    return lines


def group_words(words: list[dict], words_per_line: int = 5, max_chars: int = 22,
                max_gap: float = MAX_GAP_S) -> list[list[dict]]:
    """Caption lines. A sentence end or a pause longer than max_gap always
    ends a line; max_chars is a hard limit; words_per_line is a soft cap."""
    groups: list[list[dict]] = []
    for run in _runs(words, max_gap):
        groups.extend(_split_run(run, words_per_line, max_chars))
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
