"""The run's saved state: `<run>/state.json`.

Saved with `multiplai_core.checkpoint.save_model`, which writes
`<run>/.state-<random>.tmp` and `os.replace`s it over `state.json`, so a kill
mid-save leaves the previous state whole (row R25). This module never reads,
renames or deletes a `.state-*.tmp`; the run's own temp files are named
`<file>.tmp`.

`steps_done` lists the steps whose outputs are whole, in order: `resolve` (this
version), then `scope`, `explore`, `group`, `write`, `render`, `apply` as later
versions add them. Each later version adds its own fields with defaults, so a
state an earlier version wrote still loads.
"""

from __future__ import annotations

from pathlib import Path

from multiplai_core import checkpoint
from pydantic import BaseModel, ConfigDict

from .target import STATE_FILE, Target


class Unreadable(Exception):
    """`state.json` exists but cannot be read as a state (row R23)."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} cannot be read as a spec-extract state; nothing was written. "
                         "Run the same command with --restart to move this run folder aside "
                         "(it is renamed, never deleted) and start again")


class State(BaseModel):
    # A field this version does not know is refused, not dropped: re-saving would
    # otherwise lose what a newer version saved. The run then exits 1 (R23).
    model_config = ConfigDict(extra="forbid")

    target: Target
    steps_done: list[str] = []


def load(run_dir: Path) -> State | None:
    """The saved state, None when there is no `state.json`; Unreadable when it cannot be read."""
    path = run_dir / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return None
    state = checkpoint.load_model(path, State)
    if state is None:
        raise Unreadable(path)
    return state


def save(state: State, run_dir: Path) -> None:
    checkpoint.save_model(state, run_dir, STATE_FILE)
