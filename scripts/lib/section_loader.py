"""Section-level loading for picked memory entries.

When a catalog entry declares ``section_anchors``, the router may
pick ``"file.md#Section Name"`` instead of the bare filename. This
module parses such references and extracts the matching H2 section
from the file's text.

If the section can't be found (typo, file changed since catalog
generation), the loader returns the FULL file as a fallback —
better to show too much than nothing. Empty section refs (just
``"file.md#"``) also return the full file.

Section matching is case-insensitive on the trimmed header text.
The header line itself is included in the extracted output so the
loaded snippet stays self-describing.
"""

from __future__ import annotations

import re

# Match "## Section Name" — H2 only (not H1 or H3+). Captures the
# header text after the leading "## " (and any trailing whitespace
# before line end).
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def parse_section_ref(name: str) -> tuple[str, str | None]:
    """Split ``"file.md#Section"`` into ``("file.md", "Section")``.

    Returns ``(name, None)`` if no fragment is present or the fragment
    is empty after trimming.
    """
    if "#" not in name:
        return name, None
    base, fragment = name.split("#", 1)
    fragment = fragment.strip()
    if not fragment:
        return base, None
    return base, fragment


def extract_section(text: str, section_name: str) -> str:
    """Return the named H2 section's content, or the full text if not found.

    Searches for an H2 header matching ``section_name`` (case-insensitive,
    trimmed). Returns from that header line up to (but not including) the
    next H2, or end of file if it's the last section. The matched header
    line is included so the extracted snippet is self-describing.

    Falls back to returning ``text`` unchanged when no matching header is
    found — better to show the whole file than silently drop content.
    """
    if not text or not section_name:
        return text

    target = section_name.strip().lower()
    if not target:
        return text

    matches = list(_H2_RE.finditer(text))
    if not matches:
        return text

    for i, m in enumerate(matches):
        header = m.group(1).strip().lower()
        if header == target:
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[start:end].rstrip() + "\n"
    return text


def load_picked_content(
    name: str,
    file_text: str,
) -> tuple[str, str]:
    """Resolve ``name`` to ``(filename, content)`` for the picked entry.

    If ``name`` has a ``#Section`` fragment, returns the extracted
    section; otherwise returns the full ``file_text``. The first tuple
    element is the bare filename (no fragment) so callers can use it
    as a stable key for output formatting.
    """
    base, section = parse_section_ref(name)
    if section is None:
        return base, file_text
    return base, extract_section(file_text, section)
