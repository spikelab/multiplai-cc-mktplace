"""Progress file writer for long-running builds.

Writes a tail-able markdown file that external monitors can read.
Ported from deep-research pipeline with build-specific additions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

# A session-derived source is shown as "session": which of the session's own
# records supplied it (a parent's pin, the transcript, CLAUDE_EFFORT) does not
# change what the user does next.
_SESSION_SOURCES = {
    "MULTIPLAI_SESSION_MODEL",
    "session transcript",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_EFFORT",
}


def default_settings_line() -> str:
    """``Model: <m> (<source>) | Effort: <e> (<source>)`` for steps with none configured.

    What multiplai-core's ``default_model()`` / ``default_effort()`` give: the
    ``MULTIPLAI_MODEL``/``MULTIPLAI_EFFORT`` setting, else the Claude Code
    session's own. A value found nowhere reads ``CLI default``.
    """
    from multiplai_core import default_effort_source, default_model_source

    def part(label: str, value: str | None, source: str) -> str:
        if not value:
            return f"{label}: CLI default"
        shown = "session" if source in _SESSION_SOURCES else source
        return f"{label}: {value} ({shown})"

    return " | ".join(
        (
            part("Model", *default_model_source()),
            part("Effort", *default_effort_source()),
        )
    )


class ProgressWriter:
    """Append timestamped entries to a build progress file."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(
        self,
        change_name: str,
        tier: str,
        block_count: int | None = None,
    ) -> None:
        """Start this run's section of the progress file. Never truncates.

        A missing file gets the header and ``LAUNCHED``. An existing one is a
        resumed (or re-run) build: it gets a ``RESUMED`` section and keeps every
        earlier line. Both record the model and effort a step with none
        configured runs on, and where each came from.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blocks = f"Blocks: {block_count}\n" if block_count is not None else ""
        settings = default_settings_line()
        if self.path.exists():
            with self.path.open("a") as f:
                f.write(
                    f"\n## [{self._now()}] RESUMED\nTier: {tier}\n{blocks}{settings}\n"
                )
            return
        self.path.write_text(
            f"# Build Progress: {change_name}\n"
            f"Tier: {tier}\n"
            f"{blocks}{settings}\n\n"
            f"## [{self._now()}] LAUNCHED\n"
        )

    def log_phase(self, phase: str, details: str = "") -> None:
        with self.path.open("a") as f:
            f.write(f"\n## [{self._now()}] {phase}\n")
            if details:
                f.write(f"{details}\n")

    def log_block(self, block_num: int, total: int, name: str, status: str) -> None:
        with self.path.open("a") as f:
            f.write(f"- [{self._now()}] Block {block_num}/{total}: {name} — {status}\n")

    def log_line(self, line: str) -> None:
        """Append one line as it is, with the time in front."""
        with self.path.open("a") as f:
            f.write(f"- [{self._now()}] {line}\n")

    def log_agent(self, agent_type: str, block_name: str, status: str) -> None:
        with self.path.open("a") as f:
            f.write(f"  - [{self._now()}] {agent_type} ({block_name}): {status}\n")

    def log_review(
        self, block_name: str, iteration: int, score: float, passed: bool
    ) -> None:
        verdict = "PASS" if passed else "FAIL"
        with self.path.open("a") as f:
            f.write(
                f"  - [{self._now()}] Review ({block_name}) "
                f"iter={iteration} score={score:.1f} {verdict}\n"
            )

    def log_evidence(self, kind: str, block_name: str, output: str) -> None:
        """Append a trimmed RED/GREEN test-run evidence blob for a block."""
        tail = output.strip().splitlines()[-15:]
        with self.path.open("a") as f:
            f.write(f"  - [{self._now()}] {kind} evidence ({block_name}):\n")
            f.write("    ```\n")
            for line in tail:
                f.write(f"    {line}\n")
            f.write("    ```\n")

    def log_diagnosis(self, block_name: str, text: str) -> None:
        """Append the circuit-breaker diagnosis when integration fixes exhaust."""
        with self.path.open("a") as f:
            f.write(f"\n## [{self._now()}] DIAGNOSIS ({block_name})\n")
            for line in text.strip().splitlines()[:40]:
                f.write(f"{line}\n")

    def log_spec_impact(
        self,
        block_name: str,
        role: str,
        spec_impact: str,
        surprises: str,
    ) -> None:
        """Append an agent's spec-impact note. A `contradicts` note is written
        as a WARNING heading so it is visible in the tail of the progress file
        the moment it happens, not only in the end-of-build respec proposal."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        label = "WARNING" if spec_impact == "contradicts" else "NOTE"
        with self.path.open("a") as f:
            f.write(
                f"\n## [{self._now()}] {label} — SPEC_IMPACT: {spec_impact} "
                f"({block_name} / {role})\n"
            )
            for line in (surprises or "").strip().splitlines()[:20]:
                f.write(f"{line}\n")

    def log_board(
        self, column: str, owner_agent: str | None = None, note: str = ""
    ) -> None:
        """Append a board-column transition (the `.board.json` seam's human
        channel). Only called when the card actually moves columns."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        owner = f" (owner: {owner_agent})" if owner_agent else ""
        with self.path.open("a") as f:
            f.write(f"\n## [{self._now()}] BOARD → {column}{owner}\n")
            if note:
                f.write(f"{note}\n")

    def _now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
