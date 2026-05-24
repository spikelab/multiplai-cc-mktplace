"""Tests for synthesize_now project grouping + scoped refresh.

Uses an extractive (no-LLM) path by forcing create_client to fail, so the
tests are hermetic and fast. Diary timestamps are generated relative to the
real wall clock so they always fall inside the 48h lookback window.
"""

import asyncio
from datetime import datetime, timezone

import pytest


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_diary(ws, sessions):
    """Write a per-day diary file with the given (sid, cwd, body) sessions."""
    diary_dir = ws / ".multiplai" / "diary"
    diary_dir.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    parts = ["# Diary — test\n"]
    for sid, cwd, body in sessions:
        parts.append(f"\n## Session: {sid} — {ts} — {cwd}\n")
        parts.append(f"\n[{ts}]\n\n{body}\n")
    (diary_dir / "today.md").write_text("".join(parts))
    return diary_dir


@pytest.fixture
def workspace(monkeypatch, reset_paths_cache, tmp_path):
    ws = tmp_path / "ws"
    (ws / ".multiplai").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_workspace_dir", str(ws))
    return ws


@pytest.fixture
def _no_llm(monkeypatch):
    """Force the extractive fallback by making the model client unavailable."""
    import synthesize_now

    async def _raise(*a, **k):
        raise RuntimeError("no client in tests")

    monkeypatch.setattr(synthesize_now, "create_client", _raise)


class TestEmptyCwdParsing:
    def test_empty_and_unknown_cwd_are_skipped_no_junk(self, workspace):
        # Empty cwd must NOT swallow the following [timestamp] line as the
        # project name (the \\s-eats-newline regex bug), and 'unknown' is a
        # null placeholder — both are dropped, leaving only the real project.
        _write_diary(
            workspace,
            [
                ("aaa", "", "Empty cwd session."),
                ("bbb", "unknown", "Unknown cwd session."),
                ("ccc", "/work/PROJECTS/foo", "Real project session."),
            ],
        )
        cfg = {"project_roots": ["/work/PROJECTS"]}
        import synthesize_now
        from lib.paths import get_paths

        grouped = synthesize_now._scan_diary(get_paths().diary_dir(), config=cfg)
        assert set(grouped) == {"foo"}

    def test_empty_cwd_does_not_capture_next_line(self, workspace):
        diary_dir = workspace / ".multiplai" / "diary"
        diary_dir.mkdir(parents=True, exist_ok=True)
        ts = _now_iso()
        (diary_dir / "today.md").write_text(
            f"# Diary — test\n\n## Session: aaa — {ts} — \n\n[{ts}]\n\nbody\n"
        )
        import synthesize_now

        blocks = list(synthesize_now._iter_diary_session_blocks(diary_dir / "today.md"))
        assert len(blocks) == 1
        assert blocks[0]["working_dir"] == ""


class TestScanDiaryGrouping:
    def test_groups_by_resolved_project(self, workspace):
        _write_diary(
            workspace,
            [
                ("a", "/work/PROJECTS/foo/sub", "Did foo work."),
                ("b", "/work/PROJECTS/bar", "Did bar work."),
                ("c", "/work", "Cross-project workspace stuff."),
            ],
        )
        cfg = {
            "project_roots": ["/work/PROJECTS"],
            "umbrella_roots": ["/work"],
        }
        import synthesize_now

        from lib.paths import get_paths

        grouped = synthesize_now._scan_diary(get_paths().diary_dir(), config=cfg)
        assert set(grouped) == {"foo", "bar", "workspace"}
        assert len(grouped["foo"]) == 1


class TestScopedSynthesize:
    def test_full_rebuild_writes_all_projects(self, workspace, _no_llm):
        _write_diary(
            workspace,
            [
                ("a", "/work/PROJECTS/foo", "Foo work."),
                ("b", "/work/PROJECTS/bar", "Bar work."),
            ],
        )
        (workspace / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )
        import synthesize_now

        from lib.paths import get_paths

        asyncio.run(synthesize_now.synthesize())
        now_dir = get_paths().now_dir()
        assert (now_dir / "foo.md").exists()
        assert (now_dir / "bar.md").exists()

    def test_scoped_writes_only_named_project(self, workspace, _no_llm):
        _write_diary(
            workspace,
            [
                ("a", "/work/PROJECTS/foo", "Foo work."),
                ("b", "/work/PROJECTS/bar", "Bar work."),
            ],
        )
        (workspace / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )
        import synthesize_now

        from lib.paths import get_paths

        asyncio.run(synthesize_now.synthesize(project_filter="foo"))
        now_dir = get_paths().now_dir()
        assert (now_dir / "foo.md").exists()
        assert not (now_dir / "bar.md").exists()
        content = (now_dir / "foo.md").read_text()
        assert content.startswith("# Project Status: foo")


class TestArgParsing:
    def test_parse_project_flag_space(self):
        from synthesize_now import _parse_project_arg

        assert _parse_project_arg(["--project", "foo"]) == "foo"

    def test_parse_project_flag_equals(self):
        from synthesize_now import _parse_project_arg

        assert _parse_project_arg(["--project=bar"]) == "bar"

    def test_parse_project_absent(self):
        from synthesize_now import _parse_project_arg

        assert _parse_project_arg([]) is None
