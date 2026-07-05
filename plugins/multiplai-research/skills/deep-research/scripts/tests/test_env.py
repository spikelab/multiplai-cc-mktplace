"""The env/config helpers now live in multiplai_core.env; research_pipeline.env
re-exports them. The exhaustive behavior tests live in multiplai-core's own
suite — here we just verify the re-export surface and a couple of end-to-end
delegations so a broken import is caught."""

from pathlib import Path

from research_pipeline import env as rp_env
from research_pipeline.env import (
    find_project_root,
    load_env,
    load_multiplai_conf,
    resolve_effort,
    resolve_model,
)


def test_reexports_delegate_to_core():
    import multiplai_core.env as core_env
    assert rp_env.find_project_root is core_env.find_project_root
    assert rp_env.load_env is core_env.load_env
    assert rp_env.load_multiplai_conf is core_env.load_multiplai_conf
    assert rp_env.resolve_model is core_env.resolve_model
    assert rp_env.resolve_effort is core_env.resolve_effort


def test_resolve_model_ceiling():
    assert resolve_model("claude-opus-4", "claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert resolve_model("claude-haiku-4", "claude-sonnet-4-6") == "claude-haiku-4"


def test_resolve_effort_ceiling():
    assert resolve_effort("max", "medium") == "medium"


def test_find_project_root_kit_marker(tmp_path: Path):
    (tmp_path / ".env.example").write_text("")
    (tmp_path / "dotfiles").mkdir()
    sub = tmp_path / "a"
    sub.mkdir()
    assert find_project_root(sub) == tmp_path


def test_load_multiplai_conf_sections(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path))
    (tmp_path / "multiplai.conf").write_text(
        'MULTIPLAI_MODEL="claude-opus-4-6"\n[deep-research]\nMODEL=sonnet\n'
    )
    conf = load_multiplai_conf()
    assert conf["MULTIPLAI_MODEL"] == "claude-opus-4-6"
    assert conf["_sections"]["deep-research"]["MODEL"] == "sonnet"


def test_load_env_returns_false_when_none_found(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path))
    monkeypatch.setenv("MULTIPLAI_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.chdir(tmp_path)
    assert load_env() is False
