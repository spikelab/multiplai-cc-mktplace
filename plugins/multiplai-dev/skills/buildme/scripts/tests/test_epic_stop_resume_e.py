"""Row tests for PR E1's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`: E.S2, E.S5-E.S7,
E.S12, E.S18, E.S21, E.S22, E.S23 and E.S26, which the PR adds; E.S3, E.S4 and
E.S28 come with item 40.3's merge agent; E.S17 is E2's).

Each case stops the coordinator at the row's point (an exception at a seam, a
signal, or a forked child exiting 137), resumes, and compares with a run that
never stopped, allowing only what the row's "Resume does" names, or, for a
refusal, checks the worktree is as it was (`row_helpers.repo_state`). Fakes
as in `test_epic_merge`.
"""

from __future__ import annotations

import json
import os
import signal

import pytest

from build_pipeline.epic import coordinator, merge
from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    commit_names,
    epic_snapshot,
    last_summary,
    merge_waiting,
    name_commits,
    pr_url,
    records_snapshot,
    run_coordinator,
    run_killed,
    run_signalled,
)
from .row_helpers import assert_same_as_unstopped, repo_state
from .test_epic_merge import MergeRoot, _stop_after_the_merge, git, push_files

TESTS = ["fake-tests"]
MERGE_KINDS = ("merge", "merge-tests")


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _roots(tmp_path, monkeypatch, **kw) -> tuple[MergeRoot, MergeRoot]:
    return MergeRoot(tmp_path / "stopped", monkeypatch, **kw), MergeRoot(tmp_path / "clean", monkeypatch, **kw)


def _snapshot(r: MergeRoot) -> dict:
    names = commit_names([r.root / "o" / "r.git", r.repo])
    recs = records_snapshot(r.dir, r.root)
    return name_commits({"epic": epic_snapshot(r.dir, r.root),
                         "records": {cid: [x for x in rs if x[0] not in MERGE_KINDS] for cid, rs in recs.items()},
                         "merges": [(x[0], x[1], x[2], x[4]) for x in recs.get("a", []) if x[0] in MERGE_KINDS],
                         "summary": last_summary(r.dir, r.root)}, names)


def _launches(r: MergeRoot) -> list[str]:
    calls = [*r.calls("build"), *r.calls("merge-tests"), *r.pushes(),
             *r.fakes.review_launches(), *r.fakes.post_launches()]
    return sorted(json.dumps(x, sort_keys=True).replace(str(r.root), "<root>") for x in calls)


def _assert_same(stopped: MergeRoot, clean: MergeRoot, merges: list[tuple], *, extra: tuple[str, ...] = ()) -> None:
    """Equal epic files, summary, non-merge records and worktree; the stopped
    root's merge records are *merges* (kind, argv, exit code or None to skip,
    interrupted), the clean root's one `merge-tests` pair; launches equal plus
    one more of each kind in *extra*."""
    s, c = _snapshot(stopped), _snapshot(clean)
    for k in ("spent_usd", "spend_complete", "child_status"):          # the likeliest to differ, named first
        assert s["epic"]["epic.json"].get(k) == c["epic"]["epic.json"].get(k), k
    assert s["epic"] == c["epic"]
    assert s["summary"] == c["summary"]
    assert s["records"] == c["records"]
    assert c["merges"] == [("merge-tests", TESTS, 0, False)]
    assert [(k, a, i) for k, a, _, i in s["merges"]] == [(k, a, i) for k, a, _, i in merges]
    assert all(w is None or e == w for (_, _, e, _), (_, _, w, _) in zip(s["merges"], merges))
    want = _launches(clean)
    for kind in extra:
        want.append(next(x for x in want if json.loads(x)["kind"] == kind))
    assert _launches(stopped) == sorted(want)
    assert_same_as_unstopped(stopped.w, clean.w)


def _ends_waiting(*roots: MergeRoot) -> None:
    for r in roots:
        assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]


def _killed(r: MergeRoot, setup) -> None:
    r.use()
    run_killed(lambda: run_coordinator(r.config), setup)


def _die_at_point(r: MergeRoot, name: str):
    """A `run_killed` setup: a's merge step dies at the seam *name*, after it ran."""
    def setup():
        def dying(coord, c, point):
            r.point(coord, c, point)
            if point == name:
                os._exit(137)
        coordinator._merge_point = dying
    return setup


def _signalled(r: MergeRoot, ready) -> None:
    r.use()
    assert run_signalled(lambda: run_coordinator(r.config), ready, signal.SIGTERM) == 128 + signal.SIGTERM


def _parked(r: MergeRoot) -> dict:
    st = r.status()
    assert st["status"] == "parked" and st["merge_park"] == st["reason"]
    return st


def test_E_S2_stopped_during_the_merge_aborts_and_merges_again(tmp_path, monkeypatch):
    """E.S2: the merge stopped in progress (an exception; a signal takes the
    same `except BaseException`). `merge_stop` holds the tree; item 38 passes,
    the merge is aborted and 40.2 runs again. Nothing repeated."""
    stopped, clean = _roots(tmp_path, monkeypatch)
    _stop_after_the_merge(stopped)
    assert stopped.merging() and stopped.status()["merge_stop"]["edits"]
    _ends_waiting(stopped, clean)
    _assert_same(stopped, clean, [("merge-tests", TESTS, 0, False)])


def test_E_S5_signalled_during_the_tests_merges_again(tmp_path, monkeypatch):
    """E.S5, with a record: SIGTERM while the tests run. As E.S2: one more
    `merge-tests` launch and pair (the first `interrupted`)."""
    stopped, clean = _roots(tmp_path, monkeypatch, tests=[{"block": True}, {}])
    clean_tests = clean.root / "tests.json"
    clean_tests.write_text("[]")
    _signalled(stopped, lambda: (stopped.root / "test-attempts" / "1").exists())
    assert stopped.status()["merge_stop"] is not None and stopped.merging()
    _ends_waiting(stopped, clean)
    _assert_same(stopped, clean, [("merge-tests", TESTS, None, True), ("merge-tests", TESTS, 0, False)],
                 extra=("merge-tests",))


def test_E_S23_sighup_during_the_tests_ends_them_with_the_run(tmp_path, monkeypatch):
    """E.S23 (a row this PR adds): SIGHUP while the tests run. Unlike a build,
    they are killed with the run, so the resume (E.S5) never runs two at once."""
    stopped, clean = _roots(tmp_path, monkeypatch, tests=[{"block": True}, {}])
    (clean.root / "tests.json").write_text("[]")
    stopped.use()
    started = stopped.root / "test-attempts" / "1"
    assert run_signalled(lambda: run_coordinator(stopped.config), started.exists, signal.SIGHUP) == 128 + signal.SIGHUP
    (stopped.root / "release").mkdir(exist_ok=True)
    (stopped.root / "release" / "tests-1").write_text("")      # ends the tests if they were left running
    assert [(t.result or {}).get("exit_code") for t in stopped.tries("merge-tests")] == [-signal.SIGKILL]
    _ends_waiting(stopped, clean)
    _assert_same(stopped, clean, [("merge-tests", TESTS, None, True), ("merge-tests", TESTS, 0, False)],
                 extra=("merge-tests",))


def test_E_S5_killed_during_the_tests_parks_as_E_S4(tmp_path, monkeypatch):
    """E.S5, without a record: the coordinator killed while the tests run. As
    E.S4: the check refuses, the child parks, the tree is untouched."""
    r = MergeRoot(tmp_path, monkeypatch, tests=[{"block": True}])
    started = r.root / "test-attempts" / "1"

    def setup():
        coordinator._tick = lambda coord: started.exists() and os._exit(137)

    _killed(r, setup)
    (r.root / "release").mkdir(exist_ok=True)
    (r.root / "release" / "tests-1").write_text("")            # the orphaned tests end
    assert r.merging() and r.status()["merge_stop"] is None
    before = repo_state(r.w)
    assert r.run() == 0
    assert repo_state(r.w) == before and r.merging() and _parked(r)
    assert len(r.calls("merge-tests")) == 1 and r.step_pushes() == []


def test_E_S6_committed_not_pushed_is_retested_and_pushed(tmp_path, monkeypatch):
    """E.S6: killed after the merge commit, before the push. Item 37: the
    check passes, the tests run again (one more `merge-tests` pair), the
    commit is pushed. No second merge."""
    stopped, clean = _roots(tmp_path, monkeypatch)
    _killed(stopped, _die_at_point(stopped, "push"))
    assert not stopped.merging() and stopped.step_pushes() == []
    _ends_waiting(stopped, clean)
    _assert_same(stopped, clean, [("merge-tests", TESTS, 0, False), ("merge-tests", TESTS, 0, False)],
                 extra=("merge-tests",))


def test_E_S7_a_rejected_push_parks_and_never_forces(tmp_path, monkeypatch):
    """E.S7: origin's branch moves just before the push, in both runs. The
    push is rejected and the child parks naming where origin's branch is and
    what to run. `<W>` is clean, so a re-run retries it (E.S26): the PR's new
    head is reviewed and 40.1 parks, as <W> does not hold it; nothing is
    tested or pushed again (one more review than the run that never re-ran).
    After the hint's fetch and reset, the next run merges onto the new head
    and pushes. No push is forced."""
    stopped, clean = _roots(tmp_path, monkeypatch)
    moved = {}

    def moving(r: MergeRoot):
        def point(coord, c, name):
            r.point(coord, c, name)
            if name == "push" and r.root not in moved:
                moved[r.root] = push_files(r.root, {"other.txt": "someone\n"}, "someone", branch="buildme/e-a")
        return point

    for r in (stopped, clean):
        r.use()
        monkeypatch.setattr(coordinator, "_merge_point", moving(r))
        assert run_coordinator(r.config) == 0
        assert _parked(r)["reason"] == (f"push rejected: origin/buildme/e-a moved to {moved[r.root]}; "
                                        f"{merge.move_hint(r.w, moved[r.root])}")
    new = moved[stopped.root]
    stopped.use()
    monkeypatch.setattr(coordinator, "_merge_point", moving(stopped))
    assert run_coordinator(stopped.config) == 0
    reason = _parked(stopped)["reason"]
    assert reason.startswith("HEAD ") and f"the reviewed head {new[:12]}" in reason and reason.endswith("then re-run")
    s, c = _snapshot(stopped), _snapshot(clean)
    assert s["merges"] == c["merges"]
    want = _launches(clean)
    want.append(next(x for x in want if json.loads(x)["kind"] == "review"))
    assert _launches(stopped) == sorted(want)
    git(stopped.w, "fetch", "-q", "origin")
    git(stopped.w, "reset", "-q", "--keep", new)
    assert stopped.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert git(stopped.w, "rev-parse", "HEAD^1").strip() == new and stopped.origin("other.txt") == "someone\n"
    for r, n in ((stopped, 2), (clean, 1)):
        pushes = r.step_pushes()
        assert len(pushes) == n and not any("--force" in x["argv"] or " -f" in x["argv"] for x in pushes)


@pytest.mark.parametrize("at", ["start", "push"])
@pytest.mark.parametrize("where", ["p/app.py", "README.md"])
def test_E_S12_a_user_edit_in_the_worktree_parks_and_is_left_alone(tmp_path, monkeypatch, at, where):
    """E.S12: the project is the subdirectory `p`; after a stop before the merge
    (`start`) or before the push (`push`), the user edits a file inside the
    project or outside it. 40.1 (or 37.1) refuses on `<W>`; the child parks;
    the worktree is as the user left it."""
    r = MergeRoot(tmp_path, monkeypatch, sub="p")
    _killed(r, _die_at_point(r, at))
    (r.w / where).write_text("the user's edit\n")
    before = repo_state(r.w)
    assert r.run() == 0
    assert repo_state(r.w) == before and _parked(r)
    assert where.split("/")[-1] in r.status()["reason"]
    assert r.step_pushes() == [] and len(r.calls("merge-tests")) == (1 if at == "push" else 0)


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_E_S18_an_edit_outside_the_project_during_a_stopped_merge_is_never_aborted(
        tmp_path, monkeypatch, staged):
    """E.S18: the project is `p`; main moved `README.md`, at the repo root, and
    the merge stopped with it merged. The user edits that file (staged or
    not). Item 38 refuses; the merge is not aborted; the edit stays."""
    r = MergeRoot(tmp_path, monkeypatch, sub="p", outside={"README.md": "# r\nfrom main\n"})
    _stop_after_the_merge(r)
    assert r.status()["merge_stop"] is not None
    assert git(r.w, "diff", "--cached", "--name-only", "--", "README.md").strip() == "README.md"
    (r.w / "README.md").write_text("the user's edit\n")
    if staged:
        git(r.w, "add", "--", "README.md")
    before = repo_state(r.w)
    ran: list[tuple] = []
    real = merge.git
    monkeypatch.setattr(merge, "git", lambda w, *a: ran.append(a) or real(w, *a))
    assert r.run() == 0
    assert ("merge", "--abort") not in ran and r.merging()
    assert repo_state(r.w) == before and "README.md" in _parked(r)["reason"]


def test_E_S21_a_refused_abort_parks_with_git_s_words_and_the_hint(tmp_path, monkeypatch):
    """E.S21: the step stopped with the cleanly merged `uv.lock` also changed
    and not staged (`MM`). Item 38 passes (the edit is in `merge_stop`), but
    git refuses the abort over `uv.lock`: the child parks with git's stderr
    and the hint, and nothing else runs."""
    r = MergeRoot(tmp_path, monkeypatch)

    def edit_then_stop(coord, c, name):
        r.point(coord, c, name)
        if name == "merged":
            (r.w / "uv.lock").write_text("lock 3\n")
            raise RuntimeError("stopped at the merge")

    r.use()
    monkeypatch.setattr(coordinator, "_merge_point", edit_then_stop)
    assert run_coordinator(r.config) == 1
    assert "uv.lock" in r.status()["merge_stop"]["edits"]
    assert git(r.w, "status", "--porcelain", "--", "uv.lock").startswith("MM")
    before = repo_state(r.w)
    assert r.run() == 0
    assert repo_state(r.w) == before and r.merging()
    reason = _parked(r)["reason"]
    assert reason.startswith("git merge --abort failed: ") and "uv.lock" in reason
    assert reason.endswith(f"git -C {r.w} checkout -- uv.lock, then git -C {r.w} merge --abort")
    assert r.calls("merge-tests") == [] and r.step_pushes() == []


def test_E_S22_a_merge_park_is_kept_while_a_merge_is_in_progress(tmp_path, monkeypatch):
    """E.S22 (a row this PR adds: `merge_park` is saved state the table did not
    list): a child the merge step parked (here, a source-file conflict, which
    leaves `MERGE_HEAD`) stays parked on the next run: only the read of
    `MERGE_HEAD` runs, no check, abort, agent, tests or push, and its
    worktree is unchanged."""
    r = MergeRoot(tmp_path, monkeypatch, child_files={"app.py": "x = 3\n"}, main={"app.py": "x = 2\n"})
    assert r.run() == 0
    reason = _parked(r)["reason"]
    before = repo_state(r.w)
    ran: list[tuple] = []
    real = merge.git
    monkeypatch.setattr(merge, "git", lambda w, *a: ran.append(a) or real(w, *a))
    assert r.run() == 0
    assert ran == [("rev-parse", "-q", "--verify", "MERGE_HEAD")]
    assert repo_state(r.w) == before and _parked(r)["reason"] == reason
    assert r.calls("merge-tests") == [] and r.step_pushes() == []


def test_E_S26_a_merge_park_retries_once_the_worktree_is_clean(tmp_path, monkeypatch):
    """E.S26 (a row this PR adds; Spike, 2026-10-14): after `git merge --abort`,
    as the park's hint says, a re-run retries the merge step, but only once
    `<W>` holds no uncommitted change. A file left there keeps the child
    parked and the worktree as it was; a merge that fails again parks again,
    with the same reason; one that now passes is tested and pushed, and the
    PR waits as a never-parked child's does."""
    r = MergeRoot(tmp_path, monkeypatch, child_files={"app.py": "x = 3\n"}, main={"app.py": "x = 2\n"})
    assert r.run() == 0
    reason = _parked(r)["reason"]
    assert reason.endswith(f"; git -C {r.w} merge --abort, then re-run")
    git(r.w, "merge", "--abort")
    (r.w / "notes.txt").write_text("mine\n")
    before = repo_state(r.w)
    ran: list[tuple] = []
    real = merge.git
    monkeypatch.setattr(merge, "git", lambda w, *a: ran.append(a) or real(w, *a))
    assert r.run() == 0
    assert not any(a[0] == "merge" for a in ran) and repo_state(r.w) == before
    assert _parked(r)["reason"] == reason
    (r.w / "notes.txt").unlink()
    ran.clear()
    assert r.run() == 0                      # retried: the same conflict parks it again
    assert ("merge", "--no-ff", "--no-commit", "origin/main") in ran and r.merging()
    assert _parked(r)["reason"] == reason
    assert r.calls("merge-tests") == [] and r.step_pushes() == []
    git(r.w, "merge", "--abort")
    push_files(r.root, {"app.py": "x = 3\n"}, "main takes the child's line")
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    st = r.status()
    assert st["status"] == "built" and st["merge_park"] == "" and st["merge_stop"] is None
    assert len(r.calls("merge-tests")) == 1 and len(r.step_pushes()) == 1 and not r.merging()
    assert r.origin("app.py") == "x = 3\n"
