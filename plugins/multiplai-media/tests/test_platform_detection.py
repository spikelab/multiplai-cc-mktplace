"""Tripwire tests for platform/container detection in the media skill scripts.

A container is detected explicitly (MULTIPLAI_CONTAINER=1, /.dockerenv fallback,
MULTIPLAI_CONTAINER=0 override) — never inferred from "not macOS". A plain
Linux user must get the honest constraint (mlx-whisper is Apple-Silicon-only)
and never an SSH-bridge error; a container user without the bridge gets the
bridge message. These tests run the real bash scripts in controlled
environments (PATH shims, scratch HOME) — no network, no SSH.

The test host is itself a Linux container (/.dockerenv exists), so the
"plain Linux" scenarios rely on the MULTIPLAI_CONTAINER=0 override.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TRANSCRIBE = _PLUGIN_ROOT / "skills" / "transcribe" / "scripts" / "transcribe.sh"
_YT = _PLUGIN_ROOT / "skills" / "youtube-transcript" / "scripts" / "yt-transcript.sh"
_BOOTSTRAP = _PLUGIN_ROOT / "skills" / "screen-demo" / "scripts" / "bootstrap.sh"

_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world from the fake subtitles
"""

_FAKE_YTDLP = r"""#!/bin/bash
# Offline stand-in for yt-dlp: answers --version/--print, "downloads" subtitles.
out=""
prev=""
for a in "$@"; do
  [ "$prev" = "--output" ] && out="$a"
  prev="$a"
done
case " $* " in
  *" --version "*) echo "2026.01.01"; exit 0 ;;
  *"%(title)s"*)   echo "Test Video"; exit 0 ;;
  *"%(id)s"*)      echo "abc12345678"; exit 0 ;;
  *" --write-sub "*|*" --write-auto-sub "*)
    printf '%s' "$VTT_BODY" > "${out}.en.vtt"; exit 0 ;;
esac
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _shim_dir(tmp_path: Path, tools: list[str], fakes: dict[str, str] | None = None) -> Path:
    """Build a bin dir with symlinks to real tools plus fake executables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in tools:
        real = shutil.which(tool)
        assert real, f"test needs {tool} on the host"
        target = bin_dir / tool
        if not target.exists():
            target.symlink_to(real)
    for name, body in (fakes or {}).items():
        _write_exec(bin_dir / name, body)
    return bin_dir


def _clean_env(tmp_path: Path, path: str, **extra: str) -> dict[str, str]:
    """Minimal env: no SSH/bridge vars, scratch HOME, controlled PATH."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir(exist_ok=True)
    env = {
        "PATH": path,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cfg),
    }
    env.update(extra)
    return env


def _run(script: Path, args: list[str], env: dict[str, str], cwd: Path):
    return subprocess.run(
        [_BASH, str(script), *args],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=60,
    )


class TestTranscribe:
    def test_plain_linux_names_apple_silicon_not_the_bridge(self, tmp_path):
        bins = _shim_dir(tmp_path, ["uname"])
        env = _clean_env(tmp_path, str(bins), MULTIPLAI_CONTAINER="0")
        res = _run(_TRANSCRIBE, ["whatever.m4a"], env, tmp_path)
        assert res.returncode == 1
        assert "Apple Silicon" in res.stderr
        assert "whisper.cpp" in res.stderr or "faster-whisper" in res.stderr
        assert "SSH" not in res.stderr
        assert "bridge" not in res.stderr

    def test_container_without_bridge_names_the_bridge(self, tmp_path):
        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"\x00")
        bins = _shim_dir(tmp_path, ["uname", "dirname", "basename", "cat"])
        env = _clean_env(tmp_path, str(bins), MULTIPLAI_CONTAINER="1")
        res = _run(_TRANSCRIBE, [str(audio)], env, tmp_path)
        assert res.returncode == 1
        assert "container→host bridge" in res.stderr
        assert "SSH_BUILD_USER" in res.stderr


class TestScreenDemoBootstrap:
    def _no_ffmpeg_env(self, tmp_path, fake_uname: str | None = None, **extra):
        fakes = {}
        tools = ["dirname"]
        if fake_uname:
            fakes["uname"] = f"#!/bin/bash\necho {fake_uname}\n"
        else:
            tools.append("uname")
        bins = _shim_dir(tmp_path, tools, fakes)
        return _clean_env(tmp_path, str(bins), **extra)

    def test_mac_hint_is_brew(self, tmp_path):
        env = self._no_ffmpeg_env(tmp_path, fake_uname="Darwin")
        res = _run(_BOOTSTRAP, [], env, tmp_path)
        assert res.returncode == 1
        assert "brew install ffmpeg" in res.stderr

    def test_container_hint_is_image(self, tmp_path):
        env = self._no_ffmpeg_env(tmp_path, MULTIPLAI_CONTAINER="1")
        res = _run(_BOOTSTRAP, [], env, tmp_path)
        assert res.returncode == 1
        assert "baked into the container image" in res.stderr

    def test_plain_linux_hint_is_package_manager(self, tmp_path):
        env = self._no_ffmpeg_env(tmp_path, MULTIPLAI_CONTAINER="0")
        res = _run(_BOOTSTRAP, [], env, tmp_path)
        assert res.returncode == 1
        assert "package manager" in res.stderr
        assert "container image" not in res.stderr


class TestYtTranscript:
    def _fake_python3(self) -> str:
        real = shutil.which("python3")
        assert real
        return (
            "#!/bin/bash\n"
            'if [ "$1" = "-c" ] && [[ "$2" == *"import yt_dlp"* ]]; then exit 1; fi\n'
            f'exec {real} "$@"\n'
        )

    def test_no_workspace_never_invents_home_inbox(self, tmp_path):
        """Container mode + no workspace file: subtitles land in cwd, ~/INBOX is
        never created (previously the script mkdir'd $HOME/INBOX)."""
        bins = _shim_dir(
            tmp_path,
            ["uname", "dirname", "basename", "cat", "grep", "sed", "tr", "head",
             "find", "wc", "mktemp", "rm", "mkdir"],
            fakes={
                "yt-dlp": _FAKE_YTDLP,
                "python3": self._fake_python3(),
                "uv": "#!/bin/bash\nexit 1\n",
            },
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        env = _clean_env(
            tmp_path, str(bins),
            MULTIPLAI_CONTAINER="1", VTT_BODY=_VTT,
        )
        res = _run(_YT, ["https://youtube.com/watch?v=abc12345678"], env, run_dir)
        assert res.returncode == 0, res.stderr
        out = run_dir / "Test Video-transcript.txt"
        assert out.exists(), (res.stdout, res.stderr)
        assert "Hello world" in out.read_text()
        assert not (Path(env["HOME"]) / "INBOX").exists()

    def test_plain_linux_audio_fallback_names_apple_silicon(self, tmp_path):
        """Plain Linux + --audio-fallback + no subtitles: the error names the
        real constraint, not an SSH bridge."""
        fake_ytdlp_no_subs = _FAKE_YTDLP.replace(
            'printf \'%s\' "$VTT_BODY" > "${out}.en.vtt"; exit 0 ;;',
            "exit 1 ;;",
        )
        bins = _shim_dir(
            tmp_path,
            ["uname", "dirname", "basename", "cat", "grep", "sed", "tr", "head",
             "find", "wc", "mktemp", "rm", "mkdir", "ffmpeg"]
            if shutil.which("ffmpeg") else
            ["uname", "dirname", "basename", "cat", "grep", "sed", "tr", "head",
             "find", "wc", "mktemp", "rm", "mkdir"],
            fakes={
                "yt-dlp": fake_ytdlp_no_subs,
                "python3": self._fake_python3(),
                "uv": "#!/bin/bash\nexit 1\n",
            },
        )
        if not shutil.which("ffmpeg"):
            pytest.skip("needs ffmpeg on the host to reach the mlx_whisper check")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        env = _clean_env(tmp_path, str(bins), MULTIPLAI_CONTAINER="0")
        res = _run(_YT, ["https://youtube.com/watch?v=abc12345678", "--audio-fallback"],
                   env, run_dir)
        assert res.returncode == 1
        assert "Apple Silicon" in res.stderr
        assert "bridge" not in res.stderr
