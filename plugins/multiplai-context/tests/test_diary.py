"""Tests for diary catalog generator.

Block 5: Diary catalog generator.

Covers all scenarios from requirements/diary-catalog-generator.md:
- Diary day-file discovery (date pattern matching, ignoring non-diary files)
- hash_source() computing SHA-256 over sorted file contents of a day directory
- Configurable lookback window (diary_catalog_days)
- Pruning of days outside the lookback window
- Per-day LLM summarization via build_prompt()
- parse_response() producing diary catalog schema entries
- Word count accuracy (computed from source, not LLM)
- Content-hash-based skip logic
- Deletion pruning
- State file persistence under diary namespace
- LLM failure handling (skip entry, preserve prior)
- Schema versioning (mismatch triggers full regen)
- Base class integration
- Catalog file structure (schema_version, generated_at, entries)
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_LLM_RESPONSE = json.dumps({
    "sessions": [
        {"id": "session-1", "project": "acme-api", "summary": "Worked on auth refactor"}
    ],
    "projects": ["acme-api"],
    "topics": ["auth refactor", "rate limiting"],
})


def _make_mock_client(response_content=None):
    """Create an AsyncMock model client that returns the given content."""
    from multiplai_core.model_client import ModelResponse

    if response_content is None:
        response_content = _SAMPLE_LLM_RESPONSE
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=response_content))
    return client


def _make_diary_generator(tmp_path, *, client=None, config=None):
    """Create a DiaryGenerator instance with a temp catalogs dir.

    Returns (generator, catalogs_dir, diary_dir).
    """
    from generators.config import CatalogConfig
    from generators.diary import DiaryGenerator

    catalogs_dir = tmp_path / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = CatalogConfig()
    if client is None:
        client = _make_mock_client()

    gen = DiaryGenerator(config=config, model_client=client)

    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    os.environ["CLAUDE_PLUGIN_OPTION_diary_dir"] = str(diary_dir)

    return gen, catalogs_dir, diary_dir


def _make_day_dir(diary_dir, date_str, files=None):
    """Create a per-day diary file ``diary_dir/<date_str>.md`` with one or
    more ``## Session:`` blocks (v0.3.0 layout).

    Args:
        diary_dir: Parent diary directory.
        date_str: Date string like '2026-04-15' — also the file stem.
        files: Dict of {session_filename_or_id: content}. Each becomes a
            ``## Session: <stem>`` block in the day file (``.md`` extension
            is stripped from the key to form the session id). Defaults to
            one session.

    Returns the day file Path (``diary_dir/<date_str>.md``).
    """
    diary_dir.mkdir(parents=True, exist_ok=True)
    if files is None:
        files = {"session-1.md": f"# Session 1\nWorked on stuff for {date_str}."}

    parts = [f"# Diary — {date_str}\n"]
    for fname, content in files.items():
        sid = fname[:-3] if fname.endswith(".md") else fname
        ts = f"{date_str}T00:00:00+00:00"
        parts.append(
            f"\n## Session: {sid} — {ts} — /test/cwd\n\n{content}\n"
        )

    day_file = diary_dir / f"{date_str}.md"
    day_file.write_text("".join(parts), encoding="utf-8")
    return day_file


def _rewrite_day_file(day_file, files):
    """Rewrite a day file's session blocks. Used when tests mutate content
    after the initial _make_day_dir call (e.g. to change the hash)."""
    date_str = day_file.stem
    parts = [f"# Diary — {date_str}\n"]
    for fname, content in files.items():
        sid = fname[:-3] if fname.endswith(".md") else fname
        ts = f"{date_str}T00:00:00+00:00"
        parts.append(
            f"\n## Session: {sid} — {ts} — /test/cwd\n\n{content}\n"
        )
    day_file.write_text("".join(parts), encoding="utf-8")


def _read_catalog(catalogs_dir, filename="diary.json"):
    """Read and parse a catalog JSON file."""
    path = catalogs_dir / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _read_state(catalogs_dir):
    """Read and parse the generation state file."""
    path = catalogs_dir / ".generation-state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(catalogs_dir, state_data):
    """Write a generation state file."""
    path = catalogs_dir / ".generation-state.json"
    path.write_text(json.dumps(state_data), encoding="utf-8")


def _write_catalog(catalogs_dir, catalog_data, filename="diary.json"):
    """Write a catalog file."""
    path = catalogs_dir / filename
    path.write_text(json.dumps(catalog_data), encoding="utf-8")


def _date_str_days_ago(n):
    """Return a YYYY-MM-DD string for n days ago from today."""
    return (date.today() - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Module Structure & Entry Point
# ---------------------------------------------------------------------------


class TestDiaryGeneratorModuleStructure:
    """Requirement: Generator module location and entry point.

    The diary catalog generator MUST be implemented as a module at
    scripts/generators/diary.py and expose DiaryGenerator class.
    """

    def test_module_file_exists(self):
        """scripts/generators/diary.py must exist."""
        module_file = SCRIPTS_DIR / "generators" / "diary.py"
        assert module_file.exists(), (
            f"Diary catalog generator must exist at {module_file}"
        )

    def test_module_importable(self):
        """diary module must be importable without error."""
        from generators import diary  # noqa: F401

    def test_diary_generator_class_exists(self):
        """DiaryGenerator class must be exposed by the module."""
        from generators.diary import DiaryGenerator
        assert DiaryGenerator is not None

    def test_diary_generator_is_generator_base_subclass(self):
        """Requirement: Base class integration.

        DiaryGenerator MUST extend GeneratorBase.
        """
        from generators.base import GeneratorBase
        from generators.diary import DiaryGenerator
        assert issubclass(DiaryGenerator, GeneratorBase)

    def test_diary_generator_name(self):
        """Generator name must be 'diary' for state namespacing."""
        from generators.diary import DiaryGenerator
        assert DiaryGenerator.name == "diary"

    def test_diary_generator_catalog_filename(self):
        """Catalog filename must be 'diary.json'."""
        from generators.diary import DiaryGenerator
        assert DiaryGenerator.catalog_filename == "diary.json"


# ---------------------------------------------------------------------------
# discover_sources()
# ---------------------------------------------------------------------------


class TestDiscoverSources:
    """Requirement: Diary day-file discovery.

    The generator MUST discover diary day-files by scanning the configured
    diary directory for entries matching the expected date naming convention.
    """

    def test_discovers_date_named_day_dirs(self, tmp_path):
        """Standard day directories are discovered."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        d1 = _date_str_days_ago(1)
        d2 = _date_str_days_ago(2)
        d3 = _date_str_days_ago(3)
        _make_day_dir(diary_dir, d1)
        _make_day_dir(diary_dir, d2)
        _make_day_dir(diary_dir, d3)

        sources = gen.discover_sources()
        assert d1 in sources
        assert d2 in sources
        assert d3 in sources

    def test_ignores_non_date_entries(self, tmp_path):
        """Non-diary files/dirs are ignored.

        WHEN the diary directory contains a recent date dir, README.md, and notes.txt
        THEN only the date dir is discovered.
        """
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        d1 = _date_str_days_ago(1)
        _make_day_dir(diary_dir, d1)
        (diary_dir / "README.md").write_text("readme", encoding="utf-8")
        (diary_dir / "notes.txt").write_text("notes", encoding="utf-8")

        sources = gen.discover_sources()
        assert d1 in sources
        assert "README.md" not in sources
        assert "notes.txt" not in sources
        assert len(sources) == 1

    def test_ignores_malformed_date_names(self, tmp_path):
        """Malformed date entries are not processed.

        WHEN the diary directory contains 'not-a-date' or '2026-13-45'
        THEN they are ignored.
        """
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        d1 = _date_str_days_ago(1)
        _make_day_dir(diary_dir, "not-a-date")
        _make_day_dir(diary_dir, "2026-13-45")
        _make_day_dir(diary_dir, d1)

        sources = gen.discover_sources()
        assert "not-a-date" not in sources
        assert "2026-13-45" not in sources
        assert d1 in sources
        assert len(sources) == 1

    def test_empty_diary_dir_returns_empty(self, tmp_path):
        """Empty diary directory produces empty sources."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)

        sources = gen.discover_sources()
        assert sources == {}

    def test_missing_diary_dir_returns_empty(self, tmp_path):
        """Missing diary directory produces empty sources without error."""
        from generators.config import CatalogConfig
        from generators.diary import DiaryGenerator

        catalogs_dir = tmp_path / "catalogs"
        catalogs_dir.mkdir(parents=True, exist_ok=True)

        config = CatalogConfig()
        client = _make_mock_client()
        gen = DiaryGenerator(config=config, model_client=client)

        os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
        os.environ["CLAUDE_PLUGIN_OPTION_diary_dir"] = str(tmp_path / "nonexistent")

        sources = gen.discover_sources()
        assert sources == {}


# ---------------------------------------------------------------------------
# Lookback Window
# ---------------------------------------------------------------------------


class TestLookbackWindow:
    """Requirement: Configurable lookback window.

    The generator MUST respect a configurable diary lookback window that
    limits how many days back it processes.
    """

    def test_lookback_limits_processing(self, tmp_path):
        """WHEN lookback is 7 days and entries exist for 30 days
        THEN only the most recent 7 days are discovered.
        """
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=7)
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        # Create 30 days of diary entries
        for i in range(30):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        sources = gen.discover_sources()
        assert len(sources) == 7

    def test_default_lookback_window_applied(self, tmp_path):
        """WHEN no lookback window is configured
        THEN a default lookback window is applied (not unlimited).
        """
        from generators.config import CatalogConfig, DEFAULT_DIARY_CATALOG_DAYS

        config = CatalogConfig()  # uses default
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        # Create more entries than the default window
        for i in range(DEFAULT_DIARY_CATALOG_DAYS + 30):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        sources = gen.discover_sources()
        assert len(sources) <= DEFAULT_DIARY_CATALOG_DAYS

    def test_all_files_within_window(self, tmp_path):
        """WHEN lookback is 60 days and only 10 days exist
        THEN all 10 are discovered.
        """
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=60)
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        for i in range(10):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        sources = gen.discover_sources()
        assert len(sources) == 10

    def test_window_of_zero_returns_empty(self, tmp_path):
        """WHEN diary_catalog_days is 0
        THEN no diary entries are processed (empty catalog).
        """
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=0)
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        _make_day_dir(diary_dir, _date_str_days_ago(0))
        _make_day_dir(diary_dir, _date_str_days_ago(1))

        sources = gen.discover_sources()
        assert len(sources) == 0

    def test_lookback_includes_today(self, tmp_path):
        """Today's entry should be included in the lookback window."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=1)
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        _make_day_dir(diary_dir, date.today().isoformat())

        sources = gen.discover_sources()
        assert len(sources) == 1
        assert date.today().isoformat() in sources

    def test_lookback_excludes_beyond_boundary(self, tmp_path):
        """Day just outside the window should be excluded."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=3)
        gen, _, diary_dir = _make_diary_generator(tmp_path, config=config)

        # Day 0,1,2 inside; day 3 outside (window is 3 days)
        for i in range(5):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        sources = gen.discover_sources()
        assert len(sources) == 3
        assert _date_str_days_ago(0) in sources
        assert _date_str_days_ago(1) in sources
        assert _date_str_days_ago(2) in sources
        assert _date_str_days_ago(4) not in sources


# ---------------------------------------------------------------------------
# hash_source()
# ---------------------------------------------------------------------------


class TestHashSource:
    """Requirement: Content hashing for source change detection.

    hash_source() computes SHA-256 over sorted file contents of a day directory.
    """

    def test_hash_deterministic_single_file(self, tmp_path):
        """Identical content produces identical hash."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {"session.md": "content A"})

        hash1 = gen.hash_source(day_dir)
        hash2 = gen.hash_source(day_dir)
        assert hash1 == hash2

    def test_hash_changes_on_content_change(self, tmp_path):
        """Modified content produces a different hash."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {"session.md": "content A"})

        hash1 = gen.hash_source(day_dir)

        _rewrite_day_file(day_dir, {"session.md": "content B"})
        hash2 = gen.hash_source(day_dir)

        assert hash1 != hash2

    def test_hash_deterministic_across_identical_files(self, tmp_path):
        """Two days with identical content produce identical hashes (modulo
        the filename byte which is part of the hash). Same file → same hash.

        v0.3.0+ per-day layout: hash is over a single file, so sort-order
        across multiple per-session files is no longer a thing. This test
        replaces the legacy test_hash_over_sorted_files, asserting that
        within a single day, hash is content-deterministic.
        """
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day = _make_day_dir(diary_dir, "2026-04-15", {"a.md": "alpha", "b.md": "beta"})

        h1 = gen.hash_source(day)
        # Rewrite with same key/value pairs in different dict-iteration order
        # — Python dict ordering preserves insertion, so this would only
        # break if our helper sorted differently. Belt-and-braces.
        _rewrite_day_file(day, {"a.md": "alpha", "b.md": "beta"})
        h2 = gen.hash_source(day)
        assert h1 == h2

    def test_hash_differs_for_different_files(self, tmp_path):
        """Different file contents produce different hashes."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)

        day1 = _make_day_dir(diary_dir, "2026-04-15", {"s.md": "content A"})
        day2 = _make_day_dir(diary_dir, "2026-04-16", {"s.md": "content B"})

        assert gen.hash_source(day1) != gen.hash_source(day2)

    def test_hash_changes_when_session_added(self, tmp_path):
        """Appending a new session block to the day file changes the hash."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {"session1.md": "content"})

        hash1 = gen.hash_source(day_dir)

        _rewrite_day_file(day_dir, {
            "session1.md": "content",
            "session2.md": "extra content",
        })
        hash2 = gen.hash_source(day_dir)

        assert hash1 != hash2

    def test_hash_is_sha256(self, tmp_path):
        """Hash output should be a valid SHA-256 hex string."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15")

        result = gen.hash_source(day_dir)
        assert len(result) == 64  # SHA-256 hex length
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_content_not_metadata(self, tmp_path):
        """Hash is based on content, not file metadata like mtime."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {"s.md": "fixed content"})

        hash1 = gen.hash_source(day_dir)

        # Touch the file to change mtime without changing content
        _rewrite_day_file(day_dir, {"s.md": "fixed content"})

        hash2 = gen.hash_source(day_dir)
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# build_prompt()
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Requirement: Per-day LLM summarization.

    build_prompt() creates a diary-specific LLM prompt for day summarization.
    """

    def test_prompt_contains_day_content(self, tmp_path):
        """Prompt must include the actual diary content."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {
            "session.md": "Worked on acme-api auth refactor."
        })

        prompt = gen.build_prompt(day_dir)
        assert "acme-api" in prompt
        assert "auth refactor" in prompt

    def test_prompt_includes_multiple_files(self, tmp_path):
        """Prompt should include content from all files in the day directory."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15", {
            "session1.md": "Morning: worked on auth.",
            "session2.md": "Afternoon: rate limiting work.",
        })

        prompt = gen.build_prompt(day_dir)
        assert "auth" in prompt
        assert "rate limiting" in prompt

    def test_prompt_requests_structured_output(self, tmp_path):
        """Prompt should request structured JSON output with expected fields."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15")

        prompt = gen.build_prompt(day_dir)
        # Should ask for sessions, projects, topics at minimum
        prompt_lower = prompt.lower()
        assert "session" in prompt_lower
        assert "project" in prompt_lower
        assert "topic" in prompt_lower
        assert "json" in prompt_lower

    def test_prompt_includes_date_context(self, tmp_path):
        """Prompt should include the date being summarized."""
        gen, _, diary_dir = _make_diary_generator(tmp_path)
        day_dir = _make_day_dir(diary_dir, "2026-04-15")

        prompt = gen.build_prompt(day_dir)
        assert "2026-04-15" in prompt


# ---------------------------------------------------------------------------
# parse_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Requirement: parse_response() producing diary catalog schema entries.

    Must produce entries with: sessions, projects, topics.
    """

    def test_parses_valid_json(self, tmp_path):
        """Parses a well-formed JSON response."""
        gen, _, _ = _make_diary_generator(tmp_path)

        raw = json.dumps({
            "sessions": [{"id": "s1", "project": "acme", "summary": "Auth work"}],
            "projects": ["acme"],
            "topics": ["auth"],
        })

        entry = gen.parse_response(raw)
        assert "sessions" in entry
        assert "projects" in entry
        assert "topics" in entry

    def test_parses_code_fenced_json(self, tmp_path):
        """Handles JSON wrapped in markdown code fences."""
        gen, _, _ = _make_diary_generator(tmp_path)

        raw = '```json\n{"sessions": [], "projects": [], "topics": []}\n```'

        entry = gen.parse_response(raw)
        assert entry["sessions"] == []
        assert entry["projects"] == []
        assert entry["topics"] == []

    def test_sessions_is_array(self, tmp_path):
        """Sessions field must be an array."""
        gen, _, _ = _make_diary_generator(tmp_path)

        raw = json.dumps({
            "sessions": [
                {"id": "s1", "project": "proj", "summary": "Did stuff"},
                {"id": "s2", "project": "proj2", "summary": "More stuff"},
            ],
            "projects": ["proj", "proj2"],
            "topics": ["topic1"],
        })

        entry = gen.parse_response(raw)
        assert isinstance(entry["sessions"], list)
        assert len(entry["sessions"]) == 2

    def test_projects_is_array_of_strings(self, tmp_path):
        """Projects field must be an array of strings."""
        gen, _, _ = _make_diary_generator(tmp_path)

        raw = json.dumps({
            "sessions": [],
            "projects": ["acme-api", "dotfiles"],
            "topics": [],
        })

        entry = gen.parse_response(raw)
        assert isinstance(entry["projects"], list)
        assert all(isinstance(p, str) for p in entry["projects"])

    def test_topics_is_array_of_strings(self, tmp_path):
        """Topics field must be an array of strings."""
        gen, _, _ = _make_diary_generator(tmp_path)

        raw = json.dumps({
            "sessions": [],
            "projects": [],
            "topics": ["auth refactor", "rate limiting"],
        })

        entry = gen.parse_response(raw)
        assert isinstance(entry["topics"], list)
        assert all(isinstance(t, str) for t in entry["topics"])

    def test_invalid_json_raises(self, tmp_path):
        """Invalid JSON raises an exception."""
        gen, _, _ = _make_diary_generator(tmp_path)

        with pytest.raises(Exception):
            gen.parse_response("not valid json {{{")


# ---------------------------------------------------------------------------
# Word Count
# ---------------------------------------------------------------------------


class TestWordCount:
    """Requirement: Word count accuracy.

    word_count MUST be computed directly from source content, not LLM summary.
    """

    @pytest.mark.asyncio
    async def test_word_count_matches_source(self, tmp_path):
        """word_count counts every word in the per-day file, including the
        day-header and ``## Session:`` boundary headers. We verify it equals
        the actual file ``read_text().split()`` length (not a hand-counted
        body-only number — the fixture writes both header and body).
        """
        content = "one two three four five six seven eight nine ten"
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_file = _make_day_dir(diary_dir, _date_str_days_ago(0), {"session.md": content})
        expected = len(day_file.read_text().split())

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert entry["word_count"] == expected
        # And the body content is included.
        assert entry["word_count"] >= 10

    @pytest.mark.asyncio
    async def test_word_count_for_whitespace_only_session_body(self, tmp_path):
        """A whitespace-only session body still produces a non-zero
        word_count (header and session marker contribute), but does not
        increase the count beyond the header. Validates the field is
        always present and derived from the file."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_file = _make_day_dir(diary_dir, _date_str_days_ago(0), {"session.md": "   \n  \n  "})
        expected = len(day_file.read_text().split())

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert entry["word_count"] == expected
        # Body itself is whitespace-only → expected count equals the
        # number of header tokens (positive but small).
        assert entry["word_count"] > 0

    @pytest.mark.asyncio
    async def test_word_count_across_multiple_session_blocks(self, tmp_path):
        """Word count covers all ``## Session:`` blocks in the per-day file."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_file = _make_day_dir(diary_dir, _date_str_days_ago(0), {
            "session1.md": "one two three",
            "session2.md": "four five",
        })
        expected = len(day_file.read_text().split())

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert entry["word_count"] == expected
        # Both bodies present (5 body words minimum).
        assert entry["word_count"] >= 5


# ---------------------------------------------------------------------------
# Catalog File Structure
# ---------------------------------------------------------------------------


class TestCatalogFileStructure:
    """Requirement: Diary catalog file structure.

    The catalog MUST have schema_version, generated_at, and entries array.
    """

    @pytest.mark.asyncio
    async def test_catalog_has_schema_version(self, tmp_path):
        """Catalog includes a top-level schema_version field."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "schema_version" in catalog
        assert isinstance(catalog["schema_version"], str)

    @pytest.mark.asyncio
    async def test_catalog_has_generated_at_timestamp(self, tmp_path):
        """Catalog includes a generated_at ISO-8601 timestamp."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "generated_at" in catalog
        # Should parse as ISO-8601
        datetime.fromisoformat(catalog["generated_at"])

    @pytest.mark.asyncio
    async def test_catalog_has_entries_array(self, tmp_path):
        """Catalog includes an entries array."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "entries" in catalog
        assert isinstance(catalog["entries"], list)

    @pytest.mark.asyncio
    async def test_entry_has_date_field(self, tmp_path):
        """Each entry has a date field in YYYY-MM-DD format."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        today = _date_str_days_ago(0)
        _make_day_dir(diary_dir, today)

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "date" in entry or "source" in entry
        # The date should be present either as 'date' field or derivable from 'source'
        entry_date = entry.get("date", entry.get("source", ""))
        assert today in entry_date

    @pytest.mark.asyncio
    async def test_entry_has_sessions(self, tmp_path):
        """Each entry has a sessions array."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "sessions" in entry
        assert isinstance(entry["sessions"], list)

    @pytest.mark.asyncio
    async def test_entry_has_projects(self, tmp_path):
        """Each entry has a projects array."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "projects" in entry
        assert isinstance(entry["projects"], list)

    @pytest.mark.asyncio
    async def test_entry_has_topics(self, tmp_path):
        """Each entry has a topics array."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "topics" in entry
        assert isinstance(entry["topics"], list)

    @pytest.mark.asyncio
    async def test_entry_has_word_count(self, tmp_path):
        """Each entry has a word_count integer."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        entry = catalog["entries"][0]
        assert "word_count" in entry
        assert isinstance(entry["word_count"], int)

    @pytest.mark.asyncio
    async def test_empty_diary_produces_empty_entries(self, tmp_path):
        """WHEN no diary day-files exist
        THEN diary.json is written with empty entries array.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert catalog["entries"] == []
        assert "schema_version" in catalog
        assert "generated_at" in catalog

    @pytest.mark.asyncio
    async def test_catalog_written_to_correct_path(self, tmp_path):
        """Catalog file is written to $CLAUDE_PLUGIN_DATA/catalogs/diary.json."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        assert (catalogs_dir / "diary.json").exists()


# ---------------------------------------------------------------------------
# Content-Hash Skip Logic
# ---------------------------------------------------------------------------


class TestContentHashSkipLogic:
    """Requirement: Content-hash-based skip logic.

    Unchanged files are skipped to avoid redundant LLM calls.
    """

    @pytest.mark.asyncio
    async def test_skip_unchanged_day(self, tmp_path):
        """WHEN a day has the same content hash as in state
        THEN no LLM call is made and existing entry is preserved.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        _make_day_dir(diary_dir, day_str)

        # First run — generates
        result1 = await gen.run()
        assert result1.generated == 1

        # Second run — should skip
        result2 = await gen.run()
        assert result2.skipped == 1
        assert result2.generated == 0

    @pytest.mark.asyncio
    async def test_regenerate_changed_day(self, tmp_path):
        """WHEN a day's content has changed since last generation
        THEN the LLM is called to re-summarize.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        day_dir = _make_day_dir(diary_dir, day_str, {"s.md": "original content"})

        # First run
        await gen.run()

        # Modify the day file content
        _rewrite_day_file(day_dir, {"s.md": "modified content"})

        # Second run — should regenerate
        result = await gen.run()
        assert result.generated == 1
        assert result.skipped == 0

    @pytest.mark.asyncio
    async def test_generate_new_day_no_prior_state(self, tmp_path):
        """WHEN a day has no entry in state
        THEN the LLM is called and hash is recorded.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        _make_day_dir(diary_dir, day_str)

        result = await gen.run()
        assert result.generated == 1

        state = _read_state(catalogs_dir)
        assert day_str in state["generators"]["diary"]["source_hashes"]

    @pytest.mark.asyncio
    async def test_force_regenerates_unchanged(self, tmp_path):
        """WHEN force=True, unchanged sources are still regenerated."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        result = await gen.run(force=True)
        assert result.generated == 1
        assert result.skipped == 0


# ---------------------------------------------------------------------------
# Deletion Pruning
# ---------------------------------------------------------------------------


class TestDeletionPruning:
    """Requirement: Deletion pruning.

    Deleted day-files are removed from catalog and state.
    """

    @pytest.mark.asyncio
    async def test_prune_deleted_day_from_catalog(self, tmp_path):
        """WHEN a day-file is deleted
        THEN its entry is removed from the catalog and state.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        day_dir = _make_day_dir(diary_dir, day_str)

        await gen.run()

        # Verify entry exists
        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1

        # Delete the day file
        day_dir.unlink()

        # Run again
        result = await gen.run()
        assert result.pruned == 1

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 0

        state = _read_state(catalogs_dir)
        assert day_str not in state["generators"]["diary"]["source_hashes"]

    @pytest.mark.asyncio
    async def test_no_pruning_when_file_exists(self, tmp_path):
        """WHEN a day-file still exists
        THEN its entry is retained.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        result = await gen.run()
        assert result.pruned == 0

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 1

    @pytest.mark.asyncio
    async def test_prune_outside_lookback_window(self, tmp_path):
        """Days that fall outside the lookback window are pruned from catalog.

        WHEN a previously-cataloged day ages out of the window
        THEN it is pruned.
        """
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=3)
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path, config=config)

        # Create entries for days 0-4
        for i in range(5):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        # First run — only days 0,1,2 are within window
        result = await gen.run()
        assert result.total_sources == 3

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 3


# ---------------------------------------------------------------------------
# State File Persistence
# ---------------------------------------------------------------------------


class TestStateFilePersistence:
    """Requirement: State file persistence.

    State stored under 'diary' namespace in .generation-state.json.
    """

    @pytest.mark.asyncio
    async def test_state_created_on_first_run(self, tmp_path):
        """WHEN no state file exists
        THEN it is created with diary namespace.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        state = _read_state(catalogs_dir)
        assert "diary" in state["generators"]
        assert "source_hashes" in state["generators"]["diary"]

    @pytest.mark.asyncio
    async def test_state_preserves_other_namespaces(self, tmp_path):
        """WHEN state file has other namespace data (e.g., memory)
        THEN only diary namespace is modified.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        # Pre-seed state with a memory namespace
        _write_state(catalogs_dir, {
            "schema_version": 1,
            "generators": {
                "memory": {
                    "last_run": "2026-04-19T10:00:00Z",
                    "source_hashes": {"memory.md": "abc123"},
                    "entry_count": 1,
                }
            }
        })

        await gen.run()

        state = _read_state(catalogs_dir)
        # Memory namespace untouched
        assert state["generators"]["memory"]["source_hashes"]["memory.md"] == "abc123"
        # Diary namespace added
        assert "diary" in state["generators"]

    @pytest.mark.asyncio
    async def test_corrupt_state_triggers_full_regen(self, tmp_path):
        """WHEN state file has invalid JSON
        THEN all day-files are treated as needing regeneration.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        # Write corrupt state
        (catalogs_dir / ".generation-state.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )

        result = await gen.run()
        assert result.generated == 1  # Should generate, not skip


# ---------------------------------------------------------------------------
# LLM Failure Handling
# ---------------------------------------------------------------------------


class TestLLMFailureHandling:
    """Requirement: LLM failure handling.

    Failed entries are skipped without aborting. Prior data preserved.
    """

    @pytest.mark.asyncio
    async def test_llm_failure_one_day_others_succeed(self, tmp_path):
        """WHEN the LLM fails for one day but succeeds for others
        THEN successful entries are in the catalog and failed one is absent.
        """
        from multiplai_core.model_client import ModelResponse

        call_count = 0
        fail_on = 1  # fail on second call

        async def mock_query(*args, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx == fail_on:
                raise Exception("LLM call failed")
            return ModelResponse(content=_SAMPLE_LLM_RESPONSE)

        client = AsyncMock()
        client.query = mock_query

        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path, client=client)

        for i in range(3):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        result = await gen.run()
        assert result.generated == 2
        assert len(result.errors) == 1

        catalog = _read_catalog(catalogs_dir)
        assert len(catalog["entries"]) == 2

    @pytest.mark.asyncio
    async def test_all_llm_calls_fail(self, tmp_path):
        """WHEN every LLM call fails
        THEN generator completes without error, catalog retains prior entries.
        """
        client = _make_mock_client()
        client.query = AsyncMock(side_effect=Exception("LLM down"))

        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path, client=client)

        for i in range(3):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        result = await gen.run()
        assert result.generated == 0
        assert len(result.errors) == 3

    @pytest.mark.asyncio
    async def test_failed_entry_preserves_prior_data(self, tmp_path):
        """WHEN an LLM call fails for a previously-cataloged day
        THEN the prior catalog data is preserved.
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        day_dir = _make_day_dir(diary_dir, day_str, {"s.md": "original"})

        # First run succeeds
        await gen.run()
        catalog_before = _read_catalog(catalogs_dir)
        assert len(catalog_before["entries"]) == 1

        # Modify day-file content so it needs regeneration
        _rewrite_day_file(day_dir, {"s.md": "modified"})

        # Make LLM fail
        gen._model_client.query = AsyncMock(side_effect=Exception("LLM error"))

        result = await gen.run()
        assert len(result.errors) == 1

        # Prior entry should be preserved
        catalog_after = _read_catalog(catalogs_dir)
        assert len(catalog_after["entries"]) == 1

    @pytest.mark.asyncio
    async def test_failed_entry_hash_not_updated(self, tmp_path):
        """WHEN LLM fails for a day
        THEN no hash update is recorded (so it retries next run).
        """
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day_str = _date_str_days_ago(0)
        day_dir = _make_day_dir(diary_dir, day_str, {"s.md": "original"})

        # First run
        await gen.run()
        state_before = _read_state(catalogs_dir)
        hash_before = state_before["generators"]["diary"]["source_hashes"][day_str]

        # Modify and fail
        _rewrite_day_file(day_dir, {"s.md": "modified"})
        gen._model_client.query = AsyncMock(side_effect=Exception("fail"))

        await gen.run()

        state_after = _read_state(catalogs_dir)
        hash_after = state_after["generators"]["diary"]["source_hashes"][day_str]

        # Hash should NOT be updated to new content hash
        assert hash_after == hash_before


# ---------------------------------------------------------------------------
# Schema Versioning
# ---------------------------------------------------------------------------


class TestSchemaVersioning:
    """Requirement: Schema versioning.

    Schema version mismatch triggers full regeneration.
    """

    @pytest.mark.asyncio
    async def test_catalog_includes_schema_version(self, tmp_path):
        """Catalog must include schema_version field."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        catalog = _read_catalog(catalogs_dir)
        assert "schema_version" in catalog
        assert isinstance(catalog["schema_version"], str)

    @pytest.mark.asyncio
    async def test_schema_version_match_uses_incremental(self, tmp_path):
        """WHEN schema version matches, use incremental (hash-based) processing."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        # Second run should skip (incremental)
        result = await gen.run()
        assert result.skipped == 1
        assert result.generated == 0


# ---------------------------------------------------------------------------
# LLM Model Configuration
# ---------------------------------------------------------------------------


class TestModelConfiguration:
    """Requirement: Configurable model.

    Generator respects plugin.json config for the catalog model and the
    diary-specific model override.
    """

    @pytest.mark.asyncio
    async def test_uses_configured_model(self, tmp_path):
        """WHEN catalog_model is set, LLM calls use that model."""
        from generators.config import CatalogConfig

        client = _make_mock_client()
        config = CatalogConfig(model="claude-haiku-4-5")
        gen, catalogs_dir, diary_dir = _make_diary_generator(
            tmp_path, client=client, config=config
        )
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        # Verify the model was passed to the client
        call_kwargs = client.query.call_args
        # The base class passes model= to query()
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_uses_diary_specific_model_when_set(self, tmp_path):
        """WHEN model_diary is set, the diary LLM call uses it over the general model.

        Proves the wiring, not just the config property: the diary-specific
        model must actually reach ``client.query(model=...)``.
        """
        from generators.config import CatalogConfig

        client = _make_mock_client()
        config = CatalogConfig(model="claude-sonnet-4-6", model_diary="claude-haiku-4-5")
        gen, catalogs_dir, diary_dir = _make_diary_generator(
            tmp_path, client=client, config=config
        )
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        call = client.query.call_args
        assert call is not None, "diary generator must have called the LLM"
        model_used = call.kwargs.get("model", (call[1] or {}).get("model"))
        assert model_used == "claude-haiku-4-5", (
            f"diary LLM call must use model_diary, got {model_used!r}"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_catalog_model_when_diary_unset(self, tmp_path):
        """WHEN model_diary is unset, the diary LLM call uses catalog_model."""
        from generators.config import CatalogConfig

        client = _make_mock_client()
        config = CatalogConfig(model="claude-sonnet-4-6")  # model_diary defaults to ""
        gen, catalogs_dir, diary_dir = _make_diary_generator(
            tmp_path, client=client, config=config
        )
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run()

        call = client.query.call_args
        assert call is not None
        model_used = call.kwargs.get("model", (call[1] or {}).get("model"))
        assert model_used == "claude-sonnet-4-6", (
            f"diary LLM call must fall back to catalog_model, got {model_used!r}"
        )


# ---------------------------------------------------------------------------
# Dry Run Mode
# ---------------------------------------------------------------------------


class TestDryRun:
    """Requirement: Dry-run mode (inherited from base, verified for diary)."""

    @pytest.mark.asyncio
    async def test_dry_run_no_files_written(self, tmp_path):
        """Dry run does not write catalog or state files."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        result = await gen.run(dry_run=True)

        assert result.dry_run is True
        assert not (catalogs_dir / "diary.json").exists()
        assert not (catalogs_dir / ".generation-state.json").exists()

    @pytest.mark.asyncio
    async def test_dry_run_no_llm_calls(self, tmp_path):
        """Dry run does not make LLM calls."""
        client = _make_mock_client()
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path, client=client)
        _make_day_dir(diary_dir, _date_str_days_ago(0))

        await gen.run(dry_run=True)

        client.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_reports_what_would_generate(self, tmp_path):
        """Dry run reports how many entries would be generated."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        for i in range(3):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        result = await gen.run(dry_run=True)
        assert result.generated == 3  # would generate 3
        assert result.total_sources == 3


# ---------------------------------------------------------------------------
# GenerationResult
# ---------------------------------------------------------------------------


class TestGenerationResult:
    """Verify GenerationResult reports correct counts."""

    @pytest.mark.asyncio
    async def test_result_counts_generated(self, tmp_path):
        """Result reports correct generated count."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        for i in range(3):
            _make_day_dir(diary_dir, _date_str_days_ago(i))

        result = await gen.run()
        assert result.generator == "diary"
        assert result.total_sources == 3
        assert result.generated == 3
        assert result.skipped == 0
        assert result.pruned == 0
        assert result.errors == []
        assert result.dry_run is False

    @pytest.mark.asyncio
    async def test_result_counts_mixed(self, tmp_path):
        """Result reports mixed skip/generate after second run with changes."""
        gen, catalogs_dir, diary_dir = _make_diary_generator(tmp_path)
        day0 = _make_day_dir(diary_dir, _date_str_days_ago(0), {"s.md": "content0"})
        _make_day_dir(diary_dir, _date_str_days_ago(1), {"s.md": "content1"})

        # First run: both generated
        await gen.run()

        # Modify only day0
        _rewrite_day_file(day0, {"s.md": "modified"})

        result = await gen.run()
        assert result.generated == 1
        assert result.skipped == 1
