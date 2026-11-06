"""Incremental re-review (`incremental.py`): a re-pushed branch reviews only the files whose patch changed.

A branch `feature` changes a.py, b.py and c.py. Round one finds one defect in a.py and one in
b.py. Each test then moves the branch and runs round two.

Stop-and-resume rows of the plan's table covered here: S2 (`prepare` stopped after `keep_round`,
before the scope was saved) and S3/S4 (stopped after the scope and the carried findings were
saved; resume reuses both and carries nothing twice). `test_resume_rows.py` stops at every row
from S1 to S7 and S11 and compares the result with a run that never stopped; its docstring names
the tests for S8, S9, S10 and S12.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import cite
from review_pipeline import budget, incremental, orchestrator, rounds, sdk
from review_pipeline.__main__ import main
from review_pipeline.incremental import changed_files, normalised_patches
from review_pipeline.models import (
    AssessOutput,
    Finding,
    RecheckOutput,
    ReviewState,
    TargetInfo,
    Verdict,
)
from review_pipeline.stages import recheck
from review_pipeline.state import load_state

C_BASE = "".join(f"c{i} = {i}\n" for i in range(1, 11))


def _git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@example.com",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).write_text(text)


FEATURE = {
    "a.py": "def a():\n    return None.x\n",
    "b.py": "def b():\n    return 1 / 0\n",
    "c.py": C_BASE.replace("c10 = 10", "c10 = 'ten'"),
}


def _branch(repo: Path, base: str, files: dict[str, str], message: str) -> str:
    """Commit *files* on top of *base* and point `origin/feature` at it."""
    _git(repo, "checkout", "-q", "--detach", base)
    _write(repo, files)
    _git(repo, "commit", "-q", "-am", message)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/feature", head)
    _git(repo, "update-ref", "refs/heads/feature", head)
    return head


@pytest.fixture
def repo(tmp_path) -> Path:
    return build_repo(tmp_path)


def build_repo(tmp_path: Path) -> Path:
    """`main` with a.py, b.py and c.py, and the branch `feature` changing all three."""
    r = tmp_path / "shop"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write(
        r,
        {
            "a.py": "def a():\n    return 1\n",
            "b.py": "def b():\n    return 1\n",
            "c.py": C_BASE,
        },
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    _branch(r, "main", FEATURE, "feature")
    return r


FA = Finding(
    claim="a() dereferences None",
    severity="MEDIUM",
    file="a.py",
    line_start=2,
    line_end=2,
    failure_scenario="any call raises",
    citations=[cite("a.py", 2, "return None.x")],
)
FB = Finding(
    claim="b() divides by zero",
    severity="MEDIUM",
    file="b.py",
    line_start=2,
    line_end=2,
    failure_scenario="any call raises",
    citations=[cite("b.py", 2, "return 1 / 0")],
)


class Agents:
    """Finds FA and FB in whichever of their files the diff-bugs report is given; records every call."""

    def __init__(self):
        self.labels: list[str] = []
        self.find_prompts: list[str] = []
        self.stop_at_find = False

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        self.labels.append(budget_label)
        stage = budget_label.split(":")[0]
        if stage == "find":
            if self.stop_at_find:
                raise sdk.UsageLimitError("limit", limit="session", resets="9am")
            if budget_label != "find:diff-bugs":
                return schema()
            self.find_prompts.append(prompt)
            return schema(findings=[f for f in (FA, FB) if f"+++ b/{f.file}" in prompt])
        if stage == "verify":
            f = next(f for f in (FA, FB) if f.claim in prompt)
            return Verdict(
                status="confirmed",
                impact="breaks-users",
                reason="read it",
                citations=[f.citations[0]],
                expected_behaviour="It returns a value.",
            )
        if stage == "recheck":
            return RecheckOutput(
                status="cannot_verify", reason="the fake reads no code"
            )
        if stage == "assess":
            return AssessOutput()
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch) -> Agents:
    fake = Agents()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    return fake


def _args(repo: Path, out: Path, *extra: str) -> list[str]:
    return [
        "--out", str(out), "review", "--repo", str(repo), "--branch", "feature",
        "--base-branch", "main", "--trust-repo", *extra,
    ]  # fmt: skip


def _round_one(repo: Path, out: Path, agents: Agents) -> Path:
    assert main(_args(repo, out)) == 0
    target_dir = out / "shop" / "branch-feature"
    data = json.loads((target_dir / "findings.json").read_text())
    assert {f["id"] for f in data["findings"]} == {FA.id, FB.id}
    assert "scope" not in data  # round one reviews every file
    agents.labels.clear()
    agents.find_prompts.clear()
    return target_dir


def _accept(target_dir: Path, fid: str) -> None:
    path = target_dir / "viewer" / "decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {fid: {"decision": "accept", "note": "", "ts": "2026-01-01T00:00:00Z"}}
        )
    )


def test_a_one_file_push_reviews_that_file_and_carries_the_rest(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    _accept(target_dir, FB.id)
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(
        repo,
        base,
        {"a.py": FEATURE["a.py"] + "\n\ndef a2():\n    return 2\n"},
        "more a",
    )

    assert main(_args(repo, out)) == 0
    (prompt,) = agents.find_prompts  # one piece, one diff-bugs call
    assert "+++ b/a.py" in prompt and "b.py" not in prompt and "c.py" not in prompt
    state = load_state(target_dir / "review-state.json")
    assert state.scope.kind == "incremental"
    assert state.scope.reviewed_files == ["a.py"] and state.scope.total_files == 3
    assert state.scope.carried_ids == [FB.id]
    data = json.loads((target_dir / "findings.json").read_text())
    rows = {f["id"]: f for f in data["findings"]}
    assert set(rows) == {FA.id, FB.id}
    assert rows[FB.id]["carried_from"] == state.scope.since_round
    assert rows[FB.id]["status"] == "confirmed" and rows[FB.id]["severity"] == "MEDIUM"
    assert "carried_from" not in rows[FA.id]
    # FB made no call this round; its decision is keyed by its unchanged id.
    assert not any(FB.claim in p for p in agents.find_prompts)
    assert (
        json.loads((target_dir / "viewer" / "decisions.json").read_text())[FB.id][
            "decision"
        ]
        == "accept"
    )
    assert data["scope"]["reviewed_files"] == ["a.py"]
    review = next(target_dir.glob("review-*.md")).read_text()
    assert "Reviewed 1 of 3 files; 1 findings carried from round 1" in review
    # The recheck asked only about FA, the earlier finding on the re-reviewed file.
    assert agents.labels.count("recheck") == 1


def test_a_rebase_that_keeps_every_patch_reviews_nothing(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    # main moves: two lines at the top of c.py, so the rebased patch has new line numbers and blobs.
    _git(repo, "checkout", "-q", "--detach", "main")
    (repo / "c.py").write_text("# moved\n# down\n" + C_BASE)
    _git(repo, "commit", "-q", "-am", "main moves")
    main_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_head)
    _git(repo, "update-ref", "refs/heads/main", main_head)
    moved = dict(FEATURE, **{"c.py": "# moved\n# down\n" + FEATURE["c.py"]})
    _branch(repo, main_head, moved, "feature, rebased")

    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.kind == "incremental" and state.scope.reviewed_files == []
    assert sorted(state.scope.carried_ids) == sorted([FA.id, FB.id])
    assert agents.labels == []  # no finder, verifier, recheck, assess or write call
    assert (
        f"nothing changed since round {state.scope.since_number}"
        in (target_dir / "progress.log").read_text()
    )
    data = json.loads((target_dir / "findings.json").read_text())
    assert {f["id"] for f in data["findings"]} == {FA.id, FB.id}
    assert all(f["carried_from"] for f in data["findings"])


def test_a_force_push_that_drops_a_file_resolves_its_findings(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    _branch(
        repo,
        "main",
        {k: v for k, v in FEATURE.items() if k != "b.py"},
        "feature without b",
    )

    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.reviewed_files == [] and state.scope.carried_ids == [FA.id]
    assert agents.labels == []
    data = json.loads((target_dir / "findings.json").read_text())
    assert [f["id"] for f in data["findings"]] == [FA.id]
    (earlier,) = data["earlier"]
    assert earlier["earlier_id"] == FB.id
    assert (
        earlier["status"] == "resolved"
        and earlier["reason"] == "file no longer in the change"
    )


def test_a_finding_citing_a_file_that_left_the_change_is_rechecked_not_carried(
    repo, tmp_path, agents, monkeypatch
):
    # FA's own file keeps its patch, but it also cites c.py, which the force-push reverts.
    fac = FA.model_copy(
        update={"citations": [FA.citations[0], cite("c.py", 1, "c1 = 1")]}
    )
    monkeypatch.setitem(globals(), "FA", fac)
    out = tmp_path / "out"
    assert main(_args(repo, out)) == 0
    target_dir = out / "shop" / "branch-feature"
    agents.labels.clear()
    _branch(
        repo,
        "main",
        {k: v for k, v in FEATURE.items() if k != "c.py"},
        "feature without c",
    )

    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.reviewed_files == [] and state.scope.carried_ids == [FB.id]
    assert agents.labels.count("recheck") == 1  # FA, which cites the reverted c.py


def test_full_reviews_every_file(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(repo, base, {"a.py": FEATURE["a.py"] + "\n"}, "more a")
    assert main(_args(repo, out, "--full")) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.kind == "full" and state.scope.reason == "--full was given"
    (prompt,) = agents.find_prompts
    assert all(f"+++ b/{n}" in prompt for n in ("a.py", "b.py", "c.py"))
    assert "carried_from" not in json.dumps(
        json.loads((target_dir / "findings.json").read_text())
    )


def test_review_yaml_can_turn_it_off(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    (out / "review.yaml").write_text("incremental: false\n")
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(repo, base, {"a.py": FEATURE["a.py"] + "\n"}, "more a")
    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert (
        state.scope.kind == "full"
        and state.scope.reason == "review.yaml sets incremental: false"
    )


def test_s2_a_prepare_stopped_before_its_scope_was_saved_is_redone_the_same(
    repo, tmp_path, agents
):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(repo, base, {"a.py": FEATURE["a.py"] + "\n"}, "more a")
    spec = orchestrator.TargetSpec(repo=str(repo), branch="feature", base_branch="main")
    first, _ = orchestrator.prepare(
        spec, out
    )  # keeps round 01; its state is never saved
    rounds_before = sorted(p.name for p in (target_dir / "rounds").iterdir())

    assert main(_args(repo, out)) == 0
    assert sorted(p.name for p in (target_dir / "rounds").iterdir()) == rounds_before
    state = load_state(target_dir / "review-state.json")
    assert state.scope == first.scope


def test_s3_s4_resume_reuses_the_saved_scope_and_carries_nothing_twice(
    repo, tmp_path, agents
):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(repo, base, {"a.py": FEATURE["a.py"] + "\n"}, "more a")
    agents.stop_at_find = True
    assert main(_args(repo, out)) == 6  # the usage limit, at the first finder call
    saved = load_state(target_dir / "review-state.json")
    assert saved.scope.reviewed_files == ["a.py"] and saved.scope.carried_ids == [FB.id]
    assert [f.id for f in saved.findings] == [FB.id]

    # The branch moves again before the resume: the stored scope still decides.
    _branch(
        repo,
        "main",
        dict(FEATURE, **{"b.py": "def b():\n    return 2\n"}),
        "moved again",
    )
    agents.stop_at_find = False
    assert main(["--out", str(out), "resume", str(target_dir), "--trust-repo"]) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope == saved.scope
    (prompt,) = agents.find_prompts
    assert "+++ b/a.py" in prompt and "b.py" not in prompt
    assert [f.id for f in state.findings].count(FB.id) == 1


def _fixer_said(target_dir: Path, fid: str, decision: str) -> None:
    (target_dir / rounds.FIXER_DECISIONS).write_text(
        json.dumps({fid: {"decision": decision, "commit": "abc", "round": 1}})
    )


def _push_only_a(repo: Path) -> None:
    base = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    _branch(
        repo,
        base,
        {"a.py": FEATURE["a.py"] + "\n\ndef a2():\n    return 2\n"},
        "more a",
    )


@pytest.mark.parametrize("decision", ["fixed", "not a defect"])
def test_fixed_finding_is_rechecked_not_copied(repo, tmp_path, agents, decision):
    """Done means 7: FB's cited file is unchanged, but the fixer decided it, so
    it is not copied and the recheck stage gives it a status."""
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    _fixer_said(target_dir, FB.id, decision)
    _push_only_a(repo)

    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.carried_ids == []
    found = recheck.open_set(state, SimpleNamespace(target_dir=target_dir))
    assert {o.id for o in found} == {FA.id, FB.id}
    assert agents.labels.count("recheck") == 2
    data = json.loads((target_dir / "findings.json").read_text())
    assert FB.id not in {f["id"] for f in data["findings"]}
    assert {e["earlier_id"] for e in data["earlier"]} == {FA.id, FB.id}


def test_a_finding_with_no_fixer_decision_is_still_copied(repo, tmp_path, agents):
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    _fixer_said(target_dir, FB.id, "still open")
    _push_only_a(repo)
    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.carried_ids == [FB.id]
    assert not [e for e in state.errors if rounds.FIXER_DECISIONS in e]


@pytest.mark.parametrize("text", ["{not json", "[1]", None])
def test_lineage_s20_an_unreadable_decisions_file_copies_as_today(
    repo, tmp_path, agents, text
):
    """Row S20: a corrupt file gives one line in `state.errors`; a missing one,
    the file of every review with no fixer, gives none. The review does not fail."""
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    if text is not None:
        (target_dir / rounds.FIXER_DECISIONS).write_text(text)
    _push_only_a(repo)
    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.carried_ids == [FB.id]
    lines = [e for e in state.errors if rounds.FIXER_DECISIONS in e]
    assert len(lines) == (0 if text is None else 1)


def test_a_fixed_finding_with_nothing_changed_is_still_rechecked(
    repo, tmp_path, agents
):
    """Nothing to re-review skips the finders, but not the recheck."""
    out = tmp_path / "out"
    target_dir = _round_one(repo, out, agents)
    _fixer_said(target_dir, FB.id, "fixed")
    _git(repo, "checkout", "-q", "--detach", "main")
    (repo / "c.py").write_text("# moved\n# down\n" + C_BASE)
    _git(repo, "commit", "-q", "-am", "main moves")
    main_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_head)
    _git(repo, "update-ref", "refs/heads/main", main_head)
    moved = dict(FEATURE, **{"c.py": "# moved\n# down\n" + FEATURE["c.py"]})
    _branch(repo, main_head, moved, "feature, rebased")

    assert main(_args(repo, out)) == 0
    state = load_state(target_dir / "review-state.json")
    assert state.scope.reviewed_files == [] and state.scope.carried_ids == [FA.id]
    assert agents.labels == ["recheck"]
    data = json.loads((target_dir / "findings.json").read_text())
    assert [f["id"] for f in data["findings"]] == [FA.id]
    assert [e["earlier_id"] for e in data["earlier"]] == [FB.id]


PR88 = Path(__file__).parent / "fixtures" / "review-pr-88-rounds"
AE9 = "ae939f8f7f"
MIGRATION = "backend/DolcePricing/migrations/0005_attribuzione_attore_created_by.py"


def _pr88(repo: Path, target_dir: Path, names: list[str]) -> TargetInfo:
    """The kept rounds *names* of #88, each with the `diff.patch` and
    `review-state.json` `decide` reads, over a repository where the fix went
    into a new migration and the cited `0005` never changed."""
    (repo / "backend/DolcePricing/migrations").mkdir(parents=True)
    _git(repo, "checkout", "-q", "-b", "pr-88", "main")
    (repo / MIGRATION).write_text("".join(f"line {i}\n" for i in range(40)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the migration")
    head = _git(repo, "rev-parse", "HEAD")
    diff = _git(repo, "diff", "main", head) + "\n"
    finding = Finding(
        id=AE9,
        claim=(
            "The new non-null snapshot columns lack persistent database defaults, "
            "making inserts incompatible with rollback to the previous application image."
        ),
        severity="MEDIUM",
        file=MIGRATION,
        line_start=20,
        line_end=33,
        failure_scenario="a rollback leaves old code writing rows",
        citations=[cite(MIGRATION, 20, "line 19")],
    )
    assert finding.id == AE9  # #88's own claim, file and line
    for name in names:
        kept = target_dir / rounds.ROUNDS_DIR / name
        kept.mkdir(parents=True)
        (kept / "findings.json").write_text((PR88 / name / "findings.json").read_text())
        (kept / "diff.patch").write_text(diff)
        info = TargetInfo(
            repo_path=str(repo), base_sha="b" * 40, head_sha=head, slug="x"
        )
        save = ReviewState(target=info, mode="full", findings=[finding])
        (kept / "review-state.json").write_text(save.model_dump_json())
    (
        repo / MIGRATION.replace("0005_attribuzione_attore_created_by", "0006_fix")
    ).write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the fix")
    return TargetInfo(
        repo_path=str(repo),
        base_sha="b" * 40,
        head_sha=_git(repo, "rev-parse", "HEAD"),
        slug="x",
        kind="pr",
    )


@pytest.mark.parametrize(
    "names, decision",
    [
        (["01-157421486e29"], "fixed"),
        (["01-157421486e29", "02-a596f72c2e39"], "not a defect"),
    ],
)
def test_pr88_replay(repo, tmp_path, names, decision):
    """Done means 7: `ae939f8f7f` was fixed in `484a7ba7`, in new migrations,
    and reviews 02-07 copied it with no agent call. With the fixer's decision
    in the review's folder it is not copied."""
    target_dir = tmp_path / "pr-88"
    info = _pr88(repo, target_dir, names)
    diff = _git(repo, "diff", "main", info.head_sha) + "\n"

    def carried() -> list[str]:
        scope, _ = incremental.decide(
            target_dir, info, diff, mode="full", full=False, enabled=True
        )
        assert scope.kind == "incremental"
        return scope.carried_ids

    assert carried() == [AE9]  # what the saved reviews did
    _fixer_said(target_dir, AE9, decision)
    assert carried() == []
    next_saved = {
        "01-157421486e29": "02-a596f72c2e39",
        "02-a596f72c2e39": "03-f3ef966779b7",
    }[names[-1]]
    saved = json.loads((PR88 / next_saved / "findings.json").read_text())
    [row] = [f for f in saved["findings"] if f["id"] == AE9]
    assert row["carried_from"] == names[-1]


def test_patches_compare_without_index_lines_or_hunk_numbers():
    old = (
        "diff --git a/c.py b/c.py\nindex 111..222 100644\n--- a/c.py\n+++ b/c.py\n"
        "@@ -8,3 +8,3 @@ def f():\n c8 = 8\n-c10 = 10\n+c10 = 'ten'\n"
    )
    new = old.replace("index 111..222", "index 333..444").replace(
        "@@ -8,3 +8,3 @@", "@@ -10,3 +10,3 @@"
    )
    assert normalised_patches(old) == normalised_patches(new)
    assert changed_files(old, new) == []
    assert changed_files(old, new.replace("'ten'", "'TEN'")) == ["c.py"]
