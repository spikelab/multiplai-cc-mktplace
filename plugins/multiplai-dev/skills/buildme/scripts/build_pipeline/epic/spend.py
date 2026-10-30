"""What an epic has spent (plan-buildme-epic-mode-2026-10-12.md, item 19).

Two sources, each read from disk on every look:
- the records: each result's `spend`, the USD on the process's
  `SPEND:<usd>:<tokens>` line (`records.spend_of`); a `merge` result has no
  log line, so its `spend` is the change in `budget.process_spend()` around
  the merge agent's call (`Coordinator._resolve_conflicts`), and an
  interrupted call leaves it null. An `analysis` result (the shared codebase
  analysis, `Coordinator._shared_analysis`) is counted the same way. A result
  with no spend, or an attempt with no result yet, counts 0 and makes the
  total incomplete. Only the kinds that call a model count (`SPEND_KINDS`);
  a review's spend is in the review ledger, and posting a review or running
  a test command calls no model.
- the review ledger, `<data_dir>/reviews/ledger.jsonl`, read as a file: the
  `finished` lines of a child PR's reviews, per `(target_dir, head_sha)` and
  per review run. A `resume` continues a run: it keeps the run's first start
  (`run.started_at`) and starts from its saved spend, so the last line of
  that run holds its whole spend, and only that line counts. A fresh
  `review` of the same head is a new run (a new `run.started_at`) that starts
  from no spend, so its last line is added. A `started` line with no later
  `finished` for the same target and head (a review killed, or still
  running), or a `finished` line with no readable cost, makes the total
  incomplete. So does a run killed, or ended with no run record
  (`run: null`), when the next run of that target and head to finish is a
  fresh one: only a `resume`, whose `run.started_at` is before its own
  `started` line, holds that run's spend.

A `SPEND:` line ending `:incomplete` (`budget.spend_marker`: a skill the
process ran printed no spend) counts its USD and makes the total incomplete.

A third source covers builds the epic did not launch, such as a child built
by hand (plan-factory-merged-2026-10-29.md, item 28): each child's
`.build-state.json` and `.build-state.complete.json` in its worktree. A
checkpoint's `budget.cost_usd` is the running total of every build that
resumed it, the epic's included, so what it holds beyond the child's `build`
records is added (`state_spend`), once per child and state-file path, and
never below 0. It is read from disk on every look, so nothing is counted
twice by a later run. A checkpoint that cannot be read, or with calls of
unknown spend not accepted, makes the total incomplete.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import records

log = logging.getLogger(__name__)

SPEND_KINDS = ("build", "fix", "merge", "analysis")
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


def build_spend(tries: list[records.Attempt]) -> dict[str, float]:
    """The USD of each child's `build` records."""
    out: dict[str, float] = {}
    for a in tries:
        value = (a.result or {}).get("spend")
        if (
            a.kind == "build"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            out[a.child] = out.get(a.child, 0.0) + float(value)
    return out


def state_spend(
    paths: dict[str, list[Path]], counted: dict[str, float]
) -> tuple[float, bool]:
    """(USD, complete): what each child's checkpoints (*paths*, per child)
    hold beyond its `build` records (*counted*). A path that does not exist
    counts 0; the same file named twice counts once."""
    total, complete = 0.0, True
    for cid, files in paths.items():
        held, seen = 0.0, set()
        for path in files:
            key = Path(path).resolve()
            if key in seen:
                continue
            seen.add(key)
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                log.warning("cannot read the build checkpoint %s: %s", path, e)
                complete = False
                continue
            budget = data.get("budget") if isinstance(data, dict) else None
            value = budget.get("cost_usd") if isinstance(budget, dict) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                complete = False
                continue
            held += float(value)
            unknown = len(budget.get("unaccounted_calls") or [])
            if unknown > int(budget.get("accepted_unknown_calls") or 0):
                complete = False
        total += max(0.0, held - counted.get(cid, 0.0))
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
        text = Path(ledger).read_text(
            encoding="utf-8", errors="replace"
        )  # one bad line is skipped below
    except FileNotFoundError:
        return 0.0, True
    except OSError as e:
        log.warning("cannot read the review ledger %s: %s", ledger, e)
        return 0.0, False
    last_event: dict[tuple, str] = {}
    cost: dict[tuple, float | None] = {}
    runs: dict[tuple, _Unaccounted] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("slug") not in slugs:
            continue
        key = (entry.get("target_dir"), entry.get("head_sha"))
        event = entry.get("event")
        seen = runs.setdefault(key, _Unaccounted())
        if event == "started":
            seen.started(last_event.get(key) == "started", entry.get("generated_at"))
            last_event[key] = event
        elif event == "finished":
            last_event[key] = event
            if not isinstance(entry.get("run"), dict):
                seen.no_record()
                continue
            run = entry["run"]
            seen.finished(run.get("started_at"))
            # One review run and its resumes share `run.started_at`; without
            # it, the run's round number (which a resume keeps) tells them apart.
            run_key = (*key, run.get("started_at") or entry.get("round"))
            value = run.get("cost_usd")
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            cost[run_key] = float(value) if ok else None
    complete = (
        all(e == "finished" for e in last_event.values())
        and None not in cost.values()
        and all(s.accounted() for s in runs.values())
    )
    return sum(v for v in cost.values() if v is not None), complete


class _Unaccounted:
    """The review runs of one target and head whose spend no `finished` line
    holds yet: a run killed (its `started` line followed by another
    `started`), or one that finished with no run record. The next run to
    finish holds their spend only when it is a `resume` of them: its
    `run.started_at` is before the `started` line that followed the first
    of them. A fresh run's `run.started_at` is after its own `started`
    line, which the review skill writes to the second, before the run's
    clock starts."""

    def __init__(self) -> None:
        self.waiting = False  # one is unaccounted; no later run has started yet
        self.before: str | None = (
            None  # the `started` line after the first unaccounted run
        )
        self.lost = False

    def started(self, after_a_kill: bool, at: object) -> None:
        if after_a_kill and self.before is None and not self.waiting:
            self.waiting = True
        if self.waiting:
            self.waiting, self.before = False, at if isinstance(at, str) else ""

    def no_record(self) -> None:
        if self.before is None:
            self.waiting = True

    def finished(self, started_at: object) -> None:
        if self.waiting or (
            self.before is not None and not _earlier(started_at, self.before)
        ):
            self.lost = True
        self.waiting, self.before = False, None

    def accounted(self) -> bool:
        return not (self.lost or self.waiting or self.before is not None)


def _earlier(a: object, b: object) -> bool:
    """True when ISO time *a* is before *b*; False when either does not parse."""
    try:
        return datetime.fromisoformat(
            str(a).replace("Z", "+00:00")
        ) < datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except ValueError:
        return False
