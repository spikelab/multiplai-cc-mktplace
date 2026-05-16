"""Per-project status synthesis for multiplai plugin.

Scans recent diary entries from ``paths.diary_dir()``, groups them by
project derived from the working-directory path, and writes a short
status summary per project to ``paths.now_dir() / {project}.md``.

Runs as the manual dream trigger (``/multiplai:now``) or can be invoked
directly. Uses the path resolver for file locations and the model
client abstraction for LLM summarization; falls back to an extractive
summary when the LLM is unavailable.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.log_utils import setup_logging

logger = setup_logging("synthesize_now")

DEFAULT_LOOKBACK_HOURS = 48
MAX_SUMMARY_LINES = 5


def _parse_diary_entry(filepath: Path) -> dict | None:
    """Parse a diary entry file, returning working_dir, content, and timestamp.

    The first line is expected to be ``[TIMESTAMP] [SESSION_ID] [/path/to/cwd]``.
    Returns ``None`` if the file is empty or the header is missing.
    """
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if not text.strip():
        return None

    first_line = text.split("\n", 1)[0]
    brackets = re.findall(r"\[([^\]]+)\]", first_line)
    if len(brackets) < 3:
        return None

    timestamp_str, _session_id, working_dir = brackets[0], brackets[1], brackets[2]

    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            timestamp = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
        except OSError:
            timestamp = datetime.now(timezone.utc)

    return {
        "working_dir": working_dir,
        "content": text,
        "timestamp": timestamp,
        "filepath": filepath,
    }


def _derive_project_name(working_dir: str) -> str:
    """Project name is the final component of the working-directory path."""
    return Path(working_dir).name


def _scan_diary(
    diary_dir: Path,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, list[dict]]:
    """Scan diary entries, filter by lookback window, and group by project."""
    if not diary_dir.exists():
        logger.info("Diary directory does not exist: %s", diary_dir)
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    entries_by_project: dict[str, list[dict]] = {}

    for md_file in sorted(diary_dir.glob("*/*.md")):
        entry = _parse_diary_entry(md_file)
        if entry is None:
            continue

        entry_time = entry["timestamp"]
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        if entry_time < cutoff:
            continue

        project = _derive_project_name(entry["working_dir"])
        if not project:
            continue
        entries_by_project.setdefault(project, []).append(entry)

    return entries_by_project


def _extract_body_snippets(entries: list[dict], project: str) -> list[str]:
    """Strip metadata header lines from diary entries; return body snippets."""
    snippets: list[str] = []
    for entry in entries:
        body_lines = [
            line.strip()
            for line in entry["content"].strip().split("\n")
            if line.strip()
            and not line.startswith("[")
            and not line.startswith("Session started")
        ]
        if body_lines:
            snippets.append(" ".join(body_lines))
    return snippets or [f"Active project: {project}"]


def _extractive_summary(project: str, snippets: list[str]) -> str:
    """Fall back to a truncated-snippet summary when the LLM is unavailable."""
    lines = [
        f"- {s[:197] + '...' if len(s) > 200 else s}"
        for s in snippets[:MAX_SUMMARY_LINES]
    ]
    while len(lines) < 3:
        lines.append(f"- Active project: {project}")
    return "\n".join(lines[:MAX_SUMMARY_LINES])


async def _summarize_project(client, project: str, entries: list[dict]) -> str:
    """Use the model client to produce a 3–5 line project status summary."""
    snippets = _extract_body_snippets(entries, project)
    combined = "\n\n".join(snippets)

    system = (
        "You are a concise project status summarizer. "
        "Produce exactly 3-5 bullet lines summarizing the recent work status. "
        "Each line starts with '- '. No headers, no preamble."
    )
    prompt = (
        f"Summarize the recent work on project '{project}' in 3-5 bullet lines:\n\n"
        f"{combined}"
    )

    try:
        response = await client.query(
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = [
            line.strip() for line in response.content.split("\n") if line.strip()
        ][:MAX_SUMMARY_LINES]
        if lines:
            return "\n".join(lines)
    except Exception:
        logger.exception("LLM summary failed for project %s, using extractive", project)

    return _extractive_summary(project, snippets)


def _write_summary(
    now_dir: Path,
    project: str,
    summary: str,
    entries: list[dict],
    project_path: str,
) -> None:
    """Atomically write a project summary to ``now_dir / {project}.md``."""
    now_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header_lines = [f"Generated: {timestamp}"]
    source_names = [e["filepath"].name for e in entries if "filepath" in e]
    if source_names:
        header_lines.append("Source entries: " + ", ".join(source_names))
    if project_path:
        header_lines.append(f"Project path: {project_path}")

    content = (
        f"# Project Status: {project}\n\n"
        f"{chr(10).join(header_lines)}\n\n"
        f"---\n\n"
        f"{summary}\n"
    )

    target = now_dir / f"{project}.md"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(target))


async def synthesize() -> int:
    """Scan diary, group by project, write per-project status summaries."""
    paths = get_paths()
    diary_dir = paths.diary_dir()
    now_dir = paths.now_dir()

    entries_by_project = _scan_diary(diary_dir)
    if not entries_by_project:
        logger.info("No recent diary entries to synthesize")
        return 0

    try:
        client = await create_client()
        logger.info("Synthesize now using %s", type(client).__name__)
    except Exception:
        logger.exception("Model client unavailable, using extractive summaries")
        client = None

    for project, entries in entries_by_project.items():
        try:
            project_path = entries[0].get("working_dir", "")
            if client is not None:
                summary = await _summarize_project(client, project, entries)
            else:
                snippets = _extract_body_snippets(entries, project)
                summary = _extractive_summary(project, snippets)
            _write_summary(now_dir, project, summary, entries, project_path)
        except Exception:
            logger.exception("Failed to summarize project %s", project)
            continue

    return 0


def main() -> None:
    try:
        json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        pass
    sys.exit(asyncio.run(synthesize()))


if __name__ == "__main__":
    main()
