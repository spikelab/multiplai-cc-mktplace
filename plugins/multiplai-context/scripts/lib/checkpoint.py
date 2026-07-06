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
     ``refresh_tokens`` so it never goes stale in marathon sessions.
  3. **Handoff** — at/above the handoff threshold (default 200K) a pending
     marker is written for the session's project. The user is advised (via
     Stop-hook systemMessage and a per-prompt nudge) to ``/clear``.
  4. **Rebuild** — the next SessionStart in the same project consumes the
     pending marker (TTL-gated) and injects the checkpoint as
     additionalContext, so the fresh session resumes where the old one left
     off.

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
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

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
    _atomic_write(_state_file(data_dir, session_id), json.dumps(state, indent=2))


def _atomic_write(path: Path, content: str) -> None:
    """Write via tempfile + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_checkpoint_file(data_dir: Path, session_id: str, content: str) -> Path:
    """Atomically write ``checkpoint.md`` for *session_id*; returns its path."""
    path = checkpoint_file(data_dir, session_id)
    _atomic_write(path, content)
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
    _atomic_write(marker, json.dumps(payload, indent=2))
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
# Auto-compact steering (the fully-automatic rebuild path)
# ---------------------------------------------------------------------------

def autocompact_trigger_tokens() -> int | None:
    """Expected auto-compaction trigger, when the host steers it via env.

    Claude Code exposes ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (capacity the
    monitor assumes) and ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` (1-100, default
    ~90). When the runtime sets these so compaction fires near our handoff
    threshold, the rebuild is fully automatic: native auto-compact resets
    the window mid-session, then SessionStart(source="compact") re-injects
    the checkpoint. Returns the estimated trigger in tokens, or None when
    the window var is unset (auto mode not configured).

    Hooks inherit the Claude Code process env, so this is readable here.
    """
    raw_window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "").strip()
    if not raw_window:
        return None
    try:
        window = int(raw_window)
    except ValueError:
        return None
    raw_pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "").strip()
    try:
        pct = int(raw_pct) if raw_pct else 90
    except ValueError:
        pct = 90
    if window <= 0 or not (0 < pct <= 100):
        return None
    return int(window * pct / 100)


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
continue the work.
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
