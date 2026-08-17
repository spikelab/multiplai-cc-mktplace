"""Tripwire tests for how yt-transcript.sh reports a yt-dlp failure.

The script's whole reason for capturing yt-dlp's stderr is that the calling
Claude acts on the exit code and shows the user the output. Three failure modes
have to stay distinguishable, because SKILL.md gives each a different script:

  * exit 2 — the video genuinely has no captions
  * exit 4 — a yt-dlp call failed (network, private video, broken extractor);
    the script may never have reached the captions at all
  * exit 5 — a caption track exists but holds no cues

Conflating them is not cosmetic: a yt-dlp failure reported as exit 2 makes the
caller tell the user a video has no subtitles when nothing ever looked.

These run the real bash script against a configurable fake yt-dlp on PATH — no
network, no SSH, no real downloads.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

_BASH = shutil.which("bash") or "/bin/bash"
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_YT = _PLUGIN_ROOT / "skills" / "youtube-transcript" / "scripts" / "yt-transcript.sh"

_URL = "https://youtube.com/watch?v=abc12345678"

_VTT_WITH_CUES = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world from the fake subtitles
"""

# A caption track that exists and is non-empty on disk, but has no cues: header,
# metadata, timestamps, nothing else. This is what the -s guard cannot see.
_VTT_NO_CUES = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000

00:00:02.000 --> 00:00:04.000

"""

# Configurable stand-in for yt-dlp. Each phase reads its own env var, one of:
#   ok   — succeed (and, for a subtitle phase, write $VTT_BODY)
#   none — exit 0 having written nothing, which is what real yt-dlp does when
#          the requested subtitle language simply does not exist
#   fail — exit 1 with a message on stderr: a network error, a private video,
#          a broken extractor
_FAKE_YTDLP = r"""#!/bin/bash
out=""
prev=""
for a in "$@"; do
  [ "$prev" = "--output" ] && out="$a"
  prev="$a"
done
phase() {  # $1 = mode, $2 = label
  if [ "$1" = "fail" ]; then
    echo "ERROR: fake yt-dlp $2 failure" >&2
    exit 1
  fi
}
case " $* " in
  *" --version "*) echo "2026.01.01"; exit 0 ;;
  *"%(title)s"*)
    phase "${YTF_TITLE:-ok}" title
    echo "Test Video"; exit 0 ;;
  *"%(id)s"*)
    phase "${YTF_ID:-ok}" id
    echo "abc12345678"; exit 0 ;;
  *" --write-sub "*)
    phase "${YTF_MANUAL:-none}" manual
    if [ "${YTF_MANUAL:-none}" = "ok" ]; then printf '%s' "$VTT_BODY" > "${out}.en.vtt"; fi
    exit 0 ;;
  *" --write-auto-sub "*)
    phase "${YTF_AUTO:-none}" auto
    if [ "${YTF_AUTO:-none}" = "ok" ]; then printf '%s' "$VTT_BODY" > "${out}.en.vtt"; fi
    exit 0 ;;
  *" -x "*)
    phase "${YTF_AUDIO:-fail}" audio
    : > "$out"; exit 0 ;;
esac
exit 0
"""

_SHIM_TOOLS = [
    "uname", "dirname", "basename", "cat", "grep", "sed", "tr", "head", "tail",
    "find", "wc", "mktemp", "rm", "mkdir", "du", "cut",
]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_python3() -> str:
    """Real python3, except `import yt_dlp` fails — so the script uses the shim."""
    real = shutil.which("python3")
    assert real, "test needs python3 on the host"
    return (
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ] && [[ "$2" == *"import yt_dlp"* ]]; then exit 1; fi\n'
        f'exec {real} "$@"\n'
    )


def _env(tmp_path: Path, *, ffmpeg: bool = False, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in _SHIM_TOOLS:
        real = shutil.which(tool)
        assert real, f"test needs {tool} on the host"
        target = bin_dir / tool
        if not target.exists():
            target.symlink_to(real)
    _write_exec(bin_dir / "yt-dlp", _FAKE_YTDLP)
    _write_exec(bin_dir / "python3", _fake_python3())
    # If the script reaches for uv to self-heal, it must not succeed silently.
    _write_exec(bin_dir / "uv", "#!/bin/bash\nexit 1\n")
    if ffmpeg:
        # Faked, not symlinked: these tests are about the script's decisions,
        # and requiring a real ffmpeg would fail on hosts for reasons that say
        # nothing about the behaviour under test.
        _write_exec(bin_dir / "ffmpeg", "#!/bin/bash\nexit 0\n")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir(exist_ok=True)
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cfg),
        # Not a container: no bridge, no INBOX staging, no self-heal.
        "MULTIPLAI_CONTAINER": "0",
        "VTT_BODY": _VTT_WITH_CUES,
    }
    env.update(extra)
    return env


def _run(tmp_path: Path, args: list[str], env: dict[str, str]):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    res = subprocess.run(
        [_BASH, str(_YT), _URL, *args],
        env=env, cwd=run_dir, capture_output=True, text=True, timeout=60,
    )
    return res, run_dir


class TestMetadataFetch:
    """The first yt-dlp calls in the script, and the first thing a bad URL hits."""

    def test_title_failure_reports_and_uses_its_own_code(self, tmp_path):
        env = _env(tmp_path, YTF_TITLE="fail")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 4, (res.returncode, res.stderr)
        assert "Could not fetch video info" in res.stderr
        assert "fake yt-dlp title failure" in res.stderr

    def test_id_failure_is_not_a_silent_abort(self, tmp_path):
        """The regression: no `||` handler, so `set -e` killed the script with
        the captured log unprinted and the EXIT trap then deleting it. The user
        saw "Fetching video info ..." and then nothing, and the raw yt-dlp
        status escaped — a yt-dlp exit 2 reads to the caller as "no subtitles"."""
        env = _env(tmp_path, YTF_ID="fail")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 4, (res.returncode, res.stdout, res.stderr)
        assert "Could not fetch the video ID" in res.stderr
        assert "fake yt-dlp id failure" in res.stderr


class TestSubtitleFailureVsNoSubtitles:
    def test_no_captions_anywhere_is_exit_2(self, tmp_path):
        """Both calls succeed and find nothing: the video really has none."""
        env = _env(tmp_path, YTF_MANUAL="none", YTF_AUTO="none")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 2, (res.returncode, res.stderr)
        assert "No subtitles available" in res.stderr

    def test_manual_failure_survives_the_auto_attempt(self, tmp_path):
        """The captured-then-destroyed case. Manual subs fail hard; the auto
        attempt exits 0 with no auto-captions (normal for a video that has
        manual subs) and truncates the same stderr file. This used to end at
        "No subtitles available", exit 2, with the real error gone."""
        env = _env(tmp_path, YTF_MANUAL="fail", YTF_AUTO="none")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 4, (res.returncode, res.stderr)
        assert "Manual subtitle download failed" in res.stderr
        assert "fake yt-dlp manual failure" in res.stderr
        assert "No subtitles available" not in res.stderr

    def test_auto_failure_is_reported_too(self, tmp_path):
        env = _env(tmp_path, YTF_MANUAL="none", YTF_AUTO="fail")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 4, (res.returncode, res.stderr)
        assert "Auto-generated subtitle download failed" in res.stderr
        assert "fake yt-dlp auto failure" in res.stderr

    def test_no_fallback_promised_when_none_is_configured(self, tmp_path):
        """Without --audio-fallback there is no fallback to fall back to."""
        env = _env(tmp_path, YTF_MANUAL="fail", YTF_AUTO="fail")
        res, _ = _run(tmp_path, [], env)
        assert res.returncode == 4
        assert "falling back to audio" not in res.stderr.lower()

    def test_fallback_is_announced_when_it_is_configured(self, tmp_path):
        """With the flag set, a failed download is reported but not fatal — the
        run continues to the audio path and stops on the real constraint."""
        env = _env(tmp_path, ffmpeg=True, YTF_MANUAL="fail", YTF_AUTO="fail")
        res, _ = _run(tmp_path, ["--audio-fallback"], env)
        assert "falling back to audio" in res.stderr.lower()
        assert "fake yt-dlp manual failure" in res.stderr
        assert "fake yt-dlp auto failure" in res.stderr
        # No mlx_whisper on this host, and it is Apple-Silicon-only.
        assert res.returncode == 1, (res.returncode, res.stderr)
        assert "Apple Silicon" in res.stderr


class TestEmptyCaptionTrack:
    def test_track_with_no_cues_is_not_success(self, tmp_path):
        """A VTT of headers and timestamps passes the -s guard, the filter
        emits an empty join, and `wc -l` on the resulting single newline said
        1: "Done. Saved 1 lines" over a 1-byte file."""
        env = _env(tmp_path, YTF_MANUAL="ok", VTT_BODY=_VTT_NO_CUES)
        res, run_dir = _run(tmp_path, [], env)
        assert res.returncode == 5, (res.returncode, res.stdout, res.stderr)
        assert "Saved 1 lines" not in res.stdout
        assert "no caption text" in res.stderr
        assert not (run_dir / "Test Video-transcript.txt").exists(), (
            "a 1-byte file was left behind and presented as a transcript"
        )

    def test_track_with_no_cues_falls_through_to_audio_when_asked(self, tmp_path):
        env = _env(tmp_path, ffmpeg=True, YTF_MANUAL="ok", VTT_BODY=_VTT_NO_CUES)
        res, run_dir = _run(tmp_path, ["--audio-fallback"], env)
        assert res.returncode == 1, (res.returncode, res.stderr)
        assert "Apple Silicon" in res.stderr
        assert not (run_dir / "Test Video-transcript.txt").exists()

    def test_a_track_with_cues_still_succeeds(self, tmp_path):
        """The guard must not reject a real transcript."""
        env = _env(tmp_path, YTF_MANUAL="ok")
        res, run_dir = _run(tmp_path, [], env)
        assert res.returncode == 0, (res.returncode, res.stderr)
        out = run_dir / "Test Video-transcript.txt"
        assert out.exists()
        assert "Hello world" in out.read_text()
