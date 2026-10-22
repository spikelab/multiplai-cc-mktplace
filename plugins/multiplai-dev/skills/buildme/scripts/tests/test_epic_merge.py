"""Epic mode, PR E: the merge step (plan-buildme-epic-mode-2026-10-12.md,
items 35-42). Git is real; the build, `gh`, the review and the test command
are fakes first on PATH (`epic_row_helpers.Fakes`), and the merge agent is
`FakeAgent`. Under `auto` a pushed PR whose checks pass is merged by the fake
`gh pr merge` (PR E2), and b, waiting on a, is then built and merged; under
`manual` it waits for a human merge (`EPIC:MERGE_WAITING`, exit 3). A parked
child ends its run with exit 0, an `unknown` one with exit 1."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from build_pipeline import budget, git_ops, stop_record
from build_pipeline.config import EpicSettings, discover_test_command
from build_pipeline.epic import coordinator, merge, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.models import AgentResult

from .epic_row_helpers import (
    GIT_ENV,
    CallLog,
    EpicSplitAnswer,
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

CHAIN = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))]
)


def plugin(version: str) -> str:
    return json.dumps({"name": "x", "version": version}, indent=2) + "\n"


def changelog(*entries: tuple[str, str]) -> str:
    return "# Changelog\n" + "".join(f"\n## [{v}]\n\n- {text}\n" for v, text in entries)


BASE = {
    "plugin.json": plugin("0.1.0"),
    "CHANGELOG.md": changelog(("0.1.0", "First.")),
    "uv.lock": "lock 1\n",
    "app.py": "x = 1\n",
}
PENDING, PASSED = (
    [{"name": "ci", "bucket": "pending"}],
    [{"name": "ci", "bucket": "pass"}],
)
CHILD = {
    "plugin.json": plugin("0.2.0"),
    "CHANGELOG.md": changelog(("0.2.0", "Child a."), ("0.1.0", "First.")),
}
MAIN = {
    "plugin.json": plugin("0.1.1"),
    "CHANGELOG.md": changelog(("0.1.1", "Main."), ("0.1.0", "First.")),
    "uv.lock": "lock 2\n",
}
RESOLVED = {
    "plugin.json": plugin("0.2.0"),
    "CHANGELOG.md": changelog(
        ("0.2.0", "Child a."), ("0.1.1", "Main."), ("0.1.0", "First.")
    ),
}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


@pytest.fixture
def git_identity(monkeypatch):
    """The git identity `use_workspace` pins, for a test that runs git
    without it: a CI runner has none, so `commit` and `merge` refuse."""
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


def git(cwd: Path, *a: str) -> str:
    """The real git, so a test's own pushes are never logged as the step's."""
    return subprocess.run(
        [real_git(), *a], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def push_files(
    root: Path, files: dict[str, str], message: str, branch: str = "ENG-40-e"
) -> str:
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
            f.write(
                json.dumps({"kind": "merge-agent", "files": files}, sort_keys=True)
                + "\n"
            )
        for rel in files:
            if rel in self.resolve:
                (Path(w) / rel).write_text(self.resolve[rel])
        budget.add_process_spend(0.5, 10)
        if self.stop is not None:
            out = self.stop(Path(w))
            if out is not None:
                await out
        return AgentResult(
            success=True, output="\n".join(f"RESOLVED: {f}" for f in files)
        )


class MergeRoot:
    """Main holds BASE; an approved chain (b waits on a); a's build commits
    *child_files*; test command `fake-tests` (answering *tests*) unless
    *command* is None. *main* is pushed onto origin's ENG-40-e, the PR's
    base, at the `start` seam (the feature branch moving: a sibling merged
    into it, or main merged in). *sub*: the project's
    subdirectory, holding every path above; *outside* is pushed with *main*
    at the repo root. PR E2: *answer*
    replaces the chain, b's build commits *b_files*, and a's PR starts with
    the fields *pr* (`checks`, `merges`, `merge_state`)."""

    def __init__(
        self,
        root: Path,
        monkeypatch,
        *,
        child_files=CHILD,
        main=MAIN,
        tests=None,
        agent=None,
        merge_mode="auto",
        command="fake-tests",
        sub: str | None = None,
        outside: dict[str, str] | None = None,
        answer: EpicSplitAnswer = CHAIN,
        b_files: dict[str, str] | None = None,
        pr: dict | None = None,
    ):
        at = (
            (lambda files: {f"{sub}/{k}": v for k, v in files.items()}) if sub else dict
        )
        self.root, self.mp, self.main = (
            root,
            monkeypatch,
            main and {**at(main), **(outside or {})},
        )
        self.data = use_workspace(monkeypatch, root)
        # Another root's fakes off PATH, so its `git` shim logs none of this root's setup.
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join(
                p
                for p in os.environ["PATH"].split(os.pathsep)
                if not (Path(p).parent / "fakes.json").exists()
            ),
        )
        self.repo = repo = make_project(root)
        self.fakes = Fakes(
            root,
            monkeypatch,
            {
                "a": [{"commit": at(child_files), **({"pr": pr} if pr else {})}],
                "b": [{"commit": at(b_files or {"b.txt": "b\n"})}],
            },
        )
        self.project = repo / sub if sub else repo
        self.project.mkdir(exist_ok=True)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2, merge=merge_mode)
        files = {
            **BASE,
            **(
                {"specs/config.yaml": f"tdd:\n  test_command: {command}\n"}
                if command
                else {}
            ),
        }
        push_files(root, at(files), "base", "main")
        git(
            repo, "pull", "-q", "origin", "main"
        )  # the source checkout holds origin's config.yaml
        approve(
            self.config, answer, CallLog(root / "split.log")
        )  # the specs commit, on top, not pushed
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
        """The commit `point` pushed onto origin's ENG-40-e; a file, so a run in a forked child counts."""
        flag = self.root / "moved"
        return flag.read_text() if flag.exists() else None

    def point(self, coord, c, name: str) -> None:
        if name == "start" and self.main and not self.moved:
            (self.root / "moved").write_text(
                push_files(self.root, self.main, "main moved")
            )

    def run(self) -> int:
        self.use()
        return run_coordinator(self.config)

    def status(self, cid: str = "a") -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"][cid]

    def merged(self) -> bool:
        """a's PR merged by the step (PR E2), and no PR left waiting."""
        return merge_waiting() is None and self.status()["status"] == "merged"

    def set_checks(self, steps: list) -> None:
        """a's PR's `checks` from now on, read from the first step again."""
        self.fakes._update_prs(pr_url("e-a"), checks=steps)
        for f in (self.root / "checks-attempts").glob(f"{pr_number('e-a')}.*"):
            f.unlink()

    def pending(self) -> None:
        """A short `CHECKS_LIMIT`: a's check, pending (`pr={"checks": [PENDING]}`),
        outlasts it, and the run leaves its PR open (E.S8)."""
        self.mp.setattr(coordinator, "CHECKS_LIMIT", 3 * coordinator.CHECKS_WAIT)

    def tries(self, kind: str) -> list[records.Attempt]:
        return [
            a for a in records.attempts(self.dir) if a.child == "a" and a.kind == kind
        ]

    def calls(self, kind: str) -> list[dict]:
        return [
            x for x in CallLog(self.root / "calls.log").calls() if x["kind"] == kind
        ]

    def pushes(self) -> list[dict]:
        """Every `git push` the `git` shim saw."""
        return CallLog(self.root / "pushes.log").calls()

    def step_pushes(self) -> list[dict]:
        """`git push` calls but the fake build's own."""
        return [x for x in self.pushes() if not x["argv"].startswith("push -q -u")]

    def origin_sha(self, ref: str = "buildme/e-a") -> str:
        return git(self.root / "o" / "r.git", "rev-parse", ref).strip()

    def origin(self, rel: str, ref: str = "buildme/e-a") -> str:
        return git(self.root / "o" / "r.git", "show", f"{ref}:{rel}")

    def merging(self) -> bool:
        return merge.merging(self.w)


def test_version_and_changelog_conflicts_are_resolved_tested_and_pushed(
    tmp_path, monkeypatch
):
    """40.2-40.4: main moved under a's branch with its own version and
    CHANGELOG entry. One `merge` agent call resolves both (its spend in the
    record), the tests run in <P> with BUILDME_TRUST_REPO=1, and the merge
    commit is pushed. Its check stays pending, so the PR is left open (E.S8);
    the next run merges it with `gh pr merge` (PR E2) and does not review the
    step's own merge again."""
    r = MergeRoot(tmp_path, monkeypatch, pr={"checks": [PENDING]})
    r.pending()
    assert r.run() == 3 and merge_waiting() == [
        pr_url("e-a")
    ]  # E.S8: checks pending, the PR left open
    assert [x["files"] for x in r.calls("merge-agent")] == [
        ["CHANGELOG.md", "plugin.json"]
    ]
    [agent] = r.tries("merge")
    assert (agent.result["exit_code"], agent.result["spend"]) == (0, 0.5)
    [tests] = r.calls("merge-tests")
    assert (tests["cwd"], tests["trusted"]) == ("<root>/ws/.worktrees/buildme-e-a", "1")
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [0]
    assert len(r.step_pushes()) == 1
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]
    assert (
        r.origin("CHANGELOG.md") == RESOLVED["CHANGELOG.md"]
        and r.origin("uv.lock") == "lock 2\n"
    )
    head = git(r.w, "rev-parse", "HEAD").strip()
    assert git(r.w, "rev-parse", "HEAD^2").strip() == r.moved
    assert merge.merges_onto(
        r.w, head, r.tries("review")[-1].started["head_sha"], "ENG-40-e"
    )
    assert r.status()["status"] == "built" and r.status()["merge_stop"] is None
    r.set_checks([PASSED])
    assert r.run() == 0 and r.merged()  # its own merge was not reviewed: one review
    assert (
        len(r.tries("review")) == 1
        and len(r.calls("merge-agent")) == 1
        and len(r.step_pushes()) == 1
    )
    assert _merged_prs(r) == ["e-a", "e-b"]
    reviewed = r.tries("review")[-1].started["head_sha"]
    edits = {
        "app.py": "x = 'not reviewed'\n",  # the same merge carrying a person's edit is reviewed,
        "plugin.json": RESOLVED["plugin.json"].replace(
            '"x"', '"evil"'
        ),  # inside a resolved file too
        "CHANGELOG.md": "anything at all\n",
        "uv.lock": "lock evil\n",
    }
    for rel, text in edits.items():
        (r.w / rel).write_text(text)
        git(
            r.w,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--amend",
            "--no-edit",
            "--",
            rel,
        )
        assert not merge.merges_onto(
            r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "ENG-40-e"
        ), rel
        git(r.w, "reset", "-q", "--hard", head)
    (r.w / "uv.lock").write_text(
        'lock 2\nversion = "0.2.0"\n'
    )  # an unconflicted lockfile is git's own
    git(
        r.w,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--amend",
        "--no-edit",
        "--",
        "uv.lock",
    )
    assert not merge.merges_onto(
        r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "ENG-40-e"
    )


def test_a_clean_merge_is_tested_and_pushed_and_not_reviewed_again(
    tmp_path, monkeypatch
):
    """40.2-40.4: main moved under a's branch without a conflict. The tests run
    in <P> with BUILDME_TRUST_REPO=1 and the merge commit is pushed. Its
    check stays pending, so the PR is left open (E.S8); the next run merges
    it (PR E2). That merge is the step's own, so it is not reviewed again;
    the same merge with a person's edit in any file is. No agent runs."""
    r = MergeRoot(
        tmp_path, monkeypatch, main={"uv.lock": "lock 2\n"}, pr={"checks": [PENDING]}
    )
    r.pending()
    assert r.run() == 3 and merge_waiting() == [
        pr_url("e-a")
    ]  # E.S8: checks pending, the PR left open
    [tests] = r.calls("merge-tests")
    assert (tests["cwd"], tests["trusted"]) == ("<root>/ws/.worktrees/buildme-e-a", "1")
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [0]
    assert len(r.step_pushes()) == 1
    assert (
        r.origin("plugin.json") == CHILD["plugin.json"]
        and r.origin("uv.lock") == "lock 2\n"
    )
    head = git(r.w, "rev-parse", "HEAD").strip()
    assert git(r.w, "rev-parse", "HEAD^2").strip() == r.moved
    assert merge.merges_onto(
        r.w, head, r.tries("review")[-1].started["head_sha"], "ENG-40-e"
    )
    assert r.status()["status"] == "built" and r.status()["merge_stop"] is None
    r.set_checks([PASSED])
    assert r.run() == 0 and r.merged()  # its own merge was not reviewed: one review
    assert (
        len(r.tries("review")) == 1
        and len(r.step_pushes()) == 1
        and r.calls("merge-agent") == []
    )
    assert _merged_prs(r) == ["e-a", "e-b"]
    reviewed = r.tries("review")[-1].started["head_sha"]
    edits = {
        "app.py": "x = 'not reviewed'\n",
        "plugin.json": CHILD["plugin.json"].replace('"x"', '"evil"'),
        "CHANGELOG.md": "anything at all\n",
        "uv.lock": "lock evil\n",
    }
    for rel, text in edits.items():
        (r.w / rel).write_text(text)
        git(
            r.w,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--amend",
            "--no-edit",
            "--",
            rel,
        )
        assert not merge.merges_onto(
            r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "ENG-40-e"
        ), rel
        git(r.w, "reset", "-q", "--hard", head)


def test_a_branch_up_to_date_with_main_merges_with_no_tests_and_no_push(
    tmp_path, monkeypatch
):
    r = MergeRoot(tmp_path, monkeypatch, main=None)
    assert r.run() == 0 and r.merged()
    assert (
        r.calls("merge-tests") == []
        and r.step_pushes() == []
        and r.tries("merge") == []
    )


def test_a_source_file_conflict_parks_listing_it_and_stays_parked(
    tmp_path, monkeypatch
):
    """40.3: a conflicted file that is neither a version field, a CHANGELOG nor
    a lockfile parks before any agent call, with the merge left for a person
    to abort; nothing is pushed, and the next run does not touch it."""
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        child_files={"app.py": "x = 3\n"},
        main={"app.py": "x = 2\n"},
    )
    assert r.run() == 0
    st = r.status()
    assert st["status"] == "parked" and st["merge_park"] == st["reason"]
    assert st["reason"].startswith(
        "merging origin/ENG-40-e conflicts in app.py, which the merge step may not resolve; "
        f"git -C {r.w} merge --abort, then re-run"
    )
    assert r.calls("merge-agent") == [] and r.step_pushes() == [] and r.merging()
    assert st["merge_stop"]["edits"].keys() == {"app.py"}
    before = git(r.w, "status", "--porcelain")
    assert r.run() == 0 and r.status()["status"] == "parked"
    assert git(r.w, "status", "--porcelain") == before and r.calls("merge-tests") == []


def test_manual_merges_nothing_exits_3_and_continues_after_a_human_merge(
    tmp_path, monkeypatch
):
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
    monkeypatch.setattr(
        stop_record, "others_work", lambda *a, **k: "someone else's edit"
    )
    assert r.run() == 0
    assert ("merge", "--abort") not in ran and r.merging()
    assert (
        r.status()["reason"]
        == f"someone else's edit; git -C {r.w} merge --abort, then re-run"
    )


def test_failing_tests_park_and_push_nothing(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, tests=[{"exit": 1}])
    assert r.run() == 0
    [tests] = r.tries("merge-tests")
    assert r.status()["reason"] == (
        f"merge tests failed: fake-tests exited 1, see {tests.log}; "
        f"git -C {r.w} merge --abort, then re-run"
    )
    assert (
        r.step_pushes() == [] and r.merging() and r.status()["merge_stop"] is not None
    )


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
    assert r.status()["reason"] == (
        f"merge tests could not run: timed out after {coordinator.TESTS_TIMEOUT}s; "
        f"{merge.abort_hint(r.w)}"
    )
    [tests] = r.tries("merge-tests")
    assert (tests.result["exit_code"], tests.result["interrupted"]) == (None, False)
    assert r.step_pushes() == [] and r.merging()


def test_no_test_command_commits_and_pushes(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch, command=None)
    assert r.run() == 0 and r.merged()
    assert (
        r.tries("merge-tests") == [] and len(r.step_pushes()) == 1 and not r.merging()
    )
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]


def _failing_git(monkeypatch, *argv: str, stderr: str) -> None:
    """`merge.git` fails *argv* with *stderr*, running nothing; every other command runs."""
    real = merge.git
    monkeypatch.setattr(
        merge,
        "git",
        lambda w, *a: (
            git_ops.GitResult(["git", *a], 1, "", stderr) if a == argv else real(w, *a)
        ),
    )


def test_a_failed_merge_commit_parks_and_pushes_nothing(tmp_path, monkeypatch):
    """40.4: `git commit --no-edit` fails after the tests passed (a hook). The
    child parks with git's words and the abort hint, the merge is left in
    progress with `merge_stop` saved, and nothing is pushed."""
    r = MergeRoot(tmp_path, monkeypatch)
    _failing_git(
        monkeypatch, "commit", "--no-edit", stderr="error: pre-commit hook refused\n"
    )
    assert r.run() == 0
    assert (
        _parked_reason(r)
        == f"git commit failed: error: pre-commit hook refused; {merge.abort_hint(r.w)}"
    )
    assert r.merging() and r.status()["merge_stop"] is not None
    assert [a.result["exit_code"] for a in r.tries("merge-tests")] == [
        0
    ] and r.step_pushes() == []


def test_a_merge_that_fails_without_starting_parks_and_leaves_nothing(
    tmp_path, monkeypatch
):
    """40.2: `git merge` fails and leaves no `MERGE_HEAD` (here, refused
    outright). The child parks with git's words; nothing merged, tested or
    pushed, and `<W>` is as it was."""
    r = MergeRoot(tmp_path, monkeypatch)
    _failing_git(
        monkeypatch,
        "merge",
        "--no-ff",
        "--no-commit",
        "origin/ENG-40-e",
        stderr="fatal: refusing to merge unrelated histories\n",
    )
    assert r.run() == 0
    assert (
        _parked_reason(r)
        == "git merge origin/ENG-40-e failed: fatal: refusing to merge unrelated histories"
    )
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
    assert r.status()["reason"] == (
        "the merge agent left conflicts in CHANGELOG.md; "
        f"git -C {r.w} merge --abort, then re-run"
    )
    assert r.step_pushes() == [] and r.tries("merge-tests") == []


@pytest.mark.parametrize(
    "main_too", [False, True], ids=["untouched-by-the-merge", "merged-cleanly"]
)
@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_an_agent_that_edits_another_file_parks_and_the_hint_lets_it_retry(
    tmp_path, monkeypatch, staged, main_too
):
    """40.3: the agent resolves every conflict and also changes `app.py`,
    staging it itself (it has Bash) or not; `app.py` is a file the merge did
    not touch, or one main changed and git merged cleanly. Parks, nothing
    pushed, with a hint that works: after it, `<W>` is clean (an abort alone
    would keep the edit, or refuse over it), so the next run retries (E.S26)
    and, given an agent that keeps to its files, pushes."""

    def edit(w):
        (w / "app.py").write_text("x = 9\n")
        staged and git(w, "add", "--", "app.py")

    r = MergeRoot(
        tmp_path,
        monkeypatch,
        agent=None,
        main={**MAIN, **({"app.py": "x = 2\n"} if main_too else {})},
    )
    r.agent = FakeAgent(tmp_path, RESOLVED, stop=edit)
    assert r.run() == 0
    hint = (
        f"git -C {r.w} checkout -- app.py, then git -C {r.w} merge --abort, then re-run"
    )
    assert (
        r.status()["reason"]
        == f"the merge agent changed files it may not: app.py; {hint}"
    )
    assert r.step_pushes() == [] and r.tries("merge-tests") == []
    for step in hint.removesuffix(", then re-run").split(", then "):
        git(Path(step.split()[2]), *step.split()[3:])
    assert git(r.w, "status", "--porcelain") == "" and not r.merging()
    r.agent = FakeAgent(tmp_path, RESOLVED)
    assert r.run() == 0 and r.merged()
    assert len(r.calls("merge-agent")) == 2 and len(r.step_pushes()) == 1
    assert r.origin("app.py") == ("x = 2\n" if main_too else "x = 1\n")


def test_a_new_file_from_the_agent_is_named_for_removal(tmp_path, monkeypatch):
    """The agent leaves a new file: the hint removes it, as an abort keeps it."""
    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(
        tmp_path, RESOLVED, stop=lambda w: (w / "notes.md").write_text("x\n") and None
    )
    assert r.run() == 0
    assert r.status()["reason"] == (
        f"the merge agent changed files it may not: notes.md; rm -- {r.w / 'notes.md'}, "
        f"then git -C {r.w} merge --abort, then re-run"
    )


def test_a_failed_agent_call_parks_and_commits_no_marker(tmp_path, monkeypatch):
    class Failing(FakeAgent):
        async def __call__(self, config, w, files, name, default):
            await super().__call__(config, w, files, name, default)
            return AgentResult(success=False, output="", error="rate limited")

    r = MergeRoot(tmp_path, monkeypatch, agent=Failing(tmp_path, {}))
    assert r.run() == 0
    assert (
        r.status()["reason"]
        == f"the merge agent failed: rate limited; {merge.abort_hint(r.w)}"
    )
    assert [a.result["exit_code"] for a in r.tries("merge")] == [1] and r.merging()
    assert r.tries("merge-tests") == [] and r.step_pushes() == []


def test_an_agent_edit_beyond_the_version_lines_of_a_resolved_file_parks(
    tmp_path, monkeypatch
):
    """40.3, by content: the agent resolves `plugin.json`'s version and also
    renames the plugin in it. Parks, naming the file; nothing pushed."""
    r = MergeRoot(tmp_path, monkeypatch, agent=None)
    r.agent = FakeAgent(
        tmp_path,
        {**RESOLVED, "plugin.json": RESOLVED["plugin.json"].replace('"x"', '"evil"')},
    )
    assert r.run() == 0
    assert r.status()["reason"] == (
        "the merge agent changed more than 40.3 allows in plugin.json; "
        f"{merge.abort_hint(r.w)}"
    )
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
    assert r.status()["reason"] == (
        "the merge agent failed: RuntimeError: the SDK is not installed; "
        f"{merge.abort_hint(r.w)}"
    )
    [agent] = r.tries("merge")
    assert (
        agent.result["exit_code"],
        agent.result["spend"],
        agent.result["interrupted"],
    ) == (1, 0.5, False)
    assert r.tries("merge-tests") == [] and r.step_pushes() == []


def test_a_version_both_sides_raised_alike_is_raised_past_main_and_pushed(
    tmp_path, monkeypatch
):
    """40.3 when the child and main both raised 0.1.0 to 0.2.0: `plugin.json`
    merges cleanly, only the CHANGELOG conflicts. The agent gets both and
    writes 0.3.0 with the child's entry above main's; that is pushed. An
    agent that leaves 0.2.0, both bullets under main's heading, parks."""
    child = {
        "plugin.json": plugin("0.2.0"),
        "CHANGELOG.md": changelog(("0.2.0", "Child a."), ("0.1.0", "First.")),
    }
    main = {
        "plugin.json": plugin("0.2.0"),
        "CHANGELOG.md": changelog(("0.2.0", "Main."), ("0.1.0", "First.")),
    }
    resolved = {
        "plugin.json": plugin("0.3.0"),
        "CHANGELOG.md": changelog(
            ("0.3.0", "Child a."), ("0.2.0", "Main."), ("0.1.0", "First.")
        ),
    }
    gits = {
        "CHANGELOG.md": "# Changelog\n\n## [0.2.0]\n\n- Child a.\n- Main.\n\n## [0.1.0]\n\n- First.\n"
    }
    r = MergeRoot(
        tmp_path / "kept", monkeypatch, child_files=child, main=main, agent=None
    )
    r.agent = FakeAgent(r.root, gits)
    assert r.run() == 0
    assert r.status()["reason"] == (
        "the merge agent changed more than 40.3 allows in CHANGELOG.md, plugin.json; "
        f"{merge.abort_hint(r.w)}"
    )
    assert [x["files"] for x in r.calls("merge-agent")] == [
        ["CHANGELOG.md", "plugin.json"]
    ] and not r.step_pushes()
    r = MergeRoot(
        tmp_path / "raised", monkeypatch, child_files=child, main=main, agent=None
    )
    r.agent = FakeAgent(r.root, resolved)
    assert r.run() == 0 and r.merged()
    assert [x["files"] for x in r.calls("merge-agent")] == [
        ["CHANGELOG.md", "plugin.json"]
    ]
    assert (
        r.origin("plugin.json") == resolved["plugin.json"]
        and r.origin("CHANGELOG.md") == resolved["CHANGELOG.md"]
    )
    reviewed = r.tries("review")[-1].started["head_sha"]
    assert merge.merges_onto(
        r.w, git(r.w, "rev-parse", "HEAD").strip(), reviewed, "ENG-40-e"
    )


@pytest.mark.asyncio
async def test_the_merge_agent_runs_in_the_worktree_trusted_for_its_call_only(
    tmp_path, monkeypatch, git_identity
):
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
    assert (
        await merge.run_merge_agent(
            config, tmp_path / "w", ["plugin.json", "a/uv.lock"], "b", "main"
        )
    ).success
    assert (seen["cwd"], seen["allowed_tools"], seen["budget_label"]) == (
        str(tmp_path / "w"),
        ["Read", "Edit", "Bash"],
        "merge",
    )
    assert (
        "- `plugin.json`\n- `a/uv.lock`" in seen["prompt"]
        and "`origin/main`" in seen["prompt"]
    )
    assert "BUILDME_TRUST_REPO" not in os.environ
    monkeypatch.setenv(
        "BUILDME_TRUST_REPO", "0"
    )  # a value the run started with comes back
    assert (
        await merge.run_merge_agent(
            config, tmp_path / "w", ["plugin.json"], "b", "main"
        )
    ).success
    assert os.environ["BUILDME_TRUST_REPO"] == "0"


def test_over_budget_before_a_conflicted_merge_merges_nothing(tmp_path, monkeypatch):
    """Decided 6, E.S27: `epic.max_usd` reached when the merge would need the
    agent. Nothing is merged (no `MERGE_HEAD`, nothing changed in <W>), the
    child stays `built`, and a run with the ceiling raised merges."""
    r = MergeRoot(tmp_path, monkeypatch)
    real = coordinator.Coordinator._over_budget

    def over(self):
        if r.moved:  # after the review, at the merge step
            self.budget_stop = 1.0
        return real(self)

    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", over)
    assert r.run() == 3
    assert not r.merging() and git(r.w, "status", "--porcelain") == ""
    assert (
        r.status()["status"] == "built"
        and not r.status()["merge_park"]
        and r.status()["merge_stop"] is None
    )
    assert (
        r.calls("merge-agent") == []
        and r.tries("merge") == []
        and r.step_pushes() == []
    )
    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", real)
    assert r.run() == 0 and r.merged() and len(r.step_pushes()) == 1


@pytest.mark.parametrize("case", ["clean", "refused"])
def test_over_budget_a_merge_needing_no_agent_still_runs(tmp_path, monkeypatch, case):
    """E.S27 holds back only a merge that would launch the agent: over
    budget, a clean merge is still tested and pushed, and a conflict the
    step may not resolve still parks, naming it, with no agent call."""
    main = {"uv.lock": "lock 2\n"} if case == "clean" else {"app.py": "x = 2\n"}
    r = MergeRoot(
        tmp_path, monkeypatch, child_files={**CHILD, "app.py": "x = 3\n"}, main=main
    )
    real = coordinator.Coordinator._over_budget

    def over(self):
        if r.moved:
            self.budget_stop = 1.0
        return real(self)

    monkeypatch.setattr(coordinator.Coordinator, "_over_budget", over)
    if case == "clean":
        assert (
            r.run() == 3
            and len(r.step_pushes()) == 1
            and len(r.tries("merge-tests")) == 1
        )
        assert r.origin("uv.lock") == "lock 2\n"
    else:
        assert r.run() == 0 and r.merging()
        assert r.status()["reason"].startswith(
            "merging origin/ENG-40-e conflicts in app.py, which the merge step may "
            "not resolve"
        )
    assert r.calls("merge-agent") == []


def test_a_failed_fetch_leaves_the_child_unknown_and_the_run_exits_1(
    tmp_path, monkeypatch, capsys, caplog
):
    r = MergeRoot(tmp_path, monkeypatch)
    real = coordinator._fetch_origin

    async def fetch(repo):
        return (
            "fatal: unable to access origin"
            if ".worktrees" in str(repo)
            else await real(repo)
        )

    monkeypatch.setattr(coordinator, "_fetch_origin", fetch)
    assert r.run() == 1 and merge_waiting() is None
    reason = "cannot merge: git fetch origin failed: fatal: unable to access origin"
    assert (r.status()["status"], r.status()["reason"]) == ("unknown", reason)
    assert (
        f"EPIC: child a unknown: {reason}" in capsys.readouterr().err
        and reason in caplog.text
    )
    assert not r.merging() and r.step_pushes() == []


def test_a_failed_review_of_a_new_head_is_not_waited_on_as_passed(
    tmp_path, monkeypatch, capsys
):
    """A PR whose review was clean gets a new head, whose review fails (C.S20):
    the child stays `unknown` and the run exits 1, never naming the unreviewed
    head in `EPIC:MERGE_WAITING` as if it passed review."""
    from .test_epic_review import READ_FAILURES, Epic

    e = Epic(
        tmp_path,
        monkeypatch,
        reviews={str(pr_number("e-a")): [{}, {"exit": 2, "stderr": READ_FAILURES[0]}]},
    )
    assert e.run() == 3 and sorted(merge_waiting()) == sorted(
        [pr_url("e-a"), pr_url("e-b")]
    )
    e.fakes.set_head("e-a", "1" * 40)
    assert e.run() == 1 and merge_waiting() is None
    assert (
        e.child_status("a")["status"] == "unknown"
        and "EPIC: child a unknown: " in capsys.readouterr().err
    )


def test_a_stopped_review_of_a_new_head_is_not_waited_on_as_passed(
    tmp_path, monkeypatch
):
    """Item 41: a's review was clean; its PR then gets a new head, whose review
    is killed (C.S3). a's last clean review is of the older head, so the run
    names only b in `EPIC:MERGE_WAITING`, never a's unreviewed head."""
    from .test_epic_review import Epic

    e = Epic(
        tmp_path, monkeypatch, reviews={str(pr_number("e-a")): [{}, {"exit": 137}]}
    )
    assert e.run() == 3 and sorted(merge_waiting()) == sorted(
        [pr_url("e-a"), pr_url("e-b")]
    )
    e.fakes.set_head("e-a", "1" * 40)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.child_status("a")["status"] == "built" and len(e.reviews("a")) == 2


def test_a_pr_head_no_review_finished_on_never_ends_the_run_with_0(
    tmp_path, monkeypatch, capsys
):
    """Both PRs' reviews were clean; each then gets a new head, whose review is
    killed (C.S3). No PR passed review at its head, so nothing is waited on,
    yet both still need a review: the run exits 1 naming each on stderr, and
    the next run reviews them."""
    from .test_epic_review import Epic

    e = Epic(
        tmp_path,
        monkeypatch,
        reviews={str(pr_number(ch)): [{}, {"exit": 137}] for ch in ("e-a", "e-b")},
    )
    assert e.run() == 3
    for ch in ("e-a", "e-b"):
        e.fakes.set_head(ch, "1" * 40)
    capsys.readouterr()
    assert e.run() == 1 and merge_waiting() is None
    err = capsys.readouterr().err
    for cid in ("a", "b"):
        assert e.child_status(cid)["status"] == "built"
        assert (
            f"EPIC: child {cid} built: its PR's head 111111111111 has no finished review; "
            f"the next run reviews it"
        ) in err


def test_a_pr_head_with_a_finished_review_is_never_named_as_unreviewed(
    tmp_path, monkeypatch, capsys
):
    """Each PR's review at its head left a blocker, and its fixer exited 0
    without a new head: the PRs have not passed review, yet their heads have a
    finished review, so the run names neither as having none."""
    from .test_epic_review import Epic, high

    e = Epic(
        tmp_path,
        monkeypatch,
        reviews={str(pr_number(ch)): [{"findings": [high()]}] for ch in ("e-a", "e-b")},
    )
    assert e.run() == 0 and merge_waiting() is None
    assert "has no finished review" not in capsys.readouterr().err
    assert e.child_status("a")["status"] == e.child_status("b")["status"] == "built"


def test_40_1_parks_a_worktree_behind_the_reviewed_head_naming_what_to_run(
    tmp_path, monkeypatch
):
    """40.1 (E.S25): the review passed at a head pushed from another clone,
    which <W> lacks; its HEAD, older, is on `origin/<branch>`. The run
    fetches first (to merge main into the feature branch), so <W>'s repo has
    the head: it parks, nothing merged, with the fast-forward to run; a
    re-run before that parks again, and the one after merges onto the
    reviewed head."""
    r = MergeRoot(tmp_path, monkeypatch, merge_mode="manual")
    assert r.run() == 3
    prs = json.loads((r.root / "prs.json").read_text())
    prs[pr_url("e-a")]["head"] = new = push_files(
        r.root, {"other.py": "y = 1\n"}, "another clone", branch="buildme/e-a"
    )
    (r.root / "prs.json").write_text(json.dumps(prs))
    r.config.epic = EpicSettings(max_parallel=2, merge="auto")
    for _ in range(2):
        assert r.run() == 0 and r.status()["merge_park"]
        assert r.status()["reason"].endswith(
            f"is behind the reviewed head {new[:12]}, pushed from elsewhere; "
            f"git -C {r.w} merge --ff-only {new}, then re-run"
        )
    assert r.status()["reason"].endswith(
        f"is behind the reviewed head {new[:12]}, pushed from elsewhere; "
        f"git -C {r.w} merge --ff-only {new}, then re-run"
    )
    assert not r.merging() and r.calls("merge-tests") == [] and r.step_pushes() == []
    git(r.w, "merge", "-q", "--ff-only", new)
    assert r.run() == 0 and r.merged()
    assert len(r.step_pushes()) == 1 and git(r.w, "rev-parse", "HEAD^1").strip() == new


def test_a_person_s_merge_commit_in_the_worktree_is_never_pushed_unreviewed(
    tmp_path, monkeypatch
):
    """Item 37 takes only the step's own merges: a source-file conflict parks,
    and the person resolves it and commits in <W> instead of aborting. The
    re-run (E.S26: <W> is clean) parks at 40.1, nothing tested or pushed."""
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        child_files={"app.py": "x = 3\n"},
        main={"app.py": "x = 2\n"},
    )
    assert r.run() == 0 and r.merging()
    (r.w / "app.py").write_text("x = 'mine'\n")
    git(r.w, "add", "--", "app.py")
    git(r.w, "-c", "commit.gpgsign=false", "commit", "-q", "--no-edit")
    assert r.run() == 0
    assert r.status()["reason"].endswith(
        "so it holds commits the review did not see; push them to be reviewed, "
        "then re-run"
    )
    assert r.calls("merge-tests") == [] and r.step_pushes() == []


def test_a_push_failing_for_another_reason_is_unknown_and_the_next_run_pushes(
    tmp_path, monkeypatch, capsys
):
    """E.S24: origin's hook refuses the push, its branch unmoved: the child is
    `unknown` with git's words, not parked, and the run exits 1 naming it (a
    read of its open PR does not make it `built`); the next run retests and
    pushes (item 37, E.S6)."""
    r = MergeRoot(tmp_path, monkeypatch)
    hook = r.root / "o" / "r.git" / "hooks" / "pre-receive"

    def refusing(coord, c, name):
        r.point(coord, c, name)
        name == "push" and hook.write_text(
            "#!/bin/sh\necho 'denied by policy' >&2\nexit 1\n"
        ) and hook.chmod(0o755)

    r.use()
    monkeypatch.setattr(coordinator, "_merge_point", refusing)
    assert run_coordinator(r.config) == 1 and merge_waiting() is None
    assert (
        "EPIC: child a unknown: cannot merge: git push failed: remote: denied by policy"
        in capsys.readouterr().err
    )
    assert (
        r.status()["status"] == "unknown"
        and not r.status()["merge_park"]
        and r.step_pushes()
    )
    hook.unlink()
    assert r.run() == 0 and r.merged()
    assert (
        len(r.step_pushes()) == 2
        and len(r.tries("merge-tests")) == 2
        and len(r.calls("merge-agent")) == 1
    )
    assert r.origin("plugin.json") == RESOLVED["plugin.json"]


class Sides:
    """A scratch repo: a base commit, *ours* on it and *theirs* on it. A
    file given None is deleted; bytes are written as they are."""

    def __init__(
        self, tmp_path: Path, base: dict, ours: dict, theirs: dict, *config: str
    ):
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
            (self.w / rel).write_bytes(
                text if isinstance(text, bytes) else text.encode()
            )
        git(self.w, "add", "-A")
        git(
            self.w,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "c",
            "--allow-empty",
        )
        return git(self.w, "rev-parse", "HEAD").strip()

    def plan(self) -> merge.Plan | None:
        return merge.plan(self.w, self.ours, self.theirs)

    def beyond(self, files: dict) -> list[str] | None:
        """`not_resolutions` of a merge result: *ours* with *files* written."""
        found = merge.not_resolutions(
            self.w, self.ours, self.theirs, self.commit(files, at=self.ours)
        )
        return None if found is None else sorted(found)


def market(*versions: str) -> str:
    """A marketplace manifest: each plugin's version sits in `plugins[]`."""
    return (
        json.dumps(
            {
                "name": "m",
                "plugins": [
                    {"name": f"p{i}", "source": f"./p{i}", "version": v}
                    for i, v in enumerate(versions)
                ],
            },
            indent=2,
        )
        + "\n"
    )


def test_the_plan_gives_the_agent_versions_changelogs_and_lockfiles_only(tmp_path):
    """40.3: a conflict only in the package's own version fields (a JSON
    manifest's top-level one, a marketplace's `plugins[].version` included),
    a CHANGELOG by name or a lockfile goes to the agent. A dependency's
    version, any other source line, a deletion on either side and a binary
    file are refused, a lockfile or CHANGELOG included."""
    sides = {
        "pyproject.toml": ('[project]\nversion = "{}"\n', "0.1.0", "0.2.0", "0.1.1"),
        "p.json": ('{{\n  "version": "{}"\n}}\n', "1", "2", "3"),
        ".claude-plugin/marketplace.json": (None, "0.1.0", "0.2.0", "0.1.1"),
        "v.py": ('__version__ = "{}"\n', "1.0", "2.0", "1.9"),
        "app.py": ("x = {}\n", "1", "3", "2"),
        "rate.py": ("r = conversion({})\n", "1", "3", "2"),
        "changelog.py": ("x = {}\n", "1", "3", "2"),
        "compat.py": ("if sys.version_info < (3, {}):\n", "8", "10", "9"),
        "Cargo.toml": (
            '[dependencies.serde]\nversion = "{}"\n',
            "1.0.0",
            "1.0.150",
            "1.0.200",
        ),
        "dep.json": (
            '{{\n  "dependencies": {{\n    "version": "{}"\n  }}\n}}\n',
            "1",
            "2",
            "3",
        ),
        "pubspec.yaml": ("dependencies:\n    version: {}\n", "1.0.0", "1.3.0", "1.2.0"),
        "CHANGELOG.rst": ("- {}\n", "a", "b", "c"),
        "a/uv.lock": ('version = "{}"\n', "1", "2", "3"),
        "docs/CHANGELOG.md": ("- {}\n", "a", "b", "c"),
    }
    base, ours, theirs = (
        {
            k: market(vs[i]) if form is None else form.format(vs[i])
            for k, (form, *vs) in sides.items()
        }
        for i in range(3)
    )
    base |= {
        "gone/CHANGELOG.md": "- a\n",
        "uv.lock": "lock 1\n",
        "img.png": b"\x89PNG\x00a",
    }
    ours |= {"gone/CHANGELOG.md": "- b\n", "uv.lock": None, "img.png": b"\x89PNG\x00b"}
    theirs |= {
        "gone/CHANGELOG.md": None,
        "uv.lock": "lock 2\n",
        "img.png": b"\x89PNG\x00c",
    }
    s = Sides(tmp_path, base, ours, theirs)
    p = s.plan()
    assert sorted(p.files) == [
        ".claude-plugin/marketplace.json",
        "CHANGELOG.rst",
        "a/uv.lock",
        "docs/CHANGELOG.md",
        "p.json",
        "pyproject.toml",
        "v.py",
    ]
    assert sorted(p.refused) == [
        "Cargo.toml",
        "app.py",
        "changelog.py",
        "compat.py",
        "dep.json",
        "gone/CHANGELOG.md",
        "img.png",
        "pubspec.yaml",
        "rate.py",
        "uv.lock",
    ]


def test_a_version_both_sides_raised_alike_must_be_the_next_after_main(tmp_path):
    """40.3 (finding: both sides raise the minor version from the same base):
    `plugin.json` and the marketplace merge cleanly at main's 0.65.0, and
    git trims the shared `## [0.65.0]` heading out of the CHANGELOG conflict.
    The plan still gives the agent both version files; a resolution must
    write 0.66.0 in each and file the child's entry under it, above main's.
    git's own merge, both bullets under main's heading, is named."""
    base = {
        "plugin.json": plugin("0.64.0"),
        "m.json": market("0.64.0", "1.0.0"),
        "CHANGELOG.md": changelog(("0.64.0", "First.")),
    }
    ours = {
        "plugin.json": plugin("0.65.0"),
        "m.json": market("0.65.0", "1.0.0"),
        "CHANGELOG.md": changelog(("0.65.0", "Child."), ("0.64.0", "First.")),
    }
    theirs = {
        "plugin.json": plugin("0.65.0"),
        "m.json": market("0.65.0", "1.0.0"),
        "CHANGELOG.md": changelog(("0.65.0", "Main."), ("0.64.0", "First.")),
    }
    s = Sides(tmp_path, base, ours, theirs)
    assert s.plan() == merge.Plan(["CHANGELOG.md", "m.json", "plugin.json"], [])
    good = {
        "plugin.json": plugin("0.66.0"),
        "m.json": market("0.66.0", "1.0.0"),
        "CHANGELOG.md": changelog(
            ("0.66.0", "Child."), ("0.65.0", "Main."), ("0.64.0", "First.")
        ),
    }
    assert s.beyond(good) == []
    gits = (
        "# Changelog\n\n## [0.65.0]\n\n- Child.\n- Main.\n\n## [0.64.0]\n\n- First.\n"
    )
    assert s.beyond(
        {
            "plugin.json": plugin("0.65.0"),
            "m.json": market("0.65.0", "1.0.0"),
            "CHANGELOG.md": gits,
        }
    ) == ["CHANGELOG.md", "m.json", "plugin.json"]
    assert s.beyond({**good, "m.json": market("0.66.0", "1.0.1")}) == [
        "m.json"
    ]  # another plugin's version


def test_a_version_value_must_be_the_next_after_main_raising_the_child_s_part(tmp_path):
    """40.3, 'next after main': base 0.1.0, the child raised the minor
    version, main the patch or the minor. Keeping the child's version (a
    regression past main), main's, or any other value is named."""
    for main, want in (("0.1.1", "0.2.0"), ("0.3.0", "0.4.0")):
        s = Sides(
            tmp_path / main,
            {"plugin.json": plugin("0.1.0")},
            {"plugin.json": plugin("0.2.0")},
            {"plugin.json": plugin(main)},
        )
        assert s.beyond({"plugin.json": plugin(want)}) == [], main
        for wrong in {"0.2.0", main, "0.0.1", "7.0.0"} - {want}:
            assert s.beyond({"plugin.json": plugin(wrong)}) == ["plugin.json"], (
                main,
                wrong,
            )


def test_a_version_value_with_a_lowered_or_a_shorter_version(tmp_path):
    """40.3, `_next` (finding 69b7b0329b): a child that lowered its version
    has no version after main's, so any value is named. Versions with
    different numbers of parts are padded with 0: base 1.0, child 1.1, main
    1.0.1 gives 1.1.0; base 1.0, child 1.0.1, main 1.1 gives 1.1.1."""
    s = Sides(
        tmp_path / "lowered",
        {"plugin.json": plugin("0.2.0")},
        {"plugin.json": plugin("0.1.0")},
        {"plugin.json": plugin("0.2.1")},
    )
    assert s.plan() == merge.Plan(["plugin.json"], [])
    for value in ("0.1.0", "0.2.1", "0.2.2", "0.3.0"):
        assert s.beyond({"plugin.json": plugin(value)}) == ["plugin.json"], value
    for base, ours, main, want in (
        ("1.0", "1.1", "1.0.1", "1.1.0"),
        ("1.0", "1.0.1", "1.1", "1.1.1"),
    ):
        s = Sides(
            tmp_path / ours,
            {"plugin.json": plugin(base)},
            {"plugin.json": plugin(ours)},
            {"plugin.json": plugin(main)},
        )
        assert s.beyond({"plugin.json": plugin(want)}) == [], (ours, want)
        for wrong in {ours, main, "1.2"} - {want}:
            assert s.beyond({"plugin.json": plugin(wrong)}) == ["plugin.json"], (
                ours,
                wrong,
            )


def test_a_version_in_each_package_table_is_the_package_s_own(tmp_path):
    """40.3, `_PACKAGE_TABLES` (finding a586457d69): a version conflict under
    Cargo's `[package]` or `[workspace.package]`, `[tool.poetry]` or
    setup.cfg's `[metadata]` goes to the agent, which must write the next
    version after main's there; the same field under `[dependencies]` is
    refused."""
    files = {
        "Cargo.toml": ("package", '"{}"'),
        "a/Cargo.toml": ("workspace.package", '"{}"'),
        "pyproject.toml": ("tool.poetry", '"{}"'),
        "setup.cfg": ("metadata", "{}"),
    }
    for name, (table, value) in files.items():

        def text(v: str, table: str = table) -> str:
            return f'[{table}]\nname = "x"\nversion = {value.format(v)}\n'

        s = Sides(
            tmp_path / name.replace("/", "-"),
            {name: text("0.1.0")},
            {name: text("0.2.0")},
            {name: text("0.1.1")},
        )
        assert s.plan() == merge.Plan([name], []), name
        assert s.beyond({name: text("0.2.0")}) == [], name
        assert s.beyond({name: text("0.1.1")}) == [name], name
        s = Sides(
            tmp_path / f"dep-{name.replace('/', '-')}",
            {name: text("0.1.0", "dependencies")},
            {name: text("0.2.0", "dependencies")},
            {name: text("0.1.1", "dependencies")},
        )
        assert s.plan() == merge.Plan([], [name]), name


def test_a_version_file_both_sides_added_and_a_lockfile_both_deleted(tmp_path):
    """A version file both sides added at different versions has no version
    after main's to write: any value is named (finding 890669a415). A
    lockfile both sides deleted, which git merges cleanly, is not given to
    the agent, which could not resolve it (finding f5aaa88565)."""
    s = Sides(
        tmp_path / "added",
        {"app.py": ""},
        {"p.json": plugin("1.0.0")},
        {"p.json": plugin("2.0.0")},
    )
    assert s.plan() == merge.Plan(["p.json"], [])
    for value in ("1.0.0", "2.0.0", "2.1.0"):
        assert s.beyond({"p.json": plugin(value)}) == ["p.json"], value
    s = Sides(
        tmp_path / "deleted",
        {"plugin.json": plugin("0.1.0"), "poetry.lock": "a\n"},
        {"plugin.json": plugin("0.2.0"), "poetry.lock": None},
        {"plugin.json": plugin("0.2.0"), "poetry.lock": None, "main.py": ""},
    )
    assert s.plan() == merge.Plan(["plugin.json"], [])


def test_a_changelog_keeps_both_entries_in_full_lines_they_share_included(tmp_path):
    """40.3: both entries end with the same bullet, which git moves out of
    the conflict. Both entries in full pass, in either order; one that drops
    the child's copy of the shared bullet, keeps one entry, or changes
    another line, is named. A blank line does not count."""
    bump = "- Bump the plugin and marketplace versions."

    def log(*entries: tuple[str, str]) -> str:
        return "# Changelog\n" + "".join(
            f"\n## [{v}]\n\n- {text}\n{bump}\n" for v, text in entries
        )

    s = Sides(
        tmp_path,
        {"CHANGELOG.md": log(("0.64.0", "First."))},
        {"CHANGELOG.md": log(("0.65.0", "Child."), ("0.64.0", "First."))},
        {"CHANGELOG.md": log(("0.64.1", "Main."), ("0.64.0", "First."))},
    )
    both = log(("0.65.0", "Child."), ("0.64.1", "Main."), ("0.64.0", "First."))
    cases = {
        both: [],
        log(("0.64.1", "Main."), ("0.65.0", "Child."), ("0.64.0", "First.")): [],
        both.replace("\n\n", "\n"): [],
        both.replace(f"- Child.\n{bump}\n", "- Child.\n", 1): ["CHANGELOG.md"],
        log(("0.65.0", "Child."), ("0.64.0", "First.")): ["CHANGELOG.md"],
        both.replace("- Main.", "- Child."): ["CHANGELOG.md"],
        both.replace("- First.", "- Changed."): ["CHANGELOG.md"],
    }
    for text, want in cases.items():
        assert s.beyond({"CHANGELOG.md": text}) == want, text


def test_a_changelog_entry_goes_under_the_version_the_resolution_wrote(tmp_path):
    """40.3 (finding e846ad7621): base 0.64.0, both sides at 0.65.0, so the
    resolution writes 0.66.0. The child's lines must keep every number but
    its old version, which becomes 0.66.0: its heading left at 0.65.0 (a
    second 0.65.0 entry), any other heading, or another number changed in
    its bullets, is named. A CHANGELOG git merged cleanly is checked too
    when the child's lines name the old version."""
    entry = "\n## [{}]\n\n- Child, on Python 3.11.\n"
    old = "\n## [0.63.0]\n\n- Old.\n- One.\n- Two.\n- Three.\n- Four.\n- {}\n"
    base, main = (
        changelog(("0.64.0", "First.")) + old.format("Typo."),
        changelog(("0.64.0", "First.")),
    )
    ours = {
        "plugin.json": plugin("0.65.0"),
        "CHANGELOG.md": base.replace(
            "\n## [0.64.0]", entry.format("0.65.0") + "\n## [0.64.0]", 1
        ),
    }
    theirs = {
        "plugin.json": plugin("0.65.0"),
        "CHANGELOG.md": main.replace(
            "\n## [0.64.0]", "\n## [0.65.0]\n\n- Main.\n\n## [0.64.0]", 1
        )
        + old.format("Typo."),
    }
    s = Sides(
        tmp_path / "conflict",
        {"plugin.json": plugin("0.64.0"), "CHANGELOG.md": base},
        ours,
        theirs,
    )
    assert s.plan() == merge.Plan(["CHANGELOG.md", "plugin.json"], [])
    good = theirs["CHANGELOG.md"].replace(
        "\n## [0.65.0]", entry.format("0.66.0") + "\n## [0.65.0]", 1
    )
    cases = {
        good: [],
        good.replace("[0.66.0]", "[0.65.0]"): ["CHANGELOG.md"],
        good.replace("[0.66.0]", "[9.9.9]"): ["CHANGELOG.md"],
        good.replace("3.11", "3.9"): ["CHANGELOG.md"],
    }
    for text, want in cases.items():
        assert (
            s.beyond({"plugin.json": plugin("0.66.0"), "CHANGELOG.md": text}) == want
        ), text
    theirs = {
        "plugin.json": plugin("0.65.0"),
        "CHANGELOG.md": base.replace("Typo.", "Fixed."),
    }
    s = Sides(
        tmp_path / "clean",
        {"plugin.json": plugin("0.64.0"), "CHANGELOG.md": base},
        ours,
        theirs,
    )
    assert s.plan() == merge.Plan(["plugin.json", "CHANGELOG.md"], [])
    gits = ours["CHANGELOG.md"].replace("Typo.", "Fixed.")
    assert s.beyond({"plugin.json": plugin("0.66.0"), "CHANGELOG.md": gits}) == [
        "CHANGELOG.md"
    ]
    assert (
        s.beyond(
            {
                "plugin.json": plugin("0.66.0"),
                "CHANGELOG.md": gits.replace("[0.65.0]", "[0.66.0]"),
            }
        )
        == []
    )


def test_a_changelog_takes_only_its_own_package_s_new_version(tmp_path):
    """Two packages, each with its version file and CHANGELOG, both with the
    child at 0.65.0: a/ was raised alike on main, so it gets 0.66.0; b/'s
    main is at 0.64.1, so the child's 0.65.0 stands, and so does its b/
    CHANGELOG heading."""

    def log(*entries: str) -> str:
        return changelog(
            *((v, f"{who}.") for v, who in (e.split(":") for e in entries))
        )

    base = {
        f"{p}/{f}": text
        for p in "ab"
        for f, text in (
            ("plugin.json", plugin("0.64.0")),
            ("CHANGELOG.md", log("0.64.0:First")),
        )
    }
    ours = {
        f"{p}/{f}": text
        for p in "ab"
        for f, text in (
            ("plugin.json", plugin("0.65.0")),
            ("CHANGELOG.md", log("0.65.0:Child", "0.64.0:First")),
        )
    }
    theirs = {
        "a/plugin.json": plugin("0.65.0"),
        "a/CHANGELOG.md": log("0.65.0:Main", "0.64.0:First"),
        "b/plugin.json": plugin("0.64.1"),
        "b/CHANGELOG.md": log("0.64.1:Main", "0.64.0:First"),
    }
    s = Sides(tmp_path, base, ours, theirs)
    good = {
        "a/plugin.json": plugin("0.66.0"),
        "a/CHANGELOG.md": log("0.66.0:Child", "0.65.0:Main", "0.64.0:First"),
        "b/plugin.json": plugin("0.65.0"),
        "b/CHANGELOG.md": log("0.65.0:Child", "0.64.1:Main", "0.64.0:First"),
    }
    assert s.beyond(good) == []
    assert s.beyond(
        {**good, "b/CHANGELOG.md": log("0.66.0:Child", "0.64.1:Main", "0.64.0:First")}
    ) == ["b/CHANGELOG.md"]


def test_a_changelog_only_the_child_changed_is_given_when_its_entry_names_a_raised_version(
    tmp_path,
):
    """40.3 (finding 54632ff87f): base 0.64.0, both sides at 0.65.0, and only
    the child changed the CHANGELOG. The resolution writes 0.66.0, so the
    child's entry must move from 0.65.0, main's release, to 0.66.0: the plan
    gives the agent that CHANGELOG, and one left under 0.65.0 is named. A
    child-only CHANGELOG whose added lines name no version the child raised
    is not given."""
    base = {
        "plugin.json": plugin("0.64.0"),
        "CHANGELOG.md": changelog(("0.64.0", "First.")),
    }
    ours = {
        "plugin.json": plugin("0.65.0"),
        "CHANGELOG.md": changelog(("0.65.0", "Child."), ("0.64.0", "First.")),
    }
    theirs = {"plugin.json": plugin("0.65.0"), "main.py": ""}
    s = Sides(tmp_path / "named", base, ours, theirs)
    assert s.plan() == merge.Plan(["plugin.json", "CHANGELOG.md"], [])
    assert s.beyond({"plugin.json": plugin("0.66.0"), "main.py": ""}) == [
        "CHANGELOG.md"
    ]
    moved = changelog(("0.66.0", "Child."), ("0.64.0", "First."))
    assert (
        s.beyond(
            {"plugin.json": plugin("0.66.0"), "CHANGELOG.md": moved, "main.py": ""}
        )
        == []
    )
    other = {
        **ours,
        "CHANGELOG.md": changelog(
            ("0.64.0", "First."), ("0.63.0", "Older, fixed in 0.63.1.")
        ),
    }
    s = Sides(tmp_path / "unnamed", base, other, theirs)
    assert s.plan() == merge.Plan(["plugin.json"], [])


def test_a_child_only_changelog_of_another_package_is_not_given(tmp_path):
    """40.3 (PR #387 review, finding 9149e1b300): in a monorepo, both sides
    raise a/ to 0.65.0, and only the child raises b/ to 0.65.0 with an entry
    in b/CHANGELOG.md. The resolution writes 0.66.0 in a/ only, so b/'s
    entry stays under 0.65.0: the plan does not give the agent b/'s
    CHANGELOG, and a merge that leaves it as the child wrote it passes."""
    base = {
        "a/plugin.json": plugin("0.64.0"),
        "b/plugin.json": plugin("0.64.0"),
        "b/CHANGELOG.md": changelog(("0.64.0", "First.")),
    }
    ours = {
        "a/plugin.json": plugin("0.65.0"),
        "b/plugin.json": plugin("0.65.0"),
        "b/CHANGELOG.md": changelog(("0.65.0", "Child b."), ("0.64.0", "First.")),
    }
    theirs = {"a/plugin.json": plugin("0.65.0"), "main.py": ""}
    s = Sides(tmp_path, base, ours, theirs)
    assert s.plan() == merge.Plan(["a/plugin.json"], [])
    assert s.beyond({"a/plugin.json": plugin("0.66.0"), "main.py": ""}) == []


def _uv(project: str, req: str, digest: str) -> str:
    return (
        f'version = 1\n\n[[package]]\nname = "app"\nversion = "{project}"\nsource = {{ editable = "." }}\n\n'
        f'[[package]]\nname = "req"\nversion = "{req}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        f'sdist = {{ url = "https://x/req-{req}.tar.gz", hash = "sha256:{digest}" }}\n'
    )


def test_a_regenerated_lockfile_passes_and_one_mixing_sides_is_named(tmp_path):
    """40.3, 'lockfiles (regenerate)': both sides bumped the project and the
    one dependency in `uv.lock`. A regenerated lock (main's copy, the project
    at the version `pyproject.toml` got) passes, as does one taking the
    child's dependency whole. One side's dependency version with the other's
    hash, a hash line twice (the lock no longer loads), a line from neither
    side, or a version no version file wrote, is named."""

    def project(v: str) -> str:
        return f'[project]\nname = "app"\nversion = "{v}"\n'

    s = Sides(
        tmp_path,
        {"pyproject.toml": project("0.64.0"), "uv.lock": _uv("0.64.0", "1.0", "aa")},
        {"pyproject.toml": project("0.65.0"), "uv.lock": _uv("0.65.0", "1.1", "bb")},
        {"pyproject.toml": project("0.64.1"), "uv.lock": _uv("0.64.1", "1.2", "cc")},
    )
    assert s.plan() == merge.Plan(["pyproject.toml", "uv.lock"], [])
    good = {"pyproject.toml": project("0.65.0"), "uv.lock": _uv("0.65.0", "1.2", "cc")}
    sdist = {
        v: f'sdist = {{ url = "https://x/req-{v}.tar.gz", hash = "sha256:{h}" }}\n'
        for v, h in (("1.1", "bb"), ("1.2", "cc"))
    }
    cases = {
        "": {},
        " ": {"uv.lock": _uv("0.65.0", "1.1", "bb")},
        "uv.lock": {"uv.lock": good["uv.lock"].replace(sdist["1.2"], sdist["1.1"])},
        " uv.lock": {"uv.lock": good["uv.lock"] + sdist["1.1"]},
        "  uv.lock": {
            "uv.lock": good["uv.lock"].replace("aa", "dd").replace("cc", "dd")
        },
        "   uv.lock": {"uv.lock": _uv("0.9.9", "1.2", "cc")},
    }
    for want, change in cases.items():
        assert s.beyond({**good, **change}) == (
            [want.strip()] if want.strip() else []
        ), want


def test_a_lockfile_version_the_resolution_wrote_passes(tmp_path):
    """40.3: both sides raised the project alike, so the version files and the
    lock name the next version, which neither side holds."""
    s = Sides(
        tmp_path,
        {"plugin.json": plugin("0.64.0"), "uv.lock": _uv("0.64.0", "1.0", "aa")},
        {"plugin.json": plugin("0.65.0"), "uv.lock": _uv("0.65.0", "1.0", "aa")},
        {
            "plugin.json": plugin("0.65.0"),
            "uv.lock": _uv("0.65.0", "1.0", "aa"),
            "main.py": "",
        },
    )
    assert s.plan() == merge.Plan(["plugin.json", "uv.lock"], [])
    assert (
        s.beyond(
            {
                "plugin.json": plugin("0.66.0"),
                "uv.lock": _uv("0.66.0", "1.0", "aa"),
                "main.py": "",
            }
        )
        == []
    )
    assert s.beyond(
        {
            "plugin.json": plugin("0.66.0"),
            "uv.lock": _uv("0.67.0", "1.0", "aa"),
            "main.py": "",
        }
    ) == ["uv.lock"]


def _poetry(content: str, *names: str) -> str:
    return (
        "".join(
            f'[[package]]\nname = "{n}"\nversion = "1.0"\npython-versions = "*"\nfiles = [\n'
            f'    {{file = "{n}-1.0.tar.gz", hash = "sha256:{n * 8}"}},\n]\n\n'
            for n in names
        )
        + f'[metadata]\nlock-version = "2.0"\ncontent-hash = "{content}"\n'
    )


def _npm(*names: str) -> str:
    return (
        json.dumps(
            {
                "name": "app",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app", "dependencies": {n: "^1.0.0" for n in names}},
                    **{
                        f"node_modules/{n}": {
                            "version": "1.0.0",
                            "resolved": f"https://r/{n}-1.0.0.tgz",
                            "integrity": f"sha512-{n * 8}",
                        }
                        for n in names
                    },
                },
            },
            indent=2,
        )
        + "\n"
    )


def _pnpm(*names: str) -> str:
    return yaml.safe_dump(
        {
            "lockfileVersion": "9.0",
            "importers": {
                ".": {
                    "dependencies": {
                        n: {"specifier": "^1.0.0", "version": "1.0.0"} for n in names
                    }
                }
            },
            "packages": {
                f"{n}@1.0.0": {"resolution": {"integrity": f"sha512-{n * 8}"}}
                for n in names
            },
        }
    )


def _yarn(*names: str) -> str:
    return "".join(
        f'\n{n}@^1.0.0:\n  version "1.0.0"\n  resolved "https://r/{n}-1.0.0.tgz"\n'
        f"  integrity sha512-{n * 8}\n"
        for n in names
    )


def test_a_regenerated_lockfile_with_lines_neither_side_wrote_passes(tmp_path):
    """40.3, 'lockfiles (regenerate)' (findings 1480e8dabc, 78f307ec71,
    077f0c6eac): the child adds dependency c, main adds d, and only the lock
    conflicts. Its regeneration holds both, so it writes lines neither side
    has: poetry's new `content-hash`, a JSON comma after the child's last
    entry. A lock read as data passes when every key, every value under its
    key and each pinned package is one side's, and a manifest hash is a hex
    digest. One side's package with the other's hash, a key twice, a package
    neither side has, or a manifest hash that is not one, is named."""
    h = {k: k * 64 for k in "0123"}
    cases = {
        "poetry.lock": (
            _poetry(h["0"], "a"),
            _poetry(h["1"], "a", "c"),
            _poetry(h["2"], "a", "d"),
            _poetry(h["3"], "a", "c", "d"),
            [
                _poetry("not a hash", "a", "c", "d"),
                _poetry(h["3"], "a", "c", "d", "e"),
                _poetry(h["3"], "a", "c", "d").replace(
                    "sha256:" + "c" * 8, "sha256:" + "d" * 8
                ),
            ],
        ),
        "package-lock.json": (
            _npm("a"),
            _npm("a", "c"),
            _npm("a", "d"),
            _npm("a", "c", "d"),
            [
                _npm("a", "c", "d").replace('"sha512-' + "c" * 8, '"sha512-' + "d" * 8),
                _npm("a", "c", "d").replace(
                    '"c": "^1.0.0",', '"c": "^1.0.0",\n"c": "^1.0.0",'
                ),
                _npm("a", "c", "d", "e"),
            ],
        ),
        "pnpm-lock.yaml": (
            _pnpm("a"),
            _pnpm("a", "c"),
            _pnpm("a", "d"),
            _pnpm("a", "c", "d"),
            [
                _pnpm("a", "c", "d").replace("sha512-" + "c" * 8, "sha512-" + "d" * 8),
                _pnpm("a", "c", "d") + "lockfileVersion: '9.0'\n",
            ],
        ),
        "yarn.lock": (
            _yarn("a"),
            _yarn("a", "c"),
            _yarn("a", "d"),
            _yarn("a", "c", "d"),
            [_yarn("a", "c", "d").replace("sha512-" + "c" * 8, "sha512-" + "d" * 8)],
        ),
    }
    for name, (base, ours, theirs, good, bad) in cases.items():
        s = Sides(tmp_path / name, {name: base}, {name: ours}, {name: theirs})
        assert s.plan() == merge.Plan([name], []), name
        assert s.beyond({name: good}) == [], (name, good)
        for text in bad:
            assert s.beyond({name: text}) == [name], (name, text)


def _pipfile(digest: str, *pins: tuple[str, str]) -> str:
    return (
        json.dumps(
            {
                "_meta": {"hash": {"sha256": digest}, "pipfile-spec": 6},
                "default": {
                    n: {"hashes": [f"sha256:{h * 8}"], "version": "==1.0"}
                    for n, h in pins
                },
                "develop": {},
            },
            indent=4,
        )
        + "\n"
    )


def _resolved(digest: str, *pins: tuple[str, str, str]) -> str:
    return (
        json.dumps(
            {
                "originHash": digest,
                "pins": [
                    {
                        "identity": n,
                        "kind": "remoteSourceControl",
                        "location": f"https://g/{n}",
                        "state": {"revision": r * 40, "version": v},
                    }
                    for n, v, r in pins
                ],
                "version": 2,
            },
            indent=2,
        )
        + "\n"
    )


def _composer(digest: str, *pins: tuple[str, str, str]) -> str:
    return (
        json.dumps(
            {
                "content-hash": digest,
                "packages": [
                    {
                        "name": f"v/{n}",
                        "version": v,
                        "dist": {
                            "type": "zip",
                            "url": f"https://c/{n}.zip",
                            "shasum": h * 40,
                        },
                    }
                    for n, v, h in pins
                ],
                "packages-dev": [],
            },
            indent=4,
        )
        + "\n"
    )


def test_a_lockfile_package_is_one_side_s_entry_for_that_same_name(tmp_path):
    """40.3, 'lockfiles (regenerate)' (findings f87b11faac, 9817c93283): the
    child adds c, main adds d. A regenerated lock (each manifest hash a new
    hex digest) passes. One that files d's whole entry under c's name
    (package-lock.json's `node_modules/c`, Pipfile.lock's `default.c`), pairs
    c's version with d's revision (Package.resolved, which pins by
    `revision`) or d's shasum (composer.lock), or holds a manifest hash that
    is not one, is named."""
    h = {k: k * 64 for k in "0123"}
    a, c, d = ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d")
    npm = json.loads(_npm("a", "c", "d"))
    npm["packages"]["node_modules/c"] = npm["packages"]["node_modules/d"]
    resolved = json.loads(
        _resolved(h["3"], ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d"))
    )
    resolved["pins"][1]["state"]["revision"] = "d" * 40
    whole = json.loads(
        _resolved(h["3"], ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d"))
    )
    whole["pins"][1]["state"] = whole["pins"][2]["state"]
    cases = {
        "package-lock.json": (
            _npm("a"),
            _npm("a", "c"),
            _npm("a", "d"),
            _npm("a", "c", "d"),
            [json.dumps(npm, indent=2) + "\n"],
        ),
        "Pipfile.lock": (
            _pipfile(h["0"], ("a", "a")),
            _pipfile(h["1"], ("a", "a"), ("c", "c")),
            _pipfile(h["2"], ("a", "a"), ("d", "d")),
            _pipfile(h["3"], ("a", "a"), ("c", "c"), ("d", "d")),
            [
                _pipfile(h["3"], ("a", "a"), ("c", "d"), ("d", "d")),
                _pipfile("not a hash", ("a", "a"), ("c", "c"), ("d", "d")),
            ],
        ),
        "Package.resolved": (
            _resolved(h["0"], ("a", "1.0.0", "a")),
            _resolved(h["1"], ("a", "1.0.0", "a"), ("c", "1.1.0", "c")),
            _resolved(h["2"], ("a", "1.0.0", "a"), ("d", "1.2.0", "d")),
            _resolved(
                h["3"], ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d")
            ),
            [
                json.dumps(resolved, indent=2) + "\n",
                json.dumps(whole, indent=2) + "\n",
                _resolved(
                    "not a hash",
                    ("a", "1.0.0", "a"),
                    ("c", "1.1.0", "c"),
                    ("d", "1.2.0", "d"),
                ),
            ],
        ),
        "composer.lock": (
            _composer(h["0"], a),
            _composer(h["1"], a, c),
            _composer(h["2"], a, d),
            _composer(h["3"], a, c, d),
            [
                _composer(h["3"], a, ("c", "1.1.0", "d"), d),
                _composer("not a hash", a, c, d),
            ],
        ),
    }
    for name, (base, ours, theirs, good, bad) in cases.items():
        s = Sides(tmp_path / name, {name: base}, {name: ours}, {name: theirs})
        assert s.plan() == merge.Plan([name], []), name
        assert s.beyond({name: good}) == [], (name, good)
        for text in bad:
            assert s.beyond({name: text}) == [name], (name, text)


def _uv_git(*pins: tuple[str, str, str]) -> str:
    return "version = 1\n" + "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n'
        f'source = {{ git = "https://g/{n}?rev=main#{r * 40}" }}\n'
        for n, v, r in pins
    )


def _cargo_git(*pins: tuple[str, str, str]) -> str:
    return "version = 3\n" + "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n'
        f'source = "git+https://g/{n}?branch=main#{r * 40}"\n'
        for n, v, r in pins
    )


def test_a_lockfile_package_pinned_by_a_git_revision_is_one_side_s_whole(tmp_path):
    """40.3 (finding f87b11faac, the same check for packages with no hash):
    uv.lock and Cargo.lock pin a git dependency by the revision in its
    `source`, and Package.resolved pins a branch with no version. A
    regenerated lock passes; c's version with d's source is named."""
    a, c, d = ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d")
    mixed = ("c", "1.1.0", "d")
    branch = json.loads(_resolved("0" * 64, a))
    branch["pins"][0]["state"] = {"branch": "main", "revision": "a" * 40}
    branch = json.dumps(branch, indent=2) + "\n"
    cases = {
        "uv.lock": (
            _uv_git(a),
            _uv_git(a, c),
            _uv_git(a, d),
            _uv_git(a, c, d),
            [_uv_git(a, mixed, d).replace("https://g/c?", "https://g/d?")],
        ),
        "Cargo.lock": (
            _cargo_git(a),
            _cargo_git(a, c),
            _cargo_git(a, d),
            _cargo_git(a, c, d),
            [_cargo_git(a, mixed, d).replace("https://g/c?", "https://g/d?")],
        ),
    }
    for name, (base, ours, theirs, good, bad) in cases.items():
        s = Sides(tmp_path / name, {name: base}, {name: ours}, {name: theirs})
        assert s.plan() == merge.Plan([name], []), name
        assert s.beyond({name: good}) == [], (name, good)
        for text in bad:
            assert s.beyond({name: text}) == [name], (name, text)
    pins = [
        json.loads(branch)["pins"][0],
        *json.loads(_resolved("0" * 64, c, d))["pins"],
    ]
    s = Sides(
        tmp_path / "branch",
        {"Package.resolved": branch},
        {
            "Package.resolved": json.dumps({"pins": pins[:2], "version": 2}, indent=2)
            + "\n"
        },
        {
            "Package.resolved": json.dumps(
                {"pins": [pins[0], pins[2]], "version": 2}, indent=2
            )
            + "\n"
        },
    )
    assert s.plan() == merge.Plan(["Package.resolved"], [])
    assert (
        s.beyond(
            {
                "Package.resolved": json.dumps({"pins": pins, "version": 2}, indent=2)
                + "\n"
            }
        )
        == []
    )


def _poetry_git(*pins: tuple[str, str, str]) -> str:
    return (
        "".join(
            f'[[package]]\nname = "{n}"\nversion = "{v}"\npython-versions = "*"\nfiles = []\n\n'
            f'[package.source]\ntype = "git"\nurl = "https://g/{n}"\nreference = "main"\n'
            f'resolved_reference = "{r * 40}"\n\n'
            for n, v, r in pins
        )
        + '[metadata]\nlock-version = "2.0"\ncontent-hash = "'
        + "0" * 64
        + '"\n'
    )


def _resolved_v1(*pins: tuple[str, str, str]) -> str:
    return (
        json.dumps(
            {
                "object": {
                    "pins": [
                        {
                            "package": n,
                            "repositoryURL": f"https://g/{n}",
                            "state": {"branch": None, "revision": r * 40, "version": v},
                        }
                        for n, v, r in pins
                    ]
                },
                "version": 1,
            },
            indent=2,
        )
        + "\n"
    )


def test_a_poetry_git_package_and_a_version_1_package_resolved_pin_are_one_side_s_whole(
    tmp_path,
):
    """40.3 (owed review of 5c8a5602..8165ed8a, findings cf3a1d5b0a and
    95e5f9702a): a poetry.lock git package has no file hashes and is pinned
    only by `[package.source] resolved_reference`; a version-1
    Package.resolved nests its pins under `object`. The child adds c, main
    adds d. A regenerated lock passes; c's version with d's source, or pin c
    holding d's whole `state`, is named."""
    a, c, d = ("a", "1.0.0", "a"), ("c", "1.1.0", "c"), ("d", "1.2.0", "d")
    v1 = json.loads(_resolved_v1(a, c, d))
    v1["object"]["pins"][1]["state"] = v1["object"]["pins"][2]["state"]
    cases = {
        "poetry.lock": (
            _poetry_git(a),
            _poetry_git(a, c),
            _poetry_git(a, d),
            _poetry_git(a, c, d),
            _poetry_git(a, ("c", "1.1.0", "d"), d).replace(
                "https://g/c", "https://g/d"
            ),
        ),
        "Package.resolved": (
            _resolved_v1(a),
            _resolved_v1(a, c),
            _resolved_v1(a, d),
            _resolved_v1(a, c, d),
            json.dumps(v1, indent=2) + "\n",
        ),
    }
    for name, (base, ours, theirs, good, bad) in cases.items():
        s = Sides(tmp_path / name, {name: base}, {name: ours}, {name: theirs})
        assert s.plan() == merge.Plan([name], []), name
        assert s.beyond({name: good}) == [], name
        assert s.beyond({name: bad}) == [name], name


def _uv_requested(project: str, *deps: str) -> str:
    """uv.lock with the project's editable entry asking for a git dependency in
    `[package.metadata] requires-dist`, as this repository's own uv.lock does,
    and each of *deps* a registry package with a hash."""
    names = ["core", *deps]
    return (
        'version = 1\n\n[[package]]\nname = "app"\nversion = "'
        + project
        + '"\nsource = { editable = "." }\n'
        "dependencies = [\n"
        + "".join(f'    {{ name = "{n}" }},\n' for n in names)
        + "]\n\n"
        '[package.metadata]\nrequires-dist = [\n    { name = "core", git = "https://g/core?branch=main" },\n'
        + "".join(f'    {{ name = "{n}" }},\n' for n in deps)
        + "]\n"
        + "".join(
            f'\n[[package]]\nname = "{n}"\nversion = "1.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
            f'sdist = {{ url = "https://x/{n}-1.0.tar.gz", hash = "sha256:{n * 8}" }}\n'
            for n in deps
        )
    )


def test_a_git_dependency_a_project_asks_for_does_not_pin_the_project(tmp_path):
    """40.3 (owed review of 5c8a5602..8165ed8a, finding 99f5110ddf): a `git`
    key in uv.lock's `[package.metadata] requires-dist` is what the project
    asks for, not a lock. Both sides raise the project 0.64.0 to 0.65.0, the
    child adds dependency c and main adds d; the resolution writes 0.66.0. A
    regenerated uv.lock, the editable entry at 0.66.0 with both dependencies,
    passes."""

    def project(v: str) -> str:
        return f'[project]\nname = "app"\nversion = "{v}"\n'

    s = Sides(
        tmp_path,
        {"pyproject.toml": project("0.64.0"), "uv.lock": _uv_requested("0.64.0")},
        {"pyproject.toml": project("0.65.0"), "uv.lock": _uv_requested("0.65.0", "c")},
        {"pyproject.toml": project("0.65.0"), "uv.lock": _uv_requested("0.65.0", "d")},
    )
    assert s.plan() == merge.Plan(["uv.lock", "pyproject.toml"], [])
    assert (
        s.beyond(
            {
                "pyproject.toml": project("0.66.0"),
                "uv.lock": _uv_requested("0.66.0", "c", "d"),
            }
        )
        == []
    )


def devcontainer(node: str) -> str:
    return (
        json.dumps(
            {
                "name": "dev",
                "features": {
                    "ghcr.io/devcontainers/features/node:1": {"version": node}
                },
            },
            indent=2,
        )
        + "\n"
    )


def test_a_third_party_version_nested_in_json_is_not_the_package_s_own(tmp_path):
    """40.3 (finding e90cf86b28): a devcontainer feature's `version` is not
    the package's own. Both sides raising it alike merges cleanly at that
    value with no agent and no next version; raising it differently is a
    conflict the step refuses, never one it forces to a third value. A
    marketplace's `plugins[].version` and `metadata.version` stay its own."""
    name = ".devcontainer/devcontainer.json"
    s = Sides(
        tmp_path / "alike",
        {name: devcontainer("20")},
        {name: devcontainer("22")},
        {name: devcontainer("22"), "main.py": ""},
    )
    assert s.plan() == merge.Plan([], [])
    assert s.beyond({name: devcontainer("22"), "main.py": ""}) == []
    assert s.beyond({name: devcontainer("23"), "main.py": ""}) == [name]
    s = Sides(
        tmp_path / "differ",
        {name: devcontainer("20")},
        {name: devcontainer("22")},
        {name: devcontainer("21")},
    )
    assert s.plan() == merge.Plan([], [name])

    def meta(v: str) -> str:
        return (
            json.dumps(
                {"name": "m", "metadata": {"version": v}, "plugins": []}, indent=2
            )
            + "\n"
        )

    s = Sides(
        tmp_path / "meta",
        {"m.json": meta("1.0.0")},
        {"m.json": meta("1.1.0")},
        {"m.json": meta("1.0.1")},
    )
    assert s.plan() == merge.Plan(["m.json"], [])
    assert s.beyond({"m.json": meta("1.1.0")}) == []
    # YAML: a compose file's `version` is its format's; a Helm chart's is its own.
    compose, chart = (
        'version: "{}"\nservices:\n  app:\n    image: x\n',
        "name: c\nversion: {}\n",
    )
    s = Sides(
        tmp_path / "yaml",
        {
            "docker-compose.yml": compose.format("3.7"),
            "Chart.yaml": chart.format("0.1.0"),
        },
        {
            "docker-compose.yml": compose.format("3.8"),
            "Chart.yaml": chart.format("0.2.0"),
        },
        {
            "docker-compose.yml": compose.format("3.8"),
            "Chart.yaml": chart.format("0.1.1"),
        },
    )
    assert s.plan() == merge.Plan(["Chart.yaml"], [])
    assert (
        s.beyond(
            {
                "docker-compose.yml": compose.format("3.8"),
                "Chart.yaml": chart.format("0.2.0"),
            }
        )
        == []
    )


def test_the_prompt_asks_for_the_new_version_only_where_the_check_accepts_it(tmp_path):
    """40.3 (finding f6f5c21cbe): the check accepts the new version only in
    the version fields both sides raised (and the child's CHANGELOG lines),
    so the prompt asks for that and nothing wider. Another plugin at the old
    value, left as it is, passes; written over, it is named."""
    from build_pipeline.prompts.epic_merge import MERGE_PROMPT

    text = " ".join(MERGE_PROMPT.split())
    assert "wherever that version appears" not in text
    assert "only in the version fields both" in text
    s = Sides(
        tmp_path,
        {"m.json": market("0.64.0", "0.65.0")},
        {"m.json": market("0.65.0", "0.65.0")},
        {"m.json": market("0.65.0", "0.65.0"), "main.py": ""},
    )
    assert s.plan() == merge.Plan(["m.json"], [])
    assert s.beyond({"m.json": market("0.66.0", "0.65.0"), "main.py": ""}) == []
    assert s.beyond({"m.json": market("0.66.0", "0.66.0"), "main.py": ""}) == ["m.json"]


def test_the_prompt_leaves_a_version_both_sides_raised_that_is_not_the_package_s_own(
    tmp_path,
):
    """40.3 (owed review of 5c8a5602..8165ed8a, finding 4991ad04ec; PR #387
    review, finding 320e2b1539): both sides raise Cargo's `[package]` and a
    `[dependencies.<name>]` version alike. The check accepts the new version
    only in the package's own field, so the prompt limits the rule to it and
    names a dependency's version as one to leave as the merge left it.
    `[tool.commitizen]`'s `version` is commitizen's copy of the project's
    version, so it is the package's own and is raised with `[project]`'s."""
    from build_pipeline.prompts.epic_merge import MERGE_PROMPT

    text = " ".join(MERGE_PROMPT.split())
    assert "The package's own version field" in text
    assert "`[dependencies.<name>]`" in text

    def cargo(own: str, dep: str) -> str:
        return f'[package]\nname = "app"\nversion = "{own}"\n\n[dependencies.serde]\nversion = "{dep}"\n'

    s = Sides(
        tmp_path / "cargo",
        {"Cargo.toml": cargo("0.64.0", "1.0.0")},
        {"Cargo.toml": cargo("0.65.0", "1.1.0")},
        {"Cargo.toml": cargo("0.65.0", "1.1.0"), "main.py": ""},
    )
    assert s.plan() == merge.Plan(["Cargo.toml"], [])
    assert s.beyond({"Cargo.toml": cargo("0.66.0", "1.1.0"), "main.py": ""}) == []
    assert s.beyond({"Cargo.toml": cargo("0.66.0", "1.2.0"), "main.py": ""}) == [
        "Cargo.toml"
    ]

    def pyproject(own: str, tool: str) -> str:
        return f'[project]\nname = "app"\nversion = "{own}"\n\n[tool.commitizen]\nversion = "{tool}"\n'

    s = Sides(
        tmp_path / "cz",
        {"pyproject.toml": pyproject("0.64.0", "0.64.0")},
        {"pyproject.toml": pyproject("0.65.0", "0.65.0")},
        {"pyproject.toml": pyproject("0.65.0", "0.65.0"), "main.py": ""},
    )
    assert s.plan() == merge.Plan(["pyproject.toml"], [])
    assert (
        s.beyond({"pyproject.toml": pyproject("0.66.0", "0.66.0"), "main.py": ""}) == []
    )
    assert s.beyond(
        {"pyproject.toml": pyproject("0.66.0", "0.65.0"), "main.py": ""}
    ) == ["pyproject.toml"]


def test_a_conflict_without_markers_is_never_the_step_s_resolution(tmp_path):
    """A modify/delete conflict (a lockfile and a CHANGELOG included) or a
    binary one, resolved by keeping one side, leaves no marker and matches
    git's own tree, yet it is named, so a merge holding it is reviewed."""
    s = Sides(
        tmp_path,
        {
            "app.py": "x = 1\n",
            "uv.lock": "a\n",
            "CHANGELOG.md": "- a\n",
            "logo.png": b"\x89PNG\x00a",
        },
        {
            "app.py": "x = 2\n",
            "uv.lock": "b\n",
            "CHANGELOG.md": None,
            "logo.png": b"\x89PNG\x00b",
        },
        {
            "app.py": None,
            "uv.lock": None,
            "CHANGELOG.md": "- c\n",
            "logo.png": b"\x89PNG\x00c",
        },
    )
    assert s.plan() == merge.Plan([], ["CHANGELOG.md", "app.py", "logo.png", "uv.lock"])
    assert s.beyond({}) == ["CHANGELOG.md", "app.py", "logo.png", "uv.lock"]
    git(s.w, "update-ref", "refs/remotes/origin/x", s.theirs)
    git(s.w, "checkout", "-q", "--detach", s.ours)
    git(
        s.w,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "merge",
        "-q",
        "--no-ff",
        "-s",
        "ours",
        "-m",
        "m",
        s.theirs,
    )
    assert not merge.merges_onto(
        s.w, git(s.w, "rev-parse", "HEAD").strip(), s.ours, "x"
    )


def _start_merge(s: Sides) -> None:
    """Merge *theirs* into <W> without committing. It conflicts, so git
    exits 1; the merge must still have started."""
    done = subprocess.run(
        [real_git(), "merge", "-q", "--no-ff", "--no-commit", s.theirs],
        cwd=s.w,
        capture_output=True,
        text=True,
    )
    assert (s.w / ".git" / "MERGE_HEAD").exists(), done.stderr


def test_a_pushed_merge_s_content_is_checked_once_per_commit(
    tmp_path, monkeypatch, git_identity
):
    """The coordinator asks `merges_onto` about the same PR head on every
    pass (finding 16be8d14c9): the content check runs once per merge commit,
    named by its sha; a ref such as HEAD is checked each time."""
    s = Sides(tmp_path, BASE, CHILD, MAIN)
    git(s.w, "update-ref", "refs/remotes/origin/x", s.theirs)
    git(s.w, "checkout", "-q", "--detach", s.ours)
    _start_merge(s)
    for rel, text in RESOLVED.items():
        (s.w / rel).write_text(text)
    git(s.w, "add", "--", *RESOLVED)
    git(s.w, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-edit")
    head, calls = git(s.w, "rev-parse", "HEAD").strip(), []
    checked = merge.not_resolutions
    monkeypatch.setattr(
        merge, "not_resolutions", lambda *a: calls.append(a) or checked(*a)
    )
    assert (
        all(merge.merges_onto(s.w, head, s.ours, "x") for _ in range(3))
        and len(calls) == 1
    )
    assert merge._only_resolutions(s.w, "HEAD", s.ours) and merge._only_resolutions(
        s.w, "HEAD", s.ours
    )
    assert len(calls) == 3


def test_a_merge_in_progress_is_checked_the_same_in_any_conflict_style(
    tmp_path, git_identity
):
    """diff3 and zdiff3 markers carry a base side: a marker is found while it
    is left, and the resolution is checked against the three sides
    themselves, so the style changes nothing."""
    for style in ("merge", "diff3", "zdiff3"):
        s = Sides(tmp_path / style, BASE, CHILD, MAIN, f"merge.conflictStyle={style}")
        git(s.w, "checkout", "-q", "--detach", s.ours)
        _start_merge(s)
        assert merge.plan(s.w, "HEAD", "MERGE_HEAD") == merge.Plan(
            ["CHANGELOG.md", "plugin.json"], []
        ), style
        assert merge.unresolved(s.w, ["CHANGELOG.md", "plugin.json"]) == [
            "CHANGELOG.md",
            "plugin.json",
        ], style
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
        return "version = 1\n" + "".join(
            f'\n[[package]]\nname = "p{i}"\nversion = "1.{tag}"\n' for i in range(3000)
        )

    history = "".join(
        f"\n## [0.{i}.0]\n\n### Added\n- Thing {i}.\n- Other {i}.\n"
        for i in range(1000, 0, -1)
    )

    def entry(v: str, who: str) -> str:
        return f"\n## [{v}]\n\n### Added\n" + "".join(
            f"- {who} {k}.\n" for k in range(30)
        )

    s = Sides(
        tmp_path,
        {"uv.lock": lock("0"), "CHANGELOG.md": "# Changelog\n" + history},
        {
            "uv.lock": lock("1"),
            "CHANGELOG.md": "# Changelog\n" + entry("1.1.0", "Child") + history,
        },
        {
            "uv.lock": lock("2"),
            "CHANGELOG.md": "# Changelog\n" + entry("1.0.1", "Main") + history,
        },
    )
    both = "# Changelog\n" + entry("1.1.0", "Child") + entry("1.0.1", "Main") + history
    started = time.monotonic()
    assert s.beyond({"uv.lock": lock("2"), "CHANGELOG.md": both}) == []
    assert time.monotonic() - started < 20


def test_the_merge_tests_fall_back_to_discovery(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert merge.test_command(tmp_path) == discover_test_command(tmp_path)[0]


def _xdist_venv(p: Path, *, xdist: bool) -> None:
    bin_dir = p / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pytest").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "python").write_text(f"#!/bin/sh\nexit {0 if xdist else 1}\n")
    for name in ("pytest", "python"):
        (bin_dir / name).chmod(0o755)


@pytest.mark.parametrize(
    "tdd, xdist, expected",
    [
        (
            "tdd:\n  test_command: .venv/bin/pytest -q\n",
            True,
            ".venv/bin/pytest -q -n auto",
        ),
        ("tdd:\n  test_command: .venv/bin/pytest -q\n", False, ".venv/bin/pytest -q"),
        (
            "tdd:\n  test_command: .venv/bin/pytest -q\n  parallel: off\n",
            True,
            ".venv/bin/pytest -q",
        ),
        ("tdd:\n  test_command: fake-tests\n", True, "fake-tests"),
        ("tdd: {}\n", True, None),
    ],
)
def test_the_merge_tests_run_pytest_in_parallel_when_xdist_imports(
    tmp_path, tdd, xdist, expected
):
    """Item 4: 40.4 runs the full suite with `-n auto` as the gates do."""
    _xdist_venv(tmp_path, xdist=xdist)
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text(tdd)
    assert merge.suite_command(tmp_path) == expected
    assert merge.test_command(tmp_path) == (
        expected.removesuffix(" -n auto") if expected else None
    )


def test_the_merge_step_runs_the_suite_command(tmp_path, monkeypatch):
    r = MergeRoot(tmp_path, monkeypatch)
    monkeypatch.setattr(merge, "suite_command", lambda p: "fake-tests -n auto")
    assert r.run() == 0 and r.merged()
    [tests] = r.tries("merge-tests")
    assert tests.started["argv"] == ["fake-tests", "-n", "auto"]


def test_a_heading_underline_left_in_a_resolved_changelog_is_no_conflict(tmp_path):
    (tmp_path / "CHANGELOG.rst").write_text(
        "Changes\n=======\n\n0.2.0\n-----\n\n- Child.\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "<<<<<<< HEAD\n- a\n=======\n- b\n>>>>>>> x\n"
    )
    assert merge.unresolved(tmp_path, ["CHANGELOG.rst", "CHANGELOG.md"]) == [
        "CHANGELOG.md"
    ]


# --- 40.1 after a real fix round (item 42) ------------------------------------------


@pytest.mark.asyncio
async def test_a_child_that_had_a_fix_round_passes_40_1_and_an_extra_file_parks_it(
    tmp_path, monkeypatch
):
    """One round of D1's real `findings` loop on the PR's worktree (fakes only
    at its seams) leaves only bookkeeping, so 40.1 passes; an untracked file
    anyone else leaves makes it refuse, naming the file."""
    from build_pipeline.findings import loop

    from tests.test_findings_round import (
        FIX,
        FakePR,
        FakeReview,
        FakeTriage,
        _make_built,
        _run,
        _Stubs,
    )
    from tests.test_findings_self_check import (
        FakeCheck,
        _findings_json,
        _with_bare_origin,
        block_stubs,
    )

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
    run, lines = _run(
        config,
        change,
        triage_call=FakeTriage({"add subtracts": FIX}),
        review=review,
        self_check=FakeCheck([""]),
        decisions=None,
    )
    with _Stubs(block_stubs(repo)):
        assert await loop.run_findings(run) == 0, lines
    head = git(repo, "rev-parse", "HEAD").strip()
    assert merge.ready(repo, head, "refs/remotes/origin/buildme/calc", "main") is None
    (repo / "notes.txt").write_text("mine\n")
    assert "notes.txt" in merge.ready(
        repo, head, "refs/remotes/origin/buildme/calc", "main"
    )
    assert os.path.exists(repo / "notes.txt")


def test_a_fixer_that_cannot_start_ends_the_run_and_the_next_run_relaunches_it(
    tmp_path, monkeypatch
):
    """PR D2 as it was: only the merge tests read a spawn failure as a failed
    run (above). A fixer that cannot start (EAGAIN) raises, ending the run
    and parking nothing, and the next run launches it."""
    from .test_epic_fix import Epic, N

    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"rounds": [{}]}]})
    real = coordinator._spawn_review

    def spawn(argv, *a, **k):
        if any("findings" in x for x in argv):
            raise OSError(11, "Resource temporarily unavailable")
        return real(argv, *a, **k)

    monkeypatch.setattr(coordinator, "_spawn_review", spawn)
    assert (
        e.run() == 1 and e.status()["status"] != "parked"
    )  # the orchestrator reports the error
    monkeypatch.setattr(coordinator, "_spawn_review", real)
    assert e.run() == 3 and e.status()["status"] == "built"
    assert e.fixes()[-1].result["exit_code"] == 0


# --- 40.5 and 40.6: the required checks, then `gh pr merge` (PR E2) ------------------

TWO = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b")]
)


class SecondMerger(FakeAgent):
    """The merge agent for two children that both raised 0.1.0 to 0.2.0: the
    one merged second takes 0.3.0, its entry above the other's."""

    def __init__(self, root: Path):
        super().__init__(root, {})

    async def __call__(self, config, w, files, name, default):
        me = name.rsplit("-", 1)[1]
        other = "b" if me == "a" else "a"
        self.resolve = {
            "plugin.json": plugin("0.3.0"),
            "CHANGELOG.md": changelog(
                ("0.3.0", f"Child {me}."),
                ("0.2.0", f"Child {other}."),
                ("0.1.0", "First."),
            ),
        }
        return await super().__call__(config, w, files, name, default)


def _merged_prs(r: MergeRoot) -> list[str]:
    """The PRs `gh pr merge` was called on, in order: `e-a`, `e-b`."""
    return [x["argv"][2].rsplit("/", 1)[1] for x in r.fakes.merges()]


def test_two_children_merge_in_turn_resolving_their_version_conflict(
    tmp_path, monkeypatch
):
    """Done means 7: two independent children in one repo both raise the
    version to 0.2.0. Their merge steps take the repo's lock in turn: the
    first is up to date and merges; the second then merges main, one agent
    call resolves the version and CHANGELOG, and it is tested, pushed and
    merged with `gh pr merge --merge`."""
    b_files = {
        "plugin.json": plugin("0.2.0"),
        "CHANGELOG.md": changelog(("0.2.0", "Child b."), ("0.1.0", "First.")),
    }
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        main=None,
        answer=TWO,
        b_files=b_files,
        agent=SecondMerger(tmp_path),
    )
    order: list[str] = []

    def point(coord, c, name):
        order.append(f"{name} {c.id}")

    r.use()
    monkeypatch.setattr(coordinator, "_merge_point", point)
    assert run_coordinator(r.config) == 0 and merge_waiting() is None
    first = order[0].split()[1]
    second = "b" if first == "a" else "a"
    assert order.index(f"checks {first}") < order.index(
        f"start {second}"
    )  # one merge at a time
    assert _merged_prs(r) == [f"e-{first}", f"e-{second}"]
    assert all(
        x["argv"][3:5] == ["--merge", "--match-head-commit"] and len(x["argv"]) == 6
        for x in r.fakes.merges()
    )
    assert [x["files"] for x in r.calls("merge-agent")] == [
        ["CHANGELOG.md", "plugin.json"]
    ]
    assert {r.status(cid)["status"] for cid in "ab"} == {"merged"}
    assert r.origin("plugin.json", "ENG-40-e") == plugin("0.3.0")
    assert r.origin("CHANGELOG.md", "ENG-40-e") == changelog(
        ("0.3.0", f"Child {second}."), ("0.2.0", f"Child {first}."), ("0.1.0", "First.")
    )
    assert len(r.calls("merge-tests")) == 1 and len(r.step_pushes()) == 1


def _check_reads(r: MergeRoot) -> int:
    """a's `gh pr checks --required` calls."""
    return len(list((r.root / "checks-attempts").glob(f"{pr_number('e-a')}.*")))


def test_pending_required_checks_are_waited_for(tmp_path, monkeypatch):
    """40.5: a pending required check is read again until it passes; then the PR merges."""
    r = MergeRoot(tmp_path, monkeypatch, pr={"checks": [PENDING, PENDING, PASSED]})
    assert r.run() == 0 and r.status()["status"] == "merged"
    assert _check_reads(r) == 3
    assert _merged_prs(r) == ["e-a", "e-b"]


def test_checks_are_read_only_once_github_shows_the_pushed_head(tmp_path, monkeypatch):
    """40.5: for about 1.5 s after a push, GitHub still shows the PR's
    previous head (measured on spikelab/github-tests), and `gh pr checks`
    reads that head's checks, which passed before the review. Until `gh pr
    view` shows the head the step pushed, the checks count as pending; then
    the new head's pending check is waited for, and the PR merges once it
    passes."""
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        pr={"lag": 2, "old_checks": PASSED, "checks": [PENDING, PASSED]},
    )
    assert r.run() == 0 and r.status()["status"] == "merged"
    assert _check_reads(r) == 2 and len(r.step_pushes()) == 1
    [call] = [x for x in r.fakes.merges() if x["argv"][2] == pr_url("e-a")]
    assert call["argv"][-1] == git(r.w, "rev-parse", "HEAD").strip()


def test_a_pr_with_no_checks_merges_once_none_appeared_after_the_push(
    tmp_path, monkeypatch
):
    """40.5: gh 2.102 exits 1 with "no checks reported" for a repo with no CI
    (read on spikelab/github-tests), and also for a head GitHub has not yet
    registered checks for. After the step's own push that counts as pending
    for `CHECKS_APPEAR` seconds; then the PR merges."""
    r = MergeRoot(tmp_path, monkeypatch)
    assert r.run() == 0 and r.status()["status"] == "merged"
    assert (
        _check_reads(r) == int(coordinator.CHECKS_APPEAR / coordinator.CHECKS_WAIT) + 1
    )
    assert _merged_prs(r) == ["e-a", "e-b"]


def test_a_branch_up_to_date_with_no_checks_merges_at_once(tmp_path, monkeypatch):
    """40.5: with nothing pushed, the PR's head is the reviewed one, whose
    checks GitHub registered long ago: "no checks reported" means none."""
    r = MergeRoot(tmp_path, monkeypatch, main=None)
    assert r.run() == 0 and r.merged() and _check_reads(r) == 1


@pytest.mark.parametrize("first", [[], "late"], ids=["none-yet", "required-not-yet"])
def test_checks_that_appear_after_the_push_are_waited_for(tmp_path, monkeypatch, first):
    """40.5: the first read after the step's push finds no checks, or only a
    check that is not required, because the required one is not registered
    yet. The next read finds the required check failed: the child parks,
    naming it, and nothing is merged."""
    early = (
        [] if first == [] else [{"name": "lint", "bucket": "pass", "required": False}]
    )
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        pr={"checks": [early, [{"name": "ci", "bucket": "fail"}]]},
    )
    assert r.run() == 0
    assert r.status()["reason"] == "required check ci failed" and r.fakes.merges() == []


@pytest.mark.parametrize("bucket", ["pass", "fail"])
def test_with_no_required_check_the_pr_s_own_checks_gate(tmp_path, monkeypatch, bucket):
    """40.5: gh says "no required checks reported" in a repo with CI and no
    branch rule (gh 2.102, read on #348 in M). The PR's own checks then gate,
    as 40.4's "required checks still gate" means: a pending one is waited
    for, and a failed one parks."""
    ci = [{"name": "ci", "bucket": "pending", "required": False}]
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        main=None,
        pr={"checks": [ci, [{"name": "ci", "bucket": bucket, "required": False}]]},
    )
    assert r.run() == 0
    if bucket == "pass":
        assert r.merged() and _merged_prs(r) == ["e-a", "e-b"]
    else:
        assert r.status()["reason"] == "check ci failed" and r.fakes.merges() == []


@pytest.mark.parametrize("bucket", ["fail", "cancel"])
def test_a_failed_check_parks_naming_it_and_merges_nothing(
    tmp_path, monkeypatch, bucket
):
    """40.5: a failed or cancelled required check parks, naming it. The PR
    stays open and `<W>` clean, so a re-run reads the checks again (E.S26)
    and parks again."""
    checks = [{"name": "lint", "bucket": "pass"}, {"name": "ci", "bucket": bucket}]
    r = MergeRoot(tmp_path, monkeypatch, pr={"checks": [checks]})
    assert r.run() == 0
    st = r.status()
    assert (
        st["status"] == "parked"
        and st["reason"] == st["merge_park"] == "required check ci failed"
    )
    assert r.fakes.merges() == [] and len(r.step_pushes()) == 1
    assert (
        r.run() == 0
        and r.status()["reason"] == "required check ci failed"
        and r.fakes.merges() == []
    )
    assert _check_reads(r) == 2


def test_checks_pending_past_the_limit_leave_the_pr_open_and_a_re_run_merges_it(
    tmp_path, monkeypatch
):
    """E.S8: a required check still pending after `CHECKS_LIMIT` seconds
    ends the wait, so the repo's merge lock is let go: the PR is left open
    (`built`), b waits on it, and the run exits 3 with `EPIC:MERGE_WAITING`.
    The re-run finds the branch up to date, reads the checks again and
    merges; nothing is tested, pushed or reviewed again."""
    r = MergeRoot(tmp_path, monkeypatch, pr={"checks": [PENDING]})
    r.pending()
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert (
        r.status()["status"] == "built"
        and r.status("b")["status"] == "waiting"
        and r.fakes.merges() == []
    )
    assert _check_reads(r) == 4
    r.set_checks([PASSED])
    assert r.run() == 0 and r.merged() and _merged_prs(r) == ["e-a", "e-b"]
    assert (
        len(r.tries("review")) == 1
        and len(r.tries("merge-tests")) == 1
        and len(r.step_pushes()) == 1
    )


def test_a_check_read_that_fails_leaves_the_child_unknown(tmp_path, monkeypatch):
    """40.5: a `gh pr checks` answer that is neither JSON nor gh's "no checks
    reported" is a failed read, as a failed fetch is: `unknown`, nothing
    merged, exit 1."""
    r = MergeRoot(
        tmp_path, monkeypatch, main=None, pr={"checks": ["HTTP 502: Bad Gateway"]}
    )
    assert r.run() == 1
    st = r.status()
    assert (
        st["status"] == "unknown"
        and st["reason"]
        == f"{coordinator.MERGE_UNKNOWN}gh pr checks failed: HTTP 502: Bad Gateway"
    )
    assert r.fakes.merges() == []


def test_a_head_read_that_fails_leaves_the_child_unknown(tmp_path, monkeypatch):
    """40.5: `gh pr view --json headRefOid` failing is a failed read, as a
    failed `gh pr checks` is: `unknown`, no checks read, nothing merged,
    exit 1."""
    r = MergeRoot(tmp_path, monkeypatch, main=None)
    gh = coordinator._gh
    monkeypatch.setattr(
        coordinator,
        "_gh",
        lambda repo, *a: None if a[-1] == "headRefOid" else gh(repo, *a),
    )
    assert r.run() == 1
    st = r.status()
    assert (
        st["status"] == "unknown"
        and st["reason"] == f"{coordinator.MERGE_UNKNOWN}gh pr view failed"
    )
    assert _check_reads(r) == 0 and r.fakes.merges() == []


@pytest.mark.parametrize("when", ["during-the-checks", "after-the-checks"])
@pytest.mark.parametrize("main", [None, MAIN], ids=["up-to-date", "pushed"])
def test_a_commit_pushed_to_the_pr_during_the_checks_is_never_merged(
    tmp_path, monkeypatch, main, when
):
    """40.5 and 40.6: a commit pushed to the PR's branch never reaches main
    unreviewed. Pushed while 40.5 waits, the PR's head it reads is not the
    one 40.1 checked: after `CHECKS_APPEAR` the child parks, and no `gh pr
    merge` runs. Pushed after the checks passed, `gh pr merge` names the
    checked head (`--match-head-commit`), GitHub refuses it, and the child
    parks. E.S30: a re-run reviews the foreign head, and 40.1 parks because
    `<W>`'s HEAD is not that reviewed head; nothing is merged, tested or
    pushed again."""
    r = MergeRoot(tmp_path, monkeypatch, main=main)
    r.use()
    foreign = r.root / "foreign"

    def push_foreign(c) -> None:
        if c.id == "a" and not foreign.exists():
            foreign.write_text(
                push_files(
                    r.root,
                    {"evil.txt": "not reviewed\n"},
                    "foreign",
                    branch="buildme/e-a",
                )
            )

    def point(coord, c, name):
        r.point(coord, c, name)
        if name == "checks" and when == "during-the-checks":
            push_foreign(c)

    merge_pr = coordinator.Coordinator._merge_pr

    def merging(coord, c, head):
        if when == "after-the-checks":
            push_foreign(c)
        return merge_pr(coord, c, head)

    monkeypatch.setattr(coordinator, "_merge_point", point)
    monkeypatch.setattr(coordinator.Coordinator, "_merge_pr", merging)
    assert run_coordinator(r.config) == 0
    st, head = r.status(), git(r.w, "rev-parse", "HEAD").strip()
    assert st["status"] == "parked" and head != foreign.read_text().strip()
    if when == "during-the-checks":
        assert st["reason"] == (
            f"its PR's head on GitHub is {foreign.read_text().strip()}, not {head}, "
            "the head the merge step checked"
        )
        assert r.fakes.merges() == []
        # E.S30: the re-run reviews the foreign head, the one head never reviewed, then 40.1 parks.
        tested, pushed = len(r.tries("merge-tests")), len(r.step_pushes())
        assert (
            r.run() == 0 and r.status()["status"] == "parked" and r.fakes.merges() == []
        )
        assert (
            f"is behind the reviewed head {foreign.read_text().strip()[:12]}"
            in r.status()["reason"]
        )
        assert len(r.tries("review")) == 2
        assert (len(r.tries("merge-tests")), len(r.step_pushes())) == (tested, pushed)
    else:
        assert st["reason"].startswith(
            "gh pr merge refused: GraphQL: Head branch was modified"
        )
        [call] = r.fakes.merges()
        assert call["argv"][-1] == head
    assert (
        git(r.root / "o" / "r.git", "ls-tree", "--name-only", "ENG-40-e")
        .split()
        .count("evil.txt")
        == 0
    )


def test_a_merge_that_only_queues_the_pr_leaves_it_open(tmp_path, monkeypatch):
    """40.6, E.S29: on a branch with a merge queue, `gh pr merge` exits 0
    having only queued the PR (gh 2.102's help). `gh pr view` still says
    `OPEN`, so a is not `merged`: b, which needs a's change on main, is not
    launched, and the run exits 3 waiting on a's PR. A re-run goes through
    40.1-40.6 again (up to date: nothing tested, pushed or reviewed again)
    and merges it; then b is built."""
    r = MergeRoot(tmp_path, monkeypatch, pr={"merges": [{"queue": True}, {}]})
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert r.status()["status"] == "built" and r.status("b")["status"] == "waiting"
    assert _merged_prs(r) == ["e-a"] and r.fakes.attempts("b") == 0
    assert r.run() == 0 and r.merged() and _merged_prs(r) == ["e-a", "e-a", "e-b"]
    assert (
        len(r.tries("review")) == 1
        and len(r.tries("merge-tests")) == 1
        and len(r.step_pushes()) == 1
    )


def test_a_failed_merge_whose_pr_merged_is_merged(tmp_path, monkeypatch):
    """E.S10: `gh pr merge` merged the PR but exited 1 (its reply was lost);
    `gh pr view` says `MERGED`, so a is merged and b goes on."""
    r = MergeRoot(tmp_path, monkeypatch, main=None, pr={"merges": [{"lost": True}]})
    assert r.run() == 0 and r.merged() and _merged_prs(r) == ["e-a", "e-b"]


def test_a_failed_merge_whose_pr_cannot_be_read_is_unknown(tmp_path, monkeypatch):
    """40.6: refused, and `gh pr view` then fails: neither merged nor parked,
    but `unknown`; exit 1."""
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        main=None,
        pr={"merges": [{"refuse": "X refused", "offline": True}]},
    )
    assert r.run() == 1
    st = r.status()
    assert st["status"] == "unknown" and st["reason"] == (
        f"{coordinator.MERGE_UNKNOWN}gh pr merge failed and its PR cannot be read: X refused"
    )


def test_a_long_refusal_keeps_its_last_400_characters(tmp_path, monkeypatch):
    """40.6: the park reason holds the last 400 characters of gh's stderr."""
    refusal = "A" * 300 + "B" * 400
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        main=None,
        pr={"merges": [{"refuse": refusal}], "merge_state": "BLOCKED"},
    )
    assert r.run() == 0
    assert (
        r.status()["reason"]
        == f"gh pr merge refused: {'B' * 400} (mergeStateStatus BLOCKED)"
    )


def test_a_refused_merge_parks_with_the_refusal_and_is_not_retried_in_the_run(
    tmp_path, monkeypatch
):
    """40.6, E.S20: refused for another reason than `BEHIND`, the child parks
    with GitHub's words and the merge state, and b, waiting on it, is
    skipped. A re-run retries it once (E.S26: `<W>` is clean): one more
    `gh pr merge`, refused again, parked again."""
    refusal = (
        "X Pull request is not mergeable: the base branch policy prohibits the merge."
    )
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        pr={"merges": [{"refuse": refusal}], "merge_state": "BLOCKED"},
    )
    assert r.run() == 0
    st = r.status()
    assert (
        st["status"] == "parked"
        and st["reason"]
        == st["merge_park"]
        == f"gh pr merge refused: {refusal} (mergeStateStatus BLOCKED)"
    )
    assert _merged_prs(r) == ["e-a"] and r.status("b")["status"] == "skipped"
    assert (
        r.run() == 0
        and r.status()["reason"] == st["reason"]
        and _merged_prs(r) == ["e-a", "e-a"]
    )


def test_behind_starts_again_at_40_1_and_merges_the_new_commit(tmp_path, monkeypatch):
    """40.6: main moves while the checks are read, so `gh pr merge` is
    refused `BEHIND`. The step starts again at 40.1, merges the new commit
    (no conflict, so no agent call), tests and pushes it; the second
    `gh pr merge` merges."""
    r = MergeRoot(tmp_path, monkeypatch)
    r.use()

    def point(coord, c, name):
        r.point(coord, c, name)
        if name == "checks" and c.id == "a" and not (r.root / "moved2").exists():
            (r.root / "moved2").write_text(
                push_files(r.root, {"y.txt": "y\n"}, "main moved again")
            )

    monkeypatch.setattr(coordinator, "_merge_point", point)
    assert run_coordinator(r.config) == 0 and r.status()["status"] == "merged"
    assert _merged_prs(r) == ["e-a", "e-a", "e-b"]
    assert (
        len(r.calls("merge-agent")) == 1
        and len(r.tries("merge-tests")) == 2
        and len(r.step_pushes()) == 2
    )
    assert (
        r.origin("y.txt", "ENG-40-e") == "y\n"
        and r.origin("plugin.json", "ENG-40-e") == RESOLVED["plugin.json"]
    )


def test_behind_runs_40_1_again_so_an_edit_left_in_w_parks(tmp_path, monkeypatch):
    """42: `BEHIND` restarts at 40.1. A file a person left in `<W>` while
    the checks were read is refused there, naming it: nothing is merged,
    pushed again or discarded."""
    r = MergeRoot(tmp_path, monkeypatch)
    r.use()

    def point(coord, c, name):
        r.point(coord, c, name)
        if name == "checks" and c.id == "a" and not (r.root / "moved2").exists():
            (r.root / "moved2").write_text(
                push_files(r.root, {"y.txt": "y\n"}, "main moved again")
            )
            (r.w / "notes.txt").write_text("mine\n")

    monkeypatch.setattr(coordinator, "_merge_point", point)
    assert run_coordinator(r.config) == 0
    st = r.status()
    assert st["status"] == "parked" and st["reason"].startswith(
        f"{r.w} has uncommitted changes (notes.txt)"
    )
    assert (
        not r.merging() and git(r.w, "rev-parse", "HEAD").strip() == r.origin_sha()
    )  # 40.2 never ran again
    assert (
        _merged_prs(r) == ["e-a"]
        and len(r.step_pushes()) == 1
        and len(r.tries("merge-tests")) == 1
    )
    assert (r.w / "notes.txt").read_text() == "mine\n"


def test_behind_with_nothing_new_to_merge_parks_with_the_refusal(tmp_path, monkeypatch):
    """40.6: refused `BEHIND`, yet 40.2 finds the branch up to date with
    main: a refused merge is never tried again in one invocation, so it parks."""
    refusal = "X Pull request is not mergeable: the head branch is not up to date with the base branch."
    r = MergeRoot(
        tmp_path,
        monkeypatch,
        pr={"merges": [{"refuse": refusal}], "merge_state": "BEHIND"},
    )
    assert r.run() == 0
    assert (
        r.status()["reason"]
        == f"gh pr merge refused: {refusal} (mergeStateStatus BEHIND)"
    )
    assert _merged_prs(r) == ["e-a"] and len(r.step_pushes()) == 1


def test_epic_merge_tests_honor_cd_command(tmp_path, monkeypatch):
    import shlex
    import sys

    r = MergeRoot(tmp_path, monkeypatch)

    # Create an api directory in the actual merge worktree immediately before the suite.
    def command(project):
        (project / "api").mkdir(exist_ok=True)
        return f'cd api && {shlex.quote(sys.executable)} -c "from pathlib import Path; assert Path.cwd().name == chr(97)+chr(112)+chr(105)"'

    monkeypatch.setattr(merge, "suite_command", command)
    assert r.run() == 0 and r.merged()
    [tests] = r.tries("merge-tests")
    assert tests.started["argv"][0] == sys.executable
