"""Memory catalog generator.

Implements MemoryGenerator, a GeneratorBase subclass that catalogs
memory files (*.md) from the configured memory directory.

Design Decision 5: Preserves hand-authored fields (sections, bundle,
co_retrieve_for) across regeneration via merge_entry() override.
"""

import os
from pathlib import Path
from typing import Any

from generators.base import GeneratorBase

# Hand-authored fields preserved during merge
_HAND_AUTHORED_FIELDS = ("sections", "bundle", "co_retrieve_for")


class MemoryGenerator(GeneratorBase):
    """Catalog generator for memory files.

    Scans the memory directory for .md files, summarizes each via LLM,
    and preserves hand-authored catalog fields across regeneration.
    """

    name = "memory"
    catalog_filename = "memory.json"

    def discover_sources(self) -> dict[str, Any]:
        """Find all .md files in the configured memory directory."""
        memory_dir = Path(os.environ.get("CLAUDE_PLUGIN_OPTION_memory_dir", ""))
        if not memory_dir.exists() or not memory_dir.is_dir():
            return {}

        sources = {}
        for path in sorted(memory_dir.glob("*.md")):
            if path.is_file():
                sources[path.name] = path
        return sources

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a memory file."""
        content = source.read_text(encoding="utf-8")
        return (
            "Analyze the following memory file and produce a JSON object with:\n"
            '- "summary": a concise summary of the file\'s content\n'
            '- "topics": an array of topic strings relevant for routing\n'
            '- "keywords": an array of keyword strings\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response into a memory catalog entry dict."""
        return self._parse_json_response(raw)

    def merge_entry(self, existing: dict | None, new: dict) -> dict:
        """Merge new LLM entry with existing, preserving hand-authored fields.

        Preserves: sections, bundle, co_retrieve_for from existing entry.
        Updates: all LLM-generated fields (summary, topics, keywords, etc).
        """
        if existing is None:
            return dict(new)

        merged = dict(new)
        for field in _HAND_AUTHORED_FIELDS:
            if field in existing:
                merged[field] = existing[field]
        return merged
