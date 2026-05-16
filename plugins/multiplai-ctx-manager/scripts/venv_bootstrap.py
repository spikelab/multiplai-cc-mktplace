"""Venv bootstrap script for multiplai plugin.

Creates and populates a Python virtual environment on first session.
Idempotent — skips if venv exists and requirements hash matches.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for lib imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("venv_bootstrap")


def _requirements_hash(req_path: Path) -> str:
    """Compute SHA-256 hex digest of requirements.txt contents."""
    return hashlib.sha256(req_path.read_bytes()).hexdigest()


def _is_up_to_date(marker: Path, req_file: Path) -> bool:
    """Check whether the venv marker matches the current requirements hash."""
    if not marker.exists() or not req_file.exists():
        return False
    stored_hash = marker.read_text().strip()
    return stored_hash == _requirements_hash(req_file)


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _create_venv(venv_dir: Path) -> None:
    """Create a venv with --system-site-packages for claude_agent_sdk access.

    Skips creation if the venv Python binary already exists (uv refuses to
    overwrite; python -m venv would unnecessarily reset it).
    """
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        return
    venv_dir.mkdir(parents=True, exist_ok=True)
    try:
        if _has_uv():
            subprocess.run(
                ["uv", "venv", "--system-site-packages", str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as e:
        logger.error("Failed to create venv: %s", e.stderr)
        raise


def _install_requirements(venv_dir: Path, req_file: Path, marker: Path) -> None:
    """Install requirements using uv if available, else pip. Cleans marker on failure."""
    venv_python = venv_dir / "bin" / "python"
    try:
        if _has_uv():
            subprocess.run(
                ["uv", "pip", "install", "-r", str(req_file), "--python", str(venv_python)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-r", str(req_file)],
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as e:
        logger.error("Failed to install requirements: %s", e.stderr)
        # Remove stale marker so next run retries instead of seeing "up to date"
        if marker.exists():
            marker.unlink()
        raise


def _write_marker(marker: Path, req_file: Path) -> None:
    """Write the bootstrap-complete marker with the current requirements hash."""
    if req_file.exists():
        marker.write_text(_requirements_hash(req_file))
    else:
        marker.write_text("no-requirements")


def bootstrap() -> None:
    """Create venv and install dependencies if needed.

    Idempotent: compares SHA-256 of requirements.txt against the stored
    marker hash and skips all work when they match.
    """
    paths = get_paths()
    venv_dir = paths.venv_dir()
    req_file = paths.plugin_root() / "requirements.txt"
    marker = venv_dir / ".bootstrap-complete"

    if _is_up_to_date(marker, req_file):
        logger.info("Venv is up to date, skipping bootstrap")
        return

    logger.info("Bootstrapping venv at %s", venv_dir)

    _create_venv(venv_dir)

    if req_file.exists():
        _install_requirements(venv_dir, req_file, marker)

    _write_marker(marker, req_file)
    logger.info("Venv bootstrap complete")


if __name__ == "__main__":
    bootstrap()
