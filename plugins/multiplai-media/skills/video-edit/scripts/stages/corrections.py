"""Fix misheard words in a prep cache's transcript before they become captions.

A corrections file is a list of replacements, each anchored near a source time:

    [{"at": 812.4, "from": "CINFUORI", "to": "CIN fuori"},
     {"at": 1282.7, "from": "Century", "to": "Sentry"}]

An entry matches the run of consecutive words whose text equals `from`
(word by word, case-sensitive, punctuation ignored) and that starts within
WINDOW_S of `at`; the nearest such run wins. The run keeps its start and end:
a 1→1 replacement keeps each word's own timing, and any other count splits
the run's time between the new words in proportion to their length. The
punctuation around the run ("Century." → "Sentry.") is kept unless `to`
brings its own.

Corrections always apply to the transcript prep wrote (saved once as
transcript.raw.json), so the file holds every correction for the source and
re-running it gives the same result. If any entry matches nothing, nothing
is written.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from stages import transcript as tx

WINDOW_S = 2.0


class CorrectionError(ValueError):
    pass


def _is_punct(c: str) -> bool:
    return unicodedata.category(c).startswith("P")


def _bare(text: str) -> str:
    return "".join(c for c in text if not _is_punct(c))


def _edges(text: str) -> tuple[str, str]:
    """The punctuation before the first letter and after the last one."""
    i = 0
    while i < len(text) and _is_punct(text[i]):
        i += 1
    j = len(text)
    while j > i and _is_punct(text[j - 1]):
        j -= 1
    return text[:i], text[j:]


def load(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise CorrectionError(f"{path}: expected a list of {{at, from, to}} entries")
    for i, e in enumerate(data):
        if not isinstance(e, dict) or set(e) != {"at", "from", "to"}:
            raise CorrectionError(f"{path}: entry {i} must have exactly at, from and to: {e!r}")
        if not str(e["from"]).split() or not str(e["to"]).split():
            raise CorrectionError(f"{path}: entry {i} has an empty from or to: {e!r}")
    return data


def _find(words: list[dict], entry: dict) -> int | None:
    target = [_bare(t) for t in str(entry["from"]).split()]
    n = len(target)
    best, best_d = None, None
    for i in range(len(words) - n + 1):
        d = abs(words[i]["start"] - float(entry["at"]))
        if d > WINDOW_S:
            continue
        if [_bare(w["text"]) for w in words[i:i + n]] == target and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


def _replace(run: list[dict], to: str) -> list[dict]:
    new = to.split()
    lead, _ = _edges(run[0]["text"])
    _, trail = _edges(run[-1]["text"])
    if lead and not _is_punct(new[0][0]):
        new[0] = lead + new[0]
    if trail and not _is_punct(new[-1][-1]):
        new[-1] = new[-1] + trail
    extra = {k: v for k, v in run[0].items() if k not in ("text", "start", "end")}
    if len(new) == len(run):
        return [{**w, "text": t} for w, t in zip(run, new)]
    start, end = run[0]["start"], run[-1]["end"]
    total = sum(len(t) for t in new)
    out, t0 = [], start
    for k, t in enumerate(new):
        t1 = end if k == len(new) - 1 else round(t0 + (end - start) * len(t) / total, 3)
        out.append({"text": t, "start": round(t0, 3), "end": t1, **extra})
        t0 = t1
    return out


def apply(words: list[dict], corrections: list[dict]) -> list[dict]:
    """The corrected words; raises CorrectionError naming every entry that
    matched nothing."""
    out = [dict(w) for w in words]
    missing = []
    for e in corrections:
        i = _find(out, e)
        if i is None:
            missing.append(e)
            continue
        n = len(str(e["from"]).split())
        out[i:i + n] = _replace(out[i:i + n], str(e["to"]))
    if missing:
        raise CorrectionError(
            "no words match " + "; ".join(
                f"{e['from']!r} within {WINDOW_S:g}s of {e['at']}" for e in missing)
            + ". Check the spelling and time against transcript.json; nothing was written.")
    return out


def correct(cache: Path, corrections_path: Path) -> Path:
    """Apply a corrections file to cache/transcript.json; returns its path."""
    current = cache / "transcript.json"
    raw = cache / "transcript.raw.json"
    base = tx.load(raw if raw.exists() else current)
    words = apply(base["words"], load(corrections_path))
    if not raw.exists():
        tx.write(base, raw)
    contract = {**base, "words": words}
    tx.write(contract, current)
    (cache / "transcript.srt").write_text(tx.to_srt(contract))
    return current


def source_of(cache: Path) -> str:
    """The recording a prep cache was built from (context.md's source line)."""
    ctx = cache / "context.md"
    for line in ctx.read_text().splitlines() if ctx.exists() else []:
        if line.startswith("- source: "):
            return line[len("- source: "):]
    raise CorrectionError(f"{ctx} names no source; run `pipeline.py prep <source>` first.")
