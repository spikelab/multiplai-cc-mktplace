"""Proactive memory maintenance — the passes nobody remembers to run.

Everything memory-related today is *reactive*: routing fires on a prompt,
dream fires when Spike runs it, the catalog is rebuilt when something asks.
The result is that maintenance happens exactly as often as it is remembered,
which for a background chore is "eventually".

## Which passes write memory, and which do not

Before 0.36.0 this was one sentence — none of them did. It is now the first
thing a reader needs, because the answer stopped being uniform:

* **Writes `.multiplai/memory/`:** pass 7 (`run_triage`) and nothing else. It is
  gated on `memory_write_mode`, off entirely under `review`, and every applied
  item is in a receipt on a revertable commit.
* **Never writes memory:** passes 1-6, and pass 8's three doctor passes. The
  doctor in particular *proposes deletions and merges*, which is precisely why
  it may not apply them: an addition is undone by `git revert`, whereas a wrong
  merge destroys a fact no receipt can reconstruct (contract C5). It writes one
  markdown report and nothing else, in any mode, with no flag to change that.

This runs eight passes, unattended, at most once a day (pass 8 weekly):

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
  5. **Utilisation retention** — collapses `utilisation.jsonl` records older
     than 90 days into per-section running totals. Deterministic, no model
     call.
  6. **Utilisation judge** — the independent estimator of whether injected
     memory was actually used, on a sample of sessions that have no verdict
     yet. Cheap tier, fails closed: a failed call leaves the record *unjudged*,
     never judged-unused.

  7. **Triage apply** — the one pass that writes `.multiplai/memory/`. Gated on
     `memory_write_mode`; see below.
  8. **Memory doctor** — weekly, on its own state key: duplication,
     within-file contradiction, and dead weight read off the utilisation
     table. Cheap tier, fails closed, and writes a *report* to
     `.multiplai/dreams/doctor-YYYY-MM-DD.md` — never memory.

**Until 0.36.0 this process never modified `.multiplai/memory/`, and that was
the whole safety story.** Pass 7 trades it, deliberately, and the trade is
worth stating rather than deleting: an unattended pass that can rewrite memory
is an unattended pass that can silently corrupt it, so what was bought had to
be worth more than that.

What was bought is that the corpus stops growing behind a gate nobody can get
through. The old property held only because the review queue was unbounded —
227 pending records in two days, a 194-item proposal that costs a whole context
window to walk, and a backlog that grew instead of shrinking. "Never writes" is
not safety when the alternative is "never consolidates".

What replaces it is not trust in a model, it is four independent things, none
of which the model can reach:

* `memory_write_mode` **defaults to `triage`**, and `review` restores the old
  behaviour exactly — this pass then judges nothing and writes nothing;
* a **rubric in code** (`lib/dream_triage.py`) decides what may be written at
  all from the provenance/kind pair; `kind: RULE` never qualifies, in any mode,
  so nothing that changes how the agent behaves can arrive this way;
* a **code floor** (`lib/memory_write_floor.py`) runs after the verdict and can
  only refuse — path containment, reserved filenames, append-only;
* every applied item is in a **receipt**, and memory is a git repo, so the
  containment is `git revert <sha>` on one commit.

Passes 1, 2 and 8 write to `.multiplai/dreams/` and the health log; 3 and 4
write derived files (catalogs, `now/`) that are rebuilt from source and carry no
unique state; 5 and 6 write only `data/utilisation.jsonl`, which is telemetry —
nothing reads it to decide what memory says.

Pass 4 runs on a cheap tier via ``pick_model("haiku", …)`` — unattended work
should not spend the session's model budget, and a 3-line status summary does
not need it.

Invoked detached from SessionStart (same fire-and-forget pattern as deferred
extraction), or by hand:

    uv run --project scripts scripts/memory_maintainer.py [--force] [--dry-run]
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
from lib.dream_chunking import CHUNK_TIMEOUT_S
from lib.runtime import run_supervised, uv_run_argv

logger = setup_logging("memory_maintainer")

STATE_FILENAME = "maintainer_state.yaml"

# How long the unattended dream pass may take, derived from dream's own per-chunk
# budget rather than picked — the two must move together, and importing the
# constant is what guarantees they do. `lib.dream_chunking` is pure, so this
# import costs nothing; `dream.py` itself must NOT be imported (it configures
# logging and mutates the environment at import time).
#
# 4x because a dream run is many chunks, not one: an oversized block already gets
# 2x on its own call (`plan_chunks`), and the chunks run concurrently behind a
# semaphore with a scheduling tail no lower bound sees. The measured worst case
# is the 283 KB backlog at 37m55s (2,275 s) end to end, which 2,400 s would clear
# only by 5%; 4 x 900 = 3,600 s clears it with real margin and still fails inside
# the hour. What this replaces was a hardcoded 600 s — less than one chunk's
# deadline, so the pass could not finish however fast the model ran.
DREAM_PASS_TIMEOUT_S = 4 * CHUNK_TIMEOUT_S

# Catalog and status rebuilds are ordinary single-call scripts, not fan-outs.
SHORT_PASS_TIMEOUT_S = 600

# Once a day. The passes are cheap but not free (one model call for the dream
# proposal, one for the status rebuild), and nothing they surface changes
# meaningfully within hours — a stale-by-a-day lint report is still actionable,
# and re-running it every session would be pure spend.
GATE_HOURS = 24

# The doctor is weekly, on its own key. Two reasons it is not folded into the
# daily gate. It costs more than every other pass put together (one call per
# batch of duplicate pairs plus one per changed memory file), and its findings
# move on the timescale of the corpus, not of a session — a duplicate pair found
# on Monday is still there on Tuesday, and re-reporting it daily is how a report
# becomes something nobody opens. A separate key also means a doctor failure
# cannot wedge the daily passes, and vice versa.
DOCTOR_GATE_HOURS = 24 * 7

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


#: The daily gate's key, and the weekly doctor's. Separate keys in one file so
#: that stamping either cannot disturb the other — which is why the read and
#: write below are a merge rather than a whole-file overwrite.
LAST_RUN_KEY = "last_run"
DOCTOR_KEY = "last_doctor_run"


def _load_state(state_file: Path) -> dict:
    try:
        return load_yaml(state_file) or {}
    except Exception:
        logger.warning("Unreadable maintainer state %s", state_file)
        return {}


def gate_open(
    state_file: Path,
    *,
    now: datetime | None = None,
    key: str = LAST_RUN_KEY,
    hours: int = GATE_HOURS,
) -> bool:
    """True when >= *hours* have passed since the run recorded under *key*.

    Missing or unreadable state opens the gate — the failure mode of running
    an extra maintenance pass is a few cents; the failure mode of a corrupt
    state file wedging the gate shut forever is maintenance that silently
    never runs again.
    """
    now = now or datetime.now(timezone.utc)
    state = _load_state(state_file)

    last_run = state.get(key)
    if not last_run:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last_run))
    except (TypeError, ValueError):
        logger.warning("Unparseable %s %r; gate open", key, last_run)
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return now - last_dt >= timedelta(hours=hours)


def doctor_gate_open(state_file: Path, *, now: datetime | None = None) -> bool:
    """True when >= a week has passed since the last doctor run."""
    return gate_open(state_file, now=now, key=DOCTOR_KEY, hours=DOCTOR_GATE_HOURS)


def stamp(state_file: Path, *, now: datetime | None = None,
          key: str = LAST_RUN_KEY) -> None:
    """Record that a run happened under *key*. Best-effort: a failed stamp costs
    one duplicate run next session, so it must never abort the passes.

    Read-modify-write, never a bare overwrite: the file holds two independent
    gates, and writing only the key being stamped would silently reset the other
    one — which for the weekly doctor would mean it fired every single day.
    """
    now = now or datetime.now(timezone.utc)
    try:
        state = _load_state(state_file)
        state[key] = now.isoformat()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml(state_file, state)
    except Exception:
        logger.exception("Could not stamp maintainer state %s", state_file)


def stamp_doctor(state_file: Path, *, now: datetime | None = None) -> None:
    """Record that the weekly doctor ran, leaving the daily gate untouched."""
    stamp(state_file, now=now, key=DOCTOR_KEY)


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

    Runs plain `dream.py` — deliberately NOT `--auto`. `--auto` applies a whole
    proposal on the drafting pass's own say-so, with no separate judge, no
    rubric and no floor; the classified path in :func:`run_triage` is what
    replaced it here, and it is not the same thing wearing a different flag.

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
        proc = run_supervised(uv_run_argv(script), timeout=DREAM_PASS_TIMEOUT_S)
        if proc.returncode != 0:
            return PassResult("dream", False, f"exit {proc.returncode}")
        return PassResult("dream", True, "proposal generated")
    except subprocess.TimeoutExpired:
        logger.warning("Dream pass timed out after %.0fs; its process group was killed",
                       DREAM_PASS_TIMEOUT_S)
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
        proc = run_supervised(
            uv_run_argv(script_dir / "generate_catalog.py", "--only", "memory"),
            timeout=SHORT_PASS_TIMEOUT_S,
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


# --- pass 5: utilisation retention ------------------------------------------

def run_utilisation_compact(data_dir: Path, *, dry_run: bool = False) -> PassResult:
    """Collapse utilisation records older than 90 days into running totals.

    Deterministic, no model call. The per-section aggregate survives exactly —
    only the per-session detail is dropped — so the table reads identically
    before and after. Without this the file grows one record per session
    forever, and the first thing to suffer is the pass that reads it whole.
    """
    try:
        from lib.utilisation import RETENTION_DAYS, compact, utilisation_path

        path = utilisation_path(data_dir)
        if not path.exists():
            return PassResult("utilisation-compact", False, "no utilisation log yet")
        if dry_run:
            preview = compact(path, dry_run=True)
            return PassResult(
                "utilisation-compact", True,
                f"would collapse {preview['collapsed']} session record(s) "
                f"older than {RETENTION_DAYS}d, keeping {preview['kept']} "
                f"(dry run)")
        result = compact(path)
        if not result["collapsed"]:
            return PassResult("utilisation-compact", False, "nothing older than "
                              f"{RETENTION_DAYS}d")
        return PassResult(
            "utilisation-compact", True,
            f"collapsed {result['collapsed']} session record(s) into totals "
            f"over {result['sections']} section(s)")
    except Exception as exc:
        logger.exception("Utilisation compaction failed")
        return PassResult("utilisation-compact", False, f"error: {exc}")


# --- pass 6: utilisation judge (estimator B) --------------------------------

def run_utilisation_judge(data_dir: Path, *, dry_run: bool = False) -> PassResult:
    """Judge a sample of un-judged sessions on the cheap tier.

    The independent half of the utilisation estimate. Three behaviours are
    load-bearing and asserted by the tests:

    * **degrades** — no model client means this pass records *nothing*; there
      is no heuristic fallback, because a guessed verdict is worse than a
      missing one (contract C3);
    * **fails closed** — a failed call writes no verdict, leaving the record
      ``judge: null``. A missing judgement is never counted as unused
      (contract C4), and the count that kept its default is reported;
    * **reports coverage** — how many were sampled out of how many were
      eligible, so the sampling rate is visible rather than assumed.
    """
    try:
        from lib.utilisation import read_records, sessions_awaiting_judge, utilisation_path
        from lib.utilisation_judge import configured_sample_size, judge_sessions

        sample_size = configured_sample_size()
        if sample_size <= 0:
            return PassResult("utilisation-judge", False,
                              "disabled (utilisation_judge_sample=0)")

        path = utilisation_path(data_dir)
        eligible = sessions_awaiting_judge(read_records(path))
        if not eligible:
            return PassResult("utilisation-judge", False, "no sessions awaiting a judgement")
        if dry_run:
            return PassResult(
                "utilisation-judge", True,
                f"would judge {min(sample_size, len(eligible))} of "
                f"{len(eligible)} eligible session(s) (dry run)")

        from multiplai_core.env import pick_model
        from multiplai_core.model_client import create_client

        model = pick_model("haiku", "utilisation_judge")

        async def _run() -> dict:
            client = await create_client(component="utilisation_judge")
            return await judge_sessions(
                path, client=client, model=model, sample_size=sample_size,
            )

        report = asyncio.run(_run())
        detail = (
            f"judged {report['judged']}/{report['sampled']} sampled of "
            f"{report['eligible']} eligible on {model}"
        )
        if report["kept_default"]:
            detail += (f"; {report['kept_default']} kept the conservative "
                       f"default (call failed — recorded as unjudged, not unused)")
        return PassResult("utilisation-judge", True, detail)
    except Exception as exc:
        # Includes create_client raising on vanilla Claude Code: record
        # nothing, say so, never guess.
        logger.exception("Utilisation judge pass failed")
        return PassResult("utilisation-judge", False, f"error: {exc}")


# --- pass 7: classified triage apply ----------------------------------------

def run_triage(script_dir: Path, dreams_dir: Path, *, dry_run: bool = False) -> PassResult:
    """Classify the waiting proposal and apply what clears every check.

    The only pass that writes `.multiplai/memory/`. See the module docstring for
    what was traded to get here and what holds the line in its place.

    Off entirely under `memory_write_mode=review`, which is a one-word config
    change back to the pre-0.36.0 behaviour. Runs only when a proposal is
    actually waiting — `dream.py --triage` picks the newest pending one itself,
    so this pass exists to decide *whether* to call it, not to find its input.
    """
    try:
        from lib.dream_triage import write_mode

        mode = write_mode()
        if mode == "review":
            return PassResult("triage", False,
                              "memory_write_mode=review — nothing applied")
        waiting = pending_proposals(dreams_dir)
        if not waiting:
            return PassResult("triage", False, "no proposal awaiting triage")
        if dry_run:
            return PassResult("triage", True,
                              f"would triage {waiting[-1].name} in {mode} mode "
                              f"(dry run)")

        proc = run_supervised(
            uv_run_argv(script_dir / "dream.py", "--triage",
                        "--proposal", str(waiting[-1])),
            timeout=DREAM_PASS_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return PassResult("triage", False, f"exit {proc.returncode}")
        return PassResult("triage", True, f"triaged {waiting[-1].name} in {mode} mode")
    except subprocess.TimeoutExpired:
        logger.warning("Triage pass timed out after %.0fs; its process group was killed",
                       DREAM_PASS_TIMEOUT_S)
        return PassResult("triage", False, "timed out")
    except Exception as exc:
        logger.exception("Triage pass failed")
        return PassResult("triage", False, f"error: {exc}")


# --- pass 8: the memory doctor (weekly) -------------------------------------

def run_doctor(memory_dir: Path, data_dir: Path, dreams_dir: Path,
               *, dry_run: bool = False) -> PassResult:
    """Duplication, within-file contradiction and dead weight — as a *report*.

    **This pass never writes `.multiplai/memory/`** (contract C5), and there is
    no mode or flag that makes it. It reads memory, reads the utilisation table,
    and writes one markdown file under `dreams/`. P4's triage path writes
    additions a receipt can revert; the doctor proposes deletions and merges,
    where a wrong call destroys a fact no receipt can reconstruct — so it
    proposes and Spike decides.

    Three degradation properties, all asserted by the tests:

    * **degrades (C3)** — if `create_client` raises, the two model-backed passes
      are skipped and the report says so in place of their findings. The
      deterministic dead-weight pass still runs;
    * **fails closed (C4)** — a failed duplication batch confirms nothing and a
      failed contradiction check reports nothing for that file, and both counts
      appear in the report;
    * **weekly, on its own key** — `DOCTOR_KEY`, not the daily `LAST_RUN_KEY`,
      so a doctor run neither delays nor is delayed by the daily passes.
    """
    try:
        from lib import doctor_contradiction, doctor_deadweight, doctor_duplication
        from lib import doctor_report

        if not memory_dir.is_dir():
            return PassResult("doctor", False, f"no memory directory at {memory_dir}")

        async def _run() -> tuple[dict, dict]:
            client = None
            model = ""
            if not dry_run:
                try:
                    from multiplai_core.env import pick_model
                    from multiplai_core.model_client import create_client

                    # Unattended work does not get the session's model budget,
                    # and both passes are short classification calls.
                    model = pick_model("haiku", "memory_doctor")
                    client = await create_client(component="memory_doctor")
                except Exception:
                    # Vanilla Claude Code with no SDK. The model-backed passes
                    # are skipped and the report says so; there is no heuristic
                    # fallback, because a guess is worse than an absence here.
                    logger.info("Memory doctor: no model client — model-backed "
                                "passes will be skipped (contract C3)")
                    client = None
            dup = await doctor_duplication.run_pass(
                memory_dir, client=client, model=model)
            contra = await doctor_contradiction.run_pass(
                memory_dir, data_dir, client=client, model=model, dry_run=dry_run)
            return dup, contra

        duplication, contradiction = asyncio.run(_run())
        dead = doctor_deadweight.run_pass(memory_dir, data_dir)

        report = doctor_report.render_report(
            duplication, contradiction, dead, memory_dir=memory_dir)
        doctor_report.assert_not_appliable(report)

        summary = (
            f"{duplication['coverage']['confirmed']} duplicate pair(s) confirmed "
            f"of {duplication['shortlisted']} shortlisted, "
            f"{len(contradiction['findings'])} contradiction(s), "
            f"{_dead_total(dead)} dead-weight finding(s)"
        )
        if dry_run:
            return PassResult("doctor", True, f"{summary} (dry run — no report written)")

        dreams_dir.mkdir(parents=True, exist_ok=True)
        path = dreams_dir / doctor_report.report_name()
        path.write_text(report, encoding="utf-8")
        return PassResult("doctor", True, f"{summary} → {path.name}")
    except Exception as exc:
        logger.exception("Doctor pass failed")
        return PassResult("doctor", False, f"error: {exc}")


def _dead_total(dead: dict) -> int:
    return sum(len(dead.get(name) or [])
               for name in ("never_retrieved", "retrieved_unused", "expensive"))


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
    report.passes.append(run_utilisation_compact(data_dir, dry_run=dry_run))
    report.passes.append(run_utilisation_judge(data_dir, dry_run=dry_run))
    # Last: it consumes what pass 2 produced, and it is the only pass that
    # writes memory — so everything that reads memory has already run against
    # the state the session started with.
    report.passes.append(run_triage(script_dir, paths.dreams_dir(), dry_run=dry_run))

    # The doctor is on its own weekly key. `--force` opens both gates; nothing
    # else lets a daily run drag the weekly pass along with it.
    if force or doctor_gate_open(state):
        report.passes.append(run_doctor(memory_dir, data_dir, paths.dreams_dir(),
                                        dry_run=dry_run))
        if not dry_run:
            stamp_doctor(state)
    else:
        report.passes.append(PassResult(
            "doctor", False,
            f"doctor gate closed (<{DOCTOR_GATE_HOURS // 24}d since last run)"))

    if not dry_run:
        stamp(state)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Proactive memory maintenance (lint, dream, catalog, "
                    "status, utilisation compaction, utilisation judge, triage, "
                    "and the weekly memory doctor)")
    parser.add_argument("--force", action="store_true",
                        help="Run even if the 24h gate is closed (also opens the "
                             "doctor's weekly gate)")
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
