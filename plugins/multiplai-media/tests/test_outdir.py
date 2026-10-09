"""Output location (stages/outdir.py): workspace INBOX/ if it exists, else cwd."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import outdir  # noqa: E402


def test_workspace_inbox_when_it_exists(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".workspace").write_text(f"{ws}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert outdir.job_dir("My Show / ep 4") == ws / "INBOX" / "video-edit" / "My-Show-ep-4"


def test_current_directory_without_a_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nothing"))
    monkeypatch.chdir(tmp_path)
    assert outdir.job_dir("reels") == tmp_path / "video-edit" / "reels"


def test_workspace_without_inbox_falls_back_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".workspace").write_text(str(ws))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    assert outdir.base_dir() == tmp_path
    assert not (ws / "INBOX").exists()


def test_clip_edl_paths_are_numbered() -> None:
    assert outdir.clip_edl_path(Path("/o"), 3) == Path("/o/edl/clip-03.edl.json")
