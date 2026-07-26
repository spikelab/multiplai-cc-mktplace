"""Tests for check_changelog.py.

A release-notes gate that only ever passes is indistinguishable from no gate,
so the cases that matter are the ones where it fires: behaviour changed with no
bump, a bump with no notes, notes with no bump. The exemptions get the same
attention, because a gate that fires on a README typo is a gate someone
disables.

`check()` is a pure function over an already-computed diff, so most of this
needs no git. The two `collect()` tests build a real repository, because the
`base...head` merge-base semantics are the part most easily got wrong.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from check_changelog import (  # noqa: E402
    GateError,
    check,
    collect,
    main,
    plugin_of,
    versions,
)

BASE = {"myplug": "0.1.0", "other": "1.0.0"}


def manifest(versions_map: dict[str, str]) -> str:
    return json.dumps({
        "name": "test",
        "plugins": [{"name": n, "source": f"./plugins/{n}", "version": v}
                    for n, v in versions_map.items()],
    })


# --- the gate fires ---------------------------------------------------------

def test_behaviour_change_with_no_bump_and_no_notes_fails():
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, BASE)
    assert not f.ok
    assert len(f.failures) == 1
    msg = f.failures[0]
    assert "myplug" in msg
    assert "SKILL.md" in msg          # names what changed
    assert "marketplace.json" in msg  # names the file to edit
    assert "CHANGELOG.md" in msg
    assert "no-changelog" in msg      # names the escape hatch


def test_bump_without_notes_fails():
    head = {**BASE, "myplug": "0.2.0"}
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, head)
    assert not f.ok
    assert "0.1.0 -> 0.2.0" in f.failures[0]


def test_notes_without_bump_fails():
    f = check(
        ["plugins/myplug/skills/thing/SKILL.md",
         "plugins/myplug/CHANGELOG.md"],
        BASE, BASE)
    assert not f.ok
    assert "bump" in f.failures[0]


def test_each_offending_plugin_is_reported_separately():
    f = check(["plugins/myplug/scripts/a.py", "plugins/other/scripts/b.py"],
              BASE, BASE)
    assert len(f.failures) == 2
    assert {"myplug" in f.failures[0], "other" in f.failures[1]} == {True}


# --- the gate stays quiet ---------------------------------------------------

def test_bump_plus_notes_passes():
    head = {**BASE, "myplug": "0.2.0"}
    f = check(
        ["plugins/myplug/skills/thing/SKILL.md",
         "plugins/myplug/CHANGELOG.md"],
        BASE, head)
    assert f.ok


def test_docs_only_change_is_not_gated():
    f = check(["plugins/myplug/README.md",
               "plugins/myplug/skills/thing/README.md"], BASE, BASE)
    assert f.ok
    assert any("docs-only" in n for n in f.notes)


def test_changelog_only_change_is_not_gated():
    """Backfilling notes for past releases must not demand a version bump."""
    f = check(["plugins/myplug/CHANGELOG.md"], BASE, BASE)
    assert f.ok


def test_repo_level_change_is_not_gated():
    f = check([".github/workflows/ci.yml", "scripts/lint_skills.py",
               "README.md"], BASE, BASE)
    assert f.ok
    assert any("no plugin files" in n for n in f.notes)


def test_label_exempts():
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, BASE,
              labels=["bug", "no-changelog"])
    assert f.ok
    assert any("no-changelog" in n for n in f.notes)


def test_pr_body_marker_exempts_case_insensitively():
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, BASE,
              pr_body="Mechanical rename.\n\n[Skip Changelog]\n")
    assert f.ok


def test_unrelated_label_does_not_exempt():
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, BASE,
              labels=["documentation"])
    assert not f.ok


def test_a_new_plugin_needs_notes():
    head = {**BASE, "fresh": "0.1.0"}
    f = check(["plugins/fresh/skills/thing/SKILL.md"], BASE, head)
    assert not f.ok
    f2 = check(["plugins/fresh/skills/thing/SKILL.md",
                "plugins/fresh/CHANGELOG.md"], BASE, head)
    assert f2.ok


def test_a_removed_plugin_is_not_gated():
    head = {"other": "1.0.0"}
    f = check(["plugins/myplug/skills/thing/SKILL.md"], BASE, head)
    assert f.ok
    assert any("no longer in" in n for n in f.notes)


# --- parsing ---------------------------------------------------------------

def test_versions_reads_the_manifest():
    assert versions(manifest(BASE)) == BASE


def test_versions_rejects_broken_json():
    with pytest.raises(GateError):
        versions("{not json")


@pytest.mark.parametrize("path,expected", [
    ("plugins/a/skills/b/SKILL.md", ("a", "skills/b/SKILL.md")),
    ("plugins/a/CHANGELOG.md", ("a", "CHANGELOG.md")),
    ("plugins/a", None),
    ("scripts/x.py", None),
    ("", None),
])
def test_plugin_of(path, expected):
    assert plugin_of(path) == expected


# --- end to end over a real repository -------------------------------------

def build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".claude-plugin").mkdir(parents=True)
    skill = repo / "plugins" / "myplug" / "skills" / "thing"
    skill.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / ".claude-plugin" / "marketplace.json").write_text(manifest(
        {"myplug": "0.1.0"}))
    (skill / "SKILL.md").write_text("---\nname: thing\n---\n")
    (repo / "plugins" / "myplug" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "base-ref")
    return repo


def run(repo: Path, *extra: str) -> int:
    return main(["--repo", str(repo), "--base", "base-ref", *extra])


def test_collect_sees_the_change_and_both_manifests(tmp_path):
    repo = build_repo(tmp_path)
    (repo / "plugins" / "myplug" / "skills" / "thing" / "SKILL.md").write_text(
        "---\nname: thing\ndescription: now does more\n---\n")
    subprocess.run(["git", "commit", "-aqm", "change"], cwd=repo, check=True,
                   capture_output=True)

    changed, base_v, head_v = collect(repo, "base-ref")
    assert changed == ["plugins/myplug/skills/thing/SKILL.md"]
    assert base_v == head_v == {"myplug": "0.1.0"}
    assert not check(changed, base_v, head_v).ok


def test_cli_both_directions(tmp_path, capsys):
    """The same change fails without notes and passes with them."""
    repo = build_repo(tmp_path)
    plug = repo / "plugins" / "myplug"

    (plug / "skills" / "thing" / "SKILL.md").write_text(
        "---\nname: thing\ndescription: now does more\n---\n")
    subprocess.run(["git", "commit", "-aqm", "change"], cwd=repo, check=True,
                   capture_output=True)
    assert run(repo) == 1
    assert "ERROR" in capsys.readouterr().out

    (repo / ".claude-plugin" / "marketplace.json").write_text(manifest(
        {"myplug": "0.2.0"}))
    (plug / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-07-26\n")
    subprocess.run(["git", "commit", "-aqm", "release"], cwd=repo, check=True,
                   capture_output=True)
    assert run(repo) == 0
    assert "clean" in capsys.readouterr().out


def test_cli_escape_hatches_over_a_real_repo(tmp_path):
    repo = build_repo(tmp_path)
    (repo / "plugins" / "myplug" / "skills" / "thing" / "SKILL.md").write_text(
        "---\nname: thing\ndescription: changed\n---\n")
    subprocess.run(["git", "commit", "-aqm", "change"], cwd=repo, check=True,
                   capture_output=True)
    assert run(repo) == 1
    assert run(repo, "--labels", "no-changelog") == 0
    assert run(repo, "--pr-body", "why: [skip changelog]") == 0


def test_bad_base_ref_exits_two_not_one(tmp_path):
    """A gate that cannot run must not look like a gate that passed."""
    repo = build_repo(tmp_path)
    assert main(["--repo", str(repo), "--base", "no/such/ref"]) == 2
