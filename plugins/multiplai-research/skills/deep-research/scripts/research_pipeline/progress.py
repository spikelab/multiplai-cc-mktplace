"""Progress file writer — per-stage timestamped entries for external monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class ProgressWriter:
    """Append timestamped entries to a progress file.

    The file is human-readable and structured so external tools can `tail -f`
    it or grep for stage completions. Per-source progress goes here too.
    """

    def __init__(self, path: Path):
        self.path = path
        self._started: datetime | None = None

    def initialize(self, query: str, preset: str, fetch_budget: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._started = datetime.now(timezone.utc)
        header = (
            f"# Research Progress\n"
            f"Query: {query}\n"
            f"Preset: {preset}\n"
            f"Fetch budget: {fetch_budget}\n"
            f"\n"
            f"## [{self._now()}] LAUNCHED\n"
            f"Pipeline dispatched.\n"
        )
        self.path.write_text(header)

    def log_stage(self, stage: str, details: str = "") -> None:
        with self.path.open("a") as f:
            f.write(f"\n## [{self._now()}] {stage}\n")
            if details:
                f.write(f"{details}\n")

    def log_source(self, url: str, status: str, finding_count: int = 0, error: str | None = None) -> None:
        with self.path.open("a") as f:
            f.write(f"- [{self._now()}] {status} {url}")
            if finding_count:
                f.write(f" ({finding_count} findings)")
            if error:
                f.write(f" error={error}")
            f.write("\n")

    def cleanup(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
