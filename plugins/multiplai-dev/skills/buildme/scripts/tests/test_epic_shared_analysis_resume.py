"""One codebase analysis per epic (`epic.shared_analysis`; item 63 of
plan-factory-merged-2026-10-29.md) and its stop and resume rows S41 and S42.

Off (the default), every child runs its own analysis exactly as before: the
coordinator passes no `--shared-analysis`, writes no shared directory, and a
child's `run_codebase_analysis` calls `code_analysis.analyse` as it always
did. On, the coordinator runs the analysis once at the feature branch before
the first launch, and each child reuses it.

No model is called: the analysis agents are the fake call function of
`test_code_analysis`, which answers from the file list in the prompt.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from build_pipeline import code_analysis
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, records, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.llm_steps import spec_steps
from build_pipeline.llm_steps.spec_steps import run_codebase_analysis

from .epic_row_helpers import (
    THREE,
    CallLog,
    FakeClock,
    Fakes,
    approve,
    make_build,
    make_project,
    run_coordinator,
    use_workspace,
)
from .test_code_analysis import CALC, CLI, _config, _fake_call, _git, _repo


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _files(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _facts(directory: Path) -> dict:
    """`facts.json` without the time it was written."""
    doc = json.loads((directory / code_analysis.FACTS_FILE).read_text())
    doc.pop("generated_at")
    return doc


def _settings(project: Path) -> dict:
    return code_analysis.shared_settings(_config(project))


def _shared(project: Path, directory: Path, seen: list | None = None) -> dict:
    return asyncio.run(
        code_analysis.run_shared(
            directory,
            project,
            call=_fake_call(seen),
            settings=_settings(project),
            name="calc",
            named_text="",
        )
    )


def _commit(repo: Path, rel: str, text: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", rel)
    _git(repo, "commit", "-qm", f"edit {rel}")
    return _git(repo, "rev-parse", "HEAD").strip()


# --- the setting ----------------------------------------------------------------------


def test_the_setting_is_off_unless_config_yaml_turns_it_on(tmp_path):
    assert EpicSettings().shared_analysis is False
    config = _config(_repo(tmp_path / "p", {"a.py": "x = 1\n"}))
    config._load_epic({})
    assert config.epic.shared_analysis is False
    config._load_epic({"shared_analysis": "yes"})
    assert config.epic.shared_analysis is False
    config._load_epic({"shared_analysis": True})
    assert config.epic.shared_analysis is True


# --- off: every child analyses for itself, as on main ------------------------------------


class Epic:
    """Three independent children from an approved split, as `test_epic_run`'s."""

    def __init__(self, root: Path, monkeypatch, shared: bool):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, None)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(
            max_parallel=2, merge="manual", shared_analysis=shared
        )
        self.specs = approve(self.config, THREE, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.shared_dir = self.dir / code_analysis.SHARED_DIR

    def run(self) -> int:
        return run_coordinator(self.config)

    def builds(self) -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.kind == "build"]

    def analyses(self) -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.kind == "analysis"]


def test_off_every_child_runs_its_own_analysis_as_on_main(tmp_path, monkeypatch):
    """With `epic.shared_analysis` off, no child is given `--shared-analysis`,
    the coordinator writes no shared directory and runs no analysis, and a
    child's analysis reads every source file as it did before the setting
    existed, even when a finished shared analysis is on disk."""
    e = Epic(tmp_path, monkeypatch, shared=False)
    asked, run_shared = [], code_analysis.run_shared

    async def never(*a, **k):
        asked.append(a)
        raise AssertionError("the coordinator ran an analysis with the setting off")

    monkeypatch.setattr(code_analysis, "run_shared", never)
    assert e.run() == 3
    assert e.builds() and asked == [] and e.analyses() == []
    for a in e.builds():
        assert "--shared-analysis" not in a.started["argv"]
    assert not e.shared_dir.exists()
    monkeypatch.setattr(code_analysis, "run_shared", run_shared)

    # The child: no flag means `analyse` over the whole project, as before.
    project = _repo(tmp_path / "proj", {"src/calc.py": CALC, "src/cli.py": CLI})
    _shared(project, tmp_path / "unused-shared")  # on disk, never read
    config = _config(project)
    assert config.shared_analysis_dir is None
    calls, reused, real = [], [], code_analysis.analyse

    async def analyse(*a, **k):
        calls.append(k)
        return await real(*a, **k)

    async def reuse(*a, **k):
        reused.append(a)

    monkeypatch.setattr(code_analysis, "analyse", analyse)
    monkeypatch.setattr(code_analysis, "reuse_shared", reuse)
    seen = []
    md = asyncio.run(run_codebase_analysis(project, config, call=_fake_call(seen)))
    assert reused == [] and len(calls) == 1
    assert "only" not in calls[0] and "named_text" not in calls[0]
    assert calls[0]["change_dir"] == config.change_dir
    assert any("- src/calc.py" in p and "- src/cli.py" in p for p, _ in seen)
    assert "`src/cli.py:5`" in md and "`src/calc.py:8`" in md


def test_off_a_saved_command_with_the_flag_loses_it(tmp_path, monkeypatch):
    """A child relaunched from a command saved while the setting was on gets
    no `--shared-analysis` once it is off: only the current setting adds it."""
    e = Epic(tmp_path, monkeypatch, shared=False)
    coord = coordinator.Coordinator(
        e.config,
        None,
        e.dir,
        store.Epic(
            name="e",
            children=[
                store.EpicChild(id=c.id, depends_on=list(c.depends_on))
                for c in THREE.children
            ],
        ),
        "ENG-40-e",
    )
    c = coord.children["api"]
    c.argv = coord.new_argv("api") + ["--shared-analysis", str(e.shared_dir)]
    c.status = "failed"
    with patch.object(coordinator, "CLOCK", FakeClock()):
        assert asyncio.run(coord._launch(c)) is True
    coord.children["api"].proc.wait()
    assert "--shared-analysis" not in e.builds()[-1].started["argv"]
    assert e.builds()[-1].started["argv"] == coord.new_argv("api")


# --- on: one analysis, before any child ----------------------------------------------------


def _fake_analysis(monkeypatch, calls: list):
    """`_codebase_analysis_call` as the fake agent, counting the calls."""

    async def call(prompt, schema, *, config, **kwargs):
        calls.append(prompt)
        return await _fake_call()(prompt, schema, **kwargs)

    monkeypatch.setattr(spec_steps, "_codebase_analysis_call", call)


def test_on_the_analysis_runs_once_before_the_first_child(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, shared=True)
    _fake_analysis(monkeypatch, [])
    assert e.run() == 3
    marker = code_analysis.shared_marker(e.shared_dir)
    assert marker is not None and marker["sha"] == e.specs
    assert marker["settings"] == code_analysis.shared_settings(e.config)
    [analysis] = e.analyses()
    assert analysis.result["exit_code"] == 0
    assert all(b.n > analysis.n for b in e.builds())
    for b in e.builds():
        argv = b.started["argv"]
        assert argv[argv.index("--shared-analysis") + 1] == str(e.shared_dir)


# --- S41: killed mid-analysis --------------------------------------------------------------


def test_s41_a_killed_analysis_runs_again_from_the_start(tmp_path, monkeypatch):
    """S41: a run killed after `facts.json` and before its marker leaves a
    partial directory. A child given that directory reads none of it and
    analyses for itself; the next run deletes it, asks every unit again and
    writes what an uninterrupted run writes."""
    project = _repo(tmp_path / "proj", {"src/calc.py": CALC, "src/cli.py": CLI})
    whole = tmp_path / "whole"
    _shared(project, whole)

    directory = tmp_path / "shared"
    real = code_analysis.render_markdown

    def killed(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(code_analysis, "render_markdown", killed)
    with pytest.raises(KeyboardInterrupt):
        _shared(project, directory)
    monkeypatch.setattr(code_analysis, "render_markdown", real)
    assert (directory / code_analysis.FACTS_FILE).is_file()
    assert code_analysis.shared_marker(directory) is None

    # A child pointed at the partial directory analyses for itself.
    config = _config(project, shared_analysis_dir=directory)
    reused = []
    monkeypatch.setattr(code_analysis, "reuse_shared", lambda *a, **k: reused.append(a))
    seen = []
    asyncio.run(run_codebase_analysis(project, config, call=_fake_call(seen)))
    assert reused == [] and seen

    # The next run starts again: every unit is asked, and the result is the uninterrupted one.
    (directory / "stray.txt").write_text("left by the killed run\n")
    asked = []
    marker = _shared(project, directory, asked)
    assert asked and marker == code_analysis.shared_marker(whole)
    assert not (directory / "stray.txt").exists()
    assert _facts(directory) == _facts(whole)
    assert sorted(_files(directory)) == sorted(_files(whole))


def test_s41_the_coordinator_replaces_a_partial_directory_before_any_launch(
    tmp_path, monkeypatch
):
    e = Epic(tmp_path, monkeypatch, shared=True)
    _fake_analysis(monkeypatch, [])
    e.shared_dir.mkdir(parents=True)
    (e.shared_dir / code_analysis.FACTS_FILE).write_text("{}\n")
    assert e.run() == 3
    assert code_analysis.shared_marker(e.shared_dir)["sha"] == e.specs
    assert _facts(e.shared_dir)["head_sha"] == e.specs
    [analysis] = e.analyses()
    assert all(b.n > analysis.n for b in e.builds())


# --- S42: the feature branch moved after the analysis --------------------------------------


def test_s42_a_child_rechecks_only_the_facts_whose_cited_files_changed(
    tmp_path, monkeypatch
):
    """S42: the analysis was made at an older commit. The child re-checks the
    citations in files that changed since (calc.py, whose lines moved), keeps
    the facts about unchanged files (cli.py) without checking them, and
    writes nothing into the shared directory."""
    project = _repo(tmp_path / "proj", {"src/calc.py": CALC, "src/cli.py": CLI})
    directory = tmp_path / "shared"
    marker = _shared(project, directory)
    before = _files(directory)
    old = marker["sha"]
    _commit(project, "src/calc.py", "import os\n\n\n" + CALC)

    checked, real = [], code_analysis.check_quote

    def spy(root, sha, path, *a):
        checked.append(path)
        return real(root, sha, path, *a)

    monkeypatch.setattr(code_analysis, "check_quote", spy)
    config = _config(project, shared_analysis_dir=directory)
    seen = []
    md = asyncio.run(run_codebase_analysis(project, config, call=_fake_call(seen)))

    assert seen == []  # no file was analysed again
    assert checked and set(checked) == {"src/calc.py"}
    assert "`src/cli.py:5`" in md  # unchanged file: kept
    assert "`src/calc.py:8`" not in md  # its quoted line moved: dropped
    doc = json.loads((config.change_dir / code_analysis.FACTS_FILE).read_text())
    assert doc["head_sha"] != old
    assert all(c["path"] != "src/calc.py" for f in doc["facts"] for c in f["citations"])
    assert any(
        d["stage"] == "shared" and old[:12] in d["reason"] for d in doc["dropped"]
    )
    assert _files(directory) == before


def test_s42_the_coordinator_keeps_an_analysis_from_an_older_commit(
    tmp_path, monkeypatch
):
    """S42 at the coordinator: a finished analysis with the current settings
    is not run again, nor rewritten, when the feature branch moved since;
    every child is given it."""
    e = Epic(tmp_path, monkeypatch, shared=True)
    calls = []
    _fake_analysis(monkeypatch, calls)
    e.shared_dir.mkdir(parents=True)
    (e.shared_dir / code_analysis.FACTS_FILE).write_text('{"facts": []}\n')
    (e.shared_dir / code_analysis.SHARED_MARKER).write_text(
        json.dumps(
            {
                "sha": "0" * 40,
                "settings": code_analysis.shared_settings(e.config),
                "read": {},
            }
        )
    )
    before = _files(e.shared_dir)
    assert e.run() == 3
    assert e.analyses() == [] and calls == []
    assert _files(e.shared_dir) == before
    for b in e.builds():
        assert str(e.shared_dir) in b.started["argv"]


def test_a_child_analyses_only_the_files_its_plan_names_that_were_not_read(
    tmp_path, monkeypatch
):
    """cli.py was added after the shared analysis, and the child's plan.md
    names it: only cli.py is analysed in the child, and its facts join the
    shared ones."""
    project = _repo(tmp_path / "proj", {"src/calc.py": CALC})
    directory = tmp_path / "shared"
    _shared(project, directory)
    _commit(project, "src/cli.py", CLI)
    config = _config(project, shared_analysis_dir=directory)
    (config.change_dir / "plan.md").write_text("# Plan: x\n\nEdit `src/cli.py`.\n")
    seen = []
    md = asyncio.run(run_codebase_analysis(project, config, call=_fake_call(seen)))
    assert len(seen) == 1 and "- src/cli.py" in seen[0][0]
    assert "- src/calc.py" not in seen[0][0]
    assert "`src/cli.py:5`" in md and "`src/calc.py:8`" in md
    doc = json.loads((config.change_dir / code_analysis.FACTS_FILE).read_text())
    ids = [f["id"] for f in doc["facts"]]
    assert ids == [f"f{i + 1}" for i in range(len(ids))]
    assert any(f["source"].startswith("child:") for f in doc["facts"])
    if "## Not analysed" in md:
        assert "`src/cli.py`" not in md.split("## Not analysed")[-1]


def test_a_child_whose_settings_differ_analyses_for_itself(tmp_path):
    project = _repo(tmp_path / "proj", {"src/calc.py": CALC, "src/cli.py": CLI})
    directory = tmp_path / "shared"
    _shared(project, directory)
    config = _config(
        project, shared_analysis_dir=directory, codebase_analysis_max_units=3
    )
    seen = []
    asyncio.run(run_codebase_analysis(project, config, call=_fake_call(seen)))
    assert seen  # analysed here, not reused


def test_the_child_command_carries_the_directory(tmp_path):
    from build_pipeline.__main__ import build_parser

    argv = ["build", "--change", "x", "--project-dir", str(tmp_path)]
    assert (
        BuildConfig.from_cli_args(build_parser().parse_args(argv)).shared_analysis_dir
        is None
    )
    parsed = build_parser().parse_args(
        argv + ["--shared-analysis", str(tmp_path / "s")]
    )
    assert BuildConfig.from_cli_args(parsed).shared_analysis_dir == tmp_path / "s"


def test_the_shared_analysis_spend_counts_in_the_epic(tmp_path):
    epic_dir = tmp_path / "epic"
    a = records.start(epic_dir, "analysis", "shared", ["codebase-analysis"])
    records.finish(a, 0, interrupted=False, extra={"spend": 1.5})
    from build_pipeline.epic import spend

    assert spend.attempts_spend(records.attempts(epic_dir)) == (1.5, True)
