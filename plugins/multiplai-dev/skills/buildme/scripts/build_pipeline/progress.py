"""Progress file writer for long-running builds.

Writes a tail-able markdown file that external monitors can read.
Ported from deep-research pipeline with build-specific additions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class ProgressWriter:
    """Append timestamped entries to a build progress file."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self, change_name: str, mode: str, tier: str, block_count: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Build Progress: {change_name}\n"
            f"Mode: {mode} | Tier: {tier}\n"
            f"Blocks: {block_count}\n\n"
            f"## [{self._now()}] LAUNCHED\n"
        )
        self.path.write_text(header)

    def log_phase(self, phase: str, details: str = "") -> None:
        with self.path.open("a") as f:
            f.write(f"\n## [{self._now()}] {phase}\n")
            if details:
                f.write(f"{details}\n")

    def log_block(self, block_num: int, total: int, name: str, status: str) -> None:
        with self.path.open("a") as f:
            f.write(f"- [{self._now()}] Block {block_num}/{total}: {name} — {status}\n")

    def log_agent(self, agent_type: str, block_name: str, status: str) -> None:
        with self.path.open("a") as f:
            f.write(f"  - [{self._now()}] {agent_type} ({block_name}): {status}\n")

    def log_review(self, block_name: str, iteration: int, score: float, passed: bool) -> None:
        verdict = "PASS" if passed else "FAIL"
        with self.path.open("a") as f:
            f.write(
                f"  - [{self._now()}] Review ({block_name}) "
                f"iter={iteration} score={score:.1f} {verdict}\n"
            )

    def cleanup(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
