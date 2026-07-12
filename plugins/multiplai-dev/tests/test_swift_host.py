"""Tripwire tests for swift-build's host/bridge detection (swift-host.sh).

The SSH-user guard must live inside the non-Darwin branch: a vanilla Mac with
no bridge config runs locally and never sees an SSH error, a plain Linux box
is told the real constraint (macOS/Xcode), and only a container missing its
bridge config is told about SSH_BUILD_USER. The scripts are exercised for real
with PATH shims (fake `uname` to simulate Darwin) — no SSH, no Xcode needed.

The test host is itself a Linux container (/.dockerenv exists), so the
"plain Linux" scenario relies on the MULTIPLAI_CONTAINER=0 override.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

_BASH = shutil.which("bash") or "/bin/bash"
_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills" / "swift-build" / "scripts" / "swift-host.sh"
)


def _bin_dir(tmp_path: Path, fake_uname: str | None = None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if fake_uname:
        shim = bin_dir / "uname"
        shim.write_text(f"#!/bin/bash\necho {fake_uname}\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    else:
        real = shutil.which("uname")
        assert real
        (bin_dir / "uname").symlink_to(real)
    return bin_dir


def _run(args, tmp_path: Path, fake_uname: str | None = None, **extra_env):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(_bin_dir(tmp_path, fake_uname)),
        "HOME": str(home),
    }
    env.update(extra_env)
    return subprocess.run(
        [_BASH, str(_SCRIPT), *args],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )


def test_vanilla_mac_never_hits_the_ssh_guard(tmp_path):
    """Regression for the top-of-script guard: with no SSH vars set, a Darwin
    host must proceed to run the command locally (here: fail on missing xcrun),
    not abort with a bridge error."""
    res = _run(["sim", "list"], tmp_path, fake_uname="Darwin")
    assert "no SSH user" not in res.stderr
    assert "SSH_BUILD_USER" not in res.stderr
    assert res.returncode != 1  # 127: xcrun not found — the local path ran


def test_plain_linux_names_macos_not_the_bridge(tmp_path):
    res = _run(["sim", "list"], tmp_path, MULTIPLAI_CONTAINER="0")
    assert res.returncode == 1
    assert "needs macOS" in res.stderr
    assert "SSH_BUILD_USER" not in res.stderr


def test_container_without_bridge_names_the_bridge(tmp_path):
    res = _run(["sim", "list"], tmp_path, MULTIPLAI_CONTAINER="1")
    assert res.returncode == 1
    assert "container→host bridge" in res.stderr
    assert "SSH_BUILD_USER" in res.stderr
