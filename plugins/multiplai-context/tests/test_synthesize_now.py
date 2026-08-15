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
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR", str(ws))
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
        from multiplai_core.paths import get_paths

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

        from multiplai_core.paths import get_paths

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

        from multiplai_core.paths import get_paths

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

        from multiplai_core.paths import get_paths

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


class TestWindowAttribution:
    """Criterion 10: the file says which window wrote it.

    There is one ``now/<project>.md`` per project and several windows can be
    open on the same project, so last writer wins. On 2026-08-08 a
    ``multiplai-docker`` window's summary was handed to a freshly-cleared
    pi-eval window, which read it as its own prior work — both sessions are
    legitimately "DolceBot" and nothing in the file said otherwise.

    Full per-session merging is deliberately out of scope. Attribution alone
    makes the contamination visible, which is what the incident needed.
    """

    def test_the_header_names_the_session_that_wrote_it(
        self, workspace, _no_llm, monkeypatch
    ):
        import asyncio

        from multiplai_core.paths import get_paths
        import synthesize_now

        _write_diary(workspace, [("sess-writer", "/work/PROJECTS/foo", "Did a thing.")])
        (workspace / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )
        monkeypatch.setenv("HOSTNAME", "claude-work-04221854")

        asyncio.run(
            synthesize_now.synthesize(project_filter="foo", session_id="sess-writer")
        )

        content = (get_paths().now_dir() / "foo.md").read_text()
        assert "Written by session: sess-writer" in content
        assert "Written on host: claude-work-04221854" in content

    def test_an_unattributed_rebuild_still_writes_a_clean_header(
        self, workspace, _no_llm
    ):
        """`/multiplai-context:now` and the backfill rebuild every project at
        once and belong to no single session — no fabricated id."""
        import asyncio

        from multiplai_core.paths import get_paths
        import synthesize_now

        _write_diary(workspace, [("sess-a", "/work/PROJECTS/foo", "Did a thing.")])
        (workspace / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )

        asyncio.run(synthesize_now.synthesize(project_filter="foo"))

        content = (get_paths().now_dir() / "foo.md").read_text()
        assert "Written by session:" not in content
        assert content.startswith("# Project Status: foo")

    def test_a_slice_marker_never_leaks_into_the_summary(
        self, workspace, _no_llm
    ):
        """The diary's append-idempotency marker is a comment for machines,
        not content for a status summary."""
        import asyncio

        from multiplai_core.paths import get_paths
        import synthesize_now

        _write_diary(
            workspace,
            [("sess-a", "/work/PROJECTS/foo",
              "<!-- slice: sess-a:start -->\n\nReal work happened.")],
        )
        (workspace / ".multiplai" / "project-map.yaml").write_text(
            "project_roots:\n  - /work/PROJECTS\n"
        )

        asyncio.run(synthesize_now.synthesize(project_filter="foo"))

        content = (get_paths().now_dir() / "foo.md").read_text()
        assert "slice:" not in content
        assert "Real work happened." in content


class TestSummaryThinking:
    """The status-summary call is mechanical: it carries the thinking config
    resolved from ``now_thinking`` (default: disabled — see lib/thinking.py)."""

    def _run(self, monkeypatch, *, supported):
        import synthesize_now
        import lib.thinking as th
        from multiplai_core.plugin_options import option_var

        monkeypatch.setattr(
            th, "core_supports_thinking", lambda target=None: supported
        )
        monkeypatch.delenv(option_var(th.NOW_THINKING_OPTION), raising=False)

        captured = {}

        class _Client:
            async def query(self, **kwargs):
                captured.update(kwargs)

                class _Reply:
                    content = "- did the thing"

                return _Reply()

        entries = [{"content": "[ts]\n\nWorked on the widget.\n"}]
        result = asyncio.run(
            synthesize_now._summarize_project(_Client(), "proj", entries)
        )
        assert result == "- did the thing", "the model path must have been taken"
        return captured

    def test_summary_call_receives_thinking_disabled_by_default(self, monkeypatch):
        assert self._run(monkeypatch, supported=True)["thinking"] == {
            "type": "disabled"
        }

    def test_keyword_omitted_entirely_when_unsupported(self, monkeypatch):
        """Routed through thinking_kwargs, so an unsupported dependency is
        handed no keyword rather than `thinking=None`."""
        assert "thinking" not in self._run(monkeypatch, supported=False)
