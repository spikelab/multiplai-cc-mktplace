"""Diary catalog generator.

Implements DiaryGenerator, a GeneratorBase subclass that catalogs
diary day directories (YYYY-MM-DD named directories containing session files)
from the configured diary directory.

Design Decision 4: Per-day entries keyed by date string, each containing
session summaries, project references, topic tags, and word count.
Design Decision 2: Per-day-directory hashing (SHA-256 over sorted file contents).
"""

import hashlib
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from generators.base import GeneratorBase


# Regex for valid YYYY-MM-DD directory names
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_date_dir(name: str) -> bool:
    """Check if a directory name is a valid YYYY-MM-DD date."""
    if not _DATE_PATTERN.match(name):
        return False
    try:
        parts = name.split("-")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        date(year, month, day)
        return True
    except (ValueError, IndexError):
        return False


def _count_words(text: str) -> int:
    """Count words in text."""
    return len(text.split())


class DiaryGenerator(GeneratorBase):
    """Catalog generator for diary day directories.

    Scans the diary directory for YYYY-MM-DD named subdirectories,
    summarizes each day's files via LLM, and produces a per-day catalog
    with sessions, projects, topics, and word count.
    """

    name = "diary"
    catalog_filename = "diary.json"

    def discover_sources(self) -> dict[str, Any]:
        """Find all date-named directories within the lookback window.

        Scans CLAUDE_PLUGIN_OPTION_diary_dir for directories matching
        YYYY-MM-DD format, filtered by diary_catalog_days config.
        """
        diary_dir = Path(os.environ.get("CLAUDE_PLUGIN_OPTION_diary_dir", ""))
        if not diary_dir.exists() or not diary_dir.is_dir():
            return {}

        lookback_days = self._config.diary_catalog_days
        if lookback_days <= 0:
            return {}

        today = date.today()
        cutoff = today - timedelta(days=lookback_days - 1)

        sources = {}
        for entry in sorted(diary_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if not _is_valid_date_dir(name):
                continue
            entry_date = date.fromisoformat(name)
            if entry_date >= cutoff:
                sources[name] = entry
        return sources

    def hash_source(self, path: Path) -> str:
        """Compute SHA-256 over sorted file contents of a day directory.

        Files are sorted by name to ensure deterministic hashing
        regardless of filesystem ordering.
        """
        h = hashlib.sha256()
        for file_path in sorted(path.iterdir()):
            if file_path.is_file():
                h.update(file_path.name.encode("utf-8"))
                h.update(file_path.read_bytes())
        return h.hexdigest()

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a diary day directory."""
        date_str = source.name
        file_contents = []
        for file_path in sorted(source.iterdir()):
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                file_contents.append(f"### {file_path.name}\n{content}")

        combined = "\n\n".join(file_contents)

        return (
            f"Analyze the following diary entries for {date_str} and produce a JSON object with:\n"
            '- "sessions": an array of session objects, each with "id", "project", and "summary" fields\n'
            '- "projects": an array of project name strings mentioned in the entries\n'
            '- "topics": an array of topic strings covered in the entries\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{combined}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response text into a diary catalog entry dict.

        Handles JSON wrapped in markdown code fences.
        """
        text = raw.strip()
        # Strip markdown code fences
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        return json.loads(text)

    def _compute_word_count(self, day_dir: Path) -> int:
        """Compute total word count across all files in a day directory."""
        total = 0
        for file_path in sorted(day_dir.iterdir()):
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                total += _count_words(content)
        return total

    async def run(self, *, force: bool = False, dry_run: bool = False):
        """Override run to inject word_count into generated entries.

        word_count is computed from source content, not LLM output.
        """
        result = await super().run(force=force, dry_run=dry_run)

        if not dry_run:
            # Post-process catalog to add word_count from source files
            catalog = self._read_catalog()
            diary_dir = Path(os.environ.get("CLAUDE_PLUGIN_OPTION_diary_dir", ""))
            modified = False
            for entry in catalog.get("entries", []):
                entry_date = entry.get("date", entry.get("source", ""))
                day_dir = diary_dir / entry_date
                if day_dir.exists() and day_dir.is_dir():
                    entry["word_count"] = self._compute_word_count(day_dir)
                    # Also ensure date field is set
                    if "date" not in entry:
                        entry["date"] = entry_date
                    modified = True
                else:
                    entry.setdefault("word_count", 0)
            if modified:
                self._write_catalog(catalog)

        return result
