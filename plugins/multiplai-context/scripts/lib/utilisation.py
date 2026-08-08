"""Memory utilisation telemetry — what the injected corpus actually earns.

Retrieval frequency is a *misleading* proxy for value. A section injected on
every prompt and relevant on none of them looks maximally valuable counted by
retrievals. So this module records retrieval **and** two independent, honestly
labelled *estimates* of use:

* **self-report** — the end-of-session extraction pass, which already reads the
  whole transcript, names the sections it relied on and quotes its evidence.
  Cheap, and biased: the model is grading its own session, so it over-reports.
  Claims with no evidence are recorded ``supported: false`` and never counted.
* **offline judge** — a separate, sampled, cheap-tier call from
  ``memory_maintainer`` that compares the injected list against a distilled
  transcript. Independent of the session's own reasoning, and slow to
  accumulate because it is sampled.

**Neither number is a measurement, and neither is blended into the other.**
Every surface built on this module labels its numbers *estimated*, names the
estimator, and shows the sample size; where the two estimators disagree past a
stated margin the row is *marked*, not averaged. That rule is settled (master
plan, decision 9) and is deliberately less satisfying than a single score.

## The file

``<data_dir>/utilisation.jsonl`` — append-only JSONL, rewritten atomically
(temp + ``os.replace``, the pattern ``learnings_ledger`` uses) because every
write is a read-modify-write of one session's record. The activity log is a
rolling, 7-day-retention daily file and is the wrong home for a metric that
must accumulate for months.

Two record kinds, distinguished by ``kind`` (absent means ``"session"``, so a
record written by an older version still reads):

``kind: "session"`` — one per session::

    {"v": 1, "kind": "session", "session": "a17a532c",
     "session_id": "a17a532c-....", "transcript": "/path/to/session.jsonl",
     "ts": "2026-08-08T12:00:00Z",
     "injected": [{"file": "multiplai.md", "section": "Release Flow",
                   "bytes": 6123, "turn": 3, "co_picks": 1}],
     "self_report": [{"file": ..., "section": ..., "evidence": "...",
                      "supported": true}],
     "judge": [{"file": ..., "section": ..., "used": true, "evidence": "..."}],
     "judge_status": "ok"}

``self_report`` and ``judge`` are ``null`` until their estimator has run, and a
record is valid with neither. **A null judge means "not judged", never "judged
unused"** — counting a missing judgement as unused would quietly mark every
section dead during an outage (contract C4).

``kind: "totals"`` — the compaction output. Records older than 90 days collapse
into per-section running counters, keeping the aggregate and dropping the
per-session detail.

## Section keys

``file.md#Section`` for a section pick, bare ``file.md`` for a whole-file
injection. ``section`` is ``None`` in the structured form for a whole file —
which is a *different* fact from "no section was loaded", exactly as in P1's
``sections_by_file`` contract.

## The one approximation, stated plainly

``bytes_by_file`` on the ``inject`` event is summed across every pick from that
file, so when a turn picks two sections of one file there is no per-section
byte count to be had. Those bytes are split **evenly** across the co-picked
sections and each entry carries ``co_picks: n`` so the approximation is visible
in the raw data rather than buried. It affects the cost column only; it can
never turn an unused section into a used one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

UTILISATION_FILENAME = "utilisation.jsonl"

#: Estimator observations below which a row is *not ranked*. Three observations
#: is not evidence of anything; ranking it anyway is how a table invents
#: precision it does not have.
MIN_OBSERVATIONS = 5

#: Absolute gap between the two estimators' use rates past which a row is
#: marked ``disagreement`` rather than reconciled. Divergence is a finding.
DISAGREEMENT_MARGIN = 0.35

#: Session records older than this collapse into per-section running totals.
RETENTION_DAYS = 90

ESTIMATORS = ("self_report", "judge")

#: One line each, rendered above every table. The biases are the point.
ESTIMATOR_NOTES = {
    "self_report": (
        "self-report — the session's own end-of-session pass naming what it "
        "relied on, with evidence required; cheap, and biased upward because "
        "the model is grading its own session"
    ),
    "judge": (
        "offline judge — an independent cheap-tier pass over a distilled "
        "transcript, sampled nightly; unbiased by the session's own reasoning, "
        "but accumulates slowly and is absent for most sessions"
    ),
}

DISCLAIMER = (
    "ESTIMATED, not measured. Use is not observable; both columns are "
    "estimates from a model, each with its own bias. Never read a row as "
    "proof, and never prune from this table alone."
)


# --- keys -------------------------------------------------------------------

def section_key(file: str, section: Optional[str]) -> str:
    """``file.md#Section``, or bare ``file.md`` for a whole-file injection."""
    return f"{file}#{section}" if section else file


def split_key(key: str) -> tuple[str, Optional[str]]:
    """Inverse of :func:`section_key`. ``None`` section means the whole file."""
    file, sep, section = key.partition("#")
    return (file, section if sep and section else None)


def utilisation_path(data_dir: Path) -> Path:
    return Path(data_dir) / UTILISATION_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- io ---------------------------------------------------------------------

def iter_records(path: Path) -> Iterator[dict]:
    """Yield well-formed records, skipping anything unparseable.

    A truncated final line — the shape a kill mid-write leaves behind on a
    naive appender — costs that one line and nothing else. The file is never
    rejected wholesale: it is the only durable record of months of telemetry.
    """
    path = Path(path)
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read utilisation log %s", path)
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed utilisation line %d in %s", lineno, path)
            continue
        if isinstance(record, dict):
            yield record


def read_records(path: Path) -> list[dict]:
    return list(iter_records(path))


def _write_all(path: Path, records: Sequence[dict]) -> None:
    """Rewrite the whole file atomically (temp + ``os.replace``).

    A reader never sees a partial file, and a kill mid-write leaves the
    previous version intact rather than a half-written one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-utilisation-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_session_record(record: dict) -> bool:
    return record.get("kind", "session") == "session"


def new_session_record(session: str, *, ts: Optional[str] = None) -> dict:
    return {
        "v": SCHEMA_VERSION,
        "kind": "session",
        "session": session,
        "ts": ts or _now_iso(),
        "injected": [],
        "self_report": None,
        "judge": None,
    }


def upsert_session(
    path: Path, session: str, updates: dict, *, ts: Optional[str] = None
) -> dict:
    """Merge *updates* into *session*'s record, creating it if absent.

    Returns the resulting record. Callers hold no lock: sessions write their
    own record at session end and the maintainer's judge pass runs unattended
    once a day, so genuine contention is a rare interleaving that costs at
    worst one session's judge verdict — while the atomic rewrite guarantees
    the file itself is never corrupted.
    """
    path = Path(path)
    records = read_records(path)
    target: Optional[dict] = None
    for record in records:
        if is_session_record(record) and record.get("session") == session:
            target = record
            break
    if target is None:
        target = new_session_record(session, ts=ts)
        records.append(target)
    target.update(updates)
    _write_all(path, records)
    return target


# --- writing the `injected` half -------------------------------------------

def injected_from_inject_events(events: Iterable[dict]) -> list[dict]:
    """Turn ``context``/``inject`` activity events into ``injected`` entries.

    Reads P1's two additions verbatim: ``sections_by_file`` (bare filename →
    H2 names, **an empty list meaning the whole file was injected**, and a file
    with nothing loaded carrying no key at all) and ``bytes_by_file`` (bare
    filename → characters). ``files``/``files_by_corpus`` are deliberately not
    consulted: they carry the raw pick *including* the fragment and mixing the
    two keyings is the easiest way to double-count.
    """
    out: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("component") != "context" or event.get("event") != "inject":
            continue
        sections = event.get("sections_by_file")
        sizes = event.get("bytes_by_file")
        if not isinstance(sections, dict):
            continue
        if not isinstance(sizes, dict):
            sizes = {}
        turn = event.get("turn")
        turn = turn if isinstance(turn, int) else None
        for file in sorted(sections):
            names = sections.get(file)
            names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
            try:
                file_bytes = int(sizes.get(file) or 0)
            except (TypeError, ValueError):
                file_bytes = 0
            if not names:
                # Empty list == the whole file was injected.
                out.append({
                    "file": file, "section": None, "bytes": file_bytes,
                    "turn": turn, "co_picks": 1,
                })
                continue
            # No per-section byte count exists upstream; split evenly and say
            # so via co_picks rather than presenting a fabricated exact figure.
            share = file_bytes // len(names)
            for name in names:
                out.append({
                    "file": file, "section": name, "bytes": share,
                    "turn": turn, "co_picks": len(names),
                })
    return out


def inject_events_for_session(logs_dir: Path, session: str) -> list[dict]:
    """Every ``inject`` event for *session* across the rotated activity logs.

    ``log_event`` records only the first 8 characters of a session id, so
    *session* is matched against that prefix. Files are read oldest-first
    (``activity-YYYY-MM-DD.jsonl`` sorts before the current ``activity.jsonl``)
    so a session spanning midnight keeps its turn order.
    """
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    key = (session or "")[:8]
    if not key:
        return []
    files = sorted(logs_dir.glob("activity-*.jsonl")) + [logs_dir / "activity.jsonl"]
    events: list[dict] = []
    for path in files:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"inject"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("session") != key and record.get("session_id") != key:
                continue
            if record.get("component") == "context" and record.get("event") == "inject":
                events.append(record)
    return events


def record_injections(
    path: Path,
    session: str,
    injected: Sequence[dict],
    *,
    session_id: str = "",
    transcript: str = "",
    ts: Optional[str] = None,
) -> dict:
    """Write (or replace) the ``injected`` half of *session*'s record.

    Replaces rather than appends: the list is re-derived in full from the
    activity log every time, so a PreCompact-deferred extraction running twice
    for one session must not double-count it.

    ``session_id`` (the full id) and ``transcript`` (its JSONL path) are stored
    so the offline judge can find the session later — the 8-char activity key
    alone cannot locate a transcript.
    """
    updates: dict = {"injected": list(injected)}
    if session_id:
        updates["session_id"] = session_id
    if transcript:
        updates["transcript"] = transcript
    return upsert_session(path, session, updates, ts=ts)


def record_self_report(path: Path, session: str, entries: Sequence[dict]) -> dict:
    """Store estimator A's answer. An empty list is a real answer ("none")."""
    return upsert_session(path, session, {"self_report": list(entries)})


def record_judge(
    path: Path, session: str, entries: Sequence[dict], *, status: str = "ok"
) -> dict:
    """Store estimator B's answer.

    Only ever called with a parsed result. A failed, timed-out or unparseable
    batch calls nothing at all, leaving ``judge: null`` — see contract C4.
    """
    return upsert_session(path, session, {"judge": list(entries), "judge_status": status})


def sessions_awaiting_judge(records: Sequence[dict]) -> list[dict]:
    """Session records with injections recorded and no judge verdict yet."""
    return [
        r for r in records
        if is_session_record(r) and r.get("injected") and r.get("judge") is None
    ]


# --- aggregation ------------------------------------------------------------

@dataclass
class SectionStat:
    """Accumulated evidence for one section (or whole file) key.

    Every rate here is an **estimate**. ``*_observed`` is the denominator that
    says how much to trust it; a consumer that ignores it will rank noise.
    """

    key: str
    retrieved: int = 0          # injections, summed over turns and sessions
    sessions: int = 0           # distinct sessions it was injected into
    bytes: int = 0              # characters injected, summed
    self_report_observed: int = 0
    self_report_used: int = 0
    judge_observed: int = 0
    judge_used: int = 0

    # --- derived ---

    @property
    def file(self) -> str:
        return split_key(self.key)[0]

    @property
    def section(self) -> Optional[str]:
        return split_key(self.key)[1]

    @property
    def bytes_per_injection(self) -> Optional[float]:
        return self.bytes / self.retrieved if self.retrieved else None

    def observed(self, estimator: str) -> int:
        return getattr(self, f"{estimator}_observed")

    def used(self, estimator: str) -> int:
        return getattr(self, f"{estimator}_used")

    def rate(self, estimator: str) -> Optional[float]:
        """Estimated fraction of injections that were relied on, or ``None``.

        ``None`` means *not estimated* — never *not used*. The distinction is
        the whole of contract C4 at the aggregate layer.
        """
        n = self.observed(estimator)
        return (self.used(estimator) / n) if n else None

    def estimated_uses(self, estimator: str) -> Optional[float]:
        rate = self.rate(estimator)
        return None if rate is None else self.retrieved * rate

    def bytes_per_estimated_use(self, estimator: str) -> Optional[float]:
        """Cost per estimated use. ``None`` when unestimated **or zero-use**.

        A zero use rate makes cost-per-use unbounded, which JSON cannot carry
        and a reader should not see as a number. Callers distinguish the two
        ``None``s with :meth:`zero_estimated_use`.
        """
        uses = self.estimated_uses(estimator)
        if not uses:
            return None
        return self.bytes / uses

    def zero_estimated_use(self, estimator: str) -> bool:
        return self.rate(estimator) == 0.0

    def rank_basis(self, min_observations: int = MIN_OBSERVATIONS) -> Optional[str]:
        """Which estimator orders this row — the judge when it has enough
        observations, else self-report, else nothing.

        Naming the basis is what keeps this from being a blend: both columns
        are always shown, and exactly one of them, named, does the sorting.
        """
        if self.judge_observed >= min_observations:
            return "judge"
        if self.self_report_observed >= min_observations:
            return "self_report"
        return None

    def disagreement(self, margin: float = DISAGREEMENT_MARGIN) -> bool:
        a, b = self.rate("self_report"), self.rate("judge")
        if a is None or b is None:
            return False
        return abs(a - b) > margin

    def as_dict(
        self,
        *,
        min_observations: int = MIN_OBSERVATIONS,
        margin: float = DISAGREEMENT_MARGIN,
    ) -> dict:
        basis = self.rank_basis(min_observations)
        return {
            "key": self.key,
            "file": self.file,
            "section": self.section,
            "retrieved": self.retrieved,
            "sessions": self.sessions,
            "bytes": self.bytes,
            "bytes_per_injection": self.bytes_per_injection,
            "self_report": {
                "observed": self.self_report_observed,
                "used": self.self_report_used,
                "rate": self.rate("self_report"),
            },
            "judge": {
                "observed": self.judge_observed,
                "used": self.judge_used,
                "rate": self.rate("judge"),
            },
            "estimated_uses": {e: self.estimated_uses(e) for e in ESTIMATORS},
            "bytes_per_estimated_use": {
                e: self.bytes_per_estimated_use(e) for e in ESTIMATORS
            },
            "zero_estimated_use": {e: self.zero_estimated_use(e) for e in ESTIMATORS},
            "rank_basis": basis,
            "cost_per_use": (
                self.bytes_per_estimated_use(basis) if basis else None
            ),
            "sufficient": basis is not None,
            "disagreement": self.disagreement(margin),
            "estimate": True,
        }


@dataclass
class Aggregate:
    sections: dict[str, SectionStat] = field(default_factory=dict)
    sessions: int = 0
    sessions_self_reported: int = 0
    sessions_judged: int = 0
    compacted_sessions: int = 0

    def stat(self, key: str) -> SectionStat:
        return self.sections.setdefault(key, SectionStat(key=key))


def _entry_key(entry: object) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    file = entry.get("file")
    if not isinstance(file, str) or not file:
        return None
    section = entry.get("section")
    return section_key(file, section if isinstance(section, str) and section else None)


def aggregate(records: Iterable[dict]) -> Aggregate:
    """Fold session and totals records into per-section counters.

    Both estimators contribute a **denominator and a numerator**, never a
    blended score:

    * self-report is an opt-in list with an explicit "none of them" answer, so
      every section injected into a self-reported session counts as observed,
      and the unsupported claims (no evidence) count as observed-but-unused.
    * the judge returns an explicit verdict per section, so only the sections
      it actually ruled on count as observed. A section it skipped — or a whole
      batch that failed — is simply not observed, never observed-and-unused.
    """
    agg = Aggregate()
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("kind") == "totals":
            _fold_totals(agg, record)
            continue
        if not is_session_record(record):
            continue

        injected = record.get("injected")
        if not isinstance(injected, list):
            injected = []
        per_key: dict[str, list[int]] = {}
        for entry in injected:
            key = _entry_key(entry)
            if key is None:
                continue
            try:
                size = int(entry.get("bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            slot = per_key.setdefault(key, [0, 0])
            slot[0] += 1
            slot[1] += size
        if not per_key:
            continue

        agg.sessions += 1
        for key, (count, size) in per_key.items():
            stat = agg.stat(key)
            stat.retrieved += count
            stat.sessions += 1
            stat.bytes += size

        self_report = record.get("self_report")
        if isinstance(self_report, list):
            agg.sessions_self_reported += 1
            supported = {
                k for e in self_report
                if (k := _entry_key(e)) is not None and e.get("supported", True)
            }
            for key in per_key:
                stat = agg.stat(key)
                stat.self_report_observed += 1
                if key in supported:
                    stat.self_report_used += 1

        judge = record.get("judge")
        if isinstance(judge, list):
            agg.sessions_judged += 1
            for entry in judge:
                key = _entry_key(entry)
                if key is None or key not in per_key:
                    # Never invent an observation for something that was not
                    # injected in this session.
                    continue
                stat = agg.stat(key)
                stat.judge_observed += 1
                if entry.get("used"):
                    stat.judge_used += 1
    return agg


_TOTALS_FIELDS = (
    "retrieved", "sessions", "bytes",
    "self_report_observed", "self_report_used",
    "judge_observed", "judge_used",
)


def _fold_totals(agg: Aggregate, record: dict) -> None:
    sections = record.get("sections")
    if isinstance(sections, dict):
        for key, counters in sections.items():
            if not isinstance(key, str) or not isinstance(counters, dict):
                continue
            stat = agg.stat(key)
            for name in _TOTALS_FIELDS:
                try:
                    setattr(stat, name, getattr(stat, name) + int(counters.get(name) or 0))
                except (TypeError, ValueError):
                    continue
    for attr, name in (
        ("sessions", "sessions"),
        ("sessions_self_reported", "sessions_self_reported"),
        ("sessions_judged", "sessions_judged"),
    ):
        try:
            setattr(agg, attr, getattr(agg, attr) + int(record.get(name) or 0))
        except (TypeError, ValueError):
            pass
    try:
        agg.compacted_sessions += int(record.get("sessions") or 0)
    except (TypeError, ValueError):
        pass


# --- ranking ----------------------------------------------------------------

def rank(
    agg: Aggregate,
    *,
    min_observations: int = MIN_OBSERVATIONS,
    margin: float = DISAGREEMENT_MARGIN,
) -> tuple[list[dict], list[dict]]:
    """Split sections into ``(ranked, insufficient)``.

    Ranked rows carry enough estimator observations to order; they are sorted
    worst-first by **bytes per estimated use** under their own ``rank_basis``,
    so the most expensive-per-value row — the obvious pruning candidate — is at
    the top. A zero estimated use rate has unbounded cost per use and sorts
    above every finite one.

    Rows below ``min_observations`` are returned separately rather than ranked
    among the rest: a section seen three times is not evidence, and burying
    that in a sort order is precisely the fabricated precision this phase
    exists to avoid.
    """
    ranked: list[dict] = []
    insufficient: list[dict] = []
    for stat in agg.sections.values():
        row = stat.as_dict(min_observations=min_observations, margin=margin)
        (ranked if row["sufficient"] else insufficient).append(row)

    def sort_key(row: dict) -> tuple:
        basis = row["rank_basis"]
        zero = bool(row["zero_estimated_use"].get(basis))
        cost = row["cost_per_use"] or 0.0
        return (0 if zero else 1, -cost, -row["bytes"], row["key"])

    ranked.sort(key=sort_key)
    insufficient.sort(key=lambda r: (-r["bytes"], r["key"]))
    return ranked, insufficient


def never_retrieved(
    agg: Aggregate, known_keys: Iterable[str]
) -> tuple[list[str], list[str]]:
    """``(never_retrieved, only_as_whole_file)`` among *known_keys*.

    Two different findings, kept apart deliberately. A section that was never
    picked *and* whose file was never injected whole has genuinely never
    reached a prompt. A section never picked on its own but whose file was
    injected whole *did* reach prompts — it just never earned a pick, which is
    a routing/anchoring observation, not dead weight.
    """
    observed = set(agg.sections)
    never: list[str] = []
    whole_only: list[str] = []
    for key in known_keys:
        if key in observed:
            continue
        base, section = split_key(key)
        if section and base in observed:
            whole_only.append(key)
        else:
            never.append(key)
    return sorted(never), sorted(whole_only)


def catalog_keys(catalog: dict) -> list[str]:
    """Every section key the memory catalog says could be injected.

    ``section_anchors`` is P1's field: ``[{"name": ..., "gloss": ...}]``,
    written only for files big enough to be worth splitting. A file without
    anchors contributes just its bare name, because that is the only pick the
    router can make for it.
    """
    keys: list[str] = []
    for entry in (catalog or {}).get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, str) or not source:
            continue
        keys.append(source)
        for anchor in entry.get("section_anchors") or []:
            name = anchor.get("name") if isinstance(anchor, dict) else None
            if isinstance(name, str) and name:
                keys.append(section_key(source, name))
    return keys


# --- the table (the thing P4 and P5 consume) --------------------------------

def build_table(
    records: Iterable[dict],
    *,
    known_keys: Optional[Iterable[str]] = None,
    min_observations: int = MIN_OBSERVATIONS,
    margin: float = DISAGREEMENT_MARGIN,
) -> dict:
    """The utilisation table, as a plain JSON-serialisable dict.

    This is the programmatic contract. Consumers (P4's write gate, P5's
    dead-weight pass) read ``sections`` and MUST honour three things:

    * ``estimate`` is ``true`` on every row and ``disclaimer`` sits at the top
      level — neither number is a measurement, and both must be presented that
      way wherever they are displayed.
    * ``sufficient: false`` means *do not act on this row*. ``rank_basis`` and
      ``cost_per_use`` are ``null`` there.
    * a ``null`` rate means **not estimated**, never "estimated at zero". The
      zero case is ``rate: 0.0`` with ``zero_estimated_use`` set.
    """
    agg = aggregate(records)
    ranked, insufficient = rank(
        agg, min_observations=min_observations, margin=margin
    )
    never, whole_only = never_retrieved(agg, known_keys or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "disclaimer": DISCLAIMER,
        "estimator_notes": dict(ESTIMATOR_NOTES),
        "thresholds": {
            "min_observations": min_observations,
            "disagreement_margin": margin,
        },
        "coverage": {
            "sessions": agg.sessions,
            "sessions_self_reported": agg.sessions_self_reported,
            "sessions_judged": agg.sessions_judged,
            "sessions_compacted": agg.compacted_sessions,
        },
        "sections": ranked,
        "insufficient_data": insufficient,
        "never_retrieved": never,
        "only_as_whole_file": whole_only,
    }


def load_table(
    data_dir: Path,
    *,
    catalog: Optional[dict] = None,
    min_observations: int = MIN_OBSERVATIONS,
    margin: float = DISAGREEMENT_MARGIN,
) -> dict:
    """:func:`build_table` over the on-disk log for *data_dir*."""
    known = catalog_keys(catalog) if catalog else []
    return build_table(
        read_records(utilisation_path(data_dir)),
        known_keys=known,
        min_observations=min_observations,
        margin=margin,
    )


def _fmt_bytes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def _fmt_estimator(row: dict, estimator: str) -> str:
    data = row[estimator]
    if not data["observed"]:
        return "—"
    return f"{data['used']}/{data['observed']}"


def render_table(table: dict, *, limit: int = 25) -> str:
    """Render the table for a human, with the honesty rules baked in.

    Not decoration: the estimated label, both estimator names with their
    biases, the sample size, the disagreement mark and the separate
    never-retrieved list are all required by the phase's contract and are
    implemented here rather than left to whoever writes the report.
    """
    out: list[str] = []
    out.append("## Memory utilisation — ESTIMATED")
    out.append("")
    out.append(table["disclaimer"])
    out.append("")
    for name in ESTIMATORS:
        out.append(f"- **{name}**: {table['estimator_notes'][name]}")
    cov = table["coverage"]
    out.append("")
    out.append(
        f"Coverage: {cov['sessions']} session(s) with injections · "
        f"{cov['sessions_self_reported']} self-reported · "
        f"{cov['sessions_judged']} judged"
        + (f" · {cov['sessions_compacted']} compacted into totals"
           if cov.get("sessions_compacted") else "")
    )
    thresholds = table["thresholds"]
    out.append(
        f"Rows with fewer than {thresholds['min_observations']} estimator "
        f"observations are NOT ranked — see \"insufficient data\" below. "
        f"`!` marks estimators disagreeing by more than "
        f"{thresholds['disagreement_margin']:.0%}; they are marked, not averaged. "
        f"`∞` in B/est.use means zero estimated uses under that row's basis — "
        f"the strongest pruning candidate, and a suggestion to a human, never "
        f"a deletion."
    )
    out.append("")

    rows = table["sections"]
    if not rows:
        out.append("_No section has enough estimator observations to rank yet._")
    else:
        out.append(
            f"{'section':<44} {'retr':>5} {'self-rep':>9} {'judge':>9} "
            f"{'bytes/inj':>10} {'B/est.use':>10} {'basis':>11}"
        )
        out.append("-" * 104)
        for row in rows[:limit]:
            basis = row["rank_basis"] or "—"
            cost = row["cost_per_use"]
            zero = row["zero_estimated_use"].get(basis)
            cost_txt = "∞" if zero else _fmt_bytes(cost)
            mark = " !" if row["disagreement"] else ""
            out.append(
                f"{row['key'][:44]:<44} {row['retrieved']:>5} "
                f"{_fmt_estimator(row, 'self_report'):>9} "
                f"{_fmt_estimator(row, 'judge'):>9} "
                f"{_fmt_bytes(row['bytes_per_injection']):>10} "
                f"{cost_txt:>10} {basis:>11}{mark}"
            )
        if len(rows) > limit:
            out.append(f"… {len(rows) - limit} more row(s)")

    thin = table["insufficient_data"]
    out.append("")
    out.append(f"### Insufficient data — not ranked ({len(thin)})")
    out.append("")
    if not thin:
        out.append("_None._")
    else:
        for row in thin[:limit]:
            out.append(
                f"- `{row['key']}` — retrieved {row['retrieved']}x, "
                f"self-report {_fmt_estimator(row, 'self_report')}, "
                f"judge {_fmt_estimator(row, 'judge')} "
                f"(needs ≥{thresholds['min_observations']} observations)"
            )
        if len(thin) > limit:
            out.append(f"- … {len(thin) - limit} more")

    out.append("")
    out.append(f"### Never retrieved ({len(table['never_retrieved'])})")
    out.append("")
    out.append(
        "A different finding from \"retrieved and unused\": these never reached "
        "a prompt at all, so there is no evidence either way about their value."
    )
    out.append("")
    if not table["never_retrieved"]:
        out.append("_None._")
    else:
        for key in table["never_retrieved"][:limit]:
            out.append(f"- `{key}`")
        if len(table["never_retrieved"]) > limit:
            out.append(f"- … {len(table['never_retrieved']) - limit} more")

    if table["only_as_whole_file"]:
        out.append("")
        out.append(
            f"### Reached prompts only inside a whole-file injection "
            f"({len(table['only_as_whole_file'])})"
        )
        out.append("")
        out.append(
            "Never picked as a section on their own. This is a routing or "
            "anchoring observation, not dead weight."
        )
        out.append("")
        for key in table["only_as_whole_file"][:limit]:
            out.append(f"- `{key}`")
        if len(table["only_as_whole_file"]) > limit:
            out.append(f"- … {len(table['only_as_whole_file']) - limit} more")

    return "\n".join(out)


# --- retention --------------------------------------------------------------

def compact(
    path: Path,
    *,
    older_than_days: int = RETENTION_DAYS,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict:
    """Collapse session records older than *older_than_days* into totals.

    The aggregate survives exactly; only the per-session detail is dropped.
    :func:`aggregate` reads the totals record back into the same counters, so
    ``aggregate(before)`` and ``aggregate(after)`` agree on every per-section
    number — which is what the test asserts, and the only property that makes
    this safe to run unattended.

    A record with no parseable timestamp is kept: guessing its age in order to
    delete it is the wrong direction to guess in.

    ``dry_run`` returns the same counts and writes nothing — so the maintainer
    can report what it *would* collapse rather than a number it assumed.
    """
    path = Path(path)
    records = read_records(path)
    if not records:
        return {"collapsed": 0, "kept": 0, "sections": 0}

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)

    stale: list[dict] = []
    kept: list[dict] = []
    totals_records: list[dict] = []
    for record in records:
        if record.get("kind") == "totals":
            totals_records.append(record)
            continue
        if not is_session_record(record):
            kept.append(record)
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is not None and ts < cutoff:
            stale.append(record)
        else:
            kept.append(record)

    if not stale:
        return {"collapsed": 0, "kept": len(kept), "sections": sum(
            len(t.get("sections") or {}) for t in totals_records)}

    if dry_run:
        return {"collapsed": len(stale), "kept": len(kept), "sections": len(
            aggregate(totals_records + stale).sections)}

    folded = aggregate(totals_records + stale)
    through = max(
        (record.get("ts") or "" for record in stale), default=""
    )
    totals = {
        "v": SCHEMA_VERSION,
        "kind": "totals",
        "through": through,
        "sessions": folded.sessions,
        "sessions_self_reported": folded.sessions_self_reported,
        "sessions_judged": folded.sessions_judged,
        "sections": {
            key: {name: getattr(stat, name) for name in _TOTALS_FIELDS}
            for key, stat in sorted(folded.sections.items())
        },
    }
    _write_all(path, [totals] + kept)
    return {
        "collapsed": len(stale),
        "kept": len(kept),
        "sections": len(totals["sections"]),
    }
