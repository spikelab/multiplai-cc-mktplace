# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core[sdk] @ git+https://github.com/spikelab/multiplai-core@v0.6.0"]
# ///
# NOTE: the [sdk] extra matters — run_agent needs claude-agent-sdk in this
# ephemeral env. Only this script pays that install; the per-turn hooks
# (session_stop, checkpoint_nudge) stay SDK-free and fast.
"""Detached checkpoint writer (spawned by the Stop hook).

Independent extraction, MiMo-style: the *main* session never summarizes its
own state — this subprocess reads the transcript, distills it, and asks a
fresh model call to produce/refresh the structured 11-field
``checkpoint.md``. Incremental: only turns newer than the previous
checkpoint are distilled and merged into the prior checkpoint text.

Invoked detached (``start_new_session=True``) with a JSON payload on stdin:

    {"session_id": ..., "transcript_path": ..., "cwd": ..., "tokens": N,
     "reason": "band"|"refresh"}

Failure policy: any error leaves the previous checkpoint.md untouched and
releases the single-flight marker — a stale checkpoint at handoff beats a
blocked session. The nested SDK call goes through multiplai-core
``run_agent`` (bypass/isolation bundle: setting_sources=[],
strict-mcp-config, _HOOK_CHILD_SESSION=1), so it can never recurse into
hooks, goals, or account MCP servers.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.agent_runner import run_agent
from multiplai_core.log_utils import setup_logging, log_event
from multiplai_core.model_client import DEFAULT_MODEL
from multiplai_core.paths import get_paths
from lib import checkpoint as cp
from lib.transcript_distiller import distill

logger = setup_logging("checkpoint_writer")

# Cap on distilled transcript characters fed to the writer model. When the
# segment exceeds it, keep the head (task framing) and a larger tail (recent
# work) — the middle is what the previous checkpoint already covers.
_MAX_SEGMENT_CHARS = 240_000
_HEAD_FRACTION = 0.3

_WRITER_SYSTEM_PROMPT = """\
You are a session-state archivist. You read a distilled coding-session
transcript and produce a checkpoint document that lets a fresh session
resume the work seamlessly. You never invent state that is not evidenced in
the transcript. You write terse, factual bullets. File paths are absolute.
"""

_WRITER_PROMPT = """\
{previous_block}Below is a distilled transcript segment of a Claude Code session{increment_note}.

Produce the complete, current checkpoint as Markdown with EXACTLY these H2
sections, in this order:

## Current intent
## Next action
## Working constraints
## Task tree
## Current work
## Involved files
## Errors and fixes
## Cross-task discoveries
## Runtime state
## Design decisions
## Notes

Rules:
- {merge_rule}
- 'Task tree': bulleted tasks with status markers [done]/[in-progress]/[pending].
- 'Next action': the single most concrete next step.
- 'Involved files': absolute paths, one line each, with why the file matters.
- 'Errors and fixes': what failed and what resolved it (keep resolved ones — they prevent repeats).
- 'Runtime state': running processes, env vars, ports, active branches/worktrees.
- Terse bullets, no prose paragraphs, no commentary outside the sections.
- Output ONLY the checkpoint markdown, starting with '## Current intent'.

--- TRANSCRIPT SEGMENT ---
{segment}
--- END TRANSCRIPT SEGMENT ---
"""

_MERGE_RULE_FRESH = "Build the checkpoint from the transcript segment alone."
_MERGE_RULE_UPDATE = (
    "UPDATE the previous checkpoint in place with the new segment: carry "
    "forward still-true state, mark newly completed tasks [done], replace "
    "stale 'Current work'/'Next action', never append duplicates."
)


def _cap_segment(text: str) -> str:
    if len(text) <= _MAX_SEGMENT_CHARS:
        return text
    head = int(_MAX_SEGMENT_CHARS * _HEAD_FRACTION)
    tail = _MAX_SEGMENT_CHARS - head
    return (
        text[:head]
        + "\n\n[… middle of segment elided for length …]\n\n"
        + text[-tail:]
    )


def _distill_segment(transcript_path: str, since_iso: str | None) -> str:
    """Distill transcript turns newer than *since_iso* into one text blob."""
    since = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            since = None
    chunks = distill(Path(transcript_path), since=since)
    return _cap_segment("\n".join(chunks))


def build_writer_prompt(previous_checkpoint: str, segment: str) -> str:
    """Assemble the writer prompt (exposed for tests)."""
    if previous_checkpoint.strip():
        previous_block = (
            "--- PREVIOUS CHECKPOINT ---\n"
            + previous_checkpoint.strip()
            + "\n--- END PREVIOUS CHECKPOINT ---\n\n"
        )
        increment_note = " (only turns SINCE the previous checkpoint)"
        merge_rule = _MERGE_RULE_UPDATE
    else:
        previous_block = ""
        increment_note = ""
        merge_rule = _MERGE_RULE_FRESH
    return _WRITER_PROMPT.format(
        previous_block=previous_block,
        increment_note=increment_note,
        merge_rule=merge_rule,
        segment=segment,
    )


async def write_checkpoint(payload: dict) -> bool:
    """Produce/refresh checkpoint.md for the session in *payload*.

    Returns True on success. Never raises — all failures are logged and
    reported as False so the previous checkpoint stays authoritative.
    """
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or ""
    tokens = int(payload.get("tokens") or 0)
    reason = payload.get("reason") or "band"

    if not session_id or not transcript_path:
        logger.warning("Missing session_id/transcript_path in payload; skipping")
        return False
    if cp.is_child_session(transcript_path):
        logger.info("Child session %s — checkpoint skipped", session_id)
        return False

    cfg = cp.load_config()
    data_dir = get_paths().plugin_data()
    state = cp.load_state(data_dir, session_id)

    try:
        segment = _distill_segment(transcript_path, state.get("last_checkpoint_ts"))
    except Exception:
        logger.exception("Distillation failed for %s", transcript_path)
        return False
    if not segment.strip():
        logger.info("No new transcript content for %s; checkpoint skipped", session_id)
        return False

    previous = ""
    cp_file = cp.checkpoint_file(data_dir, session_id)
    if cp_file.exists():
        try:
            previous = cp_file.read_text(encoding="utf-8")
        except OSError:
            previous = ""

    prompt = build_writer_prompt(previous, segment)
    try:
        result = await run_agent(
            prompt,
            system_prompt=_WRITER_SYSTEM_PROMPT,
            model=cfg.model or DEFAULT_MODEL,
            timeout_s=float(cfg.timeout_s),
            max_attempts=2,
            label=f"checkpoint:{session_id[:8]}",
        )
    except Exception as e:
        logger.error("Checkpoint model call failed for %s: %s", session_id, e)
        return False

    text = (result.text or "").strip()
    if not cp.validate_checkpoint(text):
        logger.warning(
            "Writer output failed validation for %s (%d chars); keeping previous",
            session_id, len(text),
        )
        return False

    cp.write_checkpoint_file(data_dir, session_id, text)
    now_iso = datetime.now(timezone.utc).isoformat()
    state.update(
        {
            "session_id": session_id,
            "last_band_idx": max(
                cp.band_index(tokens, cfg.bands),
                int(state.get("last_band_idx") or 0),
            ),
            "last_checkpoint_tokens": tokens,
            "last_checkpoint_ts": now_iso,
            "last_reason": reason,
        }
    )
    cp.save_state(data_dir, session_id, state)

    if tokens >= cfg.handoff_tokens:
        cp.write_pending_marker(data_dir, cwd, session_id, tokens)

    logger.info(
        "Checkpoint written for %s (%d tokens, reason=%s, handoff=%s)",
        session_id, tokens, reason, tokens >= cfg.handoff_tokens,
    )
    log_event(
        "checkpoint", "write",
        f"checkpoint saved at {tokens:,} tokens ({reason})"
        + (" — handoff ready" if tokens >= cfg.handoff_tokens else ""),
        session_id=session_id,
        tokens=tokens,
        reason=reason,
    )
    return True


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        logger.warning("Unparseable checkpoint payload; exiting")
        return
    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id") or ""
    setup_logging("checkpoint_writer", session_id=session_id)
    data_dir = get_paths().plugin_data()
    try:
        asyncio.run(write_checkpoint(payload))
    finally:
        # Always release the single-flight marker claimed by the Stop hook.
        if session_id:
            try:
                cp.release_writer(data_dir, session_id)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            logger.exception("checkpoint_writer failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
