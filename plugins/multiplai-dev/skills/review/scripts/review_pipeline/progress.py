"""Tailable progress file: one timestamped line per event.

The session running the skill tails this with a bounded loop until it sees
`DONE` or `FAILED`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class ProgressWriter:
    def __init__(self, path: Path):
        self.path = path

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def line(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"[{self._now()}] {text}\n")

    def started(self, label: str, base: str, head: str, files: int) -> None:
        self.line(f"STARTED {label} {base[:12]}..{head[:12]} ({files} files)")

    def stage(self, stage: str, detail: str = "") -> None:
        self.line(f"STAGE {stage}" + (f": {detail}" if detail else ""))

    def done(self, detail: str) -> None:
        self.line(f"DONE {detail}")

    def failed(self, detail: str) -> None:
        self.line(f"FAILED {detail}")
