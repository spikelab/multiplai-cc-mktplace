"""`narrow_tests.failed_tests_command` builds one command that runs only the
tests a red whole run named, and `git_ops.fast_forward_to` puts a discarded
whole-change refactor back (item 7, E, of
plan-buildme-epic-speedups-2026-11-03.md, and its item 8 bullet
"E, `failed_tests_command`").

The Dolcebot-style config below is synthetic: a `bash -c '…; …'` whole
command, which buildme refuses to narrow, and `tdd.narrow_commands` with a
pytest `backend` entry run by a script and a jest `frontend` entry.
"""

import shlex
import subprocess
from pathlib import Path

import pytest
from build_pipeline import git_ops
from build_pipeline.config import BuildConfig
from build_pipeline.gates import failing_tests, run_test_suite
from build_pipeline.narrow_tests import NarrowCommand, failed_tests_command

DOLCEBOT_YAML = """\
tdd:
  test_command: "bash -c 'cd backend && ./bm-backend-tests.sh; cd ../frontend && npx jest'"
  narrow_commands:
    - dir: backend
      command: ./bm-backend-tests.sh
      runner: pytest
    - dir: frontend
      command: npx jest
      runner: jest
"""

BACKEND_ID = "DolceEngine/tests/test_permissions.py::PermissionsTest::test_imports"
MIGRATION_ID = (
    "DolceEngine/tests/test_migrations.py::Migrazione0004Test::test_avanti_indietro"
)


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def dolcebot(tmp_path) -> BuildConfig:
    """A repo laid out like Dolcebot, its config read from specs/config.yaml."""
    root = tmp_path / "repo"
    _write(root, "backend/bm-backend-tests.sh", "#!/bin/sh\n")
    _write(root, "backend/DolceEngine/tests/test_permissions.py")
    _write(root, "backend/DolceEngine/tests/test_migrations.py")
    _write(root, "frontend/src/bad.test.ts")
    _write(root, "frontend/src/app/[id]/page.test.tsx")
    _write(root, "specs/config.yaml", DOLCEBOT_YAML)
    config = BuildConfig(project_dir=root)
    config.specs_dir = root / "specs"
    config._load_specs_config()
    assert config.narrow_commands == [
        NarrowCommand("backend", "./bm-backend-tests.sh", "pytest"),
        NarrowCommand("frontend", "npx jest", "jest"),
    ]
    return config


class TestDolcebotNarrowCommands:
    def test_backend_ids_go_to_the_backend_entry(self, dolcebot):
        assert failed_tests_command(dolcebot, [BACKEND_ID]) == (
            f"cd backend && ./bm-backend-tests.sh {BACKEND_ID}"
        )

    def test_several_backend_ids_share_one_part_in_order(self, dolcebot):
        assert failed_tests_command(dolcebot, [MIGRATION_ID, BACKEND_ID]) == (
            f"cd backend && ./bm-backend-tests.sh {MIGRATION_ID} {BACKEND_ID}"
        )

    def test_a_parametrized_id_is_quoted(self, dolcebot):
        test_id = f"{BACKEND_ID}[a b]"
        assert failed_tests_command(dolcebot, [test_id]) == (
            f"cd backend && ./bm-backend-tests.sh {shlex.quote(test_id)}"
        )

    def test_mixed_backend_and_frontend_ids_join_two_parts(self, dolcebot):
        # Frontend first in the ids; the parts still follow the entries' order.
        command = failed_tests_command(dolcebot, ["src/bad.test.ts", BACKEND_ID])
        assert command == "sh -c " + shlex.quote(
            f"(cd backend && ./bm-backend-tests.sh {BACKEND_ID}) && "
            "(cd frontend && npx jest src/bad.test.ts)"
        )

    def test_a_jest_path_with_brackets_runs_by_path(self, dolcebot):
        assert failed_tests_command(dolcebot, ["src/app/[id]/page.test.tsx"]) == (
            "cd frontend && npx jest --runTestsByPath 'src/app/[id]/page.test.tsx'"
        )

    def test_the_ids_a_red_run_names_are_the_ids_it_takes(self, dolcebot):
        output = (
            f"FAILED {BACKEND_ID} - AssertionError: boom\n"
            " FAIL  src/bad.test.ts\n  ● bad › breaks\n"
        )
        ids = failing_tests(output)
        assert ids == [BACKEND_ID, "src/bad.test.ts"]
        assert failed_tests_command(dolcebot, ids).startswith("sh -c ")

    def test_an_id_outside_every_entry_gives_none(self, dolcebot):
        assert failed_tests_command(dolcebot, ["other/tests/test_x.py::t"]) is None

    def test_one_unknown_id_among_known_ones_gives_none(self, dolcebot):
        ids = [BACKEND_ID, "DolceEngine/tests/test_gone.py::t"]
        assert failed_tests_command(dolcebot, ids) is None

    def test_no_ids_gives_none(self, dolcebot):
        assert failed_tests_command(dolcebot, []) is None


class TestWithoutNarrowCommands:
    def _config(self, tmp_path, command: str) -> BuildConfig:
        return BuildConfig(project_dir=tmp_path, test_command=command)

    def test_a_chained_test_command_gives_none(self, tmp_path):
        config = self._config(
            tmp_path, "bash -c 'cd backend && ./bm-backend-tests.sh; npx jest'"
        )
        assert failed_tests_command(config, [BACKEND_ID]) is None

    def test_a_pytest_command_gets_the_ids_appended(self, tmp_path):
        config = self._config(tmp_path, "uv run pytest -q")
        assert failed_tests_command(config, ["tests/test_a.py::test_one"]) == (
            "uv run pytest -q tests/test_a.py::test_one"
        )

    def test_a_jest_command_gets_the_paths_appended(self, tmp_path):
        config = self._config(tmp_path, "npx jest --ci")
        assert failed_tests_command(config, ["src/bad.test.ts"]) == (
            "npx jest --ci src/bad.test.ts"
        )

    def test_an_unknown_runner_gives_none(self, tmp_path):
        config = self._config(tmp_path, "make test")
        assert failed_tests_command(config, ["tests/test_a.py::t"]) is None

    def test_no_test_command_gives_none(self, tmp_path):
        assert failed_tests_command(BuildConfig(project_dir=tmp_path), ["t"]) is None

    def test_no_ids_gives_none(self, tmp_path):
        config = self._config(tmp_path, "pytest -q")
        assert failed_tests_command(config, []) is None


class TestEntryRunner:
    def test_an_entry_with_no_runner_takes_it_from_its_command(self, tmp_path):
        _write(tmp_path, "api/tests/test_a.py")
        config = BuildConfig(project_dir=tmp_path, test_command="make test")
        config.narrow_commands = [NarrowCommand("api", "python -m pytest -q")]
        assert failed_tests_command(config, ["tests/test_a.py::t"]) == (
            "cd api && python -m pytest -q tests/test_a.py::t"
        )

    def test_an_entry_it_cannot_narrow_gives_none(self, tmp_path):
        _write(tmp_path, "api/tests/test_a.py")
        config = BuildConfig(project_dir=tmp_path, test_command="make test")
        config.narrow_commands = [NarrowCommand("api", "make api-test")]
        assert failed_tests_command(config, ["tests/test_a.py::t"]) is None

    def test_an_entry_at_the_project_root_runs_without_cd(self, tmp_path):
        _write(tmp_path, "tests/test_a.py")
        config = BuildConfig(project_dir=tmp_path, test_command="make test")
        config.narrow_commands = [NarrowCommand(".", "./run.sh", "pytest")]
        assert failed_tests_command(config, ["tests/test_a.py::t"]) == (
            "./run.sh tests/test_a.py::t"
        )


def test_the_two_part_command_runs_each_part_in_its_directory(tmp_path, monkeypatch):
    """The engine runs the command through `run_test_suite`, an argv with no
    shell: each part must run in its own directory with only its ids."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    root = tmp_path / "repo"
    record = '#!/bin/sh\nprintf "%s\\n" "$@" > ran.txt\n'
    for rel in ("backend/run-tests.sh", "frontend/run-jest.sh"):
        _write(root, rel, record).chmod(0o755)
    _write(root, "backend/DolceEngine/tests/test_permissions.py")
    _write(root, "frontend/src/bad.test.ts")
    config = BuildConfig(project_dir=root, test_command="bash -c 'a; b'")
    config.narrow_commands = [
        NarrowCommand("backend", "./run-tests.sh", "pytest"),
        NarrowCommand("frontend", "./run-jest.sh", "jest"),
    ]
    command = failed_tests_command(config, [BACKEND_ID, "src/bad.test.ts"])
    code, output = run_test_suite(command, root)
    assert code == 0, output
    assert (root / "backend" / "ran.txt").read_text() == f"{BACKEND_ID}\n"
    assert (root / "frontend" / "ran.txt").read_text() == "src/bad.test.ts\n"


# --- git_ops.fast_forward_to -------------------------------------------------


@pytest.fixture
def history(tmp_path):
    """A repo at R, whose parent is P, whose parent is the first commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    shas = []
    for name in ("first", "p", "r"):
        _write(root, f"{name}.py", f"{name} = 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", name)
        shas.append(_git(root, "rev-parse", "HEAD"))
    return root, *shas


class TestFastForwardTo:
    def test_moves_head_from_the_parent_to_the_commit(self, history):
        root, _, p, r = history
        _git(root, "reset", "-q", "--hard", p)
        assert git_ops.fast_forward_to(root, r) is True
        assert _git(root, "rev-parse", "HEAD") == r
        assert (root / "r.py").read_text() == "r = 1\n"
        assert git_ops.tree_is_clean(root)

    def test_keeps_buildme_bookkeeping(self, history):
        root, _, p, r = history
        _git(root, "reset", "-q", "--hard", p)
        _write(root, ".buildme-test-runs/x-refactor-check.log", "log\n")
        assert git_ops.fast_forward_to(root, r) is True
        assert (root / ".buildme-test-runs/x-refactor-check.log").exists()

    def test_refuses_a_dirty_tree(self, history):
        root, _, p, r = history
        _git(root, "reset", "-q", "--hard", p)
        (root / "p.py").write_text("p = 2  # mine\n")
        assert git_ops.fast_forward_to(root, r) is False
        assert _git(root, "rev-parse", "HEAD") == p
        assert (root / "p.py").read_text() == "p = 2  # mine\n"

    def test_refuses_an_untracked_file(self, history):
        root, _, p, r = history
        _git(root, "reset", "-q", "--hard", p)
        _write(root, "new.py", "n = 1\n")
        assert git_ops.fast_forward_to(root, r) is False
        assert _git(root, "rev-parse", "HEAD") == p

    def test_refuses_when_head_is_not_the_parent(self, history):
        root, first, _, r = history
        _git(root, "reset", "-q", "--hard", first)
        assert git_ops.fast_forward_to(root, r) is False
        assert _git(root, "rev-parse", "HEAD") == first

    def test_refuses_when_head_is_already_the_commit(self, history):
        root, _, _, r = history
        assert git_ops.fast_forward_to(root, r) is False
        assert _git(root, "rev-parse", "HEAD") == r

    def test_refuses_when_head_moved_past_the_parent(self, history):
        root, _, p, r = history
        _git(root, "reset", "-q", "--hard", p)
        _write(root, "other.py", "o = 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "other")
        other = _git(root, "rev-parse", "HEAD")
        assert git_ops.fast_forward_to(root, r) is False
        assert _git(root, "rev-parse", "HEAD") == other

    @pytest.mark.parametrize("sha", ["", "0" * 40, "not-a-ref"])
    def test_refuses_no_or_an_unknown_commit(self, history, sha):
        root, _, p, _ = history
        _git(root, "reset", "-q", "--hard", p)
        assert git_ops.fast_forward_to(root, sha) is False
        assert _git(root, "rev-parse", "HEAD") == p
