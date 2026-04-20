"""Skills catalog generator.

Implements SkillsGenerator, a GeneratorBase subclass that catalogs
skill files (*.md) from the configured skills directory.

Design Decision 10: Gated on enable_skills config flag (default: false).
"""

from pathlib import Path
from typing import Any

from generators.base import GenerationResult, GeneratorBase


class SkillsGenerator(GeneratorBase):
    """Catalog generator for skill files.

    Scans the skills directory for .md files, summarizes each via LLM
    to extract name, summary, and trigger phrases. Gated on the
    enable_skills config flag.
    """

    name = "skills"
    catalog_filename = "skills.json"

    def discover_sources(self) -> dict[str, Any]:
        """Find all .md files in the configured skills directory."""
        skills_dir = Path(self._config.skills_dir)
        if not skills_dir.exists() or not skills_dir.is_dir():
            return {}

        sources = {}
        for path in sorted(skills_dir.glob("*.md")):
            if path.is_file():
                sources[path.name] = path
        return sources

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a skill file."""
        content = source.read_text(encoding="utf-8")
        return (
            "Analyze the following skill file and produce a JSON object with:\n"
            '- "name": the skill name (short identifier)\n'
            '- "summary": a concise summary of what the skill does\n'
            '- "triggers": an array of trigger phrases that would invoke this skill\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response into a skills catalog entry dict."""
        return self._parse_json_response(raw)

    async def run(self, *, force: bool = False, dry_run: bool = False) -> GenerationResult:
        """Override run to gate on enable_skills config.

        When enable_skills is false, returns early with zero work
        and does not write any catalog or state files.
        """
        if not self._config.enable_skills:
            return GenerationResult(
                generator=self.name,
                total_sources=0,
                skipped=0,
                generated=0,
                pruned=0,
                errors=[],
                dry_run=dry_run,
            )

        return await super().run(force=force, dry_run=dry_run)
