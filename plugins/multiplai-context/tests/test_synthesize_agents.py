"""The `synthesize_agents.py` entry point and its wiring.

`lib/fleet.py` is tested in `test_fleet.py`; this file covers the CLI around
it and the two callers that keep the fleet view fresh without anyone typing a
command — the SessionStart hook and the host-side post-exit drain. If either
call is dropped, `AGENTS.md` silently freezes at whatever it last said, which
is the one failure mode a fleet view cannot have: you would trust it.
"""

import subprocess
import sys
from datetime import datetime, timezone

import pytest

from conftest import SCRIPTS_DIR, import_script
from lib import fleet
from test_fleet import make_session


@pytest.fixture(scope="module")
def mod():
    return import_script("synthesize_agents", "synthesize_agents.py")


class TestCli:

    def test_it_writes_both_files_under_the_data_dir(self, mod, tmp_path, monkeypatch):
        make_session(tmp_path, "a")
        monkeypatch.setattr(sys, "argv", ["synthesize_agents", "--data-dir", str(tmp_path)])

        assert mod.main() == 0
        assert (tmp_path / "AGENTS.md").exists()
        assert (tmp_path / "fleet.txt").exists()

    def test_stdout_mode_writes_nothing(self, mod, tmp_path, monkeypatch, capsys):
        make_session(tmp_path, "a", project="alpha")
        monkeypatch.setattr(
            sys, "argv",
            ["synthesize_agents", "--data-dir", str(tmp_path), "--stdout"],
        )

        assert mod.main() == 0
        assert "alpha" in capsys.readouterr().out
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "fleet.txt").exists()

    def test_an_empty_data_dir_is_not_an_error(self, mod, tmp_path, monkeypatch):
        """No sessions registered yet is an ordinary state, not a failure."""
        monkeypatch.setattr(sys, "argv", ["synthesize_agents", "--data-dir", str(tmp_path)])

        assert mod.main() == 0
        assert (tmp_path / "fleet.txt").read_text() == ""

    def test_verbose_prints_the_one_line_reading(self, mod, tmp_path, monkeypatch, capsys):
        # Anchored to wall-clock time, not test_fleet's frozen NOW: main()
        # runs against real time, and a notification pinned to a fixed date
        # ages past the quiet threshold ("needs you" → idle) within a day.
        make_session(tmp_path, "a", kind="notification",
                     now=datetime.now(timezone.utc))
        monkeypatch.setattr(
            sys, "argv",
            ["synthesize_agents", "--data-dir", str(tmp_path), "--verbose"],
        )

        mod.main()

        assert "1 need you" in capsys.readouterr().out

    def test_it_is_silent_without_verbose(self, mod, tmp_path, monkeypatch, capsys):
        make_session(tmp_path, "a")
        monkeypatch.setattr(sys, "argv", ["synthesize_agents", "--data-dir", str(tmp_path)])

        mod.main()

        assert capsys.readouterr().out == ""

    def test_help_runs(self):
        """The promote-skill gate executes every bundled entry point with
        --help; a PEP 723 header that cannot even parse args fails it."""
        r = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "synthesize_agents.py"), "--help"],
            capture_output=True, text=True, timeout=60,
        )

        assert r.returncode == 0, r.stderr
        assert "--data-dir" in r.stdout


class TestNoLlm:
    """Pure aggregation. The moment this needs a model it stops being safe to
    run from a hook on every session start."""

    def test_the_script_imports_no_model_client(self):
        src = (SCRIPTS_DIR / "synthesize_agents.py").read_text()
        lib_src = (SCRIPTS_DIR / "lib" / "fleet.py").read_text()

        for text in (src, lib_src):
            assert "model_client" not in text
            assert "create_client" not in text

    def test_the_library_needs_nothing_outside_the_plugin(self):
        """`lib/fleet.py` is imported directly by session_start.py rather than
        shelled out to, so it must not drag in a git-pinned dependency."""
        src = (SCRIPTS_DIR / "lib" / "fleet.py").read_text()

        assert "multiplai_core" not in src


class TestWiring:

    def test_session_start_refreshes_the_fleet_view(self):
        import session_start

        assert session_start.write_fleet_view is fleet.write_fleet_view

    def test_the_host_drain_refreshes_the_fleet_view(self):
        """The walk-away moment: the last tab just closed and no session will
        open for days, so this is the only chance to record where it stopped."""
        drain = import_script("drain_extractions", "drain_extractions.py")

        assert drain.write_fleet_view is fleet.write_fleet_view

    def test_the_drain_refreshes_even_without_extract_learnings(self, tmp_path, monkeypatch):
        """A broken install that cannot extract (extract_learnings.py gone)
        still exits 1 — but the refresh needs nothing from extraction, so
        the walk-away view must not be skipped with it."""
        drain = import_script("drain_extractions", "drain_extractions.py")
        refreshed = []
        monkeypatch.setattr(drain, "write_fleet_view", refreshed.append)
        # Point the script's notion of "beside me" at an empty directory.
        monkeypatch.setattr(drain, "__file__", str(tmp_path / "empty" / "drain.py"))

        rc = drain.main(["--data-dir", str(tmp_path)])

        assert rc == 1
        assert refreshed == [tmp_path.resolve()]

    def test_session_start_calls_it_in_process(self):
        """A `uv run` subprocess here would pay a cold-start (and possibly a
        network fetch, given the PEP 723 git pin) on every session start, to
        do a few file reads."""
        src = (SCRIPTS_DIR / "session_start.py").read_text()
        call = src.split("write_fleet_view(data_dir)")[0].rsplit("try:", 1)[1]

        assert "Popen" not in call

    def test_neither_caller_lets_it_break_the_session(self):
        """A fleet view is a convenience. A read-only data dir, a full disk, a
        checkpoint written by a newer version — none of those may take down a
        session start or swallow a drain, so both call sites are the lone
        statement inside a bare try."""
        for name in ("session_start.py", "drain_extractions.py"):
            src = (SCRIPTS_DIR / name).read_text()
            guarded = src.split("write_fleet_view(data_dir)")[0].rsplit("try:", 1)[1]
            assert guarded.strip() == ""
            after = src.split("write_fleet_view(data_dir)")[1]
            assert after.lstrip().startswith("except Exception:")
