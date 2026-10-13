"""What an epic has spent (plan-buildme-epic-mode-2026-10-12.md, item 19).

Two sources, each read from disk on every look:
- the records: each result's `spend`, the USD on the process's
  `SPEND:<usd>:<tokens>` line (`records.spend_of`). A result with no spend, or
  an attempt with no result yet, counts 0 and makes the total incomplete.
  Only the kinds whose process prints `SPEND:` count (`SPEND_KINDS`); a
  review's spend is in the review ledger, and posting a review or running a
  test command calls no model.
- the review ledger, `<data_dir>/reviews/ledger.jsonl`, read as a file: per
  `(target_dir, head_sha)`, the last `run.cost_usd` among the `finished` lines
  of a child PR's review. A review resumed after its budget stop starts from
  its saved spend, so its last line already holds the first run's spend.
  A `started` line with no later `finished` for the same target and head (a
  review killed, or still running) makes the total incomplete.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import records

log = logging.getLogger(__name__)

SPEND_KINDS = ("build", "fix", "merge")
LEDGER = Path("reviews") / "ledger.jsonl"
_PR_NUMBER = re.compile(r"/pull/(\d+)(?:[/?#]|$)")


def attempts_spend(tries: list[records.Attempt]) -> tuple[float, bool]:
    """(USD, complete) over every attempt of a kind that prints `SPEND:`."""
    total, complete = 0.0, True
    for a in tries:
        if a.kind not in SPEND_KINDS:
            continue
        value = (a.result or {}).get("spend")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
        else:
            complete = False
    return total, complete


def review_slugs(repo_slug: str, prs: list[str | None]) -> set[str]:
    """The review slug of each child PR: `<owner>-<repo>--pr-<n>`, sanitized as
    the review skill does (`store.sanitize_slug`)."""
    from .store import sanitize_slug

    out = set()
    for url in prs:
        m = _PR_NUMBER.search(url or "")
        if m:
            out.add(sanitize_slug(f"{repo_slug}--pr-{m.group(1)}"))
    return out


def review_spend(ledger: Path, slugs: set[str]) -> tuple[float, bool]:
    """(USD, complete) of the reviews of the PRs named by *slugs*."""
    try:
        text = Path(ledger).read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0.0, True
    except OSError as e:
        log.warning("cannot read the review ledger %s: %s", ledger, e)
        return 0.0, False
    last_event: dict[tuple, str] = {}
    cost: dict[tuple, float | None] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("slug") not in slugs:
            continue
        key = (entry.get("target_dir"), entry.get("head_sha"))
        event = entry.get("event")
        if event == "started":
            last_event[key] = event
        elif event == "finished":
            last_event[key] = event
            run = entry.get("run")
            value = run.get("cost_usd") if isinstance(run, dict) else None
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            cost[key] = float(value) if ok else None
    complete = all(e == "finished" for e in last_event.values()) and None not in cost.values()
    return sum(v for v in cost.values() if v is not None), complete
