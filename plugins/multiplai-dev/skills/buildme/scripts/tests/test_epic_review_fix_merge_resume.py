"""Findings reach the fixer, every blocker gets a decision, and open confirmed
findings never auto-merge (plan-factory-merged-2026-10-29.md, items 16-18 and
item 21's buildme part). Stop/resume rows S6-S11, and Done means 6 on PR
#39's own review (`fixtures/review-pr-39-findings.json`, trimmed from the
epic's `pr-39/findings.json` of 2026-10-29: each finding's id, severity,
status, impact, section, topic, file, line and title).

The fixer rows run the real findings loop on a fixture repo with GitHub, the
triage agent and the TDD engine's agents stubbed (`test_findings_round`); the
epic rows run the coordinator with the fake review, post and fixer first on
PATH (`test_epic_rounds.Rounds`)."""

from __future__ import annotations

import ast
import dataclasses
import json
import shutil
from pathlib import Path

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import review
from build_pipeline.findings import decisions, from_review, loop
from build_pipeline.findings.models import PRComment, Triage, Verdict

from .test_epic_rounds import Rounds, high, medium
from .test_findings_round import (
    FIX,
    FakePR,
    FakeReview,
    FakeTriage,
    _make_built,
    _run,
    _Stubs,
    _subjects,
    engine_stubs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "review-pr-39-findings.json"
REVIEW_MODELS = (
    Path(__file__).resolve().parents[3]
    / "review"
    / "scripts"
    / "review_pipeline"
    / "models.py"
)
CRASH, NAME_MATCH = "8684f487ee", "cd83b4ff3d"


@pytest.fixture
def fake_pr(monkeypatch):
    """`test_findings_round.fake_pr`: one PR on a fake GitHub."""
    pr = FakePR()
    monkeypatch.setattr(loop.git_ops, "_run_gh", pr.gh)
    monkeypatch.setattr(loop.git_ops, "push_branch", pr.push)
    monkeypatch.setattr(loop.git_ops, "_run_pr_skill", pr.pr_skill)
    return pr


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _review_39(tmp_path: Path) -> Path:
    d = tmp_path / "pr-39"
    d.mkdir()
    shutil.copy(FIXTURE, d / "findings.json")
    return d


# --- Done means 6: the merge rule on #39's review ---------------------------------


def test_done_means_6_39s_two_findings_park_at_the_round_cap(tmp_path):
    found = review.blocking_findings(_review_39(tmp_path))
    assert [f["id"] for f in found] == [CRASH, NAME_MATCH]
    assert {f["impact"] for f in found} == {"correctness-only"}
    for worth in ("breaks", "any"):
        got, why = review.decide(found, 3, 3, worth)
        assert got == "park" and CRASH in why and NAME_MATCH in why
    assert review.open_at_cap(found) == [CRASH, NAME_MATCH]


def test_done_means_6_the_crash_rated_depends_on_data_earns_a_round(tmp_path):
    [crash] = [
        f for f in review.blocking_findings(_review_39(tmp_path)) if f["id"] == CRASH
    ]
    rated = {**crash, "impact": "depends-on-data"}
    got, why = review.decide([rated], 1, 3, "breaks")
    assert got == "round" and "depends-on-data" in why
    # `--mode critical` lists it, as the review skill does.
    assert decisions._listed(rated, "critical")


def _review_critical_impacts() -> tuple[str, ...]:
    """`CRITICAL_IMPACTS` in the review skill's models.py, read as source:
    buildme never imports review_pipeline."""
    tree = ast.parse(REVIEW_MODELS.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for node in tree.body:
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            else node.target
            if isinstance(node, ast.AnnAssign)
            else None
        )
        if not isinstance(target, ast.Name) or node.value is None:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            names[target.id] = node.value.value
        if target.id == "CRITICAL_IMPACTS":
            return tuple(
                e.value if isinstance(e, ast.Constant) else names[e.id]
                for e in node.value.elts
            )
    raise AssertionError(f"no CRITICAL_IMPACTS in {REVIEW_MODELS}")


def test_buildmes_critical_impacts_match_the_review_skills():
    """Item 21: `findings.decisions._CRITICAL_IMPACTS` is a copy of the
    review skill's `CRITICAL_IMPACTS`, which PR #466 extends with
    `depends-on-data`."""
    theirs = _review_critical_impacts()
    if "depends-on-data" not in theirs:
        pytest.skip(
            "the review skill's CRITICAL_IMPACTS has no depends-on-data yet: "
            "it arrives with PR #466 (feat/review-verified-severity)"
        )
    assert decisions._CRITICAL_IMPACTS == theirs


# --- the fixer with --from-review (rows S6-S9) ------------------------------------

A, B = "aa11aa11aa", "bb22bb22bb"
NOT_A_DEFECT = Triage(verdict=Verdict.NOT_DEFECT, reason="it reads correctly")


def _finding(fid: str, claim: str) -> dict:
    return {
        "id": fid,
        "severity": "MEDIUM",
        "status": "confirmed",
        "impact": "correctness-only",
        "section": "code",
        "file": "calc.py",
        "line_start": 2,
        "claim": claim,
    }


def _write_review(d: Path, head: str, findings: list[dict]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    doc = {"mode": "full", "target": {"head_sha": head}, "findings": findings}
    (d / "findings.json").write_text(json.dumps(doc))
    return d


def _bot_thread(pr, body: str, fid: str) -> int:
    """A finding the review posted as its GitHub App."""
    cid = pr.add_thread("dolceclaude", body, fid=fid)
    node = pr.threads[-1]["comments"]["nodes"][0]
    node["author"] = {"login": "dolceclaude", "__typename": "Bot"}
    return cid


class Fixer:
    """One PR with two blockers the review posted as its App (`A` a code fix,
    `B` not a defect) and a top-level comment from a person who may only read;
    the fixer runs as the epic runs it: one round, `--severities HIGH,MEDIUM
    --code-only --from-review <dir>`."""

    def __init__(self, base: Path, pr, monkeypatch, *, ids=(A, B)):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        self.repo, self.change, config = _make_built(base)
        config.findings = dataclasses.replace(config.findings, max_rounds=1)
        self.config, self.pr = config, pr
        self.dir = _write_review(
            base / "reviews" / "pr-7",
            "h1",
            [_finding(A, "add subtracts"), _finding(B, "naming is odd")][: len(ids)],
        )
        self.threads = {
            A: _bot_thread(pr, "add subtracts", A),
            B: _bot_thread(pr, "naming is odd", B),
        }
        pr.add_issue("bob", "Please also handle floats")
        self.triage = FakeTriage({"add subtracts": FIX, "naming is odd": NOT_A_DEFECT})
        self.review = FakeReview(pr)

    def run(self, *, from_review=True):
        run, lines = _run(
            self.config, self.change, triage_call=self.triage, review=self.review
        )
        run.severities, run.code_only = ("HIGH", "MEDIUM"), True
        run.from_review = self.dir if from_review else None
        return run, lines

    def state(self) -> dict:
        doc = json.loads((self.change / loop.STATE_FILE).read_text())
        return doc["findings"]["rounds"][-1]

    def impl_commits(self) -> list[str]:
        return [
            s
            for s in _subjects(self.repo)
            if s.startswith("impl(") and "Fix finding" in s
        ]


@pytest.mark.asyncio
async def test_s6_the_fixer_decides_each_posted_blocker_whoever_posted_it(
    tmp_path, fake_pr, monkeypatch
):
    """S6: after the review's `post` and before any round, the fixer runs with
    `--from-review`. It acts on both App comments, replies under each, and
    still leaves the reader's comment alone (`findings.authors`)."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert lines[-1] == "FINDINGS:MAX_ROUNDS:1"
    assert sorted(r[1] for r in fake_pr.replies()) == sorted(f.threads.values())
    assert len(f.triage.prompts) == 2
    assert not any("handle floats" in p for p in f.triage.prompts)
    rnd = f.state()
    assert (rnd["review_head"], rnd["expected"]) == ("h1", [A, B])
    fixed = rnd["decisions"][A]
    assert fixed["decision"] == from_review.FIXED
    [reply] = [r[2] for r in fake_pr.replies() if r[1] == f.threads[A]]
    assert fixed["commit"][:12] in reply
    assert rnd["decisions"][B] == {
        "decision": from_review.NOT_DEFECT,
        "reason": "it reads correctly",
    }


@pytest.mark.asyncio
async def test_s6_without_from_review_the_apps_comments_are_not_acted_on(
    tmp_path, fake_pr, monkeypatch
):
    """The 10-29 fixer: `dolceclaude` is not allowed, so nothing is done."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    run, lines = f.run(from_review=False)
    assert await loop.run_findings(run) == 0
    assert lines[-1] == "FINDINGS:DONE:0" and f.triage.prompts == []


async def _stop_at_the_push(f: Fixer, monkeypatch) -> list[str]:
    """Round 1 stops after its fix commit, before the push."""
    real = f.pr.push
    calls = []

    def push(worktree, branch):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return real(worktree, branch)

    monkeypatch.setattr(loop.git_ops, "push_branch", push)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    assert lines[-1].startswith("FINDINGS:FAILED:publish:")
    assert f.pr.replies() == [] and len(f.impl_commits()) == 1
    return lines


@pytest.mark.asyncio
async def test_s7_a_fixer_stopped_before_its_push_finishes_the_round_once(
    tmp_path, fake_pr, monkeypatch
):
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    await _stop_at_the_push(f, monkeypatch)
    saved = f.state()["decisions"]
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert lines[-1] == "FINDINGS:MAX_ROUNDS:1"
    assert len([e for e in fake_pr.events if e[0] == "push"]) == 1
    assert sorted(r[1] for r in fake_pr.replies()) == sorted(f.threads.values())
    assert len(f.impl_commits()) == 1 and len(f.triage.prompts) == 2
    assert f.state()["decisions"] == saved and set(saved) == {A, B}

    # Equal to a fixer that never stopped.
    g = Fixer(tmp_path / "plain", fake_pr.__class__(), monkeypatch)
    monkeypatch.setattr(loop.git_ops, "_run_gh", g.pr.gh)
    monkeypatch.setattr(loop.git_ops, "push_branch", g.pr.push)
    monkeypatch.setattr(loop.git_ops, "_run_pr_skill", g.pr.pr_skill)
    run, lines = g.run()
    with _Stubs(engine_stubs(g.repo)):
        assert await loop.run_findings(run) == 0, lines
    plain = g.state()["decisions"]
    assert {k: v["decision"] for k, v in plain.items()} == {
        k: v["decision"] for k, v in saved.items()
    }


@pytest.mark.asyncio
async def test_s8_a_newer_review_replaces_the_ids_and_keeps_what_was_fixed(
    tmp_path, fake_pr, monkeypatch
):
    """A newer review replaced the review dir while the round was open: the
    resume prints `FINDINGS:REVIEW_CHANGED`, decides the newer review's ids,
    and does not fix `A` again. `C`, which the newer review adds and nobody
    posted in this round, has no decision, so the fixer ends
    `FINDINGS:INGESTION_ERROR:C`."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    await _stop_at_the_push(f, monkeypatch)
    fixed = f.state()["decisions"][A]
    c = "cc33cc33cc"
    _write_review(f.dir, "h2", [_finding(A, "add subtracts"), _finding(c, "new")])
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    assert "FINDINGS:REVIEW_CHANGED:h1→h2" in lines
    assert lines[-1] == f"FINDINGS:INGESTION_ERROR:{c}"
    rnd = f.state()
    assert (rnd["review_head"], rnd["expected"]) == ("h2", [A, c])
    assert rnd["decisions"][A] == fixed and len(f.impl_commits()) == 1
    assert len([e for e in fake_pr.events if e[0] == "push"]) == 1


@pytest.mark.asyncio
async def test_s9_a_resume_decides_only_the_ids_with_no_decision(
    tmp_path, fake_pr, monkeypatch
):
    """Stopped in the code step: `B` (not a defect) was decided after triage,
    `A` waits on its fix. The resume keeps `B`'s decision and decides `A`."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    real = loop.code.run_code_step
    calls = []

    async def stops_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return await real(*a, **k)

    monkeypatch.setattr(loop.code, "run_code_step", stops_once)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    first = f.state()["decisions"]
    assert list(first) == [B]
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    after = f.state()["decisions"]
    assert after[B] == first[B] and after[A]["decision"] == from_review.FIXED
    assert len(f.triage.prompts) == 2


@pytest.mark.asyncio
async def test_s9_a_blocker_nobody_posted_ends_ingestion_error(
    tmp_path, fake_pr, monkeypatch
):
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    d = "dd44dd44dd"
    _write_review(f.dir, "h1", [_finding(A, "add subtracts"), _finding(d, "unposted")])
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    assert lines[-1] == f"FINDINGS:INGESTION_ERROR:{d}"
    assert list(f.state()["decisions"]) == [A]


@pytest.mark.asyncio
async def test_s9_a_review_with_no_blockers_still_ends_done_0(
    tmp_path, fake_pr, monkeypatch
):
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    _write_review(f.dir, "h1", [{**_finding(A, "x"), "severity": "LOW"}])
    fake_pr.threads.clear()
    run, lines = f.run()
    assert await loop.run_findings(run) == 0
    assert lines[-1] == "FINDINGS:DONE:0"


# --- lineage: a re-review's new id answers through the earlier comment ------------

NEW, NEWER = "ee55ee55ee", "ff66ff66ff"


def _still_open(fid: str, earlier: str, claim: str = "add subtracts", **over) -> dict:
    """A finding a re-review gave a new id: `drop_posted` posts no comment for it."""
    return {
        **_finding(fid, claim),
        "lineage": earlier,
        "assessment": {"label": "still-open", "earlier_id": earlier},
        **over,
    }


def _kept(d: Path, name: str, doc: dict) -> None:
    """A review the next one kept in `rounds/`."""
    (d / "rounds" / name).mkdir(parents=True, exist_ok=True)
    (d / "rounds" / name / "findings.json").write_text(json.dumps(doc))


@pytest.mark.asyncio
async def test_still_open_blocker_uses_the_earlier_comment(
    tmp_path, fake_pr, monkeypatch
):
    """Done means 1: `NEW` has no comment; `A`'s thread answers it, once."""
    f = Fixer(tmp_path, fake_pr, monkeypatch, ids=(A,))
    _write_review(f.dir, "h1", [_still_open(NEW, A)])
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert lines[-1] in ("FINDINGS:MAX_ROUNDS:1", "FINDINGS:DONE:1")
    rnd = f.state()
    assert rnd["comment_for"] == {NEW: A}
    assert rnd["decisions"][NEW]["decision"] == from_review.FIXED
    assert [r[1] for r in fake_pr.replies()] == [f.threads[A]]


@pytest.mark.asyncio
async def test_still_open_chain_links_across_three_reviews(
    tmp_path, fake_pr, monkeypatch
):
    """Done means 2: `NEWER`'s `earlier_id`, `NEW`, was itself never posted;
    `lineage` reaches `A`'s comment."""
    f = Fixer(tmp_path, fake_pr, monkeypatch, ids=(A,))
    _kept(
        f.dir,
        "01-h0",
        {"mode": "full", "generated_at": "2026-01-01T00:00:00Z", "findings": []},
    )
    _kept(
        f.dir,
        "02-h1",
        {
            "mode": "full",
            "generated_at": "2026-01-02T00:00:00Z",
            "findings": [_still_open(NEW, A)],
        },
    )
    _write_review(
        f.dir,
        "h2",
        [{**_still_open(NEWER, NEW), "lineage": A}],
    )
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    rnd = f.state()
    assert rnd["comment_for"] == {NEWER: A}
    assert rnd["decisions"][NEWER]["decision"] == from_review.FIXED
    assert [r[1] for r in fake_pr.replies()] == [f.threads[A]]


def test_link_comments_prefers_the_same_id_then_the_newest_of_the_lineage():
    def comment(cid: int, fid: str, created: str) -> PRComment:
        return PRComment(
            id=cid, kind="review", author="b", finding_id=fid, created_at=created
        )

    old, later = comment(1, A, "2026-01-01"), comment(2, B, "2026-01-02")
    same = comment(3, NEW, "2026-01-03")
    ids = {NEW: A, B: A, NEWER: A}
    assert from_review.link_comments([NEWER], [old, later], ids) == {NEWER: B}
    assert from_review.link_comments([NEW], [old, same, later], ids) == {NEW: NEW}
    assert from_review.link_comments(["zz"], [old, later], ids) == {}


@pytest.mark.asyncio
async def test_lineage_s2_a_stop_after_the_fetch_triages_the_saved_link_with_no_fetch(
    tmp_path, fake_pr, monkeypatch
):
    """Row S2: a fetch that saved its round and stopped; the resume reads no
    comments and triages what was saved. Compared with a fixer that never stopped."""
    f = Fixer(tmp_path, fake_pr, monkeypatch, ids=(A,))
    _write_review(f.dir, "h1", [_still_open(NEW, A)])
    real = loop.run_round
    calls = []

    async def stops_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return await real(*a, **k)

    monkeypatch.setattr(loop, "run_round", stops_once)
    run, lines = f.run()
    with pytest.raises(RuntimeError), _Stubs(engine_stubs(f.repo)):
        await loop.run_findings(run)
    saved = f.state()
    assert saved["comment_for"] == {NEW: A} and saved["finished"] == ["fetch"]

    def no_fetch(*a, **k):
        raise AssertionError("a second fetch")

    monkeypatch.setattr(loop.fetch, "fetch_comments", no_fetch)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    after = f.state()
    assert after["comment_for"] == {NEW: A}
    assert after["decisions"][NEW]["decision"] == from_review.FIXED
    assert [r[1] for r in fake_pr.replies()] == [f.threads[A]]
    assert len(f.impl_commits()) == 1


@pytest.mark.asyncio
async def test_lineage_s3_a_stop_in_code_saves_the_decision_under_the_expected_id(
    tmp_path, fake_pr, monkeypatch
):
    """Row S3: stopped in the code step of a round linked by lineage."""
    f = Fixer(tmp_path, fake_pr, monkeypatch, ids=(A,))
    _write_review(f.dir, "h1", [_still_open(NEW, A)])
    real = loop.code.run_code_step
    calls = []

    async def stops_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return await real(*a, **k)

    monkeypatch.setattr(loop.code, "run_code_step", stops_once)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    assert f.state()["comment_for"] == {NEW: A} and f.state()["decisions"] == {}
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert f.state()["decisions"][NEW]["decision"] == from_review.FIXED
    assert len(f.triage.prompts) == 1 and len(f.impl_commits()) == 1


@pytest.mark.asyncio
async def test_lineage_s6_a_checkpoint_with_no_comment_for_matches_by_exact_id(
    tmp_path, fake_pr, monkeypatch
):
    """Row S6: a round saved before `comment_for` existed."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    real = loop.code.run_code_step
    calls = []

    async def stops_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return await real(*a, **k)

    monkeypatch.setattr(loop.code, "run_code_step", stops_once)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    path = f.change / loop.STATE_FILE
    doc = json.loads(path.read_text())
    del doc["findings"]["rounds"][-1]["comment_for"]
    path.write_text(json.dumps(doc))
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    rnd = f.state()
    assert rnd["comment_for"] == {}
    assert rnd["decisions"][A]["decision"] == from_review.FIXED


@pytest.mark.asyncio
async def test_lineage_s7_a_blocker_whose_only_thread_is_resolved_ends_ingestion_error(
    tmp_path, fake_pr, monkeypatch
):
    """Row S7: no comment in its lineage, as today."""
    f = Fixer(tmp_path, fake_pr, monkeypatch, ids=(A,))
    _write_review(f.dir, "h1", [_still_open(NEW, A)])
    for t in fake_pr.threads:
        t["isResolved"] = True
    run, lines = f.run()
    assert await loop.run_findings(run) == 1, lines
    assert lines[-1] == f"FINDINGS:INGESTION_ERROR:{NEW}"


# --- a round resumed under a newer review -----------------------------------------

PR91 = Path(__file__).parent / "fixtures" / "review-pr-91-rounds"
ROUND2, ROUND3 = PR91 / "02-18db26975f64", PR91 / "03-18db26975f64"
PR91_MEDIUMS = ["08672d4746", "c1541b6daf", "d62907b020"]


def _head(repo: Path) -> str:
    return loop.git_ops.rev_parse_head(repo)


def _reviewed_at(d: Path, kept: Path, head: str) -> None:
    """The saved review *kept* as `<d>/findings.json`, at *head*."""
    doc = json.loads((kept / "findings.json").read_text())
    doc["target"]["head_sha"] = head
    (d / "findings.json").write_text(json.dumps(doc))


def _two_rounds(f: Fixer) -> None:
    f.config.findings = dataclasses.replace(f.config.findings, max_rounds=2)


@pytest.mark.asyncio
async def test_pr91_replay(tmp_path, fake_pr, monkeypatch):
    """Done means 3: round 1's publish never finished and review 156 replaced
    the review at the head it pushed. The resume runs no review, and round 2
    triages the three posted MEDIUMs."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    _two_rounds(f)
    await _stop_at_the_push(f, monkeypatch)
    for fid in PR91_MEDIUMS:
        _bot_thread(fake_pr, f"medium {fid}", fid)
    _reviewed_at(f.dir, ROUND2, _head(f.repo))
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert any(ln.startswith(from_review.REVIEW_CHANGED) for ln in lines)
    doc = json.loads((f.change / loop.STATE_FILE).read_text())
    first, second = doc["findings"]["rounds"]
    assert first["review_run_dir"] == str(f.dir) and first["review_finished_at"]
    assert sorted(c["finding_id"] for c in second["comments"]) == sorted(PR91_MEDIUMS)
    assert set(second["decisions"]) == set(PR91_MEDIUMS)
    # The one review is round 2's own, of the head its records commit made.
    assert f.review.calls == 1 and f.review.posts == ["/reviews/r2"]


@pytest.mark.asyncio
async def test_lineage_s1_a_resumed_round_finishes_and_keeps_what_it_decided(
    tmp_path, fake_pr, monkeypatch
):
    """Row S1: the next round acts on the newer review's blockers, less what
    this review's round already decided. Compared with a fixer that never
    stopped, `A` is fixed once and its thread has one reply."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    _two_rounds(f)
    await _stop_at_the_push(f, monkeypatch)
    c = "cc33cc33cc"
    _bot_thread(fake_pr, "naming is odd too", c)
    _write_review(
        f.dir,
        _head(f.repo),
        [_finding(A, "add subtracts"), _finding(c, "naming is odd too")],
    )
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    doc = json.loads((f.change / loop.STATE_FILE).read_text())
    first, second = doc["findings"]["rounds"]
    assert first["review_run_dir"] == str(f.dir)
    assert second["expected"] == [c]
    assert second["decisions"][c]["decision"] == from_review.NOT_DEFECT
    assert len(f.impl_commits()) == 1
    replies = [r[1] for r in fake_pr.replies()]
    assert replies.count(f.threads[A]) == 1


def _review_killed(f: Fixer) -> list[int]:
    """A review that dies once, as a kill between its `started` and `finished`."""
    calls: list[int] = []
    real = f.review.review

    def review(run, rnd):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed")
        return real(run, rnd)

    f.review.review = review
    return calls


async def _stopped_in_review(f: Fixer) -> list[int]:
    calls = _review_killed(f)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 1, lines
    rnd = f.state()
    assert "review" in rnd["started"] and rnd["review_run_dir"] is None
    return calls


@pytest.mark.asyncio
async def test_lineage_s4_a_review_killed_with_the_newest_review_at_head_is_skipped(
    tmp_path, fake_pr, monkeypatch
):
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    calls = await _stopped_in_review(f)
    _write_review(f.dir, _head(f.repo), [_finding(A, "add subtracts")])
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert lines[-1] == "FINDINGS:MAX_ROUNDS:1" and len(calls) == 1
    rnd = f.state()
    assert rnd["review_run_dir"] == str(f.dir) and "review" in rnd["finished"]
    assert f.review.posts == []


@pytest.mark.asyncio
async def test_lineage_s5_a_review_killed_with_the_newest_review_elsewhere_runs_again(
    tmp_path, fake_pr, monkeypatch
):
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    calls = await _stopped_in_review(f)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert lines[-1] == "FINDINGS:MAX_ROUNDS:1" and len(calls) == 2
    assert f.state()["review_run_dir"] == "/reviews/r1" and f.review.posts


@pytest.mark.asyncio
async def test_lineage_s10_a_step_stopped_twice_sends_no_reply_twice(
    tmp_path, fake_pr, monkeypatch
):
    """Row S10: one thread's reply fails twice; the other's is sent once."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    fake_pr.fail_reply_to = {f.threads[B]}
    for _ in range(2):
        run, lines = f.run()
        with _Stubs(engine_stubs(f.repo)):
            assert await loop.run_findings(run) == 1, lines
        assert lines[-1].startswith("FINDINGS:FAILED:publish:")
    fake_pr.fail_reply_to = set()
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    replies = [r[1] for r in fake_pr.replies()]
    assert sorted(replies) == sorted(f.threads.values())
    assert len(f.impl_commits()) == 1


@pytest.mark.asyncio
async def test_a_second_fetch_of_one_review_drops_what_is_decided(
    tmp_path, fake_pr, monkeypatch
):
    """Item 2: an id with a decision at the review's head is not expected
    again, so a new round leaves its thread alone."""
    f = Fixer(tmp_path, fake_pr, monkeypatch)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    d = "dd44dd44dd"
    _bot_thread(fake_pr, "naming is odd again", d)
    _write_review(
        f.dir,
        "h1",
        [_finding(A, "add subtracts"), _finding(B, "x"), _finding(d, "y")],
    )
    _two_rounds(f)
    run, lines = f.run()
    with _Stubs(engine_stubs(f.repo)):
        assert await loop.run_findings(run) == 0, lines
    assert f.state()["expected"] == [d]
    assert sorted(c["finding_id"] for c in f.state()["comments"]) == [d]
    assert len(f.impl_commits()) == 1


# --- the epic (rows S9-S11) -------------------------------------------------------

INGESTION = {"exit": 1, "stdout": "FINDINGS:INGESTION_ERROR:m1"}


def test_s9_an_ingestion_error_fails_the_child_and_the_next_run_relaunches_once(
    tmp_path, monkeypatch
):
    r = Rounds(
        tmp_path,
        monkeypatch,
        [{"findings": [medium("m1", "breaks-users")]}],
        [INGESTION],
    )
    assert r.run() == 1
    st = r.status()
    assert st["status"] == "failed" and "m1" in st["reason"]
    [fixer] = r.fixers()
    assert fixer[fixer.index("--from-review") + 1].endswith(
        "pr-" + fixer[fixer.index("--pr") + 1]
    )
    assert len(r.tries("build")) == 1  # not built again

    assert r.run() == 1  # launched once more, with a fresh round
    assert len(r.fixers()) == 2 and r.status()["status"] == "failed"
    assert r.run() == 1  # never a third time
    assert len(r.fixers()) == 2 and len(r.tries("build")) == 1
    assert r.status()["status"] == "failed" and r.reviews_of_a() == 1


def _at_cap(r: Rounds, **over) -> None:
    r.config.epic = EpicSettings(
        max_parallel=2, merge="auto", max_review_rounds=1, **over
    )


def test_s10_open_confirmed_findings_at_the_cap_park_until_the_head_changes(
    tmp_path, monkeypatch
):
    r = Rounds(
        tmp_path,
        monkeypatch,
        [{"findings": [medium("m1", "correctness-only")]}],
    )
    _at_cap(r)
    r.run()
    st = r.status()
    assert st["status"] == "parked" and "m1" in st["reason"]
    [rec] = r.reviewed()
    assert (rec["next"], rec["open_ids"]) == ("park", ["m1"])
    assert r.fixers() == [] and r.fakes.merges() == []

    r.run()  # the same head: parked again, nothing reviewed, fixed or written
    assert r.status()["status"] == "parked" and len(r.reviewed()) == 1
    assert r.reviews_of_a() == 1 and r.fixers() == []

    new = "c" * 40
    r.fakes.set_head("e-a", new)
    r.run()  # a new head is reviewed again
    assert r.reviews_of_a() == 2
    assert r.reviewed()[-1]["head_sha"] == new


def test_s11_accepted_findings_send_the_parked_child_to_the_merge_step(
    tmp_path, monkeypatch
):
    r = Rounds(
        tmp_path,
        monkeypatch,
        [{"findings": [medium("m1", "correctness-only")]}],
    )
    _at_cap(r)
    r.run()
    assert r.status()["status"] == "parked"
    _at_cap(r, accepted_findings={"a": {"m1": "Spike: owner data has no duplicates"}})
    assert r.run() == 0 and r.merged()
    first, last = r.reviewed()
    assert first["head_sha"] == last["head_sha"]
    assert (last["blocking"], last["accepted"]) == ([], ["m1"])
    assert r.reviews_of_a() == 1 and r.fixers() == []


def test_s10_a_high_at_the_cap_parks_with_its_id_too(tmp_path, monkeypatch):
    r = Rounds(tmp_path, monkeypatch, [{"findings": [high("h1")]}])
    _at_cap(r)
    r.run()
    assert r.reviewed()[0]["open_ids"] == ["h1"]


def test_accepted_findings_are_read_from_config_and_a_finding_with_no_reason_still_blocks(
    tmp_path,
):
    from build_pipeline.config import BuildConfig

    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text(
        "context: x\n"
        "epic:\n"
        "  accepted_findings:\n"
        "    feed-guard:\n"
        "      '8684f487ee': 'Spike: no legacy login holds two roles'\n"
        "      'cd83b4ff3d': ''\n"
        "    owner-sections: not-a-mapping\n"
    )
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert config.epic.accepted_findings == {
        "feed-guard": {"8684f487ee": "Spike: no legacy login holds two roles"}
    }
    assert EpicSettings().accepted_findings == {}
