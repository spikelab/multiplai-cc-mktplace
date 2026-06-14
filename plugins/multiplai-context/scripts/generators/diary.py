"""Diary catalog generator.

Implements DiaryGenerator, a GeneratorBase subclass that catalogs
per-day diary files (``YYYY-MM-DD.md``) from the configured diary
directory.

Design Decision 4: Per-day entries keyed by date string, each containing
session summaries, project references, topic tags, and word count.
Design Decision 2: Per-day-file hashing (SHA-256 over file contents).

Layout (v0.3.0+): one file per day, ``diary_dir/YYYY-MM-DD.md``, with
``## Session: <id>`` blocks inside. Aligned with learnings layout.
"""

import hashlib
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from multiplai_core.paths import Paths
from generators.base import GeneratorBase


# Regex for valid YYYY-MM-DD file stems
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_date_stem(stem: str) -> bool:
    """Check if a file stem is a valid YYYY-MM-DD date."""
    if not _DATE_PATTERN.match(stem):
        return False
    try:
        date.fromisoformat(stem)
        return True
    except ValueError:
        return False


def _count_words_in_file(day_file: Path) -> int:
    """Compute total word count of a per-day diary file."""
    if not day_file.is_file():
        return 0
    return len(day_file.read_text(encoding="utf-8").split())


class DiaryGenerator(GeneratorBase):
    """Catalog generator for per-day diary files.

    Scans the diary directory for ``YYYY-MM-DD.md`` files, summarizes
    each day's content via LLM, and produces a per-day catalog with
    sessions, projects, topics, and word count.
    """

    name = "diary"
    catalog_filename = "diary.json"

    @property
    def _diary_dir(self) -> Path:
        """Configured diary directory, resolver-routed.

        Uses the path resolver (not the raw env var) so the
        workspace/standalone fallbacks apply when
        CLAUDE_PLUGIN_OPTION_diary_dir is unset.
        """
        return Paths.resolve().diary_dir()

    def discover_sources(self) -> dict[str, Any]:
        """Find all ``YYYY-MM-DD.md`` files within the lookback window."""
        diary_dir = self._diary_dir
        if not diary_dir.exists() or not diary_dir.is_dir():
            return {}

        lookback_days = self._config.diary_catalog_days
        if lookback_days <= 0:
            return {}

        cutoff = date.today() - timedelta(days=lookback_days - 1)

        sources = {}
        for entry in sorted(diary_dir.glob("*.md")):
            stem = entry.stem
            if not _is_valid_date_stem(stem):
                continue
            if date.fromisoformat(stem) >= cutoff:
                sources[stem] = entry
        return sources

    def hash_source(self, path: Path) -> str:
        """Compute SHA-256 over the per-day diary file contents."""
        h = hashlib.sha256()
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
        return h.hexdigest()

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a per-day diary file."""
        date_str = source.stem
        content = source.read_text(encoding="utf-8")

        return (
            f"Analyze the following diary entries for {date_str} and produce a JSON object with:\n"
            '- "sessions": an array of session objects, each with "id", "project", and "summary" fields\n'
            '- "projects": an array of project name strings mentioned in the entries\n'
            '- "topics": an array of topic strings covered in the entries\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response into a diary catalog entry dict."""
        return self._parse_json_response(raw)

    async def run(self, *, force: bool = False, dry_run: bool = False):
        """Override run to inject word_count and date into generated entries.

        word_count is computed from source content, not LLM output.
        """
        result = await super().run(force=force, dry_run=dry_run)

        if not dry_run:
            self._enrich_entries_with_word_counts()

        return result

    def _enrich_entries_with_word_counts(self) -> None:
        """Post-process catalog entries to add word_count and date fields.

        word_count is computed directly from the per-day file for accuracy.
        date is set from the source key (YYYY-MM-DD file stem).
        """
        catalog = self._read_catalog()
        diary_dir = self._diary_dir
        modified = False

        for entry in catalog.get("entries", []):
            entry_date = entry.get("source", "")
            day_file = diary_dir / f"{entry_date}.md"
            if day_file.is_file():
                entry["word_count"] = _count_words_in_file(day_file)
                entry.setdefault("date", entry_date)
                modified = True
            else:
                entry.setdefault("word_count", 0)

        if modified:
            self._write_catalog(catalog)
