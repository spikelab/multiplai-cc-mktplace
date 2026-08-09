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

Two things a section pick must not lose, both of which it used to:

* **The file's preamble** — everything before the first H2. That is where
  ``**Last Updated:**``, the ``**Purpose:**`` statement, cross-file
  instructions like ``> **Load core-voice.md first.**`` and
  ``Boundaries — route elsewhere:`` routing prose live. No section slice
  contains it, so :func:`preamble` exists to let the caller prepend it once
  per file. Without that, a section pick returns strictly less than the
  whole file would have, which falsifies the only safety claim this feature
  makes.
* **A repeated H2's later bodies** — see :func:`extract_section`.
"""

from __future__ import annotations

import re

# Match "## Section Name" — H2 only (not H1 or H3+), one line at a time.
#
# Horizontal whitespace only, deliberately: with `\s+` and re.MULTILINE a bare
# "##" line matched across the newline and invented a section named after the
# *next* line.
_H2_LINE_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")

# A fenced block opener/closer. Markdown allows up to three leading spaces and
# either backticks or tildes; the fence character has to match to close.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _h2_spans(text: str) -> list[tuple[int, str]]:
    """``[(offset, header)]`` for every real H2, in document order.

    **Code fences are excluded.** A ``## Fake Heading`` inside a fenced
    markdown example is a line of sample text, not a section, and treating it
    as one caused two failures at once: the enclosing real section was
    truncated mid-fence with its remaining prose silently relocated into a
    phantom section, and the phantom name entered the catalog for the model to
    gloss and the router to pick. Memory files are exactly where markdown
    examples get pasted, so this is one paste away rather than hypothetical.

    ``memory_lint`` skips fences too — it must, or the ``duplicate-h2`` check
    would be structurally blind to the collisions this generator can
    manufacture. It keeps a local copy of these two patterns because it runs
    as a standalone script where a sibling ``lib.*`` import does not resolve;
    the shapes must stay in step.

    Offsets are byte-exact indices into *text* so :func:`extract_section` can
    slice with them, and every H2 consumer in this module goes through here —
    that shared derivation is what makes "the catalog's names and the loader's
    names agree" a structural property rather than a hope.
    """
    spans: list[tuple[int, str]] = []
    fence: str | None = None
    offset = 0
    for line in (text or "").splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            token = fence_match.group(1)[0]
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
        elif fence is None:
            header_match = _H2_LINE_RE.match(stripped)
            if header_match:
                header = header_match.group(1).strip()
                if header:
                    spans.append((offset, header))
        offset += len(line)
    return spans


def h2_names(text: str) -> list[str]:
    """Return the file's H2 header texts, in document order, de-duplicated.

    The single place any caller should learn what sections a file has.
    Catalog generation glosses this exact list and the router picks from
    it, so extraction MUST use the same parser ``extract_section`` matches
    with — an anchor name produced by a second, subtly different parser is
    a permanent silent full-file fallback. Both now call :func:`_h2_spans`.

    De-duplication is case-insensitive on the trimmed text, keeping the
    first spelling: a repeated header is one addressable section whose body
    is the concatenation of every occurrence (see :func:`extract_section`),
    not two separately pickable ones.
    """
    seen: set[str] = set()
    names: list[str] = []
    for _, header in _h2_spans(text):
        key = header.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(header)
    return names


def preamble(text: str) -> str:
    """Everything before the file's first H2, or ``""`` if there is none.

    Not part of any section, and therefore dropped by every section pick
    unless a caller puts it back. It is small (40–896 characters across this
    corpus) and it is where the load-bearing prose lives: the date stamp, the
    purpose statement, cross-file load instructions, and the
    ``Boundaries — route elsewhere:`` routing hints.

    Prepend it **once per file**, not once per section — a single prompt may
    now pick several sections from one file, and repeating the preamble per
    slice would spend the saving three times over.
    """
    spans = _h2_spans(text)
    if not spans:
        return ""
    head = (text or "")[: spans[0][0]]
    return head.rstrip() + "\n" if head.strip() else ""


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

    Searches for H2 headers matching ``section_name`` (case-insensitive,
    trimmed). Returns from each matching header line up to (but not
    including) the next H2, or end of file for the last section. The matched
    header line is included so the extracted snippet is self-describing.

    **Every occurrence of a repeated name is returned, concatenated.** A file
    with the same H2 twice used to yield only the first body, because the
    slice ended at "the next H2" — which, for a repeated name, is the second
    occurrence of that same name. The second body was then unreachable by any
    pick while the model glossed the name having read the whole file. Joining
    them keeps the pick lossless; ``memory_lint`` reports the duplicate so it
    can be retitled, which is the actual fix.

    Falls back to returning ``text`` unchanged when no matching header is
    found — better to show the whole file than silently drop content.
    """
    if not text or not section_name:
        return text

    target = section_name.strip().lower()
    if not target:
        return text

    spans = _h2_spans(text)
    if not spans:
        return text

    bodies: list[str] = []
    for i, (start, header) in enumerate(spans):
        if header.strip().lower() != target:
            continue
        end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        bodies.append(text[start:end].rstrip())
    if not bodies:
        return text
    return "\n\n".join(bodies) + "\n"


def load_picked_content(
    name: str,
    file_text: str,
) -> tuple[str, str]:
    """Resolve ``name`` to ``(filename, content)`` for the picked entry.

    If ``name`` has a ``#Section`` fragment, returns the extracted
    section; otherwise returns the full ``file_text``. The first tuple
    element is the bare filename (no fragment) so callers can use it
    as a stable key for output formatting.

    Note this is a **per-pick** helper and therefore deliberately does *not*
    add the file's :func:`preamble`: doing it here would repeat the preamble
    once per section when a prompt picks several sections from one file. The
    caller groups picks by file and prepends it once — see
    ``context_manager._load_memory_content``.
    """
    base, section = parse_section_ref(name)
    if section is None:
        return base, file_text
    return base, extract_section(file_text, section)
