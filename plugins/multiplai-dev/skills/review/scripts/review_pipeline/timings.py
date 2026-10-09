"""When the run and each stage ran, as intervals in `state.timings`.

Keys are `run`, each stage name (`find`, `verify`, `merge`) and
`find:<dimension>` per finder. Each key holds a list of intervals: a resumed
run adds a new one instead of overwriting, so wall time counts only time
spent running, not the gap before a resume.

The `run` interval's end is moved forward at every checkpoint, so a run that
is killed loses only the time since its last stage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Interval, ReviewState


def now() -> str:
    """UTC ISO timestamp with milliseconds, `Z`-suffixed. Tests replace this clock."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def open_interval(state: ReviewState, key: str) -> Interval:
    interval = Interval(started_at=now())
    state.timings.setdefault(key, []).append(interval)
    return interval


def close_interval(state: ReviewState, key: str) -> None:
    """End the last interval under *key*; nothing happens when there is none."""
    intervals = state.timings.get(key)
    if intervals:
        intervals[-1].ended_at = now()


def wall_seconds(intervals: list[Interval]) -> float:
    """The summed length of the intervals that have an end."""
    total = 0.0
    for i in intervals:
        if i.ended_at:
            total += max(0.0, (parse(i.ended_at) - parse(i.started_at)).total_seconds())
    return total
