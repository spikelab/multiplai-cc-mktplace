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
        # Appended paths would go to the last command of a chain.
        "pytest && ruff check .", "pytest; echo done", "pytest | tee out.txt",
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
        # A helper module under a tests directory, which a project's
        # `python_files` may collect.
        (["tests/test_new.py", "tests/helpers.py"], [], "pytest"),
        # jest's default __tests__ directory.
        (["src/__tests__/foo.js", "src/foo.js"], ["src/foo.test.js"], "node"),
    ])
    def test_a_changed_test_file_it_cannot_name_falls_back(self, changed, files, runner):
        # The block's own tests must always run, so a changed file the engine
        # counts as a test but the narrow run cannot name means the whole command.
        assert select_test_paths(changed, files, runner) == []

    def test_conftest_and_init_do_not_stop_narrowing(self):
        # pytest never collects tests from these.
        changed = ["tests/test_new.py", "tests/conftest.py", "tests/__init__.py"]
        assert select_test_paths(changed, [], "pytest") == ["tests/test_new.py"]

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

    def test_paths_under_tool_and_environment_dirs_are_never_selected(self):
        # Untracked files count as changed, and a .venv or node_modules that no
        # .gitignore covers holds third-party tests the whole run never collects.
        changed = ["tests/test_new.py", "pkg/config.py",
                   ".venv/lib/python3.11/site-packages/foo/tests/test_foo.py",
                   "venv/lib/x/config_test.py"]
        files = ["pkg/config.py", ".venv/lib/python3.11/site-packages/foo/test_config.py"]
        assert select_test_paths(changed, files, "pytest") == ["tests/test_new.py"]
        assert select_test_paths(["src/a.test.ts", "node_modules/x/b.test.js"], [],
                                 "node") == ["src/a.test.ts"]

    def test_a_tests_dir_inside_a_venv_does_not_stop_narrowing(self):
        changed = ["tests/test_new.py", ".venv/lib/foo/tests/helpers.py"]
        assert select_test_paths(changed, [], "pytest") == ["tests/test_new.py"]

    def test_only_paths_the_whole_run_collects_are_selected(self):
        changed = ["pkg/db.py", "tests/unit/test_new.py", "tests/integration/test_api.py"]
        files = ["pkg/db.py", "tests/integration/test_db.py", "tests/unit/test_db.py"]
        collected = lambda p: p.startswith("tests/unit/")  # noqa: E731
        assert select_test_paths(changed, files, "pytest", collected) == [
            "tests/unit/test_db.py", "tests/unit/test_new.py"]

    def test_the_nearest_named_file_is_chosen_among_those_collected(self):
        # The nearest test_config.py is outside the run; the collected one wins.
        changed = ["a/pkg/config.py"]
        files = ["a/tests/test_config.py", "b/tests/test_config.py"]
        assert select_test_paths(changed, files, "pytest",
                                 lambda p: p.startswith("b/")) == ["b/tests/test_config.py"]


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

    def test_a_very_long_selection_falls_back(self):
        # One argv string past the OS limit would raise in run_test_suite, and
        # the command is also pasted into agents' prompts.
        paths = [f"tests/test_{i:05d}_{'x' * 40}.py" for i in range(1000)]
        assert narrow_command("pytest", paths, "pytest") == "pytest"


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
        ("pytest.toml", '[pytest]\naddopts = ["--cov=pkg", "--cov-fail-under=90"]\n'),
        (".pytest.toml", '[pytest]\naddopts = ["--cov=pkg"]\n'),
        (".pytest.ini", "[pytest]\naddopts = --cov=pkg\n"),
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


class TestNarrowStaysInsideTheWholeRun:
    """pytest runs any file named on its command line, so the narrow run must
    pick only files the whole command collects; jest and vitest OR every
    pattern given, so a command that already names some is never narrowed."""

    @pytest.fixture
    def project(self, tmp_path):
        p = tmp_path / "proj"
        for d in ("pkg", "tests/unit", "tests/integration"):
            (p / d).mkdir(parents=True)
        (p / "pkg" / "db.py").write_text("x = 1\n")
        (p / "tests" / "unit" / "test_old.py").write_text("def test_o(): pass\n")
        (p / "tests" / "integration" / "test_db.py").write_text(
            "def test_db(): raise ConnectionError\n")
        return p

    def _block(self, project):
        (project / "pkg" / "db.py").write_text("x = 2\n")
        (project / "tests" / "unit" / "test_new.py").write_text("def test_n(): pass\n")

    def test_an_untracked_venv_is_never_selected(self, project):
        base = _repo(project)
        venv_test = project / ".venv/lib/python3.11/site-packages/foo/tests/test_foo.py"
        venv_test.parent.mkdir(parents=True)
        venv_test.write_text("def test_f(): pass\n")
        (project / "tests" / "unit" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest -q", project, base) == \
            "pytest -q tests/unit/test_new.py"

    def test_an_untracked_node_modules_is_never_selected(self, tmp_path):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "node_modules" / "lib").mkdir(parents=True)
        base = _repo(p)
        (p / "node_modules" / "lib" / "a.test.js").write_text("test('a', () => {})\n")
        (p / "src" / "cart.test.ts").write_text("test('c', () => {})\n")
        assert narrow_test_command("npx jest", p, base) == "npx jest src/cart.test.ts"

    @pytest.mark.parametrize("name,text", [
        ("pytest.ini", "[pytest]\ntestpaths = tests/unit\n"),
        ("pytest.ini", "[pytest]\ntestpaths =\n    tests/u*\n"),
        ("pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = [\"tests/unit\"]\n"),
        ("pyproject.toml", "[tool.pytest]\ntestpaths = [\"tests/unit\"]\n"),
        ("setup.cfg", "[tool:pytest]\ntestpaths = tests/unit\n"),
        ("tox.ini", "[pytest]\ntestpaths = tests/unit\n"),
        ("pytest.toml", '[pytest]\ntestpaths = ["tests/unit"]\n'),
        (".pytest.toml", '[pytest]\ntestpaths = ["tests/unit"]\n'),
        (".pytest.ini", "[pytest]\ntestpaths = tests/unit\n"),
    ])
    def test_named_files_outside_testpaths_are_left_out(self, project, name, text):
        (project / name).write_text(text)
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -q", project, base) == \
            "pytest -q tests/unit/test_new.py"

    def test_testpaths_that_match_nothing_scope_nothing(self, project):
        # pytest then collects from where it runs.
        (project / "pytest.ini").write_text("[pytest]\ntestpaths = nowhere\n")
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -q", project, base) == \
            "pytest -q tests/integration/test_db.py tests/unit/test_new.py"

    def test_testpaths_of_a_parent_config_do_not_apply_to_a_subdirectory(self, tmp_path):
        # pytest uses testpaths only when it runs in the config's own directory.
        repo = tmp_path / "repo"
        sub = repo / "app"
        (sub / "tests").mkdir(parents=True)
        (repo / "pytest.ini").write_text("[pytest]\ntestpaths = other\n")
        base = _repo(repo)
        (sub / "tests" / "test_new.py").write_text("def test_n(): pass\n")
        assert narrow_test_command("pytest", sub, base) == "pytest tests/test_new.py"

    def test_a_tie_at_the_root_keeps_only_the_collected_file(self, tmp_path):
        p = tmp_path / "proj"
        for d in ("a/tests", "b/tests"):
            (p / d).mkdir(parents=True)
        (p / "config.py").write_text("x = 1\n")
        (p / "a" / "tests" / "test_config.py").write_text("def test_a(): pass\n")
        (p / "b" / "tests" / "test_config.py").write_text("def test_b(): pass\n")
        (p / "pytest.ini").write_text("[pytest]\ntestpaths = a/tests\n")
        base = _repo(p)
        (p / "config.py").write_text("x = 2\n")
        assert narrow_test_command("pytest", p, base) == "pytest a/tests/test_config.py"

    @pytest.mark.parametrize("ini,dirname", [
        (None, "build"),
        ("[pytest]\nnorecursedirs = legacy\n", "legacy"),
        ("[pytest]\nnorecursedirs = old/legacy\n", "old/legacy"),
    ])
    def test_files_under_norecursedirs_are_left_out(self, project, ini, dirname):
        if ini:
            (project / "pytest.ini").write_text(ini)
        (project / dirname).mkdir(parents=True)
        (project / dirname / "test_db.py").write_text("def test_x(): pass\n")
        base = _repo(project)
        self._block(project)
        command = narrow_test_command("pytest", project, base)
        assert f"{dirname}/test_db.py" not in command
        assert "tests/unit/test_new.py" in command

    @pytest.mark.parametrize("command", [
        "pytest --ignore=tests/integration", "pytest --ignore tests/integration",
        "pytest --ignore-glob=tests/integ*",
    ])
    def test_files_the_command_ignores_are_left_out(self, project, command):
        base = _repo(project)
        self._block(project)
        assert narrow_test_command(command, project, base) == \
            f"{command} tests/unit/test_new.py"

    def test_an_option_value_that_is_not_a_path_still_narrows(self, project):
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -k db", project, base) == \
            "pytest -k db tests/integration/test_db.py tests/unit/test_new.py"

    @pytest.mark.parametrize("command,ini", [
        # The command already names what to run; appending would only add.
        ("pytest -q tests/unit", None),
        ("pytest -x tests/unit/test_old.py::test_o", None),
        ("pytest .", None),
        ("pytest", "[pytest]\naddopts = tests/unit\n"),
        # Another config or root than the one read here.
        ("pytest -c pytest.ini", "[pytest]\n"),
        ("pytest --rootdir=tests", None),
        ("pytest --config-file=other.ini", None),
        ("pytest --pyargs pkg", None),
        # The project names its own test files.
        ("pytest", "[pytest]\npython_files = check_*.py\n"),
        # A config that cannot be read.
        ("pytest", "[pytest\n"),
    ])
    def test_a_command_whose_collection_it_cannot_tell_falls_back(self, project, command, ini):
        if ini:
            (project / "pytest.ini").write_text(ini)
        base = _repo(project)
        self._block(project)
        assert narrow_test_command(command, project, base) == command
        assert agent_test_command(command, project, base) == command

    def test_pytest_addopts_naming_paths_falls_back(self, project, monkeypatch):
        monkeypatch.setenv("PYTEST_ADDOPTS", "tests/unit")
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest", project, base) == "pytest"

    def test_unreadable_pyproject_falls_back(self, project):
        (project / "pyproject.toml").write_text("[tool.pytest.ini_options\n")
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest", project, base) == "pytest"

    def test_pytest_toml_comes_before_pytest_ini(self, project):
        # pytest 9 reads pytest.toml first, even an empty one, so the
        # pytest.ini testpaths here do not apply.
        (project / "pytest.toml").write_text("[pytest]\n")
        (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests/unit\n")
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -q", project, base) == \
            "pytest -q tests/integration/test_db.py tests/unit/test_new.py"

    def test_python_files_in_pytest_toml_falls_back(self, project):
        (project / "pytest.toml").write_text('[pytest]\npython_files = ["check_*.py"]\n')
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest", project, base) == "pytest"

    @pytest.mark.parametrize("conftest", [
        'collect_ignore = ["integration/test_db.py"]\n',
        'collect_ignore_glob = ["integration/*"]\n',
        "def pytest_ignore_collect(collection_path, config):\n"
        "    return 'integration' in str(collection_path)\n",
    ])
    def test_a_conftest_that_ignores_files_falls_back(self, project, conftest):
        # pytest skips collect_ignore and pytest_ignore_collect for a path
        # named on its command line, so the narrow run could hold a file the
        # whole run ignores.
        (project / "tests" / "conftest.py").write_text(conftest)
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -q", project, base) == "pytest -q"
        assert agent_test_command("pytest -q", project, base) == "pytest -q"

    def test_a_conftest_that_ignores_files_elsewhere_still_narrows(self, project):
        (project / "docs").mkdir()
        (project / "docs" / "conftest.py").write_text('collect_ignore = ["x.py"]\n')
        (project / "tests" / "conftest.py").write_text("import pytest\n")
        base = _repo(project)
        self._block(project)
        assert narrow_test_command("pytest -q", project, base) == \
            "pytest -q tests/integration/test_db.py tests/unit/test_new.py"


class TestNarrowLoadsTheWholeRunsConfig:
    """pytest finds its config from the common ancestor of the paths it is
    given, so a narrow run of files below a nested config would load that
    config instead of the whole run's."""

    @pytest.fixture
    def repo(self, tmp_path):
        p = tmp_path / "repo"
        (p / "packages" / "a" / "tests").mkdir(parents=True)
        (p / "packages" / "a" / "mod.py").write_text("x = 1\n")
        (p / "packages" / "a" / "tests" / "test_mod.py").write_text("def test_m(): pass\n")
        return p

    def _block(self, repo):
        (repo / "packages" / "a" / "mod.py").write_text("x = 2\n")

    @pytest.mark.parametrize("name,text", [
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--cov=a --cov-fail-under=90"\n'),
        ("pytest.ini", "[pytest]\n"),
        ("pytest.toml", "[pytest]\n"),
        ("setup.cfg", "[tool:pytest]\naddopts = -p no:cacheprovider\n"),
    ])
    def test_a_config_below_the_whole_runs_config_falls_back(self, repo, name, text):
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "packages" / "a" / name).write_text(text)
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == "pytest -q"
        assert agent_test_command("pytest -q", repo, base) == "pytest -q"

    def test_a_nested_config_with_no_root_config_falls_back(self, repo):
        (repo / "packages" / "a" / "pytest.ini").write_text("[pytest]\n")
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == "pytest -q"

    def test_a_nested_setup_py_with_no_config_falls_back(self, repo):
        # With no config, pytest takes the nearest setup.py's directory as
        # rootdir, and loads no conftest.py above it.
        (repo / "packages" / "a" / "setup.py").write_text("")
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == "pytest -q"

    def test_a_nested_pyproject_without_pytest_settings_still_narrows(self, repo):
        # pytest walks past it to the root's pytest.ini.
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "packages" / "a" / "pyproject.toml").write_text("[project]\nname = 'a'\n")
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == \
            "pytest -q packages/a/tests/test_mod.py"

    def test_a_nested_pyproject_with_only_a_root_pyproject_falls_back(self, repo):
        # No pytest settings anywhere: pytest takes the first pyproject.toml
        # from the paths up as rootdir, which is packages/a's, not the root's.
        (repo / "pyproject.toml").write_text("[project]\nname = 'root'\n")
        (repo / "packages" / "a" / "pyproject.toml").write_text("[project]\nname = 'a'\n")
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == "pytest -q"

    @pytest.mark.parametrize("name,text", [
        ("pytest.ini", "[pytest\n"),
        ("pytest.toml", "[pytest\n"),
    ])
    def test_an_unreadable_nested_config_falls_back(self, repo, name, text):
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "packages" / "a" / name).write_text(text)
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == "pytest -q"

    def test_no_config_anywhere_still_narrows(self, repo):
        base = _repo(repo)
        self._block(repo)
        assert narrow_test_command("pytest -q", repo, base) == \
            "pytest -q packages/a/tests/test_mod.py"

    @pytest.mark.parametrize("command,script", [
        ("npx jest src/unit", None),
        ("npx vitest run src", None),
        ("npx vitest related src/cart.ts", None),
        ("npx jest --testPathPattern=unit", None),
        ("npx jest --findRelatedTests src/cart.ts", None),
        ("npm test -- src", "jest"),
        ("npm test", "jest src"),
        ("npm test", "vitest run unit"),
    ])
    def test_node_command_with_its_own_filter_falls_back(self, tmp_path, command, script):
        p = tmp_path / "web"
        (p / "src" / "unit").mkdir(parents=True)
        if script:
            (p / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
        base = _repo(p)
        (p / "src" / "cart.test.ts").write_text("test('c', () => {})\n")
        assert narrow_test_command(command, p, base) == command

    @pytest.mark.parametrize("command", [
        "npx jest --config jest.config.js", "npx vitest run --maxWorkers 2", "npx jest -t cart",
    ])
    def test_node_option_values_do_not_stop_narrowing(self, tmp_path, command):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "jest.config.js").write_text("module.exports = {}\n")
        base = _repo(p)
        (p / "src" / "cart.test.ts").write_text("test('c', () => {})\n")
        assert narrow_test_command(command, p, base) == f"{command} src/cart.test.ts"

    def test_npm_exec_jest_is_read_as_jest_not_the_test_script(self, tmp_path):
        p = tmp_path / "web"
        (p / "src").mkdir(parents=True)
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "mocha"}}))
        base = _repo(p)
        (p / "src" / "cart.test.ts").write_text("test('c', () => {})\n")
        assert narrow_test_command("npm exec jest", p, base) == \
            "npm exec jest -- src/cart.test.ts"


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

    def test_before_the_block_has_tests_it_says_how_to_check_one_file(self, project):
        base = _repo(project)
        text = agent_test_command("pytest -xvs", project, base)
        assert text.splitlines()[0] == "pytest -xvs"
        assert "runs the whole suite" in text
        assert "test_<module>.py" in text
        assert "run the command as given before you finish" in text

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
