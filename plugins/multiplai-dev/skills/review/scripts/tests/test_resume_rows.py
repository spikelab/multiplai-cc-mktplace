"""Each stop row of the review-cost plan, stopped there and resumed, against a run that never stopped.

For S1–S7 and S11 a run is stopped at the row's point and resumed with `resume` (S2 by a new
`review`, as nothing is saved yet). Its `review-state.json` (less `runs`, `budget`, timings and
the output directory, with the per-call `checks` records compared in any order), `findings.json` (less `generated_at` and `run`) and number of agent calls
must equal those of the same review run once without a stop, in another output directory.

S8 is `test_export_zip.py` in review-viewer. S12 (the stopped process still running) is
`test_recheck.py::test_earlier_status_resume_s12_a_running_review_refuses_a_resume`: the run
lock refuses, unchanged. S9 (files edited between stop and resume) and S10 (the head moved) keep
today's behaviour: `review-state.json` is the source and the old head is finished; the existing
resume tests cover both, and `test_incremental.py` resumes after the branch moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import cite
from review_pipeline import budget, orchestrator, sdk
from review_pipeline.__main__ import main
from review_pipeline.models import (
    AssessOutput,
    Finding,
    RecheckOutput,
    Verdict,
    WriteItem,
    WriteOutput,
)
from review_pipeline.prompts import write as write_prompt
from review_pipeline.stages import paths as paths_stage
from test_incremental import FA, FB, FEATURE, _args, _branch, _git, build_repo


@pytest.fixture
def repo(tmp_path) -> Path:
    return build_repo(tmp_path)


pytestmark = (
    pytest.mark.write_calls
)  # the fake answers the write stage too, and counts it

FC = Finding(
    claim="c10 becomes a string where callers add to it",
    severity="LOW",
    file="c.py",
    line_start=10,
    line_end=10,
    failure_scenario="c10 + 1 raises TypeError",
    citations=[cite("c.py", 10, "c10 = 'ten'")],
)
STOPPED = 6  # main's exit code at a usage limit


class Agents:
    """Answers every stage in code and counts the calls that returned.

    `stop_at[stage] = n` raises a usage limit at that stage's n-th call (from 1) instead of
    answering it; the raised call is not counted, as no answer came back.
    """

    def __init__(self):
        self.calls = 0
        self.seen: dict[str, int] = {}
        self.stop_at: dict[str, int] = {}

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        stage = budget_label.split(":")[0]
        self.seen[stage] = self.seen.get(stage, 0) + 1
        if self.stop_at.get(stage) == self.seen[stage]:
            del self.stop_at[stage]
            raise sdk.UsageLimitError("limit", limit="session", resets="9am")
        self.calls += 1
        if stage == "find":
            if budget_label != "find:diff-bugs":
                return schema()
            return schema(
                findings=[f for f in (FA, FB, FC) if f"+++ b/{f.file}" in prompt]
            )
        if stage == "verify":
            f = next(f for f in (FA, FB, FC) if f.claim in prompt)
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
        if stage == "write":
            ids = [f.id for f in (FA, FB, FC) if f'"id": "{f.id}"' in prompt]
            return WriteOutput(
                findings=[
                    WriteItem(
                        id=i,
                        title="A plain title",
                        what_happens="It fails. For example, any call.",
                    )
                    for i in ids
                ]
            )
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch) -> Agents:
    fake = Agents()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    monkeypatch.setattr(
        write_prompt, "BATCH", 1
    )  # one write call per finding, so S6 can stop between
    return fake


def _setup(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.yaml").write_text(
        "concurrency: 1\n"
    )  # calls in one order, so stops land the same


def _strip(value, out: Path, drop: set[str]):
    if isinstance(value, dict):
        return {k: _strip(v, out, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_strip(v, out, drop) for v in value]
    if isinstance(value, str):
        return value.replace(str(out), "<out>")
    return value


STATE_DROP = {
    "runs",
    "budget",
    "timings",
    "started_at",
    "ended_at",
    "finished_at",
    "generated_at",
    "duration_ms",
    "duration_s",
}
FINDINGS_DROP = {"generated_at", "run"}


def _snapshot(out: Path) -> tuple[dict, dict]:
    target_dir = out / "shop" / "branch-feature"
    state = json.loads((target_dir / "review-state.json").read_text())
    findings = json.loads((target_dir / "findings.json").read_text())
    state = _strip(state, out, STATE_DROP)
    # One record per agent call, in the order they ended: a resumed run's come after the stop.
    state["checks"] = sorted(
        state.get("checks", []), key=lambda c: json.dumps(c, sort_keys=True)
    )
    return state, _strip(findings, out, FINDINGS_DROP)


def _differences(got, want, where: str = "") -> list[str]:
    """The paths at which *got* and *want* differ, for a readable failure."""
    if isinstance(got, dict) and isinstance(want, dict):
        return [
            d
            for k in sorted(set(got) | set(want), key=str)
            for d in _differences(got.get(k), want.get(k), f"{where}.{k}")
        ]
    if (
        isinstance(got, (list, tuple))
        and isinstance(want, (list, tuple))
        and len(got) == len(want)
    ):
        return [
            d
            for i, (g, w) in enumerate(zip(got, want))
            for d in _differences(g, w, f"{where}[{i}]")
        ]
    return [] if got == want else [f"{where}: {str(got)[:150]} != {str(want)[:150]}"]


def _resume(out: Path) -> int:
    return main(
        [
            "--out",
            str(out),
            "resume",
            str(out / "shop" / "branch-feature"),
            "--trust-repo",
        ]
    )


def _never_stopped(
    repo: Path, out: Path, agents: Agents, rounds: int
) -> tuple[tuple[dict, dict], int]:
    """The review run once per round with no stop; its outputs and the calls of its last round."""
    _setup(out)
    for n in range(rounds):
        if n == 1:
            _push(repo)
        before = agents.calls
        assert main(_args(repo, out)) == 0
    return _snapshot(out), agents.calls - before


def _push(repo: Path) -> None:
    """One commit on the branch that changes a.py only (incremental rows' round two)."""
    tip = _git(repo, "rev-parse", "refs/remotes/origin/feature")
    if _git(repo, "log", "-1", "--format=%s", tip) != "more a":
        _branch(
            repo,
            tip,
            {"a.py": FEATURE["a.py"] + "\n\ndef a2():\n    return 2\n"},
            "more a",
        )


def _reset_branch(repo: Path) -> None:
    _branch(repo, "main", FEATURE, "feature")


def _compare(repo, tmp_path, agents, *, rounds: int, stop) -> None:
    """Run the never-stopped review, then the same review stopped by *stop* and resumed."""
    expected, expected_calls = _never_stopped(repo, tmp_path / "plain", agents, rounds)
    _reset_branch(repo)
    out = tmp_path / "stopped"
    _setup(out)
    if rounds == 2:
        assert main(_args(repo, out)) == 0
        _push(repo)
    agents.seen.clear()
    before = agents.calls
    stop(out)
    got = _snapshot(out)
    assert _differences(got, expected) == []
    assert agents.calls - before == expected_calls


def _paths_stops_once(monkeypatch) -> None:
    real = paths_stage.map_paths
    left = [1]

    def map_paths(*args, **kwargs):
        if left:
            left.pop()
            raise sdk.UsageLimitError("limit", limit="session", resets="9am")
        return real(*args, **kwargs)

    monkeypatch.setattr(paths_stage, "map_paths", map_paths)


def test_s1_stopped_right_after_the_start_line_appends_a_second_run(
    repo, tmp_path, agents, monkeypatch
):
    def stop(out):
        _paths_stops_once(monkeypatch)  # the first stage: the state still says `target`
        assert main(_args(repo, out)) == STOPPED
        target_dir = out / "shop" / "branch-feature"
        first = json.loads((target_dir / "review-state.json").read_text())["runs"]
        assert len(first) == 1 and first[0]["producer"]
        assert _resume(out) == 0
        runs = json.loads((target_dir / "review-state.json").read_text())["runs"]
        assert runs[0] == first[0] and len(runs) == 2
        log = (target_dir / "progress.log").read_text()
        assert log.count("STARTED multiplai-dev:review ") == 2

    _compare(repo, tmp_path, agents, rounds=1, stop=stop)


def test_s2_prepare_stopped_before_the_scope_was_saved(repo, tmp_path, agents):
    def stop(out):
        spec = orchestrator.TargetSpec(
            repo=str(repo), branch="feature", base_branch="main"
        )
        orchestrator.prepare(
            spec, out
        )  # keeps round 01, saves nothing: the process dies here
        assert main(_args(repo, out)) == 0

    _compare(repo, tmp_path, agents, rounds=2, stop=stop)


def test_s3_stopped_after_the_scope_was_saved(repo, tmp_path, agents, monkeypatch):
    def stop(out):
        _paths_stops_once(monkeypatch)
        assert main(_args(repo, out)) == STOPPED
        state = json.loads(
            (out / "shop" / "branch-feature" / "review-state.json").read_text()
        )
        assert state["scope"]["kind"] == "incremental" and state["stage"] == "target"
        assert _resume(out) == 0

    _compare(repo, tmp_path, agents, rounds=2, stop=stop)


def test_s4_stopped_during_find_with_the_carried_findings_saved(repo, tmp_path, agents):
    def stop(out):
        agents.stop_at["find"] = 2
        assert main(_args(repo, out)) == STOPPED
        state = json.loads(
            (out / "shop" / "branch-feature" / "review-state.json").read_text()
        )
        assert [f["carried_from"] for f in state["findings"]] == [
            state["scope"]["since_round"]
        ] * 2
        assert _resume(out) == 0

    _compare(repo, tmp_path, agents, rounds=2, stop=stop)


def test_s5_stopped_during_verify_after_the_low_verdict(repo, tmp_path, agents):
    def stop(out):
        agents.stop_at["verify"] = 2
        assert main(_args(repo, out)) == STOPPED
        state = json.loads(
            (out / "shop" / "branch-feature" / "review-state.json").read_text()
        )
        assert (
            state["verdicts"][FC.id]["status"] == "unverified"
            and len(state["verdicts"]) == 2
        )
        assert _resume(out) == 0

    _compare(repo, tmp_path, agents, rounds=1, stop=stop)


def test_s6_stopped_mid_write(repo, tmp_path, agents):
    def stop(out):
        agents.stop_at["write"] = 2
        assert main(_args(repo, out)) == STOPPED
        state = json.loads(
            (out / "shop" / "branch-feature" / "review-state.json").read_text()
        )
        assert state["write_answers"] and state["stage"] == "assess"
        assert _resume(out) == 0

    _compare(repo, tmp_path, agents, rounds=1, stop=stop)


def test_s7_stopped_after_the_last_answer_before_write_was_marked(
    repo, tmp_path, agents, monkeypatch
):
    real = dict(orchestrator.STAGE_FUNCTIONS)["write"]

    async def write_then_stop(state, ctx):
        state = await real(state, ctx)
        state.stage = "assess"  # every answer is in; the stage is not yet marked done
        raise sdk.UsageLimitError("limit", limit="session", resets="9am")

    def stop(out):
        monkeypatch.setattr(
            orchestrator,
            "STAGE_FUNCTIONS",
            tuple(
                (n, write_then_stop if n == "write" else f)
                for n, f in orchestrator.STAGE_FUNCTIONS
            ),
        )
        assert main(_args(repo, out)) == STOPPED
        monkeypatch.setattr(
            orchestrator,
            "STAGE_FUNCTIONS",
            tuple(
                (n, real if n == "write" else f)
                for n, f in orchestrator.STAGE_FUNCTIONS
            ),
        )
        before = agents.seen.get("write", 0)
        assert _resume(out) == 0
        assert agents.seen.get("write", 0) == before  # marked done with no call

    _compare(repo, tmp_path, agents, rounds=1, stop=stop)


@pytest.mark.parametrize("stage", ["verify", "write"])
def test_s11_the_same_step_stopped_twice_in_a_row(repo, tmp_path, agents, stage):
    def stop(out):
        agents.stop_at[stage] = 2
        assert main(_args(repo, out)) == STOPPED
        agents.stop_at[stage] = agents.seen[stage] + 1  # the resume's first call there
        assert _resume(out) == STOPPED
        assert _resume(out) == 0

    _compare(repo, tmp_path, agents, rounds=1, stop=stop)
