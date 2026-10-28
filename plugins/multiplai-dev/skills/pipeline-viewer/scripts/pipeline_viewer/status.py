"""Files and git history → `ChangeStatus`. Pure: no git writes, no network,
no model calls. Every reader is defensive: a missing or unreadable file is an
empty section, never an error.

Where each stop's state comes from (all for the stop's current `revision`):

- **Reached.** Stops 1-3: pm-spec's `.pm-spec/state.json` steps `proposal`,
  `components`, `specs` recorded `passed` (pm-spec records a review stop once
  pushed). Without a run state (pm-spec ran elsewhere), the stop's documents
  being committed. Stop 4: buildme's `plan-approval.md` in the build's copy,
  or a saved `plan_approval`. Stop 5: the build has a `pr_url`.
- **Passed without a commit here.** Stops 1-3: a later pm-spec step or the
  handoff exists. Stop 4: `plan_approval` for the current plan revision, or
  the build reached TDD (the gate is off). Such a stop is `approved` with
  `approved_by: []`.
- **Decisions.** The trailers of the change's `review(<name>): …` commits on
  the write checkout's HEAD (`record.py` writes them; buildme's
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

from viewer_kit.people import Person

from . import gitio
from .config import ViewerConfig
from .models import (
    STOP_LABELS,
    BlockRow,
    BuildStatus,
    ChangeStatus,
    DocRef,
    EpicAgent,
    EpicChildRow,
    EpicStatus,
    PmSpecStatus,
    Record,
    StopStatus,
)
from .places import BUILD_STATE, BUILD_STATE_DONE, CHANGES, PM_SPEC_DIR, Place

PLAN_DOC = "implementation-plan.md"
OLD_PLAN_DOC = "design.md"
PLAN_REQUEST = "plan-approval.md"
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
        return [
            ref(plan_doc(change_dir)),
            ref("tasks.md", approve=False),
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


def epic_status(
    place: Place, epic_dir_for: Callable[[Path, str], Path] | None
) -> EpicStatus | None:
    if epic_dir_for is None:
        return None
    try:
        root = Path(epic_dir_for(place.repo, place.name))
    except Exception:  # noqa: BLE001 - no data dir means no epic records to show
        return None
    runs = root / "runs"
    try:
        nums = sorted(
            (p for p in runs.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)
        )
    except OSError:
        return None
    summary = None
    run = None
    for p in reversed(nums):
        summary = _json(p / "summary.json")
        if summary is not None:
            run = p.name
            break
    if summary is None:
        return None
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
    if place.write is None:
        return []
    out = []
    for c in gitio.review_commits(place.write.checkout, place.name):
        r = _record(c)
        if r is not None:
            out.append(r)
    return out


def stop_revision(
    n: int, place: Place, docs: list[DocRef], build: dict | None
) -> str | None:
    if n == 4:
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
    if n == 4 and b is not None:
        try:
            return iso_utc((b.change_dir(place.name) / PLAN_REQUEST).stat().st_mtime)
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
