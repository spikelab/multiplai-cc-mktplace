"""The decision record: the only module that writes the repo.

Every write goes on the change's branch, in its write checkout
(`places.Place.write`: the build's worktree, else pm-spec's worktree, else
the repo when it has the branch checked out; else it refuses).

| Write | Commit |
|---|---|
| comment | appends to `specs/changes/<name>/review/stop-<n>.md` and commits only that file |
| acknowledge | empty commit, `Acknowledged-by` |
| approve | empty commit, `Approved-by` |
| rework | `write_issues` (stops 1-3) or `review/stop-4.md` (stop 4), then a commit of those files, `Rework-requested-by` |
| decided | empty commit, `Decided-by: agent`, `Reason` |

Each commit's subject starts `review(<name>):` and its trailers carry
`Stop: <n>` and `Revision: <the stop's revision>`. A person's commit names
them with `--author "<name> <email>"` from people.json; the committer is
whoever runs the server, and nothing is signed. Hooks run.

Order of a write (the plan's Stop and resume rows):

1. No write checkout → refuse. pm-spec's `run.lock` held → refuse (S4).
2. With an upstream: fetch, then fast-forward; a branch that has diverged
   from it → refuse (S6).
3. The caller re-reads the status (the pull may have changed the revision).
4. approve only: the stop's documents have uncommitted edits → refuse (S5).
5. The same record (kind, stop, revision, person) already on HEAD's history
   → no new commit (`already`): only the push runs (S3).
6. Commit, then push. A failed push keeps the commit; the status shows
   `unpushed` until a later write or `push()` pushes it (S3).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from viewer_kit.mailbox import utc_now
from viewer_kit.people import Person

from . import buildme, gitio
from .models import APPROVABLE_STOPS, ChangeStatus, StopStatus
from .places import CHANGES, Place
from .status import pm_spec_running

log = logging.getLogger(__name__)

# One write at a time per process: the server is threaded, and two commits
# racing on one checkout would fail on git's index lock.
_WRITE_LOCK = threading.Lock()
AGENT = "agent"


class Refused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class WriteResult:
    commit: str | None
    already: bool
    pushed: bool
    unpushed: bool
    push_error: str | None = None
    path: str | None = None

    def to_json(self) -> dict:
        return {
            "commit": self.commit,
            "already": self.already,
            "pushed": self.pushed,
            "unpushed": self.unpushed,
            "push_error": self.push_error,
        }


# --- the steps every write shares -----------------------------------------------


def _checkout(place: Place) -> Path:
    if place.write is None:
        raise Refused(
            f"no checkout holds branch {place.branch or '(unknown)'}; "
            "check it out or start pm-spec/buildme on it"
        )
    return place.write.checkout


def _remote_ref(cwd: Path, up: str) -> tuple[str, str]:
    """(remote, branch) of an upstream like `origin/feature-x`."""
    remotes = (gitio.ok(cwd, "remote") or "").split()
    for remote in sorted(remotes, key=len, reverse=True):
        if up.startswith(remote + "/"):
            return remote, up[len(remote) + 1 :]
    remote, _, branch = up.partition("/")
    return remote, branch


def prepare(place: Place) -> None:
    """Steps 1-2: refuse, or bring the checkout up to its upstream."""
    cwd = _checkout(place)
    if pm_spec_running(place):
        raise Refused(f"pm-spec is running on {place.name}; try again when it stops")
    up = gitio.upstream(cwd, place.branch)
    if not up:
        return
    remote, branch = _remote_ref(cwd, up)
    if gitio.ok(cwd, "fetch", "--quiet", remote, branch) is None:
        # Offline or no such remote branch yet: write locally; the push says why.
        log.warning("fetch of %s %s failed; writing without it", remote, branch)
        return
    if gitio.ok(cwd, "merge-base", "--is-ancestor", up, "HEAD") is not None:
        return  # up to date, or only ahead (an earlier push failed)
    if gitio.ok(cwd, "merge-base", "--is-ancestor", "HEAD", up) is None:
        raise Refused(
            f"branch {place.branch or branch} has diverged from origin; resolve it, then retry"
        )
    res = gitio.run(cwd, "merge", "--ff-only", "--quiet", up, check=False)
    if res.returncode != 0:
        raise Refused(f"cannot fast-forward to {up}: {res.stderr.strip()}")


def push(place: Place) -> WriteResult:
    """Push the write checkout (the "retry push" of row S3). Never commits."""
    cwd = _checkout(place)
    up = gitio.upstream(cwd, place.branch)
    if not up:
        return WriteResult(None, False, pushed=False, unpushed=False)
    if gitio.ahead(cwd, up) == 0:
        return WriteResult(None, False, pushed=True, unpushed=False)
    remote, branch = _remote_ref(cwd, up)
    res = gitio.run(
        cwd, "push", "--quiet", remote, f"HEAD:refs/heads/{branch}", check=False
    )
    if res.returncode != 0:
        err = (res.stderr.strip() or "git push failed").splitlines()[-1]
        log.warning("push of %s failed: %s", place.name, err)
        return WriteResult(None, False, pushed=False, unpushed=True, push_error=err)
    # A detached checkout's origin/<branch> moves only on a fetch.
    gitio.ok(cwd, "fetch", "--quiet", remote, branch)
    return WriteResult(None, False, pushed=True, unpushed=gitio.ahead(cwd, up) > 0)


def _stop(status: ChangeStatus, n: int) -> StopStatus:
    if n not in range(1, 6):
        raise Refused("stop must be 1-5", 400)
    return status.stops[n - 1]


def _must_be_reached(status: ChangeStatus, s: StopStatus) -> None:
    if s.state == "not_reached":
        raise Refused(f"stop {s.n} of {status.name} is not waiting")


def _must_review(s: StopStatus, person: Person, roles: tuple[str, ...]) -> None:
    named = [r.name for r in s.reviewers]
    if person.name in named:
        return
    if not named and person.role in roles:
        return
    raise Refused(f"{person.name} is not a reviewer of stop {s.n}", 403)


def _not_closed(s: StopStatus) -> None:
    if s.state == "approved":
        if s.approved_by:
            raise Refused(f"stop {s.n} already approved by {', '.join(s.approved_by)}")
        raise Refused(f"stop {s.n} is not waiting")
    if s.state == "decided_by_agent":
        raise Refused(f"stop {s.n} already decided by the agent")


def _existing(
    place: Place, trailer: str, n: int, revision: str | None, by: str
) -> str | None:
    for c in reversed(gitio.review_commits(_checkout(place), place.name)):
        t = c.trailers
        if (
            t.get("stop") == str(n)
            and t.get(trailer) == by
            and t.get("revision") == revision
        ):
            return c.sha
    return None


def _revision(s: StopStatus) -> list[tuple[str, str]]:
    return [("Revision", s.revision)] if s.revision else []


def _message(subject: str, trailers: list[tuple[str, str]]) -> str:
    lines = [subject, ""] + [f"{k}: {v}" for k, v in trailers]
    return "\n".join(lines) + "\n"


def _author(person: Person | None) -> list[str]:
    return ["--author", f"{person.name} <{person.email}>"] if person else []


def _commit(place: Place, message: str, person: Person | None, paths: list[str]) -> str:
    cwd = _checkout(place)
    if paths:
        gitio.run(cwd, "add", "--", *paths)
        args = [
            "commit",
            "--quiet",
            "--only",
            "-m",
            message,
            *_author(person),
            "--",
            *paths,
        ]
    else:
        args = [
            "commit",
            "--quiet",
            "--only",
            "--allow-empty",
            "-m",
            message,
            *_author(person),
        ]
    res = gitio.run(cwd, *args, check=False)
    if res.returncode != 0:
        raise Refused(f"git commit failed: {(res.stderr or res.stdout).strip()}")
    return gitio.run(cwd, "rev-parse", "HEAD").stdout.strip()


def _finish(
    place: Place, sha: str, already: bool, path: str | None = None
) -> WriteResult:
    p = push(place)
    return WriteResult(sha, already, p.pushed, p.unpushed, p.push_error, path)


def _rel(place: Place, *parts: str) -> str:
    return "/".join([(CHANGES / place.name).as_posix(), *parts])


def _decision(
    place: Place,
    status_fn: Callable[[], ChangeStatus],
    n: int,
    trailer: str,
    by: str,
    person: Person | None,
    subject: str,
    check: Callable[[ChangeStatus, StopStatus], None],
    extra: list[tuple[str, str]] = (),
    files: Callable[[StopStatus], list[str]] | None = None,
) -> WriteResult:
    with _WRITE_LOCK:
        prepare(place)
        status = status_fn()
        s = _stop(status, n)
        _must_be_reached(status, s)
        found = _existing(place, trailer.lower(), n, s.revision, by)
        if found:
            return _finish(place, found, True)
        check(status, s)
        paths = files(s) if files else []
        trailers = [("Stop", str(n)), (trailer, by), *_revision(s), *extra]
        sha = _commit(place, _message(subject, trailers), person, paths)
        return _finish(place, sha, False, paths[0] if paths else None)


# --- the writes ------------------------------------------------------------------


def acknowledge(
    place: Place, status_fn, person: Person, n: int, roles: tuple[str, ...]
) -> WriteResult:
    def check(status, s):
        _must_review(s, person, roles)
        if s.state not in ("waiting", "acknowledged"):
            _not_closed(s)
            raise Refused(f"stop {n} of {status.name} is not waiting")

    return _decision(
        place,
        status_fn,
        n,
        "Acknowledged-by",
        person.name,
        person,
        f"review({place.name}): {person.name} is reviewing stop {n}",
        check,
    )


def approve(
    place: Place, status_fn, person: Person, n: int, roles: tuple[str, ...]
) -> WriteResult:
    if n not in APPROVABLE_STOPS:
        raise Refused(f"stop {n} is approved on the pull request")

    def check(status, s):
        _must_review(s, person, roles)
        _not_closed(s)
        docs = [_rel(place, d.path) for d in s.docs if d.approve]
        dirty = gitio.dirty(_checkout(place), docs)
        if dirty:
            raise Refused(
                f"{', '.join(dirty)} have uncommitted edits; commit or discard them first"
            )

    return _decision(
        place,
        status_fn,
        n,
        "Approved-by",
        person.name,
        person,
        f"review({place.name}): {person.name} approved stop {n}",
        check,
    )


def decided(place: Place, status_fn, n: int, reason: str) -> WriteResult:
    """The agent decided stop *n* after the timeout (`record decided`)."""
    if n not in APPROVABLE_STOPS:
        raise Refused(f"stop {n} is approved on the pull request")

    def check(status, s):
        _not_closed(s)

    return _decision(
        place,
        status_fn,
        n,
        "Decided-by",
        AGENT,
        None,
        f"review({place.name}): the agent decided stop {n}",
        check,
        extra=[("Reason", " ".join(reason.split()))],
    )


def rework(
    place: Place,
    status_fn,
    person: Person,
    n: int,
    roles: tuple[str, ...],
    title: str,
    text: str,
) -> WriteResult:
    if n not in APPROVABLE_STOPS:
        raise Refused(f"stop {n} is approved on the pull request")
    change_dir = _checkout(place) / CHANGES / place.name

    def check(status, s):
        _must_review(s, person, roles)
        _not_closed(s)

    def files(s: StopStatus) -> list[str]:
        body = f"{text.strip()}\n\nRequested by {person.name} on the stop {n} review (revision {s.revision})."
        if n == 4:
            _append_review(
                change_dir, n, place.name, f"Rework requested: {title}", person, body
            )
            return [_rel(place, "review", f"stop-{n}.md")]
        buildme.write_issues(change_dir, [(title, body)], change=place.name)
        paths = [_rel(place, "spec-issues.md")]
        if (change_dir / ".change.yaml").is_file():
            paths.append(_rel(place, ".change.yaml"))
        return paths

    return _decision(
        place,
        status_fn,
        n,
        "Rework-requested-by",
        person.name,
        person,
        f"review({place.name}): {person.name} asked for rework at stop {n}",
        check,
        files=files,
    )


def _append_review(
    change_dir: Path, n: int, name: str, heading: str, person: Person, text: str
) -> Path:
    path = change_dir / "review" / f"stop-{n}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        path.read_text(encoding="utf-8")
        if path.is_file()
        else f"# Review of stop {n}: {name}\n"
    )
    block = f"\n## {' '.join(heading.split())}\n\n{person.name}, {utc_now()}:\n\n{text.strip()}\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
    tmp.replace(path)
    return path


def comment(
    place: Place, status_fn, person: Person, n: int, doc: str, section: str, text: str
) -> WriteResult:
    """Comments are never idempotent: each one is new text."""
    with _WRITE_LOCK:
        prepare(place)
        status = status_fn()
        s = _stop(status, n)
        _must_be_reached(status, s)
        change_dir = _checkout(place) / CHANGES / place.name
        _append_review(change_dir, n, place.name, f"{doc} — {section}", person, text)
        rel = _rel(place, "review", f"stop-{n}.md")
        short = " ".join(section.split())[:80].replace('"', "'")
        message = _message(
            f'review({place.name}): comment on stop {n} section "{short}"',
            [("Stop", str(n)), ("Commented-by", person.name), *_revision(s)],
        )
        sha = _commit(place, message, person, [rel])
        return _finish(place, sha, False, rel)
