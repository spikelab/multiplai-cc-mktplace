"""A plugin-shipped skill must be suggested as /plugin:skill, not /skill.

Regression cover for the sole cause of a measured 23.2% Skill-tool failure
rate (2026-08-07 audit of 111,780 tool calls): the routing hook suggested
`Invoke with /<name>` using the catalog's source key — the skill *directory*
name — so every plugin skill it recommended was recommended under a name the
Skill tool rejects with "Unknown skill".
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib.plugin_skills import (  # noqa: E402
    plugin_skill_owners,
    plugin_skills,
    qualify,
)


def _make_plugins_dir(tmp_path: Path, layout: dict[str, list[str]]) -> Path:
    """Build a plugins dir: {"pack@marketplace": ["skill-a", "skill-b"]}."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    manifest: dict[str, list[dict]] = {}
    for qualified_key, skills in layout.items():
        install = plugins_dir / "cache" / qualified_key.replace("@", "_") / "1.0.0"
        for skill in skills:
            skill_dir = install / "skills" / skill
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        manifest[qualified_key] = [{"scope": "user", "installPath": str(install)}]
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": manifest}), encoding="utf-8"
    )
    return plugins_dir


class TestPluginSkillOwners:
    def test_maps_skill_to_its_plugin(self, tmp_path):
        plugins_dir = _make_plugins_dir(
            tmp_path,
            {
                "multiplai-research@multiplai": ["extract-insights", "deep-research"],
                "multiplai-media@multiplai": ["youtube-transcript"],
            },
        )
        owners = plugin_skill_owners(plugins_dir)
        assert owners == {
            "extract-insights": "multiplai-research",
            "deep-research": "multiplai-research",
            "youtube-transcript": "multiplai-media",
        }

    def test_returns_path_alongside_owner(self, tmp_path):
        plugins_dir = _make_plugins_dir(tmp_path, {"pack@mkt": ["thing"]})
        path, plugin = plugin_skills(plugins_dir)["thing"]
        assert plugin == "pack"
        assert path.name == "SKILL.md" and path.parent.name == "thing"

    @pytest.mark.parametrize(
        "broken",
        ["not json at all", json.dumps({"plugins": "wrong type"}), json.dumps({})],
    )
    def test_malformed_manifest_yields_empty(self, tmp_path, broken):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "installed_plugins.json").write_text(broken, encoding="utf-8")
        assert plugin_skill_owners(plugins_dir) == {}

    def test_missing_manifest_yields_empty(self, tmp_path):
        assert plugin_skill_owners(tmp_path / "nope") == {}


class TestQualify:
    def test_plugin_skill_gets_its_prefix(self):
        owners = {"extract-insights": "multiplai-research"}
        assert qualify("extract-insights", owners) == "multiplai-research:extract-insights"

    def test_user_local_skill_stays_bare(self):
        # A skill in the user's own skills_dir really is invoked as /<name>.
        assert qualify("my-local-skill", {"other": "pack"}) == "my-local-skill"

    def test_local_skill_shadowing_a_plugin_skill_stays_bare(self, tmp_path):
        # The catalog generator lets a local skill override a plugin skill of
        # the same name; the hint must agree, or it advertises the wrong one.
        local = tmp_path / "skills" / "writing"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text("# writing\n", encoding="utf-8")
        owners = {"writing": "multiplai-writing"}
        assert qualify("writing", owners, tmp_path / "skills") == "writing"
        assert qualify("writing", owners, tmp_path / "elsewhere") == (
            "multiplai-writing:writing"
        )

    def test_already_qualified_is_untouched(self):
        owners = {"extract-insights": "multiplai-research"}
        assert qualify("multiplai-research:extract-insights", owners) == (
            "multiplai-research:extract-insights"
        )

    def test_no_owners_at_all_is_safe(self):
        assert qualify("anything", None) == "anything"
        assert qualify("", {}) == ""


class TestRenderedHint:
    """The hook's rendered suggestion is what the model actually copies."""

    def test_hint_names_the_invocable_identifier(self, tmp_path, monkeypatch):
        plugins_dir = _make_plugins_dir(
            tmp_path, {"multiplai-research@multiplai": ["extract-insights"]}
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        import context_manager

        cfg = SimpleNamespace(enable_skills=True, plugins_dir=str(plugins_dir))

        out = context_manager._build_skills_recommendations(
            cfg,
            ["extract-insights"],
            [{"source": "extract-insights", "summary": "Pulls insights from text."}],
        )
        hint = out["extract-insights"]
        assert "/multiplai-research:extract-insights" in hint
        # The bare form is exactly what used to be emitted and what fails.
        assert "/extract-insights" not in hint.replace(
            "/multiplai-research:extract-insights", ""
        )
