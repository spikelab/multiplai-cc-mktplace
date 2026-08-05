"""Two runtime primitives every background child in this plugin needs:
how to *spawn* it, and where its cross-session *locks* live.

Both exist because the obvious answer is wrong in this deployment.

**Spawning.** Scripts in ``scripts/`` get their dependencies from
``scripts/pyproject.toml`` (a member of the repo-root uv workspace, and
self-resolving when the plugin is installed standalone). Running a child with
project resolution *disabled* — as ``uv run`` does by default outside a
project, and as the retired no-project flag did explicitly — kills it with
``ModuleNotFoundError: No module named 'multiplai_core'``. Every spawn
site pipes the child's stderr to ``DEVNULL`` and never awaits it, so the
failure is completely silent — which is exactly how it went unnoticed from
the PEP 723 consolidation (2026-08-04) until the extraction backlog was
found respawning in a loop. Build child argv with :func:`uv_run_argv` and the
``--project`` flag cannot be forgotten.

**Locks.** Every Claude session runs in its own OrbStack container, so a lock
file under ``/tmp`` is container-local: two concurrent sessions lock two
different files and both proceed. The workspace is one shared filesystem
inside the VM's single kernel, so a lock there really does exclude across
session containers — see ``scripts/dream.py:acquire_run_lock`` for the long
form of the same reasoning.
"""

from __future__ import annotations

from pathlib import Path

from multiplai_core.paths import get_paths


def scripts_dir() -> Path:
    """The plugin's ``scripts/`` directory — the uv project root for children.

    Derived from ``__file__`` (this module lives in ``scripts/lib/``) rather
    than from the script being spawned, so it is right even when a caller
    passes an entry point from somewhere else.
    """
    return Path(__file__).resolve().parent.parent


def uv_run_argv(script: Path | str, *args: str) -> list[str]:
    """argv to run *script* as a child under the plugin's uv project."""
    return ["uv", "run", "--project", str(scripts_dir()), str(script), *args]


def lock_path(name: str) -> Path:
    """Path for the cross-session lock *name*, under the workspace data dir.

    Best-effort directory creation: a read-only or racing filesystem must not
    break the caller, which fails open on an unopenable lock anyway.
    """
    d = get_paths().data_dir / "locks"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d / f"{name}.lock"
