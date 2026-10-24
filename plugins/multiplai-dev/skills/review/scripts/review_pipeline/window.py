"""Start a model call only when the Claude usage window can pay for it and for every call already running.

`read()` asks `GET https://api.anthropic.com/api/oauth/usage` (internal and
undocumented, so it may change) for the `five_hour` and `seven_day` windows'
utilization (0-100), reset time and `remaining_dollars`, with the OAuth token
of `$CLAUDE_CONFIG_DIR/.credentials.json`. The token goes into the one
request and is never logged, printed or saved. A reading is kept for
`READ_EVERY_S`; a failed read returns None.

What a window has left is `remaining_dollars` when the endpoint gives it.
Otherwise, or when both exist, it is measured: after two readings of one
window at least `MIN_POINTS` points of utilization apart, `usd_per_point` is
this process's spend between them over the points between them, and the
window has `(100 - utilization) * usd_per_point` left. The ledger prices calls
at API list rates, so the two need not agree; both go to `progress.log`.
Other sessions on the account raise utilization too, which makes
`usd_per_point` smaller and starts fewer calls.

`admit` (from `sdk._run`, after the budget check) waits while the estimates of
the calls in flight plus this one pass `HEADROOM` of what the tightest priced
window has left, and raises `sdk.WindowFullError` when nothing is in flight
and the call still does not fit. Until a window can be priced, or when the
endpoint cannot be read, every call is admitted: the stages' concurrency is
then the only limit, and an unreadable endpoint prints one NOTICE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import budget

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = ("five_hour", "seven_day")
READ_EVERY_S = 30.0
READ_TIMEOUT_S = 5.0
HEADROOM = 0.9
MIN_POINTS = 2.0

# Cost per call by ledger label, before this run's own calls of that label return: DolceTech/Dolcebot
# PR #4's ledger (`budget.by_stage`, cost / calls), 2026-10. A dimension finder on a piece costs what a
# piece finder did.
DEFAULT_ESTIMATES = {
    "explain": 0.32,
    "paths": 0.32,
    "find:piece": 1.19,
    "find:piece-convert": 0.10,
    "find:path": 2.18,
    "find:path-convert": 0.36,
    "verify": 0.25,
    "recheck": 0.16,
    "merge": 0.04,
    "repeats": 0.04,
    "assess": 1.31,
}

NOTICE = "NOTICE: usage window not readable ({reason}); calls are limited only by concurrency"


@dataclass(frozen=True)
class Reading:
    name: str  # five_hour | seven_day
    utilization: float  # 0-100
    resets_at: str  # ISO 8601, as the endpoint gives it
    remaining_dollars: float | None


def credentials_path() -> Path:
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(root) / ".credentials.json"


def _token() -> str | None:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        token = data["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def _fetch(token: str) -> dict:
    """The endpoint's JSON. Raises on anything but a 200 with a JSON object."""
    request = urllib.request.Request(
        USAGE_URL, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=READ_TIMEOUT_S) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("the answer is not a JSON object")
    return data


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def parse(data: dict) -> list[Reading]:
    """Each of `WINDOWS` that is an object with a numeric utilization."""
    out = []
    for name in WINDOWS:
        w = data.get(name)
        if not isinstance(w, dict) or _number(w.get("utilization")) is None:
            continue
        out.append(
            Reading(
                name=name,
                utilization=float(w["utilization"]),
                resets_at=str(w.get("resets_at") or ""),
                remaining_dollars=_number(w.get("remaining_dollars")),
            )
        )
    return out


_clock: Callable[[], float] = time.monotonic
_cache: tuple[float, list[Reading] | None] | None = None  # (read at, readings)
last_error = ""  # why the last read failed


def read() -> list[Reading] | None:
    """The windows now, or None when they cannot be read (`last_error` says why). At most one request per
    `READ_EVERY_S`; the answer, a failure included, is kept that long."""
    global _cache, last_error
    now = _clock()
    if _cache is not None and now - _cache[0] < READ_EVERY_S:
        return _cache[1]
    readings: list[Reading] | None = None
    token = _token()
    if token is None:
        last_error = f"no claudeAiOauth.accessToken in {credentials_path()}"
    else:
        try:
            readings = parse(_fetch(token))
            if not readings:
                last_error, readings = "the answer holds no usage window", None
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            last_error = type(e).__name__
    _cache = (now, readings)
    return readings


def estimate(label: str) -> float:
    """This run's average cost of a call under *label*, or `DEFAULT_ESTIMATES` before one has returned."""
    ledger = budget.get_budget()
    calls = (ledger.by_stage.get(label) or {}).get("calls", 0)
    if calls:
        return ledger.by_label.get(label, 0.0) / calls
    if label in DEFAULT_ESTIMATES:
        return DEFAULT_ESTIMATES[label]
    if label.startswith("find:") and label.endswith("-convert"):
        return DEFAULT_ESTIMATES["find:piece-convert"]
    return DEFAULT_ESTIMATES["find:piece"]


@dataclass
class _Anchor:
    utilization: float
    spent: float
    resets_at: str


@dataclass
class Gate:
    """The calls in flight in this process and what it has learnt about the windows. One per event loop."""

    in_flight: float = 0.0
    calls: int = 0
    spent: float = 0.0  # this process's spend, every target's
    anchors: dict[str, _Anchor] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)  # usd_per_point by window
    seen: tuple[float, list[Reading] | None] | None = (
        None  # the last reading taken into account
    )
    noticed: bool = False
    logged: dict[str, str] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    reading: asyncio.Lock = field(default_factory=asyncio.Lock)

    def learn(self, readings: list[Reading]) -> None:
        """Move each window's anchor and rate forward on a new reading."""
        for r in readings:
            a = self.anchors.get(r.name)
            if a is None or r.resets_at != a.resets_at or r.utilization < a.utilization:
                self.anchors[r.name] = _Anchor(r.utilization, self.spent, r.resets_at)
                self.rates.pop(r.name, None)
                continue
            points, dollars = r.utilization - a.utilization, self.spent - a.spent
            if points >= MIN_POINTS and dollars > 0:
                self.rates[r.name] = dollars / points

    def left(self, readings: list[Reading]) -> tuple[Reading, float] | None:
        """The priced window with the least left, and what it has left; None when none can be priced."""
        priced = []
        for r in readings:
            rate = self.rates.get(r.name)
            measured = (100.0 - r.utilization) * rate if rate is not None else None
            dollars = measured if measured is not None else r.remaining_dollars
            self._log(r, rate, measured)
            if dollars is not None:
                priced.append((max(dollars, 0.0), r))
        if not priced:
            return None
        dollars, r = min(priced, key=lambda p: p[0])
        return r, dollars

    def _log(self, r: Reading, rate: float | None, measured: float | None) -> None:
        def usd(v: float | None) -> str:
            return "unknown" if v is None else f"${v:.2f}"

        text = (
            f"WINDOW {r.name}: utilization {r.utilization:g}%, measured {usd(measured)} left "
            f"(usd_per_point {usd(rate)}), remaining_dollars {usd(r.remaining_dollars)}, resets {r.resets_at or 'unknown'}"
        )
        if self.logged.get(r.name) != text:
            self.logged[r.name] = text
            log.info("%s", text)
            if progress_line is not None:
                progress_line(text)

    def notice(self) -> None:
        if self.noticed:
            return
        self.noticed = True
        text = NOTICE.format(reason=last_error or "unknown")
        log.warning("%s", text)
        print(text, file=sys.stderr, flush=True)
        if progress_line is not None:
            progress_line(text)


# Where window lines go besides the log: the run's `progress.log` (`orchestrator._run_stages` sets it).
progress_line: Callable[[str], None] | None = None
_gates: dict[int, Gate] = {}


def gate() -> Gate:
    loop = asyncio.get_running_loop()
    key = id(loop)
    g = _gates.get(key)
    if g is None:
        _gates.clear()  # a new event loop is a new run: nothing from the last one is in flight
        g = _gates[key] = Gate()
    return g


async def _readings(g: Gate) -> list[Reading] | None:
    async with g.reading:
        readings = await asyncio.to_thread(read)
        if _cache is not None and _cache is not g.seen:
            g.seen = _cache
            if readings is not None:
                g.learn(readings)
        return readings


async def admit(label: str) -> float:
    """Wait until a call under *label* fits the usage window, count it in flight, and return its estimate."""
    from .sdk import WindowFullError

    g = gate()
    cost = estimate(label)
    while True:
        readings = await _readings(g)
        if readings is None:
            g.notice()
            break
        tightest = g.left(readings)
        if tightest is None:
            break
        r, dollars = tightest
        if g.in_flight + cost <= dollars * HEADROOM:
            break
        if g.calls == 0:
            raise WindowFullError(
                f"the Claude {r.name} usage window cannot pay for the next call (utilization "
                f"{r.utilization:g}%, about ${dollars:.2f} left, next call about ${cost:.2f}); it resets "
                f"{r.resets_at or 'at an unknown time'}",
                limit=r.name,
                resets=r.resets_at,
            )
        changed = g.changed
        await changed.wait()
    g.in_flight += cost
    g.calls += 1
    return cost


def release(cost: float) -> None:
    """A call admitted with *cost* ended: wake every call waiting to fit."""
    try:
        g = gate()
    except RuntimeError:  # no running loop: nothing to wake
        return
    g.in_flight = max(0.0, g.in_flight - cost)
    g.calls = max(0, g.calls - 1)
    g.changed.set()
    g.changed = asyncio.Event()


def spent(cost: float) -> None:
    """Count one call's cost in this process's spend, which `usd_per_point` is measured from."""
    try:
        gate().spent += cost
    except RuntimeError:
        pass
