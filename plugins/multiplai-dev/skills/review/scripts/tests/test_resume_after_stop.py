"""A review stopped by the usage limit or by the verifier resumes (plan item 23, rows S15-S19).

Each test stops a run at its row's point, runs a plain `review` of the same
target again, then resumes or starts over, and compares the result with a run
of the same target that never stopped: the finding ids and verdicts, the
names under rounds/ and the ledger events of the target directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixture_repo import _git as fixture_git
from review_pipeline import ledger, sdk
from review_pipeline.__main__ import main
from test_data_dir import _clone
from test_orchestrator import FakeAgents, _review_args


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> FakeAgents:
        fake = FakeAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake

    return install


def _lines(tmp_path: Path, folder: Path) -> list[dict]:
    path = tmp_path / "data" / "reviews" / "ledger.jsonl"
    return [
        e
        for e in map(json.loads, path.read_text().splitlines())
        if e.get("target_dir") == str(folder)
    ]


def _result(tmp_path: Path, folder: Path) -> dict:
    data = json.loads((folder / "findings.json").read_text())
    kept = folder / "rounds"
    return {
        "findings": sorted(
            (f["id"], f["severity"], f["status"], f.get("impact"))
            for f in data["findings"]
        ),
        "head": data["target"]["head_sha"],
        "rounds": sorted(p.name for p in kept.iterdir()) if kept.is_dir() else [],
    }


def _events(tmp_path: Path, folder: Path) -> list[tuple[str, str | None]]:
    return [(e["event"], e.get("stopped")) for e in _lines(tmp_path, folder)]


def _folder(out: Path, base: str, head: str) -> Path:
    return out / "booking-engine" / f"range-{base}..{head}"


def _continue_line(folder: Path) -> str:
    return f"interrupted run found; continue with: python -m review_pipeline resume {folder}"


def _verify_fails(monkeypatch, fake: FakeAgents) -> None:
    """Every verify call fails, so the run ends `verify-incomplete` (exit 5)."""

    async def failing(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "verify":
            raise sdk.AgentCallError("verify: no valid answer after a re-ask")
        return await fake(prompt, schema, budget_label=budget_label, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_structured", failing)


def test_s15_a_usage_limit_stop_makes_review_exit_4_resume_continues_and_restart_starts_over(
    fixture_repo, tmp_path, agents, capsys
):
    repo, base, head = fixture_repo
    out, other, never = tmp_path / "out", tmp_path / "other", tmp_path / "never"
    folder = _folder(out, base, head)
    fake = agents(limit_at=3, cost=1.0)
    assert main(_review_args(repo, base, head, out)) == 6
    state = (folder / "review-state.json").read_bytes()
    capsys.readouterr()

    # A plain review after the stop: no model call, nothing written, the resume command.
    fake.limit_at = None  # the limit reset
    before = len(fake.calls)
    assert main(_review_args(repo, base, head, out)) == 4
    assert _continue_line(folder) in capsys.readouterr().err
    assert len(fake.calls) == before
    assert (folder / "review-state.json").read_bytes() == state
    assert not (folder / "rounds").exists()

    assert (
        main(["--session-id", "sess-test", "resume", str(folder), "--trust-repo"]) == 0
    )
    # Only the calls the stopped run had not made: the two that came back are not asked again.
    assert "find:diff-bugs" not in fake.calls[before:]
    fake_calls = list(fake.calls)
    agents()
    assert main(_review_args(repo, base, head, never)) == 0
    never_folder = _folder(never, base, head)
    assert _result(tmp_path, folder) == _result(tmp_path, never_folder)
    assert _events(tmp_path, folder) == [
        ("started", None),
        ("finished", "usage-limit"),
        ("started", None),
        ("finished", None),
    ]
    stopped, resumed = (e for e in _lines(tmp_path, folder) if e["event"] == "finished")
    # The resume keeps the run's first start and its spend: its line holds the whole run's cost.
    assert stopped["run"]["started_at"] == resumed["run"]["started_at"]
    assert stopped["run"]["cost_usd"] == 2.0  # the calls that hit the limit are unpaid
    assert resumed["run"]["cost_usd"] == 2.0 + len(fake_calls[before:])

    # --restart after the same stop starts over and asks every call again.
    other_folder = _folder(other, base, head)
    fake = agents(limit_at=3)
    assert main(_review_args(repo, base, head, other)) == 6
    fake.limit_at = None
    before = len(fake.calls)
    assert main(_review_args(repo, base, head, other, "--restart")) == 0
    assert "find:diff-bugs" in fake.calls[before:]
    assert _result(tmp_path, other_folder) == dict(
        _result(tmp_path, never_folder), rounds=[f"01-{head[:12]}"]
    )
    assert _events(tmp_path, other_folder)[-1] == ("finished", None)


def test_s16_a_verify_incomplete_stop_makes_review_exit_4_and_resume_continues(
    fixture_repo, tmp_path, agents, monkeypatch, capsys
):
    repo, base, head = fixture_repo
    out, never = tmp_path / "out", tmp_path / "never"
    folder = _folder(out, base, head)
    fake = agents()
    _verify_fails(monkeypatch, fake)
    assert main(_review_args(repo, base, head, out)) == 5
    state = (folder / "review-state.json").read_bytes()
    capsys.readouterr()

    fake = agents()
    assert main(_review_args(repo, base, head, out)) == 4
    assert _continue_line(folder) in capsys.readouterr().err
    assert fake.calls == []
    assert (folder / "review-state.json").read_bytes() == state

    assert main(["resume", str(folder), "--trust-repo"]) == 0
    assert not any(c.startswith("find:") for c in fake.calls)
    assert main(_review_args(repo, base, head, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, _folder(never, base, head))
    assert _events(tmp_path, folder) == [
        ("started", None),
        ("finished", "verify-incomplete"),
        ("started", None),
        ("finished", None),
    ]


def test_s17_a_budget_stop_still_lets_a_plain_review_start_over(
    fixture_repo, tmp_path, agents
):
    repo, base, head = fixture_repo
    out, never = tmp_path / "out", tmp_path / "never"
    folder = _folder(out, base, head)
    fake = agents(budget_stop_at=3)
    assert main(_review_args(repo, base, head, out)) == 4
    assert _events(tmp_path, folder)[-1] == ("finished", "budget")

    before = len(fake.calls)
    assert main(_review_args(repo, base, head, out)) == 0
    assert "find:diff-bugs" in fake.calls[before:]  # started over
    assert main(_review_args(repo, base, head, never)) == 0
    # The stopped run is kept as its own round, as before this change.
    assert _result(tmp_path, folder) == dict(
        _result(tmp_path, _folder(never, base, head)), rounds=[f"01-{head[:12]}"]
    )
    assert _events(tmp_path, folder) == [
        ("started", None),
        ("finished", "budget"),
        ("started", None),
        ("finished", None),
    ]


def test_s18_a_usage_limit_stop_on_an_old_head_starts_over_on_the_new_head(
    fixture_repo, tmp_path, agents
):
    _, base, head = fixture_repo
    repo = _clone(fixture_repo, tmp_path / "checkout")
    fixture_git(repo, "update-ref", "refs/remotes/origin/main", base)
    fixture_git(repo, "update-ref", "refs/remotes/origin/feature/x", head)
    fixture_git(repo, "checkout", "-q", "-b", "next", head)
    (repo / "notes.txt").write_text("a second commit\n")
    fixture_git(repo, "add", "notes.txt")
    fixture_git(repo, "commit", "-q", "-m", "second head", date="2026-09-17T12:00:00Z")
    head2 = fixture_git(repo, "rev-parse", "HEAD")

    def args(out: Path) -> list[str]:
        return [
            "--out",
            str(out),
            "review",
            "--repo",
            str(repo),
            "--branch",
            "feature/x",
            "--trust-repo",
        ]

    out, never = tmp_path / "out", tmp_path / "never"
    folder = out / "github-tests" / "branch-feature_x"
    fake = agents(limit_at=3)
    assert main(args(out)) == 6
    assert ledger.resumable(folder, head)

    fake.limit_at = None
    fixture_git(repo, "update-ref", "refs/remotes/origin/feature/x", head2)
    before = len(fake.calls)
    assert main(args(out)) == 0
    assert "find:diff-bugs" in fake.calls[before:]  # started over
    agents()
    assert main(args(never)) == 0
    assert _result(tmp_path, folder) == dict(
        _result(tmp_path, never / "github-tests" / "branch-feature_x"),
        rounds=[f"01-{head[:12]}"],
    )
    assert _result(tmp_path, folder)["head"] == head2
    assert not ledger.resumable(folder, head2)


def test_s19_a_resume_stopped_by_the_usage_limit_again_is_offered_again(
    fixture_repo, tmp_path, agents, capsys
):
    repo, base, head = fixture_repo
    out, never = tmp_path / "out", tmp_path / "never"
    folder = _folder(out, base, head)
    fake = agents(limit_at=3)
    assert main(_review_args(repo, base, head, out)) == 6
    # The limit has not reset: the resume stops on its first call.
    assert main(["resume", str(folder), "--trust-repo"]) == 6
    assert [e for e in _events(tmp_path, folder) if e[0] == "finished"] == [
        ("finished", "usage-limit"),
        ("finished", "usage-limit"),
    ]
    capsys.readouterr()

    fake.limit_at = None
    before = len(fake.calls)
    assert main(_review_args(repo, base, head, out)) == 4
    assert _continue_line(folder) in capsys.readouterr().err
    assert len(fake.calls) == before

    assert main(["resume", str(folder), "--trust-repo"]) == 0
    agents()
    assert main(_review_args(repo, base, head, never)) == 0
    assert _result(tmp_path, folder) == _result(tmp_path, _folder(never, base, head))
    assert _events(tmp_path, folder)[-1] == ("finished", None)
    finished = [e for e in _lines(tmp_path, folder) if e["event"] == "finished"]
    assert len({e["run"]["started_at"] for e in finished}) == 1
