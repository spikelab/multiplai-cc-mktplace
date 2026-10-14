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
    agent_test_command,
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
        assert runner_of("npm t", tmp_path) == "node"

    def test_npm_test_running_something_else_is_unknown(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "mocha"}}))
        assert runner_of("npm test", tmp_path) is None

    @pytest.mark.parametrize("script", ["jest && eslint .", "vitest run; echo done",
                                        "jest | tee out.txt", "tox -e jest"])
    def test_npm_test_script_that_does_not_end_in_jest_or_vitest_is_unknown(self, tmp_path, script):
        # npm appends `-- paths` to the end of the whole script.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
        assert runner_of("npm test", tmp_path) is None

    def test_npm_test_without_package_json_is_unknown(self, tmp_path):
        assert runner_of("npm test", tmp_path) is None

    def test_npm_test_with_unreadable_package_json_is_unknown(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json")
        assert runner_of("npm test", tmp_path) is None

    @pytest.mark.parametrize("command", [
        "swift test", "cargo test", "go test ./...", "make test", "yarn test", "",
        "pytest 'unbalanced", "tox -e pytest", "make pytest", "nox -s jest",
        # A task runner behind a wrapper still takes its own arguments.
        "uv run tox -e pytest", "poetry run nox -s pytest", "python -m tox -e pytest",
        "env X=1 tox -e pytest", "npx nox -s jest",
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

    @pytest.mark.parametrize("changed,files,runner", [
        # Django: the block's tests are in shop/tests.py, which pytest-django runs.
        (["shop/tests.py", "shop/forms.py"], ["shop/test_forms.py"], "pytest"),
        # A helper module or conftest under a tests directory.
        (["tests/test_new.py", "tests/conftest.py"], [], "pytest"),
        (["tests/test_new.py", "tests/helpers.py"], [], "pytest"),
        # jest's default __tests__ directory.
        (["src/__tests__/foo.js", "src/foo.js"], ["src/foo.test.js"], "node"),
    ])
    def test_a_changed_test_file_it_cannot_name_falls_back(self, changed, files, runner):
        # The block's own tests must always run, so a changed file the engine
        # counts as a test but the narrow run cannot name means the whole command.
        assert select_test_paths(changed, files, runner) == []

    def test_a_test_file_of_another_language_does_not_stop_narrowing(self):
        changed = ["tests/test_new.py", "web/src/__tests__/a.js"]
        assert select_test_paths(changed, [], "pytest") == ["tests/test_new.py"]

    def test_the_named_test_file_nearest_the_module_wins(self):
        # A monorepo with one test_config.py per package: only the one in the
        # changed module's own package is taken.
        changed = ["a/pkg/config.py"]
        files = ["a/pkg/config.py", "a/tests/test_config.py", "b/tests/test_config.py"]
        assert select_test_paths(changed, files, "pytest") == ["a/tests/test_config.py"]

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

    @pytest.mark.parametrize("path", ["src/[id].test.js", "src/(auth)/login.test.js",
                                      "src/a+b.test.ts"])
    def test_node_path_jest_would_read_as_a_regex_falls_back(self, path):
        # jest reads each path as a regular expression.
        assert narrow_command("npx jest", ["src/ok.test.js", path], "node") == "npx jest"

    def test_pytest_takes_a_path_with_brackets(self):
        assert narrow_command("pytest", ["tests/test_[a].py"], "pytest") == \
            "pytest 'tests/test_[a].py'"


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

    def test_a_named_test_file_deleted_but_not_staged_is_not_passed(self, project):
        base = _repo(project)
        (project / "pkg" / "parser.py").write_text("x = 2\n")
        (project / "tests" / "test_parser.py").unlink()
        assert narrow_test_command("pytest", project, base) == "pytest"

    @pytest.mark.parametrize("name,text", [
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--cov=pkg --cov-fail-under=90"\n'),
        ("pytest.ini", "[pytest]\naddopts = --cov=pkg\n"),
        ("setup.cfg", "[tool:pytest]\naddopts = --cov pkg\n"),
    ])
    def test_pytest_with_coverage_configured_falls_back(self, project, name, text):
        # A coverage floor the whole suite meets fails a narrow run on coverage
        # alone, which RED would read as a right-reason failure.
        (project / name).write_text(text)
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest -xvs", project, base) == "pytest -xvs"

    def test_pytest_with_coverage_in_the_command_falls_back(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        command = "pytest --cov=pkg --cov-fail-under=90"
        assert narrow_test_command(command, project, base) == command

    def test_pytest_with_coverage_in_pytest_addopts_falls_back(self, project, monkeypatch):
        monkeypatch.setenv("PYTEST_ADDOPTS", "--cov=pkg")
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", project, base) == "pytest"

    def test_pytest_config_of_the_containing_repo_counts(self, tmp_path):
        repo = tmp_path / "repo"
        sub = repo / "app"
        (sub / "tests").mkdir(parents=True)
        (repo / "pytest.ini").write_text("[pytest]\naddopts = --cov=app\n")
        base = _repo(repo)
        (sub / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", sub, base) == "pytest"

    @pytest.mark.parametrize("package,extra", [
        ({"scripts": {"test": "jest --coverage"}}, None),
        ({"scripts": {"test": "jest"}, "jest": {"coverageThreshold": {"global": {"lines": 90}}}}, None),
        ({"scripts": {"test": "jest"}}, ("jest.config.js", "module.exports = {collectCoverage: true}\n")),
        ({"scripts": {"test": "vitest run"}}, ("vitest.config.ts", "export default {test: {coverage: {enabled: true}}}\n")),
    ])
    def test_node_with_coverage_configured_falls_back(self, tmp_path, package, extra):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "package.json").write_text(json.dumps(package))
        if extra:
            (p / extra[0]).write_text(extra[1])
        base = _repo(p)
        (p / "src" / "cart.test.ts").write_text("test('a', () => {})\n")
        assert narrow_test_command("npm test", p, base) == "npm test"

    def test_node_coverage_script_that_npm_test_does_not_run_is_ignored(self, tmp_path):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "package.json").write_text(json.dumps(
            {"scripts": {"test": "jest", "coverage": "jest --coverage"}}))
        base = _repo(p)
        (p / "src" / "cart.test.ts").write_text("test('a', () => {})\n")
        assert narrow_test_command("npm test", p, base) == "npm test -- src/cart.test.ts"

    def test_no_test_command_stays_empty(self, project):
        base = _repo(project)
        assert narrow_test_command("", project, base) == ""


# --- the test command an agent is given -----------------------------------

class TestAgentTestCommand:
    """An agent gets the narrow command plus how to add the test files it
    writes and the test file named after each module it changes, because its
    command is fixed before it changes anything."""

    @pytest.fixture
    def project(self, tmp_path):
        p = tmp_path / "proj"
        (p / "pkg").mkdir(parents=True)
        (p / "tests").mkdir()
        (p / "pkg" / "widget.py").write_text("x = 1\n")
        (p / "tests" / "test_widget.py").write_text("def test_w(): pass\n")
        return p

    def test_narrowed_command_says_how_to_add_more_tests(self, project):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        text = agent_test_command("pytest -xvs", project, base)
        assert text.splitlines()[0] == "pytest -xvs tests/test_new.py"
        assert "test_<module>.py" in text
        assert "add" in text and "path" in text
        assert "whole suite" in text

    def test_before_the_block_has_tests_it_says_to_run_only_what_it_writes(self, project):
        base = _repo(project)
        text = agent_test_command("pytest -xvs", project, base)
        assert text.splitlines()[0] == "pytest -xvs"
        assert "runs the whole suite" in text
        assert "test_<module>.py" in text

    def test_npm_whole_command_says_paths_go_after_the_separator(self, tmp_path):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        base = _repo(p)
        text = agent_test_command("npm test", p, base)
        assert text.splitlines()[0] == "npm test"
        assert "`--`" in text
        assert "<name>.test.<ext>" in text

    @pytest.mark.parametrize("command,enabled", [("make test", True), ("pytest", False)])
    def test_a_command_it_cannot_narrow_is_given_bare(self, project, command, enabled):
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert agent_test_command(command, project, base, enabled=enabled) == command

    def test_coverage_configured_is_given_bare(self, project):
        (project / "pytest.ini").write_text("[pytest]\naddopts = --cov=pkg\n")
        base = _repo(project)
        (project / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert agent_test_command("pytest", project, base) == "pytest"
