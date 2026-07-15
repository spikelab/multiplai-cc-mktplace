# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core[sdk] @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
# ///
"""Per-project status synthesis for multiplai plugin.

Scans recent diary entries from ``paths.diary_dir()``, groups them by project
via :func:`lib.project_identity.resolve_project`, and writes a short status
summary per project to ``paths.now_dir() / {project}.md``.

Invoked three ways: scoped per-project from the live extraction pipeline
(after a diary write), as a full rebuild from ``/multiplai-context:now`` and after a
backfill, or directly with ``--project NAME``. Uses the path resolver for file
locations and the model client abstraction for LLM summarization; falls back to
an extractive summary when the LLM is unavailable.
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.log_utils import setup_logging
from lib.project_identity import load_project_map, resolve_project

logger = setup_logging("synthesize_now")

DEFAULT_LOOKBACK_HOURS = 48
MAX_SUMMARY_LINES = 5


# Session block header in per-day diary files (v0.3.0+):
#     ## Session: <id> — <iso-ts> — <cwd>
# em-dash separators, written by lib.extraction.write_diary_entries.
# Separators use ``[ \t]`` (not ``\s``) so an EMPTY cwd doesn't let the
# engine swallow the trailing newline and capture the next line's text as
# the cwd. ``cwd`` is ``[^\n]*?`` (may be empty) for the same reason.
_SESSION_HEADER_RE = re.compile(
    r"^## Session:[ \t]*(?P<sid>\S+)[ \t]*—[ \t]*(?P<ts>\S+)[ \t]*—[ \t]*(?P<cwd>[^\n]*?)[ \t]*$",
    re.MULTILINE,
)


def _iter_diary_session_blocks(filepath: Path):
    """Yield one entry per ``## Session:`` block inside a per-day diary file.

    Each yielded dict has the same shape as the legacy per-session parser:
    ``{working_dir, content, timestamp, filepath}``. The ``content`` is the
    body of that session block only (header excluded).
    """
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    if not text.strip():
        return

    matches = list(_SESSION_HEADER_RE.finditer(text))
    if not matches:
        return

    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            continue

        ts_str = m.group("ts")
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                timestamp = datetime.fromtimestamp(
                    filepath.stat().st_mtime, tz=timezone.utc,
                )
            except OSError:
                timestamp = datetime.now(timezone.utc)

        yield {
            "working_dir": m.group("cwd").strip(),
            "content": body,
            "timestamp": timestamp,
            "filepath": filepath,
            "session_id": m.group("sid"),
        }


def _scan_diary(
    diary_dir: Path,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    config: dict | None = None,
) -> dict[str, list[dict]]:
    """Scan diary entries, filter by lookback window, and group by project.

    Per-day layout (v0.3.0+): each ``YYYY-MM-DD.md`` file holds one or more
    ``## Session:`` blocks. Each session block is treated as one entry. The
    project name comes from :func:`lib.project_identity.resolve_project` so it
    matches what the SessionStart hook injects (single source of truth).
    """
    if not diary_dir.exists():
        logger.info("Diary directory does not exist: %s", diary_dir)
        return {}

    if config is None:
        config = load_project_map()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    entries_by_project: dict[str, list[dict]] = {}

    for md_file in sorted(diary_dir.glob("*.md")):
        for entry in _iter_diary_session_blocks(md_file):
            entry_time = entry["timestamp"]
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            if entry_time < cutoff:
                continue

            project = resolve_project(entry["working_dir"], config)
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


async def synthesize(project_filter: str | None = None) -> int:
    """Scan diary, group by project, write per-project status summaries.

    When *project_filter* is given, only that project is (re)summarized — used
    by the live pipeline to refresh one project's ``now`` file after its diary
    is written, instead of rewriting every project.
    """
    paths = get_paths()
    diary_dir = paths.diary_dir()
    now_dir = paths.now_dir()

    entries_by_project = _scan_diary(diary_dir)
    if project_filter:
        entries_by_project = {
            k: v for k, v in entries_by_project.items() if k == project_filter
        }
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


def _parse_project_arg(argv: list[str]) -> str | None:
    """Return the value of ``--project NAME`` / ``--project=NAME`` if present."""
    for i, arg in enumerate(argv):
        if arg == "--project" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--project="):
            return arg.split("=", 1)[1]
    return None


def main() -> None:
    # stdin may carry a hook payload (ignored); drain it so a piping caller
    # doesn't block. The project scope comes from argv, not stdin.
    try:
        sys.stdin.read()
    except OSError:
        pass
    project_filter = _parse_project_arg(sys.argv[1:])
    sys.exit(asyncio.run(synthesize(project_filter)))


if __name__ == "__main__":
    main()
