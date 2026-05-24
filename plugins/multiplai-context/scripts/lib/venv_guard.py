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


def _bootstrap_venv() -> None:
    """Import and run venv_bootstrap.bootstrap() to recreate a missing venv."""
    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from venv_bootstrap import bootstrap
    bootstrap()


def ensure_venv_python() -> None:
    """Re-exec into the plugin venv's Python if not already there.

    Resolution order for the venv location:
      1. ``$CLAUDE_PLUGIN_DATA/venv/bin/python``
      2. ``<plugin-root>/data/venv/bin/python`` (standalone fallback)

    If the venv Python is missing, bootstraps it first. Then re-execs
    via :func:`os.execv` if the current interpreter differs from the venv.
    """
    venv_python = _resolve_venv_python()

    if not venv_python.exists():
        _bootstrap_venv()

    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        target = str(venv_python)
        os.execv(target, [target] + sys.argv)
