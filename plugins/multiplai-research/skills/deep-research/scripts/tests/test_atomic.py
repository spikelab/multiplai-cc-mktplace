"""atomic_write_text: a crash leaves the old file, never a half-written one.

The state checkpoint had this logic inline and the other two persisted
artifacts — the quota file and the report — did not. These tests cover the
shared helper and pin that all three now route through it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from research_pipeline.atomic import atomic_write_text, replacement_mode


class TestAtomicWriteText:
    def test_creates_a_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new.json"
        atomic_write_text(target, '{"a": 1}')
        assert target.read_text() == '{"a": 1}'

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "f.txt"
        atomic_write_text(target, "hi")
        assert target.read_text() == "hi"

    def test_replaces_existing_content(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("old content that is longer")
        atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        for i in range(5):
            atomic_write_text(target, str(i))
        assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]

    def test_a_failed_write_leaves_the_old_file_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point. `write_text` truncates first, so an interrupted
        write destroys the previous content; this must not."""
        target = tmp_path / "state.json"
        target.write_text('{"good": true}')

        real_replace = os.replace

        def boom(src, dst):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, '{"new": true}')
        monkeypatch.setattr(os, "replace", real_replace)

        assert target.read_text() == '{"good": true}'
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"], (
            "the temp file survived the failure"
        )

    def test_preserves_the_existing_files_mode(self, tmp_path: Path) -> None:
        """mkstemp creates 0600 and os.replace carries the temp file's mode
        onto the destination, so without replacement_mode a rewrite silently
        makes a shared file owner-only."""
        target = tmp_path / "f.txt"
        target.write_text("old")
        os.chmod(target, 0o644)
        atomic_write_text(target, "new")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_new_file_is_not_owner_only(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        atomic_write_text(target, "x")
        assert stat.S_IMODE(target.stat().st_mode) == replacement_mode(
            tmp_path / "does-not-exist"
        )

    def test_round_trips_non_ascii(self, tmp_path: Path) -> None:
        target = tmp_path / "f.md"
        atomic_write_text(target, "città — naïve — 日本語")
        assert target.read_text(encoding="utf-8") == "città — naïve — 日本語"


class TestPersistedArtifactsUseIt:
    """Three files outlive the process that writes them. All three must be
    written atomically, not just the one that happened to get the treatment."""

    def test_state_checkpoint_is_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline.state import ResearchState

        state_file = tmp_path / "s.json"
        state = ResearchState.new(
            query="q", output_file=tmp_path / "out.md", state_file=state_file
        )
        state.checkpoint()
        good = state_file.read_text()

        def boom(src, dst):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        state.query = "a much much longer query than the first one"
        with pytest.raises(OSError):
            state.checkpoint()

        assert state_file.read_text() == good

    def test_quota_flush_is_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline.search_router import QuotaStore

        path = tmp_path / "quotas.json"
        store = QuotaStore(path)
        store._dirty = True
        store.flush()
        good = path.read_text()

        def boom(src, dst):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        store._dirty = True
        with pytest.raises(OSError):
            store.flush()

        assert path.read_text() == good

    def test_pipeline_writes_the_report_atomically(self) -> None:
        """The report is the artifact the whole run exists to produce; a
        truncated one is a run's worth of API spend lost."""
        import inspect

        import research_pipeline.pipeline as pipeline_mod

        src = inspect.getsource(pipeline_mod)
        assert "Path(state.output_file).write_text(" not in src
        assert "atomic_write_text(Path(state.output_file)" in src
