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


MEMORY_FILES = ["me.md", "technical-pref.md", "preferences.md"]
STALENESS_THRESHOLD_DAYS = 30


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

    # Memory file inventory with size, mtime, staleness
    report["memory_files"] = [
        _check_memory_file(memory_dir, f) for f in MEMORY_FILES
    ]

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
    missing_files = [f for f in report["memory_files"] if not f["exists"]]
    if missing_files:
        names = ", ".join(f["name"] for f in missing_files)
        recommendations.append(
            f"Missing memory files: {names}. Run /multiplai:setup to create them."
        )

    stale_files = [f for f in report["memory_files"] if f.get("stale")]
    if stale_files:
        names = ", ".join(f["name"] for f in stale_files)
        recommendations.append(
            f"Stale memory files (>{STALENESS_THRESHOLD_DAYS} days): {names}. "
            f"Run /multiplai:dream to refresh them."
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
