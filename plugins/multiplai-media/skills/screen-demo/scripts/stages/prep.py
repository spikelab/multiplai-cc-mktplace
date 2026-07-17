from __future__ import annotations
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent.parent

def _cache_root() -> Path:
    ws = os.environ.get("WORKSPACE")
    if ws and Path(ws).is_dir():
        return Path(ws) / ".screen-demo-cache"
    return Path.home() / ".cache" / "screen-demo"

CACHE_ROOT = _cache_root()
MLX_BIN = "mlx_whisper"

# Multilingual by default — NEVER an `.en` model (those are architecturally
# English-only and emit "(speaking in foreign language)" on anything else).
# Matches the `transcribe` skill's DEFAULT_MODEL_MULTI. Best quality upgrade:
# pass model="mlx-community/whisper-large-v3-mlx".
MLX_MODEL_MULTI = "mlx-community/whisper-medium-mlx"

# Transcription runs EXCLUSIVELY on the macOS host via mlx_whisper (Metal GPU).
# In the container there is no local backend — we bridge to the host over SSH.
# On a Mac we run mlx_whisper locally as a convenience. There is no in-container
# whisper backend — the local-build path was removed.
IS_MAC = platform.system() == "Darwin"

# SSH bridge (container → macOS host). The bridge user must come from the
# environment (SSH_BUILD_USER) or an explicit TRANSCRIBE_USER override.
SSH_KEY = Path(
    os.environ.get("TRANSCRIBE_KEY") or os.environ.get("SSH_BUILD_KEY") or "/home/agent/.ssh/build_key"
)
SSH_HOST = os.environ.get("TRANSCRIBE_HOST", "host.docker.internal")
SSH_USER = os.environ.get("TRANSCRIBE_USER") or os.environ.get("SSH_BUILD_USER", "")


@dataclass
class CutCandidate:
    t: float
    kind: str   # "silence_end" | "scene_change" | "freeze_end"
    note: str = ""


@dataclass
class DeadSpan:
    start: float
    end: float
    kind: str   # "black" | "static" | "low"

    @property
    def duration(self) -> float:
        return self.end - self.start


# Per-second motion classification (max frame-diff luma, 160px downscale).
# Calibrated on real screen recordings: a frozen page measures ~0.00, a span
# where only the cursor blinks / characters land measures <1.0, and any real
# page activity (scroll, dropdown, navigation) spikes to 5-60.
STATIC_MOTION_TH = 0.02
LOW_MOTION_TH = 1.0
MIN_DEAD_SPAN_S = 3.0
# An agent typing into a form produces the signature "3s frozen, one keystroke
# blip, 3s frozen" — merge dead runs separated by activity gaps this short so
# the whole stretch reads as ONE dead span instead of a fragmented list.
# But only when the gap is widget-level motion (keystroke, dropdown, calendar
# paint — measures < 30): page-level events (navigation, results rendering,
# big scrolls — measure 30-130) are the money shots, and must survive as span
# boundaries rather than be swallowed into a dead span.
GAP_MERGE_S = 2.0
GAP_BURST_TH = 30.0


@dataclass
class PrepResult:
    source: str
    src_duration: float
    proxy_path: str
    audio_path: str
    transcript_srt_path: str
    cuts_path: str
    context_path: str


def _source_key(src: Path) -> str:
    st = src.stat()
    h = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
    return f"{src.stem}-{h}"


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], text=True
    ).strip()
    return float(out)


def _make_proxy(source: Path, dst: Path) -> None:
    if dst.exists():
        return
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        str(dst),
    ], check=True)


def _extract_audio(proxy: Path, dst: Path) -> None:
    if dst.exists():
        return
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(proxy),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dst),
    ], check=True)


def _silencedetect(audio: Path) -> list[CutCandidate]:
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(audio),
        "-vn", "-af", "silencedetect=noise=-30dB:d=1.0",
        "-f", "null", "-",
    ], capture_output=True, text=True)
    cuts = []
    for m in re.finditer(r"silence_end:\s+([\d.]+)", p.stderr):
        cuts.append(CutCandidate(t=float(m.group(1)), kind="silence_end"))
    return cuts


def _scenedetect_bin() -> str:
    """Locate the scenedetect CLI. Prefer one on PATH (baked into the image);
    otherwise fall back to the skill's bootstrap `.venv` so `python3 pipeline.py`
    works without the caller having activated the venv."""
    on_path = shutil.which("scenedetect")
    if on_path:
        return on_path
    venv_bin = SKILL_ROOT / ".venv" / "bin" / "scenedetect"
    if venv_bin.exists():
        return str(venv_bin)
    raise RuntimeError(
        "scenedetect not found. Run bootstrap.sh (installs it into the skill "
        "'.venv'), or bake scenedetect + opencv-python-headless into the image."
    )


def _scenedetect(proxy: Path, work: Path) -> list[CutCandidate]:
    csv = work / "scenes.csv"
    if not csv.exists():
        subprocess.run([
            _scenedetect_bin(), "-i", str(proxy), "-q",
            "detect-content", "-t", "12", "-m", "60",
            "list-scenes", "-f", str(csv), "-s",
        ], check=True)
    cuts = []
    text = csv.read_text().splitlines()
    for line in text[2:]:
        parts = line.split(",")
        if len(parts) > 3:
            try:
                cuts.append(CutCandidate(t=float(parts[3]), kind="scene_change"))
            except ValueError:
                continue
    return cuts


def _blackdetect(proxy: Path) -> list[DeadSpan]:
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(proxy),
        "-vf", "blackdetect=d=1.0:pix_th=0.05",
        "-an", "-f", "null", "-",
    ], capture_output=True, text=True)
    spans = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", p.stderr):
        spans.append(DeadSpan(start=float(m.group(1)), end=float(m.group(2)), kind="black"))
    return spans


def _activity_profile(proxy: Path, work: Path) -> list[float]:
    """Per-second motion profile: max frame-diff luma (YAVG of tblend difference,
    160px downscale) within each second. ~0 on a frozen screen, <1 when only the
    cursor blinks or a character lands, 5-60 on real page activity."""
    cache = work / "activity.json"
    if cache.exists():
        return json.loads(cache.read_text())["profile"]
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(proxy),
        "-vf", ("scale=160:-2,tblend=all_mode=difference,signalstats,"
                "metadata=print:key=lavfi.signalstats.YAVG:file=-"),
        "-an", "-f", "null", "-",
    ], capture_output=True, text=True, check=True)
    profile: list[float] = []
    for m in re.finditer(
        r"pts_time:([\d.]+)\s*\nlavfi\.signalstats\.YAVG=([\d.eE+-]+)", p.stdout
    ):
        t, y = float(m.group(1)), float(m.group(2))
        while len(profile) <= int(t):
            profile.append(0.0)
        profile[int(t)] = max(profile[int(t)], y)
    cache.write_text(json.dumps({"profile": profile}))
    return profile


def _dead_spans(profile: list[float], black: list[DeadSpan],
                src_duration: float) -> list[DeadSpan]:
    """Classify per-second motion into static/low dead spans, skipping seconds
    already covered by a black span. Dead runs separated by <= GAP_MERGE_S of
    activity (a keystroke, a click) merge into one `low` span; only merged
    spans >= MIN_DEAD_SPAN_S are reported. If the video stream ends before the
    container does (screen recordings do this), the tail is static."""
    def in_black(s: int) -> bool:
        return any(b.start <= s + 0.5 <= b.end for b in black)

    # 1. Collect every dead run, however short.
    runs: list[DeadSpan] = []
    run_start: int | None = None
    run_kind: str | None = None

    def flush(end_s: int) -> None:
        nonlocal run_start, run_kind
        if run_start is not None:
            runs.append(DeadSpan(start=float(run_start), end=float(end_s), kind=run_kind or "low"))
        run_start, run_kind = None, None

    for s, score in enumerate(profile):
        kind = None
        if not in_black(s):
            if score < STATIC_MOTION_TH:
                kind = "static"
            elif score < LOW_MOTION_TH:
                kind = "low"
        if kind != run_kind:
            flush(s)
            if kind is not None:
                run_start, run_kind = s, kind
    flush(len(profile))

    # 2. Merge runs separated by blip-length activity. A swallowed gap or a
    # kind change means there was *some* motion inside — the merged span is
    # typing-level ("low"), not frozen.
    merged: list[DeadSpan] = []
    for run in runs:
        gap = range(int(merged[-1].end), int(run.start)) if merged else range(0)
        gap_mergeable = (
            merged
            and run.start - merged[-1].end <= GAP_MERGE_S
            and not any(in_black(s) for s in gap)
            and all(profile[s] < GAP_BURST_TH for s in gap)
        )
        if gap_mergeable:
            prev = merged[-1]
            kind = run.kind if (run.kind == prev.kind and run.start == prev.end) else "low"
            merged[-1] = DeadSpan(start=prev.start, end=run.end, kind=kind)
        else:
            merged.append(run)

    spans = [s for s in merged if s.duration >= MIN_DEAD_SPAN_S]

    # Tail beyond the last video frame: no frames = nothing on screen.
    if src_duration - len(profile) >= MIN_DEAD_SPAN_S:
        spans.append(DeadSpan(start=float(len(profile)), end=src_duration, kind="static"))

    return sorted(spans + black, key=lambda x: x.start)


def _select_model(model: str | None) -> str:
    """Never default to an `.en` model — always multilingual so any language
    (Italian, etc.) transcribes correctly. An explicit `model` overrides."""
    return model or MLX_MODEL_MULTI


def _mlx_args(audio: Path, dst_stem: Path, prompt_hint: str,
              language: str | None, model: str | None) -> list[str]:
    """Build the mlx_whisper argv. SRT output is required — the EDL authoring
    step depends on real timestamps."""
    args = [
        MLX_BIN,
        "--model", _select_model(model),
        "--output-format", "srt",
        "--output-dir", str(dst_stem.parent),
        "--output-name", dst_stem.name,
        "--verbose", "False",
    ]
    if language:
        args.extend(["--language", language])
    if prompt_hint:
        args.extend(["--initial-prompt", prompt_hint])
    args.append(str(audio))
    return args


def _transcribe(audio: Path, dst_stem: Path, prompt_hint: str = "",
                language: str | None = None, model: str | None = None) -> Path:
    """Transcribe to SRT on the macOS host via mlx_whisper (Metal GPU).

    Transcription runs EXCLUSIVELY on the Mac host — either locally (when this
    runs on a Mac with mlx_whisper on PATH) or over the SSH bridge from the
    container. There is no in-container whisper backend and no silent fallback:
    if the bridge is unreachable or the host lacks mlx_whisper, this fails loudly.
    """
    srt = Path(str(dst_stem) + ".srt")
    if srt.exists():
        return srt

    # Mac-native convenience path: run mlx_whisper directly on Apple Silicon.
    if IS_MAC and shutil.which(MLX_BIN):
        return _transcribe_mlx_local(audio, dst_stem, srt, prompt_hint, language, model)

    # Container path: bridge to the macOS host. This is the ONLY backend here.
    return _transcribe_ssh(audio, dst_stem, srt, prompt_hint, language, model)


def _transcribe_mlx_local(audio: Path, dst_stem: Path, srt: Path, prompt_hint: str,
                          language: str | None, model: str | None) -> Path:
    cmd = _mlx_args(audio, dst_stem, prompt_hint, language, model)
    subprocess.run(cmd, check=True)
    if not srt.exists():
        raise RuntimeError(f"mlx_whisper completed but {srt} not found")
    return srt


def _bridge_error(detail: str) -> RuntimeError:
    return RuntimeError(
        "Host transcription bridge failed: " + detail + "\n"
        "  Transcription runs only on the macOS host via mlx_whisper (Metal GPU) —\n"
        "  there is no in-container whisper backend. Fix by ensuring:\n"
        "    • mlx_whisper is on PATH on the host  (pip install mlx-whisper)\n"
        f"    • an SSH key exists at {SSH_KEY}  (TRANSCRIBE_KEY/SSH_BUILD_KEY)\n"
        "    • the bridge user is set  (SSH_BUILD_USER or TRANSCRIBE_USER)\n"
        f"    • host {SSH_HOST} is reachable and its gateway allowlists 'mlx_whisper'\n"
        "  Verify manually:\n"
        f"    ssh -i {SSH_KEY} {SSH_USER or '<user>'}@{SSH_HOST} 'command -v mlx_whisper'"
    )


def _transcribe_ssh(audio: Path, dst_stem: Path, srt: Path, prompt_hint: str,
                    language: str | None, model: str | None) -> Path:
    if not SSH_USER:
        raise _bridge_error("no bridge user configured (SSH_BUILD_USER / TRANSCRIBE_USER is empty).")
    if not SSH_KEY.exists():
        raise _bridge_error(f"SSH key not found at {SSH_KEY}.")

    parts = _mlx_args(audio, dst_stem, prompt_hint, language, model)
    # shlex.quote every arg so an attacker-controlled path/filename cannot inject
    # shell on the remote host (CWE-78).
    remote_cmd = " ".join(shlex.quote(p) for p in parts)
    ssh_cmd = [
        "ssh", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
        "-i", str(SSH_KEY), f"{SSH_USER}@{SSH_HOST}", remote_cmd,
    ]
    proc = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise _bridge_error(
            f"ssh to {SSH_USER}@{SSH_HOST} exited {proc.returncode}.\n"
            f"  stderr: {proc.stderr.strip() or '(empty)'}"
        )
    if not srt.exists():
        raise _bridge_error(
            f"mlx_whisper ran on the host but {srt} was not produced.\n"
            f"  stdout: {proc.stdout.strip() or '(empty)'}\n"
            f"  stderr: {proc.stderr.strip() or '(empty)'}"
        )
    return srt


def _parse_srt(srt: Path) -> list[dict]:
    out = []
    txt = srt.read_text()
    for m in re.finditer(r"(\d+)\n([\d:,]+) --> ([\d:,]+)\n((?:.*\n)+?)\n", txt + "\n\n"):
        def t2s(t: str) -> float:
            h, mi, s = t.split(":")
            s, ms = s.split(",")
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        out.append({
            "idx": int(m.group(1)),
            "start": t2s(m.group(2)),
            "end": t2s(m.group(3)),
            "text": m.group(4).strip().replace("\n", " "),
        })
    return out


def _write_context(result: PrepResult, segments: list[dict], cuts: list[CutCandidate],
                   dead: list[DeadSpan], dst: Path) -> None:
    lines = [
        "# screen-demo prep context",
        "",
        f"- source: {result.source}",
        f"- duration: {result.src_duration:.1f}s",
        f"- proxy: {result.proxy_path}",
        f"- transcript (SRT): {result.transcript_srt_path}",
        f"- cuts (JSON): {result.cuts_path}",
        "",
        "⚠ When authoring the EDL, `source` must be the ORIGINAL recording above —",
        "never the proxy. The proxy is a 720p analysis artifact; rendering from it",
        "produces a blurry, grainy result (render refuses it).",
        "",
    ]
    if dead:
        total_dead = sum(s.duration for s in dead)
        lines.extend([
            "## Dead spans (nothing happens on screen — cut these, don't speed through them)",
            "",
            f"{total_dead:.0f}s of {result.src_duration:.0f}s "
            f"({100*total_dead/result.src_duration:.0f}%) is dead air:",
            "",
            "| start | end | dur (s) | kind |",
            "|---|---|---|---|",
        ])
        for s in dead:
            lines.append(f"| {s.start:.1f} | {s.end:.1f} | {s.duration:.1f} | {s.kind} |")
        lines.extend([
            "",
            "- `black` — black screen (recording gap). NEVER include, at any speed.",
            "- `static` — frozen frame, zero motion (page sitting there, or past the last video frame).",
            "- `low` — only cursor-blink / typing-level motion (e.g. an agent typing into a field,",
            "  waiting on an autocomplete). No page activity worth watching.",
            "",
            "⚠ Do NOT paper over dead spans with a flat 4-8x speed — at 6x, a 30s dead span",
            "is still 5s of a video where nothing moves. Author segments so dead spans fall",
            "in the gaps BETWEEN segments (hard cut), or cross them at speed >= 20 when you",
            "need visual continuity (e.g. to show text appearing in a field). Per-second",
            "motion scores are in activity.json next to cuts.json if you need finer grain.",
            "",
            "Nuance: dead means don't LINGER, not don't SHOW. A motionless-but-readable",
            "frame (a results list, a final state) can still carry a short speed-1 zoom",
            "money shot — the viewer is reading, not watching. Budget it like a money",
            "shot (a few seconds), not like footage.",
            "",
        ])
    lines.extend([
        "## Cut candidates (use as anchors when authoring the EDL)",
        "",
        "| t (s) | kind | note |",
        "|---|---|---|",
    ])
    for c in sorted(cuts, key=lambda x: x.t):
        lines.append(f"| {c.t:.2f} | {c.kind} | {c.note} |")
    lines.extend([
        "",
        "## Transcript (timecoded)",
        "",
    ])
    for s in segments:
        lines.append(f"- [{s['start']:.1f}–{s['end']:.1f}] {s['text']}")
    dst.write_text("\n".join(lines))


def prep(source: str | Path, prompt_hint: str = "",
         language: str | None = None, model: str | None = None) -> PrepResult:
    src = Path(source).resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    key = _source_key(src)
    cache = CACHE_ROOT / key
    cache.mkdir(parents=True, exist_ok=True)

    print(f"→ prep: cache dir = {cache}")
    duration = _ffprobe_duration(src)
    proxy = cache / "proxy_720p.mp4"
    audio = cache / "audio16k.wav"
    transcript_stem = cache / "transcript"
    transcript = Path(str(transcript_stem) + ".srt")
    cuts_path = cache / "cuts.json"
    context_path = cache / "context.md"

    print(f"→ prep: {'reusing' if proxy.exists() else 'building'} proxy")
    _make_proxy(src, proxy)
    print(f"→ prep: {'reusing' if audio.exists() else 'extracting'} audio")
    _extract_audio(proxy, audio)
    _model = _select_model(model)
    _lang = language or "auto-detect"
    where = "locally (Mac)" if (IS_MAC and shutil.which(MLX_BIN)) else f"on host via SSH bridge ({SSH_HOST})"
    print(f"→ prep: {'reusing' if transcript.exists() else 'transcribing'} {where} — model={_model}, language={_lang}")
    _transcribe(audio, transcript_stem, prompt_hint=prompt_hint, language=language, model=model)

    print("→ prep: detecting cuts (silencedetect + scenedetect)")
    silence_cuts = _silencedetect(audio)
    scene_cuts = _scenedetect(proxy, cache)
    all_cuts = silence_cuts + scene_cuts

    cuts_path.write_text(json.dumps([asdict(c) for c in all_cuts], indent=2))

    print("→ prep: profiling activity (blackdetect + per-second motion)")
    black_spans = _blackdetect(proxy)
    profile = _activity_profile(proxy, cache)
    dead = _dead_spans(profile, black_spans, duration)
    dead_total = sum(s.duration for s in dead)

    segments = _parse_srt(transcript)
    result = PrepResult(
        source=str(src),
        src_duration=duration,
        proxy_path=str(proxy),
        audio_path=str(audio),
        transcript_srt_path=str(transcript),
        cuts_path=str(cuts_path),
        context_path=str(context_path),
    )
    _write_context(result, segments, all_cuts, dead, context_path)
    print(f"→ prep: context written to {context_path}")
    print(f"   {len(segments)} transcript segments, {len(all_cuts)} cut candidates")
    print(f"   {len(dead)} dead spans ({dead_total:.0f}s = "
          f"{100*dead_total/duration:.0f}% of the recording)")
    return result
