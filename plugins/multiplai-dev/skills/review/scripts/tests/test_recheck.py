"""recheck: the open set, the earlier-status gate, failed tries, and rows S1-S4 and S9-S12 of the
stop-and-resume table in INBOX/plan-review-skill-convergence-2026-10-20.md.

Every agent call is replaced. The repository is built per test with fixed dates, so its shas are
stable and two builds give the same commits.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from conftest import high_finding
from review_pipeline import budget, rounds, runlock, sdk
from review_pipeline import target as target_mod
from review_pipeline.config import ReviewConfig
from review_pipeline.export import _finding
from review_pipeline.gates import earlier_status_gate
from review_pipeline.models import (
    EARLIER_STATUSES,
    AssessOutput,
    Citation,
    EarlierStatus,
    Finding,
    MergeOutput,
    RecheckOutput,
    RepeatsOutput,
    ReviewState,
    Verdict,
)
from review_pipeline.orchestrator import RunInProgress, resume
from review_pipeline.stages import RunContext
from review_pipeline.stages.recheck import run_recheck
from review_pipeline.stages.verify import VERIFY_TRIES
from review_pipeline.state import load_state, save_state

BUILDME_MODELS = Path(__file__).resolve().parents[3] / "buildme" / "scripts" / "build_pipeline" / "models.py"

ROUND_ONE = {
    "rateplan_service.py": "KEYWORD = 'dolcebot'\n\n\ndef eligible(rate_plans):\n"
                           "    return [rp for rp in rate_plans if KEYWORD in rp['title']]\n",
    "settings.py": "A = 1\nB = 2\n",
}
# The fix commit: line 5 now lower-cases the title.
HEAD_FILES = {**ROUND_ONE, "rateplan_service.py": ROUND_ONE["rateplan_service.py"].replace(
    "rp['title']]", "rp['title'].lower()]")}
LINE_5_AT_HEAD = "    return [rp for rp in rate_plans if KEYWORD in rp['title'].lower()]"


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="T",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def build_repo(where: Path) -> tuple[Path, str, str, str]:
    """(repo, base, round-one head, head): a README, then the code round one reviewed, then a fix."""
    repo = where / "shop"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, {"README.md": "shop\n"}, "base")
    mid = _commit(repo, ROUND_ONE, "round one")
    head = _commit(repo, HEAD_FILES, "fix the title case")
    return repo, base, mid, head


def at(claim: str, file: str, line: int, quote: str) -> Finding:
    return Finding(claim=claim, severity="MEDIUM", dimension="diff-bugs", file=file, line_start=line, line_end=line,
                   failure_scenario=f"{claim}: it fails", citations=[Citation(path=file, line_start=line,
                                                                              line_end=line, quote=quote)],
                   finder="diff-bugs", finders=["diff-bugs"])


E1 = at("the title is matched without lower-casing it", "rateplan_service.py", 5,
        "    return [rp for rp in rate_plans if KEYWORD in rp['title']]")
E2 = at("the keyword is a hardcoded literal", "rateplan_service.py", 1, "KEYWORD = 'dolcebot'")
E3 = at("B is never read", "settings.py", 2, "B = 2")


def write_round(target_dir: Path, name: str, head: str, rows: list[dict], *, when: str,
                earlier: list[dict] | None = None, mode: str = "full") -> Path:
    d = target_dir / "rounds" / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": 1, "generated_at": when, "producer": "test", "target": {"head_sha": head},
            "findings": rows, "mode": mode}
    if earlier is not None:
        data["earlier"] = earlier
    (d / "findings.json").write_text(json.dumps(data))
    return d


def row(f: Finding, status: str = "confirmed", **extra) -> dict:
    return {**_finding(f, status, "r", "e"), **extra}


def decide(target_dir: Path, **by_id: str) -> None:
    path = target_dir / "viewer" / "decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({fid: {"decision": d, "note": "", "ts": "2026-01-01T00:00:00Z"}
                                for fid, d in by_id.items()}))


# --- the open set ----------------------------------------------------------------


def test_the_open_set_of_a_round_written_before_earlier_statuses_is_what_it_listed_less_rejections(tmp_path):
    low = at("not worth it", "settings.py", 1, "A = 1")
    refuted = at("refuted", "settings.py", 2, "B = 2")
    write_round(tmp_path, f"01-{'1' * 12}", "1" * 40, [row(E1)], when="2026-01-01T00:00:00Z")
    write_round(tmp_path, f"02-{'2' * 12}", "2" * 40,
                [row(E2), row(E3), row(refuted, "refuted"),
                 row(low, assessment={"label": "low-value", "reason": "[covered]"})], when="2026-01-02T00:00:00Z")
    decide(tmp_path, **{E3.id: "reject"})
    found = rounds.open_findings(tmp_path)
    # Only the latest round counts; E1 of round one is not carried without an `earlier` entry.
    assert [(o.id, o.head_sha, o.lineage) for o in found] == [(E2.id, "2" * 40, E2.id)]


def test_the_open_set_adds_the_rounds_own_earlier_entries_that_are_not_resolved(tmp_path):
    write_round(tmp_path, f"01-{'1' * 12}", "1" * 40, [row(E1), row(E2), row(E3)], when="2026-01-01T00:00:00Z")
    reworded = at("E1 again, in other words", "rateplan_service.py", 5,
                  "    return [rp for rp in rate_plans if KEYWORD in rp['title']]")

    def entry(f: Finding, status: str) -> dict:
        return {"earlier_id": f.id, "lineage": f.id, "round": "1" * 40, "status": status, "reason": "r",
                "citations": [], "error": ""}

    write_round(tmp_path, f"02-{'2' * 12}", "2" * 40, [row(reworded, lineage=E1.id)],
                earlier=[entry(E1, "still_present"), entry(E2, "resolved"), entry(E3, "cannot_verify")],
                when="2026-01-02T00:00:00Z")
    found = {o.id: o for o in rounds.open_findings(tmp_path)}
    assert list(found) == [reworded.id, E1.id, E3.id]
    assert found[reworded.id].lineage == E1.id and found[reworded.id].head_sha == "2" * 40
    # An entry carried from an earlier round keeps that round's head and finding row.
    assert found[E1.id].head_sha == "1" * 40 and found[E1.id].finding["claim"] == E1.claim
    decide(tmp_path, **{E3.id: "reject"})
    assert [o.id for o in rounds.open_findings(tmp_path)] == [reworded.id, E1.id]


def test_an_earlier_entry_listed_again_counts_once_as_listed(tmp_path):
    write_round(tmp_path, f"01-{'1' * 12}", "1" * 40, [row(E1)], when="2026-01-01T00:00:00Z")
    write_round(tmp_path, f"02-{'2' * 12}", "2" * 40, [row(E1)], when="2026-01-02T00:00:00Z",
                earlier=[{"earlier_id": E1.id, "lineage": E1.id, "round": "1" * 40, "status": "still_present",
                          "reason": "", "citations": [], "error": ""}])
    assert [(o.id, o.head_sha) for o in rounds.open_findings(tmp_path)] == [(E1.id, "2" * 40)]


def test_the_open_set_reads_the_mode_its_round_ran_in(tmp_path):
    hygiene = row(E2, impact="hygiene")
    write_round(tmp_path, f"01-{'1' * 12}", "1" * 40, [row(E1, impact="breaks-users"), hygiene],
                when="2026-01-01T00:00:00Z", mode="critical")
    assert [o.id for o in rounds.open_findings(tmp_path)] == [E1.id]


# --- the gate ------------------------------------------------------------------------


@pytest.fixture
def shop(tmp_path):
    repo, base, mid, head = build_repo(tmp_path / "a")
    resolved = target_mod.resolve(repo, range_=f"{base}..{head}")
    info = target_mod.build_target(resolved)
    target_dir = tmp_path / "a" / "out" / "shop" / "range"
    target_dir.mkdir(parents=True)
    info = target_mod.write_target_files(info, target_mod.diff_text(repo, base, head), target_dir)
    return info, target_dir, mid


def status(s: str, *citations: Citation) -> EarlierStatus:
    return EarlierStatus(earlier_id=E1.id, lineage=E1.id, round="1" * 40, status=s, reason="r",
                         citations=list(citations))


GOOD = Citation(path="rateplan_service.py", line_start=5, line_end=5, quote=LINE_5_AT_HEAD)


@pytest.mark.parametrize("s", ["resolved", "still_present", "partially_resolved"])
def test_the_gate_turns_an_uncited_status_into_cannot_verify(shop, s):
    info, _, _ = shop
    result = earlier_status_gate(info, status(s))
    assert not result.passed and "status without citing" in result.reason
    stale = Citation(path="rateplan_service.py", line_start=5, line_end=5, quote=E1.citations[0].quote)
    assert not earlier_status_gate(info, status(s, stale)).passed  # the line as it was, not as it is
    web = Citation(path="https://example.com/doc", line_start=1, line_end=1, quote="x")
    assert not earlier_status_gate(info, status(s, web)).passed
    assert earlier_status_gate(info, status(s, web, GOOD)).passed


def test_cannot_verify_needs_no_citation(shop):
    assert earlier_status_gate(shop[0], status("cannot_verify")).passed


# --- the stage -------------------------------------------------------------------------


class Agents:
    """Answers the recheck by earlier id, and the later stages with empty answers.

    `stop_at` raises a budget stop on that recheck call (1-based); every prompt is kept by earlier id."""

    def __init__(self, answers: dict | None = None, stop_at: int | None = None):
        self.answers = answers or {}
        self.stop_at = stop_at
        self.calls: list[tuple[str, str]] = []
        self.prompts: dict[str, str] = {}
        self.tools: list[list[str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", allowed_tools=(), **kwargs):
        if budget_label != "recheck":
            self.calls.append((budget_label, ""))
            outputs = {"assess": AssessOutput, "repeats": RepeatsOutput, "merge": MergeOutput}
            if budget_label in outputs:
                return outputs[budget_label]()
            raise AssertionError(budget_label)
        await asyncio.sleep(0)  # a real call awaits I/O, where a budget stop's cancel lands
        fid = re.search(r'source="earlier finding ([0-9a-f]{10})"', prompt).group(1)
        self.calls.append(("recheck", fid))
        self.tools.append(list(allowed_tools))
        if sum(1 for label, _ in self.calls if label == "recheck") == self.stop_at:
            raise budget.BudgetExceededError("circuit breaker stopped the run during recheck")
        self.prompts[fid] = prompt
        answer = self.answers.get(fid, RecheckOutput(status="still_present", reason=f"{fid} is still there",
                                                     citations=[GOOD]))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def asked(self) -> list[str]:
        return [fid for label, fid in self.calls if label == "recheck"]


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> Agents:
        fake = Agents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def this_round(info) -> ReviewState:
    f = high_finding()
    return ReviewState(target=info, stage="verify", findings=[f],
                       verdicts={f.id: Verdict(finding_id=f.id, status="confirmed", reason="r", topic="code",
                                               impact="breaks-users", expected_behaviour="e")})


def ctx_for(target_dir: Path, info) -> RunContext:
    snapshot = target_mod.snapshot_head(info, target_dir / "tree")
    return RunContext(config=ReviewConfig(concurrency=1), snapshot=snapshot, diff="", target_dir=target_dir)


def kept_round_one(target_dir: Path, mid: str) -> None:
    write_round(target_dir, f"01-{mid[:12]}", mid, [row(E1), row(E2), row(E3)], when="2026-01-01T00:00:00Z")


async def test_one_agent_per_open_finding_sees_the_earlier_finding_and_the_diff_since_its_round(shop, agents):
    info, target_dir, mid = shop
    kept_round_one(target_dir, mid)
    fake = agents()
    state = await run_recheck(this_round(info), ctx_for(target_dir, info))
    assert fake.asked() == [E1.id, E2.id, E3.id] and state.stage == "recheck"
    assert fake.tools == [["Read", "Grep", "Glob", "WebFetch", "WebSearch"]] * 3  # the verifier's tools
    prompt = fake.prompts[E1.id]
    assert E1.claim in prompt and mid in prompt
    assert "+    return [rp for rp in rate_plans if KEYWORD in rp['title'].lower()]" in prompt  # the fix
    assert high_finding().claim not in prompt  # never this round's findings
    assert "settings.py" not in fake.prompts[E1.id].split("The paths it cites:")[1].split("What changed")[0]
    assert {k: v.status for k, v in state.recheck_answers.items()} == dict.fromkeys([E1.id, E2.id, E3.id],
                                                                                  "still_present")
    assert [(g.finding_id, g.gate, g.passed) for g in state.gate_checks] == [
        (E1.id, "earlier_status_gate", True), (E2.id, "earlier_status_gate", True),
        (E3.id, "earlier_status_gate", True)]
    import jsonschema
    from conftest import SCHEMA
    from review_pipeline.export import to_checks_file

    checks = to_checks_file(state)
    jsonschema.validate(checks, json.loads((SCHEMA.parent / "checks.v1.schema.json").read_text()))
    assert [(a["stage"], a["subject"], a["outcome"]) for a in checks["agents"]] == [
        ("recheck", E1.id, "still_present"), ("recheck", E2.id, "still_present"), ("recheck", E3.id, "still_present")]


async def test_an_uncited_resolved_answer_is_recorded_as_cannot_verify(shop, agents):
    info, target_dir, mid = shop
    kept_round_one(target_dir, mid)
    agents(answers={E1.id: RecheckOutput(status="resolved", reason="it lower-cases now")})
    state = await run_recheck(this_round(info), ctx_for(target_dir, info))
    answer = state.recheck_answers[E1.id]
    assert answer.status == "cannot_verify" and "status without citing" in answer.reason
    assert answer.reason.endswith("The recheck said: it lower-cases now")
    gate = next(g for g in state.gate_checks if g.finding_id == E1.id)
    assert (gate.passed, gate.rule) == (False, "status without citing")


async def test_failed_tries_give_cannot_verify_with_an_error_and_the_run_goes_on(shop, agents):
    info, target_dir, mid = shop
    kept_round_one(target_dir, mid)
    fake = agents(answers={E2.id: sdk.AgentCallError("the agent timed out")})
    state = await run_recheck(this_round(info), ctx_for(target_dir, info))
    assert fake.asked().count(E2.id) == VERIFY_TRIES
    answer = state.recheck_answers[E2.id]
    assert answer.status == "cannot_verify" and "timed out" in answer.error
    assert state.stage == "recheck" and state.recheck_answers[E1.id].status == "still_present"
    assert any(line.startswith(f"recheck: {E2.id}: ") for line in state.errors)
    check = next(c for c in state.checks if c.subject == E2.id)
    assert check.stage == "recheck" and check.outcome.startswith("failed")


def test_an_unknown_status_word_is_cannot_verify_never_resolved():
    assert RecheckOutput.model_validate({"status": "fixed"}).status == "cannot_verify"
    assert RecheckOutput.model_validate({"status": "Still present"}).status == "still_present"


async def test_the_stage_is_skipped_with_no_open_finding_and_on_a_tree(shop, agents):
    info, target_dir, mid = shop
    fake = agents()
    state = await run_recheck(this_round(info), ctx_for(target_dir, info))
    assert fake.calls == [] and state.stage == "recheck" and state.recheck_answers == {}
    kept_round_one(target_dir, mid)
    tree = this_round(info.model_copy(update={"kind": "tree", "base_sha": target_mod.EMPTY_TREE}))
    state = await run_recheck(tree, ctx_for(target_dir, info))
    assert fake.calls == [] and state.stage == "recheck"


def test_the_status_words_match_buildmes_prior_statuses():
    """buildme keeps the same words best first (`PRIOR_STATUSES`); this package keeps them worst first."""
    tree = ast.parse(BUILDME_MODELS.read_text(encoding="utf-8"))
    (words,) = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "PRIOR_STATUSES" for t in n.targets)]
    assert EARLIER_STATUSES == tuple(reversed(words))


# --- stop and resume: rows S1-S4 and S9-S12 -------------------------------------------------


def scenario(where: Path, *, extra_round: bool = False) -> tuple[Path, Path, ReviewState]:
    """A repository, round one kept with three findings, and this round saved after verify."""
    repo, base, mid, head = build_repo(where)
    resolved = target_mod.resolve(repo, range_=f"{base}..{head}")
    target_dir = where / "out" / "shop" / f"range-{base}..{head}"
    target_dir.mkdir(parents=True)
    info = target_mod.write_target_files(target_mod.build_target(resolved),
                                         target_mod.diff_text(repo, base, head), target_dir)
    kept_round_one(target_dir, mid)
    if extra_round:  # S4: a later run that stopped before export, kept without findings.json
        d = target_dir / "rounds" / f"02-{head[:12]}"
        d.mkdir()
        (d / "review-state.json").write_text("{}")
    state = this_round(info)
    save_state(state, target_dir)
    return repo, target_dir, state


CONFIG = ReviewConfig(concurrency=1, deps=False)


def outcome(target_dir: Path) -> tuple:
    """What a finished run leaves about earlier rounds: the exported statuses and lineage, and the answers."""
    data = json.loads((target_dir / "findings.json").read_text())
    state = load_state(target_dir / "review-state.json")
    return (data.get("earlier"), [(f["id"], f.get("lineage")) for f in data["findings"]],
            {k: v.model_dump() for k, v in state.recheck_answers.items()}, state.stage)


async def never_stopped(where: Path, monkeypatch, **answers) -> tuple[tuple, Agents]:
    _, target_dir, _ = scenario(where)
    fake = Agents(answers=answers)
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    await resume(target_dir, CONFIG)
    result = outcome(target_dir)
    assert [e["status"] for e in result[0]] == ["still_present"] * 3  # never compare two empty results
    return result, fake


async def stopped_at_s1(where: Path, agents, **answers) -> tuple[Path, Path]:
    """A budget stop on the second recheck call: one answer stored, stage still `verify`."""
    repo, target_dir, _ = scenario(where)
    agents(answers=answers, stop_at=2)
    with pytest.raises(budget.BudgetExceededError):
        await resume(target_dir, CONFIG)
    saved = load_state(target_dir / "review-state.json")
    assert saved.stage == "verify" and list(saved.recheck_answers) == [E1.id]
    assert [c.subject for c in saved.checks if c.stage == "recheck"] == [E1.id]
    return repo, target_dir


async def test_earlier_status_resume_s1_asks_only_the_findings_with_no_stored_answer(tmp_path, agents,
                                                                                     monkeypatch):
    expected, _ = await never_stopped(tmp_path / "ref", monkeypatch)
    _, target_dir = await stopped_at_s1(tmp_path / "run", agents)
    fake = agents()
    await resume(target_dir, CONFIG)
    assert fake.asked() == [E2.id, E3.id]
    assert outcome(target_dir) == expected


async def test_earlier_status_resume_s2_skips_a_finished_recheck(tmp_path, agents, monkeypatch):
    expected, _ = await never_stopped(tmp_path / "ref", monkeypatch)
    _, target_dir, state = scenario(tmp_path / "run")
    agents()
    state = await run_recheck(state, ctx_for(target_dir, state.target))
    save_state(state, target_dir)
    fake = agents()
    await resume(target_dir, CONFIG)
    assert fake.asked() == [] and ("assess", "") in fake.calls  # merge and the rest ran as before
    assert outcome(target_dir) == expected


@pytest.mark.parametrize("stage", ["merge", "repeats", "assess"])
async def test_earlier_status_resume_s3_a_checkpoint_from_before_the_stage_gives_cannot_verify(tmp_path, agents,
                                                                                              stage):
    _, target_dir, state = scenario(tmp_path)
    old = json.loads(state.model_dump_json())
    for key in ("recheck_answers", "earlier_status", "lineage"):
        del old[key]
    old["stage"] = stage
    (target_dir / "review-state.json").write_text(json.dumps(old))
    fake = agents()
    await resume(target_dir, CONFIG)
    assert fake.asked() == []
    earlier, _, answers, done = outcome(target_dir)
    assert answers == {} and done == "done"
    assert [(e["earlier_id"], e["status"], e["reason"]) for e in earlier] == [
        (E1.id, "cannot_verify", "not rechecked"), (E2.id, "cannot_verify", "not rechecked"),
        (E3.id, "cannot_verify", "not rechecked")]


async def test_earlier_status_resume_s4_a_kept_round_without_findings_json_is_passed_over(tmp_path, agents,
                                                                                         monkeypatch):
    expected, _ = await never_stopped(tmp_path / "ref", monkeypatch)
    _, target_dir, _ = scenario(tmp_path / "run", extra_round=True)
    fake = agents()
    await resume(target_dir, CONFIG)
    assert fake.asked() == [E1.id, E2.id, E3.id]
    assert outcome(target_dir) == expected


async def test_earlier_status_resume_s9_edits_in_the_worktree_change_nothing(tmp_path, agents, monkeypatch):
    expected, reference = await never_stopped(tmp_path / "ref", monkeypatch)
    repo, target_dir = await stopped_at_s1(tmp_path / "run", agents)
    (repo / "rateplan_service.py").write_text("KEYWORD = 'edited'\n")
    (repo / "untracked.py").write_text("X = 1\n")
    fake = agents()
    await resume(target_dir, CONFIG)
    assert outcome(target_dir) == expected
    assert fake.prompts[E2.id] == reference.prompts[E2.id]  # the same diff and head, read from commits


async def test_earlier_status_resume_s10_a_moved_branch_changes_nothing(tmp_path, agents, monkeypatch):
    expected, reference = await never_stopped(tmp_path / "ref", monkeypatch)
    repo, target_dir = await stopped_at_s1(tmp_path / "run", agents)
    _commit(repo, {"rateplan_service.py": "KEYWORD = 'moved on'\n"}, "a later commit")
    fake = agents()
    await resume(target_dir, CONFIG)
    assert outcome(target_dir) == expected
    assert fake.prompts[E2.id] == reference.prompts[E2.id]  # the saved head, not the branch's new one


async def test_earlier_status_resume_s11_stopping_twice_keeps_the_first_answer_per_id(tmp_path, agents,
                                                                                    monkeypatch):
    expected, _ = await never_stopped(tmp_path / "ref", monkeypatch)
    _, target_dir = await stopped_at_s1(tmp_path / "run", agents)
    # The second attempt would answer E1 otherwise, but is never asked about it; it stops on E3.
    other = RecheckOutput(status="resolved", reason="other", citations=[GOOD])
    second = agents(answers={E1.id: other}, stop_at=2)
    with pytest.raises(budget.BudgetExceededError):
        await resume(target_dir, CONFIG)
    assert second.asked() == [E2.id, E3.id]
    saved = load_state(target_dir / "review-state.json")
    assert list(saved.recheck_answers) == [E1.id, E2.id]  # answers from both attempts
    third = agents(answers={E1.id: other, E2.id: other})
    await resume(target_dir, CONFIG)
    assert third.asked() == [E3.id]
    assert outcome(target_dir) == expected


async def test_earlier_status_resume_s12_a_running_review_refuses_a_resume(tmp_path, agents):
    _, target_dir = await stopped_at_s1(tmp_path, agents)
    before = (target_dir / "review-state.json").read_bytes()
    fake = agents()
    with runlock.hold(target_dir), pytest.raises(RunInProgress):
        await resume(target_dir, CONFIG)
    assert fake.calls == [] and (target_dir / "review-state.json").read_bytes() == before


# --- the finders never see earlier rounds ---------------------------------------------------


def test_find_prompt_ignores_rounds(tmp_path, monkeypatch, capsys):
    """The finder prompts are byte-identical with and without earlier rounds of the same change."""
    import paths_stub
    from review_pipeline.__main__ import main
    from test_orchestrator import FakeAgents, _review_args

    repo, base, mid, head = build_repo(tmp_path / "src")
    out = tmp_path / "out"
    target_dir = out / "shop" / f"range-{base}..{head}"

    def run(with_rounds: bool) -> tuple[list[tuple[str, str]], FakeAgents]:
        import shutil

        shutil.rmtree(out, ignore_errors=True)
        if with_rounds:
            target_dir.mkdir(parents=True)
            kept_round_one(target_dir, mid)
            decide(target_dir, **{E3.id: "reject"})
        prompts: list[tuple[str, str]] = []
        fake = FakeAgents()

        async def structured(prompt, schema, *, budget_label="", **kwargs):
            if budget_label.startswith("find"):
                prompts.append((budget_label, prompt))
            return await fake(prompt, schema, budget_label=budget_label, **kwargs)

        async def text(prompt, **kwargs):
            prompts.append(("text", prompt))
            return await paths_stub.stub_agent_call_text(prompt, **kwargs)

        monkeypatch.setattr(sdk, "agent_call_structured", structured)
        monkeypatch.setattr(sdk, "agent_call_text", text)
        assert main(_review_args(repo, base, head, out)) == 0
        return prompts, fake

    without, plain = run(False)
    with_, rerun = run(True)
    assert "recheck" not in plain.calls and rerun.calls.count("recheck") == 2  # E3 was rejected
    assert sorted(without) == sorted(with_) and without
    capsys.readouterr()
