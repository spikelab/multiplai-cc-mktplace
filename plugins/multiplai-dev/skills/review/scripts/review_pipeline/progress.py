"""Tailable progress file: one timestamped line per event.

The session running the skill tails this with a bounded loop until it sees
`DONE` or `FAILED`. The line format and the `line`/`stage`/`done`/`failed`
writers are `multiplai_core.progress.ProgressWriter`; review adds only its
`STARTED` line.
"""

from __future__ import annotations

from multiplai_core.progress import ProgressWriter as _CoreProgressWriter


class ProgressWriter(_CoreProgressWriter):
    def started(self, label: str, base: str, head: str, files: int) -> None:
        self.line(f"STARTED {label} {base[:12]}..{head[:12]} ({files} files)")
