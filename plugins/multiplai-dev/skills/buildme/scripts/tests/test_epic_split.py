"""Epic mode, PR A: the EPIC_SPLIT step decides one change or several, writes
the child plans, and waits (plan-buildme-epic-mode-2026-10-12.md,
work items 1-5, 7 and 9-10). The model call is always a fake."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline.epic import split as epic_split_mod
from build_pipeline.epic import store
from build_pipeline.epic.split import EpicChild, EpicSplitAnswer, check_answer
from build_pipeline.models import BuildPhase

from .epic_row_helpers import (
    CYCLE,
    PLAN,
    SINGLE,
    THREE,
    CallLog,
    FakeSplit,
    args_for,
    child,
    load_state,
    make_build,
    make_project,
    run_build,
    run_killed,
    use_workspace,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    log = CallLog(tmp_path / "calls.log")
    return tmp_path, data, project, log


def _epic_dir(data: Path, change: str = "e") -> Path:
    return data / "buildme" / "epics" / f"o-r--{change}"


def _split(fake: FakeSplit):
    return patch.object(epic_split_mod, "epic_split", fake)


# --- item 4: the checks -------------------------------------------------------

class TestCheckAnswer:
    def test_a_valid_split_has_no_errors(self):
        assert check_answer(THREE) == []

    def test_single_needs_no_children(self):
        assert check_answer(SINGLE) == []

    @pytest.mark.parametrize("bad", ["API", "a_b", "-a", "a-", "a b", ""])
    def test_ids_are_lowercase_and_hyphenated(self, bad):
        ans = EpicSplitAnswer(single=False, children=[child(bad), child("ok")])
        assert any("id" in e for e in check_answer(ans))

    def test_ids_are_unique(self):
        ans = EpicSplitAnswer(single=False, children=[child("a"), child("a")])
        assert any("twice" in e for e in check_answer(ans))

    def test_every_dependency_names_a_child(self):
        ans = EpicSplitAnswer(single=False, children=[child("a", ("ghost",)), child("b")])
        assert any("ghost" in e for e in check_answer(ans))

    def test_a_cycle_is_named(self):
        errors = check_answer(CYCLE)
        assert any("cycle" in e for e in errors)

    def test_a_self_dependency_is_a_cycle(self):
        ans = EpicSplitAnswer(single=False, children=[child("a", ("a",)), child("b")])
        assert any("cycle" in e for e in check_answer(ans))

    def test_each_plan_has_a_title_and_done_means(self):
        bad = EpicChild(id="a", title="A", plan="# Plan: A\n\nno criteria\n")
        ans = EpicSplitAnswer(single=False, children=[bad, child("b")])
        errors = check_answer(ans)
        assert any("## Done means" in e and "a" in e for e in errors)

    def test_several_needs_two_children(self):
        ans = EpicSplitAnswer(single=False, children=[child("a")])
        assert check_answer(ans)


# --- item 5: the directory name ------------------------------------------------

class TestSlug:
    """Copied from the review skill's slug rule (review_pipeline/target.py,
    `origin_owner_repo` and `slug_repo_name`) and pinned here."""

    @pytest.mark.parametrize("url", [
        "https://github.com/spikelab/github-tests.git",
        "git@github.com:spikelab/github-tests.git",
        "ssh://git@github.com/spikelab/github-tests",
        "https://github.com/spikelab/github-tests/",
    ])
    def test_owner_and_repo_from_any_remote_form(self, url):
        assert store.origin_owner_repo(url) == ("spikelab", "github-tests")

    def test_a_local_bare_origin_uses_its_last_two_path_parts(self):
        assert store.origin_owner_repo("/tmp/x/o/r.git") == ("o", "r")

    def test_no_url(self):
        assert store.origin_owner_repo(None) is None
        assert store.origin_owner_repo("r") is None

    def test_epic_dir_is_under_the_data_dir(self, env):
        _root, data, project, _log = env
        assert store.epic_dir_for(project, "e") == _epic_dir(data)

    def test_a_repo_without_origin_uses_its_directory_name(self, tmp_path, monkeypatch):
        data = use_workspace(monkeypatch, tmp_path)
        repo = tmp_path / "plain"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        assert store.epic_dir_for(repo, "e") == data / "buildme" / "epics" / "plain--e"


# --- item 10: the behaviour ----------------------------------------------------

class TestSingleChange:
    def test_writes_no_epic_files_or_directory_and_goes_on(self, env):
        root, data, project, log = env
        config = make_build(project)
        fake = FakeSplit(log, SINGLE)
        with _split(fake), patch("build_pipeline.spec_generator.run_spec_generator",
                                 AsyncMock(return_value=1)) as spec:
            import asyncio

            from build_pipeline.orchestrator import run_orchestrator
            rc = asyncio.run(run_orchestrator(config, args_for(config)))
        assert rc == 1 and spec.await_count == 1   # the build went on to spec generation
        assert len(log.calls()) == 1
        assert not (data / "buildme").exists()
        assert load_state(config).epic_dir is None

    def test_a_resume_after_the_decision_makes_no_second_call(self, env):
        """Killed during spec generation, with the pointer at EPIC_SPLIT."""
        import os

        root, data, project, log = env
        config = make_build(project)

        async def killed(*a, **k):
            os._exit(137)

        with _split(FakeSplit(log, SINGLE)):
            run_killed(lambda: run_build(config, args_for(config), spec=killed))
            assert load_state(config).phase == BuildPhase.EPIC_SPLIT
            run_build(config, args_for(config))
        assert len(log.calls()) == 1


class TestSeveralChildren:
    def test_three_children_give_three_plans_and_exit_3(self, env, capsys):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            rc = run_build(config, args_for(config))
        out = capsys.readouterr().out
        epic = _epic_dir(data)
        assert rc == 3
        assert f"EPIC:SPLIT:{epic / 'split.md'}" in out
        assert "PHASE:EPIC_SPLIT:COMPLETE" in out
        assert sorted(p.name for p in (epic / "plans").iterdir()) == ["api.md", "docs.md", "ui.md"]
        saved = json.loads((epic / "epic.json").read_text())
        assert saved["state"] == "proposed"
        assert [c["id"] for c in saved["children"]] == ["api", "ui", "docs"]
        split_md = (epic / "split.md").read_text()
        assert "| docs |" in split_md and "api, ui" in split_md
        assert "in parallel" in split_md
        s = load_state(config)
        assert s.phase == BuildPhase.EPIC_SPLIT and s.epic_dir == str(epic)

    def test_a_plan_is_written_as_the_answer_gave_it(self, env):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            run_build(config, args_for(config))
        assert (_epic_dir(data) / "plans" / "api.md").read_text() == PLAN.format(title="Child api")

    def test_waiting_without_approval_repeats_the_line(self, env, capsys):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            run_build(config, args_for(config))
            capsys.readouterr()
            rc = run_build(config, args_for(config))
        assert rc == 3
        assert "EPIC:SPLIT:" in capsys.readouterr().out
        assert len(log.calls()) == 1

    def test_a_cycle_retries_once_then_exits_1(self, env, capsys):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, CYCLE)):
            rc = run_build(config, args_for(config))
        assert rc == 1
        calls = log.calls()
        assert [c["retry"] for c in calls] == [False, True]
        assert "cycle" in capsys.readouterr().err
        assert not _epic_dir(data).exists()

    def test_a_cycle_fixed_on_retry_is_used(self, env):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, CYCLE, retry=THREE)):
            rc = run_build(config, args_for(config))
        assert rc == 3
        assert (_epic_dir(data) / "plans" / "docs.md").exists()

    def test_the_retry_is_given_the_errors(self, env):
        root, data, project, log = env
        config = make_build(project)
        seen = []

        async def fake(config, inputs, errors=(), *, call=None):
            seen.append(list(errors))
            return CYCLE if not errors else THREE

        with _split(fake):
            run_build(config, args_for(config))
        assert seen[0] == [] and any("cycle" in e for e in seen[1])


class TestSkip:
    def test_no_epic_split_makes_no_call(self, env, caplog):
        root, data, project, log = env
        config = make_build(project)
        fake = FakeSplit(log, THREE)
        with _split(fake), caplog.at_level("INFO"):
            run_build(config, args_for(config, no_epic_split=True))
        assert log.calls() == []
        assert "SKIP phase=EPIC_SPLIT reason=epic-child" in caplog.text
        assert not (data / "buildme").exists()
        assert load_state(config).phase != BuildPhase.CODEBASE_ANALYSIS

    def test_only_mode_makes_no_call(self, env, caplog):
        root, data, project, log = env
        config = make_build(project, mode="only")
        from build_pipeline import orchestrator

        with _split(FakeSplit(log, THREE)), caplog.at_level("INFO"), \
                patch.object(orchestrator, "_run_research_check", AsyncMock()):
            run_build(config, args_for(config))
        assert log.calls() == []
        assert "SKIP phase=EPIC_SPLIT reason=only-mode" in caplog.text
