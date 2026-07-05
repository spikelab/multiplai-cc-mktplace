"""Skills catalog generator.

Implements SkillsGenerator, a GeneratorBase subclass that catalogs
skill files (*.md) from the configured skills directory and from
installed Claude Code plugins (themed skill packs).

Design Decision 10: Gated on enable_skills config flag (default: false).
"""

import json
import os
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
        """Find all SKILL.md files, from two places:

        1. <skills_dir>/<name>/SKILL.md — the configured directory (in-tree
           kit skills or user skills).
        2. Installed Claude Code plugins — every install record in
           <plugins_dir>/installed_plugins.json contributes
           <installPath>/skills/<name>/SKILL.md (themed skill packs).

        Source keys are the skill directory names (the canonical skill
        identifier used by /<name> invocation). On a name collision the
        explicit skills_dir entry wins — a local skill overrides a
        plugin-shipped one of the same name.
        """
        sources: dict[str, Any] = {}
        skills_dir = Path(self._config.skills_dir).expanduser()
        if skills_dir.is_dir():
            for path in sorted(skills_dir.glob("*/SKILL.md")):
                if path.is_file():
                    sources[path.parent.name] = path

        for name, path in self._discover_plugin_skills().items():
            sources.setdefault(name, path)
        return sources

    def _discover_plugin_skills(self) -> dict[str, Path]:
        """Skills shipped by installed Claude Code plugins.

        Reads installed_plugins.json (v2 layout:
        ``{"plugins": {"name@marketplace": [{"installPath": ...}, ...]}}``)
        and globs ``<installPath>/skills/*/SKILL.md`` for every install
        record. Missing or malformed manifests yield no sources — plugin
        discovery must never break catalog generation.
        """
        plugins_dir = self._config.plugins_dir or os.path.join(
            os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")),
            "plugins",
        )
        manifest = Path(plugins_dir).expanduser() / "installed_plugins.json"
        if not manifest.is_file():
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        found: dict[str, Path] = {}
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            return {}
        for records in plugins.values():
            if not isinstance(records, list):
                continue
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                install = rec.get("installPath")
                if not install:
                    continue
                for path in sorted(Path(install).glob("skills/*/SKILL.md")):
                    if path.is_file():
                        found.setdefault(path.parent.name, path)
        return found

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
