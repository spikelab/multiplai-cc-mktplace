"""Epic mode, PR E1: the merge step (plan-buildme-epic-mode-2026-10-12.md,
items 35-39, 40.1-40.4, 41 and 42). Git is real; the build, `gh`, the review
and the test command are fakes first on PATH (`epic_row_helpers.Fakes`), and
the merge agent is `FakeAgent`. Until PR E2 a merged and pushed PR waits for a
human merge (`EPIC:MERGE_WAITING`, exit 3); a parked child ends its run with
exit 0, an `unknown` one with exit 1."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from build_pipeline import budget, git_ops, stop_record
from build_pipeline.config import EpicSettings, discover_test_command
from build_pipeline.epic import coordinator, merge, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer
from build_pipeline.models import AgentResult

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    merge_waiting,
    pr_number,
    pr_url,
    real_git,
    run_coordinator,
    use_workspace,
)

CHAIN = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))])


def plugin(version: str) -> str:
    return json.dumps({"name": "x", "version": version}, indent=2) + "\n"


def changelog(*entries: tuple[str, str]) -> str:
    return "# Changelog\n" + "".join(f"\n## [{v}]\n\n- {text}\n" for v, text in entries)


BASE = {"plugin.json": plugin("0.1.0"), "CHANGELOG.md": changelog(("0.1.0", "First.")),
        "uv.lock": "lock 1\n", "app.py": "x = 1\n"}
CHILD = {"plugin.json": plugin("0.2.0"), "CHANGELOG.md": changelog(("0.2.0", "Child a."), ("0.1.0", "First."))}
MAIN = {"plugin.json": plugin("0.1.1"), "CHANGELOG.md": changelog(("0.1.1", "Main."), ("0.1.0", "First.")),
        "uv.lock": "lock 2\n"}
RESOLVED = {"plugin.json": plugin("0.2.0"),
            "CHANGELOG.md": changelog(("0.2.0", "Child a."), ("0.1.1", "Main."), ("0.1.0", "First."))}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def git(cwd: Path, *a: str) -> str:
    """The real git, so a test's own pushes are never logged as the step's."""
    return subprocess.run([real_git(), *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def push_files(root: Path, files: dict[str, str], message: str, branch: str = "main") -> str:
    """Commit *files* onto origin's *branch* from a scratch clone; the new sha."""
    clone = root / "scratch"
    if not clone.exists():
        git(root, "clone", "-q", str(root / "o" / "r.git"), str(clone))
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", branch, f"origin/{branch}")
    for rel, text in files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text)
    git(clone, "add", "--", *files)
    git(clone, "-c", "commit.gpgsign=false", "commit", "-qm", message)
    git(clone, "push", "-q", "origin", f"HEAD:{branch}")
    return git(clone, "rev-parse", "HEAD").strip()


class FakeAgent:
    """The merge agent: writes *resolve*'s content for each conflicted file it
    has, spends $0.50, logs the call; *stop(w)* runs after the writes."""

    def __init__(self, root: Path, resolve: dict[str, str], stop=None):
        self.root, self.resolve, self.stop = root, resolve, stop

    async def __call__(self, config, w, files, name, default):
        with (self.root / "calls.log").open("a") as f:
            f.write(json.dumps({"kind": "merge-agent", "files": files}, sort_keys=True) + "\n")
        for rel in files:
            if rel in self.resolve:
                (Path(w) / rel).write_text(self.resolve[rel])
        budget.add_process_spend(0.5, 10)
        if self.stop is not None:
            out = self.stop(Path(w))
            if out is not None:
                await out
        return AgentResult(success=True, output="\n".join(f"RESOLVED: {f}" for f in files))


class MergeRoot:
    """Main holds BASE; an approved chain (b waits on a); a's build commits
    *child_files*; test command `fake-tests` (answering *tests*) unless
    *command* is None. *main* is pushed onto origin's main at the `start`
    seam. *sub*: the project's subdirectory, holding every path above;
    *outside* is pushed with *main* at the repo root."""

    def __init__(self, root: Path, monkeypatch, *, child_files=CHILD, main=MAIN, tests=None, agent=None,
                 merge_mode="auto", command="fake-tests", sub: str | None = None,
                 outside: dict[str, str] | None = None):
        at = (lambda files: {f"{sub}/{k}": v for k, v in files.items()}) if sub else dict
        self.root, self.mp, self.main = root, monkeypatch, main and {**at(main), **(outside or {})}
        self.data = use_workspace(monkeypatch, root)
        # Another root's fakes off PATH, so its `git` shim logs none of this root's setup.
        monkeypatch.setenv("PATH", os.pathsep.join(
            p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p).parent / "fakes.json").exists()))
        self.repo = repo = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {"a": [{"commit": at(child_files)}]})
        self.project = repo / sub if sub else repo
        self.project.mkdir(exist_ok=True)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2, merge=merge_mode)
        approve(self.config, CHAIN, CallLog(root / "split.log"))
        files = {**BASE, **({"specs/config.yaml": f"tdd:\n  test_command: {command}\n"} if command else {})}
        push_files(root, at(files), "base")
        git(repo, "pull", "-q", "origin", "main")        # the source checkout holds origin's config.yaml
        (root / "tests.json").write_text(json.dumps(tests or []))
        self.agent = agent or FakeAgent(root, at(RESOLVED))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.w = root / "ws" / ".worktrees" / "buildme-e-a"

    def use(self) -> None:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)
        self.mp.setattr(merge, "run_merge_agent", self.agent)
        self.mp.setattr(coordinator, "_merge_point", self.point)

    @property
    def moved(self) -> str | None:
        """The commit `point` pushed onto origin's main; a file, so a run in a forked child counts."""
        flag = self.root / "moved"
        return flag.read_text() if flag.exists() else None

    def point(self, coord, c, name: str) -> None:
        if name == "start" and self.main and not self.moved:
            (self.root / "moved").write_text(push_files(self.root, self.main, "main moved"))

    def run(self) -> int:
        self.use()
        return run_coordinator(self.config)

    def status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]["a"]

    def tries(self, kind: str) -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.child == "a" and a.kind == kind]

    def calls(self, kind: str) -> list[dict]:
        return [x for x in CallLog(self.root / "calls.log").calls() if x["kind"] == kind]

    def pushes(self) -> list[dict]:
        """Every `git push` the `git` shim saw."""
        return CallLog(self.root / "pushes.log").calls()

    def step_pushes(self) -> list[dict]:
        """`git push` calls but the fake build's own."""
        return [x for x in self.pushes() if not x["argv"].startswith("push -q -u")]

    def origin(self, rel: str, ref: str = "buildme/e-a") -> str:
        return git(self.root / "o" / "r.git", "show", f"{ref}:{rel}")

    def merging(self) -> bool:
        return merge.merging(self.w)


def test_version_and_changelog_conflicts_are_resolved_tested_and_pushed(tmp_path, monkeypatch):
    """40.2-40.4: main moved under a's branch with its own version and
    CHANGELOG entry. One `merge` agent call resolves both (its spend in the
    record), the tests run in <P> with BUILDME_TRUST_REPO=1, and the merge
    commit is pushed. Until E2 the PR then waits for a human merge."""
    r = MergeRoot(tmp_path, monkeypatch)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert [x["files"] for x in r.calls("merge-agent")] == [["CHANGELOG.md", "plugin.json"]]
    [agent] = r.tries("merge")
    assert (agent.result["exit_code"], agent.result["spend"]) == (0, 0.5)
    [tests] = r.calls("merge-tests")
    assert (tests["cwd"], tests["trusted"]) == ("<root>/ws/.worktrees/buildme-e-a", "1")
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [0]
    assert len(r.step_pushes()) == 1
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]
    assert r.origin("CHANGELOG.md") == RESOLVED["CHANGELOG.md"] and r.origin("uv.lock") == "lock 2\n"
    head = git(r.w, "rev-parse", "HEAD").strip()
    assert git(r.w, "rev-parse", "HEAD^2").strip() == r.moved
    assert merge.merges_onto(r.w, head, r.tries("review")[-1].started["head_sha"], "main")
    assert r.status()["status"] == "built" and r.status()["merge_stop"] is None
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]      # its own merge: not reviewed again
    assert len(r.tries("review")) == 1 and len(r.calls("merge-agent")) == 1 and len(r.step_pushes()) == 1
    reviewed = r.tries("review")[-1].started["head_sha"]
    edits = {"app.py": "x = 'not reviewed'\n",                   # the same merge carrying a person's edit is reviewed,
             "plugin.json": RESOLVED["plugin.json"].replace('"x"', '"evil"'),   # inside a resolved file too
             "CHANGELOG.md": "anything at all\n", "uv.lock": "lock evil\n"}
    for rel, text in edits.items():
        (r.w / rel).write_text(text)
        git(r.w, "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit", "--", rel)
        assert not merge.merges_onto(r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "main"), rel
        git(r.w, "reset", "-q", "--hard", head)
    (r.w / "uv.lock").write_text('lock 2\nversion = "0.2.0"\n')     # an unconflicted lockfile is git's own
    git(r.w, "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit", "--", "uv.lock")
    assert not merge.merges_onto(r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "main")


def test_a_branch_up_to_date_with_main_waits_with_no_tests_and_no_push(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, main=None)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert r.calls("merge-tests") == [] and r.step_pushes() == [] and r.tries("merge") == []


def test_a_source_file_conflict_parks_listing_it_and_stays_parked(tmp_path, monkeypatch):
    """40.3: a conflicted file that is neither a version field, a CHANGELOG nor
    a lockfile parks before any agent call, with the merge left for a person
    to abort; nothing is pushed, and the next run does not touch it."""
    r = MergeRoot(tmp_path, monkeypatch, child_files={"app.py": "x = 3\n"}, main={"app.py": "x = 2\n"})
    assert r.run() == 0
    st = r.status()
    assert st["status"] == "parked" and st["merge_park"] == st["reason"]
    assert st["reason"].startswith("merging origin/main conflicts in app.py, which the merge step may not resolve; "
                                   f"git -C {r.w} merge --abort, then re-run")
    assert r.calls("merge-agent") == [] and r.step_pushes() == [] and r.merging()
    assert st["merge_stop"]["edits"].keys() == {"app.py"}
    before = git(r.w, "status", "--porcelain")
    assert r.run() == 0 and r.status()["status"] == "parked"
    assert git(r.w, "status", "--porcelain") == before and r.calls("merge-tests") == []


def test_manual_merges_nothing_exits_3_and_continues_after_a_human_merge(tmp_path, monkeypatch):
    """Item 41: under `manual` a PR that passed review is waited on; once a
    person merges it, the next run launches its dependant."""
    r = MergeRoot(tmp_path, monkeypatch, merge_mode="manual")
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert r.calls("merge-tests") == [] and r.step_pushes() == [] and not r.moved
    r.fakes.set_pr(pr_url("e-a"), "MERGED")
    assert r.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert r.status()["status"] == "merged" and r.fakes.attempts("b") == 1


def _stop_after_the_merge(r: MergeRoot) -> None:
    """Run a until its merge has conflicts, then stop it by an exception there."""
    def boom(coord, c, name):
        r.point(coord, c, name)
        if name == "merged":
            raise RuntimeError("stopped at the merge")

    r.use()
    r.mp.setattr(coordinator, "_merge_point", boom)
    assert run_coordinator(r.config) == 1


def test_a_refusal_from_the_shared_check_never_aborts_the_merge(tmp_path, monkeypatch):
    """Item 38: with `others_work` giving a reason, `git merge --abort` never
    runs; the child parks with the reason and the abort hint."""
    r = MergeRoot(tmp_path, monkeypatch)
    _stop_after_the_merge(r)
    assert r.merging() and r.status()["merge_stop"] is not None
    ran: list[tuple] = []
    real = merge.git
    monkeypatch.setattr(merge, "git", lambda w, *a: ran.append(a) or real(w, *a))
    monkeypatch.setattr(stop_record, "others_work", lambda *a, **k: "someone else's edit")
    assert r.run() == 0
    assert ("merge", "--abort") not in ran and r.merging()
    assert r.status()["reason"] == f"someone else's edit; git -C {r.w} merge --abort, then re-run"


def test_failing_tests_park_and_push_nothing(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, tests=[{"exit": 1}])
    assert r.run() == 0
    [tests] = r.tries("merge-tests")
    assert r.status()["reason"] == (f"merge tests failed: fake-tests exited 1, see {tests.log}; "
                                    f"git -C {r.w} merge --abort, then re-run")
    assert r.step_pushes() == [] and r.merging() and r.status()["merge_stop"] is not None


def test_tests_that_cannot_run_park_and_push_nothing(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, command="no-such-test-command")
    assert r.run() == 0
    assert r.status()["reason"].startswith("merge tests could not run: [Errno 2]")
    assert r.step_pushes() == [] and r.merging()


def test_tests_that_outlast_the_timeout_are_killed_and_park(tmp_path, monkeypatch):
    """40.4: tests still running after TESTS_TIMEOUT (the fake clock's) are
    killed with their process group; the child parks, nothing pushed."""
    r = MergeRoot(tmp_path, monkeypatch, tests=[{"block": True}])
    assert r.run() == 0
    assert r.status()["reason"] == (f"merge tests could not run: timed out after {coordinator.TESTS_TIMEOUT}s; "
                                    f"{merge.abort_hint(r.w)}")
    [tests] = r.tries("merge-tests")
    assert (tests.result["exit_code"], tests.result["interrupted"]) == (None, False)
    assert r.step_pushes() == [] and r.merging()


def test_no_test_command_commits_and_pushes(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, command=None)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert r.tries("merge-tests") == [] and len(r.step_pushes()) == 1 and not r.merging()
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]


def _failing_git(monkeypatch, *argv: str, stderr: str) -> None:
    """`merge.git` fails *argv* with *stderr*, running nothing; every other command runs."""
    real = merge.git
    monkeypatch.setattr(merge, "git", lambda w, *a: git_ops.GitResult(["git", *a], 1, "", stderr)
                        if a == argv else real(w, *a))


def test_a_failed_merge_commit_parks_and_pushes_nothing(tmp_path, monkeypatch):
    """40.4: `git commit --no-edit` fails after the tests passed (a hook). The
    child parks with git's words and the abort hint, the merge is left in
    progress with `merge_stop` saved, and nothing is pushed."""
    r = MergeRoot(tmp_path, monkeypatch)
    _failing_git(monkeypatch, "commit", "--no-edit", stderr="error: pre-commit hook refused\n")
    assert r.run() == 0
    assert _parked_reason(r) == f"git commit failed: error: pre-commit hook refused; {merge.abort_hint(r.w)}"
    assert r.merging() and r.status()["merge_stop"] is not None
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [0] and r.step_pushes() == []


def test_a_merge_that_fails_without_starting_parks_and_leaves_nothing(tmp_path, monkeypatch):
    """40.2: `git merge` fails and leaves no `MERGE_HEAD` (here, refused
    outright). The child parks with git's words; nothing merged, tested or
    pushed, and `<W>` is as it was."""
    r = MergeRoot(tmp_path, monkeypatch)
    _failing_git(monkeypatch, "merge", "--no-ff", "--no-commit", "origin/main",
                 stderr="fatal: refusing to merge unrelated histories\n")
    assert r.run() == 0
    assert _parked_reason(r) == "git merge origin/main failed: fatal: refusing to merge unrelated histories"
    assert not r.merging() and git(r.w, "status", "--porcelain") == ""
    assert r.tries("merge-tests") == [] and r.step_pushes() == []


def _parked_reason(r: MergeRoot) -> str:
    st = r.status()
    assert st["status"] == "parked" and st["merge_park"] == st["reason"]
    return st["reason"]


def test_an_agent_that_leaves_a_marker_parks(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(tmp_path, {"plugin.json": RESOLVED["plugin.json"]})
    assert r.run() == 0
    assert r.status()["reason"] == ("the merge agent left conflicts in CHANGELOG.md; "
                                    f"git -C {r.w} merge --abort, then re-run")
    assert r.step_pushes() == [] and r.tries("merge-tests") == []


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_an_agent_that_edits_another_file_parks_staged_or_not(tmp_path, monkeypatch, staged):
    """40.3: the agent resolves every conflict and also changes `app.py`,
    staging it itself (it has Bash) or not. Parks; nothing pushed."""
    def edit(w):
        (w / "app.py").write_text("x = 9\n")
        staged and git(w, "add", "--", "app.py")

    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(tmp_path, RESOLVED, stop=edit)
    assert r.run() == 0
    assert r.status()["reason"] == f"the merge agent changed files it may not: app.py; {merge.abort_hint(r.w)}"
    assert r.step_pushes() == [] and r.tries("merge-tests") == []


def test_a_failed_agent_call_parks_and_commits_no_marker(tmp_path, monkeypatch):
    class Failing(FakeAgent):
        async def __call__(self, config, w, files, name, default):
            await super().__call__(config, w, files, name, default)
            return AgentResult(success=False, output="", error="rate limited")

    r = MergeRoot(tmp_path, monkeypatch, agent=Failing(tmp_path, {}))
    assert r.run() == 0
    assert r.status()["reason"] == f"the merge agent failed: rate limited; {merge.abort_hint(r.w)}"
    assert [a.result["exit_code"] for a in r.tries("merge")] == [1] and r.merging()
    assert r.tries("merge-tests") == [] and r.step_pushes() == []


def test_an_agent_edit_beyond_the_version_lines_of_a_resolved_file_parks(tmp_path, monkeypatch):
    """40.3, by content: the agent resolves `plugin.json`'s version and also
    renames the plugin in it. Parks, naming the file; nothing pushed."""
    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(tmp_path, {**RESOLVED, "plugin.json": RESOLVED["plugin.json"].replace('"x"', '"evil"')})
    assert r.run() == 0
    assert r.status()["reason"] == ("the merge agent changed more than 40.3 allows in plugin.json; "
                                    f"{merge.abort_hint(r.w)}")
    assert r.tries("merge-tests") == [] and r.step_pushes() == [] and r.merging()


@pytest.mark.asyncio
async def test_the_merge_agent_runs_in_a_run_started_without_trust_repo(tmp_path, monkeypatch):
    """The coordinator vouches for the repo for its merge agent as it does for
    each child it launches (`--trust-repo`): `agent_call`'s gate passes."""
    from build_pipeline import sdk

    async def agent_call(prompt, **kw):
        sdk._require_trusted_repo()
        return AgentResult(success=True, output="RESOLVED: plugin.json")

    monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
    monkeypatch.setattr(merge, "agent_call", agent_call)
    config = make_build(make_project(tmp_path))
    assert (await merge.run_merge_agent(config, tmp_path, ["plugin.json"], "b", "main")).success


def test_over_budget_before_a_conflicted_merge_merges_nothing(tmp_path, monkeypatch):
    """Decided 6, E.S27: `epic.max_usd` reached when the merge would need the
    agent. Nothing is merged (no `MERGE_HEAD`, nothing changed in <W>), the
    child stays `built`, and a run with the ceiling raised merges."""
    r = MergeRoot(tmp_path, monkeypatch)
    real = coordinator.Coordinator._over_budget

    def over(self):
        if r.moved:           # after the review, at the merge step
            self.budget_stop = 1.0
        return real(self)

    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", over)
    assert r.run() == 3
    assert not r.merging() and git(r.w, "status", "--porcelain") == ""
    assert r.status()["status"] == "built" and not r.status()["merge_park"] and r.status()["merge_stop"] is None
    assert r.calls("merge-agent") == [] and r.tries("merge") == [] and r.step_pushes() == []
    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", real)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")] and len(r.step_pushes()) == 1


def test_a_failed_fetch_leaves_the_child_unknown_and_the_run_exits_1(tmp_path, monkeypatch, capsys, caplog):
    r = MergeRoot(tmp_path, monkeypatch)
    real = coordinator._fetch_origin

    async def fetch(repo):
        return "fatal: unable to access origin" if ".worktrees" in str(repo) else await real(repo)

    monkeypatch.setattr(coordinator, "_fetch_origin", fetch)
    assert r.run() == 1 and merge_waiting() is None
    reason = "cannot merge: git fetch origin failed: fatal: unable to access origin"
    assert (r.status()["status"], r.status()["reason"]) == ("unknown", reason)
    assert f"EPIC: child a unknown: {reason}" in capsys.readouterr().err and reason in caplog.text
    assert not r.merging() and r.step_pushes() == []


def test_a_failed_review_of_a_new_head_is_not_waited_on_as_passed(tmp_path, monkeypatch, capsys):
    """A PR whose review was clean gets a new head, whose review fails (C.S20):
    the child stays `unknown` and the run exits 1, never naming the unreviewed
    head in `EPIC:MERGE_WAITING` as if it passed review."""
    from .test_epic_review import READ_FAILURES, Epic

    e = Epic(tmp_path, monkeypatch, reviews={str(pr_number("e-a")): [{}, {"exit": 2, "stderr": READ_FAILURES[0]}]})
    assert e.run() == 3 and sorted(merge_waiting()) == sorted([pr_url("e-a"), pr_url("e-b")])
    e.fakes.set_head("e-a", "1" * 40)
    assert e.run() == 1 and merge_waiting() is None
    assert e.child_status("a")["status"] == "unknown" and "EPIC: child a unknown: " in capsys.readouterr().err


def test_a_stopped_review_of_a_new_head_is_not_waited_on_as_passed(tmp_path, monkeypatch):
    """Item 41: a's review was clean; its PR then gets a new head, whose review
    is killed (C.S3). a's last clean review is of the older head, so the run
    names only b in `EPIC:MERGE_WAITING`, never a's unreviewed head."""
    from .test_epic_review import Epic

    e = Epic(tmp_path, monkeypatch, reviews={str(pr_number("e-a")): [{}, {"exit": 137}]})
    assert e.run() == 3 and sorted(merge_waiting()) == sorted([pr_url("e-a"), pr_url("e-b")])
    e.fakes.set_head("e-a", "1" * 40)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.child_status("a")["status"] == "built" and len(e.reviews("a")) == 2


def test_40_1_parks_a_worktree_behind_the_reviewed_head_naming_what_to_run(tmp_path, monkeypatch):
    """40.1 (E.S25): the review passed at a head pushed from another clone,
    which <W> lacks; its HEAD, older, is on `origin/<branch>`. Parks, nothing
    merged, with what to run: fetch, then fast-forward; each re-run before
    that parks again, and the one after merges onto the reviewed head."""
    r = MergeRoot(tmp_path, monkeypatch, merge_mode="manual")
    assert r.run() == 3
    prs = json.loads((r.root / "prs.json").read_text())
    prs[pr_url("e-a")]["head"] = new = push_files(r.root, {"other.py": "y = 1\n"}, "another clone", branch="buildme/e-a")
    (r.root / "prs.json").write_text(json.dumps(prs))
    r.config.epic = EpicSettings(max_parallel=2, merge="auto")
    assert r.run() == 0
    assert r.status()["reason"].endswith(f"which {r.w} has not fetched; git -C {r.w} fetch origin, then re-run")
    assert not r.merging() and r.calls("merge-tests") == [] and r.step_pushes() == []
    git(r.w, "fetch", "-q", "origin")
    assert r.run() == 0 and r.status()["merge_park"]
    assert r.status()["reason"].endswith(f"is behind the reviewed head {new[:12]}, pushed from elsewhere; "
                                         f"git -C {r.w} merge --ff-only {new}, then re-run")
    assert not r.merging() and r.calls("merge-tests") == [] and r.step_pushes() == []
    git(r.w, "merge", "-q", "--ff-only", new)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert len(r.step_pushes()) == 1 and git(r.w, "rev-parse", "HEAD^1").strip() == new


def test_a_person_s_merge_commit_in_the_worktree_is_never_pushed_unreviewed(tmp_path, monkeypatch):
    """Item 37 takes only the step's own merges: a source-file conflict parks,
    and the person resolves it and commits in <W> instead of aborting. The
    re-run (E.S26: <W> is clean) parks at 40.1, nothing tested or pushed."""
    r = MergeRoot(tmp_path, monkeypatch, child_files={"app.py": "x = 3\n"}, main={"app.py": "x = 2\n"})
    assert r.run() == 0 and r.merging()
    (r.w / "app.py").write_text("x = 'mine'\n")
    git(r.w, "add", "--", "app.py")
    git(r.w, "-c", "commit.gpgsign=false", "commit", "-q", "--no-edit")
    assert r.run() == 0
    assert r.status()["reason"].endswith("so it holds commits the review did not see; push them to be reviewed, "
                                         "then re-run")
    assert r.calls("merge-tests") == [] and r.step_pushes() == []


def test_a_push_failing_for_another_reason_is_unknown_and_the_next_run_pushes(tmp_path, monkeypatch, capsys):
    """E.S24: origin's hook refuses the push, its branch unmoved: the child is
    `unknown` with git's words, not parked, and the run exits 1 naming it (a
    read of its open PR does not make it `built`); the next run retests and
    pushes (item 37, E.S6)."""
    r = MergeRoot(tmp_path, monkeypatch)
    hook = r.root / "o" / "r.git" / "hooks" / "pre-receive"

    def refusing(coord, c, name):
        r.point(coord, c, name)
        name == "push" and hook.write_text("#!/bin/sh\necho 'denied by policy' >&2\nexit 1\n") and hook.chmod(0o755)

    r.use()
    monkeypatch.setattr(coordinator, "_merge_point", refusing)
    assert run_coordinator(r.config) == 1 and merge_waiting() is None
    assert "EPIC: child a unknown: cannot merge: git push failed: remote: denied by policy" in capsys.readouterr().err
    assert r.status()["status"] == "unknown" and not r.status()["merge_park"] and r.step_pushes()
    hook.unlink()
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert len(r.step_pushes()) == 2 and len(r.tries("merge-tests")) == 2 and len(r.calls("merge-agent")) == 1
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]


def test_may_resolve_only_versions_changelogs_and_lockfiles(tmp_path):
    """40.3: the package's own version field, never a dependency's; a
    CHANGELOG by name, never a source file whose stem is `changelog`."""
    sides = {"pyproject.toml": '[project]|version = "0.2.0"|version = "0.1.1"',
             "p.json": '{|  "version": "2",|  "version": "1",', "v.py": '|__version__ = "2.0"|__version__ = "1.9"',
             "app.py": "|x = 3|x = 2", "rate.py": "|r = conversion(3)|r = conversion(2)",
             "changelog_tool.py": "|x = 3|x = 2", "changelog.py": "|x = 3|x = 2", "src/Changelog.ts": "|x = 3|x = 2",
             "compat.py": "|if sys.version_info < (3, 10):|if sys.version_info < (3, 9):",
             "Cargo.toml": '[dependencies.serde]|version = "1.0.150"|version = "1.0.200"',
             "CHANGELOG.rst": "|- b|- a", "pubspec.yaml": "dependencies:|    version: 1.3.0|    version: 1.2.0"}
    for name, parts in sides.items():
        above, ours, theirs = parts.split("|")
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(f"{above}\n<<<<<<< HEAD\n{ours}\n=======\n{theirs}\n>>>>>>> x\n")
    sides["dep.json"] = ""
    (tmp_path / "dep.json").write_text('{\n  "dependencies": {\n<<<<<<< HEAD\n    "version": "2",\n=======\n'
                                       '    "version": "1",\n>>>>>>> x\n  }\n}\n')
    (tmp_path / "clean.json").write_text('{"version": "1"}\n')
    assert [f for f in (*sides, "clean.json", "a/uv.lock", "docs/CHANGELOG.md") if merge.may_resolve(tmp_path, f)] \
        == ["pyproject.toml", "p.json", "v.py", "CHANGELOG.rst", "a/uv.lock", "docs/CHANGELOG.md"]


def _lock(version: str, dep: str = "1.0.0", tail: str = 'hash = "h1"\n') -> str:
    return f'version = "{version}"\n\n[[package]]\nname = "dep"\nversion = "{dep}"\n{tail}'


def test_a_resolution_is_checked_by_content_lockfiles_and_binaries_included(tmp_path):
    """40.3, by content: both sides raised the version in `plugin.json`, in
    `uv.lock` and in the CHANGELOG. A lockfile may take only lines of its
    conflict's sides, or the version the result wrote in a version file; any
    line outside a conflict, in any file, is git's own. A binary file that
    differs is named, never a crash."""
    w = tmp_path / "m"
    w.mkdir()

    def commit(files: dict, at: str | None = None) -> str:
        if at:
            git(w, "checkout", "-q", "--detach", at)
        for rel, text in files.items():
            (w / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
        git(w, "add", "--", *files)
        git(w, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "c")
        return git(w, "rev-parse", "HEAD").strip()

    git(w, "init", "-q")
    base = commit({"plugin.json": plugin("0.1.0"), "uv.lock": _lock("0.1.0"), "img.png": b"\x89PNG\xff1",
                   "CHANGELOG.md": changelog(("0.1.0", "First."))})
    ours = commit({"plugin.json": plugin("0.2.0"), "uv.lock": _lock("0.2.0"), "CHANGELOG.md": CHILD["CHANGELOG.md"]})
    theirs = commit({"plugin.json": plugin("0.1.1"), "uv.lock": _lock("0.1.1"), "CHANGELOG.md": MAIN["CHANGELOG.md"]},
                    at=base)
    good = {"plugin.json": plugin("0.2.1"), "uv.lock": _lock("0.2.1"),
            "CHANGELOG.md": changelog(("0.2.1", "Child a."), ("0.1.1", "Main."), ("0.1.0", "First."))}
    cases = {"": {}, "uv.lock": {"uv.lock": _lock("0.2.1", tail="")},          # a hash line dropped
             "uv.lock ": {"uv.lock": _lock("0.2.1", dep="6.6.6")},           # a dependency's version
             " uv.lock": {"uv.lock": _lock("0.9.9")},                       # a version plugin.json does not hold
             "img.png": {"img.png": b"\x89PNG\xff2"},
             "CHANGELOG.md": {"CHANGELOG.md": good["CHANGELOG.md"].replace("## [0.1.0]", "## [9.9.9]")}}
    for want, change in cases.items():
        result = commit({**good, **change}, at=ours)
        assert merge.not_resolutions(w, ours, theirs, result) == ([want.strip()] if want else []), want


def test_the_merge_tests_fall_back_to_discovery(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert merge.test_command(tmp_path) == discover_test_command(tmp_path)[0]


def test_a_heading_underline_left_in_a_resolved_changelog_is_no_conflict(tmp_path):
    (tmp_path / "CHANGELOG.rst").write_text("Changes\n=======\n\n0.2.0\n-----\n\n- Child.\n")
    (tmp_path / "CHANGELOG.md").write_text("<<<<<<< HEAD\n- a\n=======\n- b\n>>>>>>> x\n")
    assert merge.unresolved(tmp_path, ["CHANGELOG.rst", "CHANGELOG.md"]) == ["CHANGELOG.md"]


# --- 40.1 after a real fix round (item 42) ------------------------------------------

@pytest.mark.asyncio
async def test_a_child_that_had_a_fix_round_passes_40_1_and_an_extra_file_parks_it(tmp_path, monkeypatch):
    """One round of D1's real `findings` loop on the PR's worktree (fakes only
    at its seams) leaves only bookkeeping, so 40.1 passes; an untracked file
    anyone else leaves makes it refuse, naming the file."""
    from tests.test_findings_round import FIX, FakePR, FakeReview, FakeTriage, _make_built, _run, _Stubs
    from tests.test_findings_self_check import FakeCheck, _findings_json, _with_bare_origin, block_stubs

    from build_pipeline.findings import loop

    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    repo, change, config = _make_built(tmp_path)
    config.findings.max_rounds = 1
    _with_bare_origin(tmp_path, repo)
    pr = FakePR()
    monkeypatch.setattr(git_ops, "_run_gh", pr.gh)
    monkeypatch.setattr(git_ops, "_run_pr_skill", pr.pr_skill)
    pr.add_thread("alice", "add subtracts", fid="ab12")
    review = FakeReview(pr)
    data = tmp_path / "data" / "reviews"

    def review_run(run, rnd):
        _findings_json(data / f"r{rnd.number}")
        return loop.ReviewOutcome(str(data / f"r{rnd.number}"), True)

    review.review, review.post = review_run, lambda run, run_dir, path: (True, "")
    run, lines = _run(config, change, triage_call=FakeTriage({"add subtracts": FIX}), review=review,
                      self_check=FakeCheck([""]), decisions=None)
    with _Stubs(block_stubs(repo)):
        assert await loop.run_findings(run) == 0, lines
    head = git(repo, "rev-parse", "HEAD").strip()
    assert merge.ready(repo, head, "refs/remotes/origin/buildme/calc", "main") is None
    (repo / "notes.txt").write_text("mine\n")
    assert "notes.txt" in merge.ready(repo, head, "refs/remotes/origin/buildme/calc", "main")
    assert os.path.exists(repo / "notes.txt")


def test_a_fixer_that_cannot_start_ends_the_run_and_the_next_run_relaunches_it(tmp_path, monkeypatch):
    """PR D2 as it was: only the merge tests read a spawn failure as a failed
    run (above). A fixer that cannot start (EAGAIN) raises, ending the run
    and parking nothing, and the next run launches it."""
    from .test_epic_fix import N, Epic

    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"rounds": [{}]}]})
    real = coordinator._spawn_review

    def spawn(argv, *a, **k):
        if any("findings" in x for x in argv):
            raise OSError(11, "Resource temporarily unavailable")
        return real(argv, *a, **k)

    monkeypatch.setattr(coordinator, "_spawn_review", spawn)
    assert e.run() == 1 and e.status()["status"] != "parked"     # the orchestrator reports the error
    monkeypatch.setattr(coordinator, "_spawn_review", real)
    assert e.run() == 3 and e.status()["status"] == "built"
    assert e.fixes()[-1].result["exit_code"] == 0
