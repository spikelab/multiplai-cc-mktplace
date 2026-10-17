"""The one module that reads `paths.v1`: the paths stage's result as the rest of the review sees it.

`code_facts.map_paths` writes nothing the review reads directly; the paths
stage writes its result to `paths.json` with `code_facts.write_paths_file`,
and every later reader (the path finders, path ids on findings, the report's
Paths section) goes through `load_paths`, so a schema change is one edit here.
This module also turns what the mapping could not do (dropped entries, cut
traces) into coverage records.
"""

from __future__ import annotations

import logging
from pathlib import Path

from code_facts.paths import PathDrop, PathsResult, TracedPath

from .models import CoverageRecord, ReviewState

log = logging.getLogger(__name__)

PATHS_FILE = "paths.json"

# Coverage level and reason of each drop stage. Scoping is normal and only `info`; a trace that
# failed is an `error`. The model fallback's own drops (a citation that did not check, a
# duplicate of a discovered entry) are scoping too.
DROP_CLASSES: dict[str, tuple[str, str]] = {
    "import_reachability": ("info", "out-of-scope"),
    "no_changed_line": ("info", "out-of-scope"),
    "trace_limit": ("info", "over-cap"),
    "cap": ("info", "over-cap"),
    "trace": ("error", "trace-failed"),
    "model_fallback_citation": ("info", "out-of-scope"),
    "model_fallback_duplicate": ("info", "out-of-scope"),
}


def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None:
    """The run's `paths.json`, or None unless the paths stage produced one (`ok` or `empty`).

    The status decides, not the file: a `paths.json` beside a state that says
    the stage failed or was skipped is not this run's. A file that does not
    parse is logged and read as no paths.
    """
    if state.paths_status not in {"ok", "empty"}:
        return None
    path = Path(target_dir) / PATHS_FILE
    try:
        return PathsResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return None


def entry_subject(drop: PathDrop) -> str:
    """How a dropped entry is named in coverage and `state.errors`: its name and declaration line."""
    loc = drop.entry.location
    return f"{drop.entry.name} at {loc.path}:{loc.line}"


def classify_drop(drop: PathDrop) -> CoverageRecord:
    level, reason = DROP_CLASSES.get(drop.stage, ("info", "out-of-scope"))
    return CoverageRecord(stage="paths", subject=entry_subject(drop), level=level, reason=reason,
                          detail=drop.detail or drop.reason)


def classify_broken(path: TracedPath) -> CoverageRecord:
    """A kept path whose trace was cut: still mapped, but only up to hop `stops_at`."""
    return CoverageRecord(stage="paths", subject=path.id, level="error", reason="broken-trace",
                          detail=f"stops at hop {path.stops_at}")
