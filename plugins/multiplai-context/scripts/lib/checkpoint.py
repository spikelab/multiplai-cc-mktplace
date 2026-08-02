"""Checkpoint & context-rebuild core (MiMo-style, no LLM calls).

Long interactive sessions degrade as the context window fills. This module
implements the plumbing for the checkpoint lifecycle:

  1. **Measure** — read the current context size from the session transcript
     (last main-chain assistant ``message.usage``: input + cache-read +
     cache-creation tokens).
  2. **Checkpoint** — when a token band is crossed (default 100K / 200K), the
     Stop hook spawns a detached ``checkpoint_writer.py`` that distills the
     transcript into a structured 11-field ``checkpoint.md``. Above the
     handoff threshold the checkpoint keeps refreshing every
     ``refresh_tokens`` so it never goes stale in marathon sessions. A third,
     age-based trigger covers the opposite shape: a small session left open
     for days never crosses a band, so ``staleness_trigger`` fires on
     wall-clock age instead.
  3. **Handoff** — at/above the handoff threshold (default 200K) a pending
     marker is written for the session's project. The user is advised (via
     Stop-hook systemMessage and a per-prompt nudge) to ``/clear``.
  4. **Rebuild** — the next SessionStart in the same project consumes the
     pending marker (TTL-gated) and injects the checkpoint as
     additionalContext, so the fresh session resumes where the old one left
     off.
  5. **Retire** — once the session's diary entry is written the checkpoint is
     superseded, and ``retire_checkpoint`` deletes the directory. Live state
     and the permanent record are different artifacts with different
     lifetimes; without step 5 the first one never ends.

Interactive Claude Code cannot be force-restarted from a hook, so the
rebuild is advisory-then-automatic: advice to /clear, automatic re-seeding
after it. Hooks here must never block a Stop (goal loops depend on it) and
must skip child sessions (subagents, nested hook sessions).

All state lives under ``<data_dir>/checkpoints/``:

    checkpoints/<session_id>/checkpoint.md   the latest structured checkpoint
    checkpoints/<session_id>/state.json      band/offset bookkeeping
    checkpoints/<session_id>/writing.marker  single-flight writer liveness
    checkpoints/pending/<project>.json       handoff marker for rebuild
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from lib.fsio import atomic_write

logger = logging.getLogger("multiplai.checkpoint")

# How many bytes of transcript tail to scan for the latest usage record.
# Transcripts grow to tens of MB; the last main-chain assistant record is
# always within the final few hundred KB.
_TAIL_BYTES = 512_000

# A writing.marker older than this is considered orphaned (writer crashed).
_WRITER_STALE_S = 600

_DEFAULT_BANDS = (100_000, 200_000)
_DEFAULT_HANDOFF = 200_000
_DEFAULT_REFRESH = 25_000
_DEFAULT_TTL_HOURS = 6.0
_DEFAULT_TIMEOUT_S = 240

# Age-based checkpointing (the `stale` trigger). Deliberately NOT ttl_hours:
# that one means pending-marker expiry and is consumed by
# ``consume_pending_marker``; conflating the two meanings would silently break
# rebuild expiry.
#
# Defaults are chosen for write volume, not responsiveness. A 4-hour session
# writes twice — once when it passes the minimum age with no checkpoint at all,
# once three hours later — against 1–2 today. Extraction and interactive work
# share one rate limit, and the July 6–7 failure cluster was a single 429 from
# a large backfill, so the cadence stays well under one write per hour per
# session.
_DEFAULT_STALE_HOURS = 3.0
_DEFAULT_MIN_SESSION_MINUTES = 30

# The 11 checkpoint fields (MiMo Code spec). The writer prompt emits these
# as H2 sections; validation requires a majority of them to be present.
CHECKPOINT_SECTIONS = (
    "Current intent",
    "Next action",
    "Working constraints",
    "Task tree",
    "Current work",
    "Involved files",
    "Cross-task discoveries",
    "Errors and fixes",
    "Runtime state",
    "Design decisions",
    "Notes",
)
_MIN_VALID_SECTIONS = 6


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CheckpointConfig:
    """Thresholds for the checkpoint lifecycle (absolute tokens)."""

    bands: tuple[int, ...] = _DEFAULT_BANDS
    handoff_tokens: int = _DEFAULT_HANDOFF
    refresh_tokens: int = _DEFAULT_REFRESH
    ttl_hours: float = _DEFAULT_TTL_HOURS
    timeout_s: int = _DEFAULT_TIMEOUT_S
    model: str | None = None
    enabled: bool = True
    # Age-based trigger. ``stale_hours = 0`` disables it entirely, leaving the
    # token-band behaviour exactly as it was.
    stale_hours: float = _DEFAULT_STALE_HOURS
    min_session_minutes: int = _DEFAULT_MIN_SESSION_MINUTES


def _opt(name: str) -> str:
    """Read a ``CLAUDE_PLUGIN_OPTION_<name>`` env var ('' when unset)."""
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}", "").strip()


def _opt_int(name: str, default: int) -> int:
    raw = _opt(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Malformed %s=%r; using default %d", name, raw, default)
        return default


def _opt_float(name: str, default: float) -> float:
    raw = _opt(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Malformed %s=%r; using default %s", name, raw, default)
        return default


def load_config() -> CheckpointConfig:
    """Build config from ``CLAUDE_PLUGIN_OPTION_checkpoint_*`` env vars.

    Malformed values fall back to defaults with a warning — config problems
    must never crash a hook. Bands are normalized to sorted-unique-positive;
    the handoff threshold is clamped to at least the highest band so a
    partial override can't produce a handoff below the last checkpoint.
    """
    enabled = _opt("checkpoint_enabled").lower() not in ("false", "0", "no", "off")

    raw_bands = _opt("checkpoint_tokens")
    bands: tuple[int, ...] = _DEFAULT_BANDS
    if raw_bands:
        try:
            parsed = sorted({int(p.strip()) for p in raw_bands.split(",") if p.strip()})
            if parsed and all(b > 0 for b in parsed):
                bands = tuple(parsed)
            else:
                raise ValueError(raw_bands)
        except ValueError:
            logger.warning(
                "Malformed checkpoint_tokens=%r; using defaults %s",
                raw_bands, _DEFAULT_BANDS,
            )

    handoff = _opt_int("checkpoint_handoff_tokens", bands[-1])
    if handoff < bands[-1]:
        logger.warning(
            "checkpoint_handoff_tokens=%d below last band %d; clamping",
            handoff, bands[-1],
        )
        handoff = bands[-1]

    return CheckpointConfig(
        bands=bands,
        handoff_tokens=handoff,
        refresh_tokens=max(1, _opt_int("checkpoint_refresh_tokens", _DEFAULT_REFRESH)),
        ttl_hours=_opt_float("checkpoint_ttl_hours", _DEFAULT_TTL_HOURS),
        timeout_s=_opt_int("checkpoint_timeout_s", _DEFAULT_TIMEOUT_S),
        model=_opt("checkpoint_model") or None,
        enabled=enabled,
        stale_hours=max(0.0, _opt_float("checkpoint_stale_hours", _DEFAULT_STALE_HOURS)),
        min_session_minutes=max(
            0, _opt_int("checkpoint_min_session_minutes", _DEFAULT_MIN_SESSION_MINUTES)
        ),
    )


# ---------------------------------------------------------------------------
# Paths & state
# ---------------------------------------------------------------------------

def checkpoints_root(data_dir: Path) -> Path:
    return data_dir / "checkpoints"


def session_dir(data_dir: Path, session_id: str) -> Path:
    return checkpoints_root(data_dir) / session_id


def checkpoint_file(data_dir: Path, session_id: str) -> Path:
    return session_dir(data_dir, session_id) / "checkpoint.md"


def _state_file(data_dir: Path, session_id: str) -> Path:
    return session_dir(data_dir, session_id) / "state.json"


def load_state(data_dir: Path, session_id: str) -> dict:
    """Read per-session checkpoint state ({} when absent/corrupt)."""
    try:
        state = json.loads(_state_file(data_dir, session_id).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_state(data_dir: Path, session_id: str, state: dict) -> None:
    """Atomically persist per-session checkpoint state."""
    sdir = session_dir(data_dir, session_id)
    sdir.mkdir(parents=True, exist_ok=True)
    atomic_write(_state_file(data_dir, session_id), json.dumps(state, indent=2))


def write_checkpoint_file(data_dir: Path, session_id: str, content: str) -> Path:
    """Atomically write ``checkpoint.md`` for *session_id*; returns its path."""
    path = checkpoint_file(data_dir, session_id)
    atomic_write(path, content)
    return path


# ---------------------------------------------------------------------------
# Context-size measurement
# ---------------------------------------------------------------------------

def read_context_tokens(
    transcript_path: str | Path, after_ts: str | None = None
) -> int:
    """Return the current context size (tokens) from a session transcript.

    Scans the transcript tail (last ``_TAIL_BYTES``) backwards for the most
    recent main-chain assistant record carrying ``message.usage`` and returns
    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
    — the real context footprint. ``input_tokens`` alone badly undercounts
    under prompt caching, which is the normal steady state.

    ``after_ts`` (ISO timestamp): only records strictly newer count. Set to
    the last rebuild time so that right after a compaction — when the tail
    still ends in PRE-compact usage records — the stale (huge) numbers are
    ignored instead of instantly re-triggering bands/handoff. Verified in
    the field: without this, every compaction caused one spurious
    checkpoint cycle five seconds after the rebuild.

    Returns 0 when the transcript is missing, unreadable, or carries no
    (fresh enough) usage records — callers treat 0 as "no action".
    Sidechain (subagent) records are skipped — their usage describes a
    different context window.
    """
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
                f.readline()  # discard the partial first line
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0

    cutoff: datetime | None = None
    if after_ts:
        try:
            cutoff = datetime.fromisoformat(str(after_ts))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            cutoff = None

    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        if record.get("isSidechain"):
            continue
        usage = (record.get("message") or {}).get("usage") or {}
        if not isinstance(usage, dict) or "input_tokens" not in usage:
            continue
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(
                    str(record.get("timestamp")).replace("Z", "+00:00")
                )
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue  # freshness unverifiable — don't act on it
            if ts <= cutoff:
                # Reached pre-rebuild records; nothing newer carries usage.
                return 0
        return (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
    return 0


def is_child_session(transcript_path: str | Path | None = None) -> bool:
    """True for subagent / nested hook sessions — they must never checkpoint.

    Child SDK sessions export ``_HOOK_CHILD_SESSION=1`` (multiplai-core
    run_agent isolation bundle); subagent transcripts live under
    ``…/subagents/`` and nested hook sessions under ``…/hook-sessions/``.
    """
    if os.environ.get("_HOOK_CHILD_SESSION"):
        return True
    if transcript_path:
        parts = str(transcript_path)
        if "/subagents/" in parts or "/hook-sessions/" in parts:
            return True
    return False


# ---------------------------------------------------------------------------
# Trigger decision
# ---------------------------------------------------------------------------

def band_index(tokens: int, bands: tuple[int, ...]) -> int:
    """Highest band index (1-based) at/below *tokens*; 0 when below all."""
    idx = 0
    for i, threshold in enumerate(bands, start=1):
        if tokens >= threshold:
            idx = i
    return idx


def checkpoint_trigger(tokens: int, state: dict, cfg: CheckpointConfig) -> str | None:
    """Decide whether a checkpoint write is due. Returns a reason or None.

    Two triggers:
      * ``band`` — *tokens* crossed a band the session hasn't checkpointed
        at yet (e.g. first time past 100K).
      * ``refresh`` — the session is at/above the handoff threshold and has
        grown ``refresh_tokens`` past the last checkpoint, so marathon
        (goal-loop) sessions keep a current checkpoint even though nobody
        is around to /clear.
    """
    if not cfg.enabled:
        return None
    idx = band_index(tokens, cfg.bands)
    if idx > int(state.get("last_band_idx") or 0):
        return "band"
    if tokens >= cfg.handoff_tokens:
        last_tokens = int(state.get("last_checkpoint_tokens") or 0)
        if tokens - last_tokens >= cfg.refresh_tokens:
            return "refresh"
    return None


def _session_started_at(data_dir: Path, session_id: str) -> datetime | None:
    """When this session began, per the session registry.

    The registry is the only store that knows: ``state.json`` here is created
    by the *first checkpoint write*, so for the sessions this matters for — the
    ones that never checkpointed at all — it does not exist. The registry entry
    does, because ``record_event`` runs on SessionStart and again on every
    Stop, creating it if hooks were installed mid-session.

    Returns None when it cannot be read, and the caller then declines to fire:
    a session of unknown age is not evidence of a stale one, and today's
    behaviour (band triggers only) is the safe default.
    """
    try:
        from lib.session_registry import registry_dir

        raw = (registry_dir(data_dir) / f"{session_id}.json").read_text(encoding="utf-8")
        started = json.loads(raw).get("started_at")
        ts = datetime.fromisoformat(str(started))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def staleness_trigger(
    data_dir: Path, session_id: str, state: dict, cfg: CheckpointConfig
) -> str | None:
    """Decide whether a checkpoint is due on *age*. Returns ``"stale"`` or None.

    The gap this closes: the existing triggers are token-based, so a tab that
    sat at 40K tokens for three days has no checkpoint at all — and that is
    exactly the tab whose state you have lost track of. ``AGENTS.md`` renders
    intent, next action and files-in-hand from the checkpoint, so a dormant
    session was the least visible one in the fleet view precisely when it
    needed to be the most.

    Fires when the session is at least ``min_session_minutes`` old AND either
    it has never checkpointed, or its last checkpoint is ``stale_hours`` old.
    The age gate is what keeps short sessions out; without it every session
    that ever completes a turn would write one.

    **This is not the per-turn diary that was rejected**, and the difference is
    structural rather than a matter of degree: the check below is two file
    reads and a subtraction, the write it may trigger is an overwrite of one
    file rather than a permanent append, and ``checkpoint_writer.py`` distills
    only the transcript segment newer than ``last_checkpoint_ts`` — so writing
    more often makes each write smaller, not the total larger.

    A dormant tab fires no hooks at all, so nothing here can run *during* the
    three quiet days. That is fine: the last ``Stop`` of a session is the
    moment the work stopped, so the checkpoint it leaves is at most
    ``stale_hours`` behind where the session actually ended up.
    """
    if not cfg.enabled or cfg.stale_hours <= 0:
        return None

    started = _session_started_at(data_dir, session_id)
    if started is None:
        return None
    now = datetime.now(timezone.utc)
    if (now - started).total_seconds() < cfg.min_session_minutes * 60:
        return None

    last_ts = state.get("last_checkpoint_ts")
    if not last_ts:
        # Never checkpointed, and old enough to be worth recording. This is
        # the dormant-tab case the trigger exists for.
        return "stale"
    try:
        last = datetime.fromisoformat(str(last_ts))
    except (TypeError, ValueError):
        # A corrupt timestamp means we cannot tell how old the checkpoint is;
        # treat it as stale rather than never refreshing this session again.
        return "stale"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if (now - last).total_seconds() >= cfg.stale_hours * 3600:
        return "stale"
    return None


def writer_inflight(data_dir: Path, session_id: str) -> bool:
    """True when a fresh ``writing.marker`` exists (single-flight guard)."""
    marker = session_dir(data_dir, session_id) / "writing.marker"
    try:
        return (time.time() - marker.stat().st_mtime) < _WRITER_STALE_S
    except OSError:
        return False


def claim_writer(data_dir: Path, session_id: str) -> Path:
    """Create/refresh the writing marker; returns its path."""
    sdir = session_dir(data_dir, session_id)
    sdir.mkdir(parents=True, exist_ok=True)
    marker = sdir / "writing.marker"
    marker.write_text(datetime.now(timezone.utc).isoformat())
    return marker


def release_writer(data_dir: Path, session_id: str) -> None:
    (session_dir(data_dir, session_id) / "writing.marker").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Pending-handoff markers (rebuild linkage)
# ---------------------------------------------------------------------------

def _pending_dir(data_dir: Path) -> Path:
    return checkpoints_root(data_dir) / "pending"


def _project_key(cwd: str) -> str:
    """Stable per-project marker key derived from cwd.

    Uses the shared project resolver when available so the key survives cwd
    drift within one project; falls back to a sanitized basename.
    """
    project = ""
    try:
        from lib.project_identity import resolve_project  # type: ignore

        project = resolve_project(cwd) or ""
    except Exception:
        project = ""
    if not project:
        project = Path(cwd).name if cwd else "unknown"
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in project) or "unknown"


def write_pending_marker(
    data_dir: Path, cwd: str, session_id: str, tokens: int
) -> Path:
    """Record that *session_id* is handoff-ready; keyed by project."""
    pdir = _pending_dir(data_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    marker = pdir / f"{_project_key(cwd)}.json"
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "tokens": tokens,
        "checkpoint_path": str(checkpoint_file(data_dir, session_id)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(marker, json.dumps(payload, indent=2))
    return marker


def consume_pending_marker(
    data_dir: Path,
    cwd: str,
    new_session_id: str,
    cfg: CheckpointConfig,
    *,
    allow_same_session: bool = False,
) -> dict | None:
    """Claim-and-return the pending marker for *cwd*'s project, if fresh.

    The marker is removed on claim (atomic rename — two racing SessionStarts
    can't both inject). Expired (> ``ttl_hours``) markers are discarded.
    Self-referential markers (same session id) are normally put back — a
    *resumed* session is not a rebuild — EXCEPT when ``allow_same_session``
    is set: after auto-compaction (SessionStart source="compact") the session
    id is unchanged but the context genuinely restarted, and injecting the
    checkpoint there is exactly the automatic rebuild.
    Returns the marker payload or None.
    """
    marker = _pending_dir(data_dir) / f"{_project_key(cwd)}.json"
    if not marker.exists():
        return None

    claimed = marker.with_suffix(f".claimed-{new_session_id[:8]}")
    try:
        os.replace(str(marker), str(claimed))
    except OSError:
        return None  # another session claimed it first

    try:
        payload = json.loads(claimed.read_text())
        if not isinstance(payload, dict):
            return None
        if payload.get("session_id") == new_session_id and not allow_same_session:
            # Same session resuming — put the marker back for a real rebuild.
            os.replace(str(claimed), str(marker))
            return None
        created = datetime.fromisoformat(str(payload.get("created_at")))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_h > cfg.ttl_hours:
            logger.info("Pending checkpoint marker expired (%.1fh old); discarding", age_h)
            return None
        return payload
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    finally:
        claimed.unlink(missing_ok=True)


def reset_session_counters(data_dir: Path, session_id: str) -> None:
    """Reset band/token counters after a rebuild so the NEW physical window
    checkpoints again from scratch.

    Keeps ``last_checkpoint_ts`` (the writer stays incremental — old turns
    are already merged into checkpoint.md) and the checkpoint file itself.
    Also clears nudge cooldowns.
    """
    state = load_state(data_dir, session_id)
    state["last_band_idx"] = 0
    state["last_checkpoint_tokens"] = 0
    # Stale-usage guard: token reads ignore transcript records older than
    # this, so the pre-compact usage still sitting at the transcript tail
    # can't re-trigger bands right after the rebuild.
    state["rebuild_ts"] = datetime.now(timezone.utc).isoformat()
    try:
        save_state(data_dir, session_id, state)
    except OSError:
        pass
    sdir = session_dir(data_dir, session_id)
    for name in ("nudge.json", "claude_nudge.json"):
        (sdir / name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Retirement — the checkpoint's end of life
# ---------------------------------------------------------------------------
#
# A checkpoint is *live state*: roughly where a session is right now, so the
# next window can resume it. Once the diary entry for that session exists, the
# permanent narrative record has superseded it and the directory is dead weight
# — 182 of them on disk at plan time (2026-07-31), one per session ever run,
# none ever collected.
#
# The two guards below are this module's, because they are facts about the
# checkpoint layout. The caller owns the pipeline-level ones (diary written?
# session parked?) — see ``extract_learnings.py``.

def pending_marker_owner(data_dir: Path, session_id: str) -> Path | None:
    """The pending handoff marker still pointing at *session_id*, if any.

    Markers are keyed by project, not session, so this scans them. Small by
    construction: one file per project, and each is claimed-and-removed by the
    next SessionStart there.

    Why it gates retirement: the walk-away case is exactly the one that
    collides. A session crosses the handoff threshold, gets the ``/clear``
    advice, and the tab is closed instead — leaving an unconsumed marker.
    Extraction then runs (host-side, minutes later) and would delete the very
    ``checkpoint.md`` the marker exists to rebuild from, well inside its
    ``ttl_hours``. ``session_start.py`` degrades safely on the missing file,
    so the damage is silent: no rebuild, no error, no clue.
    """
    pdir = _pending_dir(data_dir)
    try:
        markers = sorted(pdir.glob("*.json"))
    except OSError:
        return None
    for marker in markers:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("session_id") == session_id:
            return marker
    return None


def retire_checkpoint(data_dir: Path, session_id: str) -> tuple[bool, str]:
    """Delete ``checkpoints/<session_id>/`` now that the diary supersedes it.

    Returns ``(removed, reason_kept)``, which distinguishes the three outcomes
    the caller logs differently:

    * ``(True, "")`` — the directory was there and is gone.
    * ``(False, "")`` — there was nothing to collect. The common case: most
      sessions never cross a token band, so they never had a checkpoint.
    * ``(False, "<reason>")`` — deliberately kept. A checkpoint that survives
      always says why.

    Never raises. Failing to collect a checkpoint costs disk; failing an
    extraction costs the session's diary entry, and this runs inside that
    pipeline.
    """
    try:
        if not session_id or "/" in session_id or session_id in (".", ".."):
            return False, "invalid session id"
        sdir = session_dir(data_dir, session_id)
        if not sdir.is_dir():
            return False, ""
        if writer_inflight(data_dir, session_id):
            # A detached checkpoint_writer.py is mid-write. Deleting under it
            # would race its atomic rename, and the writer would recreate a
            # half-populated directory on the way out anyway.
            return False, "writer in flight"
        marker = pending_marker_owner(data_dir, session_id)
        if marker is not None:
            return False, f"pending rebuild marker {marker.name}"

        # TOCTOU, accepted: between the two guards above and the rmtree below
        # a concurrent Stop hook could still claim the writer or write a
        # pending marker for this session. That window only matters for a
        # LIVE session, and the caller already refuses to retire live
        # sessions (pre_compact-triggered extractions are skipped upstream),
        # so reaching here means the session has ended and no new Stop hook
        # is coming. If the race fires anyway, both losers self-heal: a
        # writer that loses its directory mid-write recreates it via its
        # atomic rename on the way out (a later extraction re-retires it),
        # and a pending marker pointing at a deleted checkpoint.md is caught
        # by the marker's own staleness/`ttl_hours` handling and the rebuild
        # path's degrade-on-missing-file behaviour at the next SessionStart.
        shutil.rmtree(sdir)
        logger.info("Retired checkpoint for session %s", session_id)
        return True, ""
    except Exception as e:
        # Broad on purpose: "never raises" is the contract, and this runs
        # inside the extraction pipeline where an escape would cost the
        # session's diary entry. OSError covers the filesystem; anything
        # else (e.g. ValueError from a malformed path) is the same answer —
        # keep the directory, report why.
        logger.warning("Could not retire checkpoint for %s: %s", session_id, e)
        return False, f"removal failed: {e}"


# ---------------------------------------------------------------------------
# Auto-compact steering (the fully-automatic rebuild path)
# ---------------------------------------------------------------------------

# Native auto-compact constants, mirrored from the Claude Code binary
# (v2.1.201, functions E4/Sza/Lar/Yie; re-verify on CLI major bumps):
#   window clamp [1e5, 1e6]; env-configured windows BELOW 200K hard-disable
#   soft auto-compact (Ire gate); usable = window − min(maxOutput, 20000);
#   trigger = min(usable × pct/100, usable − 13000).
_NATIVE_WINDOW_MIN = 100_000
_NATIVE_WINDOW_MAX = 1_000_000
_NATIVE_FIRE_GATE = 200_000
_NATIVE_OUTPUT_RESERVE_CAP = 20_000
_NATIVE_THRESHOLD_MARGIN = 13_000


def autocompact_trigger_tokens() -> int | None:
    """Expected native auto-compaction trigger, when steered via env.

    Mirrors the binary's actual formula (see constants above) so the
    "compaction overdue" warning doesn't cry wolf. Two behaviors verified
    in the field (2026-07-06):

    * A configured window below the 200K fire gate does NOT lower the
      trigger — it silently DISABLES soft auto-compact. We return None
      (manual mode: the /clear nudges take over), which is the truthful
      state.
    * The trigger applies pct to the USABLE window (window minus the
      output reserve), capped at usable − 13000.

    Returns the estimated trigger in tokens, or None when auto mode isn't
    effectively configured. Hooks inherit the Claude Code process env.
    """
    raw_window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "").strip()
    if not raw_window:
        return None
    try:
        window = int(raw_window)
    except ValueError:
        return None
    if window <= 0:
        return None
    window = max(_NATIVE_WINDOW_MIN, min(_NATIVE_WINDOW_MAX, window))
    if window < _NATIVE_FIRE_GATE:
        return None  # native soft auto-compact is hard-disabled below 200K

    try:
        max_out = int(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "") or 0)
    except ValueError:
        max_out = 0
    reserve = min(max_out, _NATIVE_OUTPUT_RESERVE_CAP) if max_out > 0 else _NATIVE_OUTPUT_RESERVE_CAP
    usable = window - reserve

    raw_pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "").strip()
    try:
        pct = float(raw_pct) if raw_pct else 0.0
    except ValueError:
        pct = 0.0
    default_trigger = usable - _NATIVE_THRESHOLD_MARGIN
    if 0 < pct <= 100:
        return min(int(usable * pct / 100), default_trigger)
    return default_trigger


# ---------------------------------------------------------------------------
# Checkpoint validation & rebuild seed
# ---------------------------------------------------------------------------

def validate_checkpoint(text: str) -> bool:
    """A checkpoint is usable when most of the 11 sections are present."""
    if not text or not text.strip():
        return False
    lowered = text.lower()
    found = sum(1 for s in CHECKPOINT_SECTIONS if f"## {s.lower()}" in lowered)
    return found >= _MIN_VALID_SECTIONS


REBUILD_PREAMBLE = """\
--- CONTEXT REBUILD ---
This session continues work handed off from a previous session whose context
window filled up ({tokens:,} tokens). The checkpoint below captures its full
working state. Treat it as your own prior work — do not re-do completed
items in the task tree.
"""

REBUILD_SUFFIX = """\
Resume from the 'Next action' section of the checkpoint. Re-read any files
listed under 'Involved files' before modifying them. Confirm your
understanding of the current state to the user in one short sentence, then
continue the work. If the user's next message is clearly UNRELATED to this
checkpoint, set the checkpoint aside and follow the user — it is context,
not a directive.
--- END CONTEXT REBUILD ---"""


def build_rebuild_context(checkpoint_text: str, tokens: int) -> str:
    """Assemble the SessionStart additionalContext rebuild seed."""
    return (
        REBUILD_PREAMBLE.format(tokens=tokens)
        + "\n"
        + checkpoint_text.strip()
        + "\n\n"
        + REBUILD_SUFFIX
    )
