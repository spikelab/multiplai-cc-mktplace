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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.log_utils import setup_logging, log_event
from lib.correction_patterns import detect_corrections_in_transcript
from lib.extraction import extract_units, write_diary_entries, append_learnings

logger = setup_logging("extract_learnings")


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
        transcript = transcript_data.get("transcript", hook_input)
    except (json.JSONDecodeError, AttributeError):
        transcript = hook_input

    marker_path = (
        transcript_data.get("marker_path", "")
        if isinstance(transcript_data, dict)
        else ""
    )

    session_id = (
        transcript_data.get("session_id", "")
        if isinstance(transcript_data, dict)
        else ""
    )
    cwd = (
        transcript_data.get("cwd", "")
        if isinstance(transcript_data, dict)
        else ""
    )

    valid_targets = _list_valid_targets(memory_dir)
    units: list[dict] = []
    llm_failed = False
    try:
        client = await create_client()
        logger.info("Extract learnings using %s", type(client).__name__)
        units = await extract_units(
            transcript,
            valid_targets=valid_targets,
            client=client,
        )
    except Exception:
        logger.exception("LLM call failed during learning extraction")
        llm_failed = True

    correction_matches = detect_corrections_in_transcript(transcript)

    if not units and not correction_matches:
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

    wrote = append_learnings(units, learnings_file, session_id, correction_matches, timestamp)
    if wrote:
        logger.info("Appended structured learnings to %s", learnings_file)
        n_learnings = sum(len(u.get("learnings") or []) for u in units)
        log_event(
            "learnings", "capture",
            f"captured {n_learnings} learning(s) + {len(correction_matches)} correction(s) to backlog",
            session_id=session_id,
            learnings=n_learnings,
            corrections=len(correction_matches),
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
