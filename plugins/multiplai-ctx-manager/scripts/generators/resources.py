"""Resources catalog generator.

Implements ResourcesGenerator, a GeneratorBase subclass that catalogs
resource files from the configured resources directory.

Design Decision 10: Gated on both enable_resources config flag AND
resources_dir being set to a non-empty string.
"""

from pathlib import Path
from typing import Any

from generators.base import GenerationResult, GeneratorBase

# Hand-authored intent fields preserved across regeneration so users
# can hand-tune routing hints without losing them on the next run.
_HAND_AUTHORED_FIELDS = (
    "intent_domains",
    "anti_domains",
)


class ResourcesGenerator(GeneratorBase):
    """Catalog generator for resource files.

    Recursively scans the resources directory for all files,
    summarizes each via LLM. Gated on both the enable_resources
    config flag and resources_dir being configured.
    """

    name = "resources"
    catalog_filename = "resources.json"

    def discover_sources(self) -> dict[str, Any]:
        """Recursively find all files in the configured resources directory.

        Returns source keys as relative paths from resources_dir.
        """
        if not self._config.resources_dir.strip():
            return {}

        resources_dir = Path(self._config.resources_dir)
        if not resources_dir.exists() or not resources_dir.is_dir():
            return {}

        sources = {}
        for path in sorted(resources_dir.rglob("*")):
            if path.is_file():
                rel_path = str(path.relative_to(resources_dir))
                sources[rel_path] = path
        return sources

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a resource file.

        Emits intent_domains / anti_domains so the multi-corpus router
        can select resources by task intent (parity with memory + skills
        catalogs). These fields are hand-authorable and preserved across
        regeneration.
        """
        try:
            content = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            content = f"[Binary file: {source.name}]"

        return (
            "Analyze the following resource file and produce a JSON object with:\n"
            '- "summary": a concise summary of the resource\'s content and purpose\n'
            '- "topics": an array of topic strings relevant for routing\n'
            '- "intent_domains": an array of short phrases describing task intents '
            'for which this resource is relevant (e.g., "researching voice AI '
            'frameworks", "comparing database options"). 2-6 phrases.\n'
            '- "anti_domains": an array of short phrases describing task intents '
            'for which this resource is NOT relevant (use sparingly — most '
            'resources have none). 0-3 phrases.\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response into a resources catalog entry dict."""
        return self._parse_json_response(raw)

    def merge_entry(self, existing: dict | None, new: dict) -> dict:
        """Merge new LLM entry with existing, preserving hand-authored intent fields."""
        if existing is None:
            return dict(new)
        merged = dict(new)
        for field in _HAND_AUTHORED_FIELDS:
            if field in existing:
                merged[field] = existing[field]
        return merged

    async def run(self, *, force: bool = False, dry_run: bool = False) -> GenerationResult:
        """Override run to gate on enable_resources and resources_dir config.

        When enable_resources is false or resources_dir is not set,
        returns early with zero work and does not write any files.
        """
        if not self._config.enable_resources or not self._config.resources_dir.strip():
            return self._disabled_result(dry_run=dry_run)

        return await super().run(force=force, dry_run=dry_run)
