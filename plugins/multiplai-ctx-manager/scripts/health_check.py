"""Health check script for multiplai plugin.

Audits memory files, diary entries, learnings, and plugin data directories.
Reports which ModelClient implementation is active (R1).
Validates all Paths fields resolve to existing directories.
Outputs a structured report to stdout for the health skill to present.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import detect_client_type


# The starter-template files /multiplai:setup creates. Their *absence*
# is what drives the setup recommendation — it does not bound the
# staleness audit, which scans the whole memory corpus.
REQUIRED_MEMORY_FILES = ["me.md", "technical-pref.md", "preferences.md"]
STALENESS_THRESHOLD_DAYS = 30

# How many stale filenames to enumerate in a recommendation before
# collapsing the rest to a "+N more" tail.
_MAX_LISTED_STALE = 10


def _check_directory_status(name: str, path: Path) -> dict:
    """Check whether a directory exists and return status info."""
    exists = path.is_dir()
    return {
        "name": name,
        "path": str(path),
        "exists": exists,
        "status": "found" if exists else "missing",
    }


def _check_memory_file(memory_dir: Path, filename: str) -> dict:
    """Check a single memory file for existence, size, and staleness."""
    filepath = memory_dir / filename
    if not filepath.exists():
        return {
            "name": filename,
            "exists": False,
            "status": "missing",
            "size": 0,
            "mtime": None,
            "stale": False,
        }

    stat = filepath.stat()
    st_size = stat.st_size
    st_mtime = stat.st_mtime
    modified_dt = datetime.fromtimestamp(st_mtime, tz=timezone.utc)
    age_days = (datetime.now(timezone.utc) - modified_dt).days
    stale = age_days > STALENESS_THRESHOLD_DAYS

    return {
        "name": filename,
        "exists": True,
        "status": "stale" if stale else "ok",
        "size": st_size,
        "mtime": modified_dt.isoformat(),
        "age_days": age_days,
        "stale": stale,
    }


def _count_diary_entries(diary_dir: Path) -> int:
    """Count diary entry files.

    Canonical layout is ``diary_dir/YYYY-MM-DD/<session>.md`` (written by
    extraction.write_diary_entries), so a top-level iterdir would always
    report 0. rglob also catches any legacy flat entries.
    """
    if not diary_dir.is_dir():
        return 0
    return len(list(diary_dir.rglob("*.md")))


def _count_learnings(learnings_dir: Path) -> int:
    """Count lines across all per-day learnings files in learnings_dir."""
    if not learnings_dir.is_dir():
        return 0
    total = 0
    for f in sorted(learnings_dir.glob("*.md")):
        content = f.read_text().strip()
        if content:
            total += len(content.splitlines())
    return total


def _get_last_dream_date(data_dir: Path) -> str | None:
    """Read last dream consolidation date from dream state file."""
    dream_state_file = data_dir / "dream_state.yaml"
    if not dream_state_file.exists():
        return None
    try:
        import yaml
        with open(dream_state_file) as f:
            state = yaml.safe_load(f) or {}
        return state.get("last_run")
    except Exception:
        return None


def run_health_check() -> dict:
    """Run a full health check and return structured results."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    diary_dir = paths.diary_dir()
    learnings_dir = paths.learnings_dir()
    data_dir = paths.plugin_data()
    venv_dir = paths.venv_dir()

    report = {}

    # R1: Report active ModelClient implementation
    report["model_client"] = detect_client_type()

    # Validate directories exist
    report["directories"] = [
        _check_directory_status("memory_dir", memory_dir),
        _check_directory_status("diary_dir", diary_dir),
        _check_directory_status("data_dir", data_dir),
        _check_directory_status("venv_dir", venv_dir),
    ]

    # Check if this is a fresh install (no memory dir)
    if not memory_dir.is_dir():
        report["fresh_install"] = True
        report["memory_files"] = []
        report["recommendations"] = [
            "Memory directory not found. Run /multiplai:setup to configure the plugin."
        ]
        print(json.dumps(report, indent=2))
        return report

    report["fresh_install"] = False

    # Full-corpus inventory: every *.md in the memory dir gets size,
    # mtime, and staleness — not just the starter-template trio.
    corpus = sorted(p.name for p in memory_dir.glob("*.md"))
    report["memory_files"] = [
        _check_memory_file(memory_dir, name) for name in corpus
    ]
    # Required starter files that are absent (drives the setup hint).
    required_missing = [
        name for name in REQUIRED_MEMORY_FILES
        if not (memory_dir / name).exists()
    ]
    report["required_missing"] = required_missing

    stale = [f for f in report["memory_files"] if f.get("stale")]
    report["memory_summary"] = {
        "total": len(report["memory_files"]),
        "fresh": sum(
            1 for f in report["memory_files"]
            if f["exists"] and not f.get("stale")
        ),
        "stale": len(stale),
        "required_missing": len(required_missing),
    }

    # Diary and learnings status
    report["diary"] = {
        "entry_count": _count_diary_entries(diary_dir),
    }
    report["learnings"] = {
        "unprocessed_count": _count_learnings(learnings_dir),
    }

    # Last dream consolidation date
    last_dream = _get_last_dream_date(data_dir)
    report["dream_state"] = {
        "last_dream_date": last_dream if last_dream else "never",
    }

    # Build recommendations
    recommendations = []
    if required_missing:
        names = ", ".join(required_missing)
        recommendations.append(
            f"Missing required memory files: {names}. "
            f"Run /multiplai:setup to create them."
        )

    stale_files = [f for f in report["memory_files"] if f.get("stale")]
    if stale_files:
        ordered = sorted(stale_files, key=lambda f: f.get("age_days", 0), reverse=True)
        listed = [f["name"] for f in ordered[:_MAX_LISTED_STALE]]
        names = ", ".join(listed)
        if len(ordered) > _MAX_LISTED_STALE:
            names += f", +{len(ordered) - _MAX_LISTED_STALE} more"
        recommendations.append(
            f"{len(ordered)} stale memory file(s) (>{STALENESS_THRESHOLD_DAYS} "
            f"days), oldest first: {names}. Run /multiplai:dream to refresh them."
        )

    unprocessed = report["learnings"]["unprocessed_count"]
    if unprocessed > 0:
        recommendations.append(
            f"{unprocessed} unprocessed learning lines pending. Run /multiplai:dream then /multiplai:dream-remember."
        )

    report["recommendations"] = recommendations

    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    run_health_check()


if __name__ == "__main__":
    main()
