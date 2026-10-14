"""The narrow test command a block's TDD loop runs instead of the whole suite.

It lists the test files the block added or changed, plus the test file named
after each source file it changed, and appends them to the project's test
command when the runner is known to accept paths. Anything else falls back to
the whole command, so RED never passes on an empty selection.
"""
import json
import subprocess
from pathlib import Path

import pytest

from build_pipeline.narrow_tests import (
    narrow_command,
    narrow_test_command,
    runner_of,
    select_test_paths,
)


# --- which runner a test command uses -------------------------------------

class TestRunnerOf:
    @pytest.mark.parametrize("command", [
        "pytest", "pytest -xvs", "uv run pytest -q", "python -m pytest -x",
        ".venv/bin/pytest", "py.test",
    ])
    def test_pytest_commands(self, tmp_path, command):
        assert runner_of(command, tmp_path) == "pytest"

    @pytest.mark.parametrize("command", ["npx jest", "jest --ci", "npx vitest run", "vitest"])
    def test_direct_jest_and_vitest(self, tmp_path, command):
        assert runner_of(command, tmp_path) == "node"

    @pytest.mark.parametrize("script", ["jest", "vitest run", "NODE_ENV=test jest --ci"])
    def test_npm_test_running_jest_or_vitest(self, tmp_path, script):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
        assert runner_of("npm test", tmp_path) == "node"
        assert runner_of("npm run test", tmp_path) == "node"

    def test_npm_test_running_something_else_is_unknown(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "mocha"}}))
        assert runner_of("npm test", tmp_path) is None

    def test_npm_test_without_package_json_is_unknown(self, tmp_path):
        assert runner_of("npm test", tmp_path) is None

    def test_npm_test_with_unreadable_package_json_is_unknown(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json")
        assert runner_of("npm test", tmp_path) is None

    @pytest.mark.parametrize("command", [
        "swift test", "cargo test", "go test ./...", "make test", "yarn test", "",
        "pytest 'unbalanced",
    ])
    def test_other_commands_are_unknown(self, tmp_path, command):
        assert runner_of(command, tmp_path) is None


# --- which paths a block's changes select ---------------------------------

class TestSelectTestPaths:
    def test_pytest_takes_changed_test_files(self):
        changed = ["tests/test_new.py", "pkg/other_test.py", "README.md"]
        assert select_test_paths(changed, [], "pytest") == [
            "pkg/other_test.py", "tests/test_new.py"]

    def test_pytest_adds_the_test_file_named_after_a_changed_module(self):
        changed = ["pkg/parser.py", "pkg/lexer.py"]
        files = ["pkg/parser.py", "pkg/lexer.py", "tests/test_parser.py",
                 "tests/lexer_test.py", "tests/test_unrelated.py"]
        assert select_test_paths(changed, files, "pytest") == [
            "tests/lexer_test.py", "tests/test_parser.py"]

    def test_pytest_skips_a_module_with_no_test_file(self):
        assert select_test_paths(["pkg/util.py"], ["pkg/util.py"], "pytest") == []

    def test_pytest_ignores_conftest_and_helpers(self):
        changed = ["tests/conftest.py", "tests/helpers.txt"]
        assert select_test_paths(changed, ["tests/test_conftest.py"], "pytest") == []

    def test_pytest_does_not_select_by_import(self):
        # test_app.py imports config, but only test_config.py is named after it.
        changed = ["pkg/config.py"]
        files = ["pkg/config.py", "tests/test_config.py", "tests/test_app.py"]
        assert select_test_paths(changed, files, "pytest") == ["tests/test_config.py"]

    def test_node_takes_changed_test_and_spec_files(self):
        changed = ["src/a.test.ts", "src/b.spec.jsx", "src/test_c.py"]
        assert select_test_paths(changed, [], "node") == ["src/a.test.ts", "src/b.spec.jsx"]

    def test_node_adds_the_test_file_named_after_a_changed_source(self):
        changed = ["src/cart.ts", "src/price.js"]
        files = ["src/cart.ts", "src/cart.test.ts", "src/price.js",
                 "test/price.spec.mjs", "src/cartography.test.ts"]
        assert select_test_paths(changed, files, "node") == [
            "src/cart.test.ts", "test/price.spec.mjs"]

    def test_unknown_runner_selects_nothing(self):
        assert select_test_paths(["tests/test_a.py"], [], None) == []


# --- the command itself ---------------------------------------------------

class TestNarrowCommand:
    def test_pytest_appends_paths(self):
        assert narrow_command("pytest -xvs", ["tests/test_a.py"], "pytest") == \
            "pytest -xvs tests/test_a.py"

    def test_paths_are_quoted(self):
        assert narrow_command("pytest", ["tests/my test.py"], "pytest") == \
            "pytest 'tests/my test.py'"

    def test_npm_test_gets_a_separator(self):
        assert narrow_command("npm test", ["src/a.test.ts"], "node") == \
            "npm test -- src/a.test.ts"

    def test_npm_test_with_a_separator_already_gets_no_second_one(self):
        assert narrow_command("npm test -- --ci", ["src/a.test.ts"], "node") == \
            "npm test -- --ci src/a.test.ts"

    def test_direct_jest_appends_paths(self):
        assert narrow_command("npx jest", ["src/a.test.ts"], "node") == \
            "npx jest src/a.test.ts"

    def test_empty_paths_fall_back_to_the_whole_command(self):
        assert narrow_command("pytest -xvs", [], "pytest") == "pytest -xvs"

    def test_unknown_runner_falls_back_to_the_whole_command(self):
        assert narrow_command("cargo test", ["tests/a.rs"], None) == "cargo test"


# --- end to end against a real git repo -----------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", "--allow-empty")
    return _git(root, "rev-parse", "HEAD")


class TestNarrowTestCommand:
    @pytest.fixture
    def project(self, tmp_path):
        p = tmp_path / "proj"
        (p / "pkg").mkdir(parents=True)
        (p / "tests").mkdir()
        (p / "pkg" / "parser.py").write_text("x = 1\n")
        (p / "pkg" / "config.py").write_text("y = 1\n")
        (p / "tests" / "test_parser.py").write_text("def test_p(): pass\n")
        (p / "tests" / "test_config.py").write_text("def test_c(): pass\n")
        (p / "tests" / "test_old.py").write_text("def test_o(): pass\n")
        return p

    def test_pytest_block_runs_its_own_and_named_tests(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "test(block-1)")
        (project / "pkg" / "parser.py").write_text("x = 2\n")  # uncommitted edit
        assert narrow_test_command("pytest -xvs", project, base) == \
            "pytest -xvs tests/test_new.py tests/test_parser.py"

    def test_untracked_test_files_count(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", project, base) == "pytest tests/test_new.py"

    def test_a_deleted_test_file_is_not_passed(self, project):
        base = _repo(project)
        (project / "tests" / "test_old.py").unlink()
        assert narrow_test_command("pytest", project, base) == "pytest"

    def test_paths_are_relative_to_a_project_in_a_subdirectory(self, tmp_path):
        repo = tmp_path / "repo"
        sub = repo / "app"
        (sub / "tests").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "tests" / "test_outside.py").write_text("def test_x(): pass\n")
        base = _repo(repo)
        (sub / "tests" / "test_inside.py").write_text("def test_i(): pass\n")
        (repo / "tests" / "test_outside.py").write_text("def test_y(): pass\n")
        assert narrow_test_command("pytest", sub, base) == "pytest tests/test_inside.py"

    def test_node_block_through_npm(self, tmp_path):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        (p / "src" / "cart.ts").write_text("export const a = 1\n")
        (p / "src" / "cart.test.ts").write_text("test('a', () => {})\n")
        base = _repo(p)
        (p / "src" / "cart.ts").write_text("export const a = 2\n")
        assert narrow_test_command("npm test", p, base) == "npm test -- src/cart.test.ts"

    def test_empty_selection_falls_back(self, project):
        base = _repo(project)
        (project / "README.md").write_text("docs\n")
        assert narrow_test_command("pytest -xvs", project, base) == "pytest -xvs"

    def test_unknown_runner_falls_back(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("make test", project, base) == "make test"

    def test_disabled_falls_back(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", project, base, enabled=False) == "pytest"

    def test_no_recorded_block_start_falls_back(self, project):
        _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", project, None) == "pytest"

    def test_not_a_git_repo_falls_back(self, project):
        assert narrow_test_command("pytest", project, "HEAD") == "pytest"

    def test_no_test_command_stays_empty(self, project):
        base = _repo(project)
        assert narrow_test_command("", project, base) == ""
