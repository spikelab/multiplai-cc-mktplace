"""Session start hook for multiplai plugin.

Logs client selection, records the session start timestamp, initializes
session state, and drains deferred extraction markers. Routed *memory*
injection is handled per-prompt by context_manager.py (UserPromptSubmit);
this hook deliberately does NOT dump memory into the session context.

It DOES inject the per-project "now" snapshot once, here at session start:
the session's ``cwd`` is resolved to a project (lib.project_identity) and the
matching ``now/<project>.md`` is emitted as additionalContext so the session
opens knowing where that project left off. This is one-time on purpose —
re-injecting project status on every prompt (the old behavior) was wasteful
and added no signal.

Also checks the Dream 24h gate: when more than 24 hours have
elapsed since the last dream run and fresh learnings are pending,
emits a system nudge so the user is prompted to run ``/multiplai-context:dream``
instead of the consolidation silently falling out of rhythm.

Similarly checks the config-audit 60-day gate: when more than 60 days have
elapsed since the last subtractive config/rules review, emits a nudge to run
``/multiplai-context:config-audit``. The state file (``config_audit_state.yaml``,
beside the dream state) is stamped deterministically by that skill via
``scripts/config_audit.py --stamp``; when it is missing entirely (fresh
install) this hook seeds it with ``last_run: now`` instead of nudging, so
the 60-day clock starts at install.
"""

import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.config import load_yaml, save_yaml, read_session_state, write_session_state
from multiplai_core.log_utils import hook_run, setup_logging, log_event

# The drain itself lives in lib/ so the host-side ``drain_extractions.py``
# entry point — which runs after the container has exited, when a marker was
# just written and no session will open for days — dequeues through exactly
# the same code. Two copies of a marker-move loop is how one of them quietly
# stops matching the other.
from lib.extraction_drain import process_deferred_extractions
from lib.hook_input import read_hook_input
from lib.runtime import uv_run_argv
from lib.fleet import roster_dead_sids, write_fleet_view

logger = setup_logging("session_start")

_DREAM_GATE_HOURS = 24

# The config-audit cadence is long because config decay is slow and the audit
# is a heavyweight review. Tightened 90 → 60 days (2026-07): the binding
# constraint turned out not to be config drift but *model* drift. Model
# releases now land well inside a quarter, and each one can make prompt
# scaffolding redundant — capabilities that needed spelling out get absorbed by
# the next model. A 90-day gate meant a whole release cycle could pass with
# skills still carrying instructions the model had outgrown.
_CONFIG_AUDIT_GATE_DAYS = 60

# Mirrors ``memory_maintainer.GATE_HOURS`` / ``STATE_FILENAME``. Restated here
# so the pre-spawn gate check needs no import of that PEP 723 script; see
# ``_maintainer_gate_open`` for why the duplication is deliberate.
_MAINTAINER_GATE_HOURS = 24
_MAINTAINER_STATE_FILENAME = "maintainer_state.yaml"

# Deferred-extraction retry policy now lives with the drain itself:
# lib.extraction_drain.STALE_SECONDS / MAX_ATTEMPTS.


def _log_client_selection() -> str:
    """Log which model client is available for this session.

    Uses the model_client module's detect_client_type() to determine
    which backend will be used (AgentSDK vs API key fallback).
    """
    from multiplai_core.model_client import detect_client_type
    client_type = detect_client_type()
    logger.info("Model client selected: %s", client_type)
    return client_type


def _last_run_from(state_file: Path) -> datetime | None:
    """The aware ``last_run`` timestamp in *state_file*, or None.

    The one implementation of "load YAML → read last_run → parse" that four
    gates used to restate separately. None means "no usable record" —
    missing file, unreadable YAML, absent or garbage timestamp — which every
    caller treats as gate-open (first run or recovery): the failure mode of
    an extra pass is small, of a wedged gate is a job that silently never
    runs again.
    """
    try:
        state = load_yaml(state_file) or {}
    except Exception:
        logger.warning("Could not read state %s; treating as no record", state_file)
        return None
    last_run = state.get("last_run")
    if not last_run:
        return None
    try:
        last_dt = datetime.fromisoformat(str(last_run))
    except (ValueError, TypeError):
        return None
    return last_dt if last_dt.tzinfo else last_dt.replace(tzinfo=timezone.utc)


def _gate_open(state_file: Path, delta: timedelta) -> bool:
    """True when *state_file*'s ``last_run`` is missing/unusable or older
    than *delta* — the shared shape of every last-run gate here."""
    last_dt = _last_run_from(state_file)
    if last_dt is None:
        return True
    return datetime.now(timezone.utc) - last_dt >= delta


def _dream_gate_open(dream_state_file: Path) -> bool:
    """Return True when >=24h have passed since the last dream run.

    Missing state or an unparseable timestamp is treated as gate-open
    (first run or recovery) — the user can always recover by running
    ``/multiplai-context:dream`` manually.
    """
    return _gate_open(dream_state_file, timedelta(hours=_DREAM_GATE_HOURS))


def _config_audit_gate_open(config_audit_state_file: Path) -> bool:
    """Return True when >=60 days have passed since the last config audit.

    First run (no state file at all): the gate stays CLOSED. There is no
    record to be stale — nudging "the config audit is due" on every fresh
    install would be false and noisy. Instead the file is seeded with
    ``last_run: now`` so the 60-day clock starts at install and the first
    nudge arrives when the cadence is genuinely due. Seeding is
    best-effort: if the write fails the gate still stays closed (a
    filesystem hiccup must not turn into a false nudge) and seeding is
    retried next session start.

    A state file that EXISTS but yields no usable timestamp (corrupt YAML,
    missing or garbage ``last_run``) keeps the dream gate's fail-open
    recovery semantics: a record existed and was lost, so the gate opens
    and the user re-stamps by running ``/multiplai-context:config-audit``
    (which stamps via ``config_audit.py --stamp``). Deliberately NOT
    re-seeded — silently restarting the clock could hide a genuinely
    overdue audit.
    """
    if not config_audit_state_file.exists():
        try:
            save_yaml(
                config_audit_state_file,
                {"last_run": datetime.now(timezone.utc).isoformat()},
            )
            logger.info(
                "Seeded config-audit state %s (first run — 60-day clock starts now)",
                config_audit_state_file,
            )
        except Exception:
            logger.warning(
                "Could not seed config-audit state %s; will retry next session",
                config_audit_state_file,
            )
        return False

    return _gate_open(
        config_audit_state_file, timedelta(days=_CONFIG_AUDIT_GATE_DAYS)
    )


def _learnings_pending(learnings_dir: Path, dream_state_file: Path) -> bool:
    """Return True if any learnings file has content newer than the last dream run.

    Learnings are stored per-day (``learnings_dir/YYYY-MM-DD.md``), so checking
    only today's file misses a multi-day backlog that accrued while dream
    wasn't run. Scan every non-empty ``*.md`` in the directory.
    """
    if not learnings_dir.exists():
        return False
    # Per-file stat guard: a learnings file can vanish between the glob and
    # the stat (a concurrent dream-remember cleanup), and one OSError must
    # not kill the whole gate (M6).
    mtimes: list[float] = []
    for f in learnings_dir.glob("*.md"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size > 0:
            mtimes.append(st.st_mtime)
    if not mtimes:
        return False

    last_dt = _last_run_from(dream_state_file)
    if last_dt is None:
        return True

    newest = datetime.fromtimestamp(max(mtimes), tz=timezone.utc)
    return newest > last_dt




def _emit_no_client_warning(data_dir: Path) -> None:
    """Surface a one-time user-visible warning when no model client is available.

    Without either claude-agent-sdk or anthropic_api_key, all LLM-backed
    features (extraction, dreams, catalog generation) silently no-op. We
    warn once per install (marker file) so the user knows to run setup;
    repeating it every session would be noise.
    """
    marker = data_dir / "no_client_warning_emitted"
    if marker.exists():
        return
    print(
        "[multiplai] No Anthropic API key configured and no Agent SDK "
        "detected — extraction, dreams, and catalog generation will be "
        "skipped. Run /multiplai-context:setup or set anthropic_api_key in plugin "
        "config to enable them."
    )
    try:
        marker.touch()
    except OSError:
        pass


def _inject_project_state(now_dir: Path, cwd: str) -> bool:
    """Emit the matching project's ``now`` snapshot as additionalContext.

    Resolves *cwd* to a project via the shared resolver and, if
    ``now/<project>.md`` exists, prints it once so the session opens with
    that project's status. Returns True when something was injected.
    Best-effort: a missing file or any error is swallowed — project state
    is a nicety, never a reason to fail session start.
    """
    if not cwd:
        return False
    try:
        from lib.project_identity import resolve_project

        project = resolve_project(cwd)
        if not project:
            return False
        project_file = now_dir / f"{project}.md"
        if not project_file.exists():
            return False
        content = project_file.read_text(encoding="utf-8").strip()
        if not content:
            return False
        print(f"\n--- PROJECT STATE ---\n{content}")
        logger.info("Injected project state for %s", project)
        return True
    except Exception:
        logger.warning("Could not inject project state (cwd=%s)", cwd, exc_info=True)
        return False


def _spawn_detached(script: Path, *args: str) -> None:
    """Launch *script* detached under the plugin's uv project, fire-and-forget.

    The one spawn shape all four session-start children share: detached
    (``start_new_session`` — a SessionStart hook is kill-within-seconds and
    these children take minutes), all three stdio streams closed off (they
    are unattended; a child that blocks on stdin or fills a pipe would hang
    invisibly). Callers own their gating and their logging.
    """
    subprocess.Popen(
        uv_run_argv(script, *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _launch_qmd_refresh(scripts_dir: Path, cwd: str) -> bool:
    """Fire the incremental qmd index refresh, detached, when resources
    retrieval is configured.

    The refresh child (scripts/qmd_refresh.py) is flock-guarded per
    workspace and fully incremental, so launching it on every session
    start is cheap. Detached (start_new_session) because embedding can
    take minutes — a SessionStart hook is kill-within-seconds. Returns
    True when a child was launched. Best-effort: any failure is logged
    and swallowed.
    """
    try:
        from generators.config import load_catalog_config

        cfg = load_catalog_config()
        if not (cfg.enable_resources and cfg.resources_dir.strip()):
            return False
        script = scripts_dir / "qmd_refresh.py"
        if not script.exists():
            return False
        workspace = cwd or str(Path(cfg.resources_dir).expanduser().parent)
        _spawn_detached(script, workspace)
        logger.info("Launched detached qmd refresh (workspace=%s)", workspace)
        return True
    except Exception:
        logger.warning("Could not launch qmd refresh", exc_info=True)
        return False


def _launch_cost_collection(scripts_dir: Path) -> bool:
    """Fire the incremental cost collector, detached, when enabled.

    The collector (scripts/collect_costs.py) prices the session-transcript
    corpus into the monthly cost ledger. It is offset-checkpointed and
    dedups against the ledger, so steady-state passes read only new bytes;
    the first pass over a large corpus is a full backfill (minutes), which
    is why it runs detached (start_new_session) rather than inline in a
    kill-within-seconds hook. It self-guards with an flock, so a second
    launch while one is running is a harmless no-op. Gated on enable_costs.
    Returns True when a child was launched. Best-effort: any failure is
    logged and swallowed — cost accounting must never block session start.
    """
    try:
        from generators.config import load_catalog_config

        cfg = load_catalog_config()
        if not cfg.enable_costs:
            return False
        script = scripts_dir / "collect_costs.py"
        if not script.exists():
            return False
        _spawn_detached(script)
        logger.info("Launched detached cost collection")
        return True
    except Exception:
        logger.warning("Could not launch cost collection", exc_info=True)
        return False


def _maintainer_gate_open(maintainer_state_file: Path) -> bool:
    """True when >=24h have passed since the last maintenance run.

    Deliberately duplicates the maintainer's own ``gate_open`` rather than
    importing it: ``memory_maintainer.py`` is an entry point with its own
    argparse/asyncio setup, and importing it here to read one timestamp would
    drag all of that into the hook process. The gate is a timestamp comparison
    — cheap to state twice, and the child re-checks authoritatively anyway, so
    a disagreement costs at most one no-op child.
    """
    return _gate_open(
        maintainer_state_file, timedelta(hours=_MAINTAINER_GATE_HOURS)
    )


def _launch_bank_sync(scripts_dir: Path) -> bool:
    """Fast-forward subscribed shared banks, detached — if any exist.

    A bank is a git remote, so a sync is a network call of unbounded duration,
    which a kill-within-seconds hook cannot host. It runs detached and
    TTL-gated (the child owns the authoritative TTL); a failure inside the
    child leaves the previously synced content in place and logs, so a bank
    that cannot be reached is stale-but-working and never a session-start
    error. No shared banks configured means no child is spawned at all.
    """
    try:
        from lib.banks import shared_banks

        if not shared_banks():
            return False
        script = scripts_dir / "memory_bank.py"
        if not script.exists():
            return False
        _spawn_detached(script, "sync", "--quiet")
        logger.info("Launched detached memory-bank sync")
        return True
    except Exception:
        logger.warning("Could not launch memory bank sync", exc_info=True)
        return False


def _launch_maintainer(scripts_dir: Path, data_dir: Path) -> bool:
    """Fire the proactive memory maintainer, detached — if its gate is open.

    Same fire-and-forget shape as cost collection: the maintainer's passes
    include two model calls and a subprocess, none of which may run inside a
    kill-within-seconds hook.

    The 24h gate is checked HERE, in-process, before spawning. The child owns
    the authoritative check, but reaching it costs a `uv run` startup — and
    since the maintainer's PEP 723 header declares a git dependency, a cold uv
    cache makes that a network fetch at session start. Paying it to accomplish
    nothing ~95% of sessions is the whole reason this pre-check exists.
    Best-effort: maintenance must never block a session from starting.
    """
    try:
        if not _maintainer_gate_open(data_dir / _MAINTAINER_STATE_FILENAME):
            logger.debug("Maintainer gate closed (<24h); not spawning")
            return False
        script = scripts_dir / "memory_maintainer.py"
        if not script.exists():
            return False
        _spawn_detached(script)
        return True
    except Exception:
        logger.warning("Could not launch memory maintainer", exc_info=True)
        return False


def _emit_dream_nudge() -> None:
    """Print an additionalContext nudge prompting the user to run /multiplai-context:dream."""
    print(
        "\n--- SYSTEM NUDGE ---\n"
        "Dream gate is open (>24h since last consolidation) with "
        "unprocessed learnings on disk. Surface this to the user at the next "
        "natural stopping point: 'Dream reports look due — worth running "
        "/multiplai-context:dream?'"
    )


def _emit_config_audit_nudge() -> None:
    """Print an additionalContext nudge prompting a subtractive config audit."""
    print(
        "\n--- SYSTEM NUDGE ---\n"
        "Config-audit gate is open (no valid record of a config/rules "
        "review within the last 60 days). Surface this to the user at the "
        "next natural stopping point: 'The periodic config audit looks due "
        "— worth running /multiplai-context:config-audit to prune stale "
        "rules?'"
    )


def _emit_prospective_nudge(memory_dir: Path, data_dir: Path) -> int:
    """Surface intentions that have come due. Returns how many were surfaced.

    Prospective memory is the one channel where being silent is the failure:
    an intention nobody surfaces is indistinguishable from one never captured.
    Unlike the dream and config-audit gates there is no time window to respect
    — the intention's own due date IS the gate, so this runs every session and
    stays quiet until something is actually due.

    Condition-triggered intentions have no date to gate on, so they ride a
    30-day *elapsed* sweep stamped per-intention in ``data_dir``. Stamping is
    what stops a swept condition re-firing next session, and it happens only
    for the ones actually printed — so a crash between print and stamp costs a
    duplicate nudge, never a swallowed one.

    Never fatal. A malformed prospective.md must not stop a session from
    starting.
    """
    try:
        from lib.prospective import (
            SWEEP_STATE_FILENAME, actionable, load, load_sweep_state,
            render_nudge, save_sweep_state, sweep_key,
        )

        today = date.today()
        sweep_file = data_dir / SWEEP_STATE_FILENAME
        stamps = load_sweep_state(sweep_file)
        intentions = load(memory_dir)
        due = actionable(intentions, today, last_surfaced=stamps)
        if not due:
            return 0
        print(render_nudge(due, today))
        swept = {sweep_key(i): today for i in due if i.due is None}
        if swept:
            # Prune stamps for intentions no longer in prospective.md (deleted
            # or reworded — a reword is a new key by design). Without this the
            # state file only ever grows.
            live = {sweep_key(i) for i in intentions if i.due is None}
            kept = {k: v for k, v in stamps.items() if k in live}
            save_sweep_state(sweep_file, {**kept, **swept})
        return len(due)
    except Exception:
        logger.exception("Prospective-memory check failed; continuing")
        return 0


def _inject_checkpoint_recovery(
    data_dir, cwd: str, session_id: str, source: str = ""
) -> bool:
    """Rebuild injection: seed a fresh context window from a pending checkpoint.

    Two paths land here:

    * **Automatic (source="compact")** — the runtime steers native
      auto-compaction to fire near the handoff threshold (see README:
      ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` / ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``).
      The session id is unchanged, so same-session marker consumption is
      allowed, and the checkpoint lands right after the compaction summary —
      no user action at all.
    * **Manual (source="clear")** — the user deliberately continued via
      /clear; the fresh session consumes the marker left by the handed-off
      one.

    Any other source (startup, resume) does NOT inject — a brand-new
    session in the project starts clean rather than inheriting parked work
    (decided 2026-07-06 after live testing surprised with a
    startup-inherited seed; the now/ project-state injection covers soft
    continuity for fresh sessions).

    After injecting, per-session band counters reset so the new physical
    window checkpoints again. Best-effort: any failure means "no recovery",
    never a broken start. Returns True when a rebuild seed was injected.

    Compact-path fallback: compaction can fire while the checkpoint writer
    is still in flight (or after a startup injection already consumed the
    marker), so on ``source="compact"`` a missing marker falls back to the
    session's OWN latest ``checkpoint.md`` — the session id is unchanged
    across compaction, so that file is exactly this conversation's state.
    """
    try:
        from lib import checkpoint as cp

        cfg = cp.load_config()
        if not cfg.enabled or not cwd:
            return False
        if source not in ("clear", "compact"):
            return False
        payload = cp.consume_pending_marker(
            data_dir, cwd, session_id, cfg,
            allow_same_session=(source == "compact"),
        )
        if not payload and source == "compact":
            own = cp.checkpoint_file(data_dir, session_id)
            if own.exists():
                state = cp.load_state(data_dir, session_id)
                payload = {
                    "session_id": session_id,
                    "checkpoint_path": str(own),
                    "tokens": int(state.get("last_checkpoint_tokens") or 0),
                }
        if not payload:
            return False
        checkpoint_path = Path(str(payload.get("checkpoint_path") or ""))
        if not checkpoint_path.exists():
            return False
        text = checkpoint_path.read_text(encoding="utf-8").strip()
        if not cp.validate_checkpoint(text):
            logger.warning("Pending checkpoint failed validation; not injecting")
            return False
        tokens = int(payload.get("tokens") or 0)
        print("\n" + cp.build_rebuild_context(text, tokens))
        # The new physical window must checkpoint again from scratch.
        cp.reset_session_counters(data_dir, session_id)
        old_session = payload.get("session_id", "")
        if old_session and old_session != session_id:
            cp.reset_session_counters(data_dir, old_session)
        logger.info(
            "Injected checkpoint rebuild from session %s (%d tokens, source=%s)",
            payload.get("session_id"), tokens, source or "startup",
        )
        log_event(
            "checkpoint", "rebuild",
            f"session rebuilt from checkpoint of {payload.get('session_id', '?')} "
            f"({tokens:,} tokens, {'automatic via compact' if source == 'compact' else 'manual'})",
            session_id=session_id,
            source_session=payload.get("session_id", ""),
            tokens=tokens,
        )
        return True
    except Exception:
        logger.warning("Checkpoint recovery injection failed", exc_info=True)
        return False


def main() -> None:
    # SessionStart hook input carries the session cwd; we use it to pick the
    # project's now-snapshot to inject. Read defensively — a missing/garbage
    # payload just means "no project state".
    hook_input = read_hook_input()

    # A subagent / nested hook session must not run any of this: it would
    # register itself in the fleet, launch four detached children and drain
    # the extraction queues on every SDK child spawn (M7).
    from lib.checkpoint import is_child_session

    if is_child_session(hook_input.get("transcript_path") or ""):
        return

    cwd = hook_input.get("cwd", "")
    setup_logging("session_start", session_id=hook_input.get("session_id") or "")

    with hook_run(
        "session_start", logger, session_id=hook_input.get("session_id") or "",
    ) as run:
        _start_pass(hook_input, cwd, run)


def _start_pass(hook_input: dict, cwd: str, run) -> None:
    """The hook body: register the session, inject state, run the gates.

    Timed in stages because this hook does a dozen unrelated things behind one
    60s ceiling — a slow one is invisible in a single total.
    """
    paths = get_paths()
    data_dir = paths.plugin_data()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Hub session registry (hub input contract): GC week-old ended entries,
    # then stamp this session's "start" event. Best-effort — with no hub
    # installed the files are simply never read.
    #
    # The roster read is the launcher's `docker ps` from moments ago (it writes
    # it just before starting this container), so a session whose container is
    # gone is collected on the very next launch instead of ageing out over a
    # week or a month. No roster — vanilla Claude Code — and this is an empty
    # set and the age windows are all there is, exactly as before.
    with run.stage("registry"):
        try:
            from lib import session_registry

            session_registry.gc_stale(data_dir, dead_sids=roster_dead_sids(data_dir))
            session_registry.record_event(data_dir, hook_input, "start")
        except Exception:
            logger.warning("Session registry start-event failed", exc_info=True)

    # Log which model client is available. Try-wrapped like the registry and
    # fleet stages: HookRun.stage never suppresses exceptions by contract, and
    # an unwrapped raise here would skip everything below — including both
    # drains, silently accumulating deferred diary/learnings markers (M6).
    with run.stage("client_select"):
        try:
            client_type = _log_client_selection()
        except Exception:
            logger.exception("Client detection failed; continuing as unknown")
            client_type = "unknown"

    # Warn the user once if neither the SDK nor an API key is present.
    if client_type.startswith("none"):
        _emit_no_client_warning(data_dir)

    # List available memory files for the session-state record. Contents
    # are NOT read or injected here — context_manager.py performs routed,
    # per-prompt memory injection on UserPromptSubmit.
    memory_dir = paths.memory_dir()
    memory_files = (
        sorted(p.name for p in memory_dir.glob("*.md"))
        if memory_dir.is_dir()
        else []
    )

    # Use the real Claude Code session id so every hook (context, nudge,
    # extract, session-end) logs under one id and the activity stream is
    # followable end-to-end. The random fallback only applies when the hook
    # input omits it (older clients / tests).
    session_id = hook_input.get("session_id") or str(uuid.uuid4())[:8]
    session_identity = {
        "session_id": session_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "plugin_mode": paths.is_plugin_mode(),
        "client_type": client_type,
        "memory_files_available": memory_files,
        # Recorded so SessionEnd can tag the diary entry with the project's
        # working directory. context_manager refreshes this each prompt as a
        # fallback for environments where SessionStart input lacks cwd.
        "cwd": cwd,
    }

    # Read-merge-write, not wholesale rewrite: session_state.json is a single
    # file shared across sessions running against the same plugin-data dir. A
    # blind overwrite here would drop turn_index / recently_injected and clear
    # a *concurrent* session's re-recommendation cooldown map. Merging our
    # identity keys over the existing state preserves that bookkeeping. Write
    # is atomic (temp+rename) via the shared core helper — see the
    # known-limitation note in context_manager._persist_turn_state.
    session_state = read_session_state(data_dir) or {}
    session_state.update(session_identity)
    if not write_session_state(data_dir, session_state):
        logger.warning("Could not write session_state.json")

    # One-time per-project "now" snapshot injection (additionalContext).
    with run.stage("project_state"):
        _inject_project_state(paths.now_dir(), cwd)

    # Context-rebuild injection: if this project handed off at the context
    # ceiling, seed this window from its checkpoint. source="compact" is the
    # fully-automatic path (steered auto-compaction, same session id).
    with run.stage("checkpoint_rebuild"):
        _inject_checkpoint_recovery(
            data_dir, cwd, session_id, hook_input.get("source", "")
        )

    # Keep the qmd resources index fresh (no-op unless the qmd backend
    # is active). Detached + flock-guarded, so this never blocks the hook.
    with run.stage("launch_qmd"):
        _launch_qmd_refresh(paths.scripts_dir(), cwd)

    # Price the session-transcript corpus into the cost ledger (no-op unless
    # enable_costs is set). Detached + flock-guarded — never blocks the hook.
    with run.stage("launch_costs"):
        _launch_cost_collection(paths.scripts_dir())

    # Fast-forward subscribed shared memory banks (no-op unless a bank is
    # configured). Detached + TTL-gated — a bank that will not pull is stale,
    # never a session-start failure.
    with run.stage("launch_banks"):
        _launch_bank_sync(paths.scripts_dir())

    # Drain any deferred extraction markers left by previous session_end
    # hooks. SessionEnd is kill-within-seconds, so the heavy LLM
    # extraction is intentionally deferred here where the SessionStart
    # hook has more headroom.
    extract_script = paths.scripts_dir() / "extract_learnings.py"
    with run.stage("drain_extractions"):
        try:
            processed = process_deferred_extractions(data_dir, extract_script).launched
            if processed:
                logger.info("Launched %d deferred extraction(s)", processed)
                log_event(
                    "extract", "launch",
                    f"launched {processed} deferred extraction(s) from prior session(s)",
                    session_id=session_id,
                    count=processed,
                )
        except Exception:
            logger.exception("Deferred extraction processing failed (non-fatal)")

    # Same deal for end-of-session checkpoints queued by a SessionEnd whose
    # container was exiting. The launcher's host-side drain normally gets
    # there first, but it only exists inside the kit — on vanilla Claude Code
    # this hook is the only consumer the queue has.
    with run.stage("drain_checkpoints"):
        try:
            from lib.checkpoint_drain import process_pending_checkpoints

            written = process_pending_checkpoints(
                data_dir, paths.scripts_dir() / "checkpoint_writer.py"
            ).launched
            if written:
                logger.info("Launched %d queued end-of-session checkpoint(s)", written)
                log_event(
                    "checkpoint", "drain",
                    f"launched {written} queued end-of-session checkpoint(s)",
                    session_id=session_id,
                    count=written,
                )
        except Exception:
            logger.exception("Queued checkpoint processing failed (non-fatal)")

    # Collect the checkpoint store. Retirement is attempted once per session,
    # minutes after it ends, and refuses while a pending marker still points at
    # it — and nothing ever revisited a refusal, so 216 directories had piled
    # up by 2026-08-10 with none collected since Jul 7. This is the revisit.
    # In-process for the same reason as the fleet view: a few stats and the
    # occasional rmtree, far cheaper than a `uv run` cold start.
    with run.stage("checkpoint_sweep"):
        try:
            from lib import checkpoint as cp

            expired, collected = cp.sweep_checkpoints(data_dir, cp.load_config())
            if collected:
                log_event(
                    "checkpoint", "sweep",
                    f"collected {collected} superseded checkpoint(s) and "
                    f"{expired} expired marker(s)",
                    session_id=session_id,
                    collected=collected,
                    expired=expired,
                )
        except Exception:
            logger.exception("Checkpoint sweep failed (non-fatal)")

    # Refresh the fleet view (data/AGENTS.md). Runs
    # in-process rather than detached: it is a pure read of sessions/ +
    # checkpoints/ with no LLM call, so it costs a few file reads — far less
    # than the `uv run` cold start a subprocess would pay. Also the moment it
    # matters most, since this hook has just registered a new session.
    with run.stage("fleet_view"):
        try:
            write_fleet_view(data_dir)
        except Exception:
            logger.warning("Fleet view refresh failed (non-fatal)", exc_info=True)

    # Dream gate: emit a nudge when the 24h window has elapsed and
    # fresh learnings are waiting. The nudge is additionalContext only —
    # the actual dream still runs via /multiplai-context:dream when the user
    # chooses.
    dream_state_file = paths.dream_state_file()
    learnings_dir = paths.learnings_dir
    with run.stage("dream_gate"):
        try:
            dream_due = (
                _dream_gate_open(dream_state_file)
                and _learnings_pending(learnings_dir, dream_state_file)
            )
        except Exception:
            logger.exception("Dream gate check failed; no nudge this session")
            dream_due = False
    if dream_due:
        logger.info("Dream gate open with pending learnings; emitting nudge")
        log_event(
            "nudge", "dream",
            "dream gate open (>24h, pending learnings) — surfaced to user",
            session_id=session_id,
        )
        _emit_dream_nudge()

    # Config-audit gate: emit a nudge when >=60 days have passed since the
    # last subtractive config/rules review. State lives beside the dream
    # state and is stamped by config_audit.py --stamp (invoked by the
    # /multiplai-context:config-audit skill); a missing state file is
    # seeded inside the gate check (clock starts at install, no nudge).
    config_audit_state_file = dream_state_file.parent / "config_audit_state.yaml"
    with run.stage("config_gate"):
        config_audit_due = _config_audit_gate_open(config_audit_state_file)
    if config_audit_due:
        logger.info("Config-audit gate open; emitting nudge")
        log_event(
            "nudge", "config_audit",
            "config-audit gate open (>60 days since last audit) — surfaced to user",
            session_id=session_id,
        )
        _emit_config_audit_nudge()

    # Prospective memory: intentions whose due date has arrived. No cadence
    # gate — the due date is the gate.
    with run.stage("prospective"):
        surfaced = _emit_prospective_nudge(memory_dir, data_dir)
    if surfaced:
        logger.info("Prospective memory: %d intention(s) surfaced", surfaced)
        log_event(
            "nudge", "prospective",
            f"{surfaced} due intention(s) surfaced to user",
            session_id=session_id,
        )

    # Proactive memory maintenance. Detached and silent: it produces proposals
    # and derived files, nothing the user needs to see at session start, and
    # nothing it does may delay the session.
    with run.stage("launch_maintainer"):
        maintainer_launched = _launch_maintainer(paths.scripts_dir(), data_dir)
    if maintainer_launched:
        logger.info("Memory maintainer launched (detached)")
        log_event("maintenance", "memory_maintainer",
                  "proactive maintenance pass launched", session_id=session_id)

    run.note(client=client_type, memory_files=len(memory_files))
    logger.info(
        "Session started: %s (%d memory files on disk; not injected — routed per-prompt)",
        session_id, len(memory_files),
    )
    log_event(
        "session", "start",
        f"session started — {len(memory_files)} memory files indexed "
        f"(not injected; routed per-prompt), client={client_type}",
        session_id=session_id,
        memory_files=len(memory_files),
        client=client_type,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session (e.g. disk full, corrupt
        # state) — log and exit cleanly.
        try:
            logger.exception("session_start hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
