"""Item 33 of plan-factory-merged-2026-10-29.md: before planning or TDD spends
anything, the build checks with no model what a paid call needs, prints one
line per failed check and exits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from build_pipeline import orchestrator, preflight
from build_pipeline.config import BuildConfig
from build_pipeline.epic import CHILD_ENV, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.narrow_tests import NarrowCommand

from tests.epic_row_helpers import use_workspace


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    (project / "specs").mkdir()
    (project / "specs" / "config.yaml").write_text("tdd:\n  test_command: true\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "main")
    return project


def _config(project: Path, **kwargs) -> BuildConfig:
    config = BuildConfig(project_dir=project, change_name="feat", test_command="true")
    config.specs_dir = project / "specs"
    for key, value in kwargs.items():
        setattr(config, key, value)
    return config


def _checks(failed) -> list[str]:
    return [f.check for f in failed]


def test_every_check_passes_on_a_ready_repo(repo, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    config = _config(repo, db_check="true")
    assert preflight.run(config, publishes=True, target="main") == []


def test_a_config_that_does_not_parse_fails(repo):
    (repo / "specs" / "config.yaml").write_text("tdd: [unclosed\n")
    failed = preflight.run(_config(repo), publishes=False, target=None)
    assert _checks(failed) == ["config"]
    assert "does not parse" in failed[0].reason


def test_a_missing_test_runner_fails_for_the_command_and_each_narrow_entry(repo):
    (repo / "backend").mkdir()
    config = _config(
        repo,
        test_command="cd backend && no-such-runner-xyz -q",
        narrow_commands=[NarrowCommand("backend", "./missing-script", None)],
    )
    failed = preflight.run(config, publishes=False, target=None)
    assert _checks(failed) == ["test-runner", "test-runner"]
    assert "no-such-runner-xyz is not on PATH" in failed[0].reason
    assert "./missing-script is not an executable file" in failed[1].reason


# DolceBot's `tdd.test_command` (feature branch DFACCMGMT-1-account-management,
# 2026-10-30), with its machine-specific script path replaced. Its script starts
# with `set -e`, which the runner check looked up on PATH and failed.
DOLCEBOT_TEST_COMMAND = (
    'bash -c \'set -e; R="$PWD"; bash /opt/db2291/tools/local-mysql.sh; '
    'cd "$R/backend"; DOLCE_TEST_DB_HOST=127.0.0.1 DOLCE_TEST_MYSQL_PORT=3306 '
    'DOLCE_TEST_DB_NAME="bm_$(printf %s "$R" | md5sum | cut -c1-10)" '
    '"$HOME/.local/dolce-test-venv/bin/python" -m pytest -q -p no:cacheprovider; '
    'cd "$R/frontend"; [ -d node_modules ] || corepack pnpm install '
    "--frozen-lockfile --prefer-offline; TZ=Europe/Rome corepack pnpm test:ci "
    '--maxWorkers=50% --testPathIgnorePatterns "/node_modules/" '
    '"/__tests__/helpers/" "dolcekey/__tests__/db2187-controls\\.test\\.tsx" '
    '"channex/__tests__/bulk-edit-dialog\\.test\\.tsx"\''
)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'set -e; true'",
        DOLCEBOT_TEST_COMMAND,
        "bash -c 'export X=1; true'",
        "bash -c 'X=1 true'",
        "bash -c 'if true; then true; fi'",
        "bash -c 'source /dev/null && true'",
        "sh -c 'DJANGO_SETTINGS_MODULE=app.settings true'",
        # A `cd` the shell expands: gates runs the shell, not the `cd`.
        "bash -c 'cd $HOME && true'",
        "sh -c '(cd . && true) && (cd . && true)'",
    ],
)
def test_a_shell_script_that_starts_with_a_builtin_passes(repo, command):
    config = _config(repo, test_command=command)
    assert preflight.run(config, publishes=False, target=None) == []


def test_a_narrow_entry_whose_script_starts_with_a_builtin_passes(repo):
    (repo / "backend").mkdir()
    config = _config(
        repo, narrow_commands=[NarrowCommand("backend", "bash -c 'set -e; true'", None)]
    )
    assert preflight.run(config, publishes=False, target=None) == []


def test_a_shell_script_whose_program_is_missing_still_fails(repo):
    config = _config(repo, test_command="bash -c 'nosuchtool -q'")
    (failed,) = preflight.run(config, publishes=False, target=None)
    assert failed.check == "test-runner"
    assert "nosuchtool is not on PATH" in failed.reason


def test_only_sh_bash_and_zsh_are_unwrapped(repo):
    config = _config(repo, test_command="nosuchshell -c 'set -e; true'")
    (failed,) = preflight.run(config, publishes=False, target=None)
    assert failed.check == "test-runner"
    assert "nosuchshell is not on PATH" in failed.reason


def test_a_runner_in_a_directory_that_does_not_exist_fails(repo):
    config = _config(repo, test_command=f"cd nowhere && {sys.executable} -V")
    (failed,) = preflight.run(config, publishes=False, target=None)
    assert failed.check == "test-runner" and "does not exist" in failed.reason


def test_a_target_branch_origin_lacks_fails(repo):
    (failed,) = preflight.run(_config(repo), publishes=True, target="ENG-1-feat")
    assert failed.check == "target-branch"
    assert failed.reason == "origin has no branch ENG-1-feat"


def test_gh_is_asked_only_when_the_run_publishes(repo, monkeypatch):
    asked = []

    def permission(path):
        asked.append(path)
        return "READ", ""

    monkeypatch.setattr(preflight, "_gh_permission", permission)
    assert preflight.run(_config(repo), publishes=False, target=None) == []
    assert asked == []
    (failed,) = preflight.run(_config(repo), publishes=True, target="main")
    assert failed.check == "gh" and "permission READ" in failed.reason


def test_gh_that_cannot_read_the_repo_fails(repo, monkeypatch):
    monkeypatch.setattr(
        preflight, "_gh_permission", lambda path: (None, "HTTP 401: Bad credentials")
    )
    (failed,) = preflight.run(_config(repo), publishes=True, target="main")
    assert failed.reason == "gh cannot read the repository: HTTP 401: Bad credentials"


def test_a_test_db_that_does_not_answer_fails(repo, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    command = (
        f"{sys.executable} -c \"import sys; print('connection refused'); sys.exit(2)\""
    )
    (failed,) = preflight.run(
        _config(repo, db_check=command), publishes=False, target=None
    )
    assert failed.check == "test-db"
    assert failed.reason.endswith("exited 2: connection refused")


def test_the_db_check_is_not_run_in_an_untrusted_repo(repo, tmp_path):
    marker = tmp_path / "ran"
    (failed,) = preflight.run(
        _config(repo, db_check=f"touch {marker}"), publishes=False, target=None
    )
    assert failed.check == "test-db" and "not trusted" in failed.reason
    assert not marker.exists()


def test_the_db_check_is_read_from_the_config(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text(
        "tdd:\n  db_check: pg_isready -h localhost\n"
    )
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert config.db_check == "pg_isready -h localhost"


def test_an_epic_held_by_another_process_fails_a_component_built_by_hand(
    repo, tmp_path, monkeypatch
):
    use_workspace(monkeypatch, tmp_path)
    epic_dir = store.epic_dir_for(repo, "account-management")
    epic_dir.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, json, os, sys, time\n"
                f"fd = os.open({str(epic_dir / '.lock')!r}, os.O_RDWR | os.O_CREAT)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "os.write(fd, json.dumps({'pid': os.getpid(), 'host': 'h', 'since': 's'}).encode())\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        config = _config(repo, change_name="account-management/account-model")
        (failed,) = preflight.run(config, publishes=False, target=None)
        assert failed.check == "epic-lock"
        assert failed.reason.startswith("epic account-management is running now (pid ")
        # A child the epic launched runs under its parent's lock.
        monkeypatch.setenv(CHILD_ENV, "1")
        assert preflight.run(config, publishes=False, target=None) == []
    finally:
        holder.kill()
        holder.wait()
    monkeypatch.delenv(CHILD_ENV)
    assert preflight.run(config, publishes=False, target=None) == []
    assert epic_lock.holder_elsewhere(epic_dir) is None


def test_each_failed_check_prints_one_line(capsys):
    preflight.report(
        [preflight.Failed("gh", "no"), preflight.Failed("test-db", "down")]
    )
    assert capsys.readouterr().out.splitlines() == [
        "PREFLIGHT:FAILED:gh:no",
        "PREFLIGHT:FAILED:test-db:down",
    ]


def test_the_build_stops_before_any_model_call(repo, monkeypatch, capsys):
    """`_preflight` runs before RESEARCH; a failure returns 1 and the phases
    that call a model never start."""
    config = _config(repo, test_command="no-such-runner-xyz")
    state = SimpleNamespace(branch="", worktree_path="", is_phase_complete=None)
    assert orchestrator._preflight(config, state) == 1
    out = capsys.readouterr().out
    assert out.startswith("PREFLIGHT:FAILED:test-runner:")
    source = Path(orchestrator.__file__).read_text()
    assert source.index("rc = _preflight(config, state)") < source.index(
        "# Phase: Research"
    )
