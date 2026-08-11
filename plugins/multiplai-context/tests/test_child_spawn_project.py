"""Every background child must be able to import its dependencies.

The 2026-08-04 outage this guards against: consolidating the 26 PEP 723 script
headers into ``scripts/pyproject.toml`` left the in-code spawn sites launching
children with project resolution disabled. ``scripts/pyproject.toml`` is then
never read, so every child died on ``import multiplai_core`` — and because all
of them are spawned with ``stderr=DEVNULL`` and never awaited, ``Popen``
succeeded and nothing was logged anywhere. Learnings/diary extraction, cost
collection, qmd refresh, the checkpoint writer and the memory maintainer were
all silently dead for a day, with the extraction drain respawning in a loop.

The bug class is "a spawn site hand-rolls its own uv argv", so that is what is
asserted here, not any one call site.
"""

import ast
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import runtime  # noqa: E402
from lib.runtime import run_supervised, scripts_dir, uv_run_argv  # noqa: E402

# Assembled rather than written out, so a repo-wide grep for the retired flag
# stays clean and can itself serve as the coarse check.
FORBIDDEN_FLAG = "--no" + "-project"

PY_SOURCES = sorted(SCRIPTS_DIR.rglob("*.py"))


class TestUvRunArgv:
    def test_argv_names_the_scripts_dir_as_the_uv_project(self):
        argv = uv_run_argv(SCRIPTS_DIR / "collect_costs.py")
        assert argv[:3] == ["uv", "run", "--project"]
        assert argv[3] == str(SCRIPTS_DIR)
        assert argv[4] == str(SCRIPTS_DIR / "collect_costs.py")

    def test_extra_args_follow_the_script(self):
        argv = uv_run_argv(SCRIPTS_DIR / "generate_catalog.py", "--only", "memory")
        assert argv[-3:] == [
            str(SCRIPTS_DIR / "generate_catalog.py"), "--only", "memory",
        ]

    def test_scripts_dir_resolves_to_the_plugin_scripts_dir(self):
        # Derived from lib/runtime.py's own location, so it is right no matter
        # what path the caller hands in.
        assert scripts_dir() == SCRIPTS_DIR.resolve()
        assert (scripts_dir() / "pyproject.toml").is_file()

    def test_the_named_project_actually_declares_the_dependencies(self):
        # The whole point of --project: this is the file that carries
        # multiplai-core. If it stops declaring it, children break again.
        text = (scripts_dir() / "pyproject.toml").read_text(encoding="utf-8")
        assert "multiplai-core" in text


class TestNoSpawnSiteHandRollsUvArgv:
    @pytest.mark.parametrize("path", PY_SOURCES, ids=lambda p: p.name)
    def test_source_never_disables_project_resolution(self, path):
        assert FORBIDDEN_FLAG not in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(PLUGIN_ROOT)} disables uv project resolution — "
            "the child cannot then import multiplai_core, and the failure is "
            "invisible (stderr is DEVNULL). Build argv with lib.runtime."
        )

    @pytest.mark.parametrize("path", PY_SOURCES, ids=lambda p: p.name)
    def test_uv_argv_is_built_only_by_the_shared_helper(self, path):
        """No literal ``["uv", "run", ...]`` list anywhere but lib/runtime.py.

        A hand-rolled list is how the flag came back last time; routing every
        site through one helper is what makes the fix hold.
        """
        if path.name == "runtime.py":
            pytest.skip("lib/runtime.py is the one place that builds the argv")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            head = [
                e.value for e in node.elts[:2]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            assert head[:2] != ["uv", "run"], (
                f"{path.relative_to(PLUGIN_ROOT)}:{node.lineno} builds uv argv "
                "by hand — call lib.runtime.uv_run_argv instead"
            )


class TestLocksLiveInTheWorkspace:
    """A ``/tmp`` lock is container-local under OrbStack, so it excludes
    nothing: two sessions racing on the same diary file lock two different
    paths and both proceed. See ``scripts/dream.py:acquire_run_lock``."""

    LOCK_HOLDERS = [
        SCRIPTS_DIR / "collect_costs.py",
        SCRIPTS_DIR / "qmd_refresh.py",
        SCRIPTS_DIR / "lib" / "extraction.py",
    ]

    @pytest.mark.parametrize("path", LOCK_HOLDERS, ids=lambda p: p.name)
    def test_no_lock_path_outside_the_data_dir(self, path):
        text = path.read_text(encoding="utf-8")
        stray = re.findall(r'["\'][^"\']*/tmp/[^"\']*\.lock', text)
        assert not stray, f"{path.name} still locks under /tmp: {stray}"
        assert "gettempdir" not in text, f"{path.name} still locks in the temp dir"

    def test_lock_path_is_under_the_resolved_data_dir(self, tmp_path, monkeypatch):
        import multiplai_core.paths as core_paths
        from lib.runtime import lock_path

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(tmp_path / "data"))
        core_paths._reset_cache()
        try:
            p = lock_path("costs-collector")
            assert p == tmp_path / "data" / "locks" / "costs-collector.lock"
            assert p.parent.is_dir()
        finally:
            core_paths._reset_cache()


# A child that spawns a grandchild and then outlives its own supervisor's
# deadline — the shape of every real spawn site here, where the direct child is a
# `uv run` wrapper and the work happens in processes below it. The grandchild
# deliberately does NOT start its own session: it must inherit the group the
# supervisor gave its parent, which is what makes one killpg reach it.
_ORPHANING_CHILD = """
import subprocess, sys, time

heartbeat = sys.argv[1]
subprocess.Popen([
    sys.executable, "-c",
    "import sys, time\\n"
    "f = open(sys.argv[1], 'a')\\n"
    "while True:\\n"
    "    f.write('.')\\n"
    "    f.flush()\\n"
    "    time.sleep(0.05)\\n",
    heartbeat,
])
time.sleep(120)
"""


class TestTimeoutKillsTheWholeTree:
    """F2 (log-doctor, 2026-08-05): `subprocess.run(timeout=…)` kills only the
    process it started. On 2026-08-05 the maintainer logged `Dream pass timed out`
    at 08:51:57Z and the dream it had "killed" fanned out at 08:52:26Z and wrote
    its proposal at 08:55:00Z, with eight CLI subprocesses still under it — the
    caller had reported failure while the work carried on unsupervised.
    """

    def test_the_child_is_a_process_group_leader(self, monkeypatch):
        """Without `start_new_session` there is no group to kill, so this is the
        assertion the whole fix rests on."""
        popen_kwargs = {}

        class FakeProc:
            pid = 4242
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return "out", "err"

        monkeypatch.setattr(
            runtime.subprocess, "Popen",
            lambda argv, **kw: (popen_kwargs.update(kw), FakeProc())[1],
        )
        run_supervised(["true"], timeout=5)
        assert popen_kwargs["start_new_session"] is True

    def test_a_timeout_sigkills_the_group_and_reraises(self, monkeypatch):
        signalled = {}
        waits = []

        class FakeProc:
            pid = 4242
            returncode = -9

            def communicate(self, input=None, timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    raise subprocess.TimeoutExpired(["child"], timeout)
                return "partial out", "partial err"

        monkeypatch.setattr(runtime.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(
            runtime.os, "killpg",
            lambda pgid, sig: signalled.update(pgid=pgid, sig=sig),
        )

        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_supervised(["child"], timeout=7)

        assert signalled == {"pgid": 4242, "sig": signal.SIGKILL}
        # The second, deadline-free communicate() is what reaps the child instead
        # of leaving a zombie, and what recovers the output for the caller's log.
        assert waits == [7, None]
        assert caught.value.output == "partial out"
        assert caught.value.stderr == "partial err"

    def test_a_grandchild_does_not_survive_the_timeout(self, tmp_path):
        """The end-to-end property, with real processes: the thing that actually
        orphaned was two levels down, so a mock of the kill cannot prove this."""
        heartbeat = tmp_path / "heartbeat"
        child = tmp_path / "child.py"
        child.write_text(_ORPHANING_CHILD, encoding="utf-8")

        with pytest.raises(subprocess.TimeoutExpired):
            run_supervised([sys.executable, str(child), str(heartbeat)], timeout=2)

        # Guards against a vacuous pass: a grandchild that never ran would also
        # "stop growing".
        assert heartbeat.is_file() and heartbeat.stat().st_size > 0, (
            "the grandchild never started — this test proves nothing")

        time.sleep(0.5)
        settled = heartbeat.stat().st_size
        time.sleep(0.5)
        assert heartbeat.stat().st_size == settled, (
            "the grandchild is still writing — it outlived the killpg")

    def test_the_success_path_matches_subprocess_run(self):
        """Callers were written against `subprocess.run`; the contract has to be
        identical or the swap changes behaviour on the path that works."""
        proc = run_supervised(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('hi'); sys.stderr.write('bye');"
             " sys.exit(3)"],
            timeout=60,
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == (3, "hi", "bye")
