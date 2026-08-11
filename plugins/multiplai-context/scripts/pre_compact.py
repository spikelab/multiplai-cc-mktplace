"""PreCompact hook for multiplai plugin.

Conversation context is about to be compacted, so the full transcript
may not survive. Two jobs:

1. **Synchronous checkpoint** (lib/checkpoint.py): this is the LAST
   chance to capture session state before the window is summarized away.
   Stop-hook band checkpoints can be outrun by a single big turn (field
   log 2026-07-06: one turn jumped ~65K→90K+, compaction fired with only
   the stale 60K checkpoint on disk). Here we run the checkpoint writer
   and WAIT for it, so the SessionStart(source=compact) rebuild always
   injects fresh state. Time-bounded — on timeout/failure compaction
   proceeds with the previous checkpoint (graceful degradation, plus the
   native compaction summary covers the gap).

2. Enqueues a deferred extraction marker (same mechanism as
   session_end.py) pointing at the pre-compaction transcript. The next
   session_start.py drains it through extract_learnings.py, capturing
   learnings/diary before they're lost to compaction.

3. **Writes the pending rebuild marker**, so the
   SessionStart(source=compact) injection fires even for a manual
   ``/compact`` below the handoff threshold (where the Stop hook has not
   written one).

Removed in 0.32.0: a fourth job that steered the native summarizer. This
hook's stdout is appended to the compaction prompt as custom instructions,
and it used that channel to ask for a one-sentence stub instead of a full
summary, on the reasoning that the checkpoint re-injection already carries
the state. It did not work, and could not: the directive had to out-rank the
summarizer's own instructions, so it was phrased as a priority override
telling the model to ignore them — which is indistinguishable, from inside
the summarizer, from a prompt-injection attempt in the text being
summarized. Sessions correctly refused it and produced the full summary
anyway; one flagged it to the user as a live injection against their own
tooling, which is the right call on the evidence available to it and a cost
in its own right. Steering a model by impersonating an authority it is
trained to distrust is not a mechanism that gets more reliable with better
wording. The way to skip a compaction summary is to not compact — hand off
at the threshold instead (see ``checkpoint_hard_stop_tokens``).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state, write_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import hook_run, setup_logging, log_event
from lib import checkpoint as cp
from lib.hook_input import read_hook_input
from lib.runtime import run_supervised, uv_run_argv

logger = setup_logging("pre_compact")

# Poll step while waiting for an already-in-flight band writer to finish.
_INFLIGHT_POLL_S = 2.0

# Ceiling for the synchronous writer wait, tied to the 300s PreCompact
# timeout in hooks/hooks.json. The harness kills this hook at 300s, and the
# ``finally: cp.release_writer(...)`` below must run before that kill — a
# killed hook leaves the writing.marker in place, and for _WRITER_STALE_S
# (~10 minutes) after compaction no Stop hook will spawn a new writer, right
# when context grows fastest. 270 leaves margin for interpreter startup and
# the other passes. cfg.timeout_s (default 600) is sized for the *detached*
# writer nobody waits on; here it must be clamped under the hook budget.
_HOOK_BUDGET_S = 270


def _sync_checkpoint(hook_input: dict, data_dir) -> bool:
    """Write a fresh checkpoint synchronously before compaction.

    Best-effort with a hard time bound (cfg.timeout_s, which also caps the
    writer's own model call). If a detached band writer is already running,
    wait for it instead of double-writing.

    Returns True only when a fresh checkpoint was produced (or confirmed)
    THIS pass; every silent-degradation path returns False. Nothing gates on
    the result any more — it is kept because "did the last-chance checkpoint
    actually land?" is the one question worth asking of this hook's logs.
    """
    cfg = cp.load_config()
    if not cfg.enabled:
        return False
    session_id = hook_input.get("session_id") or ""
    transcript_path = hook_input.get("transcript_path") or ""
    if not session_id or not transcript_path:
        return False
    if cp.is_child_session(transcript_path):
        return False

    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens <= 0:
        return False

    deadline = time.monotonic() + min(cfg.timeout_s, _HOOK_BUDGET_S)

    # A band writer may already be mid-flight — let it finish (its result
    # is at most one turn stale) rather than racing it.
    if cp.writer_inflight(data_dir, session_id):
        logger.info("PreCompact: band writer in flight — waiting for it")
        while cp.writer_inflight(data_dir, session_id):
            if time.monotonic() >= deadline:
                logger.warning("PreCompact: in-flight writer didn't finish in time")
                return False
            time.sleep(_INFLIGHT_POLL_S)
        log_event(
            "checkpoint", "precompact",
            f"pre-compaction checkpoint ready (band writer, {tokens:,} tokens)",
            session_id=session_id, tokens=tokens,
        )
        return True

    script = get_paths().scripts_dir() / "checkpoint_writer.py"
    if not script.exists():
        logger.warning("PreCompact: checkpoint writer script missing at %s", script)
        return False
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": hook_input.get("cwd", ""),
        "tokens": tokens,
        "reason": "precompact",
    })
    cp.claim_writer(data_dir, session_id)
    try:
        # Synchronous on purpose: compaction is imminent and this state is
        # about to be summarized away. The writer releases the marker itself.
        # run_supervised, not subprocess.run: the child is a `uv run` wrapper
        # whose real writer spawns CLI subprocesses, and a plain timeout kills
        # only the wrapper — the work carries on unsupervised while `finally`
        # releases its marker (see lib/runtime.py, "Supervising").
        proc = run_supervised(
            uv_run_argv(script),
            input=payload,
            timeout=max(5.0, deadline - time.monotonic()),
        )
        if proc.returncode != 0:
            logger.warning(
                "PreCompact: checkpoint writer exited rc=%d — compaction proceeds",
                proc.returncode,
            )
            return False
        logger.info("PreCompact: synchronous checkpoint completed at %d tokens", tokens)
        log_event(
            "checkpoint", "precompact",
            f"pre-compaction checkpoint written ({tokens:,} tokens)",
            session_id=session_id, tokens=tokens,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.warning("PreCompact: checkpoint writer timed out — compaction proceeds")
        return False
    except Exception:
        logger.exception("PreCompact: synchronous checkpoint failed (non-fatal)")
        return False
    finally:
        cp.release_writer(data_dir, session_id)


def _mark_pending_rebuild(hook_input: dict, data_dir) -> bool:
    """Write the pending marker so SessionStart(source=compact) re-injects.

    The Stop hook writes this marker at the handoff threshold; a manual
    ``/compact`` can happen well below it, and then only this call stands
    between the user and a compaction with no checkpoint injection after it.
    Gated on a *valid* checkpoint — a marker pointing at an unusable file
    would fail validation in session_start and cost a log line to say so.

    Deliberately not gated on freshness. Freshness used to matter because a
    stale checkpoint would have replaced the summary; now the summary always
    happens and the injection is additive, so a checkpoint that lags by a
    band is strictly better than none.
    """
    cfg = cp.load_config()
    if not cfg.enabled:
        return False
    session_id = hook_input.get("session_id") or ""
    transcript_path = hook_input.get("transcript_path") or ""
    if not session_id or not transcript_path:
        return False
    if cp.is_child_session(transcript_path):
        return False

    try:
        text = cp.checkpoint_file(data_dir, session_id).read_text()
    except OSError:
        return False
    if not cp.validate_checkpoint(text):
        logger.info("PreCompact: checkpoint invalid — no rebuild marker")
        return False

    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens <= 0:
        logger.info("PreCompact: context size unknown (0 tokens) — no rebuild marker")
        return False

    try:
        cp.write_pending_marker(
            data_dir, hook_input.get("cwd", ""), session_id, tokens
        )
    except OSError:
        logger.exception("PreCompact: pending-marker write failed")
        return False

    logger.info("PreCompact: pending rebuild marker written at %d tokens", tokens)
    log_event(
        "checkpoint", "precompact",
        f"rebuild marker written for post-compaction injection ({tokens:,} tokens)",
        session_id=session_id, tokens=tokens,
    )
    return True


def main() -> None:
    hook_input = read_hook_input()

    # Setup inside a guard (M11): a raise here used to escape to __main__
    # (exit 0), silently skipping the checkpoint AND the deferred extraction
    # marker for the compacting session.
    try:
        paths = get_paths()
        data_dir = paths.plugin_data()
    except Exception:
        logger.exception("pre_compact: paths resolution failed; nothing saved")
        return
    try:
        session_state = read_session_state(data_dir) or {}
    except Exception:
        logger.exception("pre_compact: session_state unreadable; continuing without it")
        session_state = {}

    # Prefer the hook input's session_id: the shared session_state.json may
    # hold a different concurrent session's id, which would misattribute this
    # marker (see session_end.py for the same fix).
    session_id = (
        hook_input.get("session_id")
        or session_state.get("session_id")
        or "unknown"
    )
    # Bind the session to the formatter *before* the ENTRY line, not partway
    # through the body. Rebinding it later stamped ENTRY with `--------` and
    # EXIT with the real id, so every run looked like an unmatched ENTRY plus a
    # stray EXIT — two records, one of them a phantom kill.
    setup_logging("pre_compact", session_id=session_id)
    with hook_run("pre_compact", logger, session_id=session_id) as run:
        _compact_pass(hook_input, session_state, data_dir, session_id, run)


def _compact_pass(
    hook_input: dict, session_state: dict, data_dir, session_id: str, run
) -> None:
    """Checkpoint, arm the rebuild, and queue extraction before compaction."""
    # Compaction summarizes the conversation, so any context the
    # UserPromptSubmit hook injected this session may no longer be
    # present verbatim. Clear the re-recommendation cooldown map so every
    # file becomes eligible again — otherwise a file injected just before
    # compaction would stay suppressed for X turns despite being gone.
    if session_state.get("recently_injected"):
        session_state["recently_injected"] = {}
        if write_session_state(data_dir, session_state):
            logger.info("PreCompact: cleared re-recommendation cooldown map")

    # Fresh checkpoint BEFORE compaction — this is the state the
    # SessionStart(source=compact) rebuild will inject. Never fatal. This
    # stage makes a model call, which is why this hook's ceiling is 300s and
    # every other hook's is under 60.
    with run.stage("checkpoint"):
        try:
            _sync_checkpoint(hook_input, data_dir)
        except Exception:
            logger.exception("PreCompact: checkpoint pass failed (non-fatal)")

    # Arm the post-compaction injection. Nothing is printed to stdout: this
    # hook's stdout reaches the summarizer as custom instructions, and this
    # plugin no longer has anything to say to it (see the module docstring).
    with run.stage("rebuild_marker"):
        try:
            _mark_pending_rebuild(hook_input, data_dir)
        except Exception:
            logger.exception("PreCompact: rebuild-marker pass failed (non-fatal)")

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        logger.info("PreCompact: no transcript_path in payload — nothing to defer")
        run.note(outcome="no_transcript")
        return
    # A compacting subagent / nested hook session must not queue an extraction
    # of its transcript into the user's diary (M7). The checkpoint and
    # rebuild-marker passes above carry the same guard internally.
    if cp.is_child_session(transcript_path):
        logger.info("PreCompact: child session — no deferred extraction marker")
        run.note(outcome="child_session")
        return

    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": hook_input.get("cwd", session_state.get("cwd", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": "pre_compact",
    }

    pending_dir = data_dir / "pending_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)
    # Distinct name so a PreCompact marker never overwrites the SessionEnd
    # marker for the same session (and vice versa).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    marker_path = pending_dir / f"precompact-{session_id}-{stamp}.json"
    tmp = marker_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(marker, indent=2))
        os.replace(str(tmp), str(marker_path))
        logger.info("PreCompact: wrote deferred extraction marker %s", marker_path)
        log_event(
            "session", "precompact",
            "context compacting — queued deferred extraction to preserve learnings",
            session_id=session_id,
        )
        run.note(outcome="queued")
    except OSError:
        logger.exception("PreCompact: failed to write deferred extraction marker")
        run.note(outcome="marker_failed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session — log and exit cleanly.
        try:
            logger.exception("pre_compact hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
