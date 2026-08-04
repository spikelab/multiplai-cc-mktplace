"""Proactive memory maintenance — the passes nobody remembers to run.

Everything memory-related today is *reactive*: routing fires on a prompt,
dream fires when Spike runs it, the catalog is rebuilt when something asks.
The result is that maintenance happens exactly as often as it is remembered,
which for a background chore is "eventually".

This runs four passes, unattended, at most once a day:

  1. **Staleness lint** (`lib/memory_lint`) — expired `review by` dates and
     volatile facts with no annotation. Deterministic, no model call.
  2. **Dream proposal** — only when the dream gate is open AND there is a
     learnings backlog. Generates a *proposal*; applying stays behind
     `/dream-remember`.
  3. **Catalog refresh** — only when the catalog is older than the memory
     files it indexes.
  4. **Project status** (`synthesize_now`) — rebuilds `now/<project>.md` for
     the active project only, so the state injected at the next SessionStart
     reflects yesterday's work rather than whenever `/now` was last run.

**This process never modifies `.multiplai/memory/`.** That is the whole
safety story: an unattended pass that could rewrite memory is an unattended
pass that can silently corrupt it. Passes 1 and 2 write to `.multiplai/dreams/`
and the health log; 3 and 4 write derived files (catalogs, `now/`) that are
rebuilt from source and carry no unique state.

Pass 4 runs on a cheap tier via ``pick_model("haiku", …)`` — unattended work
should not spend the session's model budget, and a 3-line status summary does
not need it.

Invoked detached from SessionStart (same fire-and-forget pattern as deferred
extraction), or by hand:

    uv run --no-project scripts/memory_maintainer.py [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import load_yaml, save_yaml
from multiplai_core.log_utils import setup_logging
from multiplai_core.paths import get_paths

logger = setup_logging("memory_maintainer")

STATE_FILENAME = "maintainer_state.yaml"

# Once a day. The passes are cheap but not free (one model call for the dream
# proposal, one for the status rebuild), and nothing they surface changes
# meaningfully within hours — a stale-by-a-day lint report is still actionable,
# and re-running it every session would be pure spend.
GATE_HOURS = 24

# Catalog rebuilds are only worth it when memory has actually moved. Rebuilding
# on a timer instead burns a model call to reproduce a byte-identical file.
CATALOG_STALE_SECONDS = 0  # any memory file newer than the catalog counts


@dataclass
class PassResult:
    name: str
    ran: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"pass": self.name, "ran": self.ran, "detail": self.detail}


@dataclass
class MaintenanceReport:
    gate_open: bool
    passes: list[PassResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "gate_open": self.gate_open,
            "passes": [p.as_dict() for p in self.passes],
        }


# --- gate -------------------------------------------------------------------

def _state_file(data_dir: Path) -> Path:
    return data_dir / STATE_FILENAME


def gate_open(state_file: Path, *, now: datetime | None = None) -> bool:
    """True when >=24h have passed since the last maintenance run.

    Missing or unreadable state opens the gate — the failure mode of running
    an extra maintenance pass is a few cents; the failure mode of a corrupt
    state file wedging the gate shut forever is maintenance that silently
    never runs again.
    """
    now = now or datetime.now(timezone.utc)
    try:
        state = load_yaml(state_file) or {}
    except Exception:
        logger.warning("Unreadable maintainer state %s; gate open", state_file)
        return True

    last_run = state.get("last_run")
    if not last_run:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last_run))
    except (TypeError, ValueError):
        logger.warning("Unparseable last_run %r; gate open", last_run)
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return now - last_dt >= timedelta(hours=GATE_HOURS)


def stamp(state_file: Path, *, now: datetime | None = None) -> None:
    """Record that maintenance ran. Best-effort: a failed stamp costs one
    duplicate run next session, so it must never abort the passes."""
    now = now or datetime.now(timezone.utc)
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml(state_file, {"last_run": now.isoformat()})
    except Exception:
        logger.exception("Could not stamp maintainer state %s", state_file)


# --- pass 1: staleness lint -------------------------------------------------

def run_lint(memory_dir: Path, dreams_dir: Path, *, dry_run: bool = False) -> PassResult:
    """Write a lint report next to the dream proposals.

    Reported, never fixed: the linter's whole design is that it warns about
    facts it cannot verify. Auto-editing on a heuristic is how a correct
    memory line gets rewritten by a regex at 3am.
    """
    try:
        from lib.memory_lint import lint_dir, summarize

        findings = lint_dir(memory_dir)
        if not findings:
            return PassResult("lint", True, "no findings")
        if dry_run:
            return PassResult("lint", True, f"{len(findings)} finding(s) (dry run)")
        dreams_dir.mkdir(parents=True, exist_ok=True)
        report = dreams_dir / "memory-lint-latest.md"
        report.write_text(summarize(findings, memory_dir), encoding="utf-8")
        return PassResult("lint", True, f"{len(findings)} finding(s) → {report.name}")
    except Exception as exc:
        logger.exception("Lint pass failed")
        return PassResult("lint", False, f"error: {exc}")


# --- pass 2: dream proposal -------------------------------------------------

def pending_proposals(dreams_dir: Path) -> list[Path]:
    """Un-archived proposals sitting in *dreams_dir*, oldest first.

    `dream.py` archives a proposal into ``dreams_dir/<disposition>/`` once it
    has been applied or rejected, so anything still at the top level is
    awaiting Spike.
    """
    if not dreams_dir.is_dir():
        return []
    return sorted(p for p in dreams_dir.glob("processed-learnings-*.md")
                  if p.is_file())


def run_dream(script_dir: Path, dream_state: Path, learnings_dir: Path,
              dreams_dir: Path, *, dry_run: bool = False) -> PassResult:
    """Generate a consolidation proposal when there is a backlog to consolidate.

    Runs plain `dream.py` — deliberately NOT `--auto`. The plan's hard
    constraint is that `/dream-remember` stays the only path that writes to
    memory, and an unattended `--auto` would quietly become a second one.

    Skipped entirely while an un-archived proposal is already waiting.
    Generating does not (and should not) stamp the dream gate, since nothing
    was applied — so without this check a backlog left unconsolidated for a
    week produces seven dated proposal files, each costing a proposal plus a
    critique model call, and `/dream-remember` is then handed a pile to choose
    between instead of the one thing to review.
    """
    try:
        from session_start import _dream_gate_open, _learnings_pending

        if not _dream_gate_open(dream_state):
            return PassResult("dream", False, "dream gate closed (<24h)")
        if not _learnings_pending(learnings_dir, dream_state):
            return PassResult("dream", False, "no pending learnings")
        waiting = pending_proposals(dreams_dir)
        if waiting:
            return PassResult(
                "dream", False,
                f"{len(waiting)} proposal(s) already awaiting review "
                f"(oldest: {waiting[0].name}) — run /dream-remember")
        if dry_run:
            return PassResult("dream", True, "would generate a proposal (dry run)")

        script = script_dir / "dream.py"
        proc = subprocess.run(
            ["uv", "run", "--no-project", str(script)],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            return PassResult("dream", False, f"exit {proc.returncode}")
        return PassResult("dream", True, "proposal generated")
    except subprocess.TimeoutExpired:
        logger.warning("Dream pass timed out")
        return PassResult("dream", False, "timed out")
    except Exception as exc:
        logger.exception("Dream pass failed")
        return PassResult("dream", False, f"error: {exc}")


# --- pass 3: catalog refresh ------------------------------------------------

def catalog_is_stale(memory_dir: Path, catalog_path: Path) -> bool:
    """True when any memory file is newer than the catalog that indexes it."""
    if not catalog_path.is_file():
        return True
    try:
        cat_mtime = catalog_path.stat().st_mtime
        return any(f.stat().st_mtime > cat_mtime + CATALOG_STALE_SECONDS
                   for f in memory_dir.glob("*.md"))
    except OSError:
        return False


def run_catalog(script_dir: Path, memory_dir: Path, catalogs_dir: Path,
                *, dry_run: bool = False) -> PassResult:
    try:
        catalog_path = catalogs_dir / "memory.json"
        if not catalog_is_stale(memory_dir, catalog_path):
            return PassResult("catalog", False, "catalog is current")
        if dry_run:
            return PassResult("catalog", True, "would rebuild (dry run)")
        proc = subprocess.run(
            ["uv", "run", "--no-project", str(script_dir / "generate_catalog.py"),
             "--only", "memory"],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            return PassResult("catalog", False, f"exit {proc.returncode}")
        return PassResult("catalog", True, "memory catalog rebuilt")
    except subprocess.TimeoutExpired:
        return PassResult("catalog", False, "timed out")
    except Exception as exc:
        logger.exception("Catalog pass failed")
        return PassResult("catalog", False, f"error: {exc}")


# --- pass 4: project status -------------------------------------------------

def active_project(diary_dir: Path) -> str | None:
    """The project with the most recent diary activity.

    Scoped to one project on purpose: rebuilding every `now/` file is a model
    call per project to refresh state for projects nobody is working on.
    """
    try:
        from synthesize_now import _scan_diary

        entries_by_project = _scan_diary(diary_dir)
        if not entries_by_project:
            return None
        return max(
            entries_by_project,
            key=lambda p: max(
                (e.get("timestamp") or "") for e in entries_by_project[p]),
        )
    except Exception:
        logger.exception("Could not determine the active project")
        return None


def run_status(diary_dir: Path, *, dry_run: bool = False) -> PassResult:
    try:
        project = active_project(diary_dir)
        if not project:
            return PassResult("status", False, "no recent diary activity")
        if dry_run:
            return PassResult("status", True, f"would rebuild now/{project}.md (dry run)")

        from multiplai_core.env import pick_model
        from synthesize_now import synthesize

        # Unattended work does not get the session's model budget. A 3-line
        # status summary from diary text is exactly the shape of task the
        # cheap tier handles, and `[maintainer_status] MODEL=` in
        # multiplai.conf can override it without a code change.
        model = pick_model("haiku", "maintainer_status")
        asyncio.run(synthesize(project, model=model))
        return PassResult("status", True, f"rebuilt now/{project}.md on {model}")
    except Exception as exc:
        logger.exception("Status pass failed")
        return PassResult("status", False, f"error: {exc}")


# --- orchestration ----------------------------------------------------------

def run_maintenance(*, force: bool = False, dry_run: bool = False) -> MaintenanceReport:
    paths = get_paths()
    data_dir = paths.data_dir()
    memory_dir = paths.memory_dir()
    script_dir = Path(__file__).parent

    state = _state_file(data_dir)
    if not force and not gate_open(state):
        logger.info("Maintainer gate closed (<%dh since last run)", GATE_HOURS)
        return MaintenanceReport(gate_open=False)

    report = MaintenanceReport(gate_open=True)
    report.passes.append(run_lint(memory_dir, paths.dreams_dir(), dry_run=dry_run))
    report.passes.append(run_dream(script_dir, paths.dream_state_file(),
                                   paths.learnings_dir(), paths.dreams_dir(),
                                   dry_run=dry_run))
    report.passes.append(run_catalog(script_dir, memory_dir, paths.catalogs_dir(),
                                     dry_run=dry_run))
    report.passes.append(run_status(paths.diary_dir(), dry_run=dry_run))

    if not dry_run:
        stamp(state)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Proactive memory maintenance (lint, dream, catalog, status)")
    parser.add_argument("--force", action="store_true",
                        help="Run even if the 24h gate is closed")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what each pass would do; write nothing")
    args = parser.parse_args()

    # stdin may carry a hook payload; drain it so a piping caller doesn't block.
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except OSError:
            pass

    report = run_maintenance(force=args.force, dry_run=args.dry_run)
    print(json.dumps(report.as_dict(), indent=2))


if __name__ == "__main__":
    main()
