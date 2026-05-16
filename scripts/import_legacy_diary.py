"""Import legacy Captain's Log archive into canonical diary format.

Parses ``diary-archive-per-day/YYYY-MM-DD.md`` files (kit-era format) and
writes ``diary/YYYY-MM-DD/<sessionId>.md`` in the canonical plugin format
so ``synthesize_now`` and ``generate_catalog`` gain historical depth.

Usage::

    python scripts/import_legacy_diary.py [--dry-run] [--archive-dir PATH]

Options:
    --dry-run      Print what would be written; make no changes.
    --archive-dir  Path to archive directory (default: auto-resolved from paths).

Idempotent: skips sessions already imported (target file exists).
Non-destructive: archive-per-day/ files are never modified.
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("import_legacy_diary")

# Matches header lines: [TIMESTAMP] [SESSION_ID] [CWD]
_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]")

# Noise patterns to drop (these entries carry no narrative content)
_NOISE_PATTERNS = [
    re.compile(r"^Session started", re.IGNORECASE),
    re.compile(r"^Session ended", re.IGNORECASE),
    re.compile(r"integer expression expected"),
    re.compile(r"auto-commit failed"),
    re.compile(r"skip commit.*nothing staged", re.IGNORECASE),
    re.compile(r"^\s*$"),
]


def _is_noise(lines: list[str]) -> bool:
    """Return True if the entry body is pure noise (no real content)."""
    body = "\n".join(lines).strip()
    if not body:
        return True
    for pat in _NOISE_PATTERNS:
        if pat.search(body):
            return True
    return False


def _parse_archive_file(filepath: Path) -> list[dict]:
    """Parse a Captain's Log archive file into entry dicts.

    Each entry: {'timestamp': str, 'session_id': str, 'cwd': str, 'body': str}
    Returns only entries with real content (noise stripped).
    """
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("Could not read %s", filepath)
        return []

    entries = []
    current_meta = None
    current_body: list[str] = []

    def _flush():
        if current_meta is None:
            return
        ts, sid, cwd = current_meta
        # Drop pure auto-commit noise entries
        if cwd == "auto-commit":
            return
        body_lines = [l for l in current_body if l.strip()]
        if _is_noise(body_lines):
            return
        entries.append({
            "timestamp": ts,
            "session_id": sid,
            "cwd": cwd,
            "body": "\n".join(body_lines).strip(),
        })

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            current_meta = (m.group(1), m.group(2), m.group(3))
            current_body = []
        elif current_meta is not None:
            current_body.append(line)

    _flush()
    return entries


def _group_by_session(entries: list[dict]) -> dict[str, list[dict]]:
    """Group entries by session_id, preserving timestamp order."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        groups[e["session_id"]].append(e)
    return groups


def _diary_date(entries: list[dict]) -> str:
    """Determine the diary date from the first entry's timestamp."""
    ts = entries[0]["timestamp"]
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_diary_file(diary_dir: Path, date_str: str, session_id: str, entries: list[dict], *, dry_run: bool) -> bool:
    """Write a canonical diary file from session entries.

    Returns True if a new file was written, False if skipped.
    """
    day_dir = diary_dir / date_str
    target = day_dir / f"{session_id}.md"

    if target.exists():
        logger.debug("Skip (exists): %s", target)
        return False

    first = entries[0]
    header = f"[{first['timestamp']}] [{session_id}] [{first['cwd']}]"
    lines = [header, ""]
    for e in entries:
        lines.append(f"[{e['timestamp']}]")
        lines.append("")
        lines.append(e["body"])
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"

    if dry_run:
        print(f"  [dry-run] Would write: {target}")
        return True

    day_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("Wrote: %s", target)
    return True


def import_archive(
    archive_dir: Path,
    diary_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import all archive files. Returns stats dict."""
    stats = {"files": 0, "sessions": 0, "written": 0, "skipped": 0, "noise_only": 0}

    archive_files = sorted(archive_dir.glob("*.md"))
    if not archive_files:
        logger.info("No archive files found in %s", archive_dir)
        return stats

    for archive_file in archive_files:
        stats["files"] += 1
        entries = _parse_archive_file(archive_file)
        if not entries:
            continue

        groups = _group_by_session(entries)
        for session_id, session_entries in groups.items():
            stats["sessions"] += 1
            date_str = _diary_date(session_entries)
            wrote = _write_diary_file(diary_dir, date_str, session_id, session_entries, dry_run=dry_run)
            if wrote:
                stats["written"] += 1
            else:
                stats["skipped"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print planned writes; no changes")
    parser.add_argument("--archive-dir", type=Path, default=None, help="Archive directory path")
    args = parser.parse_args()

    paths = get_paths()
    diary_dir = paths.diary_dir()

    if args.archive_dir:
        archive_dir = args.archive_dir.expanduser().resolve()
    else:
        # Default: sibling of diary_dir
        archive_dir = diary_dir.parent / "diary-archive-per-day"

    if not archive_dir.exists():
        print(f"Archive directory not found: {archive_dir}")
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Archive: {archive_dir}")
        print(f"[dry-run] Target:  {diary_dir}")

    stats = import_archive(archive_dir, diary_dir, dry_run=args.dry_run)

    print(
        f"Import complete: {stats['files']} archive files, "
        f"{stats['sessions']} sessions parsed, "
        f"{stats['written']} written, "
        f"{stats['skipped']} skipped (already exist)"
    )


if __name__ == "__main__":
    main()
