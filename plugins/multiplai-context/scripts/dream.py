"""Dream consolidation script for multiplai plugin.

Default mode (no flags): generates a human-readable change proposal and writes it
to .multiplai/dreams/ for review. Run /multiplai-context:dream-remember to apply.

--auto: fully autonomous — applies changes directly to memory files without review.
--check: report pending learnings count, chunk plan and predicted duration, and exit.
--gc-learnings: delete learnings files that are fully consolidated and fully
    decided. Pure code, no model call.

The report path is a batching pipeline, not one big call: learnings are parsed
into `## Session Learnings` blocks, filtered against a ledger of what has already
been consolidated, packed into timeout-sized chunks, drafted concurrently, and
merged deterministically into ONE document. Learnings files are never moved or
deleted here — the ledger, not the filesystem, is what says "already done".
Deletion lives in exactly two places: `--auto` after a successful apply, and the
explicit `--gc-learnings` subcommand.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Per-CALL ceiling, and the number chunk sizes are derived from. It used to be
# 1800 s because dream made one serial call over the whole backlog — which is
# arithmetically guaranteed to time out past ~150 KB at the measured ~85 input
# bytes/s. Chunking inverts the relationship: the timeout is now the input to
# `plan_chunks`, which sizes every chunk to finish well inside it, so a lower
# ceiling makes a stuck call fail fast instead of burning half an hour. Must run
# before model_client is imported — the timeout is read into a module constant
# at import time; setdefault preserves an explicit override.
os.environ.setdefault("MULTIPLAI_SDK_CALL_TIMEOUT_S", "900")

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.config import load_yaml, save_yaml
from multiplai_core.log_utils import setup_logging
from generators.config import load_catalog_config
from generators.dispatcher import generate_catalogs
from lib import citation_repair, learnings_ledger
from lib.dream_processed import (
    PROCESSED_HEADING,
    Decision,
    has_pending_items,
    mark_many_processed,
    mark_processed,
)

logger = setup_logging("dream", propagate_loggers=("multiplai_core",))

# The per-chunk deadline handed to `plan_chunks` and to `query(timeout_s=…)`.
# Kept in step with the MULTIPLAI_SDK_CALL_TIMEOUT_S setdefault above.
CHUNK_TIMEOUT_S = 900.0

# Each chunk spawns a Claude Code CLI subprocess. Unbounded fan-out over a dozen
# chunks is a subprocess storm, so the gather runs behind a semaphore.
#
# 8, not 4, because 4 cannot finish a real backlog in a tolerable time. Measured
# on the 283 KB fixture: 48.9 B/s over 19 chunks is 5,875 s of model work, which
# four workers cannot do in under 24m28s however it is scheduled — the run took
# 37m55s end to end. Eight halves the floor to ~12m14s (bounded below by the one
# slowest chunk, 556 s), and the critic's to ~4m (bounded by its slowest batch,
# 465 s).
#
# The cost is eight concurrent CLI subprocesses on a machine that is usually also
# running the user's own session. That is the trade being made deliberately; the
# semaphore still bounds it, and MULTIPLAI_DREAM_CONCURRENCY lowers it for anyone
# who would rather wait than share the CPU.
DEFAULT_CONCURRENCY = 8

# Weight of the newest observation in the throughput EWMA. Low enough that one
# slow chunk (a cold model, a retried call) does not swing the next run's plan.
_THROUGHPUT_EWMA_ALPHA = 0.3
# Guard rails on the calibrated value: an outlier that poisons the stored EWMA
# would mis-size every future chunk, and the failure is silent.
_THROUGHPUT_MIN, _THROUGHPUT_MAX = 10.0, 2000.0

# Set once if the installed multiplai-core predates `query(timeout_s=…)`
# (< v0.12.0), so the fallback is logged once rather than per chunk.
_TIMEOUT_KWARG_UNSUPPORTED = False

# Holds the run lock's fd for the process lifetime. Closing it — including by
# letting it be garbage-collected — releases the lock, so it must stay reachable.
_RUN_LOCK_FD: int | None = None


# ---------------------------------------------------------------------------
# Run state — lock, staging area, throughput calibration
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Dream's git-ignored state bucket: lock, ledger, staged chunk drafts."""
    return get_paths().skill_state_dir("dream")


def _lock_path() -> Path:
    return _state_dir() / "lock"


def acquire_run_lock() -> bool:
    """Take dream's exclusive run lock. ``True`` if this process may proceed.

    Non-blocking ``flock`` on ``skill_state_dir("dream")/lock`` — **in the
    workspace, deliberately not in ``/tmp``**. Every Claude session runs in its
    own OrbStack container, so a ``/tmp`` path is container-local: two sessions
    invoking ``/dream`` concurrently — the exact scenario this exists for —
    would lock two *different* files and both proceed. The workspace is one
    shared filesystem inside the VM's single kernel, so a lock there really does
    exclude across session containers, and it keys by workspace for free.
    (``scripts/qmd_refresh.py`` keys on ``/tmp`` and has this hole; do not copy
    it.)

    The lock is held for the whole run — fold, draft, merge, critic, and the
    proposal write — and is **released by the OS when the process dies**. There
    is therefore no stale-lock class of bug and no cleanup path to get wrong:
    nothing unlinks the file, nothing has to run on the crash path, and a killed
    run leaves a lock that is already free. The fd is parked in a module global
    for exactly that reason — closing it would release the lock early.

    Residual limit: a **host-side** process (a dream run from a Mac terminal, or
    the multiplai-gui hub) locks in a different kernel and is not excluded.
    Accepted — the hub only ever invokes ``--stamp``/``--archive``, which do not
    take this lock at all, and host-terminal dream runs are not part of the
    workflow.

    On contention the holder's start time is read back out of the lock file and
    reported; the caller exits 0, because a second run is a no-op, not an error.
    Fails **open** if the lock file cannot be opened or locked at all (read-only
    or flock-less filesystem): refusing to consolidate is worse than the race
    the lock prevents.
    """
    global _RUN_LOCK_FD
    path = _lock_path()
    try:
        # No O_TRUNC: truncating before the flock attempt would erase the
        # holder's timestamp — the one thing the contention message needs.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        logger.exception("Could not open dream run lock %s — proceeding unlocked", path)
        return True

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 256).decode("utf-8", "replace").strip()
        except OSError:
            holder = ""
        os.close(fd)
        msg = (
            "Another dream run is already in progress"
            + (f" (started {holder})" if holder else "")
            + " — nothing to do."
        )
        logger.info(msg)
        print(msg)
        return False
    except OSError:
        os.close(fd)
        logger.exception("flock unavailable on %s — proceeding unlocked", path)
        return True

    try:
        os.ftruncate(fd, 0)
        os.write(fd, (datetime.now(timezone.utc).isoformat() + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError:
        logger.warning("Could not stamp the run lock with a start time", exc_info=True)
    _RUN_LOCK_FD = fd
    return True


def _dream_state_path() -> Path:
    """Calibration state, kept beside the ledger in the skill state bucket.

    Deliberately NOT ``paths.dream_state_file()``: that file is the *gate* state
    (``last_run``, ``files_updated``) that ``--stamp`` and the session hooks
    read, and a run's throughput measurements have no business landing in a
    record other tools poll for "has dream run recently".
    """
    return _state_dir() / "dream_state.yaml"


def _calibrated_throughput() -> float | None:
    """Stored input-bytes-per-second EWMA, or ``None`` if not calibrated yet."""
    try:
        state = load_yaml(_dream_state_path()) or {}
        value = state.get("throughput_bytes_per_s")
    except Exception:
        return None
    if isinstance(value, (int, float)) and _THROUGHPUT_MIN <= value <= _THROUGHPUT_MAX:
        return float(value)
    return None


def _update_throughput(observed: float) -> None:
    """Fold one chunk's observed B/s into the stored EWMA.

    Self-calibration matters because the 85 B/s default is one machine's
    measurement; a slower model or host silently makes every estimate a lie, and
    the estimate is what decides chunk size.
    """
    if not observed or observed <= 0:
        return
    observed = min(max(observed, _THROUGHPUT_MIN), _THROUGHPUT_MAX)
    try:
        path = _dream_state_path()
        state = load_yaml(path) or {}
        prior = _calibrated_throughput()
        value = observed if prior is None else (
            (1 - _THROUGHPUT_EWMA_ALPHA) * prior + _THROUGHPUT_EWMA_ALPHA * observed
        )
        state["throughput_bytes_per_s"] = round(value, 1)
        save_yaml(path, state)
    except Exception:
        logger.warning("Could not update throughput calibration", exc_info=True)


def _runs_dir() -> Path:
    """Staging area for per-chunk drafts: ``.../dream/runs/<run-id>/``."""
    return _state_dir() / "runs"


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _stage_draft(run_dir: Path, index: int, text: str, keys: list[str], kind: str = "chunk") -> Path:
    """Persist one draft plus the block keys it consolidated.

    Written **before** the ledger records those keys. Crashing in between costs
    a re-draft (the resume path discards a staged draft whose blocks are not
    ledgered), which is the safe direction: duplicated work, never lost input.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    md = run_dir / f"{kind}-{index:02d}.md"
    md.write_text(text)
    (run_dir / f"{kind}-{index:02d}.json").write_text(
        json.dumps({"kind": kind, "keys": list(keys)})
    )
    return md


def _resume_staged_drafts(current_run_dir: Path, ledger: dict) -> list[str]:
    """Drafts left behind by a crashed prior run, ready to join this merge.

    A staged draft is included only when every block it consolidated is recorded
    in the ledger. If it is not, those blocks are still `unprocessed` and are
    being re-drafted right now — keeping the stale draft too would put the same
    learning in front of the reviewer twice, so it is deleted instead.
    """
    out: list[str] = []
    runs = _runs_dir()
    if not runs.is_dir():
        return out
    processed = ledger.get("processed", {})
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        if run_dir == current_run_dir:
            continue
        for md in sorted(run_dir.glob("*.md")):
            side = md.with_suffix(".json")
            try:
                keys = list(json.loads(side.read_text()).get("keys") or [])
            except (OSError, json.JSONDecodeError):
                # An unreadable sidecar means "unknown blocks", not "no draft":
                # keep the content and let the ledger's own dedup handle any
                # overlap. Dropping it would be the one irreversible choice.
                logger.warning("Staged draft %s has no readable sidecar — kept anyway", md)
                keys = []
            if keys and not all(k in processed for k in keys):
                logger.warning(
                    "Discarding staged draft %s — its blocks are not ledgered, "
                    "so they are being re-drafted this run", md,
                )
                md.unlink(missing_ok=True)
                side.unlink(missing_ok=True)
                continue
            try:
                out.append(md.read_text())
            except OSError:
                logger.error("Unreadable staged draft %s — skipped", md, exc_info=True)
                continue
            logger.info("Resuming staged draft from an interrupted run: %s", md)
    return out


def _clear_staging() -> None:
    """Drop every staged run directory once their content is in a written proposal."""
    runs = _runs_dir()
    if not runs.is_dir():
        return
    for run_dir in runs.iterdir():
        if run_dir.is_dir():
            shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Learnings I/O
# ---------------------------------------------------------------------------

def _read_all_learnings(learnings_dir: Path) -> tuple[str, list[Path]]:
    """Read all pending learnings files. Returns (combined_text, source_files).

    Each content line is prefixed with its 1-indexed line number (matching what an editor
    shows for that file) so the model can cite `filename:line` provenance accurately rather
    than guessing — line numbers it can't see are line numbers it would fabricate.
    """
    if not learnings_dir.exists():
        return "", []
    files = sorted(learnings_dir.glob("*.md"))
    if not files:
        return "", []
    parts = []
    for f in files:
        raw = f.read_text()
        if not raw.strip():
            continue
        numbered = "\n".join(
            f"{i}: {line}" for i, line in enumerate(raw.splitlines(), start=1)
        )
        parts.append(f"### File: {f.name}\n\n{numbered}")
    combined = "\n\n---\n\n".join(parts)
    return combined, files


def _proposal_output_path(dreams_dir: Path, today: str) -> Path:
    """Return a non-colliding path for today's proposal.

    A same-day dream run (scheduled, or kicked off in parallel) must never
    silently overwrite a proposal that may be mid-review in dream-remember —
    that's silent data loss and forces a full re-generation (observed
    2026-06-21). If the base name is free, use it; otherwise append an
    incrementing counter so the prior proposal survives untouched.
    dream-remember globs `processed-learnings-*.md` and takes the most recent
    by mtime, so the versioned name is still discovered first.
    """
    base = dreams_dir / f"processed-learnings-{today}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = dreams_dir / f"processed-learnings-{today}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def _archive_proposal(proposal_path: Path, dreams_dir: Path, disposition: str = "applied") -> Path:
    """Move a reviewed proposal out of the dreams root into `applied/` (or
    `rejected/`), so the root holds only pending proposals.

    Collision-safe: `_proposal_output_path` only checks the dreams root, so a
    same-day re-run reuses a base name freed by an earlier archive. Archiving
    that second proposal must not clobber the first — suffix like the root
    naming does. A plain rename is used even in git-tracked workspaces: git
    detects the rename at the next commit, and `git mv` would fail on the
    (typically untracked) freshly generated proposal.
    """
    dest_dir = dreams_dir / disposition
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / proposal_path.name
    if dest.exists():
        stem, suffix = proposal_path.stem, proposal_path.suffix
        n = 2
        while (dest_dir / f"{stem}-{n}{suffix}").exists():
            n += 1
        dest = dest_dir / f"{stem}-{n}{suffix}"
    proposal_path.rename(dest)
    return dest


def _collect_blocks(learnings_dir: Path) -> tuple[list, list[Path]]:
    """Parse every learnings file into ``## Session Learnings`` blocks.

    Returns ``(blocks, files)`` in filename order. Nothing is moved or deleted:
    which blocks are new is a set difference against the ledger, not a question
    about which files happen to be on disk.
    """
    if not learnings_dir.exists():
        return [], []
    files = sorted(learnings_dir.glob("*.md"))
    blocks: list = []
    for f in files:
        try:
            blocks.extend(learnings_ledger.parse_blocks(f.name, f.read_text()))
        except OSError:
            logger.warning("Could not read %s — skipped", f, exc_info=True)
    return blocks, files


# ---------------------------------------------------------------------------
# "dream wrote this, untouched" stamp
# ---------------------------------------------------------------------------
#
# Spike curates a proposal by editing the file directly — deleting whole
# sections and individual items so that what remains is exactly what he wants
# applied. A curated proposal has no `## Processed` items, so the fold in
# `_fold_pending_proposals` would happily absorb it and destroy the curation.
# The stamp is the guard: fold only a document that is byte-identical to what
# dream wrote.
#
# Content hash, NOT mtime: `.multiplai/dreams/` is git-tracked, and
# checkout/stash/restore rewrite mtimes — an mtime guard would read untouched
# proposals as hand-edited and start exactly the `-2` pileup the fold exists to
# stop. "Differs from what dream wrote" is the real criterion, so hash it.

_GENERATED_MARKER_RE = re.compile(r"^<!-- dream:generated sha256=([0-9a-f]{64}) -->$")


def _stamp_generated(text: str) -> str:
    """Append the ``<!-- dream:generated sha256=… -->`` trailer to a proposal."""
    body = text.rstrip("\n") + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{body}<!-- dream:generated sha256={digest} -->\n"


def _is_unmodified_generated(text: str) -> bool:
    """True only if *text* still hashes to the value in its own trailer.

    A proposal written by a pre-ledger dream carries no trailer and can never
    pass — by design. Leaving those alone is the safe default, not a bug.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return False
    m = _GENERATED_MARKER_RE.match(lines[-1].rstrip("\n"))
    if not m:
        return False
    body = "".join(lines[:-1])
    return hashlib.sha256(body.encode("utf-8")).hexdigest() == m.group(1)


# Sections dream re-derives from current state on every run. A folded proposal
# carries yesterday's copies; merging them in would put two `## Routing Warnings`
# sections in one document, the stale one first.
_REGENERATED_SECTIONS = ("## Routing Warnings", "## Conflict Resolutions")


def _strip_regenerated(text: str) -> str:
    """Reduce a finished proposal back to a draft, ready to merge.

    Drops the ``dream:generated`` trailer (it describes the file it was written
    into, not the content) and any deterministically regenerated section.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = any(line.startswith(h) for h in _REGENERATED_SECTIONS)
        if skipping:
            continue
        if _GENERATED_MARKER_RE.match(line):
            continue
        out.append(line)
    while out and (not out[-1].strip() or out[-1].strip().startswith("---")):
        out.pop()
    return "\n".join(out) + "\n"


def _has_decided_items(text: str) -> bool:
    """True if any item block sits under ``## Processed``.

    That heading is the cross-tool decision contract with multiplai-gui. A
    proposal carrying decisions is off-limits: folding it would discard them,
    and renumbering it would make a decision the hub has already queued apply to
    the wrong entry.
    """
    in_processed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == PROCESSED_HEADING:
            in_processed = True
            continue
        if stripped.startswith("## "):
            in_processed = False
            continue
        if in_processed and stripped.startswith("### "):
            return True
    return False


def _fold_verdict(text: str) -> tuple[bool, str]:
    """Whether a pending proposal may be folded, and why not when it may not."""
    if _has_decided_items(text):
        return False, "it carries decided items under `## Processed`"
    if not _is_unmodified_generated(text):
        return False, (
            "its content differs from what dream wrote (hand-curated, or written "
            "by a pre-ledger dream that left no `dream:generated` stamp)"
        )
    if not has_pending_items(text):
        return False, "it has no pending items to fold"
    return True, ""


def _fold_pending_proposals(dreams_dir: Path) -> list[str]:
    """Absorb undecided, untouched pending proposals into this run.

    Called at the START of the run, under the lock, before any drafting — not at
    write time. The hub writes `## Processed` blocks into a proposal *item by
    item* during an apply, so "has no decided items" is a claim that decays over
    a multi-minute run; checking first shrinks the window from minutes to
    milliseconds. The verdict is re-taken immediately before each individual
    move, inside the same lock hold, so a proposal that gained a decision during
    the scan is skipped rather than swallowed.

    Returns the folded documents' text, in filename order. The files themselves
    move to ``dreams/superseded/`` — a sibling of ``applied/``/``rejected/``,
    with the same collision-safe suffixing — so the hub's non-recursive glob of
    the dreams root stops listing them, exactly as archiving does.
    """
    folded: list[str] = []
    if not dreams_dir.is_dir():
        return folded
    for path in sorted(dreams_dir.glob("processed-learnings-*.md")):
        try:
            text = path.read_text()
        except OSError:
            logger.warning("Could not read pending proposal %s — left in place", path.name)
            continue
        ok, reason = _fold_verdict(text)
        if not ok:
            logger.info("Not folding %s — %s; left pending", path.name, reason)
            continue
        # Re-check against the file as it stands right now: the hub may have
        # written a decision into it since the scan above.
        try:
            fresh = path.read_text()
        except OSError:
            continue
        if fresh != text:
            logger.info("Not folding %s — it changed during the scan; left pending", path.name)
            continue
        ok, reason = _fold_verdict(fresh)
        if not ok:
            logger.info("Not folding %s — %s; left pending", path.name, reason)
            continue
        try:
            dest = _archive_proposal(path, dreams_dir, "superseded")
        except OSError:
            logger.exception("Could not supersede %s — left pending, not folded", path.name)
            continue
        folded.append(_strip_regenerated(fresh))
        logger.info("Folded undecided proposal %s into this run (moved to %s)",
                    path.name, dest)
    return folded


def _read_memory_files(memory_dir: Path) -> dict[str, str]:
    """Return {filename: content} for all .md files in memory_dir."""
    if not memory_dir.exists():
        return {}
    return {
        f.name: f.read_text()
        for f in sorted(memory_dir.glob("*.md"))
        if f.name != "learnings.md"
    }


def _extract_headers(content: str) -> str:
    """Return H1–H3 headers from markdown content."""
    headers = [l for l in content.split("\n") if l.startswith("#")]
    return "\n".join(headers) if headers else content[:300]


def _load_memory_catalog(catalogs_dir: Path) -> dict[str, dict]:
    """Return {filename: {summary, intent_domains, anti_domains}} from the memory catalog.

    The catalog (built by the router) carries each memory file's domain — its
    summary, intent_domains, and anti_domains (what does NOT belong there).
    Routing by that domain is far more reliable than guessing from
    section-header names, which makes broadly-named files (e.g.
    ai-agent-patterns.md) act as catch-alls. Returns {} if the catalog is absent
    or unreadable — the proposal then falls back to headers-only routing.
    """
    import json

    catalog_file = catalogs_dir / "memory.json"
    if not catalog_file.exists():
        return {}
    try:
        data = json.loads(catalog_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("entries", []):
        src = entry.get("source")
        if src:
            out[src] = {
                "summary": entry.get("summary", ""),
                "intent_domains": entry.get("intent_domains", []),
                "anti_domains": entry.get("anti_domains", []),
            }
    return out


# ---------------------------------------------------------------------------
# Report mode (default)
# ---------------------------------------------------------------------------

_PROPOSAL_SYSTEM = """\
You are a memory consolidation analyst for a personal Claude Code memory system.

## The one thing to understand

Every learning has exactly ONE of three dispositions. Choosing correctly IS the job:

- DIARY (already recorded elsewhere) — WHAT HAPPENED: facts, events, decisions, fixes, in
  chronological order. Never duplicate it → these get FILTERED OUT.
- MEMORY (what you mostly write to) — GENERALIZED, REUSABLE KNOWLEDGE: guidance that changes
  how a FUTURE, DIFFERENT task is done.
- ACTION ITEM — a concrete change the TOOLCHAIN ITSELF should make to its own code, config,
  or structure (the memory/dream/plugin system, file layout, scripts). This is engineering
  work, NOT knowledge to remember. It goes in its own section and does NOT become memory.

Your job is NOT to log this session. It is to DISTILL the pending learnings: most are diary
(drop), some are reusable knowledge (memory), a few are change-requests to the system (action
items). A learning that only says "X happened" / "we decided Y" / "fixed Z" is diary — drop
it unless it contains a general lesson you can lift out.

The MEMORY-vs-ACTION-ITEM cut: if a learning says the system *should be changed* ("split these
files", "delete this orphan", "this script should also check X"), the change is an ACTION ITEM.
Then ask whether it ALSO carries a general principle that outlives the change — one that would
guide a DIFFERENT future situation after this task is done and forgotten:
- NO — pure cleanup with no transferable rule (e.g. "delete this stale orphan file", "remove
  this dead catalog reference") -> Action Item only, no memory.
- YES — e.g. "before making ANY repo public, scrub+rotate secrets and strip employer content"
  (useful for the next repo), or "split memory files by retrieval domain, not topic affinity"
  (a design heuristic for the next file) -> BOTH: the principle as a memory entry AND the
  concrete change as an action item.
Memory is for knowledge that informs work, not a backlog of refactors — but a durable principle
earns its memory place even when it also spawns a task.

## Generalization transform (apply to every candidate)

Strip the point-in-time scaffolding, keep the transferable rule:

- DROP: commit hashes / SHAs; "committed as ...", "fixed in ...", "decided on <date>";
  finished-task residue ("update file X", "rename Y now"); one-off absolute paths;
  specific project / repo / file names UNLESS the lesson is genuinely scoped to that
  project and useless elsewhere.
- KEEP, phrased as conditional guidance: "When <situation>, do <action>, because
  <outcome>." Prefer this shape over a narrated fact.

## Litmus gate (decide keep vs filter)

For each candidate ask, in order:
1. "Does this ask the toolchain to change its own code/config/structure?" -> ACTION ITEM.
2. "Facing a DIFFERENT but similar situation in the future, does this change what I'd do?"
   - YES, and it reads as transferable guidance -> KEEP (generalized) as memory.
   - It only records that something happened / was decided / was fixed -> FILTER OUT.
   - A true general lesson wrapped in specifics -> KEEP THE LESSON, DROP THE SPECIFICS.

## Examples

RAW: "npm install -g @anthropic-ai/claude-code is deprecated. Use
curl -fsSL https://claude.ai/install.sh | bash. Update multiplai-container Dockerfile."
KEEP: "Claude Code is no longer installed via npm; official method is
`curl -fsSL https://claude.ai/install.sh | bash`."
(Dropped "update the Dockerfile" — a one-time task, now done.)

RAW: "pluginConfigs key must be plugin@marketplace compound form; wrong key silently
falls back to the home-directory defaults with no error. Sideloaded plugins ignore
pluginConfigs — use CLAUDE_PLUGIN_OPTION_* env vars. Committed as a8cbec9."
KEEP: "`pluginConfigs` keys use the compound `plugin@marketplace` form; a wrong key
fails silently (falls back to defaults, no error). Sideloaded plugins (`--plugin-dir`)
ignore `pluginConfigs` — pass options via `CLAUDE_PLUGIN_OPTION_*` env vars instead."
(Dropped the specific fallback path and the commit SHA.)

RAW: "Decision (2026-06-15): multiplai-core, mktplace, and kit all going public.
Pre-public: scrub gho_ token from history + rotate; remove scalestack skill;
secret scan. De-personalization machinery deleted; identity moves to memory."
FILTER OUT as written — it's a dated decision + checklist (diary). If a reusable rule
exists, extract ONLY that: "Before making any repo public, scrub secrets from git
history AND rotate them, and strip employer-specific content."

RAW: "multiplai-plugin git fetch not run regularly; origin/main tracking ref weeks
stale. Always fetch before checking ahead/behind or assuming sync with remote."
KEEP: "Always `git fetch` before checking ahead/behind counts or assuming sync with a
remote — local tracking refs go stale."
(Dropped the multiplai-plugin framing; the lesson is general.)

RAW: "Memory files covering multiple domains (career facts + career strategy) degrade
routing precision — split memory files by retrieval domain, not topic affinity."
ACTION ITEM only: "Delete the stale `.multiplai/memory/memory-catalog.json` orphan (the live
catalog is `.multiplai/data/catalogs/memory.json`)."
(Pure cleanup — no rule that outlives the deletion, so NO memory entry.)

RAW: "Mixed-domain memory files (career facts + career strategy) degrade routing precision —
split memory files by retrieval domain, not topic affinity."
BOTH — ACTION ITEM: "Split career-history vs career-strategy by retrieval domain." PLUS MEMORY
(design heuristic, guides the next file too): "Split memory files by retrieval domain, not
topic affinity — mixed-domain files degrade routing precision."

RAW: "Decision: scrub gho_ token from kit history + rotate before going public; remove
scalestack skill (employer content)."
BOTH — ACTION ITEMS: "Scrub gho_ token from kit history and rotate it"; "Remove scalestack
skill from the public kit". PLUS MEMORY (general principle, outlives these one-time tasks):
"Before making any repo public, scrub secrets from git history AND rotate them, and strip
employer-specific content."

## Output format

```
# Processed Learnings — {date}

**Sources:** {N} files, ~{M} entries
Generated by Dream. Review with `/multiplai-context:dream-remember` to apply.

---

## Updates for `{filename}`

### {N}. {short_title}
**Section:** {existing section or "New section"}
**Change:** add / update / replace
> Exact text to insert (generalized, concise, ideally "When X, do Y")

**Source:** {learnings_file}:{line-number(s)}

---

## Action Items ({N} items)

Changes the toolchain itself should make — NOT memory. Approved ones get written to PLANS/.

### A{N}. {short imperative title}
**What:** concrete change to make (file/script/config + the change), one or two lines.
**Why:** the problem it fixes.
**Source:** {learnings_file}:{line-number(s)}

---

## Filtered Out ({N} items)

**{reason}**
- {short title} — {reason} (Source: {learnings_file}:{line})
- {short title} — {reason} (Source: {learnings_file}:{line})

**{other reason}**
- {short title} — {reason} (Source: {learnings_file}:{line})
```

`## Filtered Out` is an audit trail, so it is EXACTLY ONE LINE PER DROPPED ITEM — grouped
under its reason, no sub-bullets, no quoted text, no explanation paragraphs. Every dropped
item gets its own line: a dropped learning is marked consolidated and never resurfaces, so
a bare count would make the drop silent and permanent. Reasons: diary/event-only, already
applied, too specific, task residue, superseded, no clear target file.

Title markers (prefix the {short_title}, none in the normal case):
- **[RULE-PROPOSAL]** — a change to CLAUDE.md behavioral rules; requires individual approval.
- **[warning low confidence]** — an item you are including despite weak/unverified support.

## Routing — pick the target file by DOMAIN, not by header keyword

Each candidate file is shown with PURPOSE, OWNS DOMAINS, and NOT HERE (its anti-domains).
Route each entry to the file whose domain actually owns the learning's SUBJECT, then pick a
section within it. The headers only choose the section — never the file. All file-specific
routing knowledge is in those blocks — apply these generic principles to them:
- Respect NOT HERE: when a file's NOT-HERE line names the learning's subject, that file is
  disqualified — route to the file whose PURPOSE owns the subject instead.
- No catch-alls: broadly-named files are never fallbacks. Route by what the lesson is ABOUT,
  not by which tool or agent happened to perform the work — "an agent ran the migration" does
  not make it an agent pattern.
- Portability test: would the knowledge survive switching away from this specific tool or
  platform? Tool/platform-agnostic principles go to the general craft/design file for their
  subject; knowledge about operating a specific tool or platform goes to that tool's file.
- If no file's domain fits, say so (propose a new file or filter) — do not force-fit into the
  nearest broadly-named file.

## Rules

- Group updates by target memory file. Do NOT print per-file learning counts, "seen Nx"
  repetition notes, or trust levels — they cost tokens and serve no reader.
- Each entry ends with a **Source:** line for provenance: the learnings filename and the
  line number(s) it was distilled from, so the origin is traceable on re-processing. The
  pending learnings are shown with `N: ` line-number prefixes — cite those exact numbers.
  Format `filename:line` or `filename:start-end`; if an entry merges several learnings, cite
  each separated by `; ` (e.g. `2026-06-15.md:42; 2026-06-16.md:10-12`). Cite only numbers
  you actually see — never invent a line number.
- Deduplicate: if the same lesson appears multiple times, merge into one entry. (Don't
  annotate the count.)
- Resolve contradictions: keep the most recent / most reliable version; note what was superseded.
- **Date volatile facts.** If a proposed line makes a claim about the *present* that names a
  changeable value — a price or rate, a version or model generation, who someone works for,
  "the current/latest/best X" — append `(as of YYYY-MM)`, and add `, review by YYYY-MM` when
  you can name a date by which it should be re-checked. Memory files carry only a file-level
  freshness stamp, so an undated volatile fact reads as true forever. Do NOT annotate
  permanent facts that merely mention a number ("Swift 6.3 rejects covariant Self" is not a
  volatile fact), and do NOT annotate a fact already marked historical.
- Most learnings are verified — do NOT label them. Filter out genuinely junk low-trust
  single-occurrence items. If you DO include a weakly-supported item, prefix its title with
  **[warning low confidence]** instead of dropping it.
- Filter out: diary/event-only entries, finished-task residue, already-applied facts,
  one-time fixes with no general pattern, entries with no clear target file. List each
  dropped item on its own single line under its reason heading — one line, never more.
- Route to Action Items any learning that calls for the toolchain to change its own
  code/config/file-structure (A{N} numbering, **Source:** line, same provenance rules). Do NOT
  mirror it as a memory entry UNLESS it also carries a general principle that outlives the
  change (one that would guide a different future situation) — then keep BOTH: the principle as
  memory, the concrete change as the action item. If the principle is just the action restated,
  action only.
- Omit the Action Items section entirely if there are none.
- Keep proposed text concise — one-line bullets over paragraphs. Memory costs tokens.
- Never invent changes not supported by the learnings.
"""


def _memory_context(memory_contents: dict[str, str]) -> str:
    """Render the memory-domain block the drafting and critic passes both see.

    Each file's catalog domain (summary + intent_domains + anti_domains) drives
    routing; the headers only pick the section WITHIN the chosen file. This block
    is byte-identical across every chunk of a run, so the shared prompt prefix
    stays cacheable — do not vary it per chunk.
    """
    catalog = _load_memory_catalog(get_paths().catalogs_dir())
    blocks = []
    for name, content in memory_contents.items():
        meta = catalog.get(name, {})
        lines = [f"### {name}"]
        if meta.get("summary"):
            lines.append(f"PURPOSE: {meta['summary']}")
        if meta.get("intent_domains"):
            lines.append("OWNS DOMAINS: " + "; ".join(meta["intent_domains"]))
        if meta.get("anti_domains"):
            lines.append("NOT HERE: " + "; ".join(meta["anti_domains"]))
        lines.append(f"SECTIONS:\n{_extract_headers(content)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _draft_prompt_prefix(memory_context: str, source_names: str) -> str:
    """Everything in the user message that precedes the learnings themselves.

    Constant for the whole run (including the source-file list, which names the
    run's files rather than the chunk's) so that only the tail of the prompt
    differs between chunks.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Today's date: {today}\n"
        f"Source files: {source_names}\n\n"
        f"## Current memory file structure:\n\n{memory_context}\n\n"
        f"## Pending learnings:\n\n"
    )


async def _query(client, system: str, messages: list[dict], timeout_s: float | None):
    """``client.query`` with a per-call timeout, tolerating an older core.

    ``timeout_s`` on ``query()`` arrives in multiplai-core v0.12.0. Against an
    older pin the keyword does not exist, so the call is retried without it and
    every chunk falls back to the module-wide ceiling. Patching
    ``model_client._SDK_CALL_TIMEOUT_S`` instead is not an option: it is private,
    and it is racy under ``asyncio.gather`` with concurrent chunks.
    """
    global _TIMEOUT_KWARG_UNSUPPORTED
    if timeout_s and not _TIMEOUT_KWARG_UNSUPPORTED:
        try:
            return await client.query(system=system, messages=messages, timeout_s=timeout_s)
        except TypeError as exc:
            if "timeout_s" not in str(exc):
                raise
            _TIMEOUT_KWARG_UNSUPPORTED = True
            logger.warning(
                "Installed multiplai-core has no query(timeout_s=…) (pre-v0.12.0) — "
                "per-chunk timeouts fall back to MULTIPLAI_SDK_CALL_TIMEOUT_S"
            )
    return await client.query(system=system, messages=messages)


async def _generate_proposal(
    client,
    all_learnings: str,
    memory_contents: dict[str, str],
    source_files: list[Path],
) -> str:
    """Single-call proposal generation, kept for ``--auto``.

    Report mode uses the chunked pipeline in :func:`_draft_chunks` instead; this
    stays because ``--auto``'s applier stage is out of scope and shares it.
    """
    source_names = ", ".join(f.name for f in source_files)
    memory_context = _memory_context(memory_contents)
    messages = [
        {
            "role": "user",
            "content": _draft_prompt_prefix(memory_context, source_names) + all_learnings,
        }
    ]

    response = await client.query(system=_PROPOSAL_SYSTEM, messages=messages)
    cleaned = await _critique_proposal(client, response.content, memory_context)
    sourced = _with_repaired_citations(cleaned, get_paths().learnings_dir)
    validated = _with_routing_warnings(sourced, memory_contents)
    return _with_conflict_resolutions(validated, all_learnings, memory_contents)


# ---------------------------------------------------------------------------
# Chunked parallel draft
# ---------------------------------------------------------------------------

def _concurrency() -> int:
    raw = os.environ.get("MULTIPLAI_DREAM_CONCURRENCY", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY
    return max(1, value)


def _plan_line(*, new_bytes: int, total_bytes: int, chunks: list, throughput: float) -> str:
    """One planning line, logged and printed BEFORE anything is spent."""
    from lib import dream_chunking

    return dream_chunking.format_plan_line(
        new_bytes=new_bytes,
        total_bytes=total_bytes,
        chunks=chunks,
        concurrency=_concurrency(),
        throughput=throughput,
    )


async def _draft_one_chunk(client, chunk, *, prefix: str, sem, run_dir: Path,
                           ledger_path: Path, proposal_name: str) -> str | None:
    """Draft one chunk, stage it, and ledger its blocks. ``None`` on failure.

    Staging and recording happen here rather than after the gather so that a
    crash costs only the in-flight chunks. The order is deliberate: draft on
    disk first, ledger second.
    """
    from lib import dream_chunking

    async with sem:
        started = time.monotonic()
        messages = [{"role": "user", "content": prefix + dream_chunking.render_chunk(chunk)}]
        try:
            response = await _query(client, _PROPOSAL_SYSTEM, messages, chunk.timeout_s)
        except Exception:
            logger.exception(
                "Chunk %02d failed (%d bytes) — skipped; its blocks stay unprocessed "
                "for the next run", chunk.index, chunk.n_bytes,
            )
            return None
        elapsed = max(time.monotonic() - started, 1e-6)

    text = (response.content or "").strip()
    if not text:
        logger.warning(
            "Chunk %02d returned nothing (%d bytes, %.0fs) — skipped; its blocks stay "
            "unprocessed", chunk.index, chunk.n_bytes, elapsed,
        )
        return None

    observed = chunk.n_bytes / elapsed
    logger.info(
        "Chunk %02d complete: %d bytes in, %d bytes out, %.0fs, %.0f B/s",
        chunk.index, chunk.n_bytes, len(text.encode("utf-8")), elapsed, observed,
    )
    try:
        _stage_draft(run_dir, chunk.index, text, [b.key for b in chunk.blocks])
        learnings_ledger.record(ledger_path, list(chunk.blocks), proposal_name)
    except OSError:
        logger.exception(
            "Could not stage/ledger chunk %02d — the draft still joins this merge",
            chunk.index,
        )
    _update_throughput(observed)
    return text


async def _draft_chunks(client, chunks: list, *, memory_context: str, source_names: str,
                        run_dir: Path, ledger_path: Path, proposal_name: str) -> list[str]:
    """Draft every chunk concurrently behind a semaphore, in chunk order.

    A failed chunk is skipped: its blocks are never ledgered, so the next run
    picks them up. Only a run in which EVERY chunk failed re-raises — that is an
    outage, not a bad block, and swallowing it would write an empty proposal and
    look like success.
    """
    prefix = _draft_prompt_prefix(memory_context, source_names)
    sem = asyncio.Semaphore(_concurrency())
    results = await asyncio.gather(*(
        _draft_one_chunk(
            client, chunk, prefix=prefix, sem=sem, run_dir=run_dir,
            ledger_path=ledger_path, proposal_name=proposal_name,
        )
        for chunk in chunks
    ))
    drafts = [r for r in results if r]
    if chunks and not drafts:
        raise RuntimeError(
            f"All {len(chunks)} draft chunk(s) failed — see the per-chunk tracebacks above"
        )
    if len(drafts) < len(chunks):
        logger.warning(
            "%d of %d chunks failed; their blocks stay unprocessed for the next run",
            len(chunks) - len(drafts), len(chunks),
        )
    return drafts


def _enforce_ledger_coverage(ledger_path: Path, staged_run_dir: Path, merged: list[str]) -> None:
    """Un-record any block whose draft did not reach the merge.

    The hard invariant of this pipeline: a block marked consolidated but absent
    from the written proposal is silent learning loss — it never resurfaces and
    nobody ever sees it. Recording is tied to a staged draft, and the merge input
    is built from those same staged drafts, so this should be structurally
    impossible; it is checked anyway, and a violation is repaired by dropping the
    keys so the next run re-consolidates them.
    """
    try:
        merged_texts = set(merged)
        orphan: list[str] = []
        for side in sorted(staged_run_dir.glob("*.json")):
            md = side.with_suffix(".md")
            meta = json.loads(side.read_text())
            keys = list(meta.get("keys") or [])
            if not keys:
                continue
            if not md.exists() or md.read_text() not in merged_texts:
                orphan.extend(keys)
        if not orphan:
            return
        logger.error(
            "Ledger coverage violation: %d block(s) were recorded but their draft did "
            "not reach the merge — un-recording so they are consolidated next run",
            len(orphan),
        )
        ledger = learnings_ledger.load(ledger_path)
        processed = ledger.get("processed", {})
        for key in orphan:
            processed.pop(key, None)
        learnings_ledger.save(ledger_path, ledger)
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not verify ledger coverage")


def _with_conflict_resolutions(
    proposal: str, all_learnings: str, memory_contents: dict[str, str]
) -> str:
    """Prepend the deterministic ``## Conflict Resolutions`` section.

    Generic consolidation *may* notice that a verified correction contradicts an
    existing memory line. Nothing makes it. This pass finds those pairings in
    code and puts them where review starts, because a stale line that
    contradicts a confirmed fact keeps teaching the wrong thing until someone
    spots it.

    Placed above the model's own output rather than appended: the model's
    section headings are what a reviewer skims, and a conflict block below them
    reads as a footnote. Fail-open like the routing gate — a crash here must
    never lose a generated proposal.
    """
    try:
        from lib.conflict_edits import conflict_section_for

        section = conflict_section_for(all_learnings, memory_contents)
    except Exception:
        logger.exception("Conflict-edit pass failed; proposal left unannotated")
        return proposal

    if not section:
        return proposal

    logger.info("Conflict-edit pass: %d resolution(s) prepended",
                section.count("\n### "))
    return f"{section}\n\n---\n\n{proposal}"


def _with_repaired_citations(proposal: str, learnings_dir: Path) -> str:
    """Correct ``**Source:**`` citations that name the wrong learnings file.

    A record that opens ``## Session Learnings — 2026-07-28T20:54`` but lives in
    ``2026-07-29.md`` (its session ran past midnight) is sometimes cited under
    the timestamp's date rather than the file it was rendered from. The line
    number stays right, which is what lets `lib/citation_repair` fix the
    filename deterministically instead of guessing.

    Fail-open like the routing and conflict gates: a crash here must never lose
    a generated proposal. See `lib/citation_repair` for why it repairs only
    provably-broken, unambiguously-resolvable citations.
    """
    try:
        from lib import citation_repair

        blocks, files = _collect_blocks(learnings_dir)
        learnings = {}
        for f in files:
            try:
                learnings[f.name] = f.read_text()
            except OSError:
                logger.warning("Could not read %s for citation repair", f.name)

        repaired, findings = citation_repair.repair_citations(
            proposal, blocks, learnings
        )
        section = citation_repair.render_findings(findings)
    except Exception:
        logger.exception("Citation repair failed; proposal left as written")
        return proposal

    if not findings:
        logger.info("Citations verified — every **Source:** line resolves")
        return proposal

    fixed = sum(1 for f in findings if f.repaired)
    logger.info(
        "Citation repair: %d corrected, %d left unresolved",
        fixed, len(findings) - fixed,
    )
    for finding in findings:
        if not finding.repaired:
            logger.warning(
                "Unverifiable citation %s:%d — %s",
                finding.cited_file, finding.line, finding.reason,
            )
    return f"{repaired.rstrip()}\n\n---\n\n{section}"


def _with_routing_warnings(proposal: str, memory_contents: dict[str, str]) -> str:
    """Append the deterministic ``## Routing Warnings`` section to a proposal.

    Pure code (section-registry + cross-file dedup, see lib/routing_validation).
    Fail-open + loud: a crash in the gate must never lose a generated proposal —
    log the failure and return the proposal unvalidated. Never rewrites entries;
    the human reviewing via dream-remember stays the gate.
    """
    try:
        from lib.routing_validation import validate_proposal, render_warnings_section

        warnings = validate_proposal(proposal, memory_contents)
        if warnings:
            logger.warning(
                "Routing validation flagged %d issue(s):\n%s",
                len(warnings), "\n".join(f"  - {w}" for w in warnings),
            )
        else:
            logger.info("Routing validation clean — no misroutes or cross-file duplicates")
        return proposal.rstrip() + render_warnings_section(warnings)
    except Exception:
        logger.exception(
            "Routing validation gate failed — proposal written WITHOUT a Routing Warnings section"
        )
        return proposal


# Second pass — bounded surgical critic, expressed as DIRECTIVES rather than a rewrite.
# The critic used to regenerate the entire document to change under 1% of it (measured:
# 40846 -> 41252 bytes at 173-630 s a pass), which is both the slowest step and the one
# most able to lose content it was not asked to touch. Emitting one directive per edit
# makes the cost proportional to the number of edits, and makes every change auditable in
# the log. `lib/proposal_edits.py` parses and applies them in pure code.
_CRITIC_SYSTEM = """\
You are a strict editor doing a SECOND PASS over an already-drafted memory proposal. The
analyst that drafted it generalizes most things well but still (a) leaves point-in-time
residue on some KEEP entries, (b) keeps whole past-event records because they embed a
useful fragment, and (c) — because the proposal was drafted in parallel over separate
slices of the backlog — repeats the same lesson in more than one entry.

You do NOT rewrite the document. You emit EDIT DIRECTIVES, one per line, and nothing else.

## Your five jobs

### 1. Strip residue (every '### N.' entry)

Surgically remove any residual:
- commit hashes / SHAs ("committed as abc1234", "fixed in def5678")
- dated-decision framing ("Decision (2026-06-15):", "as of <date>", "(decided <date>)")
- finished-task imperatives ("update file X", "remove Y now", "... accordingly")
- one-off absolute paths and over-scoped project / repo / file names, UNLESS the lesson is
  genuinely scoped to that project and useless elsewhere
Keep the transferable rule, phrased as guidance. Emit `REPLACE` with the cleaned text.

### 2. Demote past-event records (be bold)

An entry that is fundamentally a record of a PAST EVENT — a dated decision, a completed
checklist/migration/cutover, a "we did/decided/shipped X" status — is DIARY, even when it
embeds a reusable fragment. Do NOT keep the event in order to save the fragment. Instead:
- If a genuine general rule can be lifted out, `REPLACE` the entry's text with that rule
  alone (strip ALL event scaffolding: dates, specific repo/project names, the checklist
  itself, what was done). Example: "Decision: repos A/B/C go public; pre-public: scrub gho_
  token, remove scalestack skill, secret scan" → "Before making any repo public: scrub
  secrets from git history AND rotate them, and strip employer-specific content."
- Otherwise `DROP` it with a one-line reason.
When unsure whether something is a durable rule or a one-time event, treat it as an EVENT:
extract any rule, drop the rest. Memory is guidance that changes future action — not a log
of what happened.

DO keep durable reference facts — how a system is configured, stable identifiers (regions,
instance/secret names, ports), standing preferences. Those are not events; they inform future
work. The target is records of things that HAPPENED or were DECIDED at a point in time.

### 3. Reroute mis-filed action items

If a memory '### N.' entry is really a change-request to the TOOLCHAIN's own code / config /
file-structure ("split these files", "delete this orphan", "this script should also check X"),
the change belongs in '## Action Items' — emit `TO-ACTION`. Do not move general knowledge
that merely mentions the system.

### 4. Fix catch-all mis-routing

The user message includes each memory file's PURPOSE / OWNS DOMAINS / NOT HERE block. If an
entry is filed under a file whose NOT-HERE line names its subject, or under a broadly-named
file when another file's PURPOSE clearly owns the subject, `MOVE` it to the owning file.
Broadly-named files are never fallbacks, and a tool/agent having performed the work does not
make the learning about that tool/agent — route by what the lesson is ABOUT. Only move on a
clear subject mismatch; do not reshuffle borderline entries.

### 5. Merge cross-slice duplicates (new — read this one carefully)

This proposal was assembled from several independently drafted slices of the backlog, so the
SAME lesson learned on two different days can appear as two entries — often under the same
target file, sometimes worded differently. Nothing upstream can catch that. You are the first
and only place it is visible.

When two entries state the same rule, `MERGE` them: keep the better-worded one and absorb
the other. The surviving entry keeps BOTH `**Source:**` lines, so no provenance is lost.
Merge only genuine restatements of one rule — two related rules about the same subject are
two entries, not one.

## Directive grammar — emit ONLY these, one per line

REPLACE <file>#<n> <new text for the entry>
MOVE <file>#<n> -> <other.md>
TO-ACTION <file>#<n>
DROP <file>#<n> <reason>
MERGE <file>#<n> <- <file>#<m>
NOOP

`<file>` is the target file named in the `## Updates for \\`file\\`` heading; `<n>` is the
entry's `### N.` number within that file. For an action item use `ACTIONS` as `<file>` and
its `A{N}` number as `<n>`.

## Hard rules

- Emit directives and NOTHING else. No preamble, no explanation, no markdown fences, no
  restatement of the proposal. A line that is not a directive is discarded.
- If the proposal needs no changes, emit exactly: NOOP
- NEVER touch a `**Source:**` line. It cites `filename:line` for traceability and must stay
  exact; a directive that would edit or remove one is refused. `MERGE`'s append is the one
  sanctioned operation — the surviving entry ends up with both Source lines verbatim.
- One directive per line, one edit per directive. Do not batch several entries into one.
- Do not invent entries, do not reorder, do not renumber, and do not touch entries that are
  already clean.
- Preserve **[RULE-PROPOSAL]** / **[warning low confidence]** markers in any REPLACE text.
"""


def _batch_proposal_for_critic(proposal: str) -> list[str]:
    """Split *proposal* into critic-sized batches, never splitting a ``## `` section.

    The critic is input-bound: it reads a whole proposal and emits only a handful
    of directive lines. That asymmetry is what made the unbatched pass impossible
    — a 283 KB fixture run handed it a 228 KB prompt, which burned both 900 s
    attempts without finishing and silently degraded to "keep the merged draft",
    so the second-pass quality gate never ran at all on exactly the backlogs that
    need it most.

    Sections are kept whole because ``MERGE <file>#<n> <- <file>#<m>`` compares
    two entries of the same file; splitting a file across batches would hide half
    its duplicates from the comparison. A single section larger than the budget
    gets a batch of its own rather than being split.
    """
    from lib import dream_chunking

    budget = dream_chunking.chunk_budget_bytes(
        CHUNK_TIMEOUT_S, _calibrated_throughput()
    )
    # Keep each "## " heading with its body; the split loses the delimiter, so
    # re-attach it. Anything before the first heading is the proposal preamble
    # and rides along with the first batch.
    parts = re.split(r"^(?=## )", proposal, flags=re.M)
    sections = [p for p in parts if p.strip()]

    batches: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for section in sections:
        n = len(section.encode("utf-8"))
        if current and current_bytes + n > budget:
            batches.append("".join(current))
            current, current_bytes = [], 0
        current.append(section)
        current_bytes += n
    if current:
        batches.append("".join(current))
    return batches or [proposal]


async def _critique_batch(client, batch: str, memory_context: str, sem, index: int,
                          total: int) -> str:
    """One critic call over one batch. Returns raw directive text ("" on failure).

    Fails open per batch: a batch that times out loses only its own directives,
    and the other batches' edits still land.
    """
    from lib import dream_chunking

    content = f"## Drafted proposal:\n\n{batch}"
    if memory_context:
        content = (
            f"## Memory file domains (for the mis-routing check):\n\n{memory_context}\n\n"
            + content
        )
    n_bytes = len(batch.encode("utf-8"))
    # An oversized section cannot be split, so give it the same escalation
    # plan_chunks gives an oversized block rather than letting it fail outright.
    budget = dream_chunking.chunk_budget_bytes(CHUNK_TIMEOUT_S, _calibrated_throughput())
    timeout_s = (
        min(2.0 * CHUNK_TIMEOUT_S, dream_chunking.MAX_ESCALATED_TIMEOUT_S)
        if n_bytes > budget
        else CHUNK_TIMEOUT_S
    )

    async with sem:
        started = time.monotonic()
        try:
            response = await _query(
                client, _CRITIC_SYSTEM, [{"role": "user", "content": content}], timeout_s
            )
        except Exception:
            logger.exception(
                "Critic batch %d/%d failed (%d bytes) — its directives are lost, the "
                "other batches still apply", index, total, n_bytes,
            )
            return ""
        elapsed = max(time.monotonic() - started, 1e-6)

    raw = (response.content or "").strip()
    logger.info(
        "Critic batch %d/%d: %d bytes in, %d directive bytes out, %.0fs",
        index, total, n_bytes, len(raw.encode("utf-8")), elapsed,
    )
    return raw


async def _critique_proposal(client, proposal: str, memory_context: str = "",
                             stats: dict | None = None) -> str:
    """Run the directive critic over a drafted proposal; return the edited version.

    ``memory_context`` carries the same PURPOSE / OWNS DOMAINS / NOT HERE file
    blocks the drafting pass saw, so the critic's mis-routing check works from
    the live catalog instead of hardcoded file knowledge.

    The pass runs in batches (see ``_batch_proposal_for_critic``) and applies
    every batch's directives to the **whole** proposal in one go. That is safe
    because a directive addresses ``<file>#<n>`` and ``apply_directives`` sorts
    back-to-front, so an index can never be shifted by another edit.

    Fails **open**, exactly as the rewrite critic did: an unparseable, empty or
    all-NOOP response keeps the merged draft and logs a warning. A
    residue-bearing proposal is still useful; a lost one is not.
    """
    try:
        from lib import proposal_edits

        batches = _batch_proposal_for_critic(proposal)
        if len(batches) > 1:
            logger.info(
                "Critic: %d bytes over %d batches", len(proposal.encode("utf-8")),
                len(batches),
            )
        sem = asyncio.Semaphore(_concurrency())
        raws = await asyncio.gather(*(
            _critique_batch(client, b, memory_context, sem, i, len(batches))
            for i, b in enumerate(batches, 1)
        ))
        if stats is not None:
            stats["batches"] = len(batches)
            stats["failed"] = sum(1 for r in raws if not r)

        directives, rejected = [], []
        for raw in raws:
            if not raw:
                continue
            parsed, bad = proposal_edits.parse_directives(raw)
            directives.extend(parsed)
            rejected.extend(bad)

        if not any(raws):
            logger.warning("Critic returned nothing — keeping the merged draft")
            return proposal
        if rejected:
            logger.info("Critic: %d unparseable line(s) ignored", len(rejected))
        if not directives:
            logger.warning(
                "Critic emitted no usable directives (%d line(s) rejected) — keeping the "
                "merged draft", len(rejected),
            )
            return proposal

        edited, applied, refused = proposal_edits.apply_directives(proposal, directives)
        for description in applied:
            logger.info("Critic applied: %s", description)
        for reason in refused:
            logger.info("Critic refused: %s", reason)
        logger.info(
            "Critic pass: %d applied, %d refused, %d rejected (%d -> %d bytes)",
            len(applied), len(refused), len(rejected),
            len(proposal.encode("utf-8")), len(edited.encode("utf-8")),
        )
        # Same degenerate-output guard as the rewrite critic: never let the pass
        # hand back something that no longer looks like a proposal.
        if "## Updates for" not in edited and "## Filtered Out" not in edited:
            logger.warning("Critic output lost the proposal structure — keeping the draft")
            return proposal
        return edited
    except Exception:
        logger.exception("Critic pass failed — keeping the merged draft")
        return proposal


def _plan_run(learnings_dir: Path) -> tuple[list, list, list[Path], float]:
    """Shared planning for ``--check`` and a real run — no LLM, no lock, no spend.

    Returns ``(pending_blocks, chunks, files, throughput)``. Keeping one
    implementation is the point: ``--check``'s prediction is worthless if it is
    computed differently from what the run actually does.
    """
    from lib import dream_chunking

    blocks, files = _collect_blocks(learnings_dir)
    ledger_path = learnings_ledger.default_ledger_path()
    ledger = learnings_ledger.load(ledger_path)
    pending = learnings_ledger.unprocessed(blocks, ledger)
    throughput = dream_chunking.resolve_throughput(_calibrated_throughput())
    chunks = dream_chunking.plan_chunks(pending, timeout_s=CHUNK_TIMEOUT_S,
                                        throughput=throughput)
    return pending, chunks, files, throughput


async def dream_report() -> None:
    """Generate ONE change proposal and write it to .multiplai/dreams/ for review.

    Order matters and is load-bearing: lock, plan, fold, draft — everything
    before the first token is spent. Folding ahead of drafting is what keeps the
    "no decided items" check honest: the hub writes decisions into a proposal
    item by item, so checking at write time would be a claim that had decayed
    over a multi-minute run.
    """
    if not acquire_run_lock():
        return

    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dreams_dir = paths.dreams_dir()
    ledger_path = learnings_ledger.default_ledger_path()

    pending, chunks, source_files, throughput = _plan_run(learnings_dir)

    # Resume BEFORE pruning: a staged draft is kept only while its blocks are
    # ledgered, and pruning drops keys for learnings files that have since been
    # deleted. Pruning first would discard a perfectly good draft whose source
    # file is gone — the one case where the draft is the only surviving copy.
    run_dir = _runs_dir() / _new_run_id()
    resumed = _resume_staged_drafts(run_dir, learnings_ledger.load(ledger_path))

    # Keys for learnings files that no longer exist are dead weight (--auto and
    # /dream-remember delete files after applying).
    pruned = learnings_ledger.prune(ledger_path, {f.name for f in source_files})
    if pruned:
        logger.info("Ledger: pruned %d key(s) for deleted learnings files", pruned)

    if not pending and not resumed:
        logger.info("No new learnings since the last consolidation — nothing to propose")
        print("No new learnings — nothing to propose.")
        return

    total_bytes = 0
    for f in source_files:
        try:
            total_bytes += len(f.read_text().encode("utf-8"))
        except OSError:
            pass
    new_bytes = sum(len(b.text.encode("utf-8")) for b in pending)
    plan = _plan_line(new_bytes=new_bytes, total_bytes=total_bytes,
                      chunks=chunks, throughput=throughput)
    logger.info("%s", plan)
    print(plan)

    # Fold now, under the lock, before a single token is spent. Stage what was
    # folded straight away: the files have already left the dreams root, so
    # crashing before the write would otherwise lose them entirely.
    folded = _fold_pending_proposals(dreams_dir)
    for i, text in enumerate(folded, start=1):
        try:
            _stage_draft(run_dir, i, text, [], kind="folded")
        except OSError:
            logger.warning("Could not stage folded proposal %d", i, exc_info=True)

    dreams_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = _proposal_output_path(dreams_dir, today)

    # Mirror dream_auto(): a crash inside the SDK call — including an
    # SDK-unavailable RuntimeError from create_client itself — must leave a
    # traceback in dream.log / hook-errors.log, not just the ephemeral task
    # stdout. Re-raise so exit status stays non-zero.
    try:
        client = await create_client(component="dream")
        logger.info("Dream using %s", type(client).__name__)

        memory_contents = _read_memory_files(memory_dir)
        logger.info(
            "Loaded %d memory files for context: %s",
            len(memory_contents), ", ".join(sorted(memory_contents)),
        )
        memory_context = _memory_context(memory_contents)

        drafts = await _draft_chunks(
            client, chunks,
            memory_context=memory_context,
            source_names=", ".join(f.name for f in source_files),
            run_dir=run_dir,
            ledger_path=ledger_path,
            proposal_name=output_file.name,
        )

        merge_input = resumed + drafts + folded
        _enforce_ledger_coverage(ledger_path, run_dir, resumed + drafts)

        from lib import proposal_merge

        merged = proposal_merge.merge_drafts(merge_input)
        logger.info(
            "Merged %d draft(s) (%d fresh, %d resumed, %d folded) into %d bytes",
            len(merge_input), len(drafts), len(resumed), len(folded),
            len(merged.encode("utf-8")),
        )

        critic_stats: dict = {}
        cleaned = await _critique_proposal(client, merged, memory_context, critic_stats)
        # Repair citations before the deterministic sections are attached, so the
        # regex only ever sees model-written provenance and cannot rewrite a
        # citation that this code put there itself.
        sourced = _with_repaired_citations(cleaned, learnings_dir)
        validated = _with_routing_warnings(sourced, memory_contents)
        pending_text = "\n\n".join(b.text for b in pending)
        proposal = _with_conflict_resolutions(validated, pending_text, memory_contents)
    except Exception:
        logger.exception("Dream report generation failed")
        raise

    # Quick structural digest so the log answers "what did the model decide?"
    # without having to open the proposal file.
    proposal_lines = proposal.splitlines()
    target_files = [
        l.split("`")[1] for l in proposal_lines
        if l.startswith("## Updates for `") and "`" in l[16:]
    ]
    has_filtered = any(l.startswith("## Filtered Out") for l in proposal_lines)
    logger.info(
        "Proposal generated: %d bytes, %d target files (%s), filtered-out section=%s",
        len(proposal.encode("utf-8")),
        len(target_files),
        ", ".join(target_files) if target_files else "none",
        has_filtered,
    )
    if not has_filtered or not target_files:
        logger.warning(
            "Proposal looks incomplete — missing target updates or Filtered Out section"
        )

    output_file.write_text(_stamp_generated(proposal))
    logger.info("Proposal written to %s", output_file)

    # Everything staged is now inside a written proposal; keeping it would fold
    # the same drafts in again on the next run.
    _clear_staging()

    # Report what was actually consolidated, not what was planned. A run that
    # loses chunks — to a rate limit, a timeout, an outage — still writes a
    # useful proposal and still exits 0, because the lost blocks stay pending and
    # come back next time. But printing "231 new learning block(s)" when 109 of
    # them were deferred tells the user their backlog is done when it is not.
    # The ledger is the authority on what landed, so ask it rather than the plan.
    deferred = learnings_ledger.unprocessed(
        list(pending), learnings_ledger.load(ledger_path)
    )
    consolidated = len(pending) - len(deferred)
    failed_chunks = len(chunks) - len(drafts)

    print(f"Proposal written to {output_file}")
    if deferred:
        print(
            f"Sources: {len(source_files)} files, {consolidated} of {len(pending)} new "
            f"learning block(s) consolidated"
        )
        print(
            f"  ⚠ {failed_chunks} of {len(chunks)} chunk(s) did not complete — "
            f"{len(deferred)} block(s) stay pending and are picked up by the next run "
            f"(see dream.log)"
        )
    else:
        print(f"Sources: {len(source_files)} files, {len(pending)} new learning block(s)")
    if critic_stats.get("failed"):
        print(
            f"  ⚠ second-pass review incomplete: {critic_stats['failed']} of "
            f"{critic_stats['batches']} batch(es) failed — duplicates and mis-routed "
            f"items may remain"
        )
    if folded:
        print(f"Folded in {len(folded)} undecided pending proposal(s) → dreams/superseded/")
    print(f"Targets: {len(target_files)} files ({', '.join(target_files) or 'none'})")
    print("Review with: /multiplai-context:dream-remember")


# ---------------------------------------------------------------------------
# Auto mode (--auto)
# ---------------------------------------------------------------------------

def _memory_dir_is_git_repo(memory_dir: Path) -> bool:
    if not memory_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _commit_memory_changes(memory_dir: Path) -> bool:
    """Stage and commit memory changes. Returns True on commit, False otherwise."""
    if not _memory_dir_is_git_repo(memory_dir):
        logger.warning(
            "Memory auto-commit skipped — %s is not a git repository.", memory_dir
        )
        return False

    try:
        # Stage only memory markdown files. Staging the whole tree would
        # sweep in unrelated dirty work when memory_dir lives inside a
        # larger repo (dotfiles/workspace) and record it in the snapshot.
        subprocess.run(
            ["git", "-C", str(memory_dir), "add", "--", "*.md"],
            check=True, timeout=15, capture_output=True,
        )
        # Check for staged changes scoped to the *.md pathspec only — otherwise
        # anything the user had pre-staged elsewhere would make this look
        # "dirty" and fire a snapshot that sweeps those unrelated files in.
        diff = subprocess.run(
            ["git", "-C", str(memory_dir), "diff", "--cached", "--quiet", "--", "*.md"],
            timeout=10, capture_output=True,
        )
        if diff.returncode == 0:
            logger.info("Memory auto-commit skipped — no changes to record")
            return False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Restrict the snapshot to the *.md pathspec: passing the pathspec
        # records just those paths and leaves any other staged files untouched.
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit",
             "-m", f"dream: consolidate {today}", "--", "*.md"],
            check=True, timeout=30, capture_output=True,
        )
        logger.info("Memory auto-committed in %s", memory_dir)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Memory auto-commit failed: git %s exited %d (stderr: %s)",
            e.cmd, e.returncode, e.stderr.decode("utf-8", "replace") if e.stderr else "",
        )
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        logger.warning("Memory auto-commit failed: %s", e)
        return False


# Mechanical applier — executes an already-generalized proposal. It does NOT decide
# what is or isn't a memory; all that judgment lives in _PROPOSAL_SYSTEM. This keeps
# --auto using the exact same generalization brain as report mode (just no human gate).
_APPLIER_SYSTEM = (
    "You apply an approved set of memory updates to a memory file. Make ONLY the "
    "changes the proposal specifies (add / update / replace at the named sections). "
    "Match the file's existing style and formatting exactly. Do not generalize, "
    "re-judge, invent, or add anything not in the proposal. If a 'Last Updated' line "
    "exists, refresh its date. Return the full updated file content and nothing else."
)


def _split_proposal_by_file(proposal: str) -> dict[str, str]:
    """Split a proposal into {filename: section_text} by '## Updates for `file`' headers.

    'Filtered Out' and any preamble are not target sections and are dropped — only the
    per-file update blocks become applier instructions.
    """
    sections: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []

    def _flush():
        if current_file is not None:
            sections[current_file] = "\n".join(buf).strip()

    for line in proposal.splitlines():
        if line.startswith("## Updates for `") and "`" in line[16:]:
            _flush()
            current_file = line.split("`")[1]
            buf = [line]
        elif line.startswith("## "):
            # any other H2 (e.g. "## Filtered Out") ends the current file section
            _flush()
            current_file = None
            buf = []
        elif current_file is not None:
            buf.append(line)
    _flush()
    return sections


def _is_safe_memory_update(current: str, new: str) -> bool:
    """Guard against an applier response that would destroy a memory file.

    A consolidation rewrites the file in full, so a truncated response or a
    refusal preamble ("I'm sorry, I can't…") would silently overwrite good
    content with garbage. Consolidation only adds or lightly edits, so the
    result should never collapse to a fraction of the original. Reject an
    empty/whitespace result or one that lost more than 40% of the original
    length — the caller then keeps the existing file and the learnings so the
    run can be retried.
    """
    stripped = new.strip()
    if not stripped:
        return False
    # A memory file is prose+markdown; a bare apology/refusal is not a valid
    # rewrite. Cheap heuristic on the opening.
    head = stripped[:80].lower()
    if head.startswith(("i'm sorry", "i am sorry", "i cannot", "i can't", "sorry,")):
        return False
    if len(current.strip()) >= 200 and len(stripped) < 0.6 * len(current.strip()):
        return False
    return True


async def _apply_proposal_to_file(client, memory_file: Path, proposal_section: str) -> str | None:
    """Apply one file's slice of the proposal. Returns validated new content,
    or None if the call failed or the result looks unsafe to write."""
    if not memory_file.exists():
        return None

    current_content = memory_file.read_text()
    messages = [
        {
            "role": "user",
            "content": (
                f"## Approved updates for {memory_file.name}:\n{proposal_section}\n\n"
                f"## Current file content:\n{current_content}"
            ),
        }
    ]

    # The applier rewrites the whole file, so its cost scales with section +
    # current file, not with the section alone. A single call cannot be left on
    # the default deadline: a 283 KB fixture run produced sections of 41 KB
    # (`claude-code-tools.md`), 38 KB (`multiplai.md`), 33 KB (`dolcebot.md`) and
    # 27 KB (`writing-workflow.md`), and at the slow tail that run measured
    # (25 B/s) every one of those needs more than 900 s. Size the deadline to the
    # work and escalate, exactly as `plan_chunks` does for an oversized block.
    from lib import dream_chunking

    n_bytes = len(proposal_section.encode("utf-8")) + len(current_content.encode("utf-8"))
    timeout_s = min(
        dream_chunking.MAX_ESCALATED_TIMEOUT_S,
        max(CHUNK_TIMEOUT_S, dream_chunking.estimate_seconds(n_bytes, _calibrated_throughput())),
    )

    try:
        response = await _query(client, _APPLIER_SYSTEM, messages, timeout_s)
    except Exception:
        logger.exception(
            "Failed to apply updates to %s (%d bytes, %.0fs deadline)",
            memory_file.name, n_bytes, timeout_s,
        )
        return None

    if not _is_safe_memory_update(current_content, response.content):
        logger.error(
            "Rejected unsafe applier output for %s (%d chars -> %d); keeping original",
            memory_file.name, len(current_content), len(response.content.strip()),
        )
        return None
    return response.content


async def dream_auto() -> None:
    """Apply learnings directly to memory files without review (autonomous mode).

    Takes the same exclusive run lock as report mode — two concurrent runs
    writing the same memory files is strictly worse than two writing proposals.
    The applier stage itself is unchanged; it shares ``_generate_proposal()``.
    """
    if not acquire_run_lock():
        return

    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dream_state_file = paths.dream_state_file()

    all_learnings, source_files = _read_all_learnings(learnings_dir)
    if not all_learnings:
        logger.info("No pending learnings to consolidate")
    else:
        try:
            client = await create_client(component="dream")
            logger.info("Dream (auto) using %s", type(client).__name__)

            # Stage 1 — generalize. IDENTICAL to report mode: same _PROPOSAL_SYSTEM,
            # same call. All the diary-vs-memory judgment happens here. The only
            # difference from report mode is that we apply the result instead of
            # waiting for /dream-remember approval.
            memory_contents = _read_memory_files(memory_dir)
            proposal = await _generate_proposal(
                client, all_learnings, memory_contents, source_files
            )

            # Audit trail: write the same proposal artifact report mode would,
            # without clobbering a prior same-day artifact.
            dreams_dir = paths.dreams_dir()
            dreams_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            proposal_file = _proposal_output_path(dreams_dir, today)
            proposal_file.write_text(proposal)

            # Stage 2 — mechanically apply each file's slice of the proposal.
            # Files are independent, so apply them concurrently.
            per_file = _split_proposal_by_file(proposal)
            logger.info("Dream (auto) proposal targets %d files: %s",
                        len(per_file), ", ".join(sorted(per_file)) or "none")

            targets = []
            for filename, section in per_file.items():
                memory_file = memory_dir / filename
                if not memory_file.exists():
                    logger.warning("Proposal targets unknown file %s — skipped", filename)
                    continue
                targets.append((filename, memory_file, section))

            results = await asyncio.gather(*(
                _apply_proposal_to_file(client, mf, section)
                for _, mf, section in targets
            ))

            updated_count = 0
            failed_count = 0
            for (filename, memory_file, _), updated_content in zip(targets, results):
                if updated_content:
                    memory_file.write_text(updated_content)
                    updated_count += 1
                    logger.info("Applied updates to %s", filename)
                else:
                    failed_count += 1

            # Only delete the raw learnings once every target that was supposed
            # to change actually did. If any apply failed (API outage, unsafe
            # output), keep the backlog so the next run can retry — deleting it
            # here would lose the source insights with nothing persisted.
            if failed_count == 0:
                for f in source_files:
                    f.unlink(missing_ok=True)
                    logger.info("Deleted processed learnings: %s", f.name)
                # Fully applied → the audit artifact is no longer pending;
                # archive it so the dreams root holds only pending proposals
                # (dream-remember Step 1 must never re-present it). On any
                # failure it stays put alongside the kept learnings, as a
                # recovery path for a human review.
                try:
                    archived = _archive_proposal(proposal_file, dreams_dir)
                    logger.info("Archived auto-applied proposal to %s", archived)
                except OSError:
                    logger.exception("Could not archive %s — left in dreams root", proposal_file)
            else:
                logger.warning(
                    "Kept %d learnings file(s): %d/%d targets failed to apply — "
                    "will retry next run",
                    len(source_files), failed_count, len(targets),
                )

            state = load_yaml(dream_state_file)
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            state["learnings_processed"] = sum(1 for _ in all_learnings.splitlines())
            state["files_updated"] = updated_count
            save_yaml(dream_state_file, state)

            logger.info("Dream (auto) complete: %d files updated", updated_count)
        except Exception:
            logger.exception("Dream (auto) consolidation failed")
            raise

    try:
        config = load_catalog_config()
        catalog_results = await generate_catalogs(config=config)
        logger.info("Catalog regeneration complete: %d generators ran", len(catalog_results))
    except Exception:
        logger.exception("Catalog generation failed (dream still complete)")

    _commit_memory_changes(memory_dir)


# ---------------------------------------------------------------------------
# Learnings garbage collection (--gc-learnings)
# ---------------------------------------------------------------------------


def _gc_learnings() -> None:
    """Delete learnings files that are fully consolidated **and** fully decided.

    This replaces a judgement call the reviewing skill used to make in prose
    ("delete the sources, but only if the proposal is now fully decided, else
    skip this step"). That conditional was easy to get wrong and unrecoverable
    when it was, so the decision moves into code, per file, with a stated reason
    for everything kept.

    A file is deleted only when **all** of these hold:

    (a) every ``## Session Learnings`` record in it hashes to a key the ledger
        has recorded — i.e. dream has already consolidated all of it. A file
        appended to since the run (today's, typically) fails this and is kept,
        which is why the old "today's file exception" no longer needs writing
        down;
    (b) no proposal those keys were recorded into is still sitting in the
        dreams root awaiting review;
    (c) every one of those proposals is *present* in ``applied/``,
        ``rejected/`` or ``superseded/``. Note this is positive evidence, not
        (b)'s absence restated. A proposal's name enters the ledger chunk by
        chunk as a run proceeds, but the file itself is only written when the
        run completes — so during a long run, and permanently after a crashed
        one, a fully-ledgered name exists in no directory at all. Absence used
        to read as "decided", which meant gc deleted the inputs of the very run
        that was still consuming them;
    (d) no proposal still in the dreams root cites the file. The ledger names
        the proposal a block was *first* consolidated into, and a fold-forward
        (``_fold_pending_proposals``) moves items into a successor and archives
        the original to ``superseded/`` without re-pointing those entries — so
        by the ledger alone the sources behind a live, unreviewed proposal look
        decided. The citations travel with the items, so reading them is what
        stays true.

    (b)-(d) all serve one property: a pending review must remain readable. Its
    ``**Source:** <file>:<line>`` citations have to resolve while the reviewer
    is still deciding.

    Anything else is kept, with the reason printed per file. Deliberately
    conservative in every direction — an unreadable pending proposal or an
    unreadable learnings file stops deletion rather than licensing it, because
    the failure being guarded against is unrecoverable and the cost of keeping
    a file too long is a few kilobytes.

    Takes no lock, and does not need one: after (c) and (d) it can no longer
    delete anything a consolidation run is mid-way through. (The previous
    version of this docstring claimed the same immunity while (c) was missing,
    which was simply untrue.)
    """
    paths = get_paths()
    learnings_dir = paths.learnings_dir
    files = sorted(learnings_dir.glob("*.md")) if learnings_dir.exists() else []
    if not files:
        print("GC learnings: no learnings files")
        return

    ledger_path = learnings_ledger.default_ledger_path()
    recorded = learnings_ledger.load(ledger_path).get("processed", {})
    dreams_dir = paths.dreams_dir()
    # Non-recursive on purpose: applied/, rejected/ and superseded/ are decided,
    # and the hub's own listing globs the root the same way.
    pending_paths = sorted(dreams_dir.glob("*.md")) if dreams_dir.exists() else []
    pending = {p.name for p in pending_paths}

    # Positive evidence of a decision, rather than inferring it from absence.
    # A proposal name is ledgered per chunk as the run goes, but the file is
    # only written when the run finishes — so between those two moments, and
    # forever after a crash, a fully-ledgered name exists in no directory at
    # all. Reading that gap as "decided" is what let gc delete a live run's
    # own inputs.
    decided: set[str] = set()
    for disposition in ("applied", "rejected", "superseded"):
        sub = dreams_dir / disposition
        if sub.exists():
            decided |= {p.name for p in sub.glob("*.md")}

    # What the pending proposals actually cite, which is the only thing that
    # stays true across a fold-forward: `_fold_pending_proposals` moves items
    # (citations and all) into a successor and archives the original to
    # superseded/, but never re-points the ledger at the successor. Going by
    # the ledger alone, those sources look decided while a reviewer is still
    # reading them.
    still_cited: set[str] = set()
    for p in pending_paths:
        try:
            still_cited |= citation_repair.cited_files(p.read_text())
        except OSError as exc:
            # Fail closed. An unreadable pending proposal is the one case where
            # we cannot know what it cites, so nothing may be deleted on the
            # strength of not having seen a citation in it.
            logger.warning("Pending proposal %s unreadable (%s) — keeping every "
                           "learnings file this pass", p.name, exc.__class__.__name__)
            print(f"GC learnings: {p.name} unreadable — nothing deleted this pass")
            return

    deleted: list[str] = []
    kept: list[tuple[str, str]] = []

    for f in files:
        try:
            text = f.read_text()
        except OSError as exc:
            kept.append((f.name, f"unreadable ({exc.__class__.__name__})"))
            continue
        blocks = learnings_ledger.parse_blocks(f.name, text)
        if not blocks:
            kept.append((f.name, "no `## Session Learnings` records to consolidate"))
            continue
        missing = sum(1 for b in blocks if b.key not in recorded)
        if missing:
            kept.append((f.name, f"{missing}/{len(blocks)} record(s) not yet consolidated"))
            continue
        proposals = {recorded[b.key].get("proposal") or "" for b in blocks}
        if "" in proposals:
            kept.append((f.name, "ledger entry names no proposal"))
            continue
        undecided = sorted(n for n in proposals if Path(n).name in pending)
        if undecided:
            kept.append((f.name, "proposal still pending review: " + ", ".join(undecided)))
            continue
        unwritten = sorted(n for n in proposals if Path(n).name not in decided)
        if unwritten:
            kept.append((f.name, "proposal not yet written — a run may be in "
                                 "flight or have crashed: " + ", ".join(unwritten)))
            continue
        if f.name in still_cited:
            kept.append((f.name, "still cited by a pending proposal"))
            continue
        try:
            f.unlink()
        except OSError as exc:
            kept.append((f.name, f"could not delete ({exc.__class__.__name__})"))
            continue
        deleted.append(f.name)

    if deleted:
        remaining = {p.name for p in learnings_dir.glob("*.md")}
        learnings_ledger.prune(ledger_path, remaining)

    print(f"GC learnings: deleted {len(deleted)}, kept {len(kept)}")
    for name in deleted:
        print(f"  deleted  {name}")
    for name, reason in kept:
        print(f"  kept     {name} — {reason}")
    logger.info("gc-learnings: deleted=%d kept=%d", len(deleted), len(kept))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_decisions(source: str) -> list[Decision]:
    """Parse a JSON array of decisions from stdin (``-``) or a file.

    stdin is the shape the reviewing skill uses: a 70-item review would not fit
    argv, and a heredoc needs no shell quoting rules written into a SKILL.md.
    """
    import json

    raw = sys.stdin.read() if source == "-" else Path(source).read_text()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"--decisions must be a JSON array, got {type(data).__name__}")
    return [Decision.from_dict(item) for item in data]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dream — learnings consolidation")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report the pending-learnings count, the chunk plan and the predicted "
             "wall clock, then exit. Takes no run lock, so it answers while a "
             "consolidation is in progress.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Apply changes directly to memory files without review (autonomous mode)",
    )
    # --run kept as deprecated alias for --auto
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="Record that a consolidation was applied (updates dream_state). "
             "Used by /dream-remember after the human-in-the-loop apply so the "
             "dream gate stops nudging.",
    )
    parser.add_argument("--files-updated", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--learnings-processed", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--archive",
        metavar="PROPOSAL_PATH",
        help="With --stamp: move the reviewed proposal file out of the dreams "
             "root into applied/ (or rejected/, per --archive-as), so the root "
             "holds only pending proposals. Collision-safe.",
    )
    parser.add_argument(
        "--archive-as",
        choices=("applied", "rejected"),
        default="applied",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mark-processed",
        action="store_true",
        help="Move decided proposal items into the proposal's `## Processed` "
             "section — the in-file decision record the multiplai-gui hub writes "
             "too (natively; it does not call this script for it). Batch form: "
             "--proposal PATH --decisions - with a JSON array on stdin, which is "
             "what /dream-remember uses, one call per target file. Single-item "
             "form: --proposal, --kind, --index, --status (and --file for updates).",
    )
    parser.add_argument("--proposal", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument(
        "--decisions",
        metavar="PATH_OR_DASH",
        help="With --mark-processed: read a JSON array of decisions from this "
             "file, or from stdin when given `-`. Each element is "
             '{"kind","file","index","status","target"} — kind update|action, '
             "status applied|edited|rejected, file names the `## Updates for` "
             "group, target the memory file actually written.",
    )
    parser.add_argument(
        "--kind", choices=("update", "action"), default="update", help=argparse.SUPPRESS
    )
    parser.add_argument("--index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--file", metavar="TARGET.md", help=argparse.SUPPRESS)
    parser.add_argument(
        "--status", choices=("applied", "edited", "rejected"), help=argparse.SUPPRESS
    )
    parser.add_argument("--target", metavar="TARGET.md", help=argparse.SUPPRESS)
    parser.add_argument(
        "--gc-learnings",
        action="store_true",
        help="Delete learnings files that are fully consolidated (every record "
             "in the ledger) and fully decided (no proposal citing them is still "
             "pending). Pure code, no model call, no lock. Prints what it removed "
             "and why it kept the rest.",
    )
    args = parser.parse_args()

    if args.mark_processed and args.decisions:
        if not args.proposal:
            print("ERROR: --mark-processed --decisions needs --proposal")
            sys.exit(2)
        proposal_path = Path(args.proposal)
        if not proposal_path.is_file():
            print(f"ERROR: --proposal path not found: {proposal_path}")
            sys.exit(1)
        try:
            decisions = _load_decisions(args.decisions)
        except (OSError, ValueError) as exc:
            print(f"ERROR: --decisions could not be read: {exc}")
            sys.exit(2)
        if not decisions:
            print("marked 0 processed, 0 unchanged")
            return
        try:
            marked, unchanged = mark_many_processed(proposal_path, decisions)
        except OSError as exc:
            # The new document is built in memory and swapped in atomically, so
            # a write failure here leaves the proposal exactly as it was.
            print(f"ERROR: could not write {proposal_path}: {exc} — proposal unchanged")
            sys.exit(1)
        print(f"marked {marked} processed, {unchanged} unchanged")
        return

    if args.mark_processed:
        if not args.proposal or args.index is None or not args.status:
            print("ERROR: --mark-processed needs --proposal, --index and --status")
            sys.exit(2)
        proposal_path = Path(args.proposal)
        if not proposal_path.is_file():
            print(f"ERROR: --proposal path not found: {proposal_path}")
            sys.exit(1)
        if args.kind == "update":
            if not args.file:
                print("ERROR: --mark-processed --kind update needs --file")
                sys.exit(2)
            ref: tuple = ("update", args.file, args.index)
            label = f"update {args.file}#{args.index}"
        else:
            ref = ("action", args.index)
            label = f"action A{args.index}"
        changed = mark_processed(proposal_path, ref, args.status, target=args.target)
        if changed:
            print(f"Marked {label} as {args.status} in {proposal_path.name}")
        else:
            print(f"No change: {label} not pending (already processed or not found)")
        return

    if args.stamp:
        paths = get_paths()
        dream_state_file = paths.dream_state_file()
        state = load_yaml(dream_state_file) or {}
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["files_updated"] = args.files_updated
        state["learnings_processed"] = args.learnings_processed
        save_yaml(dream_state_file, state)
        print(f"Stamped dream_state: last_run={state['last_run']}")
        if args.archive:
            proposal_path = Path(args.archive)
            if not proposal_path.is_file():
                print(f"ERROR: --archive path not found: {proposal_path}")
                sys.exit(1)
            # Backstop: a proposal is archivable only once every item is decided
            # (moved under ## Processed). Undecided items outside that section
            # would be silently discarded by the move — leave it pending instead.
            if has_pending_items(proposal_path.read_text()):
                print(
                    f"ERROR: {proposal_path.name} still has pending items (not under "
                    "## Processed) — left pending, not archived. Decide or move them first."
                )
                sys.exit(3)
            archived = _archive_proposal(
                proposal_path, paths.dreams_dir(), args.archive_as
            )
            print(f"Archived proposal to {archived}")
        return

    if args.gc_learnings:
        _gc_learnings()
        return

    if args.check:
        # Deliberately takes NO lock: the plan is read-only, and its whole point
        # is to be answerable while a run is in progress.
        from lib import dream_chunking

        paths = get_paths()
        pending, chunks, files, throughput = _plan_run(paths.learnings_dir)
        if not files:
            print("No pending learnings")
            return
        if not pending:
            print(f"No new learnings ({len(files)} file(s), all blocks already consolidated)")
            return
        new_bytes = sum(len(b.text.encode("utf-8")) for b in pending)
        total_bytes = 0
        for f in files:
            try:
                total_bytes += len(f.read_text().encode("utf-8"))
            except OSError:
                pass
        # Wall clock, not serial time: chunks run `concurrency` at a time, so a
        # wave costs its slowest chunk rather than the sum of its chunks.
        eta = dream_chunking.estimate_wall_clock(chunks, _concurrency(), throughput)
        print(
            f"Pending learnings: {len(files)} file(s), {len(pending)} new block(s), "
            f"{new_bytes} new bytes of {total_bytes}"
        )
        print(_plan_line(new_bytes=new_bytes, total_bytes=total_bytes,
                         chunks=chunks, throughput=throughput))
        # A floor, not an estimate — see estimate_wall_clock. Saying "at least"
        # is the difference between under-promising and being wrong.
        print(f"Estimated wall clock: at least {eta / 60:.0f} min")
        return

    if args.auto or args.run:
        asyncio.run(dream_auto())
    else:
        asyncio.run(dream_report())


if __name__ == "__main__":
    main()
