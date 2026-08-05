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
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime import scripts_dir, uv_run_argv  # noqa: E402

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

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_data_dir", str(tmp_path / "data"))
        core_paths._reset_cache()
        try:
            p = lock_path("costs-collector")
            assert p == tmp_path / "data" / "locks" / "costs-collector.lock"
            assert p.parent.is_dir()
        finally:
            core_paths._reset_cache()
