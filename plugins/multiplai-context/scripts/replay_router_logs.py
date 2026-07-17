# /// script
# requires-python = ">=3.11"
# ///
"""Replay real ROUTING_SCORES log lines against candidate keep_ratio values.

A label-free, real-traffic complement to eval_router.py: it does not need a
golden set. It reads the per-prompt ranking the context manager already logs
(``INFO: ROUTING_SCORES memory={...}``) and re-applies the relative-cutoff
policy at several ``keep_ratio`` values, reporting how each would change the
cap-saturation rate and the number of files injected.

Coverage caveat (important, and honest): for a call that hit the cap the log
stored only the injected top-k scores, not the full candidate pool. Every
ratio this tool sweeps is >= the ratio the logs were produced under, and a
higher ratio can only TRIM the injected set — so the injected-set metrics are
EXACT for these candidates. It cannot measure a LOOSER policy (that would need
scores the log truncated away); by design it does not try.

Usage:
    python replay_router_logs.py                       # default log dir, 0.20..0.40
    python replay_router_logs.py --logs DIR --glob 'context_manager-2026-07-1*.log'
    python replay_router_logs.py --ratios 0.25 0.30 0.35
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.memory_router import MIN_SIGNAL  # noqa: E402


def _default_log_dir() -> Path:
    ws = os.environ.get("WORKSPACE", "")
    if ws:
        return Path(ws) / ".multiplai" / "data" / "logs"
    return Path.home() / ".multiplai" / "data" / "logs"


def load_rankings(log_dir: Path, glob: str) -> list[list[float]]:
    """Return one best-first score list per injecting ROUTING_SCORES line."""
    rankings: list[list[float]] = []
    for f in sorted(globmod.glob(str(log_dir / glob))):
        for line in open(f, errors="replace"):
            i = line.find("ROUTING_SCORES memory=")
            if i < 0:
                continue
            try:
                d = json.loads(line[i + len("ROUTING_SCORES memory="):].strip())
            except json.JSONDecodeError:
                continue
            picked = d.get("picked", [])
            if picked:  # [[filename, score], ...] best first
                rankings.append([s for _fn, s in picked])
    return rankings


def kept_count(scores: list[float], keep_ratio: float, cap: int = 10) -> int:
    top = scores[0]
    if top < MIN_SIGNAL:
        return 0
    thr = max(MIN_SIGNAL, keep_ratio * top)
    return min(cap, sum(1 for s in scores if s >= thr))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs", type=Path, default=_default_log_dir())
    ap.add_argument("--glob", default="context_manager-*.log")
    ap.add_argument("--ratios", type=float, nargs="+",
                    default=[0.20, 0.25, 0.30, 0.35, 0.40])
    ap.add_argument("--cap", type=int, default=10)
    args = ap.parse_args()

    rankings = load_rankings(args.logs, args.glob)
    n = len(rankings)
    if not n:
        raise SystemExit(f"no ROUTING_SCORES lines in {args.logs}/{args.glob}")
    print(f"{n} injecting calls · {args.logs}/{args.glob}\n")
    print(f"{'keep_ratio':>10}{'%cap-hit':>10}{'avg files':>11}{'median':>8}"
          f"{'avg floor':>11}")
    for r in args.ratios:
        counts = [kept_count(s, r, args.cap) for s in rankings]
        capped = sum(1 for c in counts if c >= args.cap)
        floors = [s[c - 1] for s, c in zip(rankings, counts) if c > 0]
        print(f"{r:>10.2f}{100*capped/n:>9.1f}%{statistics.mean(counts):>11.2f}"
              f"{statistics.median(counts):>8}"
              f"{(statistics.mean(floors) if floors else 0):>11.2f}")


if __name__ == "__main__":
    main()
