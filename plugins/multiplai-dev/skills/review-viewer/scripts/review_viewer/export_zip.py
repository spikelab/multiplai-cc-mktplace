"""The **Export zip** button: one review folder and its sessions' transcripts, scanned by gitleaks.

`build()` copies what the zip holds into a staging directory, writes `MANIFEST.txt`, runs
`gitleaks dir` over all of it and zips it only when gitleaks finds nothing. A hit refuses the
export: there is no redaction and no override, because the transcripts, `diff.patch` and the
reports all quote code and command output that can hold a secret.

What the zip holds, by path inside it:
- every regular file at the top of the review folder;
- `rounds/**`, less a half-copied `rounds/.partial-*`;
- `viewer/decisions.json`, `viewer/inbox.jsonl` and `viewer/outbox.jsonl`;
- `transcripts/<session_id>.jsonl` and `transcripts/<session_id>/` (the session's `subagents/`
  and `tool-results/`), for every session in `review-state.json` `runs` and in the ledger's lines
  for this folder;
- `MANIFEST.txt`.

Never: `tree/` and every other directory not named above (`paths-cache/`, `base-tree/`,
`source/`), `viewer/server.token`, `viewer/open.html`, `viewer/server.json`,
`viewer/target.json`, `.DS_Store`, and a `.gitleaksignore` or `.gitleaks.toml`, which would
change what the scan reports.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

VIEWER_FILES = ("decisions.json", "inbox.jsonl", "outbox.jsonl")
# Files never copied, wherever they are: Finder's litter, and gitleaks' own allow-lists.
NEVER = frozenset({".DS_Store", ".gitleaksignore", ".gitleaks.toml"})
TRANSCRIPT_PARTS = ("subagents", "tool-results")
# A Claude Code session id; anything else is not looked up, so it cannot reach a glob or a path.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
GITLEAKS_TIMEOUT_S = 300
INSTALL_HINT = "install gitleaks (brew install gitleaks)"


class GitleaksMissing(RuntimeError):
    pass


class GitleaksFailed(RuntimeError):
    pass


@dataclass
class Leaks(Exception):
    """gitleaks found something: where, never what."""

    hits: list[dict] = field(default_factory=list)


def zip_name(slug: str, head_sha: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", slug).strip("._") or "review"
    return f"review-{safe}-{head_sha[:12]}.zip"


def default_ledger() -> Path:
    """The review skill's ledger, `<data_dir>/reviews/ledger.jsonl` (its `ledger.py`)."""
    from multiplai_core.paths import get_paths

    return Path(get_paths().data_dir) / "reviews" / "ledger.jsonl"


def config_dir() -> Path:
    """Where Claude Code keeps its transcripts: `$CLAUDE_CONFIG_DIR`, else `~/.claude`."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def session_ids(review_dir: Path, ledger: Path | None) -> list[str]:
    """Every session that ran this review, in first-seen order: the state's `runs`, then the ledger."""
    ids: list[str] = []
    try:
        state = json.loads(
            (review_dir / "review-state.json").read_text(encoding="utf-8")
        )
        ids += [r.get("session_id") or "" for r in state.get("runs") or []]
    except (OSError, ValueError, AttributeError):
        pass
    if ledger is not None:
        wanted = {str(review_dir), str(review_dir.resolve())}
        try:
            lines = ledger.read_text(encoding="utf-8").split("\n")
        except OSError:
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("target_dir") in wanted:
                ids.append(row.get("session_id") or "")
    return [s for s in dict.fromkeys(ids) if SESSION_ID_RE.match(s)]


def find_transcript(session_id: str, cfg: Path) -> Path | None:
    hits = sorted((cfg / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name not in NEVER:
            _copy(path, dst / path.relative_to(src))


def stage(
    review_dir: Path, staging: Path, *, ledger: Path | None, cfg: Path
) -> list[str]:
    """Copy the zip's files into *staging*; return the sessions whose transcript was not found."""
    for path in sorted(review_dir.iterdir()):
        if path.is_file() and not path.is_symlink() and path.name not in NEVER:
            _copy(path, staging / path.name)
    rounds = review_dir / "rounds"
    if rounds.is_dir() and not rounds.is_symlink():
        for child in sorted(rounds.iterdir()):
            if child.name.startswith(".partial-") or child.is_symlink():
                continue
            if child.is_dir():
                _copy_tree(child, staging / "rounds" / child.name)
            elif child.is_file() and child.name not in NEVER:
                _copy(child, staging / "rounds" / child.name)
    for name in VIEWER_FILES:
        path = review_dir / "viewer" / name
        if path.is_file() and not path.is_symlink():
            _copy(path, staging / "viewer" / name)
    missing = []
    for sid in session_ids(review_dir, ledger):
        found = find_transcript(sid, cfg)
        if found is None:
            missing.append(sid)
            continue
        _copy(found, staging / "transcripts" / f"{sid}.jsonl")
        for part in TRANSCRIPT_PARTS:
            sub = found.parent / sid / part
            if sub.is_dir() and not sub.is_symlink():
                _copy_tree(sub, staging / "transcripts" / sid / part)
    return missing


def gitleaks_version(exe: str) -> str:
    try:
        out = subprocess.run(
            [exe, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return out.stdout.strip() or "unknown"


def write_manifest(
    staging: Path, *, producer: str, gitleaks: str, missing: list[str]
) -> None:
    files = sorted(p for p in staging.rglob("*") if p.is_file())
    lines = [
        f"review: {producer or 'unknown'}",
        f"scanned by: gitleaks {gitleaks}",
        "",
        "files (bytes, path):",
        *(f"{p.stat().st_size}\t{p.relative_to(staging).as_posix()}" for p in files),
    ]
    if missing:
        lines += ["", "sessions whose transcript was not found:", *missing]
    (staging / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan(staging: Path, report: Path, exe: str) -> None:
    """Raise `Leaks` with each hit's file, line and rule; return when gitleaks finds nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
    try:
        done = subprocess.run(
            [
                exe, "dir", str(staging), "--no-banner", "--redact",
                "--report-format", "json", "--report-path", str(report),
            ],
            check=False, capture_output=True, text=True, timeout=GITLEAKS_TIMEOUT_S,
            stdin=subprocess.DEVNULL, env=env,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitleaksFailed(f"gitleaks did not finish: {type(exc).__name__}") from exc
    if done.returncode == 0:
        return
    if done.returncode != 1:
        raise GitleaksFailed(f"gitleaks exited {done.returncode}")
    try:
        rows = json.loads(report.read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        rows = []
    hits = []
    for row in rows if isinstance(rows, list) else []:
        file = str(row.get("File", ""))
        try:
            file = Path(file).resolve().relative_to(staging.resolve()).as_posix()
        except ValueError:
            pass
        hits.append(
            {"file": file, "line": row.get("StartLine"), "rule": row.get("RuleID")}
        )
    raise Leaks(hits or [{"file": "?", "line": None, "rule": "unknown"}])


def build(
    review_dir: Path,
    work: Path,
    *,
    producer: str,
    ledger: Path | None,
    cfg: Path | None = None,
) -> Path:
    """The zip, written under *work* (a temporary directory the caller deletes).

    Raises `GitleaksMissing` with no gitleaks on PATH, `Leaks` on a hit, `GitleaksFailed` when
    the scan itself fails. No zip exists in any of those cases.
    """
    exe = shutil.which("gitleaks")
    if exe is None:
        raise GitleaksMissing(INSTALL_HINT)
    staging = work / "staging"
    staging.mkdir()
    missing = stage(review_dir, staging, ledger=ledger, cfg=cfg or config_dir())
    write_manifest(
        staging, producer=producer, gitleaks=gitleaks_version(exe), missing=missing
    )
    scan(staging, work / "gitleaks.json", exe)
    out = work / "export.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(staging).as_posix())
    return out
