"""Venv bootstrap script for multiplai plugin.

Creates and populates a Python virtual environment on first session.
Idempotent — skips if venv exists and requirements hash matches.
"""

import hashlib
import os
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


# Module name kept split from the "import" keyword: this script runs
# *before* the venv exists and must never import the SDK itself — it
# only probes a *candidate* interpreter for it via a subprocess.
_SDK_MODULE = "claude_agent_sdk"


def _python_has_sdk(python: str | Path) -> bool:
    """True if *python* can import ``claude_agent_sdk``."""
    try:
        return subprocess.run(
            [str(python), "-c", f"import {_SDK_MODULE}"],
            capture_output=True, text=True, timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_SDK_PTH = "_multiplai_runtime_sdk.pth"


def _sdk_python_candidates():
    """Interpreters that might have ``claude_agent_sdk`` installed.

    ``sys.executable`` first — when the host runs a hook it is the
    runtime Python (SDK present). Then a ``.venv`` under
    ``$CLAUDE_MULTIPLAI_HOME`` (case variants: the env value is
    ``multiplai-runtime``; the dir is ``MULTIPLAI-RUNTIME`` on a
    case-insensitive FS).
    """
    yield sys.executable
    home = os.environ.get("CLAUDE_MULTIPLAI_HOME", "").strip()
    if home:
        root = Path(home)
        for base in (root, root.parent / "MULTIPLAI-RUNTIME",
                     root.parent / root.name.upper()):
            cand = base / ".venv" / "bin" / "python"
            if cand.exists():
                yield str(cand)


def _resolve_sdk_site() -> str | None:
    """Locate the site-packages dir that actually contains the SDK.

    ``--system-site-packages`` cannot reach it: the SDK lives inside the
    multiplai runtime *venv*'s site-packages, and that flag only bridges
    to a base interpreter's real (non-venv) site-packages. So instead we
    point the plugin venv at the runtime site-packages via a ``.pth``
    file (see _ensure_sdk_bridge) — no second install, version-locked to
    the runtime. Returns that directory, or ``None`` if the SDK is
    nowhere (then the venv works for everything except llm features,
    rather than the bootstrap failing).
    """
    code = (
        f"import {_SDK_MODULE} as _m, os; "
        "print(os.path.dirname(os.path.dirname(_m.__file__)))"
    )
    for cand in _sdk_python_candidates():
        try:
            r = subprocess.run(
                [str(cand), "-c", code],
                capture_output=True, text=True, timeout=20,
            )
            out = r.stdout
            if r.returncode == 0 and isinstance(out, str) and out.strip():
                return out.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _venv_site_packages(venv_dir: Path) -> Path | None:
    sites = sorted(venv_dir.glob("lib/python*/site-packages"))
    return sites[0] if sites else None


def _ensure_sdk_bridge(venv_dir: Path, sdk_site: str | None) -> bool:
    """Write/refresh the ``.pth`` that adds *sdk_site* to the venv path.

    Idempotent: only writes when missing or stale. Returns True when the
    bridge is in place (or already correct), False otherwise.
    """
    if not sdk_site:
        return False
    site = _venv_site_packages(venv_dir)
    if site is None:
        return False
    pth = site / _SDK_PTH
    try:
        if not pth.exists() or pth.read_text().strip() != sdk_site:
            pth.write_text(sdk_site + "\n")
        return True
    except OSError:
        return False


def _create_venv(venv_dir: Path) -> None:
    """Create a venv with --system-site-packages.

    The flag still buys system packages where present; the SDK itself is
    wired separately via _ensure_sdk_bridge (it lives in a venv, which
    --system-site-packages cannot reach).

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
    """Create venv, install dependencies, and wire the SDK if needed.

    Idempotent: compares SHA-256 of requirements.txt against the stored
    marker hash and skips all work when they match.

    SDK wiring: ``claude_agent_sdk`` lives in the multiplai runtime
    venv's site-packages, which ``--system-site-packages`` cannot reach.
    Bootstrap writes a ``.pth`` (``_multiplai_runtime_sdk.pth``) into the
    plugin venv pointing at the runtime site-packages — no second
    install, version-locked to the runtime. A ``.sdk-bridge`` sentinel
    records the wired path so the steady state is a zero-subprocess
    no-op; the SDK resolution is a one-time migration cost, not an
    every-session tax. Loop-safe: the sentinel is written even when no
    SDK is found (value ``none``), so we never re-probe in a loop.
    """
    paths = get_paths()
    venv_dir = paths.venv_dir()
    req_file = paths.plugin_root() / "requirements.txt"
    marker = venv_dir / ".bootstrap-complete"
    sentinel = venv_dir / ".sdk-bridge"

    # Fast path (the D4 < 50ms no-op, zero subprocess): venv present,
    # requirements current, and the SDK bridge already resolved.
    if (
        venv_dir.exists()
        and _is_up_to_date(marker, req_file)
        and sentinel.exists()
    ):
        logger.info("Venv is up to date, skipping bootstrap")
        return

    # (Re)build only when missing or requirements changed — the SDK
    # wiring below is independent and never needs a rebuild.
    if not (venv_dir.exists() and _is_up_to_date(marker, req_file)):
        logger.info("Bootstrapping venv at %s", venv_dir)
        _create_venv(venv_dir)
        if req_file.exists():
            _install_requirements(venv_dir, req_file, marker)
        _write_marker(marker, req_file)

    # SDK bridge: point the venv at the runtime site-packages. This is
    # what makes claude_agent_sdk importable for standalone/skill runs
    # (hooks already get host injection). Cheap and idempotent.
    sdk_site = _resolve_sdk_site()
    bridged = _ensure_sdk_bridge(venv_dir, sdk_site)
    try:
        sentinel.write_text(sdk_site or "none")
    except OSError:
        pass
    if sdk_site and bridged:
        venv_python = venv_dir / "bin" / "python"
        if venv_python.exists() and not _python_has_sdk(venv_python):
            logger.warning(
                "SDK bridge written (%s) but venv still cannot load "
                "claude_agent_sdk", sdk_site,
            )
    elif not sdk_site:
        logger.warning(
            "claude_agent_sdk not found in any candidate interpreter; "
            "venv works for everything except llm features"
        )
    logger.info("Venv bootstrap complete")


if __name__ == "__main__":
    bootstrap()
