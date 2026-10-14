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
import time
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


def test_a_clean_merge_is_tested_and_pushed_and_not_reviewed_again(tmp_path, monkeypatch):
    """40.2-40.4: main moved under a's branch without a conflict. The tests run
    in <P> with BUILDME_TRUST_REPO=1 and the merge commit is pushed. Until E2
    the PR then waits for a human merge. That merge is the step's own, so the
    next run does not review it again; the same merge with a person's edit
    in any file is reviewed. No agent runs."""
    r = MergeRoot(tmp_path, monkeypatch, main={"uv.lock": "lock 2\n"})
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    [tests] = r.calls("merge-tests")
    assert (tests["cwd"], tests["trusted"]) == ("<root>/ws/.worktrees/buildme-e-a", "1")
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [0]
    assert len(r.step_pushes()) == 1
    assert r.origin("plugin.json") == CHILD["plugin.json"] and r.origin("uv.lock") == "lock 2\n"
    head = git(r.w, "rev-parse", "HEAD").strip()
    assert git(r.w, "rev-parse", "HEAD^2").strip() == r.moved
    assert merge.merges_onto(r.w, head, r.tries("review")[-1].started["head_sha"], "main")
    assert r.status()["status"] == "built" and r.status()["merge_stop"] is None
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]      # its own merge: not reviewed again
    assert len(r.tries("review")) == 1 and len(r.step_pushes()) == 1 and r.calls("merge-agent") == []
    reviewed = r.tries("review")[-1].started["head_sha"]
    edits = {"app.py": "x = 'not reviewed'\n", "plugin.json": CHILD["plugin.json"].replace('"x"', '"evil"'),
             "CHANGELOG.md": "anything at all\n", "uv.lock": "lock evil\n"}
    for rel, text in edits.items():
        (r.w / rel).write_text(text)
        git(r.w, "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit", "--", rel)
        assert not merge.merges_onto(r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "main"), rel
        git(r.w, "reset", "-q", "--hard", head)


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


@pytest.mark.parametrize("main_too", [False, True], ids=["untouched-by-the-merge", "merged-cleanly"])
@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_an_agent_that_edits_another_file_parks_and_the_hint_lets_it_retry(tmp_path, monkeypatch, staged, main_too):
    """40.3: the agent resolves every conflict and also changes `app.py`,
    staging it itself (it has Bash) or not; `app.py` is a file the merge did
    not touch, or one main changed and git merged cleanly. Parks, nothing
    pushed, with a hint that works: after it, `<W>` is clean (an abort alone
    would keep the edit, or refuse over it), so the next run retries (E.S26)
    and, given an agent that keeps to its files, pushes."""
    def edit(w):
        (w / "app.py").write_text("x = 9\n")
        staged and git(w, "add", "--", "app.py")

    r = MergeRoot(tmp_path, monkeypatch, agent=None, main={**MAIN, **({"app.py": "x = 2\n"} if main_too else {})})
    r.agent = FakeAgent(tmp_path, RESOLVED, stop=edit)
    assert r.run() == 0
    hint = f"git -C {r.w} checkout -- app.py, then git -C {r.w} merge --abort, then re-run"
    assert r.status()["reason"] == f"the merge agent changed files it may not: app.py; {hint}"
    assert r.step_pushes() == [] and r.tries("merge-tests") == []
    for step in hint.removesuffix(", then re-run").split(", then "):
        git(Path(step.split()[2]), *step.split()[3:])
    assert git(r.w, "status", "--porcelain") == "" and not r.merging()
    r.agent = FakeAgent(tmp_path, RESOLVED)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert len(r.calls("merge-agent")) == 2 and len(r.step_pushes()) == 1
    assert r.origin("app.py") == ("x = 2\n" if main_too else "x = 1\n")


def test_a_new_file_from_the_agent_is_named_for_removal(tmp_path, monkeypatch):
    """The agent leaves a new file: the hint removes it, as an abort keeps it."""
    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(tmp_path, RESOLVED, stop=lambda w: (w / "notes.md").write_text("x\n") and None)
    assert r.run() == 0
    assert r.status()["reason"] == (f"the merge agent changed files it may not: notes.md; rm -- {r.w / 'notes.md'}, "
                                    f"then git -C {r.w} merge --abort, then re-run")


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


def test_an_agent_that_raises_parks_with_the_error_and_its_spend(tmp_path, monkeypatch):
    """An ordinary error from the agent call (not a stop) is a failed call:
    the child parks with it, the `merge` record keeps its spend, and the run
    goes on and ends 0, its spend complete."""
    class Raising(FakeAgent):
        async def __call__(self, config, w, files, name, default):
            await super().__call__(config, w, files, name, default)
            raise RuntimeError("the SDK is not installed")

    r = MergeRoot(tmp_path, monkeypatch, agent=Raising(tmp_path, {}))
    assert r.run() == 0
    assert r.status()["reason"] == ("the merge agent failed: RuntimeError: the SDK is not installed; "
                                    f"{merge.abort_hint(r.w)}")
    [agent] = r.tries("merge")
    assert (agent.result["exit_code"], agent.result["spend"], agent.result["interrupted"]) == (1, 0.5, False)
    assert r.tries("merge-tests") == [] and r.step_pushes() == []


def test_a_version_both_sides_raised_alike_is_raised_past_main_and_pushed(tmp_path, monkeypatch):
    """40.3 when the child and main both raised 0.1.0 to 0.2.0: `plugin.json`
    merges cleanly, only the CHANGELOG conflicts. The agent gets both and
    writes 0.3.0 with the child's entry above main's; that is pushed. An
    agent that leaves 0.2.0, both bullets under main's heading, parks."""
    child = {"plugin.json": plugin("0.2.0"), "CHANGELOG.md": changelog(("0.2.0", "Child a."), ("0.1.0", "First."))}
    main = {"plugin.json": plugin("0.2.0"), "CHANGELOG.md": changelog(("0.2.0", "Main."), ("0.1.0", "First."))}
    resolved = {"plugin.json": plugin("0.3.0"),
                "CHANGELOG.md": changelog(("0.3.0", "Child a."), ("0.2.0", "Main."), ("0.1.0", "First."))}
    gits = {"CHANGELOG.md": "# Changelog\n\n## [0.2.0]\n\n- Child a.\n- Main.\n\n## [0.1.0]\n\n- First.\n"}
    r = MergeRoot(tmp_path / "kept", monkeypatch, child_files=child, main=main, agent=None)
    r.agent = FakeAgent(r.root, gits)
    assert r.run() == 0
    assert r.status()["reason"] == ("the merge agent changed more than 40.3 allows in CHANGELOG.md, plugin.json; "
                                    f"{merge.abort_hint(r.w)}")
    assert [x["files"] for x in r.calls("merge-agent")] == [["CHANGELOG.md", "plugin.json"]] and not r.step_pushes()
    r = MergeRoot(tmp_path / "raised", monkeypatch, child_files=child, main=main, agent=None)
    r.agent = FakeAgent(r.root, resolved)
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert [x["files"] for x in r.calls("merge-agent")] == [["CHANGELOG.md", "plugin.json"]]
    assert r.origin("plugin.json") == resolved["plugin.json"] and r.origin("CHANGELOG.md") == resolved["CHANGELOG.md"]
    reviewed = r.tries("review")[-1].started["head_sha"]
    assert merge.merges_onto(r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "main")


@pytest.mark.asyncio
async def test_the_merge_agent_runs_in_the_worktree_trusted_for_its_call_only(tmp_path, monkeypatch):
    """The coordinator vouches for the repo for its merge agent as it does for
    each child it launches (`--trust-repo`): `agent_call`'s gate passes. The
    call runs in <W> with Read, Edit and Bash, its prompt names the files,
    and the process is not left trusted after it."""
    from build_pipeline import sdk
    seen = {}

    async def agent_call(prompt, **kw):
        sdk._require_trusted_repo()
        seen.update(kw, prompt=prompt)
        return AgentResult(success=True, output="RESOLVED: plugin.json")

    monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
    monkeypatch.setattr(merge, "agent_call", agent_call)
    config = make_build(make_project(tmp_path))
    assert (await merge.run_merge_agent(config, tmp_path / "w", ["plugin.json", "a/uv.lock"], "b", "main")).success
    assert (seen["cwd"], seen["allowed_tools"], seen["budget_label"]) == (str(tmp_path / "w"), ["Read", "Edit", "Bash"],
                                                                          "merge")
    assert "- `plugin.json`\n- `a/uv.lock`" in seen["prompt"] and "`origin/main`" in seen["prompt"]
    assert "BUILDME_TRUST_REPO" not in os.environ


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


@pytest.mark.parametrize("case", ["clean", "refused"])
def test_over_budget_a_merge_needing_no_agent_still_runs(tmp_path, monkeypatch, case):
    """E.S27 holds back only a merge that would launch the agent: over
    budget, a clean merge is still tested and pushed, and a conflict the
    step may not resolve still parks, naming it, with no agent call."""
    main = {"uv.lock": "lock 2\n"} if case == "clean" else {"app.py": "x = 2\n"}
    r = MergeRoot(tmp_path, monkeypatch, child_files={**CHILD, "app.py": "x = 3\n"}, main=main)
    real = coordinator.Coordinator._over_budget

    def over(self):
        if r.moved:
            self.budget_stop = 1.0
        return real(self)

    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", over)
    if case == "clean":
        assert r.run() == 3 and len(r.step_pushes()) == 1 and len(r.tries("merge-tests")) == 1
        assert r.origin("uv.lock") == "lock 2\n"
    else:
        assert r.run() == 0 and r.merging()
        assert r.status()["reason"].startswith("merging origin/main conflicts in app.py, which the merge step may "
                                               "not resolve")
    assert r.calls("merge-agent") == []


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


class Sides:
    """A scratch repo: a base commit, *ours* on it and *theirs* on it. A
    file given None is deleted; bytes are written as they are."""

    def __init__(self, tmp_path: Path, base: dict, ours: dict, theirs: dict, *config: str):
        self.w = tmp_path / "m"
        self.w.mkdir(parents=True)
        git(self.w, "init", "-q")
        for item in config:
            git(self.w, "config", *item.split("=", 1))
        self.base = self.commit(base)
        self.ours = self.commit(ours)
        self.theirs = self.commit(theirs, at=self.base)

    def commit(self, files: dict, at: str | None = None) -> str:
        if at:
            git(self.w, "checkout", "-q", "--detach", at)
        for rel, text in files.items():
            if text is None:
                git(self.w, "rm", "-q", "--", rel)
                continue
            (self.w / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.w / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
        git(self.w, "add", "-A")
        git(self.w, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "c",
            "--allow-empty")
        return git(self.w, "rev-parse", "HEAD").strip()

    def plan(self) -> merge.Plan | None:
        return merge.plan(self.w, self.ours, self.theirs)

    def beyond(self, files: dict) -> list[str] | None:
        """`not_resolutions` of a merge result: *ours* with *files* written."""
        found = merge.not_resolutions(self.w, self.ours, self.theirs, self.commit(files, at=self.ours))
        return None if found is None else sorted(found)


def market(*versions: str) -> str:
    """A marketplace manifest: each plugin's version sits in `plugins[]`."""
    return json.dumps({"name": "m", "plugins": [{"name": f"p{i}", "source": f"./p{i}", "version": v}
                                                for i, v in enumerate(versions)]}, indent=2) + "\n"


def test_the_plan_gives_the_agent_versions_changelogs_and_lockfiles_only(tmp_path):
    """40.3: a conflict only in the package's own version fields (a JSON
    manifest's at any depth but under a dependency key, a marketplace's
    `plugins[].version` included), a CHANGELOG by name or a lockfile goes to
    the agent. A dependency's version, any other source line, a deletion on
    either side and a binary file are refused, a lockfile or CHANGELOG
    included."""
    sides = {"pyproject.toml": ('[project]\nversion = "{}"\n', "0.1.0", "0.2.0", "0.1.1"),
             "p.json": ('{{\n  "version": "{}"\n}}\n', "1", "2", "3"),
             ".claude-plugin/marketplace.json": (None, "0.1.0", "0.2.0", "0.1.1"),
             "v.py": ('__version__ = "{}"\n', "1.0", "2.0", "1.9"),
             "app.py": ("x = {}\n", "1", "3", "2"),
             "rate.py": ("r = conversion({})\n", "1", "3", "2"),
             "changelog.py": ("x = {}\n", "1", "3", "2"),
             "compat.py": ("if sys.version_info < (3, {}):\n", "8", "10", "9"),
             "Cargo.toml": ('[dependencies.serde]\nversion = "{}"\n', "1.0.0", "1.0.150", "1.0.200"),
             "dep.json": ('{{\n  "dependencies": {{\n    "version": "{}"\n  }}\n}}\n', "1", "2", "3"),
             "pubspec.yaml": ("dependencies:\n    version: {}\n", "1.0.0", "1.3.0", "1.2.0"),
             "CHANGELOG.rst": ("- {}\n", "a", "b", "c"), "a/uv.lock": ('version = "{}"\n', "1", "2", "3"),
             "docs/CHANGELOG.md": ("- {}\n", "a", "b", "c")}
    base, ours, theirs = ({k: market(vs[i]) if form is None else form.format(vs[i]) for k, (form, *vs) in sides.items()}
                          for i in range(3))
    base |= {"gone/CHANGELOG.md": "- a\n", "uv.lock": "lock 1\n", "img.png": b"\x89PNG\x00a"}
    ours |= {"gone/CHANGELOG.md": "- b\n", "uv.lock": None, "img.png": b"\x89PNG\x00b"}
    theirs |= {"gone/CHANGELOG.md": None, "uv.lock": "lock 2\n", "img.png": b"\x89PNG\x00c"}
    s = Sides(tmp_path, base, ours, theirs)
    p = s.plan()
    assert sorted(p.files) == [".claude-plugin/marketplace.json", "CHANGELOG.rst", "a/uv.lock", "docs/CHANGELOG.md",
                               "p.json", "pyproject.toml", "v.py"]
    assert sorted(p.refused) == ["Cargo.toml", "app.py", "changelog.py", "compat.py", "dep.json",
                                 "gone/CHANGELOG.md", "img.png", "pubspec.yaml", "rate.py", "uv.lock"]


def test_a_version_both_sides_raised_alike_must_be_the_next_after_main(tmp_path):
    """40.3 (finding: both sides raise the minor version from the same base):
    `plugin.json` and the marketplace merge cleanly at main's 0.65.0, and
    git trims the shared `## [0.65.0]` heading out of the CHANGELOG conflict.
    The plan still gives the agent both version files; a resolution must
    write 0.66.0 in each and file the child's entry under it, above main's.
    git's own merge, both bullets under main's heading, is named."""
    base = {"plugin.json": plugin("0.64.0"), "m.json": market("0.64.0", "1.0.0"),
            "CHANGELOG.md": changelog(("0.64.0", "First."))}
    ours = {"plugin.json": plugin("0.65.0"), "m.json": market("0.65.0", "1.0.0"),
            "CHANGELOG.md": changelog(("0.65.0", "Child."), ("0.64.0", "First."))}
    theirs = {"plugin.json": plugin("0.65.0"), "m.json": market("0.65.0", "1.0.0"),
              "CHANGELOG.md": changelog(("0.65.0", "Main."), ("0.64.0", "First."))}
    s = Sides(tmp_path, base, ours, theirs)
    assert s.plan() == merge.Plan(["CHANGELOG.md", "m.json", "plugin.json"], [])
    good = {"plugin.json": plugin("0.66.0"), "m.json": market("0.66.0", "1.0.0"),
            "CHANGELOG.md": changelog(("0.66.0", "Child."), ("0.65.0", "Main."), ("0.64.0", "First."))}
    assert s.beyond(good) == []
    gits = "# Changelog\n\n## [0.65.0]\n\n- Child.\n- Main.\n\n## [0.64.0]\n\n- First.\n"
    assert s.beyond({"plugin.json": plugin("0.65.0"), "m.json": market("0.65.0", "1.0.0"),
                     "CHANGELOG.md": gits}) == ["CHANGELOG.md", "m.json", "plugin.json"]
    assert s.beyond({**good, "m.json": market("0.66.0", "1.0.1")}) == ["m.json"]     # another plugin's version


def test_a_version_value_must_be_the_next_after_main_raising_the_child_s_part(tmp_path):
    """40.3, 'next after main': base 0.1.0, the child raised the minor
    version, main the patch or the minor. Keeping the child's version (a
    regression past main), main's, or any other value is named."""
    for main, want in (("0.1.1", "0.2.0"), ("0.3.0", "0.4.0")):
        s = Sides(tmp_path / main, {"plugin.json": plugin("0.1.0")}, {"plugin.json": plugin("0.2.0")},
                  {"plugin.json": plugin(main)})
        assert s.beyond({"plugin.json": plugin(want)}) == [], main
        for wrong in {"0.2.0", main, "0.0.1", "7.0.0"} - {want}:
            assert s.beyond({"plugin.json": plugin(wrong)}) == ["plugin.json"], (main, wrong)


def test_a_changelog_keeps_both_entries_in_full_lines_they_share_included(tmp_path):
    """40.3: both entries end with the same bullet, which git moves out of
    the conflict. Both entries in full pass, in either order; one that drops
    the child's copy of the shared bullet, keeps one entry, or changes
    another line, is named. A blank line does not count."""
    bump = "- Bump the plugin and marketplace versions."

    def log(*entries: tuple[str, str]) -> str:
        return "# Changelog\n" + "".join(f"\n## [{v}]\n\n- {text}\n{bump}\n" for v, text in entries)

    s = Sides(tmp_path, {"CHANGELOG.md": log(("0.64.0", "First."))},
              {"CHANGELOG.md": log(("0.65.0", "Child."), ("0.64.0", "First."))},
              {"CHANGELOG.md": log(("0.64.1", "Main."), ("0.64.0", "First."))})
    both = log(("0.65.0", "Child."), ("0.64.1", "Main."), ("0.64.0", "First."))
    cases = {both: [], log(("0.64.1", "Main."), ("0.65.0", "Child."), ("0.64.0", "First.")): [],
             both.replace("\n\n", "\n"): [],
             both.replace(f"- Child.\n{bump}\n", "- Child.\n", 1): ["CHANGELOG.md"],
             log(("0.65.0", "Child."), ("0.64.0", "First.")): ["CHANGELOG.md"],
             both.replace("- Main.", "- Child."): ["CHANGELOG.md"],
             both.replace("- First.", "- Changed."): ["CHANGELOG.md"]}
    for text, want in cases.items():
        assert s.beyond({"CHANGELOG.md": text}) == want, text


def _uv(project: str, req: str, digest: str) -> str:
    return (f'version = 1\n\n[[package]]\nname = "app"\nversion = "{project}"\nsource = {{ editable = "." }}\n\n'
            f'[[package]]\nname = "req"\nversion = "{req}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
            f'sdist = {{ url = "https://x/req-{req}.tar.gz", hash = "sha256:{digest}" }}\n')


def test_a_regenerated_lockfile_passes_and_one_mixing_sides_is_named(tmp_path):
    """40.3, 'lockfiles (regenerate)': both sides bumped the project and the
    one dependency in `uv.lock`. A regenerated lock (main's copy, the project
    at the version `pyproject.toml` got) passes, as does one taking the
    child's dependency whole. One side's dependency version with the other's
    hash, a hash line twice (the lock no longer loads), a line from neither
    side, or a version no version file wrote, is named."""
    def project(v: str) -> str:
        return f'[project]\nname = "app"\nversion = "{v}"\n'

    s = Sides(tmp_path, {"pyproject.toml": project("0.64.0"), "uv.lock": _uv("0.64.0", "1.0", "aa")},
              {"pyproject.toml": project("0.65.0"), "uv.lock": _uv("0.65.0", "1.1", "bb")},
              {"pyproject.toml": project("0.64.1"), "uv.lock": _uv("0.64.1", "1.2", "cc")})
    assert s.plan() == merge.Plan(["pyproject.toml", "uv.lock"], [])
    good = {"pyproject.toml": project("0.65.0"), "uv.lock": _uv("0.65.0", "1.2", "cc")}
    sdist = {v: f'sdist = {{ url = "https://x/req-{v}.tar.gz", hash = "sha256:{h}" }}\n' for v, h in (("1.1", "bb"),
                                                                                               ("1.2", "cc"))}
    cases = {"": {}, " ": {"uv.lock": _uv("0.65.0", "1.1", "bb")},
             "uv.lock": {"uv.lock": good["uv.lock"].replace(sdist["1.2"], sdist["1.1"])},
             " uv.lock": {"uv.lock": good["uv.lock"] + sdist["1.1"]},
             "  uv.lock": {"uv.lock": good["uv.lock"].replace("aa", "dd").replace("cc", "dd")},
             "   uv.lock": {"uv.lock": _uv("0.9.9", "1.2", "cc")}}
    for want, change in cases.items():
        assert s.beyond({**good, **change}) == ([want.strip()] if want.strip() else []), want


def test_a_lockfile_version_the_resolution_wrote_passes(tmp_path):
    """40.3: both sides raised the project alike, so the version files and the
    lock name the next version, which neither side holds."""
    s = Sides(tmp_path, {"plugin.json": plugin("0.64.0"), "uv.lock": _uv("0.64.0", "1.0", "aa")},
              {"plugin.json": plugin("0.65.0"), "uv.lock": _uv("0.65.0", "1.0", "aa")},
              {"plugin.json": plugin("0.65.0"), "uv.lock": _uv("0.65.0", "1.0", "aa"), "main.py": ""})
    assert s.plan() == merge.Plan(["plugin.json", "uv.lock"], [])
    assert s.beyond({"plugin.json": plugin("0.66.0"), "uv.lock": _uv("0.66.0", "1.0", "aa"), "main.py": ""}) == []
    assert s.beyond({"plugin.json": plugin("0.66.0"), "uv.lock": _uv("0.67.0", "1.0", "aa"), "main.py": ""}) \
        == ["uv.lock"]


def test_a_conflict_without_markers_is_never_the_step_s_resolution(tmp_path):
    """A modify/delete conflict (a lockfile and a CHANGELOG included) or a
    binary one, resolved by keeping one side, leaves no marker and matches
    git's own tree, yet it is named, so a merge holding it is reviewed."""
    s = Sides(tmp_path, {"app.py": "x = 1\n", "uv.lock": "a\n", "CHANGELOG.md": "- a\n", "logo.png": b"\x89PNG\x00a"},
              {"app.py": "x = 2\n", "uv.lock": "b\n", "CHANGELOG.md": None, "logo.png": b"\x89PNG\x00b"},
              {"app.py": None, "uv.lock": None, "CHANGELOG.md": "- c\n", "logo.png": b"\x89PNG\x00c"})
    assert s.plan() == merge.Plan([], ["CHANGELOG.md", "app.py", "logo.png", "uv.lock"])
    assert s.beyond({}) == ["CHANGELOG.md", "app.py", "logo.png", "uv.lock"]
    git(s.w, "update-ref", "refs/remotes/origin/x", s.theirs)
    git(s.w, "checkout", "-q", "--detach", s.ours)
    git(s.w, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-s", "ours", "-m", "m", s.theirs)
    assert not merge.merges_onto(s.w, git(s.w, "rev-parse", "HEAD").strip(), s.ours, "x")


def test_a_merge_in_progress_is_checked_the_same_in_any_conflict_style(tmp_path):
    """diff3 and zdiff3 markers carry a base side: a marker is found while it
    is left, and the resolution is checked against the three sides
    themselves, so the style changes nothing."""
    for style in ("merge", "diff3", "zdiff3"):
        s = Sides(tmp_path / style, BASE, CHILD, MAIN, f"merge.conflictStyle={style}")
        git(s.w, "checkout", "-q", "--detach", s.ours)
        subprocess.run([real_git(), "merge", "-q", "--no-ff", "--no-commit", s.theirs], cwd=s.w, capture_output=True)
        assert merge.plan(s.w, "HEAD", "MERGE_HEAD") == merge.Plan(["CHANGELOG.md", "plugin.json"], []), style
        assert merge.unresolved(s.w, ["CHANGELOG.md", "plugin.json"]) == ["CHANGELOG.md", "plugin.json"], style
        for rel, text in RESOLVED.items():
            (s.w / rel).write_text(text)
        git(s.w, "add", "--", *RESOLVED)
        tree = git(s.w, "write-tree").strip()
        assert merge.not_resolutions(s.w, "HEAD", "MERGE_HEAD", tree) == [], style


def test_a_large_lockfile_and_changelog_conflict_is_checked_in_seconds(tmp_path):
    """Thousands of conflicting lockfile lines and a long CHANGELOG history:
    the check is linear in the lockfile and len(main's CHANGELOG) x
    len(the child's entry) in the CHANGELOG, and the coordinator runs it in a
    thread."""
    def lock(tag: str) -> str:
        return 'version = 1\n' + "".join(f'\n[[package]]\nname = "p{i}"\nversion = "1.{tag}"\n' for i in range(3000))

    history = "".join(f"\n## [0.{i}.0]\n\n### Added\n- Thing {i}.\n- Other {i}.\n" for i in range(1000, 0, -1))

    def entry(v: str, who: str) -> str:
        return f"\n## [{v}]\n\n### Added\n" + "".join(f"- {who} {k}.\n" for k in range(30))

    s = Sides(tmp_path, {"uv.lock": lock("0"), "CHANGELOG.md": "# Changelog\n" + history},
              {"uv.lock": lock("1"), "CHANGELOG.md": "# Changelog\n" + entry("1.1.0", "Child") + history},
              {"uv.lock": lock("2"), "CHANGELOG.md": "# Changelog\n" + entry("1.0.1", "Main") + history})
    both = "# Changelog\n" + entry("1.1.0", "Child") + entry("1.0.1", "Main") + history
    started = time.monotonic()
    assert s.beyond({"uv.lock": lock("2"), "CHANGELOG.md": both}) == []
    assert time.monotonic() - started < 20


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
