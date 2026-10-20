"""Health check script for multiplai plugin.

Audits memory files, diary entries, learnings, and plugin data directories.
Reports which ModelClient implementation is active (R1).
Validates all Paths fields resolve to existing directories.
Outputs a structured report to stdout for the health skill to present.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.model_client import detect_client_type
from lib.memory_router import resolve_strategy

# Machine-readable routing line emitted by context_manager:
#   ... INFO: ROUTING_SCORES memory={"picked": [...], "capped": ..., ...}
_ROUTING_RE = re.compile(r"^ROUTING_SCORES memory=(\{.*\})\s*$")
# Written by context_manager for every routed prompt, whatever the strategy:
#   ... INFO: Router llm picked: memory=2 skills=1
# ROUTING_SCORES above is written only by routers that expose scores
# (token_overlap, llm_hybrid), so for the llm router this line, plus the two
# LLMRouter lines below, is the whole record of what it did.
_PICKED_RE = re.compile(r"^Router (\S+) picked: memory=(\d+) skills=(\d+)\s*$")
_LLM_ABSTAINED_RE = re.compile(r"^LLMRouter abstained")
_LLM_DEGRADED_RE = re.compile(r"^LLMRouter .*degrading to ")
# The multiplai-core line prefix: `[ts] [component] [session:x] LEVEL: msg`.
# Every pattern above is anchored to the start of *msg*, because a
# ROUTING_SCORES line quotes the user's prompt, and a prompt that contains
# "Router llm picked: ..." must not read as a router record.
_LINE_RE = re.compile(r"^\[([^\]]+)\] \[[^\]]*\] \[[^\]]*\] [A-Z]+: (.*)$")
# Strategies whose router writes ROUTING_SCORES (``last_scores``).
_SCORED_STRATEGIES = frozenset({"token_overlap", "llm_hybrid"})
# Bound how much routing history we aggregate (logs have 7-day
# retention; this also caps work on a busy log).
_MAX_ROUTING_SAMPLES = 500


# The starter-template files /multiplai-context:setup creates. Their *absence*
# is what drives the setup recommendation — it does not bound the
# staleness audit, which scans the whole memory corpus.
REQUIRED_MEMORY_FILES = ["me.md", "technical-pref.md", "preferences.md"]
STALENESS_THRESHOLD_DAYS = 30

# How many stale filenames to enumerate in a recommendation before
# collapsing the rest to a "+N more" tail.
_MAX_LISTED_STALE = 10


def _check_directory_status(name: str, path: Path) -> dict:
    """Check whether a directory exists and return status info."""
    exists = path.is_dir()
    return {
        "name": name,
        "path": str(path),
        "exists": exists,
        "status": "found" if exists else "missing",
    }


def _check_memory_file(memory_dir: Path, filename: str) -> dict:
    """Check a single memory file for existence, size, and staleness."""
    filepath = memory_dir / filename
    if not filepath.exists():
        return {
            "name": filename,
            "exists": False,
            "status": "missing",
            "size": 0,
            "mtime": None,
            "stale": False,
        }

    stat = filepath.stat()
    st_size = stat.st_size
    st_mtime = stat.st_mtime
    modified_dt = datetime.fromtimestamp(st_mtime, tz=timezone.utc)
    age_days = (datetime.now(timezone.utc) - modified_dt).days
    stale = age_days > STALENESS_THRESHOLD_DAYS

    return {
        "name": filename,
        "exists": True,
        "status": "stale" if stale else "ok",
        "size": st_size,
        "mtime": modified_dt.isoformat(),
        "age_days": age_days,
        "stale": stale,
    }


def _count_diary_days(diary_dir: Path) -> int:
    """Count ``YYYY-MM-DD.md`` per-day diary files.

    Layout (v0.3.0+): one file per UTC day, with internal ``## Session:``
    blocks. Aligned with learnings. Returns the number of distinct day
    files; not the number of sessions within them.
    """
    if not diary_dir.is_dir():
        return 0
    return len([p for p in diary_dir.glob("*.md") if p.is_file()])


def _count_learnings(learnings_dir: Path) -> int:
    """Count lines across all per-day learnings files in learnings_dir."""
    if not learnings_dir.is_dir():
        return 0
    total = 0
    for f in sorted(learnings_dir.glob("*.md")):
        content = f.read_text().strip()
        if content:
            total += len(content.splitlines())
    return total


def _check_extraction_queue(data_dir: Path) -> dict:
    """Snapshot the deferred-extraction queue.

    Extraction runs as a detached subprocess after `SessionStart` returns,
    so a freshly-started session can briefly show "0 diary entries" while
    the subprocess is still working. Surfacing this prevents users from
    misreading the snapshot as a bug.

    Layout (managed by session_start._process_deferred_extractions and
    session_start._recover_stale_processing):
      - data/pending_extractions/      — queued markers, not yet started
      - data/processing_extractions/   — in-flight (subprocess alive)
      - data/failed_extractions/       — permanently failed after 3 attempts
    """
    pending_dir = data_dir / "pending_extractions"
    processing_dir = data_dir / "processing_extractions"
    failed_dir = data_dir / "failed_extractions"

    def _count(d: Path) -> int:
        return len(list(d.glob("*.json"))) if d.is_dir() else 0

    pending = _count(pending_dir)
    processing = _count(processing_dir)
    failed = _count(failed_dir)

    # An in-flight extraction's marker file mtime reflects when the
    # subprocess took it off the pending queue. Surfacing the oldest
    # in-flight age helps distinguish "extraction running normally"
    # from "extraction child crashed and the marker is orphaned".
    oldest_processing_age_s = None
    if processing_dir.is_dir():
        markers = list(processing_dir.glob("*.json"))
        if markers:
            oldest_mtime = min(m.stat().st_mtime for m in markers)
            oldest_processing_age_s = int(time.time() - oldest_mtime)

    return {
        "pending": pending,
        "processing": processing,
        "failed": failed,
        "in_flight": pending + processing,
        "oldest_processing_age_s": oldest_processing_age_s,
    }


def _get_last_dream_date(data_dir: Path) -> str | None:
    """Read last dream consolidation date from dream state file."""
    dream_state_file = data_dir / "dream_state.yaml"
    if not dream_state_file.exists():
        return None
    try:
        import yaml
        with open(dream_state_file) as f:
            state = yaml.safe_load(f) or {}
        return state.get("last_run")
    except Exception:
        return None


def _routing_status(paths) -> dict:
    """Routing-quality block — the single most critical health signal.

    Zero LLM cost: configured-vs-effective strategy, live aggregates
    parsed from the machine routing log, and the last eval snapshot.
    Note the honest caveats consumed by the skill:
    - token_overlap is the default; its NONE-accuracy is ~0% by
      construction (a known ceiling, not a regression) — abstention
      needs the semantic llm router.
    - llm is deferred/opt-in: ~17s/prompt via the Agent SDK (unusable
      as a blocking pre-prompt hook) pending an async / API-key design.
    """
    status: dict = {}
    try:
        configured = resolve_strategy()
    except Exception:
        configured = "unknown"
    client = detect_client_type()
    # create_router() degrades both model-backed strategies without a client.
    degraded = configured in ("llm", "llm_hybrid") and client.startswith("none")
    status["configured_strategy"] = configured
    status["effective_strategy"] = "token_overlap" if degraded else configured
    status["degraded_to_fallback"] = degraded

    # Live aggregates from the routing log: one file per UTC day
    # (context_manager-YYYY-MM-DD.log), read newest first until there are
    # enough samples. An undated context_manager.log from before multiplai-core
    # 0.21 holds lines older than any dated file, so it is read last.
    events = _read_routing_events(paths.logs_dir())

    picks = [e for e in events if e[0] == "picked"]
    logged = picks[-1] if picks else None
    status["logged_strategy"] = logged[1] if logged else None
    status["logged_strategy_at"] = logged[4] if logged else None
    status["strategy_mismatch"] = bool(
        logged and logged[1] != status["effective_strategy"]
    )

    # Describe what the hook actually ran: its logged strategy wins over the
    # configured one, which this process may resolve differently. Score lines
    # with no pick line to attribute them to (logs older than that line) are
    # reported with strategy None rather than credited to any router.
    if logged:
        live_strategy = logged[1]
    elif any(e[0] == "scores" for e in events):
        live_strategy = None
    else:
        live_strategy = status["effective_strategy"]
    if live_strategy is None or live_strategy in _SCORED_STRATEGIES:
        status["live"] = _score_aggregates(events, live_strategy)
    else:
        status["live"] = _pick_aggregates(events, live_strategy)

    # Last offline eval snapshot (written by eval_router.py).
    snap = paths.data_dir() / "router-eval" / "latest.json"
    status["last_eval"] = None
    if snap.exists():
        try:
            rec = json.loads(snap.read_text())
            gen = rec.get("generated_at")
            age_days = None
            if gen:
                try:
                    dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - dt).days
                except ValueError:
                    pass
            status["last_eval"] = {
                "generated_at": gen,
                "age_days": age_days,
                "strategy": rec.get("strategy"),
                "total_cases": rec.get("total_cases"),
                "recall_pct": rec.get("recall_pct"),
                "precision_pct": rec.get("precision_pct"),
                "none_accuracy_pct": rec.get("none_accuracy_pct"),
                "cap_saturation_pct": rec.get("cap_saturation_pct"),
            }
        except (OSError, json.JSONDecodeError, ValueError):
            status["last_eval"] = None
    return status


def _read_routing_events(logs_dir: Path) -> list:
    """Router events from the context_manager logs, oldest first, bounded.

    Each event is ``("picked", strategy, n_memory, n_skills, ts, abstained,
    degraded)`` or ``("scores", dict, strategy)``. The llm router logs its
    abstain/degrade line before the pick line of the same run, so those flags
    ride on the next pick; a ROUTING_SCORES line follows its run's pick line,
    so it takes the strategy of the pick before it (``None`` when there is
    none). Concurrent runs interleaving in one file can misattribute a line;
    that is accepted. Files are read newest first, and reading stops once
    either kind of sample reaches ``_MAX_ROUTING_SAMPLES``.
    """
    log_files = sorted(logs_dir.glob("context_manager-*.log"), reverse=True)
    log_files.append(logs_dir / "context_manager.log")
    events: list = []
    n_picked = n_scores = 0
    for log_file in log_files:
        if max(n_picked, n_scores) >= _MAX_ROUTING_SAMPLES:
            break
        if not log_file.exists():
            continue
        found: list = []
        abstained = degraded = False
        last_strategy = None
        try:
            lines = log_file.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            line_m = _LINE_RE.match(ln)
            if not line_m:
                continue
            ts, msg = line_m.group(1), line_m.group(2)
            m = _ROUTING_RE.match(msg)
            if m:
                try:
                    found.append(("scores", json.loads(m.group(1)), last_strategy))
                    n_scores += 1
                except (json.JSONDecodeError, ValueError):
                    pass
                continue
            m = _PICKED_RE.match(msg)
            if m:
                last_strategy = m.group(1)
                found.append((
                    "picked", last_strategy, int(m.group(2)), int(m.group(3)),
                    ts, abstained, degraded,
                ))
                abstained = degraded = False
                n_picked += 1
            elif _LLM_ABSTAINED_RE.match(msg):
                abstained = True
            elif _LLM_DEGRADED_RE.match(msg):
                degraded = True
        events = found + events
    return events


def _score_aggregates(events: list, strategy: str | None) -> dict:
    """Live stats from ROUTING_SCORES lines (token_overlap, llm_hybrid).

    Only samples written by *strategy* count; with ``None`` (no pick line to
    say which router wrote them) every sample counts.
    """
    samples = [
        e[1] for e in events
        if e[0] == "scores" and (strategy is None or e[2] == strategy)
    ]
    samples = samples[-_MAX_ROUTING_SAMPLES:]
    if not samples:
        return {"strategy": strategy, "source": "ROUTING_SCORES", "samples": 0}
    n = len(samples)
    capped = sum(1 for s in samples if s.get("capped"))
    picked_counts = [
        s.get("n_picked", len(s.get("picked") or [])) for s in samples
    ]
    tops = [s["picked"][0][1] for s in samples if s.get("picked")]
    floors = [s["picked"][-1][1] for s in samples if s.get("picked")]
    return {
        "strategy": strategy,
        "source": "ROUTING_SCORES",
        "samples": n,
        "cap_saturation_pct": round(100 * capped / n, 1),
        "empty_pct": round(
            100 * sum(1 for c in picked_counts if c == 0) / n, 1
        ),
        "mean_picked": round(sum(picked_counts) / n, 1),
        "mean_top_score": round(sum(tops) / len(tops), 2) if tops else None,
        "mean_floor_score": (
            round(sum(floors) / len(floors), 2) if floors else None
        ),
    }


def _pick_aggregates(events: list, strategy: str) -> dict:
    """Live stats from ``Router <strategy> picked`` lines (the llm router).

    Over the newest ``_MAX_ROUTING_SAMPLES`` runs of *strategy*:
    ``memory_empty_pct`` is the share where the router picked no memory
    (before cooldown; it includes runs that picked only skills);
    ``abstained_pct`` the share where the model answered and picked nothing
    in any corpus; ``degraded_pct`` the share where the llm call failed and
    token_overlap picked instead.
    """
    picks = [e for e in events if e[0] == "picked" and e[1] == strategy]
    picks = picks[-_MAX_ROUTING_SAMPLES:]
    out: dict = {"strategy": strategy, "source": "router_picks",
                 "samples": len(picks)}
    if not picks:
        return out
    n = len(picks)
    out["memory_empty_pct"] = round(
        100 * sum(1 for e in picks if e[2] == 0) / n, 1
    )
    out["mean_picked_memory"] = round(sum(e[2] for e in picks) / n, 1)
    if strategy == "llm":
        out["abstained_pct"] = round(100 * sum(1 for e in picks if e[5]) / n, 1)
        out["degraded_pct"] = round(100 * sum(1 for e in picks if e[6]) / n, 1)
    return out


def _routing_recommendations(routing: dict) -> list:
    """Recommendations about which router is running."""
    recs = []
    if routing.get("degraded_to_fallback"):
        recs.append(
            "memory_router is explicitly set to llm but no model client "
            "is available, so routing fell back to token_overlap. Unset "
            "the option to use the default, or provide a model client."
        )
    if routing.get("strategy_mismatch"):
        recs.append(
            f"The context hook last routed with "
            f"{routing.get('logged_strategy')} "
            f"(at {routing.get('logged_strategy_at')}), but this check "
            f"resolved the configured strategy as "
            f"{routing.get('effective_strategy')}. Either the setting changed "
            f"since, or this process cannot see the configuration the hook "
            f"reads. The live stats describe the logged strategy."
        )
    return recs


def run_health_check() -> dict:
    """Run a full health check and return structured results."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    diary_dir = paths.diary_dir()
    learnings_dir = paths.learnings_dir()
    data_dir = paths.plugin_data()
    venv_dir = paths.venv_dir()

    report = {}

    # R1: Report active ModelClient implementation
    report["model_client"] = detect_client_type()

    # Routing quality — the most critical memory signal (zero LLM cost)
    report["routing"] = _routing_status(paths)

    # Validate directories exist
    report["directories"] = [
        _check_directory_status("memory_dir", memory_dir),
        _check_directory_status("diary_dir", diary_dir),
        _check_directory_status("data_dir", data_dir),
        _check_directory_status("venv_dir", venv_dir),
    ]

    # Check if this is a fresh install (no memory dir)
    if not memory_dir.is_dir():
        report["fresh_install"] = True
        report["memory_files"] = []
        report["recommendations"] = [
            "Memory directory not found. Run /multiplai-context:setup to configure the plugin."
        ]
        print(json.dumps(report, indent=2))
        return report

    report["fresh_install"] = False

    # Full-corpus inventory: every *.md in the memory dir gets size,
    # mtime, and staleness — not just the starter-template trio.
    corpus = sorted(p.name for p in memory_dir.glob("*.md"))
    report["memory_files"] = [
        _check_memory_file(memory_dir, name) for name in corpus
    ]
    # Required starter files that are absent (drives the setup hint).
    required_missing = [
        name for name in REQUIRED_MEMORY_FILES
        if not (memory_dir / name).exists()
    ]
    report["required_missing"] = required_missing

    stale = [f for f in report["memory_files"] if f.get("stale")]
    report["memory_summary"] = {
        "total": len(report["memory_files"]),
        "fresh": sum(
            1 for f in report["memory_files"]
            if f["exists"] and not f.get("stale")
        ),
        "stale": len(stale),
        "required_missing": len(required_missing),
    }

    # Per-fact staleness. The file-level mtime above says when a file was
    # touched; it says nothing about whether the volatile facts *inside* it are
    # still true, and those rot on their own schedule while the file around
    # them stays accurate. Never fatal: a lint failure must not take down the
    # health report that people run when something else is already wrong.
    try:
        from lib.memory_lint import lint_dir

        lint_findings = lint_dir(memory_dir)
        report["memory_validity"] = {
            "expired": [
                {"file": f.path.name, "line": f.lineno, "detail": f.detail}
                for f in lint_findings if f.kind == "expired"
            ],
            "unmarked": [
                {"file": f.path.name, "line": f.lineno, "class": f.fact_class}
                for f in lint_findings if f.kind == "unmarked"
            ],
            # Corpus-wide H2 collisions. Reported here because they are
            # what makes a `file.md#Section` router pick ambiguous, so
            # they degrade retrieval rather than just tidiness.
            "duplicate_h2": [
                {"file": f.path.name, "line": f.lineno, "detail": f.detail}
                for f in lint_findings if f.kind == "duplicate-h2"
            ],
        }
    except Exception as exc:  # pragma: no cover - defensive
        report["memory_validity"] = {"error": str(exc)}

    # Diary and learnings status. Renamed in v0.3.0 to reflect the
    # per-day layout: each value is a day file, not a session file.
    report["diary"] = {
        "day_count": _count_diary_days(diary_dir),
    }

    # Deferred-extraction queue snapshot — explains why a just-started
    # session might transiently report 0 new diary entries.
    report["extractions"] = _check_extraction_queue(data_dir)
    report["learnings"] = {
        "unprocessed_count": _count_learnings(learnings_dir),
    }

    # Last dream consolidation date
    last_dream = _get_last_dream_date(data_dir)
    report["dream_state"] = {
        "last_dream_date": last_dream if last_dream else "never",
    }

    # Build recommendations
    recommendations = []
    if required_missing:
        names = ", ".join(required_missing)
        recommendations.append(
            f"Missing required memory files: {names}. "
            f"Run /multiplai-context:setup to create them."
        )

    stale_files = [f for f in report["memory_files"] if f.get("stale")]
    if stale_files:
        ordered = sorted(stale_files, key=lambda f: f.get("age_days", 0), reverse=True)
        listed = [f["name"] for f in ordered[:_MAX_LISTED_STALE]]
        names = ", ".join(listed)
        if len(ordered) > _MAX_LISTED_STALE:
            names += f", +{len(ordered) - _MAX_LISTED_STALE} more"
        recommendations.append(
            f"{len(ordered)} stale memory file(s) (>{STALENESS_THRESHOLD_DAYS} "
            f"days), oldest first: {names}. Run /multiplai-context:dream to refresh them."
        )

    unprocessed = report["learnings"]["unprocessed_count"]
    if unprocessed > 0:
        recommendations.append(
            f"{unprocessed} unprocessed learning lines pending. Run /multiplai-context:dream then /multiplai-context:dream-remember."
        )

    # Surface in-flight extractions so the user knows diary/learnings
    # counts may grow shortly without further action.
    extractions = report["extractions"]
    if extractions["in_flight"] > 0:
        age = extractions["oldest_processing_age_s"]
        if extractions["processing"] > 0 and age is not None and age < 60:
            recommendations.append(
                f"{extractions['in_flight']} extraction(s) in flight (oldest "
                f"{age}s old). Diary/learnings counts above are a live "
                f"snapshot — re-run /multiplai-context:health in ~30s for the "
                f"settled state."
            )
        else:
            recommendations.append(
                f"{extractions['in_flight']} extraction(s) queued "
                f"(pending={extractions['pending']}, "
                f"processing={extractions['processing']}). They'll run on "
                f"the next /multiplai-context:health-triggering SessionStart, or "
                f"are running now in the background."
            )
    if extractions["failed"] > 0:
        recommendations.append(
            f"{extractions['failed']} extraction(s) permanently failed "
            f"after 3 retries. Inspect "
            f"`<data>/failed_extractions/` for details, or delete the "
            f"markers to suppress."
        )

    routing = report.get("routing") or {}
    recommendations.extend(_routing_recommendations(routing))
    # eval_router needs user-supplied golden cases; only recommend it when some
    # are actually discoverable, otherwise the suggested command just SystemExits.
    try:
        from eval_router import _default_case_paths
        _have_eval_cases = bool(_default_case_paths())
    except Exception:
        _have_eval_cases = False

    last_eval = routing.get("last_eval")
    if last_eval is None and _have_eval_cases:
        recommendations.append(
            "No router eval snapshot. Run "
            'python "${CLAUDE_PLUGIN_ROOT}/scripts/eval_router.py" '
            "(zero LLM cost under token_overlap) to baseline routing quality."
        )
    elif last_eval is not None and (last_eval.get("age_days") or 0) > 30 and _have_eval_cases:
        recommendations.append(
            f"Router eval is {last_eval['age_days']} days old. Re-run "
            'python "${CLAUDE_PLUGIN_ROOT}/scripts/eval_router.py".'
        )

    report["recommendations"] = recommendations

    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    run_health_check()


if __name__ == "__main__":
    main()
