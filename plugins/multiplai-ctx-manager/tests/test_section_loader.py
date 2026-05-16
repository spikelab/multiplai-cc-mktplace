"""Tests for scripts/lib/section_loader.py."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# parse_section_ref
# ---------------------------------------------------------------------------


class TestParseSectionRef:
    def test_no_fragment_returns_filename_and_none(self):
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md") == ("file.md", None)

    def test_with_fragment_returns_both(self):
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md#Section Name") == ("file.md", "Section Name")

    def test_empty_fragment_returns_none(self):
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md#") == ("file.md", None)

    def test_whitespace_fragment_returns_none(self):
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md#   ") == ("file.md", None)

    def test_fragment_is_trimmed(self):
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md#  Section  ") == ("file.md", "Section")

    def test_only_first_hash_splits(self):
        """Multiple # — only the first one is the separator."""
        from lib.section_loader import parse_section_ref
        assert parse_section_ref("file.md#A#B") == ("file.md", "A#B")


# ---------------------------------------------------------------------------
# extract_section
# ---------------------------------------------------------------------------


_SAMPLE_DOC = """# Title

Intro paragraph.

## Architecture

Some architecture text.

### Subheader

Nested.

## Decisions

Decisions content.

## Operations

Ops content (last section).
"""


class TestExtractSection:
    def test_returns_full_text_when_section_empty(self):
        from lib.section_loader import extract_section
        assert extract_section(_SAMPLE_DOC, "") == _SAMPLE_DOC

    def test_returns_full_text_when_text_empty(self):
        from lib.section_loader import extract_section
        assert extract_section("", "Foo") == ""

    def test_extracts_named_section(self):
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "Architecture")
        assert result.startswith("## Architecture")
        assert "Some architecture text." in result
        # Should stop at next H2
        assert "Decisions content." not in result
        assert "## Decisions" not in result

    def test_includes_nested_subheaders(self):
        """### subheaders within an H2 section are included."""
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "Architecture")
        assert "### Subheader" in result
        assert "Nested." in result

    def test_extracts_last_section_to_eof(self):
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "Operations")
        assert result.startswith("## Operations")
        assert "Ops content" in result

    def test_case_insensitive_matching(self):
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "architecture")
        assert result.startswith("## Architecture")

    def test_trimmed_matching(self):
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "  Architecture  ")
        assert result.startswith("## Architecture")

    def test_missing_section_returns_full_text(self):
        """Fallback: section not found → return whole file."""
        from lib.section_loader import extract_section
        result = extract_section(_SAMPLE_DOC, "Nonexistent")
        assert result == _SAMPLE_DOC

    def test_h1_does_not_match(self):
        """H1 is not eligible — only H2 anchors are loadable sections."""
        from lib.section_loader import extract_section
        # "Title" is an H1, asking for it falls back to full doc
        assert extract_section(_SAMPLE_DOC, "Title") == _SAMPLE_DOC

    def test_h3_does_not_match(self):
        """H3 is not eligible — only H2."""
        from lib.section_loader import extract_section
        assert extract_section(_SAMPLE_DOC, "Subheader") == _SAMPLE_DOC


# ---------------------------------------------------------------------------
# load_picked_content
# ---------------------------------------------------------------------------


class TestLoadPickedContent:
    def test_no_fragment_returns_full_text(self):
        from lib.section_loader import load_picked_content
        filename, content = load_picked_content("file.md", _SAMPLE_DOC)
        assert filename == "file.md"
        assert content == _SAMPLE_DOC

    def test_with_fragment_returns_section(self):
        from lib.section_loader import load_picked_content
        filename, content = load_picked_content("file.md#Architecture", _SAMPLE_DOC)
        assert filename == "file.md"
        assert content.startswith("## Architecture")
        assert "Decisions content" not in content

    def test_filename_strips_fragment(self):
        from lib.section_loader import load_picked_content
        filename, _ = load_picked_content("complex/path/to/file.md#Anchor", "doc")
        assert filename == "complex/path/to/file.md"
