"""Backfill learnings and diary from existing Claude Code session transcripts.

Discovers JSONL transcripts in ``$CLAUDE_CONFIG_DIR/projects/``, distills
them, extracts diary entries + learnings, and writes them using the same
shared helpers as the live pipeline.

Privacy / prompt-injection caveat: this reads **every local Claude Code
transcript across all projects** (not just the current workspace) and
feeds their text to the extraction model. A transcript containing
attacker-influenced content (pasted web data, untrusted repo files) is an
indirect prompt-injection surface into your memory files. Use
``--projects`` to scope, and review consolidations before applying them
via ``/multiplai-context:dream-remember``.

Usage::

    python scripts/backfill.py [--days N] [--since YYYY-MM-DD] [--all]
                               [--projects slug,slug] [--concurrency 3]
                               [--dry-run] [--no-catalogs] [--no-now]

Default window: last 7 days by record timestamp (file mtime fallback).
"""

import argparse
import asyncio
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.log_utils import setup_logging
from lib.extraction import (
    extract_units,
    load_target_charters,
    write_diary_entries,
    append_learnings,
)
from lib.transcript_distiller import distill, estimate_tokens

logger = setup_logging("backfill")

DEFAULT_DAYS = 7
DEFAULT_CONCURRENCY = 3


def _find_transcripts(claude_config_dir: Path) -> list[Path]:
    """Find all JSONL transcript files under CLAUDE_CONFIG_DIR/projects/."""
    projects_dir = claude_config_dir / "projects"
    if not projects_dir.exists():
        return []
    return sorted(projects_dir.glob("**/*.jsonl"))


def _parse_ts(record: dict) -> datetime | None:
    ts = record.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _transcript_time_bounds(jsonl_path: Path) -> tuple[datetime | None, datetime | None]:
    """Return (first, last) parseable timestamps from a transcript.

    Scans the head for the first and the tail for the last so a long session
    that spans the window boundary is judged by its full extent, not just its
    start. Falls back to file mtime for both when no timestamp is found.
    """
    first = last = None
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        nonblank = [ln.strip() for ln in lines if ln.strip()]
        for line in nonblank[:50]:
            try:
                dt = _parse_ts(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
            if dt is not None:
                first = dt
                break
        for line in reversed(nonblank[-50:]):
            try:
                dt = _parse_ts(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
            if dt is not None:
                last = dt
                break
    except OSError:
        pass

    if first is None or last is None:
        try:
            mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = None
        first = first or mtime
        last = last or first or mtime
    return first, last


def _transcript_timestamp(jsonl_path: Path) -> datetime | None:
    """Return the first parseable timestamp (or mtime fallback)."""
    return _transcript_time_bounds(jsonl_path)[0]


def _session_id_from_path(jsonl_path: Path) -> str:
    """Extract session ID from transcript filename (stem) or first record."""
    stem = jsonl_path.stem
    if stem and len(stem) > 8:
        return stem
    try:
        for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()[:10]:
            record = json.loads(line.strip())
            sid = record.get("session_id") or record.get("sessionId")
            if sid:
                return str(sid)
    except (OSError, json.JSONDecodeError):
        pass
    return stem or jsonl_path.name


def _ledger_path() -> Path:
    """Where "this session has been extracted" is recorded.

    One session id per line, append-only. Deliberately not JSON: concurrent
    workers append under a lock, and a truncated write must cost one duplicate
    line rather than a corrupt document nothing can read.
    """
    return get_paths().skill_state_dir("backfill") / "processed-sessions.txt"


def _read_ledger(path: Path) -> set[str]:
    try:
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        }
    except OSError:
        # Fail open: an unreadable ledger costs a re-extraction, and refusing
        # to back up because bookkeeping is unavailable is the worse trade.
        return set()


def _record_processed(session_id: str, path: Path) -> None:
    """Note that *session_id* was extracted, whatever the extraction produced."""
    if not session_id:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.with_suffix(path.suffix + ".lock")
        with open(lock_file, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"{session_id}\n")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        logger.warning("Could not record %s as processed", session_id)


def _is_already_processed(
    session_id: str,
    learnings_file: Path,
    diary_dir: Path,
    processed: set[str] | None = None,
) -> bool:
    """Pre-LLM idempotency gate: has this session already been extracted?

    **The ledger is the answer; the two file markers are a fallback.** Both
    markers are written only when the extraction produced something of that
    kind — `write_diary_entries` returns early when no unit carries a diary
    entry, and `append_learnings` writes its `Session: <id>` line inside a loop
    that `continue`s past units with no learnings. So the presence of a marker
    proves a session was processed, but its absence proves nothing.

    This gate used to require *both* markers, which made it permanently False
    for the commonest asymmetric shape: a session that produced a diary entry
    and no learnings — the diary is the primary record and learnings are a
    projection of it, so that combination is ordinary, not exceptional. Every
    subsequent `--since <old-date>` re-ran a full LLM pass over those
    transcripts, landing month-old material in today's backlog and sharing a
    rate limit with interactive work (the 2026-07-06/07 `failed_extractions`
    cluster was one 429 from a large backfill).

    Requiring *either* marker fixes that, but not the session that produced
    neither — a short or uneventful transcript extracts to nothing at all and
    would re-extract forever. Hence the ledger, which records the *act* of
    extracting rather than its output. The markers stay as a fallback so the
    first run after this change does not re-extract a history that predates the
    ledger.

    *processed* is the ledger contents, read once per run by `backfill()`. This
    function does not read it itself: an empty default keeps the predicate
    answerable from its arguments alone, with no data directory to resolve and
    no directory created as a side effect of asking a question.
    """
    processed = processed or set()
    if session_id in processed:
        return True

    if learnings_file.exists() and f"Session: {session_id}" in learnings_file.read_text(
        encoding="utf-8", errors="replace",
    ):
        return True

    if diary_dir.is_dir():
        marker = f"## Session: {session_id}"
        for day_file in diary_dir.glob("*.md"):
            if marker in day_file.read_text(encoding="utf-8", errors="replace"):
                return True
    return False


def _session_cwd(jsonl_path: Path) -> str:
    """Get the dominant cwd from the transcript (first non-empty cwd record)."""
    try:
        for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()[:100]:
            try:
                r = json.loads(line.strip())
                cwd = r.get("cwd", "")
                if cwd:
                    return cwd
            except (json.JSONDecodeError, ValueError):
                pass
    except OSError:
        pass
    return ""


async def _process_session(
    jsonl_path: Path,
    session_id: str,
    since: datetime,
    until: datetime | None,
    *,
    valid_targets: list[dict],
    diary_dir: Path,
    learnings_file: Path,
    client,
    dry_run: bool,
    sem: asyncio.Semaphore,
    processed: set[str] | None = None,
) -> dict:
    """Process one session transcript. Returns a stats dict."""
    stats = {"session_id": session_id, "status": "ok", "diary": False, "learnings": False}

    if _is_already_processed(session_id, learnings_file, diary_dir, processed):
        stats["status"] = "skipped"
        return stats

    chunks = distill(jsonl_path, since=since, until=until)
    if not chunks:
        stats["status"] = "empty"
        return stats

    cwd = _session_cwd(jsonl_path)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if dry_run:
        stats["status"] = "dry-run"
        stats["chunks"] = len(chunks)
        stats["est_tokens"] = estimate_tokens(jsonl_path)
        return stats

    units: list[dict] = []
    failed_chunks = 0
    async with sem:
        for i, chunk in enumerate(chunks):
            try:
                chunk_units = await extract_units(
                    chunk,
                    valid_targets=valid_targets,
                    client=client,
                )
                # Tag with session_id for multi-chunk dedup
                units.extend(chunk_units)
            except Exception as e:
                failed_chunks += 1
                logger.warning("LLM call failed for session %s chunk %d: %s", session_id, i, e)

    if units:
        diary_path = write_diary_entries(units, diary_dir, session_id, cwd, timestamp)
        stats["diary"] = diary_path is not None

    wrote = append_learnings(units, learnings_file, session_id, timestamp)
    stats["learnings"] = wrote

    # Record the *act* of extracting, so a session that legitimately yielded
    # nothing is not re-extracted on every future run. Not when a chunk failed:
    # a 429 or a timeout means this transcript has not actually been read, and
    # marking it done would lose it permanently — the one thing worse than
    # re-extracting it.
    if failed_chunks:
        stats["status"] = "partial"
        logger.warning(
            "Session %s: %d/%d chunk(s) failed — not recorded as processed, "
            "so a later run will retry it",
            session_id, failed_chunks, len(chunks),
        )
    else:
        _record_processed(session_id, _ledger_path())

    return stats


async def backfill(
    since: datetime,
    until: datetime | None = None,
    *,
    project_slugs: list[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    run_catalogs: bool = True,
    run_now: bool = True,
) -> dict:
    """Run the full backfill pipeline. Returns a summary dict."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    diary_dir = paths.diary_dir()
    valid_targets = load_target_charters(memory_dir, paths.catalogs_dir())

    claude_config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    all_transcripts = _find_transcripts(claude_config_dir)

    # Filter by window using the session's full [first, last] extent so a long
    # session that started before `since` but continued into the window is not
    # wholly excluded.
    in_window = []
    for t in all_transcripts:
        first, last = _transcript_time_bounds(t)
        if last is None:
            continue
        if last < since:
            continue
        if until and (first or last) > until:
            continue
        in_window.append(t)

    # Filter by project
    if project_slugs:
        slugs = set(project_slugs)
        in_window = [t for t in in_window if Path(t.parent.name).name in slugs or t.parent.name in slugs]

    if not in_window:
        return {"scanned": 0, "written": 0, "skipped": 0, "errored": 0, "dry_run": dry_run}

    summary = {"scanned": len(in_window), "written": 0, "skipped": 0, "errored": 0, "dry_run": dry_run}

    if dry_run:
        total_est = sum(estimate_tokens(t) for t in in_window)
        summary["sessions"] = []
        for t in in_window:
            sid = _session_id_from_path(t)
            summary["sessions"].append({
                "path": str(t),
                "session_id": sid,
                "est_tokens": estimate_tokens(t),
            })
        summary["total_est_tokens"] = total_est
        return summary

    client = await create_client()
    logger.info("Backfill using %s", type(client).__name__)

    sem = asyncio.Semaphore(concurrency)
    # Read once for the whole run: every worker asks the same question, and the
    # ledger only grows during the run (nothing here is skipped because of a
    # session processed moments ago in the same pass).
    processed = _read_ledger(_ledger_path())
    tasks = []
    for jsonl_path in sorted(in_window):
        session_id = _session_id_from_path(jsonl_path)
        # Key the learnings file by the session's LAST timestamp (stop date) to
        # match the live pipeline, which extracts at SessionEnd — otherwise a
        # session crossing UTC midnight lands in a different day file than the
        # live run and gets double-extracted.
        _, last_ts = _transcript_time_bounds(jsonl_path)
        date_str = last_ts.strftime("%Y-%m-%d") if last_ts else None
        learnings_file = paths.learnings_file(date_str)
        tasks.append(_process_session(
            jsonl_path, session_id, since, until,
            valid_targets=valid_targets,
            diary_dir=diary_dir,
            learnings_file=learnings_file,
            client=client,
            dry_run=False,
            sem=sem,
            processed=processed,
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            summary["errored"] += 1
            logger.exception("Session error: %s", r)
        elif r.get("status") == "skipped":
            summary["skipped"] += 1
        else:
            if r.get("diary") or r.get("learnings"):
                summary["written"] += 1

    # Post-pass: regenerate now/ and catalogs
    if run_now:
        try:
            from synthesize_now import synthesize
            await synthesize()
            logger.info("Synthesized now/")
        except Exception as e:
            logger.warning("synthesize_now failed (non-fatal): %s", e)

    if run_catalogs:
        try:
            # Call the async API directly — generate_catalog.main() wraps it
            # in asyncio.run(), which cannot run inside this already-running
            # event loop.
            from generators.config import load_catalog_config
            from generators.dispatcher import generate_catalogs
            await generate_catalogs(
                config=load_catalog_config(), generators=["diary"]
            )
            logger.info("Regenerated diary catalog")
        except Exception as e:
            logger.warning("generate_catalog failed (non-fatal): %s", e)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=DEFAULT_DAYS, metavar="N",
                        help=f"Process last N days (default: {DEFAULT_DAYS})")
    window.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Process sessions since this date")
    window.add_argument("--all", action="store_true",
                        help="Process entire history (no date window)")
    parser.add_argument("--projects", metavar="SLUG,...",
                        help="Comma-separated project slugs to include")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        metavar="N", help=f"Max parallel LLM calls (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List in-window sessions + token estimate; no writes")
    parser.add_argument("--no-catalogs", action="store_true",
                        help="Skip catalog regeneration after backfill")
    parser.add_argument("--no-now", action="store_true",
                        help="Skip now/ regeneration after backfill")
    args = parser.parse_args()

    if args.all:
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    elif args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Invalid date: {args.since!r} — expected YYYY-MM-DD")
            sys.exit(1)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    project_slugs = [s.strip() for s in args.projects.split(",")] if args.projects else None

    if not args.dry_run:
        print(
            "Privacy notice: backfill reads ALL local Claude Code transcripts "
            "across every project and feeds them to the extraction model "
            "(indirect prompt-injection surface into memory)."
        )
        if project_slugs:
            print(f"Scoped to projects: {', '.join(project_slugs)}")
        else:
            print("Tip: use --projects SLUG,... to scope. Review results with "
                  "/multiplai-context:dream-remember before applying.")

    summary = asyncio.run(backfill(
        since,
        project_slugs=project_slugs,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        run_catalogs=not args.no_catalogs,
        run_now=not args.no_now,
    ))

    if args.dry_run:
        print(f"\n[dry-run] {summary['scanned']} sessions in window")
        print(f"[dry-run] Estimated tokens: {summary.get('total_est_tokens', '?')}")
        print("[dry-run] Privacy notice: backfill reads ALL local Claude Code transcripts.")
        if project_slugs:
            print(f"[dry-run] Scoped to projects: {', '.join(project_slugs)}")
        else:
            print("[dry-run] Use --projects to scope to specific projects.")
        for s in summary.get("sessions", []):
            print(f"  {s['session_id']}: ~{s['est_tokens']} tokens — {s['path']}")
    else:
        print(
            f"Backfill complete: {summary['scanned']} scanned, "
            f"{summary['written']} written, "
            f"{summary['skipped']} skipped, "
            f"{summary['errored']} errored"
        )
        print("Run /multiplai-context:dream then /multiplai-context:dream-remember to consolidate new learnings.")


if __name__ == "__main__":
    main()
