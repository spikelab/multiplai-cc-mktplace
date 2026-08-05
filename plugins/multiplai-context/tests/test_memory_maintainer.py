"""Tests for the proactive memory maintainer.

Two things matter here and they pull in opposite directions. The maintainer
must actually run unattended (or it's just another thing to remember), and it
must never touch `.multiplai/memory/` (or an unattended bug rewrites the
memory nobody was watching). Most of these tests are about the second.
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import memory_maintainer as mm

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    return tmp_path / "maintainer_state.yaml"


class TestGate:
    def test_open_when_no_state(self, state):
        assert mm.gate_open(state, now=NOW)

    def test_closed_just_after_a_run(self, state):
        mm.stamp(state, now=NOW - timedelta(hours=1))
        assert not mm.gate_open(state, now=NOW)

    def test_open_after_24h(self, state):
        mm.stamp(state, now=NOW - timedelta(hours=mm.GATE_HOURS, minutes=1))
        assert mm.gate_open(state, now=NOW)

    def test_boundary_is_inclusive(self, state):
        mm.stamp(state, now=NOW - timedelta(hours=mm.GATE_HOURS))
        assert mm.gate_open(state, now=NOW)

    def test_corrupt_state_opens_the_gate(self, state):
        """A wedged-shut gate means maintenance silently never runs again —
        strictly worse than one redundant pass."""
        state.write_text("last_run: [not, a, timestamp\n", encoding="utf-8")
        assert mm.gate_open(state, now=NOW)

    def test_naive_timestamp_is_treated_as_utc(self, state):
        state.write_text("last_run: '2026-07-26T11:00:00'\n", encoding="utf-8")
        assert not mm.gate_open(state, now=NOW)

    def test_stamp_round_trips(self, state):
        mm.stamp(state, now=NOW)
        assert not mm.gate_open(state, now=NOW)

    def test_stamp_failure_is_not_fatal(self, tmp_path):
        """A read-only data dir must cost one duplicate run, not a crash in an
        unattended process nobody is watching."""
        mm.stamp(tmp_path / "nonexistent-file" / "state.yaml", now=NOW)


class TestLintPass:
    def test_writes_a_report_when_there_are_findings(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "dev.md").write_text(
            "- The current best model is Opus 5 (as of 2026-01, review by 2026-02).\n",
            encoding="utf-8")
        dreams = tmp_path / "dreams"

        result = mm.run_lint(memory, dreams)
        assert result.ran
        report = dreams / "memory-lint-latest.md"
        assert report.is_file()
        assert "Expired" in report.read_text(encoding="utf-8")

    def test_clean_tree_writes_nothing(self, tmp_path):
        """No findings means no file. A report that says "clean" every day is
        a file people stop opening."""
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "dev.md").write_text("- Python is used here.\n", encoding="utf-8")
        dreams = tmp_path / "dreams"

        assert mm.run_lint(memory, dreams).ran
        assert not (dreams / "memory-lint-latest.md").exists()

    def test_dry_run_writes_nothing(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "dev.md").write_text(
            "- The current price is EUR 20/mo (as of 2026-01, review by 2026-02).\n",
            encoding="utf-8")
        dreams = tmp_path / "dreams"

        assert mm.run_lint(memory, dreams, dry_run=True).ran
        assert not dreams.exists()

    def test_a_broken_linter_does_not_abort_the_pass(self, tmp_path, monkeypatch):
        memory = tmp_path / "memory"
        memory.mkdir()
        import lib.memory_lint as ml
        monkeypatch.setattr(ml, "lint_dir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        result = mm.run_lint(memory, tmp_path / "dreams")
        assert not result.ran and "error" in result.detail


class TestDreamPass:
    def _spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mm, "run_supervised",
                            lambda cmd, **kw: calls.append(cmd) or
                            subprocess.CompletedProcess(cmd, 0, "", ""))
        return calls

    def test_skipped_when_the_dream_gate_is_closed(self, tmp_path, monkeypatch):
        calls = self._spy(monkeypatch)
        dream_state = tmp_path / "dream_state.yaml"
        dream_state.write_text(
            f"last_run: '{datetime.now(timezone.utc).isoformat()}'\n", encoding="utf-8")
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- something\n", encoding="utf-8")

        result = mm.run_dream(tmp_path, dream_state, learnings, tmp_path / "dreams")
        assert not result.ran and "gate closed" in result.detail
        assert calls == []

    def test_skipped_when_there_is_no_backlog(self, tmp_path, monkeypatch):
        calls = self._spy(monkeypatch)
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        result = mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings, tmp_path / "dreams")
        assert not result.ran and "no pending" in result.detail
        assert calls == []

    def test_runs_dream_without_auto(self, tmp_path, monkeypatch):
        """The hard constraint: `/dream-remember` stays the only path that
        writes memory. An unattended `--auto` would quietly become a second."""
        calls = self._spy(monkeypatch)
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- something\n", encoding="utf-8")

        result = mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings, tmp_path / "dreams")
        assert result.ran
        assert len(calls) == 1
        assert "--auto" not in calls[0] and "--run" not in calls[0]
        assert calls[0][-1].endswith("dream.py")

    def test_nonzero_exit_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm, "run_supervised",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "nope"))
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- x\n", encoding="utf-8")
        result = mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings, tmp_path / "dreams")
        assert not result.ran and "exit 1" in result.detail

    def test_skipped_while_a_proposal_is_still_awaiting_review(self, tmp_path, monkeypatch):
        """Generating doesn't stamp the dream gate, so without this check an
        unconsolidated week costs seven proposals and seven pairs of model
        calls, and /dream-remember is handed a pile to choose between."""
        calls = self._spy(monkeypatch)
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- something\n", encoding="utf-8")
        dreams = tmp_path / "dreams"
        dreams.mkdir()
        (dreams / "processed-learnings-2026-07-20.md").write_text("x", encoding="utf-8")

        result = mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings, dreams)
        assert not result.ran
        assert "awaiting review" in result.detail
        assert "processed-learnings-2026-07-20.md" in result.detail
        assert calls == []

    def test_an_archived_proposal_does_not_block_the_pass(self, tmp_path, monkeypatch):
        """Archived means dealt with. Only the top level is a pending queue."""
        calls = self._spy(monkeypatch)
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- something\n", encoding="utf-8")
        dreams = tmp_path / "dreams"
        (dreams / "applied").mkdir(parents=True)
        (dreams / "applied" / "processed-learnings-2026-07-20.md").write_text(
            "x", encoding="utf-8")

        result = mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings, dreams)
        assert result.ran
        assert len(calls) == 1

    def test_pending_proposals_are_oldest_first(self, tmp_path):
        dreams = tmp_path / "dreams"
        dreams.mkdir()
        for day in ("2026-07-24", "2026-07-20", "2026-07-22"):
            (dreams / f"processed-learnings-{day}.md").write_text("x", encoding="utf-8")
        (dreams / "memory-lint-latest.md").write_text("x", encoding="utf-8")

        names = [p.name for p in mm.pending_proposals(dreams)]
        assert names == [
            "processed-learnings-2026-07-20.md",
            "processed-learnings-2026-07-22.md",
            "processed-learnings-2026-07-24.md",
        ]

    def test_missing_dreams_dir_is_not_a_pending_queue(self, tmp_path):
        assert mm.pending_proposals(tmp_path / "nope") == []


class TestCatalogPass:
    def test_stale_when_catalog_is_missing(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        assert mm.catalog_is_stale(memory, tmp_path / "memory.json")

    def test_not_stale_when_catalog_is_newer(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "dev.md").write_text("x", encoding="utf-8")
        catalog = tmp_path / "memory.json"
        catalog.write_text("{}", encoding="utf-8")
        import os
        os.utime(catalog, (0, (memory / "dev.md").stat().st_mtime + 100))
        assert not mm.catalog_is_stale(memory, catalog)

    def test_stale_when_a_memory_file_is_newer(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        catalog = tmp_path / "memory.json"
        catalog.write_text("{}", encoding="utf-8")
        f = memory / "dev.md"
        f.write_text("x", encoding="utf-8")
        import os
        os.utime(f, (0, catalog.stat().st_mtime + 100))
        assert mm.catalog_is_stale(memory, catalog)

    def test_current_catalog_skips_the_rebuild(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(mm, "run_supervised",
                            lambda cmd, **kw: calls.append(cmd) or
                            subprocess.CompletedProcess(cmd, 0, "", ""))
        memory = tmp_path / "memory"
        memory.mkdir()
        catalogs = tmp_path / "catalogs"
        catalogs.mkdir()
        (catalogs / "memory.json").write_text("{}", encoding="utf-8")

        result = mm.run_catalog(tmp_path, memory, catalogs)
        assert not result.ran and calls == []


class TestActiveProject:
    def test_none_without_diary_entries(self, tmp_path):
        d = tmp_path / "diary"
        d.mkdir()
        assert mm.active_project(d) is None

    def test_missing_diary_dir_is_not_an_error(self, tmp_path):
        assert mm.active_project(tmp_path / "absent") is None


class TestMemoryIsNeverModified:
    """Plan done-condition 4: the maintainer 'demonstrably does not modify
    `.multiplai/memory/` itself'."""

    def _snapshot(self, d: Path) -> dict:
        return {p.name: (p.read_bytes(), p.stat().st_mtime)
                for p in sorted(d.glob("*.md"))}

    def test_a_full_run_leaves_memory_byte_identical(self, tmp_path, monkeypatch):
        memory = tmp_path / "memory"
        memory.mkdir()
        # Content that trips the linter, so the pass with the most reason to
        # "fix" something is the one under test.
        (memory / "dev.md").write_text(
            "- The current best model is Opus 5 (as of 2026-01, review by 2026-02).\n"
            "- Spike works at Multiplai.\n",
            encoding="utf-8")
        before = self._snapshot(memory)

        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- x\n", encoding="utf-8")

        class FakePaths:
            def data_dir(self): return tmp_path / "data"
            def memory_dir(self): return memory
            def dreams_dir(self): return tmp_path / "dreams"
            def learnings_dir(self): return learnings
            def catalogs_dir(self): return tmp_path / "catalogs"
            def diary_dir(self): return tmp_path / "diary"
            def dream_state_file(self): return tmp_path / "data" / "dream_state.yaml"

        monkeypatch.setattr(mm, "get_paths", lambda: FakePaths())
        # Every subprocess pass is stubbed: this test is about what the
        # maintainer writes, not about running a real dream.
        monkeypatch.setattr(mm, "run_supervised",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))

        report = mm.run_maintenance(force=True)
        assert report.gate_open
        # Guard against the test going vacuous: if the paths patch stopped
        # working, no pass would touch memory and this would "pass" for the
        # wrong reason. The lint pass must have actually read the fixture.
        lint = next(p for p in report.passes if p.name == "lint")
        assert lint.ran and "finding" in lint.detail
        assert self._snapshot(memory) == before

    def test_lint_pass_never_writes_into_the_memory_dir(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "dev.md").write_text(
            "- Currently EUR 20/mo (as of 2026-01, review by 2026-02).\n",
            encoding="utf-8")
        mm.run_lint(memory, tmp_path / "dreams")
        assert [p.name for p in memory.iterdir()] == ["dev.md"]


class TestSessionStartWiring:
    def test_launch_is_detached_and_non_blocking(self, tmp_path, monkeypatch):
        import session_start

        (tmp_path / "memory_maintainer.py").write_text("", encoding="utf-8")
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return type("P", (), {"stdin": None})()

        monkeypatch.setattr(session_start.subprocess, "Popen", fake_popen)
        assert session_start._launch_maintainer(tmp_path, tmp_path)
        assert seen["kwargs"]["start_new_session"] is True
        assert seen["cmd"][-1].endswith("memory_maintainer.py")

    def test_missing_script_is_a_quiet_no_op(self, tmp_path):
        import session_start
        assert not session_start._launch_maintainer(tmp_path, tmp_path)

    def test_launch_failure_never_breaks_session_start(self, tmp_path, monkeypatch):
        import session_start

        (tmp_path / "memory_maintainer.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(session_start.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no fork")))
        assert not session_start._launch_maintainer(tmp_path, tmp_path)

    def test_a_closed_gate_costs_no_subprocess_at_all(self, tmp_path, monkeypatch):
        """The child re-checks the gate authoritatively, but reaching it costs a
        `uv run` startup — and the scripts project declares a git dependency,
        so a cold uv cache makes that a network fetch at session start. Paying
        it to accomplish nothing must not happen."""
        import session_start

        (tmp_path / "memory_maintainer.py").write_text("", encoding="utf-8")
        (tmp_path / mm.STATE_FILENAME).write_text(
            f"last_run: '{datetime.now(timezone.utc).isoformat()}'\n", encoding="utf-8")

        def explode(*a, **k):
            raise AssertionError("spawned a child with the gate closed")

        monkeypatch.setattr(session_start.subprocess, "Popen", explode)
        assert not session_start._launch_maintainer(tmp_path, tmp_path)

    def test_an_open_gate_still_spawns(self, tmp_path, monkeypatch):
        import session_start

        (tmp_path / "memory_maintainer.py").write_text("", encoding="utf-8")
        (tmp_path / mm.STATE_FILENAME).write_text(
            f"last_run: '{(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()}'\n",
            encoding="utf-8")
        monkeypatch.setattr(session_start.subprocess, "Popen",
                            lambda *a, **k: type("P", (), {"stdin": None})())
        assert session_start._launch_maintainer(tmp_path, tmp_path)

    def test_the_two_gates_agree_on_hours_and_filename(self):
        """`session_start` restates the maintainer's gate rather than importing
        that PEP 723 script. Restated constants drift, so pin them together."""
        import session_start

        assert session_start._MAINTAINER_GATE_HOURS == mm.GATE_HOURS
        assert session_start._MAINTAINER_STATE_FILENAME == mm.STATE_FILENAME

    def test_missing_state_fails_open(self, tmp_path):
        """Same direction as the maintainer's own gate: an extra pass costs
        cents, a wedged gate costs maintenance that never runs again."""
        import session_start

        assert session_start._maintainer_gate_open(tmp_path / "absent.yaml")

    def test_corrupt_state_fails_open(self, tmp_path):
        import session_start

        state = tmp_path / mm.STATE_FILENAME
        state.write_text("last_run: not-a-timestamp\n", encoding="utf-8")
        assert session_start._maintainer_gate_open(state)


class TestCheapTierIsRealNotJustDocumented:
    def test_synthesize_threads_the_model_through_to_the_client(self):
        """The docstring claims pass 4 runs on a cheap tier. Before this,
        `synthesize()` took no model at all and the claim was decoration."""
        import inspect
        import synthesize_now

        assert "model" in inspect.signature(synthesize_now.synthesize).parameters
        src = inspect.getsource(synthesize_now._summarize_project)
        assert "model" in src and "client.query" in src

    def test_no_model_means_the_client_default(self):
        """Interactive callers (`/now`, backfill) must keep their old behavior."""
        import inspect
        import synthesize_now
        assert (inspect.signature(synthesize_now.synthesize)
                .parameters["model"].default is None)


class TestDreamPassTimeoutIsDerived:
    """F1 (log-doctor, 2026-08-05): the maintainer used a hardcoded 600 s cap
    while dream's own per-chunk budget was 900 s — and an oversized chunk gets
    1800 s. The unattended pass could not finish however fast the model ran, and
    6/6 runs in the week to 2026-08-05 timed out. The cap must therefore be
    derived from dream's constant, not chosen next to it.
    """

    def test_the_cap_is_at_least_two_chunk_deadlines(self):
        from lib.dream_chunking import CHUNK_TIMEOUT_S

        assert mm.DREAM_PASS_TIMEOUT_S >= 2 * CHUNK_TIMEOUT_S

    def test_the_cap_covers_the_worst_measured_run(self):
        """The 283 KB backlog took 37m55s end to end. 2400 s clears it by 5%,
        which is not margin; the derived 3600 s is."""
        assert mm.DREAM_PASS_TIMEOUT_S >= 2275 * 1.25

    def test_the_cap_is_imported_not_hardcoded(self):
        """A literal here is the defect: it drifts the moment dream's own budget
        moves, and the drift is silent until an unattended run dies."""
        import inspect

        src = inspect.getsource(mm)
        assert "from lib.dream_chunking import CHUNK_TIMEOUT_S" in src
        assert "timeout=600" not in src
        assert "DREAM_PASS_TIMEOUT_S = 4 * CHUNK_TIMEOUT_S" in src

    def test_the_dream_pass_hands_the_derived_cap_to_the_child(self, tmp_path,
                                                               monkeypatch):
        seen = {}

        def spy(cmd, *, timeout, **kw):
            seen["timeout"] = timeout
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(mm, "run_supervised", spy)
        learnings = tmp_path / "learnings"
        learnings.mkdir()
        (learnings / "2026-07-26.md").write_text("- x\n", encoding="utf-8")

        assert mm.run_dream(tmp_path, tmp_path / "absent.yaml", learnings,
                            tmp_path / "dreams").ran
        assert seen["timeout"] == mm.DREAM_PASS_TIMEOUT_S

    def test_every_child_pass_is_supervised(self):
        """F2's fix is only worth anything if nothing bypasses it. A bare
        `subprocess.run` on a child is how the orphaning came back."""
        import inspect

        src = inspect.getsource(mm)
        assert "subprocess.run(" not in src
