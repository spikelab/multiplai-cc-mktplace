"""Tests for memory templates (templates/*.md)."""

import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

TEMPLATES_DIR = PLUGIN_ROOT / "templates"
TEMPLATE_FILES = ["me.md", "technical-pref.md", "preferences.md"]


class TestTemplateExistence:
    """Verify template files exist."""

    def test_templates_dir_exists(self):
        assert TEMPLATES_DIR.is_dir()

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_template_exists(self, filename):
        assert (TEMPLATES_DIR / filename).is_file(), f"Template missing: {filename}"

    def test_exactly_three_templates(self):
        md_files = [f.name for f in TEMPLATES_DIR.iterdir() if f.suffix == ".md"]
        assert set(md_files) == set(TEMPLATE_FILES)


class TestTemplateValidMarkdown:
    """Verify templates are valid markdown with headings and placeholders."""

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_has_heading(self, filename):
        text = (TEMPLATES_DIR / filename).read_text()
        assert re.search(r"^#{1,2}\s+\S", text, re.MULTILINE), \
            f"{filename} has no markdown heading"

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_has_placeholder_or_prompt(self, filename):
        text = (TEMPLATES_DIR / filename).read_text()
        has_comment = "<!--" in text
        has_prompt = re.search(r"(?i)(list|describe|what|how|your)", text) is not None
        assert has_comment or has_prompt, \
            f"{filename} has no placeholder or instructional text"


class TestMeTemplate:
    """Verify me.md template structure."""

    @pytest.fixture(autouse=True)
    def load_template(self):
        self.text = (TEMPLATES_DIR / "me.md").read_text()

    def test_has_identity_section(self):
        assert re.search(r"(?i)(identity|about)", self.text)

    def test_has_background_section(self):
        assert re.search(r"(?i)(background|experience)", self.text)

    def test_has_communication_section(self):
        assert re.search(r"(?i)(communication|style)", self.text)

    def test_no_developer_specific_data(self):
        assert "Spike" not in self.text
        assert "spike" not in self.text.split("<!--")[0]  # Allow in comments only if needed
        assert "spikelab" not in self.text


class TestTechnicalPrefTemplate:
    """Verify technical-pref.md template structure."""

    @pytest.fixture(autouse=True)
    def load_template(self):
        self.text = (TEMPLATES_DIR / "technical-pref.md").read_text()

    def test_has_languages_section(self):
        assert re.search(r"(?i)language", self.text)

    def test_has_frameworks_section(self):
        assert re.search(r"(?i)(framework|librar)", self.text)

    def test_has_coding_style_section(self):
        assert re.search(r"(?i)(coding\s+style|style)", self.text)

    def test_has_tooling_section(self):
        assert re.search(r"(?i)tool", self.text)

    def test_has_actionable_placeholders(self):
        has_examples = re.search(r"(?i)(list|describe|e\.g\.|example|prefer)", self.text)
        has_comments = "<!--" in self.text
        assert has_examples or has_comments


class TestPreferencesTemplate:
    """Verify preferences.md template structure."""

    @pytest.fixture(autouse=True)
    def load_template(self):
        self.text = (TEMPLATES_DIR / "preferences.md").read_text()

    def test_has_verbosity_section(self):
        assert re.search(r"(?i)(verbos|detail)", self.text)

    def test_has_tone_section(self):
        assert re.search(r"(?i)tone", self.text)

    def test_has_workflow_section(self):
        assert re.search(r"(?i)workflow", self.text)


class TestNoSensitiveContent:
    """Verify templates contain no sensitive or environment-specific content."""

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_hardcoded_paths(self, filename):
        text = (TEMPLATES_DIR / filename).read_text()
        assert "/Users/" not in text
        assert "/home/" not in text
        assert "C:\\" not in text

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_credentials(self, filename):
        text = (TEMPLATES_DIR / filename).read_text()
        assert not re.search(r"sk-[a-zA-Z0-9]{20,}", text), "Possible API key found"
        assert not re.search(r"(?i)password\s*[:=]\s*\S+", text), "Possible password found"


class TestEncoding:
    """Verify UTF-8 encoding and LF line endings."""

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_no_bom(self, filename):
        raw = (TEMPLATES_DIR / filename).read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", f"{filename} has UTF-8 BOM"

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_valid_utf8(self, filename):
        raw = (TEMPLATES_DIR / filename).read_bytes()
        raw.decode("utf-8")  # should not raise

    @pytest.mark.parametrize("filename", TEMPLATE_FILES)
    def test_lf_line_endings(self, filename):
        raw = (TEMPLATES_DIR / filename).read_bytes()
        assert b"\r\n" not in raw, f"{filename} has CRLF line endings"
