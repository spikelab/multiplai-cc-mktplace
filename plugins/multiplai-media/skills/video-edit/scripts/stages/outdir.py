"""Where a job's outputs go (docs/degradation-contract.md, output-location rule).

The workspace INBOX/ if there is one — the workspace root is read from
$CLAUDE_CONFIG_DIR/.workspace — else the current directory. Nothing here
creates INBOX/ on a machine that does not have it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def base_dir() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    marker = Path(cfg) / ".workspace"
    try:
        root = Path(marker.read_text().strip()).expanduser()
    except OSError:
        root = None
    if root and (root / "INBOX").is_dir():
        return root / "INBOX"
    return Path.cwd()


def job_dir(job: str) -> Path:
    """<INBOX or cwd>/video-edit/<job>/, with the job name kept path-safe."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", job).strip("-") or "job"
    return base_dir() / "video-edit" / safe


def clip_edl_path(out_dir: Path, n: int) -> Path:
    return out_dir / "edl" / f"clip-{n:02d}.edl.json"
