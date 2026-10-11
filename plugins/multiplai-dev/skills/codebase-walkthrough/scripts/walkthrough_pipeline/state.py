"""`walkthrough-state.json`: written after every stage, read by `resume`.

The atomic write and the tolerant read are `multiplai_core.checkpoint`; this
module supplies only the model class and the file name.
"""

from __future__ import annotations

from pathlib import Path

from multiplai_core.checkpoint import load_model, save_model

from .models import WalkState

STATE_FILE = "walkthrough-state.json"


def save_state(state: WalkState, target_dir: Path) -> Path:
    """Atomic replace, so a kill mid-write leaves the previous checkpoint."""
    return save_model(state, target_dir, STATE_FILE)


def load_state(path: Path) -> WalkState | None:
    """The saved state, or None (logged) when the file is missing or unreadable."""
    return load_model(path, WalkState)
