"""`review-state.json`: written after every stage, read by `resume`."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .models import ReviewState

log = logging.getLogger(__name__)

STATE_FILE = "review-state.json"


def save_state(state: ReviewState, target_dir: Path) -> Path:
    """Atomic replace, so a kill mid-write leaves the previous checkpoint."""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / STATE_FILE
    fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".review-state-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(state.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path


def load_state(path: Path) -> ReviewState | None:
    if not path.is_file():
        return None
    try:
        return ReviewState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as e:
        log.error("Unreadable review state %s: %s", path, e)
        return None
