"""Tests for lint_skills.py.

A linter that passes on the current tree proves nothing on its own — the whole
value is in what it *catches*, so every check here is exercised against a
deliberately broken fixture tree. The calibration tests matter as much: the
first draft of the host-path check flagged 21 lines, all of them correct code,
which is how a gate gets disabled.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lint_skills import lint, parse_frontmatter  # noqa: E402


def make_repo(tmp_path: Path, *, skills=None, plugins=("myplug",)) -> Path:
    """Build a minimal but valid marketplace tree, then let tests break it."""
    repo = tmp_path / "repo"
    (repo / ".claude-plugin").mkdir(parents=True)
    for p in plugins:
        (repo / "plugins" / p / "skills").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "test",
        "plugins": [{"name": p, "source": f"./plugins/{p}"} for p in plugins],
    }))
    for name, body in (skills or {}).items():
        d = repo / "plugins" / plugins[0] / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body)
    return repo


VALID = """---
name: {name}
description: Does a thing.
model: opus
effort: medium
---

# Thing
"""


def errors_for(repo: Path) -> list[str]:
    return lint(repo).errors


class TestCleanTreePasses:
    def test_valid_tree_has_no_errors(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        assert errors_for(repo) == []

    def test_optional_model_and_effort_may_be_absent(self, tmp_path):
        repo = make_repo(tmp_path, skills={
            "alpha": "---\nname: alpha\ndescription: d\n---\n\n# A\n"})
        assert errors_for(repo) == []


class TestFrontmatter:
    def test_missing_frontmatter_is_an_error(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": "# No frontmatter\n"})
        assert any("no frontmatter" in e for e in errors_for(repo))

    def test_unclosed_frontmatter_is_an_error(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": "---\nname: alpha\n"})
        assert any("never closed" in e for e in errors_for(repo))

    @pytest.mark.parametrize("field", ["name", "description"])
    def test_required_fields(self, tmp_path, field):
        body = "---\nname: alpha\ndescription: d\n---\n\n# A\n".replace(
            f"{field}: ", f"x{field}: ")
        repo = make_repo(tmp_path, skills={"alpha": body})
        assert any(f"missing required field '{field}'" in e for e in errors_for(repo))

    def test_name_must_match_directory(self, tmp_path):
        """A mismatch means the catalog and the slash-command disagree."""
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="beta")})
        assert any("!= directory" in e for e in errors_for(repo))

    def test_unknown_model_is_caught(self, tmp_path):
        body = VALID.format(name="alpha").replace("model: opus", "model: sonnet-4")
        repo = make_repo(tmp_path, skills={"alpha": body})
        assert any("unknown model" in e for e in errors_for(repo))

    def test_unknown_effort_is_caught(self, tmp_path):
        body = VALID.format(name="alpha").replace("effort: medium", "effort: ultra")
        repo = make_repo(tmp_path, skills={"alpha": body})
        assert any("unknown effort" in e for e in errors_for(repo))

    def test_quoted_values_are_unquoted(self):
        fields, err = parse_frontmatter('---\nname: "alpha"\ndesc: \'x\'\n---\n')
        assert err == ""
        assert fields is not None and fields["name"] == "alpha" and fields["desc"] == "x"

    def test_unquoted_value_with_colon_space_is_an_error(self, tmp_path):
        """Real YAML reads `Default window: last 7 days` as a nested mapping.

        Found in the wild in backfill's SKILL.md: the frontmatter failed to
        load entirely, which this lenient parser happily ignored until the
        check was added.
        """
        body = ("---\nname: alpha\n"
                "description: Does a thing. Default window: last 7 days.\n"
                "---\n\n# A\n")
        repo = make_repo(tmp_path, skills={"alpha": body})
        assert any("unquoted" in e for e in errors_for(repo))

    def test_quoted_value_with_colon_space_is_fine(self, tmp_path):
        body = ('---\nname: alpha\n'
                'description: "Does a thing. Default window: last 7 days."\n'
                "---\n\n# A\n")
        repo = make_repo(tmp_path, skills={"alpha": body})
        assert errors_for(repo) == []

    def test_multiline_continuation_joins(self):
        fields, _ = parse_frontmatter(
            "---\nname: a\ndescription: one\n  two\n---\n")
        assert fields is not None and fields["description"] == "one two"


class TestScriptReferences:
    def _skill_with_ref(self, ref):
        return VALID.format(name="alpha") + f"\nRun `${{CLAUDE_PLUGIN_ROOT}}{ref}`\n"

    def test_broken_reference_is_caught(self, tmp_path):
        repo = make_repo(tmp_path, skills={
            "alpha": self._skill_with_ref("/scripts/missing.py")})
        assert any("broken script reference" in e for e in errors_for(repo))

    def test_resolving_reference_passes(self, tmp_path):
        repo = make_repo(tmp_path, skills={
            "alpha": self._skill_with_ref("/scripts/real.py")})
        s = repo / "plugins" / "myplug" / "scripts"
        s.mkdir(parents=True)
        (s / "real.py").write_text("print(1)\n")
        assert errors_for(repo) == []

    def test_placeholder_references_are_skipped(self, tmp_path):
        """`/skills/<name>/scripts/` is documentation, not a real path."""
        repo = make_repo(tmp_path, skills={
            "alpha": self._skill_with_ref("/skills/<name>/scripts/x.py")})
        assert errors_for(repo) == []

    def test_trailing_punctuation_is_stripped(self, tmp_path):
        repo = make_repo(tmp_path, skills={
            "alpha": VALID.format(name="alpha")
            + "\nSee ${CLAUDE_PLUGIN_ROOT}/scripts/real.py.\n"})
        s = repo / "plugins" / "myplug" / "scripts"
        s.mkdir(parents=True)
        (s / "real.py").write_text("x\n")
        assert errors_for(repo) == []


class TestHostPaths:
    def test_author_home_path_in_runtime_file_is_caught(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / "plugins" / "myplug" / "scripts").mkdir(parents=True)
        (repo / "plugins" / "myplug" / "scripts" / "go.py").write_text(
            'P = "/Users/someone/Documents/knowhere"\n')
        assert any("absolute host path" in e for e in errors_for(repo))

    def test_container_home_is_not_flagged(self, tmp_path):
        """`/home/agent` is the container's own home — correct and portable.

        Flagging it produced 15 false positives against real, correct code on
        the first run; a gate that cries wolf gets switched off.
        """
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / "plugins" / "myplug" / "scripts").mkdir(parents=True)
        (repo / "plugins" / "myplug" / "scripts" / "go.sh").write_text(
            'for c in /home/agent/.ssh/build_key "$HOME/.ssh/build_key"; do :; done\n')
        assert errors_for(repo) == []

    def test_test_fixtures_are_exempt(self, tmp_path):
        """Tests use host paths as synthetic strings, not as runtime paths."""
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        t = repo / "plugins" / "myplug" / "tests"
        t.mkdir(parents=True)
        (t / "test_x.py").write_text('assert norm("/Users/spike/a.md")\n')
        assert errors_for(repo) == []


class TestMarketplaceManifest:
    def test_unlisted_plugin_directory_is_caught(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / "plugins" / "ghost" / "skills").mkdir(parents=True)
        assert any("is not listed" in e for e in errors_for(repo))

    def test_listed_plugin_without_a_directory_is_caught(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        manifest = repo / ".claude-plugin" / "marketplace.json"
        data = json.loads(manifest.read_text())
        data["plugins"].append({"name": "phantom", "source": "./plugins/phantom"})
        manifest.write_text(json.dumps(data))
        errs = errors_for(repo)
        assert any("no directory under plugins/" in e for e in errs)

    def test_invalid_json_is_reported_not_raised(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / ".claude-plugin" / "marketplace.json").write_text("{nope")
        assert any("invalid JSON" in e for e in errors_for(repo))

    def test_missing_manifest_is_reported(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / ".claude-plugin" / "marketplace.json").unlink()
        assert any("manifest is missing" in e for e in errors_for(repo))


class TestSdkComesFromCore:
    """The duplicate-declaration bug this check exists for.

    deep-research declared `multiplai-core` (no extra) plus a bare
    `claude-agent-sdk>=0.1.0`. Core's own floor lives inside its `[sdk]` extra,
    so nothing activated it and the resolver picked 0.1.56 — the line that
    misparses terminal result messages and raises `Claude Code returned an
    error result: success` after a full generation. The specs agreeing today
    is not what makes a tree safe; not having two of them is.
    """

    def _with_manifest(self, tmp_path, body: str, name="pyproject.toml"):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        (repo / "plugins" / "myplug" / "scripts").mkdir(parents=True, exist_ok=True)
        (repo / "plugins" / "myplug" / "scripts" / name).write_text(body)
        return repo

    def test_bare_sdk_alongside_core_is_caught(self, tmp_path):
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = [\n'
            '  "multiplai-core @ git+https://example.com/c@v0.11.0",\n'
            '  "claude-agent-sdk>=0.2.116,<0.3",\n]\n'
        ))
        assert any("directly alongside multiplai-core" in e for e in errors_for(repo))

    def test_matching_version_specs_do_not_excuse_the_duplicate(self, tmp_path):
        """Even the [sdk] extra plus an identical local pin is an error —
        the second copy is what drifts when core bumps its floor."""
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = [\n'
            '  "multiplai-core[sdk] @ git+https://example.com/c@v0.11.0",\n'
            '  "claude-agent-sdk>=0.2.116,<0.3",\n]\n'
        ))
        assert any("directly alongside multiplai-core" in e for e in errors_for(repo))

    def test_sdk_extra_alone_is_clean(self, tmp_path):
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = [\n'
            '  "multiplai-core[sdk] @ git+https://example.com/c@v0.11.0",\n'
            '  "pydantic>=2.0",\n]\n'
        ))
        assert errors_for(repo) == []

    def test_sdk_hiding_in_an_optional_extra_is_caught(self, tmp_path):
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = ["multiplai-core"]\n'
            '[project.optional-dependencies]\n'
            'agent = ["claude-agent-sdk>=0.2.116"]\n'
        ))
        assert any("directly alongside multiplai-core" in e for e in errors_for(repo))

    def test_pep723_inline_metadata_is_checked_too(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        s = repo / "plugins" / "myplug" / "scripts"
        s.mkdir(parents=True, exist_ok=True)
        (s / "run.py").write_text(
            "# /// script\n"
            '# dependencies = ["multiplai-core", "claude-agent-sdk>=0.1.0"]\n'
            "# ///\nprint(1)\n"
        )
        assert any("directly alongside multiplai-core" in e for e in errors_for(repo))

    def test_pep723_with_the_extra_is_clean(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        s = repo / "plugins" / "myplug" / "scripts"
        s.mkdir(parents=True, exist_ok=True)
        (s / "run.py").write_text(
            "# /// script\n"
            '# dependencies = ["multiplai-core[sdk] @ git+https://e.com/c@v0.12.0"]\n'
            "# ///\nprint(1)\n"
        )
        assert errors_for(repo) == []

    def test_distribution_name_is_normalised(self, tmp_path):
        """`multiplai_core` and `Claude_Agent_SDK` are the same distributions."""
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = [\n'
            '  "multiplai_core",\n  "Claude_Agent_SDK>=0.1.0",\n]\n'
        ))
        assert any("directly alongside multiplai-core" in e for e in errors_for(repo))

    def test_sdk_without_core_is_not_this_checks_business(self, tmp_path):
        """A standalone script that never touches core is out of scope —
        a narrow rule that never false-positives is one that stays enabled."""
        repo = self._with_manifest(tmp_path, (
            '[project]\nname = "p"\ndependencies = ["claude-agent-sdk>=0.2.116"]\n'
        ))
        assert errors_for(repo) == []

    def test_test_fixtures_are_exempt(self, tmp_path):
        repo = make_repo(tmp_path, skills={"alpha": VALID.format(name="alpha")})
        t = repo / "plugins" / "myplug" / "tests"
        t.mkdir(parents=True)
        (t / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\n'
            'dependencies = ["multiplai-core", "claude-agent-sdk>=0.1.0"]\n'
        )
        assert errors_for(repo) == []

    def test_malformed_manifest_is_skipped_not_raised(self, tmp_path):
        repo = self._with_manifest(tmp_path, "[project\nnope = ")
        assert errors_for(repo) == []


class TestRealTree:
    def test_the_shipped_marketplace_is_clean(self):
        """The gate must pass on what we actually publish."""
        repo = Path(__file__).resolve().parent.parent.parent
        if not (repo / "plugins").is_dir():
            pytest.skip("not running inside the marketplace repo")
        assert errors_for(repo) == []
