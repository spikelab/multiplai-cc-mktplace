"""Mark likely retakes: a sentence the speaker said again shortly after.

A speaker who stumbles usually restarts within seconds and says the same
words again. The later take is normally the one to keep; this only marks the
pairs so the EDL author can decide.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

WINDOW_S = 30.0
SIMILARITY = 0.6


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def find_retakes(segments: list[Any]) -> list[dict]:
    """Pairs (earlier, later) where the later segment repeats the earlier one.

    `segments` are objects or dicts with `start`, `end` and `text`. A segment is
    flagged when a later one that starts within WINDOW_S of its end has
    normalised text with a SequenceMatcher ratio >= SIMILARITY, or starts with
    its whole normalised text. Each earlier segment is reported once, against
    the first later segment that repeats it.
    """
    def get(s: Any, k: str) -> Any:
        return s[k] if isinstance(s, dict) else getattr(s, k)

    rows = [(get(s, "start"), get(s, "end"), get(s, "text"), _norm(get(s, "text"))) for s in segments]
    out = []
    for i, (s1, e1, t1, n1) in enumerate(rows):
        if not n1:
            continue
        for s2, e2, t2, n2 in rows[i + 1:]:
            if s2 - e1 > WINDOW_S:
                break
            if not n2:
                continue
            ratio = difflib.SequenceMatcher(None, n1, n2).ratio()
            if ratio >= SIMILARITY or n2.startswith(n1):
                out.append({
                    "start": s1, "end": e1, "text": t1,
                    "retake_start": s2, "retake_end": e2, "retake_text": t2,
                    "ratio": round(ratio, 2),
                })
                break
    return out
