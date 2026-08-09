"""Structured learning extraction (Stop hook).

Decomposes the session transcript into logical units of work, writes a
rich diary entry per unit to ``diary/YYYY-MM-DD/<sessionId>.md``, and
appends typed learnings to the per-day ``learnings/YYYY-MM-DD.md`` file.

Diary is PRIMARY — learnings are a projection of it. See lib/extraction.py
for the canonical data contract and shared helpers.
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.log_utils import setup_logging, log_event
from lib import utilisation
from lib.extraction import (
    DEFAULT_DISPOSITION,
    clear_diary_bookmark,
    extract_session_signals,
    load_diary_bookmark,
    load_target_charters,
    save_diary_bookmark,
    slice_key,
    write_diary_entries,
    append_learnings,
)
from lib.session_registry import record_disposition
from lib.transcript_distiller import distill, iter_distilled_turns

logger = setup_logging("extract_learnings")


def _last_turn_ts(path: Path, since: datetime | None) -> datetime | None:
    """The newest timestamp in the slice that was just distilled.

    Deliberately the last turn READ, never ``now``. ``iter_distilled_turns``
    keeps turns with ``ts >= since``, so bookmarking the last turn re-reads
    exactly one boundary turn on the next pass. Bookmarking ``now`` instead
    would skip anything written to the transcript between the read and the
    write — and the safe direction here is duplicating a turn, never losing
    one.
    """
    last: datetime | None = None
    try:
        for turn in iter_distilled_turns(path, since=since):
            ts = turn.get("ts")
            if ts is not None and (last is None or ts > last):
                last = ts
    except Exception:
        logger.exception("Could not determine the last turn timestamp for %s", path)
        return None
    return last


def _distill_transcript(
    transcript_path: str, raw_transcript: str, since: datetime | None = None
) -> list[str]:
    """Distill the slice newer than *since* into token-bounded chunks.

    Prefers the on-disk JSONL path; falls back to raw JSONL piped on stdin
    (staged to a temp file, since the distiller reads from a path). Returns
    an empty list when there is nothing to extract — a missing/empty
    transcript, or nothing new since the bookmark — and the caller then drops
    the marker instead of retrying.
    """
    if transcript_path:
        p = Path(transcript_path)
        if not p.exists():
            logger.info("Transcript gone: %s — nothing to extract", transcript_path)
            return []
        try:
            return distill(p, since=since)
        except Exception:
            logger.exception("Distillation failed for %s", transcript_path)
            return []

    if raw_transcript.strip():
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(raw_transcript)
                tmp_path = Path(tmp.name)
            try:
                return distill(tmp_path, since=since)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Distillation of raw piped transcript failed")
            return []

    return []


def _drop_marker(marker_path: str) -> None:
    """Delete the processing marker once this session is fully handled."""
    if marker_path:
        try:
            Path(marker_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove processed marker %s: %s", marker_path, e)


def _retire_checkpoint(
    data_dir: Path,
    session_id: str,
    disposition: dict,
    *,
    fully_extracted: bool = True,
    trigger: str = "",
) -> None:
    """Collect this session's checkpoint, now that the diary supersedes it.

    Called on the one edge that makes it safe: a diary entry for the session
    now exists on disk. A checkpoint is *live state* — roughly where a session
    is right now — and the diary is the permanent record of what it did, so
    past that edge the directory is dead weight. It never was collected: 182
    of them had accumulated by 2026-07-31, one per session ever run.

    Retirement deletes data, so it demands MORE than the diary write did:

    * ``fully_extracted`` — every chunk succeeded AND the final chunk produced
      a real disposition. A partial failure still writes a diary entry from the
      surviving units, but that entry covers only part of the session, and a
      failed final chunk leaves ``disposition`` at the fabricated default —
      the same fabrication ``record_disposition`` refuses to write must not
      drive an irreversible delete three lines later.
    * ``trigger != "pre_compact"`` — a PreCompact-deferred extraction runs
      against a session that is STILL RUNNING under the same session_id after
      compaction. Its checkpoint (``state.json``'s ``rebuild_ts``, the
      incrementally merged ``checkpoint.md``) is live working state, not a
      leftover.

    **A parked session is the deliberate exception.** ``AGENTS.md`` renders its
    intent, next action and files-in-hand from the checkpoint, and a parked
    session is precisely the one still being listed weeks later — deleting its
    checkpoint would leave the registry entry it deliberately kept alive with
    nothing to say. (The other two guards, an in-flight writer and an
    unconsumed rebuild marker, live in ``checkpoint.retire_checkpoint``: they
    are facts about the checkpoint store, not about this pipeline.)

    Best-effort by construction. Disk is not worth a diary entry, so nothing
    here may raise into the extraction path.
    """
    if not session_id:
        return
    if not fully_extracted:
        logger.info(
            "Extraction for %s was incomplete; keeping its checkpoint "
            "(the diary does not fully supersede it)", session_id,
        )
        return
    if trigger == "pre_compact":
        logger.info(
            "Extraction for %s was compaction-deferred and the session is "
            "still live; keeping its checkpoint", session_id,
        )
        return
    state = (disposition or {}).get("state") or DEFAULT_DISPOSITION
    if state == "parked":
        logger.info("Session %s is parked; keeping its checkpoint", session_id)
        return
    try:
        from lib import checkpoint as cp

        removed, kept = cp.retire_checkpoint(data_dir, session_id)
    except Exception:
        logger.exception("Checkpoint retirement failed for %s (non-fatal)", session_id)
        return
    if kept:
        logger.info("Kept checkpoint for %s: %s", session_id, kept)
    elif removed:
        # Same edge, same reasoning: the session is finished with and its
        # permanent record exists, so its reading bookmark is dead weight too.
        clear_diary_bookmark(data_dir, session_id)
        log_event(
            "checkpoint", "retire",
            "retired checkpoint — superseded by the diary entry",
            session_id=session_id,
        )


def _record_injected(paths, session_id: str, transcript_path: str) -> list[str]:
    """Persist this session's ``injected`` half and return its section keys.

    Read back out of the ``inject`` events rather than written from the hook:
    ``context_manager`` runs on **every prompt**, and a read-modify-write of an
    accumulating file on that path buys latency on the one code path where
    latency is visible. The activity log already holds exactly this data (P1's
    ``sections_by_file``/``bytes_by_file``), it is written for free, and its
    7-day retention comfortably outlives the gap between a session ending and
    its deferred extraction draining.

    Best-effort by construction: telemetry must never cost a diary entry.
    """
    if not session_id:
        return []
    try:
        events = utilisation.inject_events_for_session(paths.logs_dir(), session_id)
        injected = utilisation.injected_from_inject_events(events)
        if not injected:
            return []
        utilisation.record_injections(
            utilisation.utilisation_path(paths.data_dir()),
            session_id[:8],
            injected,
            session_id=session_id,
            transcript=transcript_path or "",
        )
        keys = []
        for entry in injected:
            key = utilisation.section_key(entry["file"], entry.get("section"))
            if key not in keys:
                keys.append(key)
        logger.info(
            "Recorded %d injection(s) across %d section(s) for utilisation",
            len(injected), len(keys),
        )
        return keys
    except Exception:
        logger.exception("Could not record injected memory for utilisation (non-fatal)")
        return []


def _record_self_report(paths, session_id: str, claims: list[dict],
                        injected_keys: list[str]) -> None:
    """Persist estimator A's answer, filtered to what was actually injected.

    Two filters, both fail-closed. A claim naming something the session never
    had injected is dropped outright — the model does not get to invent an
    observation. A claim with no evidence is *kept* but marked
    ``supported: False``, so it counts in the denominator and never in the
    numerator: over-reporting shows up as a low rate rather than disappearing.
    """
    if not session_id:
        return
    try:
        allowed = set(injected_keys)
        entries = [
            claim for claim in claims
            if utilisation.section_key(claim["file"], claim.get("section")) in allowed
        ]
        utilisation.record_self_report(
            utilisation.utilisation_path(paths.data_dir()), session_id[:8], entries,
        )
        supported = sum(1 for e in entries if e.get("supported"))
        logger.info(
            "Self-reported utilisation: %d claim(s), %d with evidence, "
            "out of %d injected section(s)",
            len(entries), supported, len(injected_keys),
        )
        log_event(
            "utilisation", "self-report",
            f"session claimed {supported} of {len(injected_keys)} injected "
            f"memory section(s) as used (estimated, self-reported)",
            session_id=session_id,
            claimed=len(entries),
            supported=supported,
            injected=len(injected_keys),
        )
    except Exception:
        logger.exception("Could not record self-reported utilisation (non-fatal)")


async def _refresh_now(cwd: str, session_id: str) -> None:
    """Re-summarize this session's project ``now`` file after a diary write.

    Keeps ``now/<project>.md`` current on the live pipeline (it used to refresh
    only during a backfill). Scoped to the one project this session belongs to,
    so it's a single summary call. Best-effort: any failure is logged and
    swallowed — a stale ``now`` file must never break extraction.
    """
    from lib.project_identity import resolve_project

    project = resolve_project(cwd)
    if not project:
        return
    try:
        from synthesize_now import synthesize

        await synthesize(project_filter=project, session_id=session_id)
        logger.info("Refreshed now/%s.md", project)
        log_event(
            "now", "refresh",
            f"refreshed now/{project}.md after diary write",
            session_id=session_id,
            project=project,
        )
    except Exception:
        logger.exception("now refresh failed for project %s (non-fatal)", project)


async def extract() -> bool:
    """Process one deferred session.

    Returns True when the session was handled (written, or there was
    genuinely nothing to write) — caller may drop the marker. Returns
    False when extraction FAILED (LLM/transient error) so the marker is
    retained for stale-recovery retry by the next SessionStart.
    """
    paths = get_paths()
    memory_dir = paths.memory_dir()
    learnings_file = paths.learnings_file()
    diary_dir = paths.diary_dir()

    hook_input = sys.stdin.read()
    if not hook_input.strip():
        logger.info("No session data on stdin, skipping extraction")
        return True

    transcript_data: dict = {}
    try:
        transcript_data = json.loads(hook_input)
    except (json.JSONDecodeError, AttributeError):
        transcript_data = {}

    def _field(key: str) -> str:
        return transcript_data.get(key, "") if isinstance(transcript_data, dict) else ""

    marker_path = _field("marker_path")
    session_id = _field("session_id")
    trigger = _field("trigger")
    setup_logging("extract_learnings", session_id=session_id)
    cwd = _field("cwd")
    transcript_path = _field("transcript_path")
    # Back-compat: a raw transcript may still arrive inline, or as bare
    # (non-JSON) stdin from a direct invocation.
    raw_transcript = _field("transcript") or (hook_input if not transcript_data else "")

    # Read forward from where the last successful extraction stopped. A
    # session that extracts twice (PreCompact, then SessionEnd) used to
    # re-read its whole transcript the second time; now the second pass sees
    # only what the first one could not.
    since = load_diary_bookmark(paths.data_dir(), session_id)
    chunks = _distill_transcript(transcript_path, raw_transcript, since)
    this_slice = slice_key(session_id, since)
    # Only the on-disk path bookmarks. The raw-stdin fallback stages a temp
    # file that is deleted on the way out, so there is nothing stable for a
    # bookmark to point into — that path keeps re-reading whatever it is
    # handed, exactly as before.
    last_ts = (
        _last_turn_ts(Path(transcript_path), since)
        if chunks and transcript_path and Path(transcript_path).exists()
        else None
    )
    if since:
        logger.info(
            "Extracting the slice after %s for %s (%d chunk(s))",
            since.isoformat(), session_id, len(chunks),
        )

    # Before the model call, and independent of it: the retrieval half of the
    # utilisation record needs no LLM, so it survives a client that cannot be
    # created (contract C3 — P3 records no *estimate* without a model, but
    # what was injected is a fact we already have).
    injected_keys = _record_injected(paths, session_id, transcript_path)

    valid_targets = load_target_charters(memory_dir, paths.catalogs_dir())
    units: list[dict] = []
    # Self-reported utilisation, unioned across chunks (unlike the disposition,
    # which is a property of the closing exchange only). First evidence wins.
    utilisation_claims: dict[tuple[str, str | None], dict] = {}
    # Disposition is session-level but extraction runs per chunk, so only the
    # FINAL chunk's answer counts — that is the one holding the closing
    # exchange, and the closing exchange is the entire signal. Earlier chunks
    # end mid-work and would always say "active".
    disposition = {"state": DEFAULT_DISPOSITION, "reason": ""}
    llm_failed = False
    # The disposition write is gated on the FINAL chunk specifically — not on
    # "no chunk failed". An earlier chunk's failure costs some diary units,
    # but the closing exchange still parsed fine; gating on any-chunk failure
    # silently lost a valid `parked` forever (the surviving units meant the
    # diary was written and the marker consumed, so there was no retry).
    # With no chunks there was no LLM pass to fail, so the default `active`
    # is a fact, not a guess.
    final_chunk_ok = not chunks
    # Whether the model was asked the utilisation question at all. An empty
    # self-report is only meaningful as "none of them" if a chunk came back;
    # writing `[]` after a total failure would fabricate an answer, and every
    # injected section would read as estimated-unused.
    chunks_ok = 0
    utilisation_missing = 0
    if chunks:
        try:
            client = await create_client()
            logger.info(
                "Extract learnings using %s (%d chunk(s))",
                type(client).__name__, len(chunks),
            )
            for i, chunk in enumerate(chunks):
                try:
                    (
                        chunk_units,
                        chunk_disposition,
                        chunk_utilisation,
                    ) = await extract_session_signals(
                        chunk,
                        valid_targets=valid_targets,
                        client=client,
                        injected_sections=injected_keys,
                    )
                    units.extend(chunk_units)
                    # None means the reply carried no <utilisation> block at
                    # all — most often a truncated response, since that block
                    # is emitted last. Counted separately from an explicitly
                    # empty one, which is a real "none of them".
                    if chunk_utilisation is None:
                        utilisation_missing += 1
                    else:
                        for claim in chunk_utilisation:
                            utilisation_claims.setdefault(
                                (claim["file"], claim.get("section")), claim,
                            )
                    chunks_ok += 1
                    if i == len(chunks) - 1:
                        disposition = chunk_disposition
                        final_chunk_ok = True
                except Exception:
                    logger.exception(
                        "LLM call failed during extraction (chunk %d/%d)",
                        i + 1, len(chunks),
                    )
                    llm_failed = True
        except Exception:
            logger.exception("Could not create model client for extraction")
            llm_failed = True

    # Third projection of the same pass, beside the diary and the learnings
    # backlog. Recorded here rather than down in the write section so that a
    # session with nothing worth a diary entry — "park it, I'm out" and
    # little else — still gets labelled. Skipped when the FINAL chunk failed:
    # the disposition rides only on that chunk, so a fabricated "active"
    # would be a guess written as a fact (and would strip a parked session's
    # GC protection on the way).
    if final_chunk_ok and session_id:
        state = disposition.get("state") or DEFAULT_DISPOSITION
        recorded = record_disposition(
            paths.data_dir(), session_id, state, disposition.get("reason", "")
        )
        if recorded and state != DEFAULT_DISPOSITION:
            logger.info("Session %s recorded as %s", session_id, state)
            log_event(
                "session", "disposition",
                f"session left {state}: {disposition.get('reason', '')}".strip(),
                session_id=session_id,
                disposition=state,
            )
        elif not recorded and state != DEFAULT_DISPOSITION:
            # Losing a `parked`/`done` label is user-visible (the session
            # vanishes from or lingers in AGENTS.md); a missing entry or a
            # lost lock must not be a debug-level shrug.
            logger.warning(
                "Could not record %s disposition for session %s "
                "(missing registry entry or lock lost); label dropped",
                state, session_id,
            )

    # Recorded only when the whole transcript was actually read AND every chunk
    # answered the utilisation question. Both conditions guard the same hazard:
    # the denominator is *every* injected section, so a partial numerator is
    # written as "observed, and mostly unused" rather than as "not estimated".
    #
    # `chunks_ok` alone was not enough. A 3-chunk session whose first two chunks
    # failed recorded "used in the last third of the transcript" as the whole
    # session's answer; and a chunk that succeeded but was truncated before its
    # <utilisation> block contributed nothing while still counting toward the
    # denominator. A null here is honest and costs one session's estimate; a
    # false zero puts a live memory section top of the doctor's pruning list.
    if injected_keys and chunks_ok == len(chunks) and not utilisation_missing:
        _record_self_report(
            paths, session_id, list(utilisation_claims.values()), injected_keys,
        )
    elif injected_keys and session_id:
        logger.warning(
            "Not recording self-reported utilisation for session %s: "
            "%d/%d chunk(s) parsed, %d without a <utilisation> block. "
            "Recording a partial answer against a full denominator would "
            "read as 'injected and unused'.",
            session_id[:8], chunks_ok, len(chunks), utilisation_missing,
        )

    if not units:
        if llm_failed:
            # Distinguish a real failure from a genuinely empty session:
            # keep the marker so the next SessionStart retries instead of
            # silently dropping the session's learnings.
            logger.warning("Extraction failed and produced nothing; retaining marker for retry")
            return False
        logger.info("No actionable content found, nothing to write")
        _drop_marker(marker_path)
        return True

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if units:
        diary_path = write_diary_entries(
            units, diary_dir, session_id, cwd, timestamp, slice_id=this_slice
        )
        if diary_path:
            # The bookmark moves ONLY here — after the permanent record for
            # this slice is on disk. Advancing it anywhere else (on a failed
            # LLM call, on an empty result) would skip the slice for good, and
            # re-reading a slice costs a distillation while losing one costs
            # the record.
            if last_ts is not None:
                save_diary_bookmark(paths.data_dir(), session_id, last_ts)
            logger.info("Wrote diary entry to %s", diary_path)
            log_event(
                "diary", "write",
                f"wrote diary entry ({len(units)} unit(s)) to {Path(diary_path).name}",
                session_id=session_id,
                units=len(units),
                path=str(diary_path),
            )
            await _refresh_now(cwd, session_id)
            _retire_checkpoint(
                paths.data_dir(), session_id, disposition,
                # Strictest coherent gate: retirement deletes data, so it
                # requires a FULLY successful extraction — no failed chunks
                # (a partial diary does not supersede the checkpoint) and a
                # real, non-fabricated disposition from the final chunk.
                fully_extracted=not llm_failed and final_chunk_ok,
                trigger=trigger,
            )

    wrote = append_learnings(
        units, learnings_file, session_id, timestamp, slice_id=this_slice
    )
    if wrote:
        logger.info("Appended structured learnings to %s", learnings_file)
        n_learnings = sum(len(u.get("learnings") or []) for u in units)
        log_event(
            "learnings", "capture",
            f"captured {n_learnings} learning(s) to backlog",
            session_id=session_id,
            learnings=n_learnings,
        )
    elif session_id:
        logger.info("Session %s already in %s, skipping", session_id, learnings_file)
        log_event(
            "learnings", "skip",
            "session already in learnings backlog — nothing new captured",
            session_id=session_id,
        )

    _drop_marker(marker_path)
    return True


def main() -> None:
    # On any unhandled exception the marker is intentionally NOT removed,
    # so the next SessionStart's stale-recovery retries this session.
    asyncio.run(extract())


if __name__ == "__main__":
    main()
