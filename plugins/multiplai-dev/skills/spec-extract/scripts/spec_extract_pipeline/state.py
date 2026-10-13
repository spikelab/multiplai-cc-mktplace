"""The run's saved state: `<run>/state.json`.

Saved with `multiplai_core.checkpoint.save_model`, which writes
`<run>/.state-<random>.tmp` and `os.replace`s it over `state.json`, so a kill
mid-save leaves the previous state whole (row R25). This module never reads,
renames or deletes a `.state-*.tmp`; the run's own temp files are named
`<file>.tmp`.

`steps_done` lists the steps whose outputs are whole, in order: `resolve`,
`scope`, `explore` (this version), then `group`, `write`, `render`, `apply` as
later versions add them. Each later version adds its own fields with defaults, so a
state an earlier version wrote still loads.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from multiplai_core import checkpoint
from pydantic import BaseModel, ConfigDict

from .scope import TargetScope
from .target import STATE_FILE, Target


class Unreadable(Exception):
    """`state.json` exists but cannot be read as a state (row R23)."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} cannot be read as a spec-extract state; nothing was written. "
                         "Run the same command with --restart to move this run folder aside "
                         "(it is renamed, never deleted) and start again. If the folder holds "
                         "another target's run, do not use --restart: pass --out <another root>")


class State(BaseModel):
    # A field this version does not know is refused, not dropped: re-saving would
    # otherwise lose what a newer version saved. The run then exits 1 (R23).
    model_config = ConfigDict(extra="forbid")

    target: Target
    steps_done: list[str] = []
    # The scope step's result, saved once and never recomputed (R26).
    scope: TargetScope | None = None
    # {unit key: an explore answer, or {"error": ...}}, filled in return order.
    # No reader uses this order: every reader orders it by `scope.units`.
    explore_answers: dict[str, dict] = {}
    # {unit key: calls started}. Counted and saved before each call (item 7).
    explore_attempts: dict[str, int] = {}
    # `ledger.to_state()`, saved with every answer: the spend of every call kept.
    budget: dict | None = None
    # facts.json's `generated_at`: the clock when explore first finished, saved before
    # facts.json is first written, so every later rewrite is the same bytes (row R28).
    facts_generated_at: datetime | None = None


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
