# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ file:///Users/spike/Documents/knowhere/PROJECTS/multiplai-core"]
# ///
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
from lib.extraction import extract_units, write_diary_entries, append_learnings
from lib.transcript_distiller import distill

logger = setup_logging("extract_learnings")


def _distill_transcript(transcript_path: str, raw_transcript: str) -> list[str]:
    """Distill a transcript into token-bounded chunks before the LLM call.

    Prefers the on-disk JSONL path; falls back to raw JSONL piped on stdin
    (staged to a temp file, since the distiller reads from a path). Returns
    an empty list when there is nothing to extract (missing/empty
    transcript) — the caller then drops the marker instead of retrying.
    """
    if transcript_path:
        p = Path(transcript_path)
        if not p.exists():
            logger.info("Transcript gone: %s — nothing to extract", transcript_path)
            return []
        try:
            return distill(p)
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
                return distill(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Distillation of raw piped transcript failed")
            return []

    return []


def _list_valid_targets(memory_dir: Path) -> list[str]:
    if not memory_dir.exists():
        return []
    return sorted(p.name for p in memory_dir.glob("*.md") if p.is_file())


def _drop_marker(marker_path: str) -> None:
    """Delete the processing marker once this session is fully handled."""
    if marker_path:
        try:
            Path(marker_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove processed marker %s: %s", marker_path, e)


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

        await synthesize(project_filter=project)
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
    cwd = _field("cwd")
    transcript_path = _field("transcript_path")
    # Back-compat: a raw transcript may still arrive inline, or as bare
    # (non-JSON) stdin from a direct invocation.
    raw_transcript = _field("transcript") or (hook_input if not transcript_data else "")

    chunks = _distill_transcript(transcript_path, raw_transcript)

    valid_targets = _list_valid_targets(memory_dir)
    units: list[dict] = []
    llm_failed = False
    if chunks:
        try:
            client = await create_client()
            logger.info(
                "Extract learnings using %s (%d chunk(s))",
                type(client).__name__, len(chunks),
            )
            for i, chunk in enumerate(chunks):
                try:
                    chunk_units = await extract_units(
                        chunk,
                        valid_targets=valid_targets,
                        client=client,
                    )
                    units.extend(chunk_units)
                except Exception:
                    logger.exception(
                        "LLM call failed during extraction (chunk %d/%d)",
                        i + 1, len(chunks),
                    )
                    llm_failed = True
        except Exception:
            logger.exception("Could not create model client for extraction")
            llm_failed = True

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
        diary_path = write_diary_entries(units, diary_dir, session_id, cwd, timestamp)
        if diary_path:
            logger.info("Wrote diary entry to %s", diary_path)
            log_event(
                "diary", "write",
                f"wrote diary entry ({len(units)} unit(s)) to {Path(diary_path).name}",
                session_id=session_id,
                units=len(units),
                path=str(diary_path),
            )
            await _refresh_now(cwd, session_id)

    wrote = append_learnings(units, learnings_file, session_id, timestamp)
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
