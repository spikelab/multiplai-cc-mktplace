"""Research type-specific guidance injected into PLAN/DIVERGE/CHALLENGE prompts.

Loads and caches the research-types.md reference file so prompts can include
type-specific patterns. Falls back to inline guidance if the file is missing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


# Inline fallback — concise type guidance used if references/research-types.md is missing
INLINE_GUIDANCE = {
    "general": {
        "plan": "No domain bias. Break the query into 2-3 focused aspects.",
        "diverge": "Generate broad mechanism angles and directory searches.",
        "challenge": "Apply generic contrarian patterns: 'problems with X', 'alternatives to X'.",
    },
    "company": {
        "plan": "Focus on: funding, product, leadership, culture, competitors. Staleness threshold: 90 days for facts.",
        "diverge": "Target crunchbase.com, linkedin.com, news sites. Search for recent leadership moves.",
        "challenge": "Ask: 'failures of [company]', '[company] culture problems', 'leaving [company] for', 'layoffs [company]'.",
    },
    "job-market": {
        "plan": "Focus on: demand, salary, location, trends. Staleness threshold: 30 days.",
        "diverge": "Target linkedin.com, glassdoor.com, levels.fyi, industry reports.",
        "challenge": "Ask: 'why NOT to take [role]', 'regret becoming [role]', '[role] salary declining', '[role] replaced by AI'.",
    },
    "fact-check": {
        "plan": "Focus on: verification via primary sources. Require authoritative sources.",
        "diverge": "Target .gov, .edu, and primary data sources. Cross-reference multiple sources.",
        "challenge": "Ask: 'counter-evidence for [claim]', '[claim] disputed by', 'debunking [claim]'.",
    },
    "theme": {
        "plan": "Focus on: multiple perspectives, examples, evidence. Acceptable staleness: 1-2 years.",
        "diverge": "Target academic sources, news, expert blogs, diverse viewpoints.",
        "challenge": "Ask: '[concept] is wrong', 'arguments against [approach]', '[popular opinion] debunked'.",
    },
}


# Default location — SKILL.md references directory
DEFAULT_REFERENCE_PATH = (
    Path(__file__).parent.parent.parent / "references" / "research-types.md"
)


@lru_cache(maxsize=1)
def _load_reference_file(path: str) -> str | None:
    """Load the research-types.md file content, cached."""
    try:
        p = Path(path)
        if p.exists():
            return p.read_text()
    except Exception:  # noqa: BLE001
        pass
    return None


def guidance_for(
    research_type: str, stage: str, reference_path: Path | None = None
) -> str:
    """Return type-specific guidance for a given pipeline stage.

    Tries to extract the relevant section from references/research-types.md
    (if available), falling back to inline guidance otherwise.
    """
    path = reference_path or DEFAULT_REFERENCE_PATH
    reference = _load_reference_file(str(path))

    # If we have the reference file, extract the section for this research type
    if reference:
        section = _extract_section(reference, research_type)
        if section:
            return section

    # Fallback to inline guidance
    inline = INLINE_GUIDANCE.get(research_type, INLINE_GUIDANCE["general"])
    return inline.get(stage, inline.get("plan", ""))


def _extract_section(reference: str, research_type: str) -> str | None:
    """Extract a markdown section for a research type from the reference file.

    Looks for a heading like `## {research_type}` or `### {research_type}` and
    returns the content until the next heading of the same or higher level.
    """
    lines = reference.split("\n")
    section_lines: list[str] = []
    in_section = False
    section_level = 0

    import re

    for line in lines:
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).lower().strip()

            if in_section and level <= section_level:
                break

            if not in_section and research_type.lower() in title:
                in_section = True
                section_level = level
                section_lines.append(line)
                continue

        if in_section:
            section_lines.append(line)

    if section_lines:
        return "\n".join(section_lines).strip()
    return None
