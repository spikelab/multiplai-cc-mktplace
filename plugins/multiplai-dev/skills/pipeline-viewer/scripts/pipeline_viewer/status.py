"""Files and git history → `ChangeStatus`. Pure: no git writes, no network,
no model calls. Every reader is defensive: a missing or unreadable file is an
empty section, never an error.

Where each stop's state comes from (all for the stop's current `revision`):

- **Reached.** Stops 1-3: pm-spec's `.pm-spec/state.json` steps `proposal`,
  `components`, `specs` recorded `passed` (pm-spec records a review stop once
  pushed). Without a run state (pm-spec ran elsewhere), the stop's documents
  being committed. Stop 4: buildme's `plan-approval.md` in the build's copy,
  or a saved `plan_approval`. For a change with components (no build copy
  at the change, a `components/` dir), stop 4 is the change's one plan:
  `plan-approval.md` at the change in `change_plan_dir`, and its revision
  is the one that file names (`Plan revision:`). Stop 5: the build has a
  `pr_url`.
- **Passed without a commit here.** Stops 1-3: a later pm-spec step or the
  handoff exists. Stop 4: `plan_approval` for the current plan revision, or
  the build reached TDD (`--auto`). For a change with components: the plan
  is there with no `plan-approval.md` (written under `--auto`). Such a stop
  is `approved` with `approved_by: []`.
- **Decisions.** The trailers of the change's `review(<name>): …` commits on
  the write checkout's HEAD (and on buildme's feature worktree's HEAD, where
  `--approve-plan` commits the approval of a change's plan) (`record.py` writes them; buildme's
  `--approve-plan` too): `Approved-by`, `Decided-by`, `Rework-requested-by`,
  `Acknowledged-by`, `Commented-by`, each with `Stop` and `Revision`.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from multiplai_core import runs as runs_mod
from viewer_kit.people import Person

from . import gitio
from .config import ViewerConfig
from .models import (
    STOP_LABELS,
    BlockRow,
    BuildStatus,
    CauseCount,
    ChangeStatus,
    DocRef,
    EpicAgent,
    EpicChildRow,
    EpicStatus,
    NeedsYou,
    NeedsYouRow,
    PmSpecStatus,
    Record,
    Reviewer,
    RunRef,
    Scorecard,
    ScorecardChild,
    ScorecardRun,
    ScorecardVersion,
    StopStatus,
)
from .places import BUILD_STATE, BUILD_STATE_DONE, CHANGES, PM_SPEC_DIR, Place

PLAN_DOC = "implementation-plan.md"
OLD_PLAN_DOC = "design.md"
PLAN_REQUEST = "plan-approval.md"
PLAN_REVISION_LINE = "Plan revision:"
TAIL_LINES = 20
# Build phases at or past TDD: stop 4 is behind the build.
PAST_PLAN_PHASES = {"tdd_build", "docs_update", "respec", "publish", "complete"}
STAGE_STEPS = {1: "proposal", 2: "components", 3: "specs"}
LATER_STEPS = {
    1: ("components", "prototype", "tickets", "specs", "consistency"),
    2: ("prototype", "tickets", "specs", "consistency"),
    3: ("consistency",),
}
TRAILER_KINDS = {
    "approved-by": "approve",
    "decided-by": "decided",
    "rework-requested-by": "rework",
    "acknowledged-by": "ack",
    "commented-by": "comment",
    "answered-for": "answer",
}


# --- small readers ---------------------------------------------------------------


def _json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tail(path: Path, n: int = TAIL_LINES) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.strip()][-n:]


def iso_utc(value: str | float | None) -> str | None:
    """ISO 8601 in UTC with a `Z`, from an ISO string or a POSIX time."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, UTC)
        else:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_held(path: Path) -> bool:
    """True when a process holds pm-spec's `run.lock` at *path*.

    pm-spec writes its pid and host into the file when it takes the lock and
    leaves them there after. A holder on another host, or a pid that is gone,
    is not running here. Only when the pid is alive is the `flock` probed
    (shared, non-blocking, dropped at once), so a pm-spec starting while no
    run is alive never races this check."""
    try:
        info = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    if not isinstance(info, dict):
        return False
    pid = info.get("pid")
    if not isinstance(pid, int) or info.get("host") not in (None, socket.gethostname()):
        return False
    if not _pid_alive(pid):
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def pm_spec_running(place: Place) -> bool:
    return any(
        lock_held(c.change_dir(place.name) / PM_SPEC_DIR / "run.lock")
        for c in place.copies
    )


# buildme's last progress note while it waits at stop 4 (`_plan_approval_gate`).
PLAN_WAITING_NOTE = "Waiting for an engineer to approve "


def build_running(place: Place) -> bool:
    """Whether buildme may be at work in the build's worktree, inferred from
    its files (buildme takes no lock for a single build). Stopped means one
    of: the build completed (`.build-state.complete.json`), it stopped on a
    failure (`failed_stop`), or it waits at stop 4: `plan-approval.md` is
    there, no approval is saved for the current plan revision, and the last
    note in `build-progress.md` is the waiting one. Anything else counts as
    running."""
    b = place.build()
    if b is None:
        return False
    state, done = build_state(place)
    if done:
        return False
    if not isinstance(state, dict):
        return True
    if state.get("failed_stop"):
        return False
    revision = (state.get("spec_gen") or {}).get("revision")
    approval = state.get("plan_approval") or {}
    waiting_for_approval = (
        b.change_dir(place.name) / PLAN_REQUEST
    ).is_file() and approval.get("revision") != revision
    tail = _tail(b.checkout / "build-progress.md", 1)
    return not (
        waiting_for_approval and bool(tail) and tail[-1].startswith(PLAN_WAITING_NOTE)
    )


# --- documents -------------------------------------------------------------------


def _files(change_dir: Path, pattern: str) -> list[str]:
    return sorted(
        p.relative_to(change_dir).as_posix()
        for p in change_dir.glob(pattern)
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(change_dir).parts)
    )


def plan_doc(change_dir: Path) -> str:
    """`implementation-plan.md`, or `design.md` for a build from before the rename."""
    if not (change_dir / PLAN_DOC).is_file() and (change_dir / OLD_PLAN_DOC).is_file():
        return OLD_PLAN_DOC
    return PLAN_DOC


def change_plan_dir(place: Place) -> Path | None:
    """The change dir holding the plan of a change with components: buildme's
    feature worktree's copy (what buildme last pushed), else the copy
    documents are read from. None for a single build, or a change with no
    `components/`."""
    if place.build() is not None:
        return None
    feature = place.feature()
    d = feature.change_dir(place.name) if feature is not None else place.read_dir()
    return d if (d / "components").is_dir() else None


def plan_request_revision(change_dir: Path) -> str | None:
    """The revision `plan-approval.md` asks to approve (its `Plan revision:`
    line, as buildme's `epic.change_plan.request_text` writes it)."""
    try:
        text = (change_dir / PLAN_REQUEST).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(PLAN_REVISION_LINE):
            rev = line[len(PLAN_REVISION_LINE) :].strip().strip("`").strip()
            return rev or None
    return None


def stop_docs(n: int, change_dir: Path) -> list[DocRef]:
    def ref(path: str, approve: bool = True) -> DocRef:
        return DocRef(path=path, approve=approve, exists=(change_dir / path).is_file())

    if n == 1:
        return [ref("proposal.md")]
    if n == 2:
        names = ["use-cases.md", "components.md"]
        names = [p for p in names if (change_dir / p).is_file()] or ["use-cases.md"]
        names += _files(change_dir, "components/*/plan.md")
        if (change_dir / "plan.md").is_file():
            names.append("plan.md")
        return [ref(p) for p in names]
    if n == 3:
        names = [p for p in ("specs.md",) if (change_dir / p).is_file()]
        names += _files(change_dir, "components/*/specs.md")
        if (change_dir / "tickets.json").is_file():
            names.append("tickets.json")
        names += _files(change_dir, "prototype/**/*")
        return [ref(p) for p in names or ["specs.md"]]
    if n == 4:
        # The plan of a change with components has no tasks.md beside it:
        # each component writes its own once the plan is approved.
        second = (
            "components.md"
            if not (change_dir / "tasks.md").is_file()
            and (change_dir / "components.md").is_file()
            else "tasks.md"
        )
        return [
            ref(plan_doc(change_dir)),
            ref(second, approve=False),
            ref(PLAN_REQUEST, approve=False),
        ]
    return []


# --- pm-spec and build -----------------------------------------------------------


def pm_spec_status(place: Place) -> PmSpecStatus | None:
    d = place.pm_spec_dir()
    if d is None:
        return None
    state = _json(d / PM_SPEC_DIR / "state.json") or {}
    steps = {
        k: v.get("outcome", "")
        for k, v in (state.get("steps") or {}).items()
        if isinstance(v, dict)
    }
    handoff = state.get("handoff") if isinstance(state.get("handoff"), dict) else None
    meta_status = None
    try:
        from multiplai_core.specs import read_change_meta

        meta = read_change_meta(d)
        meta_status = meta.get("spec_status") if isinstance(meta, dict) else None
    except (OSError, ValueError):
        pass
    return PmSpecStatus(
        steps=steps,
        interview_waiting=bool(state.get("interview_waiting")),
        spec_status=meta_status,
        handoff_revision=handoff.get("revision") if handoff else None,
        running=pm_spec_running(place),
        progress_tail=_tail(d / PM_SPEC_DIR / "progress.md"),
    )


def build_state(place: Place) -> tuple[dict | None, bool]:
    """The build's saved state and whether it is the completed one."""
    b = place.build()
    if b is None:
        return None, False
    d = b.change_dir(place.name)
    live = _json(d / BUILD_STATE)
    if live is not None:
        return live, False
    return _json(d / BUILD_STATE_DONE), True


def _float(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_status(place: Place) -> BuildStatus | None:
    state, complete = build_state(place)
    if state is None:
        return None
    b = place.build()
    d = b.change_dir(place.name)
    reviews = _files(d, "reviews/block-*-round-*.json")
    blocks = []
    for raw in (state.get("tdd") or {}).get("blocks") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            continue
        num = raw["number"]
        blocks.append(
            BlockRow(
                number=num,
                name=str(raw.get("name", "")),
                status=str(raw.get("status", "pending")),
                review_iterations=int(raw.get("review_iterations") or 0),
                timed_out=bool(raw.get("timed_out")),
                fix_pass=raw.get("fix_pass") is not None,
                test_commit=raw.get("test_commit"),
                impl_commit=raw.get("impl_commit"),
                refactor_commit=raw.get("refactor_commit"),
                red_evidence=str(raw.get("red_evidence") or ""),
                green_evidence=str(raw.get("green_evidence") or ""),
                review_rounds=[
                    r for r in reviews if r.startswith(f"reviews/block-{num:02d}-")
                ],
            )
        )
    started, updated = (
        iso_utc(state.get("started_at")),
        iso_utc(state.get("updated_at")),
    )
    s, u = parse_utc(started), parse_utc(updated)
    budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
    return BuildStatus(
        phase=state.get("phase"),
        complete=complete,
        started_at=started,
        updated_at=updated,
        branch=state.get("branch"),
        base_sha=state.get("base_sha"),
        pr_url=state.get("pr_url"),
        failed_stop=state.get("failed_stop") is not None,
        spent_usd=_float(budget.get("cost_usd")),
        max_usd=_float(budget.get("max_usd")),
        blocks=blocks,
        blocks_done=sum(1 for x in blocks if x.status == "done"),
        blocks_total=len(blocks),
        elapsed_s=(u - s).total_seconds() if s and u else None,
        board=_json(d / ".board.json"),
        progress_tail=_tail(b.checkout / "build-progress.md"),
    )


def _epic_root(
    place: Place, epic_dir_for: Callable[[Path, str], Path] | None
) -> Path | None:
    if epic_dir_for is None:
        return None
    try:
        return Path(epic_dir_for(place.repo, place.name))
    except Exception:  # noqa: BLE001 - no data dir means no epic records to show
        return None


def latest_summary(root: Path) -> tuple[str, dict, Path] | None:
    """(run, summary, its path) of the epic's latest readable `runs/NN/summary.json`."""
    try:
        nums = sorted(
            (p for p in (root / "runs").iterdir() if p.name.isdigit()),
            key=lambda p: int(p.name),
        )
    except OSError:
        return None
    for p in reversed(nums):
        summary = _json(p / "summary.json")
        if summary is not None:
            return p.name, summary, p / "summary.json"
    return None


def epic_status(
    place: Place, epic_dir_for: Callable[[Path, str], Path] | None
) -> EpicStatus | None:
    root = _epic_root(place, epic_dir_for)
    latest = latest_summary(root) if root is not None else None
    if latest is None:
        return None
    run, summary, _ = latest
    children = []
    for c in summary.get("children") or []:
        if isinstance(c, dict) and c.get("id"):
            children.append(
                EpicChildRow(
                    id=str(c["id"]),
                    status=str(c.get("status", "")),
                    pr=c.get("pr"),
                    rounds=int(c.get("rounds") or 0),
                    reason=str(c.get("reason") or ""),
                )
            )
    agents = []
    try:
        started_files = sorted((root / "agents").glob("*.started.json"))
    except OSError:
        started_files = []
    for f in started_files:
        head, _, rest = f.name.partition("-")
        if not head.isdigit():
            continue
        started = _json(f) or {}
        result = _json(f.with_name(f.name.replace(".started.json", ".result.json")))
        kind = str(started.get("kind") or rest.split("-", 1)[0])
        agents.append(
            EpicAgent(
                n=int(head),
                kind=kind,
                child=str(started.get("child") or rest.removesuffix(".started.json")),
                started_at=iso_utc(started.get("at") or started.get("started_at")),
                exit_code=result.get("exit_code") if result else None,
                finished=result is not None,
            )
        )
    totals = {
        k: v for k, v in (summary.get("totals") or {}).items() if isinstance(v, int)
    }
    return EpicStatus(
        run=run,
        children=children,
        totals=totals,
        spent_usd=_float(summary.get("spent_usd")),
        agents=agents,
    )


# --- scorecard -------------------------------------------------------------------

# A child's status at the end of a run → what the scorecard counts it as.
# `skipped` (a dependency parked) and a `dependency` cause are a consequence
# of another child, counted apart from the causes.
STOPPED_AS = {"parked": "parked", "failed": "failed", "unknown": "failed"}
BLOCKED_STATUS = "skipped"
BLOCKED_CAUSE = "dependency"
BUILDME_SKILL = ":buildme "


class ScorecardSources(NamedTuple):
    """What `scorecard` reads (`buildme.scorecard_sources` builds it)."""

    repo: str
    data_dir: Path
    runs_file: Path
    epic_dirs: list[Path]
    cause_of: Callable[[str, str], str | None]


def _version_key(v: str | None) -> tuple:
    if v is None:
        return (0, ())
    try:
        return (2, tuple(int(x) for x in v.split(".")))
    except ValueError:
        return (1, (v,))


def _rel(path: Path, data_dir: Path) -> str:
    try:
        return path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(path)


def _epic_name(epic_dir: Path) -> str:
    name = (_json(epic_dir / "epic.json") or {}).get("name")
    return str(name) if isinstance(name, str) and name else epic_dir.name


def _child_cause(c: dict, cause_of) -> tuple[str | None, bool]:
    """(cause, inferred): recorded when the child carries `cause`, else read
    from its reason text by buildme's own patterns."""
    status, reason = str(c.get("status", "")), str(c.get("reason") or "")
    if c.get("cause") is not None:
        return str(c["cause"]), False
    try:
        return cause_of(status, reason), True
    except Exception:  # noqa: BLE001 - an unreadable reason is no cause
        return None, True


def _epic_entries(src: ScorecardSources) -> tuple[list[dict], list[dict], int]:
    """(epic runs, single builds, rows skipped) of the repo, each run a dict
    for `_scorecard_run`. A run with a row is read from the row; the others
    from their summary."""
    rows, skipped = runs_mod.read_rows(src.runs_file)
    epics: dict[tuple[str, str], dict] = {}
    builds: list[dict] = []
    for row in rows:
        producer = str(row.get("producer") or "")
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        run = row.get("run") if isinstance(row.get("run"), dict) else {}
        if BUILDME_SKILL not in producer or target.get("repo") != src.repo:
            continue
        entry = {
            "source": "row",
            "version": producer.rsplit(" ", 1)[-1],
            "epic": str(target.get("change") or ""),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "outcome": row.get("outcome"),
            "cause": row.get("cause"),
            "spend": _float(row.get("spend_usd")),
            "total": _float(run.get("spent_usd_total")),
            "spend_complete": run.get("spend_complete") is not False,
            "children": [c for c in run.get("children") or [] if isinstance(c, dict)],
        }
        if row.get("kind") == "build":
            builds.append({**entry, "kind": "build", "run": None, "folder": None})
        elif row.get("kind") == "epic" and row.get("folder") and run.get("run"):
            folder = Path(str(row["folder"]))
            folder = folder if folder.is_absolute() else src.data_dir / folder
            key = (_rel(folder, src.data_dir), str(run["run"]))
            epics[key] = {**entry, "kind": "epic", "folder": key[0], "run": key[1]}
    for d in src.epic_dirs:
        folder, name = _rel(d, src.data_dir), _epic_name(d)
        try:
            nums = [p for p in (d / "runs").iterdir() if p.name.isdigit()]
        except OSError:
            continue
        for p in nums:
            if (folder, p.name) in epics:
                continue
            summary = _json(p / "summary.json")
            if summary is None:
                # A run that stopped before writing its summary (killed), or
                # whose summary cannot be read: a run, with nothing known.
                epics[(folder, p.name)] = {
                    "source": "run-folder",
                    "kind": "epic",
                    "version": None,
                    "epic": name,
                    "folder": folder,
                    "run": p.name,
                    "started_at": None,
                    "finished_at": None,
                    "outcome": None,
                    "cause": None,
                    "spend": None,
                    "total": None,
                    "spend_complete": False,
                    "children": [],
                }
                continue
            version = summary.get("buildme_version")
            epics[(folder, p.name)] = {
                "source": "summary",
                "kind": "epic",
                "version": str(version) if version else None,
                "epic": name,
                "folder": folder,
                "run": p.name,
                "started_at": summary.get("started_at"),
                "finished_at": summary.get("finished_at"),
                "outcome": None,
                "cause": None,
                "spend": None,
                "total": _float(summary.get("spent_usd")),
                "spend_complete": summary.get("spend_complete") is not False,
                "children": [
                    c for c in summary.get("children") or [] if isinstance(c, dict)
                ],
            }
    return list(epics.values()), builds, skipped


def _scorecard_run(e: dict, prev: dict | None, cause_of) -> ScorecardRun:
    """One run, its spend and its children compared with the epic's last
    earlier run that has a summary or row (*prev*): merged means merged in
    this run."""
    before = {
        str(c.get("id")): (c.get("status"), c.get("reason") or "")
        for c in (prev or {}).get("children", [])
    }
    spend = e["spend"]
    if e["source"] == "summary" and e["total"] is not None:
        if prev is None:
            spend = e["total"]
        elif prev["total"] is not None:
            spend = round(e["total"] - prev["total"], 6)
    merged, stopped = [], []
    for c in e["children"]:
        cid, st = str(c.get("id") or ""), str(c.get("status", ""))
        if not cid:
            continue
        if st == "merged":
            if before.get(cid, (None,))[0] != "merged":
                merged.append(cid)
            continue
        if st not in STOPPED_AS and st != BLOCKED_STATUS:
            continue
        cause, inferred = _child_cause(c, cause_of)
        stopped.append(
            ScorecardChild(
                id=cid,
                status=st,
                cause=cause,
                inferred=inferred,
                reason=str(c.get("reason") or ""),
                pr=c.get("pr"),
                same_as_previous_run=before.get(cid) == (st, c.get("reason") or ""),
            )
        )
    if e["kind"] == "build" and e["outcome"] == "failed":
        stopped.append(ScorecardChild(id=e["epic"], status="failed", cause=e["cause"]))
    finished = iso_utc(e["finished_at"])
    run_id = (
        f"{e['folder']}#{e['run']}"
        if e["kind"] == "epic"
        else f"build:{e['epic']}:{finished or ''}"
    )
    return ScorecardRun(
        id=run_id,
        kind=e["kind"],
        epic=e["epic"],
        run=e["run"],
        version=e["version"],
        source=e["source"],
        folder=e["folder"],
        started_at=iso_utc(e["started_at"]),
        finished_at=finished,
        outcome=e["outcome"],
        cause=e["cause"],
        spend_usd=spend,
        spend_complete=e["spend_complete"] and spend is not None,
        merged=merged,
        stopped=stopped,
    )


def scorecard(src: ScorecardSources) -> Scorecard:
    """For each buildme version, the repo's epic runs and single builds, the
    children merged, parked, failed and held back by another child, the
    spend, and the parks and failures by cause; each count lists the runs
    (and children) it came from.

    Runs come from the shared `runs.jsonl` (rows of `multiplai_core.runs`,
    `target.repo` this repo), and for epic runs without a row from every
    `runs/NN/summary.json` of the old and new epic folders. A summary
    without `buildme_version` is of version null, and its causes are read
    from the reason text (`inferred`). A summary holds the epic's state at
    the end of the run, so a child counts in the run that merged it, and a
    child parked or failed counts again only when its status or reason
    changed since the epic's previous run."""
    epics, builds, skipped = _epic_entries(src)
    by_folder: dict[str, list[dict]] = {}
    for e in epics:
        by_folder.setdefault(e["folder"], []).append(e)
    out: list[ScorecardRun] = []
    for entries in by_folder.values():
        entries.sort(key=lambda e: int(e["run"]))
        prev = None
        for e in entries:
            out.append(_scorecard_run(e, prev, src.cause_of))
            if e["source"] != "run-folder":
                prev = e
    out += [_scorecard_run(b, None, src.cause_of) for b in builds]
    out.sort(
        key=lambda r: (r.finished_at or "", int(r.run) if r.run else 0), reverse=True
    )
    versions: dict[str | None, ScorecardVersion] = {}
    causes: dict[str | None, dict[str, CauseCount]] = {}
    for r in out:
        v = versions.setdefault(r.version, ScorecardVersion(version=r.version))
        v.runs.count += 1
        v.runs.refs.append(RunRef(run=r.id))
        if r.spend_usd is not None:
            v.spend_usd = round(v.spend_usd + r.spend_usd, 6)
        v.spend_complete = v.spend_complete and r.spend_complete
        for cid in r.merged:
            v.merged.count += 1
            v.merged.refs.append(RunRef(run=r.id, child=cid))
        for c in r.stopped:
            if c.same_as_previous_run:
                continue
            ref = RunRef(run=r.id, child=c.id)
            if c.status == BLOCKED_STATUS or c.cause == BLOCKED_CAUSE:
                v.blocked.count += 1
                v.blocked.refs.append(ref)
                continue
            bucket = v.parked if STOPPED_AS[c.status] == "parked" else v.failed
            bucket.count += 1
            bucket.refs.append(ref)
            key = c.cause or "other"
            cc = causes.setdefault(r.version, {}).setdefault(key, CauseCount(cause=key))
            cc.count += 1
            cc.inferred += int(c.inferred)
            cc.refs.append(ref)
    for version, v in versions.items():
        v.causes = sorted(
            causes.get(version, {}).values(), key=lambda c: (-c.count, c.cause)
        )
    return Scorecard(
        repo=src.repo,
        versions=sorted(
            versions.values(), key=lambda v: _version_key(v.version), reverse=True
        ),
        runs=out,
        skipped_rows=skipped,
    )


# --- stops -----------------------------------------------------------------------


def _record(c: gitio.Commit) -> Record | None:
    try:
        stop = int(c.trailers.get("stop", ""))
    except ValueError:
        return None
    for key, kind in TRAILER_KINDS.items():
        if key in c.trailers:
            return Record(
                sha=c.sha,
                kind=kind,
                stop=stop,
                by=c.trailers[key],
                at=iso_utc(c.at) or c.at,
                subject=c.subject,
                revision=c.trailers.get("revision"),
                reason=c.trailers.get("reason"),
            )
    return None


def records(place: Place) -> list[Record]:
    checkouts = [c.checkout for c in (place.write, place.feature()) if c is not None]
    out: dict[str, Record] = {}
    for cwd in dict.fromkeys(checkouts):
        for c in gitio.review_commits(cwd, place.name):
            r = _record(c)
            if r is not None:
                out.setdefault(r.sha, r)
    return list(out.values())


def stop_revision(
    n: int, place: Place, docs: list[DocRef], build: dict | None
) -> str | None:
    if n == 4:
        plan_dir = change_plan_dir(place)
        if plan_dir is not None:
            return plan_request_revision(plan_dir)
        sg = (build or {}).get("spec_gen") or {}
        return sg.get("revision") or None
    if n == 5 or place.write is None:
        return None
    rel = (CHANGES / place.name).as_posix()
    return gitio.tree_revision(
        place.write.checkout,
        [f"{rel}/{d.path}" for d in docs if d.approve and d.exists],
    )


def _reached_and_past(
    n: int,
    place: Place,
    pm: PmSpecStatus | None,
    build: dict | None,
    change_dir: Path,
    revision: str | None,
) -> tuple[bool, bool]:
    if n in STAGE_STEPS:
        steps = pm.steps if pm else {}
        own = steps.get(STAGE_STEPS[n])
        later = any(s in steps for s in LATER_STEPS[n]) or bool(
            pm and pm.handoff_revision
        )
        ready = bool(pm and pm.spec_status == "ready")
        if pm is not None and steps:
            reached = own in ("passed", "skipped") or later or ready
            past = later or ready or own == "skipped"
            return reached, past
        # No run state here: judge by the committed documents.
        reached = revision is not None
        later_docs = {
            1: ("use-cases.md", "components.md"),
            2: ("specs.md", "tickets.json"),
            3: (),
        }
        past = (
            ready
            or any((change_dir / p).is_file() for p in later_docs[n])
            or build is not None
        )
        return reached or past, past
    if n == 4:
        plan_dir = change_plan_dir(place)
        if plan_dir is not None:
            request = (plan_dir / PLAN_REQUEST).is_file()
            plan = (plan_dir / plan_doc(plan_dir)).is_file()
            return request or plan, plan and not request
        if build is None:
            return False, False
        approval = build.get("plan_approval")
        approved = (
            isinstance(approval, dict)
            and approval.get("revision") == revision
            and revision
        )
        past = bool(approved) or build.get("phase") in PAST_PLAN_PHASES
        b = place.build()
        request = b is not None and (b.change_dir(place.name) / PLAN_REQUEST).is_file()
        return bool(request or past), past
    return bool(build and build.get("pr_url")), False


def _waiting_since(
    n: int, place: Place, docs: list[DocRef], build: dict | None
) -> str | None:
    if n in STAGE_STEPS and place.write is not None:
        rel = (CHANGES / place.name).as_posix()
        return iso_utc(
            gitio.last_commit_time(
                place.write.checkout,
                [f"{rel}/{d.path}" for d in docs if d.approve and d.exists],
            )
        )
    b = place.build()
    plan_dir = change_plan_dir(place) if n == 4 else None
    if n == 4 and (b is not None or plan_dir is not None):
        d = plan_dir if plan_dir is not None else b.change_dir(place.name)
        try:
            return iso_utc((d / PLAN_REQUEST).stat().st_mtime)
        except OSError:
            pass
    return iso_utc((build or {}).get("updated_at"))


def _role_of(
    name: str, cfg: ViewerConfig, people: Mapping[str, Person], roles: tuple[str, ...]
) -> str | None:
    for role in roles:
        if name in cfg.reviewers.get(role, []):
            return role
    for p in people.values():
        if p.name == name:
            return p.role
    return None


def stop_status(
    n: int,
    place: Place,
    cfg: ViewerConfig,
    people: Mapping[str, Person],
    pm: PmSpecStatus | None,
    build: dict | None,
    recs: list[Record],
) -> StopStatus:
    change_dir = place.read_dir()
    if n == 4 and place.build() is not None:
        change_dir = place.build().change_dir(place.name)
    elif n == 4 and (plan_dir := change_plan_dir(place)) is not None:
        change_dir = plan_dir
    docs = stop_docs(n, change_dir)
    revision = stop_revision(n, place, docs, build)
    reached, past = _reached_and_past(n, place, pm, build, change_dir, revision)
    stop_cfg = cfg.stop(n)
    reviewers = cfg.reviewers_of(n, people)
    mine = [r for r in recs if r.stop == n]
    current = [r for r in mine if n == 5 or r.revision == revision]
    status = StopStatus(
        n=n,
        label=STOP_LABELS[n],
        state="not_reached",
        reviewers=reviewers,
        docs=docs,
        records=mine,
        revision=revision if reached else None,
        pr_url=(build or {}).get("pr_url") if n == 5 else None,
    )
    if not reached:
        return status

    def names(kind: str) -> list[str]:
        return list(dict.fromkeys(r.by for r in current if r.kind == kind))

    status.approved_by = names("approve")
    status.acknowledged_by = names("ack")
    status.rework_by = names("rework")
    decided = [r for r in current if r.kind == "decided"]
    status.decided_by = decided[-1].by if decided else None
    approved_roles = {
        _role_of(name, cfg, people, stop_cfg.roles) for name in status.approved_by
    }
    fully_approved = bool(status.approved_by) and all(
        role in approved_roles for role in stop_cfg.roles
    )
    if n != 5 and fully_approved:
        status.state = "approved"
    elif n != 5 and decided:
        status.state = "decided_by_agent"
    elif n != 5 and status.rework_by:
        status.state = "rework_requested"
    elif past:
        status.state = "approved"
    elif status.acknowledged_by:
        status.state = "acknowledged"
    else:
        status.state = "waiting"
    if status.state in ("waiting", "acknowledged"):
        status.waiting_on = [
            r.name
            for r in reviewers
            if r.role not in approved_roles and r.name not in status.approved_by
        ]
        status.waiting_since = _waiting_since(n, place, docs, build)
        since = parse_utc(status.waiting_since)
        if since is not None and n != 5:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            status.ack_by = (
                since + timedelta(minutes=stop_cfg.ack_timeout_minutes)
            ).strftime(fmt)
            if status.state == "waiting":
                status.decide_at = (
                    since + timedelta(minutes=stop_cfg.decide_timeout_minutes)
                ).strftime(fmt)
    return status


def change_status(
    place: Place,
    cfg: ViewerConfig,
    people: Mapping[str, Person],
    epic_dir_for: Callable[[Path, str], Path] | None = None,
) -> ChangeStatus:
    pm = pm_spec_status(place)
    build, _ = build_state(place)
    recs = records(place)
    stops = [stop_status(n, place, cfg, people, pm, build, recs) for n in range(1, 6)]
    reached = [s for s in stops if s.state != "not_reached"]
    open_ = [s for s in reached if s.state not in ("approved", "decided_by_agent")]
    current = open_[0] if open_ else (reached[-1] if reached else None)
    unpushed = False
    if place.write is not None:
        up = gitio.upstream(place.write.checkout, place.branch)
        unpushed = gitio.ahead(place.write.checkout, up) > 0
    return ChangeStatus(
        name=place.name,
        branch=place.branch,
        current_stop=current.n if current else None,
        waiting_on=current.waiting_on if current else [],
        stops=stops,
        pm_spec=pm,
        build=build_status(place),
        epic=epic_status(place, epic_dir_for),
        unpushed=unpushed,
        writable=place.write is not None,
    )


# --- needs you -------------------------------------------------------------------

# Parks a PM answers (`pm-spec revise`); every other park waits on an engineer.
PM_CAUSES = frozenset({"spec-issues", "spec-not-ready"})
NEED_STOPS = (1, 2, 3, 4)  # stop 5 is approved on GitHub; its threads are read there


def people_of(
    role: str, cfg: ViewerConfig, people: Mapping[str, Person]
) -> list[Reviewer]:
    """The names config lists for *role*, else the people.json people of that role."""
    known = {p.name for p in people.values()}
    names = list(cfg.reviewers.get(role, [])) or [
        p.name for p in people.values() if p.role == role
    ]
    return [
        Reviewer(name=n, role=role, invited=n in known) for n in dict.fromkeys(names)
    ]


def _waited(since: str | None, now: datetime) -> float | None:
    t = parse_utc(since)
    return (now - t).total_seconds() if t is not None else None


def _last_review(root: Path, child: str) -> dict | None:
    """The result of the child's latest review agent (`agents/NNN-review-<child>`)."""
    best: tuple[int, Path] | None = None
    try:
        files = list((root / "agents").glob("*-review-*.started.json"))
    except OSError:
        return None
    for f in files:
        head = f.name.split("-", 1)[0]
        if not head.isdigit() or (_json(f) or {}).get("child") != child:
            continue
        if best is None or int(head) > best[0]:
            best = (int(head), f)
    if best is None:
        return None
    return _json(
        best[1].with_name(best[1].name.replace(".started.json", ".result.json"))
    )


def _open_issues(place: Place) -> Path | None:
    """The open `spec-issues.md` that a copy's `.change.yaml` `spec_issues`
    names (buildme's `readiness.open_issues`), first found."""
    from multiplai_core.specs import read_change_meta

    for d in dict.fromkeys(
        [place.read_dir()] + [c.change_dir(place.name) for c in place.copies]
    ):
        try:
            value = read_change_meta(d).get("spec_issues")
        except (OSError, UnicodeError, ValueError):
            continue
        if not value:
            continue
        path = Path(str(value))
        path = path if path.is_absolute() else d / path
        if path.is_file():
            return path
    return None


def _children(root: Path | None) -> tuple[list[dict], str | None]:
    """The latest summary's children and when that run ended (its
    `finished_at`, else the file's time)."""
    latest = latest_summary(root) if root is not None else None
    if latest is None:
        return [], None
    _, summary, path = latest
    since = iso_utc(summary.get("finished_at"))
    if since is None:
        try:
            since = iso_utc(path.stat().st_mtime)
        except OSError:
            since = None
    kids = [c for c in summary.get("children") or [] if isinstance(c, dict)]
    return [c for c in kids if c.get("id")], since


def child_prs(
    items: list[tuple[Place, ChangeStatus]],
    epic_dir_for: Callable[[Path, str], Path] | None,
) -> list[tuple[str, str, str]]:
    """(change, child, pr) of every child PR not merged in each change's latest
    epic summary: the PRs whose review threads `github.add_thread_rows` reads."""
    out = []
    for place, _ in items:
        kids, _since = _children(_epic_root(place, epic_dir_for))
        for c in kids:
            if c.get("pr") and c.get("status") != "merged":
                out.append((place.name, str(c["id"]), str(c["pr"])))
    return out


def needs_you(
    items: list[tuple[Place, ChangeStatus]],
    cfg: ViewerConfig,
    people: Mapping[str, Person],
    *,
    epic_dir_for: Callable[[Path, str], Path] | None,
    cause_of: Callable[[str, str], str | None] | None,
    merge_mode: Callable[[Path, str | None], str] | None,
    now: datetime,
) -> NeedsYou:
    """Everything in this repo waiting on a person, oldest first, from files
    only (GitHub's review threads are `github.add_thread_rows`, a separate
    step):

    - a stop 1-4 `waiting` or `acknowledged` (`stop_status`), on its `waiting_on`;
    - a child `parked` in the change's latest epic summary, with its cause (a
      summary from before causes were recorded: read from `reason` by
      *cause_of*, `inferred`), on the PMs for a spec cause, else the engineers;
    - a child `built` with a PR whose latest review agent finished with exit 0
      and no blocking finding, when the epic merges by hand (*merge_mode*
      `manual`): buildme waits on it with `EPIC:MERGE_WAITING`. This reads
      only the records, so it stands in for buildme's own check (the review
      was of the PR's current head);
    - an open `spec-issues.md` (`.change.yaml` `spec_issues`), on the PMs.
    """
    rows: list[NeedsYouRow] = []
    engineers = people_of("engineer", cfg, people)
    pms = people_of("pm", cfg, people)
    for place, st in items:
        for s in st.stops:
            if s.n not in NEED_STOPS or s.state not in ("waiting", "acknowledged"):
                continue
            rows.append(
                NeedsYouRow(
                    kind="stop",
                    change=place.name,
                    stop=s.n,
                    what=f"Stop {s.n} of {place.name} ({s.label}) waits for approval",
                    waiting_on=[r for r in s.reviewers if r.name in s.waiting_on],
                    since=s.waiting_since,
                    waited_s=_waited(s.waiting_since, now),
                    link=f"#change={place.name}&stop={s.n}",
                )
            )
        root = _epic_root(place, epic_dir_for)
        kids, since = _children(root)
        mode = None
        for c in kids:
            cid, cstatus = str(c["id"]), str(c.get("status", ""))
            reason = str(c.get("reason") or "")
            pr = str(c["pr"]) if c.get("pr") else None
            if cstatus == "parked":
                cause, inferred = c.get("cause"), False
                if cause is None and cause_of is not None:
                    cause, inferred = cause_of(cstatus, reason), True
                rows.append(
                    NeedsYouRow(
                        kind="parked",
                        change=place.name,
                        child=cid,
                        what=f"{cid} of {place.name} is parked",
                        waiting_on=pms if cause in PM_CAUSES else engineers,
                        since=since,
                        waited_s=_waited(since, now),
                        link=f"#change={place.name}",
                        url=pr,
                        cause=cause,
                        inferred=inferred,
                        reason=reason,
                    )
                )
            elif cstatus == "built" and pr and merge_mode is not None:
                if mode is None:
                    mode = merge_mode(place.repo, place.branch)
                last = _last_review(root, cid) if mode == "manual" else None
                if last and last.get("exit_code") == 0 and not last.get("blocking"):
                    rows.append(
                        NeedsYouRow(
                            kind="merge",
                            change=place.name,
                            child=cid,
                            what=f"{cid}'s pull request passed review and waits for a person to merge it",
                            waiting_on=engineers,
                            since=since,
                            waited_s=_waited(since, now),
                            link=f"#change={place.name}",
                            url=pr,
                        )
                    )
        issues = _open_issues(place)
        if issues is not None:
            try:
                at = iso_utc(issues.stat().st_mtime)
            except OSError:
                at = None
            rows.append(
                NeedsYouRow(
                    kind="spec-issues",
                    change=place.name,
                    what=f"spec-issues.md of {place.name} waits for /multiplai-pm:pm-spec revise",
                    waiting_on=pms,
                    since=at,
                    waited_s=_waited(at, now),
                    link=f"#change={place.name}",
                )
            )
    return NeedsYou(rows=sort_needs(rows))


def sort_needs(rows: list[NeedsYouRow]) -> list[NeedsYouRow]:
    """Oldest wait first; rows with no time last."""
    return sorted(rows, key=lambda r: (r.since is None, r.since or "", r.change))
