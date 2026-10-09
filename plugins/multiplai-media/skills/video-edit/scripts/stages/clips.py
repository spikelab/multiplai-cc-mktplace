"""Sentences from word timings, and clip edges snapped to them.

A reel cut mid-sentence sounds broken, and one cut mid-breath clicks. So a
proposed clip [start, end] is first widened to the sentences it touches, then
each edge moves to the middle of the nearest short silence within
SNAP_WINDOW_S, when there is one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

SENTENCE_END = (".", "?", "!", "…")
# With no punctuation (whisper drops it on long unpunctuated runs), a pause
# this long also ends a sentence, and no sentence runs past MAX_SENTENCE_S.
PAUSE_BREAK_S = 1.2
MAX_SENTENCE_S = 30.0
SNAP_WINDOW_S = 0.5


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    speaker: str | None = None


def build_sentences(words: list[dict]) -> list[Sentence]:
    out: list[Sentence] = []
    cur: list[dict] = []

    def flush() -> None:
        if cur:
            speakers = [w.get("speaker") for w in cur if w.get("speaker")]
            speaker = max(set(speakers), key=speakers.count) if speakers else None
            out.append(Sentence(start=cur[0]["start"], end=cur[-1]["end"],
                                text=" ".join(w["text"] for w in cur), speaker=speaker))
            cur.clear()

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (w["text"].endswith(SENTENCE_END)
                or nxt is None
                or nxt["start"] - w["end"] >= PAUSE_BREAK_S
                or w["end"] - cur[0]["start"] >= MAX_SENTENCE_S):
            flush()
    return out


def sentences_to_json(sentences: list[Sentence]) -> list[dict]:
    return [{k: v for k, v in asdict(s).items() if v is not None} for s in sentences]


def sentences_from_json(rows: list[dict]) -> list[Sentence]:
    return [Sentence(start=r["start"], end=r["end"], text=r["text"], speaker=r.get("speaker"))
            for r in rows]


def _nearest_trough(t: float, silences: list[tuple[float, float]]) -> float | None:
    """Middle of the silence nearest t, if that middle is within SNAP_WINDOW_S."""
    best = None
    for s, e in silences:
        mid = (s + e) / 2
        if abs(mid - t) <= SNAP_WINDOW_S and (best is None or abs(mid - t) < abs(best - t)):
            best = mid
    return best


def snap(start: float, end: float, sentences: list[Sentence],
         silences: list[tuple[float, float]]) -> tuple[float, float]:
    """Widen [start, end] to whole sentences, then move each edge into a silence.

    The start goes to the first sentence that ends after `start`; the end to the
    last sentence that starts before `end`. An edge only moves to a silence
    that does not cut back into the kept speech: the start's trough must not be
    later than the first kept word, the end's not earlier than the last.
    """
    if end <= start:
        raise ValueError(f"clip end {end} is not after start {start}")
    touched = [s for s in sentences if s.end > start and s.start < end]
    s0 = touched[0].start if touched else start
    e0 = touched[-1].end if touched else end
    ts = _nearest_trough(s0, silences)
    te = _nearest_trough(e0, silences)
    new_start = ts if ts is not None and ts <= s0 else s0
    new_end = te if te is not None and te >= e0 else e0
    return round(new_start, 3), round(new_end, 3)
