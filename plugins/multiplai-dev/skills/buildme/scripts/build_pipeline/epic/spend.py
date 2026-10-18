"""What an epic has spent (plan-buildme-epic-mode-2026-10-12.md, item 19).

Two sources, each read from disk on every look:
- the records: each result's `spend`, the USD on the process's
  `SPEND:<usd>:<tokens>` line (`records.spend_of`); a `merge` result has no
  log line, so its `spend` is the change in `budget.process_spend()` around
  the merge agent's call (`Coordinator._resolve_conflicts`), and an
  interrupted call leaves it null. A result with no spend, or an attempt
  with no result yet, counts 0 and makes the total incomplete. Only the
  kinds that call a model count (`SPEND_KINDS`); a review's spend is in the
  review ledger, and posting a review or running a test command calls no
  model.
- the review ledger, `<data_dir>/reviews/ledger.jsonl`, read as a file: the
  `finished` lines of a child PR's reviews, per `(target_dir, head_sha)` and
  per review run. A `resume` continues a run: it keeps the run's first start
  (`run.started_at`) and starts from its saved spend, so the last line of
  that run holds its whole spend, and only that line counts. A fresh
  `review` of the same head is a new run (a new `run.started_at`) that starts
  from no spend, so its last line is added. A `started` line with no later
  `finished` for the same target and head (a review killed, or still
  running), or a `finished` line with no readable cost, makes the total
  incomplete.

A `SPEND:` line ending `:incomplete` (`budget.spend_marker`: a skill the
process ran printed no spend) counts its USD and makes the total incomplete.
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
            if records.spend_incomplete(a.result.get("markers") or []):
                complete = False
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
        text = Path(ledger).read_text(encoding="utf-8", errors="replace")   # one bad line is skipped below
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
            run = entry.get("run") if isinstance(entry.get("run"), dict) else {}
            # One review run and its resumes share `run.started_at`; without
            # it, the run's round number (which a resume keeps) tells them apart.
            run_key = (*key, run.get("started_at") or entry.get("round"))
            value = run.get("cost_usd")
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            cost[run_key] = float(value) if ok else None
    complete = all(e == "finished" for e in last_event.values()) and None not in cost.values()
    return sum(v for v in cost.values() if v is not None), complete
