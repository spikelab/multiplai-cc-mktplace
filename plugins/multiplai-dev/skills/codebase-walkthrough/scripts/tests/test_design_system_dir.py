"""`--design-system-dir`: the HTML page is styled with a design-system folder, checked before any model call.

Stop and resume rows of plan-design-system-2026-10-22:

- S2: a run stopped after `prepare` keeps its folder; `resume` without the option styles the page with it.
- S3: the saved folder is broken since: `resume` exits 1 before any model call and changes nothing;
  `resume --design-system-dir <good>` replaces it and finishes.
- S5: a folder edited between the run and `render`: `render` inlines the edited file when it passes the
  check, and exits 1 writing nothing when it does not.
- S7: a folder broken during a run fails the render stage (exit 1) with the write stage kept, so
  `render --design-system-dir <good>` writes the files for free.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import design_system
import pytest
from fakes import FakeAgents
from test_pipeline import run_args
from test_render_command import no_model  # noqa: F401  (a fixture)
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE

NEUTRAL_ACCENT = "--accent: #1f5fc4;"
NEUTRAL_TOKENS = (design_system.NEUTRAL / "tokens.css").read_text()


def _system(base: Path, name: str, accent: str) -> Path:
    """A copy of the neutral design system with its light accent changed to *accent* and a fonts.css."""
    folder = base / name
    shutil.copytree(design_system.NEUTRAL, folder)
    css = folder / "tokens.css"
    css.write_text(css.read_text().replace(NEUTRAL_ACCENT, f"--accent: {accent};"))
    (folder / "fonts.css").write_text(
        f'@import url("https://fonts.example/{name}.css");\n'
    )
    assert design_system.check_folder(folder) == []
    return folder


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    return _system(tmp_path / "systems", "acme", "#1a56b0")


@pytest.fixture
def globex(tmp_path: Path) -> Path:
    return _system(tmp_path / "systems", "globex", "#174f9f")


def _page(tmp_path: Path) -> str:
    return (tmp_path / "out" / "bookings-walkthrough.html").read_text()


def _break(folder: Path) -> None:
    css = folder / "tokens.css"
    css.write_text(css.read_text().replace("  --radius: 6px;\n", ""))


def test_without_the_option_the_page_uses_the_neutral_design_system(done_run):
    md_path, run_dir, _ = done_run
    page = md_path.with_suffix(".html").read_text()
    assert NEUTRAL_TOKENS in page
    assert "{{" not in page
    assert (
        json.loads((run_dir / STATE_FILE).read_text())["options"]["design_system_dir"]
        is None
    )


def test_run_styles_the_page_with_the_folder(
    ws, tmp_path, monkeypatch, fake_fetch, acme
):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert main(run_args(ws, tmp_path, "--design-system-dir", str(acme))) == 0
    page = _page(tmp_path)
    assert "--accent: #1a56b0;" in page
    assert page.index("fonts.example/acme.css") < page.index("--accent: #1a56b0;")
    assert NEUTRAL_TOKENS not in page
    # The Markdown does not change with the style.
    assert "#1a56b0" not in (tmp_path / "out" / "bookings-walkthrough.md").read_text()


@pytest.mark.parametrize("problem", ["missing", "broken"])
def test_a_bad_folder_stops_run_before_any_model_call(
    ws, tmp_path, monkeypatch, fake_fetch, acme, capsys, problem
):
    agents = FakeAgents()
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    folder = tmp_path / "nope" if problem == "missing" else acme
    if problem == "broken":
        _break(acme)
    assert main(run_args(ws, tmp_path, "--design-system-dir", str(folder))) == 1
    err = capsys.readouterr().err
    assert f"ERROR: design system {folder.resolve()}:" in err
    assert (
        "is not a directory" if problem == "missing" else "is missing --radius"
    ) in err
    assert agents.calls == []
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())
    assert not (tmp_path / "out").exists()


def _stopped_run(ws, tmp_path, monkeypatch, *extra) -> tuple[Path, FakeAgents]:
    agents = FakeAgents(cost=1.0)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--max-usd", "1.5", *extra)) == 2
    return next((tmp_path / "runs").iterdir()), agents


def test_resume_keeps_the_saved_folder(ws, tmp_path, monkeypatch, fake_fetch, acme):
    """S2."""
    run_dir, _ = _stopped_run(
        ws, tmp_path, monkeypatch, "--design-system-dir", str(acme)
    )
    saved = json.loads((run_dir / STATE_FILE).read_text())["options"][
        "design_system_dir"
    ]
    assert saved == str(acme.resolve())
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 0
    assert "--accent: #1a56b0;" in _page(tmp_path)


def test_resume_refuses_a_broken_saved_folder_and_takes_a_new_one(
    ws, tmp_path, monkeypatch, fake_fetch, acme, globex, capsys
):
    """S3."""
    run_dir, agents = _stopped_run(
        ws, tmp_path, monkeypatch, "--design-system-dir", str(acme)
    )
    _break(acme)
    state_before = (run_dir / STATE_FILE).read_bytes()
    asked = len(agents.calls)
    capsys.readouterr()
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 1
    assert "is missing --radius" in capsys.readouterr().err
    assert len(agents.calls) == asked
    assert (run_dir / STATE_FILE).read_bytes() == state_before

    args = ["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]
    assert main([*args, "--design-system-dir", str(globex)]) == 0
    assert "--accent: #174f9f;" in _page(tmp_path)
    saved = json.loads((run_dir / STATE_FILE).read_text())["options"][
        "design_system_dir"
    ]
    assert saved == str(globex.resolve())


def test_render_restyles_a_finished_run_for_free(done_run, no_model, acme, globex):  # noqa: F811
    md_path, run_dir, _ = done_run
    page = md_path.with_suffix(".html")
    md_before = md_path.read_bytes()
    state_before = (run_dir / STATE_FILE).read_bytes()
    assert main(["render", str(run_dir), "--design-system-dir", str(acme)]) == 0
    assert "--accent: #1a56b0;" in page.read_text()
    assert md_path.read_bytes() == md_before
    assert (run_dir / STATE_FILE).read_bytes() == state_before  # render saves nothing
    assert main(["render", str(run_dir), "--design-system-dir", str(globex)]) == 0
    assert "--accent: #174f9f;" in page.read_text()


def test_render_checks_an_edited_folder_and_writes_nothing_when_it_fails(
    done_run,
    no_model,  # noqa: F811
    acme,
    capsys,
):
    """S5."""
    md_path, run_dir, _ = done_run
    page = md_path.with_suffix(".html")
    css = acme / "tokens.css"
    css.write_text(css.read_text().replace("#1a56b0", "#164c9c"))
    assert main(["render", str(run_dir), "--design-system-dir", str(acme)]) == 0
    assert "--accent: #164c9c;" in page.read_text()

    before = {p.name: p.read_bytes() for p in md_path.parent.iterdir()}
    _break(acme)
    capsys.readouterr()
    assert main(["render", str(run_dir), "--design-system-dir", str(acme)]) == 1
    assert "render: design system" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in md_path.parent.iterdir()} == before


def test_a_folder_broken_during_a_run_keeps_the_write_and_page_stages(
    ws, tmp_path, monkeypatch, fake_fetch, acme, globex, capsys
):
    """S7: the folder passes the check at start and fails at the render stage, after the page stage."""
    from walkthrough_pipeline import orchestrator

    real = orchestrator.write_outputs

    def break_then_write(state, out_dir, *a, **kw):
        _break(acme)
        return real(state, out_dir, *a, **kw)

    monkeypatch.setattr(orchestrator, "write_outputs", break_then_write)
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert main(run_args(ws, tmp_path, "--design-system-dir", str(acme))) == 1
    assert "is missing --radius" in capsys.readouterr().err
    run_dir = next((tmp_path / "runs").iterdir())
    assert json.loads((run_dir / STATE_FILE).read_text())["stage"] == "page"
    assert not (tmp_path / "out" / "bookings-walkthrough.md").exists()

    monkeypatch.setattr(orchestrator, "write_outputs", real)
    assert main(["render", str(run_dir), "--design-system-dir", str(globex)]) == 0
    assert "--accent: #174f9f;" in _page(tmp_path)
