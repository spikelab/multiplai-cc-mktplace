"""The stop and resume rows of plan-pm-spec-gaps-2026-10-25.md: each case
stops a step at its row's point, or applies the row's change between runs,
resumes, and compares the result with a run that never stopped. S1-S3 are
the one writer of spec-issues.md (`readiness.write_issues`), S4-S8 the
epic's spec preflight, its parked children and its archived children, S13
an epic still running when the resume starts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from build_pipeline import readiness
from build_pipeline.epic import coordinator
from build_pipeline.epic import lock as epic_lock
from multiplai_core.specs import mark_ready, read_change_meta, write_change_meta

from . import test_findings_round as rounds
from .epic_row_helpers import EpicSplitAnswer, child, pr_url, run_killed
from .spec_fixtures import write_specs
from .test_epic_run import Epic, _push_specs
from .test_epic_store import hold_in_other_process

CHANGE = "calc"
A = ("Design: a", "one")
B = ("Design: b", "two")
ONE = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b")]
)
ISSUES = "SPEC:ISSUES:origin/ENG-40-e:specs/changes/e-a/spec-issues.md"


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


# --- S1-S3: spec-issues.md ----------------------------------------------------------


def _change(root: Path) -> Path:
    return write_specs(root / "specs" / "changes" / CHANGE)


def _state(change_dir: Path) -> tuple[str, object, list[str]]:
    """The file, the `spec_issues` record and any stray temp file."""
    return (
        (change_dir / "spec-issues.md").read_text(),
        read_change_meta(change_dir).get("spec_issues"),
        sorted(p.name for p in change_dir.glob(".spec-issues.md.tmp")),
    )


def _never_stopped(root: Path, *calls: list[tuple[str, str]]) -> tuple:
    d = _change(root)
    for issues in calls:
        readiness.write_issues(d, issues, change=CHANGE)
    return _state(d)


def test_s1_a_write_stopped_before_its_rename_resumes_as_one_write(
    tmp_path, monkeypatch
):
    plain = _never_stopped(tmp_path / "plain", [A], [A, B])
    d = _change(tmp_path / "stopped")
    readiness.write_issues(d, [A], change=CHANGE)
    before = (d / "spec-issues.md").read_text()
    real = Path.replace

    def stop(self, target):
        if self.name == ".spec-issues.md.tmp":
            raise KeyboardInterrupt
        return real(self, target)

    monkeypatch.setattr(Path, "replace", stop)
    with pytest.raises(KeyboardInterrupt):
        readiness.write_issues(d, [A, B], change=CHANGE)
    monkeypatch.setattr(Path, "replace", real)
    assert (d / "spec-issues.md").read_text() == before
    assert (d / ".spec-issues.md.tmp").exists()
    readiness.write_issues(d, [A, B], change=CHANGE)
    assert _state(d) == plain


def test_s2_a_write_stopped_before_spec_issues_is_set_resumes_with_the_flag(
    tmp_path, monkeypatch
):
    plain = _never_stopped(tmp_path / "plain", [A, B])
    d = _change(tmp_path / "stopped")
    real = readiness.set_spec_issues

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(readiness, "set_spec_issues", stop)
    with pytest.raises(KeyboardInterrupt):
        readiness.write_issues(d, [A, B], change=CHANGE)
    monkeypatch.setattr(readiness, "set_spec_issues", real)
    assert read_change_meta(d).get("spec_issues") is None
    written = (d / "spec-issues.md").read_text()
    readiness.write_issues(d, [A, B], change=CHANGE)
    assert (d / "spec-issues.md").read_text() == written
    assert _state(d) == plain


def test_s3_a_hand_edit_between_writes_is_kept_and_only_missing_headings_are_added(
    tmp_path,
):
    d = _change(tmp_path)
    path = readiness.write_issues(d, [A], change=CHANGE)
    edited = (
        path.read_text().replace("\none\n", "\none, as the user put it\n")
        + "\n## Mine\n\nmy note\n"
    )
    path.write_text(edited)
    readiness.write_issues(d, [A, B], change=CHANGE)
    assert path.read_text() == edited + "\n## Design: b\n\ntwo\n"


# --- S4-S8, S13: the epic -------------------------------------------------------------


def _set_issues(e: Epic, cid: str, open_: bool) -> None:
    """What buildme writes on the feature branch (open_) and what `pm-spec
    revise` leaves there (not open_)."""
    d = e.config.specs_dir / "changes" / f"e-{cid}"
    if open_:
        (d / "spec-issues.md").write_text(f"# Spec issues: e-{cid}\n\n## A\n\nvague\n")
        write_change_meta(d, {"spec_issues": "spec-issues.md"})
    else:
        write_change_meta(d, {"spec_issues": None})
    _push_specs(e.project, "spec issues" if open_ else "pm-spec revise")


def _outcome(e: Epic) -> dict:
    return {
        "status": e.status(),
        "launches": {cid: e.launches().count(cid) for cid in ("a", "b")},
    }


def _refusing(root: Path, monkeypatch) -> Epic:
    """a's first build refuses with SPEC:ISSUES (exit 4); its next builds."""
    return Epic(
        root,
        monkeypatch,
        answer=ONE,
        steps={"a": [{"marker": ISSUES, "exit": 4}, {}]},
        parallel=1,
    )


def _finish(e: Epic) -> int:
    """The rest of the runs: refused while the branch holds the issue, then
    one run once revise cleared it."""
    _set_issues(e, "a", True)
    refused = e.run()
    _set_issues(e, "a", False)
    e.run()
    return refused


def _plain(root: Path, monkeypatch) -> dict:
    e = _refusing(root, monkeypatch)
    e.run()
    assert e.status()["a"] == "parked"
    assert _finish(e) == readiness.EXIT_SPEC_ISSUES
    return _outcome(e)


def test_s4_a_refused_preflight_writes_nothing_and_checks_again(tmp_path, monkeypatch):
    plain = Epic(tmp_path / "plain", monkeypatch, answer=ONE)
    plain.run()
    e = Epic(tmp_path / "stopped", monkeypatch, answer=ONE)
    _set_issues(e, "a", True)
    assert e.run() == readiness.EXIT_SPEC_ISSUES
    assert not e.dir.exists() and e.launches() == []
    assert e.run() == readiness.EXIT_SPEC_ISSUES and not e.dir.exists()
    _set_issues(e, "a", False)
    e.run()
    assert _outcome(e) == _outcome(plain)


def _die_on_save(*, after: bool):
    """A `run_killed` setup: exit 137 at the first `_save` that holds a's park,
    before it writes (S5) or once it has (S6)."""

    def setup() -> None:
        real = coordinator.Coordinator._save

        def dying(self):
            if self.children["a"].status != "parked":
                return real(self)
            if after:
                real(self)
            import os

            os._exit(137)

        coordinator.Coordinator._save = dying

    return setup


def test_s5_stopped_before_the_park_is_saved_resumes_as_a_parked_child(
    tmp_path, monkeypatch
):
    plain = _plain(tmp_path / "plain", monkeypatch)
    e = _refusing(tmp_path / "stopped", monkeypatch)
    run_killed(e.run, _die_on_save(after=False))
    assert e.status().get("a") != "parked"
    assert _finish(e) == readiness.EXIT_SPEC_ISSUES
    assert _outcome(e) == plain


def test_s6_stopped_after_the_park_is_saved_stays_parked_until_the_check_passes(
    tmp_path, monkeypatch
):
    plain = _plain(tmp_path / "plain", monkeypatch)
    e = _refusing(tmp_path / "stopped", monkeypatch)
    run_killed(e.run, _die_on_save(after=True))
    assert e.status()["a"] == "parked" and e.reasons()["a"] == ISSUES
    _set_issues(e, "a", True)
    assert e.run() == readiness.EXIT_SPEC_ISSUES
    assert e.status()["a"] == "parked" and e.launches().count("a") == 1
    _set_issues(e, "a", False)
    e.run()
    assert _outcome(e) == plain


def test_s7_a_revise_pushed_between_runs_launches_the_parked_child_once(
    tmp_path, monkeypatch
):
    e = _refusing(tmp_path, monkeypatch)
    e.run()
    assert e.status()["a"] == "parked"
    d = e.config.specs_dir / "changes" / "e-a"
    req = next((d / "requirements").glob("*.md"))
    req.write_text(req.read_text() + "\nThe system SHALL say what None does.\n")
    mark_ready(d)
    _push_specs(e.project, "pm-spec revise")
    e.run()
    assert e.launches().count("a") == 2 and e.status()["a"] == "built"
    e.run()
    assert e.launches().count("a") == 2


def test_s8_an_archived_child_whose_records_were_lost_never_launches(
    tmp_path, monkeypatch
):
    from .test_epic_from_specs import CHAIN, _archive
    from .test_epic_from_specs import Epic as SpecsEpic

    def epic(root: Path) -> SpecsEpic:
        e = SpecsEpic(root, monkeypatch, answer=CHAIN)
        assert e.run() == 3
        e.fakes.set_pr(pr_url("e-a"), "MERGED")
        _archive(e, "e-a")
        return e

    def outcome(e: SpecsEpic) -> dict:
        return {
            "status": {k: v["status"] for k, v in e.status().items()},
            "builds": {cid: len(e.build_records(cid)) for cid in ("b",)},
        }

    plain = epic(tmp_path / "plain")
    plain.run()
    e = epic(tmp_path / "stopped")
    for p in (e.dir / "agents").glob("*-build-a.*"):
        p.unlink()
    saved = json.loads((e.dir / "epic.json").read_text())
    del saved["child_status"]["a"]
    (e.dir / "epic.json").write_text(json.dumps(saved))
    e.run()
    assert e.build_records("a") == []
    assert outcome(e) == outcome(plain)
    assert outcome(e)["status"]["a"] == "merged"


def test_s13_a_resume_while_the_stopped_epic_still_runs_is_refused(
    tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "plain", monkeypatch)
    e = _refusing(tmp_path / "stopped", monkeypatch)
    e.run()
    proc = hold_in_other_process(e.dir / ".lock")
    try:
        capsys.readouterr()
        assert e.run() == 2
        assert "epic e is running now" in capsys.readouterr().err
        assert e.launches().count("a") == 1
    finally:
        proc.communicate("")
    assert _finish(e) == readiness.EXIT_SPEC_ISSUES
    assert _outcome(e) == plain


# --- S9-S13: findings retries a deferred fix after revise -----------------------------

FIXED = "def add(a, b):\n    return a + b\n"


def _findings_repo(base: Path, monkeypatch):
    """`built` from test_findings_round, with `spec_revision` rev-1 recorded,
    a fresh fake PR, and the conflicting comment c0f1 on it."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    repo, change, config = rounds._make_built(base)
    rounds._ready_at(repo, change, "rev-1", issues=False)
    pr = rounds._install_pr(monkeypatch)
    pr.add_thread("alice", "add should refuse None", fid="c0f1")
    return repo, change, config, pr


async def _findings(change, config, pr, verdict, *, triage=None, review=None) -> int:
    triage = triage or rounds.FakeTriage({"add should refuse None": verdict})
    run, _ = rounds._run(
        config,
        change,
        triage_call=triage,
        review=review or rounds.FakeReview(pr),
    )
    with rounds._Stubs(rounds.engine_stubs(config.project_dir)):
        return await rounds.loop.run_findings(run)


def _findings_outcome(repo: Path, change: Path, pr) -> tuple:
    """What a finished run leaves, without shas and times: spec-issues.md,
    calc.py, the commit subjects, each saved entry's retry fields, the
    triage rows for c0f1 in each round's file, and each reply's kind."""
    st = rounds.BuildState.load(change / ".findings-state.json")
    entries = [
        (
            i.heading,
            i.deferred_comment_id,
            i.deferred_at_revision,
            i.retried_at_revision,
        )
        for r in st.findings.rounds
        for i in r.spec_issues
    ]
    rows = [
        sum(ln.startswith("| c0f1 ") for ln in p.read_text().splitlines())
        for p in sorted(change.glob("findings-r[0-9].md"))
    ]
    replies = [
        "fixed" if "Fixed in" in r[2] else "deferred" if "Not fixed" in r[2] else r[2]
        for r in pr.replies()
    ]
    return (
        (change / "spec-issues.md").read_text(),
        (repo / "calc.py").read_text(),
        rounds._subjects(repo),
        entries,
        rows,
        replies,
    )


async def _plain_retry(base: Path, monkeypatch) -> tuple:
    """Defer, revise to rev-2, retry: the run that never stopped."""
    repo, change, config, pr = _findings_repo(base, monkeypatch)
    assert await _findings(change, config, pr, rounds.CONFLICT) == 0
    rounds._ready_at(repo, change, "rev-2", issues=False)
    assert await _findings(change, config, pr, rounds.CONFLICT_FIXABLE) == 0
    return _findings_outcome(repo, change, pr)


class _StopFirst(rounds.FakeTriage):
    """Triage that stops the process (Ctrl-C) on its first *stops* calls."""

    def __init__(self, verdicts, stops=1):
        super().__init__(verdicts)
        self.stops = stops

    async def __call__(self, prompt, schema, **kw):
        if self.stops:
            self.stops -= 1
            raise KeyboardInterrupt
        return await super().__call__(prompt, schema, **kw)


@pytest.mark.asyncio
async def test_s9_a_round_stopped_after_the_deferred_entry_is_saved_resumes_once(
    tmp_path, monkeypatch
):
    """Stopped at the review, after the round's checkpoint saved the entry
    with `deferred_at_revision`: the round resumes and the entry is one."""
    repo, change, config, pr = _findings_repo(tmp_path / "plain", monkeypatch)
    assert await _findings(change, config, pr, rounds.CONFLICT) == 0
    plain = _findings_outcome(repo, change, pr)

    repo, change, config, pr = _findings_repo(tmp_path / "stopped", monkeypatch)

    class StopAtReview(rounds.FakeReview):
        def review(self, run, rnd):
            st = rounds.BuildState.load(change / ".findings-state.json")
            (entry,) = st.findings.rounds[0].spec_issues
            assert entry.deferred_at_revision == "rev-1"
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await _findings(change, config, pr, rounds.CONFLICT, review=StopAtReview(pr))
    assert await _findings(change, config, pr, rounds.CONFLICT) == 0
    assert _findings_outcome(repo, change, pr) == plain


@pytest.mark.asyncio
@pytest.mark.parametrize("stops", [1, 2], ids=["s10_once", "s11_twice"])
async def test_s10_s11_a_retry_stopped_before_it_is_saved_retries_once(
    tmp_path, monkeypatch, stops
):
    """S10: stopped in the retry's triage, before the round's checkpoint
    saves `retried_at_revision`. S11: the same step stopped twice in a row.
    Either resume ends as one retry: one triage row in the finished round."""
    plain = await _plain_retry(tmp_path / "plain", monkeypatch)

    repo, change, config, pr = _findings_repo(tmp_path / "stopped", monkeypatch)
    assert await _findings(change, config, pr, rounds.CONFLICT) == 0
    rounds._ready_at(repo, change, "rev-2", issues=False)
    stopping = _StopFirst({"add should refuse None": rounds.CONFLICT_FIXABLE}, stops)
    for _ in range(stops):
        with pytest.raises(KeyboardInterrupt):
            await _findings(change, config, pr, None, triage=stopping)
    assert await _findings(change, config, pr, rounds.CONFLICT_FIXABLE) == 0
    outcome = _findings_outcome(repo, change, pr)
    assert outcome == plain
    assert outcome[4] == [1, 1]  # one triage of c0f1 in each finished round
    assert (repo / "calc.py").read_text() == FIXED


@pytest.mark.asyncio
async def test_s12_a_base_moved_between_runs_does_not_change_the_retry(
    tmp_path, monkeypatch
):
    """The retry reads only `.change.yaml`: a commit from the base branch
    merged in between the runs changes nothing but that commit."""
    plain = await _plain_retry(tmp_path / "plain", monkeypatch)

    repo, change, config, pr = _findings_repo(tmp_path / "moved", monkeypatch)
    assert await _findings(change, config, pr, rounds.CONFLICT) == 0
    rounds._ready_at(repo, change, "rev-2", issues=False)
    (repo / "NOTES.md").write_text("from the base branch\n")
    rounds._git(repo, "add", "NOTES.md")
    rounds._git(repo, "commit", "-qm", "Merge origin/main")
    assert await _findings(change, config, pr, rounds.CONFLICT_FIXABLE) == 0
    outcome = _findings_outcome(repo, change, pr)
    subjects = [s for s in outcome[2] if s != "Merge origin/main"]
    assert (*outcome[:2], subjects, *outcome[3:]) == plain
    assert outcome[1] == FIXED


def test_s13_findings_holds_no_run_lock():
    """Findings has no run lock, so two findings runs on one PR can overlap.
    Spike chose no lock (2026-10-25): resume adds none, and this pins it, so
    adding one is a decision rather than a side effect."""
    findings = Path(rounds.loop.__file__).parent
    for py in sorted(findings.glob("*.py")):
        text = py.read_text()
        assert "flock" not in text and "epic.lock" not in text, py.name
