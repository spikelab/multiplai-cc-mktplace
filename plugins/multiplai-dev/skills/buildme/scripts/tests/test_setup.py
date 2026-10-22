"""`build_pipeline setup` and `config-get`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from build_pipeline.__main__ import build_parser, main
from build_pipeline.setup import apply_setup

from tests.test_git_ops import make_repo

COMMENTED = """\
# Project config for buildme — keep this header.
schema: spec-driven
context: |
  Orders service
tdd:
  enabled: true
  test_command: pytest -q  # the fast suite only
interview: user
# Git lifecycle: worktree on, draft PRs.
git:
  worktree: true
  pr: draft
architecture: docs/old.md
"""


def _run(capsys, *argv: str) -> tuple[int, str]:
    rc = main(list(argv))
    return rc, capsys.readouterr().out


class TestSetupCommand:
    def test_creates_the_file_from_nothing(self, tmp_path, capsys):
        rc, out = _run(capsys, "setup", "--project-dir", str(tmp_path),
                       "--architecture", "docs/a.md", "--pr-template", "auto")
        path = tmp_path / "specs" / "config.yaml"
        assert rc == 0
        data = yaml.safe_load(path.read_text())
        assert data["architecture"] == "docs/a.md"
        assert data["pr"]["template"] == "auto"
        assert f"SETUP:WROTE:{path}" in out
        # Not a git repo, so certainly not committed.
        assert f"SETUP:COMMIT_REQUIRED:{path}" in out
        assert "ADDED: architecture = docs/a.md" in out

    def test_untracked_file_in_a_repo_requires_a_commit(self, tmp_path, capsys):
        repo = make_repo(tmp_path / "proj")
        rc, out = _run(capsys, "setup", "--project-dir", str(repo), "--architecture", "a.md")
        assert rc == 0
        assert "SETUP:COMMIT_REQUIRED:" in out

    def test_committed_and_unchanged_needs_no_commit(self, tmp_path, capsys):
        repo = make_repo(tmp_path / "proj")
        _run(capsys, "setup", "--project-dir", str(repo), "--architecture", "a.md")
        subprocess.run(["git", "add", "specs/config.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "config"], cwd=repo, check=True)
        rc, out = _run(capsys, "setup", "--project-dir", str(repo), "--architecture", "a.md")
        assert rc == 0
        assert "UNCHANGED" in out
        assert "SETUP:COMMIT_REQUIRED" not in out

    def test_a_flag_not_passed_leaves_its_key_untouched(self, tmp_path, capsys):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            "interview: ticket\nintake:\n  provider: plane\n  plane:\n    project: DB\n")
        rc, out = _run(capsys, "setup", "--project-dir", str(tmp_path),
                       "--architecture", "docs/architecture.md")
        assert rc == 0
        data = yaml.safe_load((specs / "config.yaml").read_text())
        assert data["interview"] == "ticket"
        assert data["intake"] == {"provider": "plane", "plane": {"project": "DB"}}
        assert data["architecture"] == "docs/architecture.md"
        assert "CHANGED" not in out

    def test_comments_and_key_order_survive_a_change_and_an_addition(self, tmp_path, capsys):
        specs = tmp_path / "specs"
        specs.mkdir()
        path = specs / "config.yaml"
        path.write_text(COMMENTED)

        rc, out = _run(capsys, "setup", "--project-dir", str(tmp_path),
                       "--architecture", "docs/new.md", "--pr-template", "auto")
        assert rc == 0
        text = path.read_text()
        for comment in ("# Project config for buildme — keep this header.\n",
                        "# Git lifecycle: worktree on, draft PRs.\n",
                        "  test_command: pytest -q  # the fast suite only\n"):
            assert comment in text, comment
        assert text.startswith("# Project config for buildme — keep this header.\n")
        top_keys = [line.split(":")[0] for line in text.splitlines()
                    if line and not line.startswith((" ", "#"))]
        assert top_keys[:5] == ["schema", "context", "tdd", "interview", "git"]
        data = yaml.safe_load(text)
        assert data["architecture"] == "docs/new.md"
        assert data["interview"] == "user", "a key buildme no longer reads is kept"
        assert data["pr"]["template"] == "auto"
        assert "CHANGED: architecture: docs/old.md → docs/new.md" in out
        assert "ADDED: pr.template = auto" in out

    def test_findings_authors_list_and_pr_template(self):
        text, added, changed = apply_setup("", [
            (("findings", "authors"), ["alice", "bob"]),
            (("pr", "template"), "auto"),
        ])
        data = yaml.safe_load(text)
        assert data["findings"]["authors"] == ["alice", "bob"]
        assert data["pr"]["template"] == "auto"
        assert changed == []
        assert [k for k, _ in added] == ["findings.authors", "pr.template"]

    def test_no_key_is_ever_removed(self, tmp_path, capsys):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(COMMENTED)
        _run(capsys, "setup", "--project-dir", str(tmp_path), "--pr-template", "auto")
        data = yaml.safe_load((specs / "config.yaml").read_text())
        assert set(data) >= {"schema", "context", "tdd", "interview", "git", "pr"}

    def test_unparseable_yaml_is_never_overwritten(self, tmp_path, capsys):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("interview: [unclosed\n")
        rc = main(["setup", "--project-dir", str(tmp_path), "--architecture", "a.md"])
        assert rc == 1
        assert (specs / "config.yaml").read_text() == "interview: [unclosed\n"


class TestConfigGet:
    def test_prints_a_resolved_value(self, tmp_path, capsys):
        assert main(["config-get", "pr.max_change_lines", "--project-dir", str(tmp_path)]) == 0
        assert capsys.readouterr().out.strip() == "1000"
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("architecture: docs/a.md\n")
        assert main(["config-get", "architecture", "--project-dir", str(tmp_path)]) == 0
        assert capsys.readouterr().out.strip() == "docs/a.md"

    def test_pm_specs_keys_are_not_buildmes(self, tmp_path, capsys):
        for key in ("intake.provider", "interview"):
            assert main(["config-get", key, "--project-dir", str(tmp_path)]) == 2

    def test_unknown_key_is_a_usage_error(self, tmp_path, capsys):
        assert main(["config-get", "nope", "--project-dir", str(tmp_path)]) == 2


class TestHelp:
    def test_help_lists_the_new_subcommands_and_flags(self):
        parser = build_parser()
        top = parser.format_help()
        assert "setup" in top and "config-get" in top
        build_help = parser._subparsers._group_actions[0].choices["build"].format_help()
        for flag in ("--change", "--research-path", "--accept-size"):
            assert flag in build_help, flag
        for flag in ("--mode", "--interview-summary", "--context-files", "--ticket-file",
                     "--ticket-ref", "--ticket-url", "--plan-file", "--interview-replies",
                     "--approve-split", "--no-epic-split", "--prototype", "--no-prototype"):
            assert flag not in build_help, flag
