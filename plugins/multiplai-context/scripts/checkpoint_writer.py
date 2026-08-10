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
     "reason": "band"|"refresh"|"stale"}

Failure policy: never come back empty-handed. When the model call fails or
its output fails validation, the writer falls back to a **degraded write** —
the previous checkpoint verbatim plus an ``## Unsummarised since <ts>``
section built mechanically from the slice (user turns and tool names, no
model involved) — and only then advances the bookmark. Advancing it without
keeping the content would silently discard the window; keeping the content is
what makes advancing honest. With no previous checkpoint to carry there is
nothing that could satisfy ``validate_checkpoint`` without fabricating
sections, so that case writes nothing, counts the failure, and leaves the
bookmark where it is.

The nested SDK call goes through multiplai-core ``run_agent``
(bypass/isolation bundle: setting_sources=[], strict-mcp-config,
_HOOK_CHILD_SESSION=1), so it can never recurse into hooks, goals, or account
MCP servers.
"""

import asyncio
import json
import re
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

# Defined here rather than beside the rest of the degraded-write machinery
# below because _MERGE_RULE_UPDATE names it: the instruction to fold these
# sections in and drop them has to be a PROMPT rule the model is given, not a
# sentence sitting inside the checkpoint text where it is merely data.
_DEGRADED_HEADING = "## Unsummarised since"

_MERGE_RULE_FRESH = "Build the checkpoint from the transcript segment alone."
_MERGE_RULE_UPDATE = (
    "UPDATE the previous checkpoint in place with the new segment: carry "
    "forward still-true state, mark newly completed tasks [done], replace "
    "stale 'Current work'/'Next action', never append duplicates. "
    "If the previous checkpoint contains one or more "
    f"'{_DEGRADED_HEADING} <timestamp>' sections, those are raw turns from a "
    "window an earlier write could not summarise: fold their content into the "
    "numbered sections above and DO NOT reproduce the sections themselves. "
    "Your output must contain no such section."
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


# --------------------------------------------------------------------------
# Degraded write — what gets kept when the model call cannot be made to work
# --------------------------------------------------------------------------

# Bounded so a run of failures can't grow checkpoint.md without limit. The
# tail is kept, not the head: the most recent work is the part a rebuild
# needs.
_MAX_DEGRADED_CHARS = 20_000
# Consecutive degraded writes each append one section. Keep the newest few and
# drop the rest — older windows are the ones a successful write is most likely
# to have already folded in.
_MAX_DEGRADED_SECTIONS = 3

# The distiller's own line format: ``[ts] [project] role: text``.
_TURN_RE = re.compile(
    r"^\[(?P<ts>[^\]]*)\] \[[^\]]*\] (?P<role>user|assistant): (?P<text>.*)$"
)
_TOOL_RE = re.compile(r"\[call ([A-Za-z_][A-Za-z0-9_-]*)\(")
_DEGRADED_SECTION_RE = re.compile(rf"^{re.escape(_DEGRADED_HEADING)} ", re.MULTILINE)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def build_degraded_section(segment: str, since_iso: str | None) -> str:
    """Render the un-summarised slice as plain, mechanically-derived bullets.

    No model call and no inference: user turns are quoted (clipped) and
    assistant turns contribute the names of the tools they invoked. That is
    deliberately less than a checkpoint — it is the raw material a reader (or
    the next successful write) can work from, labelled as such.
    """
    # One turn spans several lines — the distiller emits the tool_use and
    # tool_result stubs under the turn's header line — so accumulate a turn
    # until the next header rather than reading line by line.
    lines: list[str] = []
    header: re.Match[str] | None = None
    body: list[str] = []

    def flush() -> None:
        if header is None:
            return
        ts, role = header.group("ts"), header.group("role")
        text = "\n".join([header.group("text"), *body])
        if role == "user":
            spoken = "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("[")
            ).strip()
            if spoken:
                lines.append(f"- [{ts}] user: {_clip(spoken, 300)}")
            return
        tools = list(dict.fromkeys(_TOOL_RE.findall(text)))
        if tools:
            lines.append(f"- [{ts}] tools: {', '.join(tools[:12])}")

    for raw in segment.splitlines():
        m = _TURN_RE.match(raw)
        if m:
            flush()
            header, body = m, []
        elif header is not None:
            body.append(raw)
    flush()

    # Keep the tail within the budget.
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > _MAX_DEGRADED_CHARS:
            kept.append("- [… earlier turns in this window elided for length …]")
            break
        kept.append(line)
        used += len(line)
    kept.reverse()

    rendered = "\n".join(kept) or "- (no user turns or tool calls in this window)"
    return (
        f"{_DEGRADED_HEADING} {since_iso or 'the start of the session'}\n\n"
        "- The model call that would have folded this window into the sections "
        "above failed; its raw material is kept here rather than discarded.\n"
        "- Everything above this heading is the previous checkpoint, unchanged.\n"
        "- On the next successful checkpoint, fold these into the sections "
        "above and drop this section.\n"
        f"{rendered}\n"
    )


def _trim_degraded_sections(text: str) -> str:
    """Keep at most ``_MAX_DEGRADED_SECTIONS`` degraded sections, newest last."""
    starts = [m.start() for m in _DEGRADED_SECTION_RE.finditer(text)]
    if len(starts) <= _MAX_DEGRADED_SECTIONS:
        return text
    cut_to = starts[len(starts) - _MAX_DEGRADED_SECTIONS]
    return text[: starts[0]] + text[cut_to:]


def build_degraded_checkpoint(
    previous: str, segment: str, since_iso: str | None
) -> str:
    """Previous checkpoint + the raw slice, or ``""`` when that is impossible.

    Empty when there is no previous checkpoint that already passes
    ``validate_checkpoint``. Manufacturing the six sections the validator
    requires would mean inventing their content, and a validator that accepts
    fabricated sections is worse than the bug this fallback exists for.
    """
    if not cp.validate_checkpoint(previous or ""):
        return ""
    merged = previous.rstrip() + "\n\n" + build_degraded_section(segment, since_iso)
    return _trim_degraded_sections(merged)


def _record_failure(data_dir: Path, session_id: str, state: dict, why: str) -> None:
    """Count a failed write so the Stop hook can surface a run of them."""
    state[cp.CONSECUTIVE_FAILURE_KEY] = cp.consecutive_failures(state) + 1
    state["session_id"] = session_id
    state["last_failure"] = why
    state["last_failure_ts"] = datetime.now(timezone.utc).isoformat()
    try:
        cp.save_state(data_dir, session_id, state)
    except OSError:
        logger.warning("Could not persist failure count for %s", session_id)


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
    since_iso = state.get("last_checkpoint_ts")

    try:
        segment = _distill_segment(transcript_path, since_iso)
    except Exception as e:
        logger.exception("Distillation failed for %s", transcript_path)
        _record_failure(data_dir, session_id, state, f"distillation failed: {e}")
        return False
    if not segment.strip():
        # Not a failure: there is genuinely nothing new to fold in. Leave the
        # counter alone so an idle session never reads as degraded.
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
    text = ""
    failure = ""
    try:
        result = await run_agent(
            prompt,
            system_prompt=_WRITER_SYSTEM_PROMPT,
            model=cfg.model or DEFAULT_MODEL,
            timeout_s=float(cfg.timeout_s),
            max_attempts=2,
            label=f"checkpoint:{session_id[:8]}",
            # Tags the run in the cost ledger. Without it ``_record_cost``
            # returns early and checkpoint writes are invisible there, which
            # is why the cadence could never be costed before now.
            component="checkpoint",
        )
    except Exception as e:
        logger.error("Checkpoint model call failed for %s: %s", session_id, e)
        failure = f"model call failed: {e}"
    else:
        text = (result.text or "").strip()
        if not cp.validate_checkpoint(text):
            logger.warning(
                "Writer output failed validation for %s (%d chars)",
                session_id, len(text),
            )
            failure = f"output failed validation ({len(text)} chars)"
            text = ""

    degraded = False
    if failure:
        text = build_degraded_checkpoint(previous, segment, since_iso)
        if not text:
            # Nothing to carry forward, and the six sections the validator
            # requires cannot be filled without inventing them. Keep the
            # bookmark where it is so the window is retried, not skipped.
            logger.error(
                "Checkpoint failed for %s with no previous checkpoint to "
                "degrade onto (%s); nothing written", session_id, failure,
            )
            _record_failure(data_dir, session_id, state, failure)
            return False
        degraded = True
    else:
        # The prompt tells the model to fold any degraded sections in and emit
        # none. If it copies them through anyway, bound the growth here rather
        # than stripping them: stripping would destroy the raw window in the
        # one case where the model did NOT fold it in, which is the failure
        # this whole fallback exists to prevent.
        text = _trim_degraded_sections(text)

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
            # Advanced on a degraded write too — the slice it covers is inside
            # the file, so moving the bookmark discards nothing. This is what
            # stops a failure from handing the next attempt a bigger segment
            # than the one it just lost on.
            "last_checkpoint_ts": now_iso,
            "last_reason": f"{reason}-degraded" if degraded else reason,
            cp.CONSECUTIVE_FAILURE_KEY: (
                cp.consecutive_failures(state) + 1 if degraded else 0
            ),
        }
    )
    if degraded:
        state["last_failure"] = failure
        state["last_failure_ts"] = now_iso
    else:
        state.pop("last_failure", None)
        state.pop("last_failure_ts", None)
    cp.save_state(data_dir, session_id, state)

    # Unconditional: the handoff threshold governs the /clear nudge, not
    # whether a checkpoint is restorable. A session that ended at 143K tokens
    # used to leave a perfectly good checkpoint that nothing could ever find.
    cp.write_pending_marker(data_dir, cwd, session_id, tokens)

    logger.info(
        "Checkpoint %s for %s (%d tokens, reason=%s, handoff=%s)",
        "degraded" if degraded else "written",
        session_id, tokens, reason, tokens >= cfg.handoff_tokens,
    )
    log_event(
        "checkpoint", "degraded" if degraded else "write",
        (
            f"checkpoint DEGRADED at {tokens:,} tokens ({reason}): {failure} — "
            "previous checkpoint kept plus the raw unsummarised window"
            if degraded
            else f"checkpoint saved at {tokens:,} tokens ({reason})"
            + (" — handoff ready" if tokens >= cfg.handoff_tokens else "")
        ),
        session_id=session_id,
        tokens=tokens,
        reason=reason,
    )
    return not degraded


def main() -> int:
    """Run one checkpoint write. Returns the process exit code.

    **Non-zero means this write failed or degraded.** That matters only on the
    queued path: ``lib.checkpoint_drain.process_pending_checkpoints(wait=True)``
    is the one caller that waits, and it is the only place a failed
    end-of-session checkpoint can still be reported — the session that queued
    it has ended, so ``session_stop``'s degraded message will never run for it
    again. A writer that always exited 0 made that report unreachable.

    The counter is the signal rather than ``write_checkpoint``'s bool because
    the bool is False for "nothing new to fold in" as well, and an idle session
    is not a failure. ``consecutive_failures`` moves on exactly the two
    outcomes that are.

    Detached spawns (``cp.spawn_writer``) are never waited on, so the code is
    ignored there.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        logger.warning("Unparseable checkpoint payload; exiting")
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = payload.get("session_id") or ""
    setup_logging("checkpoint_writer", session_id=session_id)
    data_dir = get_paths().plugin_data()
    before = (
        cp.consecutive_failures(cp.load_state(data_dir, session_id))
        if session_id else 0
    )
    try:
        asyncio.run(write_checkpoint(payload))
    finally:
        # Always release the single-flight marker claimed by the Stop hook.
        if session_id:
            try:
                cp.release_writer(data_dir, session_id)
            except OSError:
                pass
        # Queued-by-SessionEnd runs carry their queue marker. Dropped on the
        # way out whatever the outcome: the writer already re-reads the
        # transcript from its own bookmark, so a retry would repeat work the
        # degraded-write path has by then folded in. A child that dies before
        # reaching here leaves the marker for recover_stale_processing.
        marker_path = payload.get("marker_path") if isinstance(payload, dict) else ""
        if marker_path:
            try:
                Path(str(marker_path)).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove checkpoint marker %s", marker_path)

    after = (
        cp.consecutive_failures(cp.load_state(data_dir, session_id))
        if session_id else 0
    )
    return 1 if after > before else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        try:
            logger.exception("checkpoint_writer crashed")
        except Exception:
            pass
        # A crash is a failed write, and the waiting drain is the only thing
        # that reads this. Detached spawns never look, so this cannot break a
        # hook.
        sys.exit(1)
