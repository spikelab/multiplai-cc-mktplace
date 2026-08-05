"""Three runtime primitives every background child in this plugin needs:
how to *spawn* it, how to *supervise* it, and where its cross-session
*locks* live.

All three exist because the obvious answer is wrong in this deployment.

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

**Supervising.** ``subprocess.run(argv, timeout=…)`` kills only the process it
started. Every child here is a ``uv run`` wrapper around a Python script that
itself spawns Claude Code CLI subprocesses, so the thing the timeout kills is
the one process in the tree that was merely relaying — and the work carries on
unsupervised. Observed on 2026-08-05: ``memory_maintainer`` logged
``Dream pass timed out`` at 08:51:57Z and the dream it had "killed" went on to
fan out at 08:52:26Z and write its proposal at 08:55:00Z, with eight CLI
subprocesses still under it. Nothing was cleaned up, and the caller had already
reported failure. :func:`run_supervised` closes that by giving the child its own
process group and killing the *group*, so a timeout means what it says.

**Locks.** Every Claude session runs in its own OrbStack container, so a lock
file under ``/tmp`` is container-local: two concurrent sessions lock two
different files and both proceed. The workspace is one shared filesystem
inside the VM's single kernel, so a lock there really does exclude across
session containers — see ``scripts/dream.py:acquire_run_lock`` for the long
form of the same reasoning.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Sequence

from multiplai_core.paths import get_paths

logger = logging.getLogger(__name__)


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


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL *proc*'s whole process group, falling back to the process alone.

    SIGKILL rather than SIGTERM: this runs only after a deadline the child
    already blew through, and the point is that nothing in the tree outlives the
    call. A cooperative shutdown would have to be waited on, which is the thing
    that just failed.

    Every failure mode is benign and must not mask the ``TimeoutExpired`` the
    caller is about to see: the child may have exited between the timeout and
    the signal (``ProcessLookupError``), and a platform without process groups
    raises rather than pretending.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("killpg on pid %d failed (%s) — killing the process alone",
                     proc.pid, exc)
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_supervised(
    argv: Sequence[str], *, timeout: float, text: bool = True
) -> subprocess.CompletedProcess:
    """Run *argv* to completion, killing its whole process tree on timeout.

    A drop-in replacement for ``subprocess.run(argv, capture_output=True,
    text=text, timeout=timeout)``: same ``CompletedProcess`` on success, same
    ``subprocess.TimeoutExpired`` (carrying whatever output was captured) when
    the deadline passes. The difference is what a timeout *does* — see this
    module's docstring under "Supervising".

    ``start_new_session=True`` makes the child a session and process-group
    leader, so its own descendants inherit that group and one ``killpg`` reaches
    all of them. It also detaches the child from this process's controlling
    terminal, which is correct for every caller here (they are hooks and
    unattended passes, never interactive).
    """
    proc = subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        # Second communicate() with no deadline: the pipes are closed by the
        # kill, so this returns immediately and reaps the child instead of
        # leaving a zombie. Its output is handed to TimeoutExpired so a caller
        # logging the failure still sees how far the child got.
        try:
            out, err = proc.communicate()
        except Exception:  # pragma: no cover - defensive
            out, err = None, None
        raise subprocess.TimeoutExpired(list(argv), timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


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
