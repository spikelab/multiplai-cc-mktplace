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
     Stop-hook systemMessage and a per-prompt nudge) to ``/clear``. Advice
     only, unless ``hard_stop_tokens`` is set: above that the nudge stops
     accepting new prompts until the handoff happens.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from multiplai_core.plugin_options import option, option_float, option_int

from lib.fsio import atomic_write

logger = logging.getLogger("multiplai.checkpoint")

# How many bytes of transcript tail to scan for the latest usage record.
# Transcripts grow to tens of MB; the last main-chain assistant record is
# always within the final few hundred KB.
_TAIL_BYTES = 512_000

# When the tail window holds no complete usage record at all (one oversized
# tool-result line can swallow it), retry with a doubled window before
# answering 0: 512KB → 1MB → 2MB, then stop. A spurious 0 makes
# session_stop skip the whole checkpoint pass for that turn (P8).
_MAX_WINDOW_DOUBLINGS = 2

# A writing.marker older than this is considered orphaned (writer crashed).
_WRITER_STALE_S = 600

_DEFAULT_BANDS = (100_000, 200_000)
_DEFAULT_HANDOFF = 200_000
_DEFAULT_REFRESH = 25_000
_DEFAULT_TTL_HOURS = 6.0
# The writer is detached (start_new_session=True) and nobody waits on it, so a
# generous ceiling costs nothing and a tight one costs the whole checkpoint.
# 240s was measured sitting exactly on the boundary for a backlogged session
# on 2026-08-08 — attempt 1 timed out at 240s, attempt 2 finished at 480s —
# which turned a slow write into a coin flip that lost eight times running.
# With the 30-minute cadence below this should never bind; it is the net, not
# the fix.
_DEFAULT_TIMEOUT_S = 600

# Hard stop. 0 (the default) means "advisory only" — the handoff nudge asks
# for a /clear and nothing enforces it. Set it and checkpoint_nudge.py stops
# *accepting new prompts* above the threshold instead of merely mentioning it.
#
# Off by default on purpose. The nudge is safe everywhere; a block is a
# behaviour a user has to choose, because getting it wrong means a session
# that refuses to talk. It exists for the setup where auto-compaction is
# disabled outright (DISABLE_AUTO_COMPACT / autoCompactEnabled:false): there
# the only thing between the handoff threshold and the model's real context
# ceiling is advice, and advice does not stop a session drifting into the
# degraded zone the checkpoint system exists to avoid.
_DEFAULT_HARD_STOP = 0

# Age-based checkpointing (the `stale` trigger). Deliberately NOT ttl_hours:
# that one means pending-marker expiry and is consumed by
# ``consume_pending_marker``; conflating the two meanings would silently break
# rebuild expiry.
#
# Defaults are chosen for RECOVERABILITY, not for write volume. That is an
# inversion of the original 3.0-hour choice, and the sentence it replaces
# ("the cadence stays well under one write per hour per session") is no longer
# true — 0.5 hours is deliberately about two writes per hour per session.
#
# What changed the trade-off, measured on 2026-08-08: at 3.0 hours the fleet
# averaged roughly one write per session (28 writes across 21 sessions on
# Aug 7), and the one session that fell behind could never catch up. The
# writer distills only the segment newer than ``last_checkpoint_ts``, and that
# field advanced only on success — so each failure left a LARGER segment for
# the next attempt: 174,154 characters against 23,287 for a healthy write, a
# model call sitting exactly on its timeout, and eight consecutive losses over
# 18 hours. Writing every 30 minutes keeps each segment to 30 minutes of work,
# so the backlog that made the call unfinishable never forms. Prevention, not
# error handling.
#
# A 4-hour session now writes 8 small delta-merges rather than 2 large ones
# (``TestWriteVolume`` pins the arithmetic). Each write is smaller in
# proportion, because the segment is the dominant input; the total is roughly
# flat rather than 4x. Measured, not assumed — see the PR that landed this.
#
# That 8 is a STALENESS figure and therefore a floor, not a ceiling. The
# ``refresh`` trigger below is no longer gated on ``handoff_tokens``, so a
# session growing faster than ``refresh_tokens`` (25K) per ``stale_hours``
# writes on the token cadence instead, and writes more often. The two do not
# add up — a write resets ``last_checkpoint_ts`` and ``last_checkpoint_tokens``
# together, so the rate is max(staleness, growth/refresh_tokens), not the sum —
# but a goal loop burning 25K tokens every ten minutes writes ~6x/hour rather
# than 2. ``TestWriteVolume.test_token_growth_can_outpace_the_staleness_floor``
# pins that this is the shape, so the cost-per-session figure quoted for the
# 0.5h cadence is the token-light case.
#
# Two accepted burst shapes, deliberately without a global cap or jitter:
#
# - Thundering herd: after any break longer than stale_hours every open tab is
#   simultaneously stale, so the first Stop in each resumed tab spawns a
#   detached writer — N tabs resuming together means N concurrent writers
#   sharing the interactive rate limit. At 0.5 hours the qualifying break is a
#   lunch break rather than an overnight one, so this shape is now routine
#   instead of daily. It is still bounded the same way — one write per tab per
#   stale_hours, single-flight (`writer_inflight`) per session — so the
#   magnitude stays single-digit concurrent calls at any realistic tab count
#   (the July 6–7 429 came from a hundreds-of-calls backfill, not this shape).
#
# - Writer-failure retries: a failed writer releases its marker without
#   updating state (checkpoint_writer.py), so the next Stop refires. Same
#   retry pattern as before, now reached more often. The compounding is gone
#   though: ``checkpoint_writer.py``'s degraded fallback writes the previous
#   checkpoint plus the raw unsummarised slice and advances the bookmark, so a
#   retry storm no longer hands each attempt a bigger segment than the last.
_DEFAULT_STALE_HOURS = 0.5
_DEFAULT_MIN_SESSION_MINUTES = 30

# Collection floor for the checkpoint store. A session directory whose last
# sign of life — newest file mtime, or its registry ``last_event`` — is older
# than this is collected by ``sweep_checkpoints``.
#
# Retirement is attempted exactly once, minutes after a session ends, and
# refuses while a pending marker still points at the session. Nothing ever
# revisited a refusal, so a refusal was permanent: 216 directories (3.5 MB) had
# accumulated by 2026-08-10, one per session since Jul 7, none ever collected.
# The sweep is what revisits them.
#
# 7 days is far past every other clock in the system — extraction runs minutes
# after the session ends, and a marker expires after ``ttl_hours`` (6) — so a
# directory this old is not pinned by anything that is still going to happen.
_DEFAULT_GC_DAYS = 7.0

# Bound on one sweep, so a first run against a large backlog cannot turn a
# SessionStart hook into a long filesystem walk. Whatever is left is collected
# on the next start; the sweep logs when it stops early.
_SWEEP_MAX_RETIRE = 500

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
    # ``hard_stop_tokens = 0`` disables the block entirely (the default),
    # leaving the handoff nudge advisory exactly as it was.
    hard_stop_tokens: int = _DEFAULT_HARD_STOP
    # ``gc_days = 0`` disables checkpoint collection, keeping every session
    # directory forever — which is what every version before this one did.
    gc_days: float = _DEFAULT_GC_DAYS


def load_config() -> CheckpointConfig:
    """Build config from the plugin's ``checkpoint_*`` options.

    Malformed values fall back to defaults with a warning — config problems
    must never crash a hook. Bands are normalized to sorted-unique-positive;
    the handoff threshold is clamped to at least the highest band so a
    partial override can't produce a handoff below the last checkpoint.
    """
    enabled = option("checkpoint_enabled").lower() not in ("false", "0", "no", "off")

    raw_bands = option("checkpoint_tokens")
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

    handoff = option_int("checkpoint_handoff_tokens", bands[-1])
    if handoff < bands[-1]:
        logger.warning(
            "checkpoint_handoff_tokens=%d below last band %d; clamping",
            handoff, bands[-1],
        )
        handoff = bands[-1]

    # A hard stop below the handoff threshold would block prompts before the
    # nudge ever asks for a handoff — the user would meet the wall with no
    # warning. Clamp rather than honour it.
    hard_stop = max(0, option_int("checkpoint_hard_stop_tokens", _DEFAULT_HARD_STOP))
    if hard_stop and hard_stop < handoff:
        logger.warning(
            "checkpoint_hard_stop_tokens=%d below handoff %d; clamping",
            hard_stop, handoff,
        )
        hard_stop = handoff

    return CheckpointConfig(
        bands=bands,
        handoff_tokens=handoff,
        refresh_tokens=max(1, option_int("checkpoint_refresh_tokens", _DEFAULT_REFRESH)),
        ttl_hours=option_float("checkpoint_ttl_hours", _DEFAULT_TTL_HOURS),
        timeout_s=option_int("checkpoint_timeout_s", _DEFAULT_TIMEOUT_S),
        model=option("checkpoint_model") or None,
        enabled=enabled,
        stale_hours=max(0.0, option_float("checkpoint_stale_hours", _DEFAULT_STALE_HOURS)),
        min_session_minutes=max(
            0, option_int("checkpoint_min_session_minutes", _DEFAULT_MIN_SESSION_MINUTES)
        ),
        hard_stop_tokens=hard_stop,
        gc_days=max(0.0, option_float("checkpoint_gc_days", _DEFAULT_GC_DAYS)),
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

    A window that holds no usage record *at all* is retried with a doubled
    window (up to ``_MAX_WINDOW_DOUBLINGS``) before answering 0 — one
    oversized tool-result line at the tail must not read as "empty
    session". A window that reaches a determinate answer (a usage record,
    or the ``after_ts`` cutoff) is never retried: scanning further back
    can only find *older* records.
    """
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
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

    window = _TAIL_BYTES
    for _ in range(_MAX_WINDOW_DOUBLINGS + 1):
        tokens = _scan_usage_tail(path, size, window, cutoff)
        if tokens is not None:
            return tokens
        if window >= size:
            return 0  # whole file scanned — genuinely no usage records
        window *= 2
    return 0


def _scan_usage_tail(
    path: Path, size: int, window: int, cutoff: datetime | None
) -> int | None:
    """One pass over the last *window* bytes for the newest usage record.

    Returns a token count on a determinate answer (including 0 at the
    ``cutoff``), or ``None`` when the window held no decidable record —
    the caller's signal to widen and retry.
    """
    try:
        with path.open("rb") as f:
            if size > window:
                f.seek(size - window)
                f.readline()  # discard the partial first line
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0

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
    return None


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
      * ``refresh`` — the session has grown ``refresh_tokens`` past its last
        checkpoint, so a session doing real work keeps a current checkpoint
        even though nobody is around to /clear.

    ``refresh`` applies at **every** token level. It used to be gated behind
    ``tokens >= cfg.handoff_tokens``, which left a session at 150K with no
    token cadence at all — only the one-off 100K and 200K band crossings — so
    50K tokens of work could sit between checkpoints. ``handoff_tokens`` keeps
    its other job (governing the nudge) and no longer governs this one.

    The cadence still requires a previous checkpoint to measure growth
    *from*. Without one, ``last_checkpoint_tokens`` is 0 and every session
    past ``refresh_tokens`` would fire on its first Stop; the band trigger and
    ``staleness_trigger`` own the first write instead. (``reset_session_counters``
    zeroes the field after a rebuild for the same reason: the new physical
    window checkpoints from scratch.)
    """
    if not cfg.enabled:
        return None
    idx = band_index(tokens, cfg.bands)
    if idx > int(state.get("last_band_idx") or 0):
        return "band"
    last_tokens = int(state.get("last_checkpoint_tokens") or 0)
    if last_tokens > 0 and tokens - last_tokens >= cfg.refresh_tokens:
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

    Known blind spot — registry GC resets the age anchor. ``gc_stale``
    collects a non-parked entry after ``GC_LIVE_AFTER_DAYS`` (30 days) of
    silence, and ``record_event`` runs *before* the checkpoint pass in
    ``session_stop.main``, so on the user's return the entry is recreated
    with ``started_at = now``. A tab dormant for more than 30 days — the
    extreme case the stale trigger exists for — therefore reads as a
    0-minute-old session and gets no age checkpoint until
    ``min_session_minutes`` into the resumed work, at which point the
    trigger self-corrects. Parked sessions are immune (GC never collects
    ``disposition: parked`` entries, nor ones with a pending extraction
    marker), so the reset only hits tabs that went silent without being
    parked. Accepted as graceful degradation: the alternative — GC
    exemptions keyed to checkpoint state — would couple the registry to
    this module for a case that self-heals in half an hour. Pinned by
    ``TestRegistryGcAgeReset``.
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


def spawn_writer(payload: dict) -> bool:
    """Launch the detached ``checkpoint_writer.py`` with *payload* on stdin.

    Lives here rather than in ``session_stop.py`` because ``session_end.py``
    needs the identical spawn on ``/clear`` and a second copy would drift.
    Detached (``start_new_session=True``) and never awaited: the caller is a
    hook with a seconds-long budget, the child takes minutes.

    **The child only outlives its parent while the container does.** That is
    why ``session_end.py`` spawns for ``reason`` in {clear, resume} and writes
    a queue marker for every other reason: the session container runs under
    ``docker run --rm``, so on a real exit PID 1 goes and the detached child
    goes with it.
    """
    import subprocess  # local: hooks that never spawn shouldn't pay the import

    from multiplai_core.paths import get_paths

    from lib.runtime import uv_run_argv

    script = get_paths().scripts_dir() / "checkpoint_writer.py"
    if not script.exists():
        logger.warning("checkpoint_writer.py missing at %s", script)
        return False
    try:
        proc = subprocess.Popen(
            uv_run_argv(script),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(json.dumps(payload).encode("utf-8"))
            proc.stdin.close()
        return True
    except Exception:
        logger.exception("Failed to launch checkpoint writer")
        return False


# ---------------------------------------------------------------------------
# Degraded-write bookkeeping
# ---------------------------------------------------------------------------
#
# A checkpoint write that fails is not an event anyone sees: the writer is
# detached, its stderr goes to /dev/null, and its log line reaches a file
# nobody tails. Eight consecutive failures over 18 hours reached no one on
# 2026-08-08, and the component's entire job is not losing work. The counter
# below is what turns the second failure into a sentence in the user's
# session.

CONSECUTIVE_FAILURE_KEY = "consecutive_failures"

# Two, not one: a single failure is a retryable blip (the next Stop refires),
# two in a row is a pattern the user should hear about.
DEGRADED_ALERT_AFTER = 2


def consecutive_failures(state: dict) -> int:
    """How many checkpoint writes have failed in a row for this session."""
    try:
        return max(0, int(state.get(CONSECUTIVE_FAILURE_KEY) or 0))
    except (TypeError, ValueError):
        return 0


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
    return _sanitize_key(project) or "unknown"


def _sanitize_key(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in raw)


def _hostname_key() -> str:
    """The current container/machine name, sanitized for use in a filename.

    Empty when it cannot be read — callers then fall back to the legacy
    project-only marker name rather than inventing a discriminator.
    """
    try:
        from lib.session_registry import _hostname  # type: ignore

        return _sanitize_key((_hostname() or "").strip())
    except Exception:
        return ""


def session_hostname() -> str:
    """This process's container/machine name, sanitized for use as a key.

    Public because ``session_end.py`` has to *record* it: that hook is the last
    code that runs inside the session's own container, and the writer that
    needs it may run minutes later somewhere else entirely (#182).
    """
    return _hostname_key()


def marker_name(cwd: str, hostname: str | None = None) -> str:
    """Filename for the pending marker of *cwd*'s project on this host.

    Keyed by project **and** hostname. Project alone was the bug: two windows
    open on the same project share one marker file and the last writer wins,
    so on 2026-08-08 a second DolceBot window crossing its token band
    overwrote the pointer of the window that was about to be ``/clear``-ed,
    and the cleared window was rebuilt from the other one's checkpoint.

    ``hostname`` is the discriminator because it is the one the registry
    already records (``session_registry._hostname`` → ``$HOSTNAME``, the
    container name in kit containers) and because ``/clear`` keeps the same
    container — verified in the field: sessions ``24c0a766`` and ``2e29e3cb``
    are the pre- and post-``/clear`` halves of one tab and share hostname
    ``claude-work-04221854``. A window in a *different* container therefore
    cannot clobber this one's pointer, and the window that was cleared still
    finds its own.

    **This separates windows only where one window means one container** — the
    kit, where each session gets its own OrbStack container. On vanilla Claude
    Code ``$HOSTNAME`` is the machine, so every window on a project shares one
    key and the clobbering described above is unchanged there. Fixing that
    needs a per-window id the hook layer does not currently expose; keying on
    hostname is strictly better than keying on project alone in both setups,
    and it is not a general fix.

    Falls back to the legacy ``<project>.json`` when the hostname is unknown.

    Prefer :func:`session_marker_name` wherever a session id is in hand: both
    halves of this key are properties of the SESSION, and neither survives
    being re-derived from the writing process (see that function).
    """
    host = _sanitize_key(hostname) if hostname is not None else _hostname_key()
    key = _project_key(cwd)
    return f"{key}__{host}.json" if host else f"{key}.json"


def _registry_identity(data_dir: Path, session_id: str) -> dict:
    """The session's own registry entry, or ``{}`` when there isn't one.

    The hub registry (``lib/session_registry``) is the only place a session's
    project and hostname are recorded *as the session's own*, rather than
    re-derived from whichever process happens to be asking.
    """
    if not session_id or "/" in session_id or session_id in (".", ".."):
        return {}
    try:
        from lib.session_registry import registry_dir  # type: ignore

        raw = (registry_dir(data_dir) / f"{session_id}.json").read_text(encoding="utf-8")
        entry = json.loads(raw)
    except Exception:
        return {}
    return entry if isinstance(entry, dict) else {}


def session_marker_name(
    data_dir: Path, session_id: str, cwd: str, hostname: str | None = None
) -> str:
    """Marker filename for *session_id*, keyed by the session's OWN identity.

    :func:`marker_name` derives both halves of the key from the calling
    process — ``cwd`` for the project, ``$HOSTNAME`` for the container — and
    each of those is wrong for a different caller:

    * **Project (#183).** Claude Code's ``cwd`` follows shell navigation, so a
      session rooted at the workspace that does some work inside a sub-repo
      filed its marker under the SUB-REPO. A later ``/clear`` from the
      workspace root looked under the workspace, missed, and fell back to the
      legacy project-only marker — which on 2026-08-10 pointed at a different
      session entirely. The registry pins ``project`` on the first hook that
      sees the session and never moves it, so drift cannot move the pointer.
    * **Hostname (#182).** The host drain runs ``checkpoint_writer.py`` in a
      throwaway container after the session's own container has exited, so
      ``$HOSTNAME`` there is a random Docker id no future session will ever
      have. The marker was orphaned the moment it was written.

    Resolution order, each falling through only when the previous is empty:
    explicit *hostname* (the queued payload carries the session's own),
    then the registry entry, then this process's environment.
    """
    project, host = _session_key_parts(data_dir, session_id, cwd, hostname)
    return f"{project}__{host}.json" if host else f"{project}.json"


def _session_key_parts(
    data_dir: Path, session_id: str, cwd: str, hostname: str | None = None
) -> tuple[str, str]:
    """``(project, host)`` for :func:`session_marker_name`; ``host`` may be ""."""
    entry = _registry_identity(data_dir, session_id)
    project = _sanitize_key(str(entry.get("project") or "")) or _project_key(cwd)
    host = _sanitize_key(hostname) if hostname else ""
    host = host or _sanitize_key(str(entry.get("hostname") or "")) or _hostname_key()
    return project, host


def write_pending_marker(
    data_dir: Path,
    cwd: str,
    session_id: str,
    tokens: int,
    *,
    hostname: str | None = None,
) -> Path:
    """Record that *session_id* has a restorable checkpoint; keyed per window.

    Written whenever a checkpoint exists — the handoff threshold governs the
    *nudge*, not this. A session that ended at 143K tokens used to leave
    nothing restorable purely because 143K < 200K.

    *hostname* is the container the session ITSELF ran in. Pass it whenever the
    writer might not be that container — the host drain is exactly that case,
    and left unset it keyed the marker to a throwaway container id (#182).
    """
    pdir = _pending_dir(data_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    project, host = _session_key_parts(data_dir, session_id, cwd, hostname)
    marker = pdir / (f"{project}__{host}.json" if host else f"{project}.json")
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "tokens": tokens,
        "project": project,
        "hostname": host,
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

    Looks for this window's marker (``<project>__<hostname>.json``) first and
    falls back to the legacy project-only name, so a marker written by an
    older version — or on a host where the name cannot be read — is still
    claimable for its ``ttl_hours``.

    The project half of the key comes from the claiming session's registry
    entry rather than from live ``cwd``, matching what the write side records:
    on the compaction path the session has been running for hours and its cwd
    may have drifted into a sub-repo since the marker was written (#183).
    """
    pdir = _pending_dir(data_dir)
    project, host = _session_key_parts(data_dir, new_session_id, cwd)
    marker = pdir / (f"{project}__{host}.json" if host else f"{project}.json")
    if not marker.exists():
        legacy = pdir / f"{project}.json"
        marker = legacy if legacy != marker and legacy.exists() else marker
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


def pending_marker_owners(data_dir: Path) -> dict[str, str]:
    """``{session_id: marker filename}`` for every pending marker, one scan.

    The bulk form of :func:`pending_marker_owner`, for callers about to ask
    the question once per session directory: the sweep used to re-read and
    re-parse every marker for every candidate — O(sessions × markers) JSON
    parses inside a SessionStart hook (P9).
    """
    owners: dict[str, str] = {}
    try:
        markers = sorted(_pending_dir(data_dir).glob("*.json"))
    except OSError:
        return owners
    for marker in markers:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            sid = str(payload.get("session_id") or "")
            if sid and sid not in owners:
                owners[sid] = marker.name
    return owners


def retire_checkpoint(
    data_dir: Path,
    session_id: str,
    pending_owners: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Delete ``checkpoints/<session_id>/`` now that the diary supersedes it.

    Returns ``(removed, reason_kept)``, which distinguishes the three outcomes
    the caller logs differently:

    * ``(True, "")`` — the directory was there and is gone.
    * ``(False, "")`` — there was nothing to collect. The common case: most
      sessions never cross a token band, so they never had a checkpoint.
    * ``(False, "<reason>")`` — deliberately kept. A checkpoint that survives
      always says why.

    *pending_owners* (from :func:`pending_marker_owners`) lets a bulk caller
    pre-load the marker scan once; ``None`` keeps the per-call scan.

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
        if pending_owners is None:
            marker = pending_marker_owner(data_dir, session_id)
            marker_name = marker.name if marker is not None else None
        else:
            marker_name = pending_owners.get(session_id)
        if marker_name is not None:
            return False, f"pending rebuild marker {marker_name}"

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


def _marker_age_hours(marker: Path) -> float:
    """Age of *marker* in hours, from ``created_at`` and falling back to mtime.

    A marker whose payload cannot be parsed still has to age out — otherwise a
    truncated write pins a checkpoint against retirement forever.
    """
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(payload["created_at"]))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            created = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return 0.0
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def _last_activity(data_dir: Path, session_id: str, sdir: Path) -> datetime | None:
    """Newest sign of life for *session_id*: its files, or its registry event.

    The registry half is what keeps a live-but-quiet session's checkpoint: a
    session writes its checkpoint once and then may go hours without another,
    while every Stop and Notification restamps ``last_event``.
    """
    newest: float = 0.0
    try:
        for p in sdir.rglob("*"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
        newest = max(newest, sdir.stat().st_mtime)
    except OSError:
        pass
    latest = (
        datetime.fromtimestamp(newest, tz=timezone.utc) if newest else None
    )

    entry = _registry_identity(data_dir, session_id)
    raw = ((entry.get("last_event") or {}) if isinstance(entry, dict) else {}).get("ts")
    if raw:
        try:
            ts = datetime.fromisoformat(str(raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            latest = ts if latest is None else max(latest, ts)
        except (ValueError, TypeError):
            pass
    return latest


def sweep_checkpoints(data_dir: Path, cfg: CheckpointConfig) -> tuple[int, int]:
    """Expire dead pending markers, then collect the checkpoints they pinned.

    Returns ``(markers_expired, checkpoints_retired)``. Never raises — this
    runs inside SessionStart, where disk hygiene must never cost a session.

    Retirement is attempted exactly once per session, minutes after it ends,
    and :func:`retire_checkpoint` refuses while a pending marker still points
    at it. Nothing ever revisited a refusal, so every refusal was permanent —
    216 directories by 2026-08-10, none collected since Jul 7 (#181). Two
    changes in 0.39.0 turned that from an edge case into the common one: the
    marker is now written for *every* session with a checkpoint rather than
    only those past ``handoff_tokens``, and host-keyed markers are only ever
    removed by a claim from the same container, which for a closed tab never
    comes.

    This does NOT relax ``pending_marker_owner``. That guard is what stops a
    walked-away handoff losing its rebuild, and it stays. The sweep only
    removes markers the guard itself already considers dead — past
    ``ttl_hours`` — and then re-offers the sessions they were pinning.
    """
    expired = retired = 0
    try:
        pdir = _pending_dir(data_dir)
        for marker in sorted(pdir.glob("*.json")):
            if _marker_age_hours(marker) > cfg.ttl_hours:
                marker.unlink(missing_ok=True)
                expired += 1
        # Claim files are a half-second artefact of `consume_pending_marker`;
        # one that outlives its TTL means the claiming process died mid-read.
        for stray in sorted(pdir.glob("*.claimed-*")):
            if _marker_age_hours(stray) > cfg.ttl_hours:
                stray.unlink(missing_ok=True)
    except OSError:
        logger.warning("Pending-marker sweep failed (non-fatal)", exc_info=True)

    if cfg.gc_days <= 0:
        return expired, 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.gc_days)
    try:
        candidates = sorted(
            d for d in checkpoints_root(data_dir).iterdir()
            if d.is_dir() and d.name != "pending"
        )
    except OSError:
        return expired, 0

    # One marker scan for the whole sweep (P9): retire_checkpoint's own
    # per-session scan is O(sessions × markers) JSON parses from inside a
    # SessionStart hook. Snapshot semantics are fine here — every candidate
    # is at least gc_days old, so a marker appearing mid-sweep belongs to a
    # session this pass would not collect anyway.
    owners = pending_marker_owners(data_dir)
    for index, sdir in enumerate(candidates):
        if retired >= _SWEEP_MAX_RETIRE:
            # Count what the loop has not LOOKED AT yet (P14) — the old
            # ``len(candidates) - retired`` mixed up collections with
            # positions and overstated the backlog.
            logger.info(
                "Checkpoint sweep stopped at %d collections; %d directories "
                "remain for the next run",
                retired, len(candidates) - index,
            )
            break
        seen = _last_activity(data_dir, sdir.name, sdir)
        if seen is None or seen > cutoff:
            continue
        removed, kept = retire_checkpoint(data_dir, sdir.name, pending_owners=owners)
        if removed:
            retired += 1
        elif kept:
            logger.debug("Sweep kept checkpoint %s: %s", sdir.name, kept)

    if expired or retired:
        logger.info(
            "Checkpoint sweep: %d expired marker(s), %d checkpoint(s) collected",
            expired, retired,
        )
    return expired, retired


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


def _truthy(raw: str | None) -> bool:
    """Env-var truthiness, matching the CLI's own reading of these flags."""
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _autocompact_disabled_in_settings() -> bool:
    """Best-effort read of ``autoCompactEnabled: false`` from user settings.

    The env vars above are authoritative and always visible to a hook. This
    key is not: it lives in a settings file, and Claude Code layers several
    (managed / user / project / local) with rules this function does not
    reproduce. It reads the user-level file only — the one the /config
    toggle writes — because a false negative here costs a nudge that was
    already being shown, while missing the disable entirely costs the
    silence this whole function exists to prevent.
    """
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not cfg:
        return False
    try:
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("autoCompactEnabled") is False


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

    Checked in the binary's own order: ``DISABLE_COMPACT``, then
    ``DISABLE_AUTO_COMPACT``, then the ``autoCompactEnabled`` setting. Either
    env var beats the steering vars — leaving a window/pct pair behind when
    you disable compaction is the normal shape of that config, not a
    contradiction, and reading it as "auto mode on" silences the handoff
    advice in exactly the setup that has nothing else to fall back on.
    """
    if _truthy(os.environ.get("DISABLE_COMPACT")) or _truthy(
        os.environ.get("DISABLE_AUTO_COMPACT")
    ):
        return None
    if _autocompact_disabled_in_settings():
        return None

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
