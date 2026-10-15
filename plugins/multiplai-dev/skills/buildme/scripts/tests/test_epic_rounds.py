"""At most 3 reviews per epic child PR, and the decision after each review
the coordinator records: another round, a final fix, or a park
(plan-review-rounds-and-finders-2026-10-15.md, items 1-2). The build,
`gh`, the review skill and the fixer are fakes first on PATH
(`epic_row_helpers.Fakes`); git is real."""

from __future__ import annotations

import json

import pytest

from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import review


def medium(fid: str, impact: str | None = "correctness-only") -> dict:
    f = {"id": fid, "severity": "MEDIUM", "status": "confirmed", "claim": f"{fid} is wrong",
         "file": "app.py", "line_start": 1}
    return {**f, "impact": impact} if impact else f


def high(fid: str, impact: str = "breaks-users") -> dict:
    return {**medium(fid, impact), "severity": "HIGH"}


# --- config ----------------------------------------------------------------------

@pytest.mark.parametrize("yaml_text, want", [
    ("", "breaks"),
    ("epic:\n  round_worth: any\n", "any"),
    ("epic:\n  round_worth: always\n", "breaks"),
])
def test_round_worth_defaults_to_breaks_and_falls_back(tmp_path, yaml_text, want):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text("context: x\n" + yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert (config.epic.round_worth, config.epic.max_review_rounds) == (want, 3)
    assert EpicSettings().round_worth == "breaks"


# --- the decision ------------------------------------------------------------------

@pytest.mark.parametrize("found, pr_round, worth, want", [
    ([medium("m1"), medium("m2", "hygiene")], 1, "breaks", "final_fix"),
    ([medium("m1"), medium("m2", "breaks-users")], 1, "breaks", "round"),
    ([medium("m1", "breaks-business")], 2, "breaks", "round"),
    ([medium("m1", None)], 1, "breaks", "round"),          # not rated: not known to be harmless
    ([high("h1", "correctness-only")], 1, "breaks", "round"),
    ([medium("m1")], 1, "any", "round"),
    ([medium("m1", "breaks-users")], 3, "breaks", "final_fix"),     # at the cap only MEDIUMs: final fix
    ([medium("m1")], 3, "any", "final_fix"),
    ([high("h1"), medium("m1")], 3, "breaks", "park"),
    ([high("h1")], 4, "breaks", "park"),
], ids=["only-correctness-only", "one-breaks-users", "breaks-business", "unrated", "high",
        "any-earns-a-round", "cap-mediums", "cap-mediums-any", "cap-high", "past-cap-high"])
def test_decide(found, pr_round, worth, want):
    got, why = review.decide(found, pr_round, 3, worth)
    assert got == want and why


def test_blocking_findings_are_the_listed_code_highs_and_mediums(tmp_path):
    doc = {"mode": None, "findings": [medium("m1"), high("h1"), {**medium("low"), "severity": "LOW"},
                                      {**medium("t1"), "section": "tests"}, {**medium("r1"), "status": "refuted"}]}
    (tmp_path / "findings.json").write_text(json.dumps(doc))
    assert [f["id"] for f in review.blocking_findings(tmp_path)] == ["m1", "h1"]
    assert [b.split()[:2] for b in review.blockers(tmp_path)] == [["MEDIUM", "m1"], ["HIGH", "h1"]]
    assert review.blocking_findings(tmp_path / "missing") is None


# --- the coordinator -----------------------------------------------------------------
#
# A on a chain (b waits on a's merge). a's build pushes its commit; its first
# review answers from `reviews`, its fixer launches from `fixes` (each round
# pushed, as `findings` publishes it). Under `auto` a clean or final-fixed PR
# is merged by the fake `gh pr merge`.

import os  # noqa: E402

from build_pipeline.epic import coordinator, records  # noqa: E402
from build_pipeline.epic import lock as epic_lock  # noqa: E402

from .epic_row_helpers import merge_waiting, pr_number, pr_url, run_killed  # noqa: E402
from .test_epic_merge import MergeRoot  # noqa: E402

N = pr_number("e-a")
FINAL = {"rounds": [{}], "max_rounds": True, "push": True}       # the final fix: one round, no review
LEFT = 'MEDIUM m1 app.py:1 m1 is wrong'


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Rounds(MergeRoot):
    def __init__(self, root, monkeypatch, reviews: list[dict], fixes: list[dict] | None = None, merge_mode="auto"):
        super().__init__(root, monkeypatch, main=None, merge_mode=merge_mode)
        (root / "reviews.json").write_text(json.dumps({str(N): reviews}))
        (root / "fixes.json").write_text(json.dumps({str(N): fixes} if fixes else {}))

    def reviewed(self) -> list[dict]:
        """a's review records' results that carry a review's fields."""
        return [a.result for a in records.attempts(self.dir) if a.child == "a" and a.kind in ("review", "resume")
                and a.result is not None and "blocking" in a.result]

    def fixers(self) -> list[list[str]]:
        return [x["argv"] for x in self.fakes.fix_launches()]

    def reviews_of_a(self) -> int:
        return len([x for x in self.fakes.review_launches() if str(N) in x["argv"]])

    def issues(self) -> list[dict]:
        p = self.root / "issues.json"
        return json.loads(p.read_text()) if p.exists() else []

    def fix_rounds(self) -> int:
        doc = self.w / "specs" / "changes" / "e-a" / ".findings-state.json"
        return len(json.loads(doc.read_text())["findings"]["rounds"])


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_only_correctness_only_mediums_get_one_final_fix_then_merge_and_one_issue(tmp_path, monkeypatch):
    """Done means 3: the review records `next: final_fix`; one fixer runs,
    with `--no-review` and one round; nothing reviews its head; the PR merges
    under `auto` and one issue lists what was left."""
    r = Rounds(tmp_path, monkeypatch, [{"findings": [medium("m1"), medium("m2", "hygiene")]}], [FINAL])
    assert r.run() == 0 and r.merged()
    [rec] = r.reviewed()
    assert (rec["next"], rec["pr_round"]) == ("final_fix", 1) and rec["next_reason"]
    [fixer] = r.fixers()
    assert "--no-review" in fixer and _opt(fixer, "--max-rounds") == "1"
    assert len(r.fakes.post_launches()) == 1 and r.reviews_of_a() == 1
    [issue] = r.issues()
    assert f"#{N}" in issue["title"] and pr_url("e-a") in issue["body"]
    assert LEFT in issue["body"] and "MEDIUM m2" in issue["body"] and "last fixes had no review" in issue["body"]
    assert "One final fix acted on them" in issue["body"] and "no fix ran" not in issue["body"]


def test_under_manual_the_final_fixed_pr_waits_for_a_human_merge_with_its_issue(tmp_path, monkeypatch):
    r = Rounds(tmp_path, monkeypatch, [{"findings": [medium("m1")]}], [FINAL], merge_mode="manual")
    assert r.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert r.status()["status"] == "built" and r.fakes.merges() == []
    assert len(r.issues()) == 1 and r.reviews_of_a() == 1
    assert r.run() == 3 and len(r.issues()) == 1 and len(r.fixers()) == 1


def test_a_breaks_users_medium_earns_another_round(tmp_path, monkeypatch):
    r = Rounds(tmp_path, monkeypatch, [{"findings": [medium("m1"), medium("m2", "breaks-users")]}],
               [{"rounds": [{}], "push": True}])
    assert r.run() == 0 and r.merged()
    first, last = r.reviewed()
    assert first["next"] == "round" and "m2 (breaks-users)" in first["next_reason"]
    assert last["blocking"] == [] and "next" not in last
    [fixer] = r.fixers()
    assert "--no-review" not in fixer and _opt(fixer, "--max-rounds") == "1"
    assert r.issues() == []


@pytest.mark.parametrize("left, want", [(high("h3"), "park"), (medium("m3", "breaks-users"), "final_fix")])
def test_at_round_3_an_open_high_parks_and_mediums_get_the_final_fix(tmp_path, monkeypatch, left, want):
    """The cap is 3 by default. Each fixer launch runs one round, whose
    review is the next: reviews 2 and 3 are two launches' reviews, and each
    gets its own recorded decision."""
    one = lambda found: {"rounds": [{"findings": found}], "max_rounds": True, "push": True}
    r = Rounds(tmp_path, monkeypatch, [{"findings": [high("h1")]}], [one([high("h2")]), one([left]), FINAL])
    r.run()
    nexts = [(x["pr_round"], x.get("next")) for x in r.reviewed()]
    assert nexts == [(1, "round"), (2, "round"), (3, want)]
    assert [_opt(f, "--max-rounds") for f in r.fixers()[:2]] == ["1", "2"]
    assert not any("--no-review" in f for f in r.fixers()[:2])
    if want == "park":
        assert r.status()["status"] == "parked" and r.status()["reason"].startswith("review round 3 of 3 left 1 blocker")
        assert len(r.fixers()) == 2 and r.issues() == [] and r.fakes.merges() == []
    else:
        assert r.merged() and len(r.issues()) == 1 and "MEDIUM m3" in r.issues()[0]["body"]
        assert "--no-review" in r.fixers()[2] and _opt(r.fixers()[2], "--max-rounds") == "3"
    assert r.reviews_of_a() == 1          # the coordinator's own; reviews 2 and 3 are the fixers'


def test_a_round_fixer_runs_one_round_so_a_low_value_review_2_gets_the_final_fix(tmp_path, monkeypatch):
    """Spike, 2026-10-15: one round per fixer launch, so a decision follows
    every review. Review 2 leaves only a `correctness-only` MEDIUM: the
    decision on it is `final_fix`, not a third review."""
    one = {"rounds": [{"findings": [medium("m2")]}], "max_rounds": True, "push": True}
    r = Rounds(tmp_path, monkeypatch, [{"findings": [high("h1")]}], [one, FINAL])
    assert r.run() == 0 and r.merged()
    assert [(x["pr_round"], x.get("next")) for x in r.reviewed()] == [(1, "round"), (2, "final_fix")]
    first, final = r.fixers()
    assert _opt(first, "--max-rounds") == "1" and "--no-review" not in first
    assert _opt(final, "--max-rounds") == "2" and "--no-review" in final
    assert len(r.issues()) == 1 and "MEDIUM m2" in r.issues()[0]["body"]


def test_a_final_fix_that_found_nothing_to_act_on_merges_and_its_issue_says_no_fix_ran(tmp_path, monkeypatch):
    """A final fixer whose fetch finds no new comment (the blockers were
    already on the PR, so `post` skipped them) ends `FINDINGS:DONE` with no
    round and HEAD still the reviewed head. The decision already chose not
    to review again, so the PR still goes to the merge step, and the issue
    says no fix ran on what it lists instead of that a fix acted on it."""
    r = Rounds(tmp_path, monkeypatch, [{"findings": [medium("m1")]}], [{}])
    assert r.run() == 0 and r.merged()
    [fixer] = r.fixers()
    assert "--no-review" in fixer and r.reviews_of_a() == 1
    [fix] = [a for a in records.attempts(r.dir) if a.kind == "fix"]
    assert fix.result["final_head"] == fix.started["head_sha"]
    [issue] = r.issues()
    assert LEFT in issue["body"] and "no fix ran" in issue["body"]
    assert "acted on them" not in issue["body"]
    assert r.run() == 0 and len(r.issues()) == 1 and len(r.fixers()) == 1


# --- stop and resume (rows R2, R3, R4) ---------------------------------------------------

def _outcome(r: Rounds) -> dict:
    return {"status": r.status()["status"], "fixers": [_opt(f, "--max-rounds") for f in r.fixers()],
            "fix_rounds": r.fix_rounds(), "reviews": r.reviews_of_a(),
            "issues": [(i["title"], i["body"]) for i in r.issues()], "merged": r.merged()}


def _never_stopped(tmp_path, monkeypatch) -> dict:
    r = Rounds(tmp_path / "plain", monkeypatch, [{"findings": [medium("m1")]}], [FINAL])
    assert r.run() == 0
    return _outcome(r)


def _die_after_the_fixer() -> None:
    real = coordinator.Coordinator._run_step

    async def dying(self, c, attempt, cwd, **kw):
        out = await real(self, c, attempt, cwd, **kw)
        if attempt.kind == "fix":
            os._exit(137)
        return out

    coordinator.Coordinator._run_step = dying


def test_r2_a_final_fixer_killed_before_its_round_runs_again_once_with_the_same_argv(tmp_path, monkeypatch):
    plain = _never_stopped(tmp_path, monkeypatch)
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [medium("m1")]}], [{"die": True}, FINAL])
    assert r.run() == 3
    assert r.status()["status"] == "built" and r.issues() == []
    assert r.run() == 0
    first, again = r.fixers()
    assert first == again and "--no-review" in first
    assert _outcome(r) == {**plain, "fixers": ["1", "1"]}


def test_r2_a_final_fixer_killed_at_round_3_runs_again_once_though_no_review_round_is_left(tmp_path, monkeypatch):
    """At the cap no review round is left, and a killed final fixer still
    runs again: left to the review step, its head would have no final fix
    and the child would wait with its blockers. Compared with the run that
    never stopped."""
    one = lambda found: {"rounds": [{"findings": found}], "max_rounds": True, "push": True}
    fixes = [one([high("h2")]), one([medium("m3")])]
    plain = Rounds(tmp_path / "plain", monkeypatch, [{"findings": [high("h1")]}], [*fixes, FINAL])
    assert plain.run() == 0
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [high("h1")]}], [*fixes, {"die": True}, FINAL])
    assert r.run() == 3
    assert r.status()["status"] == "built" and r.issues() == [] and not r.merged()
    assert r.run() == 0
    assert [(x["pr_round"], x.get("next")) for x in r.reviewed()] == [(1, "round"), (2, "round"), (3, "final_fix")]
    final, again = r.fixers()[2:]
    assert final == again and "--no-review" in final
    assert _outcome(r) == {**_outcome(plain), "fixers": [*_outcome(plain)["fixers"], _opt(final, "--max-rounds")]}


def test_r2_a_final_fixer_killed_mid_round_with_its_commit_pushed_finishes_that_round_only(tmp_path, monkeypatch):
    """Row R2 as the plan states it: killed after its round's commit is on the
    child branch and before the round is recorded finished. The relaunch
    keeps `--max-rounds` 1, so `findings` finishes the open round and stops;
    recounted from the state (one round, open), it would allow a second."""
    plain = _never_stopped(tmp_path, monkeypatch)
    killed = {"rounds": [{"stop": "review"}], "push": True}
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [medium("m1")]}], [killed, FINAL])
    assert r.run() == 3
    assert r.status()["status"] == "built" and r.issues() == [] and r.fix_rounds() == 1
    assert r.run() == 0
    first, again = r.fixers()
    assert first == again and "--no-review" in first
    assert _outcome(r) == {**plain, "fixers": ["1", "1"]}


def test_a_final_fix_after_a_fixer_stopped_in_its_review_at_round_3_gets_a_fresh_round(tmp_path, monkeypatch):
    """A fixer killed after its review 3 reached the ledger and before its
    round was recorded finished leaves that round open, and no review round
    left, so the review step decides on review 3: `final_fix`. `findings`
    resumes the open round first, so the final fix's `--max-rounds` counts
    every round in the state, finished or not, plus one: the fresh round
    that fetches and fixes the blockers just posted. Compared with the run
    that never stopped."""
    def fixes(stop: bool) -> list[dict]:
        last = {"findings": [medium("m3")], **({"stop": "review"} if stop else {})}
        first = {"rounds": [{"findings": [high("h2")]}], "max_rounds": True, "push": True}
        second = {"rounds": [last], "max_rounds": True, "push": True}
        final = {"rounds": [{}, {}] if stop else [{}], "max_rounds": True, "push": True}
        return [first, second, final]

    plain = Rounds(tmp_path / "plain", monkeypatch, [{"findings": [high("h1")]}], fixes(False))
    assert plain.run() == 0
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [high("h1")]}], fixes(True))
    assert r.run() == 3
    assert r.status()["status"] == "built" and r.fix_rounds() == 2
    assert r.run() == 0
    assert [(x["pr_round"], x.get("next")) for x in r.reviewed()] == [(1, "round"), (2, "round"), (3, "final_fix")]
    assert "--no-review" in r.fixers()[2]
    assert _outcome(r) == _outcome(plain) == {**_outcome(plain), "fixers": ["1", "2", "3"], "fix_rounds": 3}


def test_r2_a_final_fixer_that_ended_before_its_result_was_written_is_not_paid_twice(tmp_path, monkeypatch):
    """Its relaunch keeps `--max-rounds` 1, so `findings`, which counts the
    finished round, ends at once (`FINDINGS:MAX_ROUNDS`) instead of running
    a second round."""
    plain = _never_stopped(tmp_path, monkeypatch)
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [medium("m1")]}], [FINAL, {"max_rounds": True}])
    run_killed(r.run, _die_after_the_fixer)
    fix = [a for a in records.attempts(r.dir) if a.kind == "fix"]
    assert len(fix) == 1 and fix[0].result is None
    assert r.run() == 0
    first, again = r.fixers()
    assert first == again
    assert _outcome(r) == {**plain, "fixers": ["1", "1"]}


def test_r2b_a_round_fixer_that_ended_before_its_result_was_written_is_not_paid_twice(tmp_path, monkeypatch):
    """A `round` fixer finished its one round (pushed, reviewed clean) and the
    coordinator died before writing its result. The relaunch keeps
    `--max-rounds` 1, so `findings` ends at once; recounted from the state it
    would get 2 and run, and pay for, a second round with no decision."""
    reviews = [{"findings": [medium("m2", "breaks-users")]}]
    one = {"rounds": [{}], "max_rounds": True, "push": True}
    plain = Rounds(tmp_path / "plain", monkeypatch, reviews, [one])
    assert plain.run() == 0
    r = Rounds(tmp_path / "stopped", monkeypatch, reviews, [one, {"max_rounds": True}])
    run_killed(r.run, _die_after_the_fixer)
    fix = [a for a in records.attempts(r.dir) if a.kind == "fix"]
    assert len(fix) == 1 and fix[0].result is None
    assert r.run() == 0
    first, again = r.fixers()
    assert first == again and "--no-review" not in first
    assert _outcome(r) == {**_outcome(plain), "fixers": ["1", "1"]}


def test_r3_a_final_fix_done_and_not_merged_goes_to_the_merge_with_no_review(tmp_path, monkeypatch):
    plain = _never_stopped(tmp_path, monkeypatch)
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [medium("m1")]}], [FINAL])

    def die_at_the_merge():
        async def dying(self, c):
            os._exit(137)
        coordinator.Coordinator._merge = dying

    run_killed(r.run, die_at_the_merge)
    [fix] = [a for a in records.attempts(r.dir) if a.kind == "fix"]
    assert fix.result["final_head"] == r.origin_sha()
    assert r.run() == 0
    assert _outcome(r) == plain


def test_r4_merged_and_no_issue_yet_files_exactly_one(tmp_path, monkeypatch):
    plain = _never_stopped(tmp_path, monkeypatch)
    r = Rounds(tmp_path / "stopped", monkeypatch, [{"findings": [medium("m1")]}], [FINAL])

    def die_before_the_issue():
        def dying(self, c):
            os._exit(137)
        coordinator.Coordinator._left_issue = dying

    run_killed(r.run, die_before_the_issue)
    assert r.status()["status"] == "merged" and r.issues() == []
    assert r.run() == 0 and r.run() == 0
    assert _outcome(r) == plain
