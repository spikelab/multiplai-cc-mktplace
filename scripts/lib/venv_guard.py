"""Venv re-exec guard for multiplai plugin scripts.

Ensures hook scripts run inside the plugin's virtual environment.
If not already in the venv, re-execs the current script using the
venv's Python interpreter via os.execv, preserving all arguments.

Usage in hook scripts::

    from lib.venv_guard import ensure_venv_python
    ensure_venv_python()  # re-execs if not already in venv
"""

import os
import sys
from pathlib import Path

# Standalone fallback: lib/ -> scripts/ -> multiplai-plugin/
_PLUGIN_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


def _resolve_venv_python() -> Path:
    """Derive the venv Python path from env vars or standalone fallback."""
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or str(
        _PLUGIN_ROOT_FALLBACK / "data"
    )
    return Path(data_dir) / "venv" / "bin" / "python"


def ensure_venv_python() -> None:
    """Re-exec into the plugin venv's Python if not already there.

    Resolution order for the venv location:
      1. ``$CLAUDE_PLUGIN_DATA/venv/bin/python``
      2. ``<plugin-root>/data/venv/bin/python`` (standalone fallback)

    If the venv Python exists on disk and differs from the current
    ``sys.executable``, replaces the current process via :func:`os.execv`.
    Otherwise returns immediately (no-op).
    """
    venv_python = _resolve_venv_python()

    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        target = str(venv_python)
        os.execv(target, [target] + sys.argv)
