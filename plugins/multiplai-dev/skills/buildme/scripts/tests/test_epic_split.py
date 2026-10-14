"""Epic mode, PR A: the EPIC_SPLIT step decides one change or several, writes
the child plans, and waits for approval (plan-buildme-epic-mode-2026-10-12.md,
work items 1-10). The model call is always a fake."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline.epic import lock as epic_lock
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
    use_workspace,
)


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


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

    @pytest.mark.parametrize("plan, missing", [("# Plan: A\n\nno criteria\n", "## Done means"),
                                               ("## Done means\n1. x\n", "# Plan:")])
    def test_each_plan_has_a_title_and_done_means(self, plan, missing):
        bad = EpicChild(id="a", title="A", plan=plan)
        errors = check_answer(EpicSplitAnswer(single=False, children=[bad, child("b")]))
        assert errors == [f"child a: the plan has no `{missing}` " + ("line" if missing == "# Plan:" else "section")]

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
        assert (epic / ".lock").exists()

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

    def test_approval_in_the_run_that_wrote_the_split_is_not_taken(self, env):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            rc = run_build(config, args_for(config, approve_split=True))
        assert rc == 3
        assert json.loads((_epic_dir(data) / "epic.json").read_text())["state"] == "proposed"

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

    def test_a_split_saved_by_another_build_while_deciding_is_kept(self, env):
        """Another build of the same change saved its split, and the user edited
        one of its plans, while this build was still deciding: under the lock
        this build takes that epic.json, and writes over no file (A.S4, A.S8)."""
        root, data, project, log = env
        config = make_build(project)
        epic = _epic_dir(data)
        theirs = store.Epic(name="e", reason="Two parts.", children=[child("x"), child("y", ("x",))])
        edited = PLAN.format(title="Edited while waiting")

        async def another_build_saves_meanwhile(config, inputs, errors=(), *, call=None):
            store.save_epic(epic, theirs)
            store.write_files(epic, theirs)
            (epic / "plans" / "x.md").write_text(edited)
            return THREE

        with _split(another_build_saves_meanwhile):
            assert run_build(config, args_for(config)) == 3
        assert [c["id"] for c in json.loads((epic / "epic.json").read_text())["children"]] == ["x", "y"]
        assert sorted(p.name for p in (epic / "plans").iterdir()) == ["x.md", "y.md"]
        assert (epic / "plans" / "x.md").read_text() == edited
        assert [c.id for c in store.parse_split((epic / "split.md").read_text())] == ["x", "y"]
        assert load_state(config).epic_dir == str(epic)

    @pytest.mark.parametrize("lost_state", [False, True])
    def test_an_unparsable_epic_json_exits_1_and_is_kept(self, env, lost_state):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            run_build(config, args_for(config))
            (_epic_dir(data) / "epic.json").write_text("{")
            if lost_state:      # entered from the step itself (row A.S3's path)
                fresh = load_state(config)
                fresh.phase, fresh.epic_dir = BuildPhase.RESEARCH, None
                fresh.checkpoint(config.state_file_path())
            assert run_build(config, args_for(config)) == 1
        assert (_epic_dir(data) / "epic.json").read_text() == "{"


class TestPrompt:
    """The real `epic_split` body, with a stub for the model call."""

    def test_the_prompt_asks_for_every_mandatory_plan_section_and_the_retry_gets_the_errors(self, env):
        import asyncio
        import re

        root, data, project, log = env
        prompts = []

        async def call(prompt, schema, **kw):
            prompts.append(prompt)
            return SINGLE

        template = (Path(__file__).parents[3] / "plan" / "SKILL.md").read_text()
        template = template.split("## Template (mandatory sections)", 1)[1].split("```", 2)[1]
        sections = re.findall(r"^(#+ [^<\n]+|> File:|\*\*Objective\*\*)", template, re.M)
        assert "## Stop and resume states" in sections and "## Verification" in sections
        config = make_build(project)
        asyncio.run(epic_split_mod.epic_split(config, "INPUTS", call=call))
        asyncio.run(epic_split_mod.epic_split(config, "INPUTS", ["child a: no plan"], call=call))
        assert all(s.strip() in prompts[0] for s in sections), sections
        assert "INPUTS" in prompts[0] and "child a: no plan" not in prompts[0]
        assert "- child a: no plan" in prompts[1]


class TestApproval:
    def _proposed(self, env):
        root, data, project, log = env
        config = make_build(project)
        with _split(FakeSplit(log, THREE)):
            assert run_build(config, args_for(config)) == 3
        return config, _epic_dir(data)

    def test_approve_saves_hashes_and_prints_approved(self, env, capsys):
        config, epic = self._proposed(env)
        capsys.readouterr()
        rc = run_build(config, args_for(config, approve_split=True))
        assert rc == 0
        assert f"EPIC:APPROVED:{epic / 'epic.json'}" in capsys.readouterr().out
        saved = json.loads((epic / "epic.json").read_text())
        assert saved["state"] == "approved"
        assert set(saved["hashes"]) == {"split.md", "plans/api.md", "plans/ui.md", "plans/docs.md"}

    def test_a_hand_edited_plan_is_used(self, env):
        config, epic = self._proposed(env)
        edited = PLAN.format(title="Child api, edited")
        (epic / "plans" / "api.md").write_text(edited)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        saved = json.loads((epic / "epic.json").read_text())
        assert saved["children"][0]["plan"] == edited

    def test_a_hand_edited_table_is_used(self, env):
        config, epic = self._proposed(env)
        split_md = epic / "split.md"
        split_md.write_text(split_md.read_text().replace("| api, ui |", "| api |"))
        assert run_build(config, args_for(config, approve_split=True)) == 0
        saved = json.loads((epic / "epic.json").read_text())
        assert saved["children"][2]["depends_on"] == ["api"]

    def test_a_broken_plan_fails_approval_naming_it_and_saves_nothing(self, env, capsys):
        config, epic = self._proposed(env)
        (epic / "plans" / "ui.md").write_text("# Plan: UI\n")
        before = (epic / "epic.json").read_bytes()
        rc = run_build(config, args_for(config, approve_split=True))
        assert rc == 1
        assert "plans/ui.md" in capsys.readouterr().err
        assert (epic / "epic.json").read_bytes() == before

    def test_a_plan_changed_after_approval_exits_2(self, env, capsys):
        config, epic = self._proposed(env)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        (epic / "plans" / "docs.md").write_text(PLAN.format(title="Changed later"))
        capsys.readouterr()
        rc = run_build(config, args_for(config))
        assert rc == 2
        assert "plans/docs.md" in capsys.readouterr().err

    def test_an_approved_run_prints_approved_again(self, env, capsys):
        config, epic = self._proposed(env)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        capsys.readouterr()
        assert run_build(config, args_for(config)) == 0
        assert "EPIC:APPROVED:" in capsys.readouterr().out

    def test_the_repo_column_round_trips(self):
        children = [EpicChild(id="api", title="API | server", repo="org/other"),
                    EpicChild(id="ui", title="UI", depends_on=["api"])]
        parsed = store.parse_split(store.render_split(store.Epic(name="e", children=children)))
        assert parsed == children

    def test_a_repo_in_the_table_is_saved(self, env):
        config, epic = self._proposed(env)
        split_md = epic / "split.md"
        text = split_md.read_text()
        assert "| api | Child api | — | — |" in text
        split_md.write_text(text.replace("| api | Child api | — | — |", "| api | Child api | — | org/other |"))
        other = config.project_dir / "org" / "other"                   # PR F: a repo is a checkout with origin
        other.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/org/other.git"], cwd=other, check=True)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        saved = json.loads((epic / "epic.json").read_text())
        assert [c["repo"] for c in saved["children"]] == ["org/other", None, None]

    @pytest.mark.parametrize("damage", ["one row", "no table", "missing"])
    def test_a_split_md_that_is_not_an_epic_fails_approval_and_saves_nothing(self, env, capsys, damage):
        config, epic = self._proposed(env)
        split_md = epic / "split.md"
        if damage == "one row":
            split_md.write_text("".join(line for line in split_md.read_text().splitlines(keepends=True)
                                        if not line.startswith(("| ui |", "| docs |"))))
        elif damage == "no table":
            split_md.write_text("# Epic split: e\n")
        else:
            split_md.unlink()
        before = (epic / "epic.json").read_bytes()
        capsys.readouterr()
        assert run_build(config, args_for(config, approve_split=True)) == 1
        assert "split.md" in capsys.readouterr().err
        assert (epic / "epic.json").read_bytes() == before

    def test_an_approved_plan_deleted_exits_2_naming_it(self, env, capsys):
        config, epic = self._proposed(env)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        (epic / "plans" / "docs.md").unlink()
        capsys.readouterr()
        assert run_build(config, args_for(config)) == 2
        assert "plans/docs.md" in capsys.readouterr().err

    @pytest.mark.parametrize("edit", ["plan", "depends_on", "repo", "title", "child dropped",
                                      "hashes emptied", "hash dropped"])
    def test_an_epic_json_edited_after_approval_exits_2_naming_it(self, env, capsys, edit):
        """A.S8: epic.json is what B1's coordinator builds from, so after approval
        its children must still be the approved split.md and plans."""
        config, epic = self._proposed(env)
        assert run_build(config, args_for(config, approve_split=True)) == 0
        path = epic / "epic.json"
        saved = json.loads(path.read_text())
        if edit == "plan":
            saved["children"][0]["plan"] = PLAN.format(title="Not the approved plan")
        elif edit == "depends_on":
            saved["children"][2]["depends_on"] = []
        elif edit == "repo":
            saved["children"][1]["repo"] = "org/other"
        elif edit == "title":
            saved["children"][1]["title"] = "Another title"
        elif edit == "child dropped":
            saved["children"].pop()
        else:
            if edit == "hashes emptied":
                saved["hashes"] = {}
            else:
                del saved["hashes"]["plans/api.md"]
            (epic / "plans" / "api.md").write_text(PLAN.format(title="Changed later"))
        path.write_text(json.dumps(saved))
        edited = path.read_bytes()
        capsys.readouterr()
        assert run_build(config, args_for(config)) == 2
        assert "epic.json" in capsys.readouterr().err
        assert path.read_bytes() == edited

    def test_an_epic_json_set_to_approved_by_hand_is_refused(self, env, capsys):
        """Hand-setting `approved` skips neither `--approve-split` nor item 4's checks."""
        config, epic = self._proposed(env)
        path = epic / "epic.json"
        saved = json.loads(path.read_text())
        saved["state"] = "approved"
        path.write_text(json.dumps(saved))
        (epic / "plans" / "ui.md").write_text("no plan heading at all\n")
        edited = path.read_bytes()
        for flags in ({}, {"approve_split": True}):
            capsys.readouterr()
            assert run_build(config, args_for(config, **flags)) == 2
            assert "epic.json" in capsys.readouterr().err
            assert path.read_bytes() == edited

    def test_an_unparsable_epic_json_exits_1_and_is_never_rewritten(self, env):
        config, epic = self._proposed(env)
        (epic / "epic.json").write_text("{not json")
        assert run_build(config, args_for(config, approve_split=True)) == 1
        assert (epic / "epic.json").read_text() == "{not json"


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


# --- item 6: the lock ----------------------------------------------------------

HOLDER = r"""
import fcntl, json, os, socket, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "since": "then"}).encode(), 0)
print("locked", flush=True)
sys.stdin.read()
"""


def hold_in_other_process(lock_file: Path) -> subprocess.Popen:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_file)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


class TestLock:
    def test_a_second_hold_in_the_same_process_is_a_no_op(self, tmp_path):
        epic_lock.hold(tmp_path / "e", "e")
        epic_lock.hold(tmp_path / "e", "e")
        assert epic_lock.held(tmp_path / "e")

    def test_the_holder_writes_its_pid(self, tmp_path):
        import os

        epic_lock.hold(tmp_path / "e", "e")
        info = json.loads((tmp_path / "e" / ".lock").read_text())
        assert info["pid"] == os.getpid() and info["host"] and info["since"]

    def test_another_process_holding_it_is_refused_with_its_pid(self, tmp_path):
        proc = hold_in_other_process(tmp_path / "e" / ".lock")
        try:
            with pytest.raises(epic_lock.EpicLocked) as err:
                epic_lock.hold(tmp_path / "e", "e")
            assert str(err.value).startswith(f"epic e is running now (pid {proc.pid} on ")
        finally:
            proc.communicate("")

    def test_the_lock_is_not_inherited_by_a_child_process(self, tmp_path):
        import os

        epic_lock.hold(tmp_path / "e", "e")
        assert not os.get_inheritable(epic_lock._held[tmp_path / "e"])
        out = subprocess.run(
            [sys.executable, "-c",
             "import fcntl,os,sys\n"
             "fd=os.open(sys.argv[1],os.O_RDWR)\n"
             "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);print('got')\n"
             "except OSError:\n print('held')\n",
             str(tmp_path / "e" / ".lock")], capture_output=True, text=True)
        assert out.stdout.strip() == "held"
