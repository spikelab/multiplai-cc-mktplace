"""Skills catalog generator.

Implements SkillsGenerator, a GeneratorBase subclass that catalogs
skill files (*.md) from the configured skills directory.

Design Decision 10: Gated on enable_skills config flag (default: false).
"""

from pathlib import Path
from typing import Any

from generators.base import GenerationResult, GeneratorBase

# Hand-authored intent fields preserved across regeneration. Skills use
# the same intent_domains / anti_domains schema as memory and resources
# so the multi-corpus router can apply uniform routing rules.
_HAND_AUTHORED_FIELDS = (
    "intent_domains",
    "anti_domains",
)


class SkillsGenerator(GeneratorBase):
    """Catalog generator for skill files.

    Scans the skills directory for .md files, summarizes each via LLM
    to extract name, summary, and intent_domains. Gated on the
    enable_skills config flag.
    """

    name = "skills"
    catalog_filename = "skills.json"

    def discover_sources(self) -> dict[str, Any]:
        """Find all SKILL.md files in <skills_dir>/<name>/SKILL.md.

        Matches the Claude Code skill layout: one directory per skill
        containing a SKILL.md (plus optional assets/, references/, scripts/).
        Source keys are the skill directory names (the canonical skill
        identifier used by /<name> invocation).
        """
        skills_dir = Path(self._config.skills_dir)
        if not skills_dir.exists() or not skills_dir.is_dir():
            return {}

        sources = {}
        for path in sorted(skills_dir.glob("*/SKILL.md")):
            if path.is_file():
                sources[path.parent.name] = path
        return sources

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a skill file.

        Emits intent_domains (uniform with memory + resources) so the
        multi-corpus router can match skills against task intent. The
        old "triggers" field has been renamed to intent_domains for
        cross-corpus consistency.
        """
        content = source.read_text(encoding="utf-8")
        return (
            "Analyze the following skill file and produce a JSON object with:\n"
            '- "name": the skill name (short identifier)\n'
            '- "summary": a concise summary of what the skill does\n'
            '- "intent_domains": an array of short phrases describing task intents '
            'that should invoke this skill (e.g., "writing a blog post", '
            '"running a code review"). 2-6 phrases.\n'
            '- "anti_domains": an array of short phrases describing task intents '
            'where this skill should NOT be invoked (use sparingly). 0-3 phrases.\n\n'
            "Respond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response into a skills catalog entry dict."""
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

    async def run(
        self, *, force: bool = False, dry_run: bool = False, force_enable: bool = False
    ) -> GenerationResult:
        """Override run to gate on enable_skills config.

        When enable_skills is false, returns early with zero work
        and does not write any catalog or state files.

        ``force_enable`` (set by the dispatcher when this generator is
        explicitly named in an ``--only`` filter) bypasses the
        enable_skills flag so an explicit request runs regardless.
        """
        if not force_enable and not self._config.enable_skills:
            return self._disabled_result(dry_run=dry_run)

        return await super().run(force=force, dry_run=dry_run)
