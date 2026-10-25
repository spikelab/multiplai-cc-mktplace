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
