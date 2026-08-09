"""Checkpoint retirement — the artifact finally gets an end of life.

A checkpoint is *live state*: roughly where a session is right now, so the next
window can pick it up. The diary is the permanent record of what the session
did. Those are different artifacts with different lifetimes, but until now only
the first one had no ending — **182 checkpoint directories on disk at plan time
(2026-07-31), one per session ever run, none ever collected.**

The edge that makes deletion safe is narrow and specific: a diary entry for the
session now exists on disk. Past it the checkpoint says nothing the diary does
not. Before it, deleting is data loss.

Criterion 11 names three cases that must RETAIN, and they are tested separately
below because they fail for three unrelated reasons:

* **parked** — `AGENTS.md` renders a parked session's intent and files from its
  checkpoint, and a parked session is exactly the one still listed weeks later
  (its registry entry is GC-exempt). Collecting it would leave a deliberately
  preserved entry with nothing to say.
* **diary write failed** — no record exists, so nothing has superseded anything.
* **writer in flight** — a detached `checkpoint_writer.py` is mid-write.

A fourth guard is not in the criterion but is the same class of bug, and it is
the *walk-away* case this whole plan exists for: a session crosses the handoff
threshold, is told to `/clear`, and the tab gets closed instead. The unconsumed
pending marker is what the next morning's session rebuilds from — and
`session_start.py` degrades silently on a missing checkpoint file, so deleting
it would produce no rebuild, no error, and no clue.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import checkpoint as cp


def make_checkpoint(data_dir, sid, *, text="## Current intent\nDoing a thing.\n"):
    """A checkpoint directory as the writer leaves it."""
    sdir = cp.session_dir(data_dir, sid)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "checkpoint.md").write_text(text)
    (sdir / "state.json").write_text(json.dumps({"last_band_idx": 1}))
    return sdir


def make_pending_marker(data_dir, sid, project="alpha"):
    pdir = data_dir / "checkpoints" / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    marker = pdir / f"{project}.json"
    marker.write_text(json.dumps({
        "session_id": sid,
        "cwd": f"/work/{project}",
        "tokens": 210_000,
        "checkpoint_path": str(cp.checkpoint_file(data_dir, sid)),
        "created_at": "2026-08-01T09:00:00+00:00",
    }))
    return marker


# ---------------------------------------------------------------------------
# lib/checkpoint.py — the store's own two guards
# ---------------------------------------------------------------------------

class TestRetireCheckpoint:

    def test_it_removes_the_whole_directory(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")

        assert cp.retire_checkpoint(tmp_path, "s1") == (True, "")
        assert not sdir.exists()

    def test_it_removes_only_that_session(self, tmp_path):
        make_checkpoint(tmp_path, "s1")
        other = make_checkpoint(tmp_path, "s2")

        cp.retire_checkpoint(tmp_path, "s1")

        assert other.exists()
        assert cp.checkpoints_root(tmp_path).is_dir()

    def test_a_session_that_never_checkpointed_is_not_an_error(self, tmp_path):
        """Most sessions never cross a token band. `(False, "")` — nothing
        collected, nothing wrong — is distinct from a deliberate keep."""
        assert cp.retire_checkpoint(tmp_path, "never-ran") == (False, "")

    def test_an_in_flight_writer_retains_it(self, tmp_path):
        make_checkpoint(tmp_path, "s1")
        cp.claim_writer(tmp_path, "s1")

        removed, reason = cp.retire_checkpoint(tmp_path, "s1")

        assert removed is False
        assert reason == "writer in flight"
        assert cp.checkpoint_file(tmp_path, "s1").exists()

    def test_a_stale_writer_marker_does_not_retain_it(self, tmp_path):
        """`writer_inflight` already treats a marker older than 10 minutes as
        a crashed writer; retirement inherits that judgement rather than
        making a second one."""
        make_checkpoint(tmp_path, "s1")
        marker = cp.claim_writer(tmp_path, "s1")
        old = time.time() - 3600
        os.utime(marker, (old, old))

        assert cp.retire_checkpoint(tmp_path, "s1")[0] is True

    def test_an_unconsumed_rebuild_marker_retains_it(self, tmp_path):
        make_checkpoint(tmp_path, "s1")
        make_pending_marker(tmp_path, "s1")

        removed, reason = cp.retire_checkpoint(tmp_path, "s1")

        assert removed is False
        assert "pending rebuild marker" in reason
        assert cp.checkpoint_file(tmp_path, "s1").exists()

    def test_a_marker_for_a_different_session_does_not_retain_it(self, tmp_path):
        make_checkpoint(tmp_path, "s1")
        make_pending_marker(tmp_path, "other-session")

        assert cp.retire_checkpoint(tmp_path, "s1")[0] is True

    def test_an_unreadable_marker_is_skipped_not_fatal(self, tmp_path):
        make_checkpoint(tmp_path, "s1")
        pdir = tmp_path / "checkpoints" / "pending"
        pdir.mkdir(parents=True)
        (pdir / "broken.json").write_text("{not json")

        assert cp.retire_checkpoint(tmp_path, "s1")[0] is True

    def test_a_path_traversing_session_id_is_refused(self, tmp_path):
        make_checkpoint(tmp_path, "s1")

        assert cp.retire_checkpoint(tmp_path, "..")[0] is False
        assert cp.retire_checkpoint(tmp_path, "../checkpoints")[0] is False
        assert cp.checkpoints_root(tmp_path).is_dir()

    def test_it_never_raises(self, tmp_path):
        assert cp.retire_checkpoint(tmp_path / "no-such-dir", "s1") == (False, "")

    def test_it_never_raises_even_on_non_oserror(self, tmp_path):
        """The docstring says "never raises", full stop — not "never raises
        OSError". This runs inside the extraction pipeline, where an escaped
        exception costs the session's diary entry."""
        make_checkpoint(tmp_path, "s1")

        with patch.object(cp, "writer_inflight",
                          side_effect=ValueError("embedded null byte")):
            removed, reason = cp.retire_checkpoint(tmp_path, "s1")

        assert removed is False
        assert "removal failed" in reason
        assert cp.checkpoint_file(tmp_path, "s1").exists()


class TestPendingMarkerOwner:

    def test_it_finds_the_marker_for_a_session(self, tmp_path):
        marker = make_pending_marker(tmp_path, "s1", project="alpha")

        assert cp.pending_marker_owner(tmp_path, "s1") == marker

    def test_it_scans_across_projects(self, tmp_path):
        make_pending_marker(tmp_path, "other", project="alpha")
        marker = make_pending_marker(tmp_path, "s1", project="beta")

        assert cp.pending_marker_owner(tmp_path, "s1") == marker

    def test_no_marker_directory_is_none(self, tmp_path):
        assert cp.pending_marker_owner(tmp_path, "s1") is None

    def test_a_consumed_marker_no_longer_owns_it(self, tmp_path):
        """`consume_pending_marker` claims by rename, so the checkpoint is
        collectable as soon as the rebuild has happened."""
        make_checkpoint(tmp_path, "s1")
        make_pending_marker(tmp_path, "s1", project="alpha")
        cfg = cp.CheckpointConfig()

        claimed = cp.consume_pending_marker(tmp_path, "/work/alpha", "s2", cfg)

        assert claimed is None or claimed["session_id"] == "s1"
        assert cp.pending_marker_owner(tmp_path, "s1") is None


# ---------------------------------------------------------------------------
# Criterion 11 — the pipeline edge, through extract_learnings.py
# ---------------------------------------------------------------------------

def _unit(text="Did a thing."):
    return {"timestamp": "2026-08-01T10:00:00Z", "diary_entry": text, "learnings": []}


class TestExtractionRetiresCheckpoints:
    """Driven through ``extract()`` so the guard is tested where it lives, not
    re-expressed in the test."""

    def _run(self, tmp_path, *, units, disposition, diary_ok=True, llm_ok=True,
             session_id="s1", trigger=None, n_chunks=1, fail_chunks=()):
        """Drive ``extract()`` with a stubbed pipeline.

        ``n_chunks``/``fail_chunks`` model a multi-chunk transcript where
        some chunk indices raise (partial LLM failure): each surviving chunk
        yields *units* and *disposition*; the disposition only counts on the
        final chunk, exactly as in production.
        """
        import extract_learnings as el

        class _Paths:
            def memory_dir(self): return tmp_path / "memory"
            def learnings_file(self): return tmp_path / "memory" / "learnings.md"
            def diary_dir(self): return tmp_path / "diary"
            def catalogs_dir(self): return tmp_path / "catalogs"
            def data_dir(self): return tmp_path
            def logs_dir(self): return tmp_path / "logs"

        calls = {"n": 0}

        async def _fake_extract(chunk, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            if not llm_ok or i in fail_chunks:
                raise RuntimeError("model unavailable")
            return units, disposition, []

        diary_file = tmp_path / "diary" / "2026-08-01.md"

        def _fake_diary(*a, **k):
            if not diary_ok:
                return None
            diary_file.parent.mkdir(parents=True, exist_ok=True)
            diary_file.write_text("# Diary\n")
            return diary_file

        payload = {"session_id": session_id, "cwd": "/work/alpha"}
        if trigger is not None:
            payload["trigger"] = trigger
        stdin = json.dumps(payload)
        chunks = [f"chunk-{i}" for i in range(n_chunks)]
        with patch.object(el, "get_paths", _Paths), \
             patch.object(el, "load_target_charters", lambda *a, **k: []), \
             patch.object(el, "create_client", AsyncMock(return_value=object())), \
             patch.object(el, "_distill_transcript", lambda *a: chunks), \
             patch.object(el, "extract_session_signals", _fake_extract), \
             patch.object(el, "write_diary_entries", _fake_diary), \
             patch.object(el, "append_learnings", lambda *a, **k: True), \
             patch.object(el, "record_disposition", lambda *a, **k: True), \
             patch.object(el, "_refresh_now", AsyncMock(return_value=None)), \
             patch.object(el.sys, "stdin",
                          type("S", (), {"read": staticmethod(lambda: stdin)})):
            return asyncio.run(el.extract())

    def test_a_successful_diary_write_retires_the_checkpoint(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()], disposition={"state": "done", "reason": "shipped"})

        assert not sdir.exists()

    def test_an_active_session_is_retired_too(self, tmp_path):
        """Retirement keys on the diary entry existing, not on the session
        being declared finished — an unlabelled session that produced a diary
        entry is over as far as the checkpoint is concerned."""
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()], disposition={"state": "active", "reason": ""})

        assert not sdir.exists()

    # --- the three retention cases (criterion 11) --------------------------

    def test_parked_retains_it(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()],
                  disposition={"state": "parked", "reason": "back tomorrow"})

        assert (sdir / "checkpoint.md").exists()

    def test_a_failed_diary_write_retains_it(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()],
                  disposition={"state": "done", "reason": "x"}, diary_ok=False)

        assert (sdir / "checkpoint.md").exists()

    def test_an_in_flight_writer_retains_it(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")
        cp.claim_writer(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()], disposition={"state": "done", "reason": "x"})

        assert (sdir / "checkpoint.md").exists()

    # --- and the walk-away guard -------------------------------------------

    def test_an_unconsumed_rebuild_marker_retains_it(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")
        make_pending_marker(tmp_path, "s1", project="alpha")

        self._run(tmp_path, units=[_unit()], disposition={"state": "done", "reason": "x"})

        assert (sdir / "checkpoint.md").exists()

    # --- failure isolation --------------------------------------------------

    def test_a_failed_extraction_retires_nothing(self, tmp_path):
        """No units, no diary, nothing superseded — and the marker is kept for
        retry, so the next attempt still needs whatever state there is."""
        sdir = make_checkpoint(tmp_path, "s1")

        assert self._run(tmp_path, units=[], disposition={"state": "active"},
                         llm_ok=False) is False
        assert (sdir / "checkpoint.md").exists()

    def test_a_partial_failure_retains_it(self, tmp_path):
        """An earlier chunk fails but the final one survives: units exist, the
        diary IS written and the marker consumed — yet that diary covers only
        part of the session, so the checkpoint must survive. Retirement
        demands a FULLY successful extraction; a real disposition alone is
        not enough."""
        sdir = make_checkpoint(tmp_path, "s1")
        diary_file = tmp_path / "diary" / "2026-08-01.md"

        assert self._run(tmp_path, units=[_unit()],
                         disposition={"state": "done", "reason": "shipped"},
                         n_chunks=3, fail_chunks={0}) is True
        assert diary_file.exists()
        assert (sdir / "checkpoint.md").exists()

    def test_a_failed_final_chunk_retains_it(self, tmp_path):
        """The final chunk is the entire disposition signal. When it fails,
        ``disposition`` stays at the fabricated default "active" — the same
        fabrication `record_disposition` refuses to write must not drive an
        irreversible delete. Units from earlier chunks still write a diary
        entry; the checkpoint stays."""
        sdir = make_checkpoint(tmp_path, "s1")
        diary_file = tmp_path / "diary" / "2026-08-01.md"

        assert self._run(tmp_path, units=[_unit()],
                         disposition={"state": "done", "reason": "shipped"},
                         n_chunks=3, fail_chunks={2}) is True
        assert diary_file.exists()
        assert (sdir / "checkpoint.md").exists()

    # --- PreCompact: the session is still running ---------------------------

    def test_a_pre_compact_extraction_retains_it(self, tmp_path):
        """A PreCompact-deferred extraction runs against a session that is
        STILL LIVE under the same session_id after compaction. The diary
        entry is welcome; deleting the live session's `state.json`
        (`rebuild_ts`) and incrementally merged `checkpoint.md` is not."""
        sdir = make_checkpoint(tmp_path, "s1")
        diary_file = tmp_path / "diary" / "2026-08-01.md"

        assert self._run(tmp_path, units=[_unit()],
                         disposition={"state": "active", "reason": ""},
                         trigger="pre_compact") is True
        assert diary_file.exists()
        assert (sdir / "checkpoint.md").exists()
        assert (sdir / "state.json").exists()

    def test_a_session_end_trigger_still_retires_it(self, tmp_path):
        """SessionEnd markers carry no `trigger` (forwarded as "") — the
        session is over, so the successful-extraction path retires."""
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[_unit()],
                  disposition={"state": "done", "reason": "shipped"}, trigger="")

        assert not sdir.exists()

    def test_a_session_with_no_units_retires_nothing(self, tmp_path):
        sdir = make_checkpoint(tmp_path, "s1")

        self._run(tmp_path, units=[], disposition={"state": "done", "reason": "x"})

        assert (sdir / "checkpoint.md").exists()

    def test_retirement_failing_does_not_fail_the_extraction(self, tmp_path):
        """Disk is not worth a diary entry. The write already happened by the
        time this runs, and its return value must not depend on cleanup."""
        make_checkpoint(tmp_path, "s1")

        with patch.object(cp, "retire_checkpoint", side_effect=OSError("read-only fs")):
            assert self._run(tmp_path, units=[_unit()],
                             disposition={"state": "done", "reason": "x"}) is True

    def test_no_checkpoint_is_not_an_error(self, tmp_path):
        assert self._run(tmp_path, units=[_unit()],
                         disposition={"state": "done", "reason": "x"}) is True
