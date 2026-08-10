"""The diary reads incrementally and still only ever appends.

Two properties that pull in opposite directions and must both hold:

* **Incremental reading.** ``extract_learnings.py`` called ``distill(p)`` with
  no ``since`` and re-read the whole transcript on every pass. The checkpoint
  writer already had the answer — a bookmark — and the reading half of it
  transfers directly.
* **Append-only writing.** The checkpoint's other half must NOT transfer.
  It overwrites, keeping only what is true now: diffing one session's
  checkpoint across 28 hours, "11/21 done, block 13 reviewing" had become
  "21/21 done" and four learnings-grade findings were simply gone. The diary
  is the permanent record learnings project from and dream consolidates.

So: same bookmark, opposite write semantics. The bookmark advances only after
the entry is on disk, and it points at the last turn READ rather than at
``now`` — a turn re-read costs a distillation, a turn skipped costs the record.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from lib import extraction as ex


BASE = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _turn(text, minutes, role="user"):
    return {
        "type": role,
        "timestamp": (BASE + timedelta(minutes=minutes)).isoformat(),
        "cwd": "/work/proj",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _unit(text, ts=None):
    return {
        "diary_entry": text,
        "timestamp": (ts or BASE).isoformat(),
        "learnings": [
            {"trust": "high", "type": "PATTERN", "description": text,
             "target": "dev.md", "action": "note it"}
        ],
    }


class _Paths:
    """The path surface ``extract()`` uses, pinned to one tmp dir."""

    def __init__(self, root):
        self.root = root

    def memory_dir(self): return self.root / "memory"
    def learnings_file(self): return self.root / "memory" / "learnings.md"
    def diary_dir(self): return self.root / "diary"
    def catalogs_dir(self): return self.root / "catalogs"
    def data_dir(self): return self.root


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(json.dumps(t) for t in [
            _turn("first slice turn A", 0),
            _turn("first slice turn B", 5),
        ]) + "\n"
    )
    return path


def _run_extract(tmp_path, transcript, *, units, session_id="s1", fail=False):
    """Drive ``extract()`` for real distillation, with a canned LLM result.

    Returns ``(handled, chunks_seen)`` — the chunks the fake extractor was
    given, which is what "read a non-overlapping slice" is asserted on.
    """
    import extract_learnings as el

    seen: list[str] = []

    async def _fake_extract(chunk, **kwargs):
        seen.append(chunk)
        if fail:
            raise RuntimeError("model exploded")
        return units, {"state": "active", "reason": ""}, []

    stdin = json.dumps({
        "session_id": session_id,
        "cwd": "/work/proj",
        "transcript_path": str(transcript),
    })
    with patch.object(el, "get_paths", lambda: _Paths(tmp_path)), \
         patch.object(el, "load_target_charters", lambda *a, **k: []), \
         patch.object(el, "create_client", AsyncMock(return_value=object())), \
         patch.object(el, "extract_session_signals", _fake_extract), \
         patch.object(el, "_refresh_now", AsyncMock(return_value=None)), \
         patch.object(el.sys, "stdin",
                      type("S", (), {"read": staticmethod(lambda: stdin)})):
        handled = asyncio.run(el.extract())
    return handled, seen


def _grow(transcript, turns):
    with transcript.open("a") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


class TestIncrementalReading:
    """Criterion 4: two consecutive extractions read non-overlapping slices."""

    def test_the_second_pass_reads_only_what_the_first_could_not(
        self, tmp_path, transcript
    ):
        _, first = _run_extract(tmp_path, transcript, units=[_unit("pass one")])
        assert "first slice turn A" in "\n".join(first)

        _grow(transcript, [_turn("second slice turn C", 60),
                           _turn("second slice turn D", 65)])
        _, second = _run_extract(tmp_path, transcript, units=[_unit("pass two")])

        joined = "\n".join(second)
        assert "second slice turn C" in joined
        assert "second slice turn D" in joined
        # Non-overlapping: nothing from the first slice is read again. The
        # boundary turn itself is the one exception the bookmark permits, and
        # it is not one of these.
        assert "first slice turn A" not in joined

    def test_the_bookmark_is_the_last_turn_read_not_the_clock(
        self, tmp_path, transcript
    ):
        """Bookmarking ``now`` would skip anything written to the transcript
        between the read and the write. The last turn read cannot."""
        _run_extract(tmp_path, transcript, units=[_unit("pass one")])

        mark = ex.load_diary_bookmark(tmp_path, "s1")
        assert mark == BASE + timedelta(minutes=5)

    def test_a_first_pass_reads_everything(self, tmp_path, transcript):
        _, first = _run_extract(tmp_path, transcript, units=[_unit("pass one")])
        joined = "\n".join(first)
        assert "first slice turn A" in joined and "first slice turn B" in joined


class TestTheBookmarkComesFromTheSamePassAsTheChunks:
    """One read, not two — for correctness before cost.

    A separate re-read to find the newest timestamp runs at a LATER wall-clock
    moment, so a turn appended in between is counted by the bookmark while
    never appearing in the chunks the model was given. The slice is then
    skipped forever. Reading it out of the same pass makes that impossible by
    construction, and halves the I/O on a multi-MB transcript as a side effect.
    """

    def test_the_transcript_is_parsed_once(self, tmp_path, transcript):
        from lib import transcript_distiller as td

        reads: list = []
        real = td.iter_distilled_turns

        def counting(path, **kwargs):
            reads.append(path)
            return real(path, **kwargs)

        with patch.object(td, "iter_distilled_turns", counting):
            _run_extract(tmp_path, transcript, units=[_unit("pass one")])

        assert len(reads) == 1

    def test_a_turn_appended_mid_extraction_is_not_skipped(
        self, tmp_path, transcript
    ):
        """The bookmark can only ever point at a turn that was in the chunks.

        Simulates the race: the transcript grows immediately after distillation
        but before the bookmark is written. The new turn must be left for the
        next pass, not silently bookmarked past.
        """
        late = _turn("appended during the model call", 90)

        original = ex.save_diary_bookmark

        def grow_then_save(data_dir, session_id, ts):
            _grow(transcript, [late])
            return original(data_dir, session_id, ts)

        import extract_learnings as el

        with patch.object(el, "save_diary_bookmark", grow_then_save):
            _run_extract(tmp_path, transcript, units=[_unit("pass one")])

        assert ex.load_diary_bookmark(tmp_path, "s1") == BASE + timedelta(minutes=5)

        _, second = _run_extract(tmp_path, transcript, units=[_unit("pass two")])
        assert "appended during the model call" in "\n".join(second)

    def test_distill_slice_returns_the_newest_timestamp_it_consumed(self, transcript):
        from lib.transcript_distiller import distill, distill_slice

        chunks, last_ts = distill_slice(transcript)

        assert last_ts == BASE + timedelta(minutes=5)
        # The bool-compatible wrapper still behaves exactly as before.
        assert distill(transcript) == chunks


class TestAppendOnly:
    """Criterion 4's second half: the second pass APPENDS."""

    def test_the_second_pass_appends_a_second_block(self, tmp_path, transcript):
        _run_extract(tmp_path, transcript, units=[_unit("pass one")])
        _grow(transcript, [_turn("more work", 60)])
        _run_extract(tmp_path, transcript, units=[_unit("pass two")])

        diary = next((tmp_path / "diary").glob("*.md")).read_text()
        assert diary.count("## Session: s1") == 2
        # The first pass's text is still there — appended, never replaced.
        assert "pass one" in diary
        assert "pass two" in diary

    def test_learnings_from_the_second_slice_are_not_deduped_away(
        self, tmp_path, transcript
    ):
        """The dedup key was ``session_id`` alone, which is correct while a
        session extracts once and silently drops everything the moment it
        extracts twice."""
        _run_extract(tmp_path, transcript, units=[_unit("pass one")])
        _grow(transcript, [_turn("more work", 60)])
        _run_extract(tmp_path, transcript, units=[_unit("pass two")])

        learnings = (tmp_path / "memory" / "learnings.md").read_text()
        assert "pass one" in learnings
        assert "pass two" in learnings

    def test_the_same_slice_twice_writes_once(self, tmp_path, transcript):
        """What the old session-id dedup was really for: a marker retried
        after its child died mid-write must not duplicate the entry. The slice
        key still catches it — the bookmark did not move, so the retry
        computes the same key."""
        _run_extract(tmp_path, transcript, units=[_unit("pass one")])
        # Rewind the bookmark to simulate a child that wrote the diary and
        # died before advancing it.
        ex.clear_diary_bookmark(tmp_path, "s1")
        _run_extract(tmp_path, transcript, units=[_unit("pass one")])

        diary = next((tmp_path / "diary").glob("*.md")).read_text()
        assert diary.count("## Session: s1") == 1


class TestBookmarkDiscipline:

    def test_a_failed_extraction_leaves_the_bookmark_unmoved(
        self, tmp_path, transcript
    ):
        handled, _ = _run_extract(
            tmp_path, transcript, units=[_unit("never written")], fail=True
        )

        assert handled is False  # marker retained for retry
        assert ex.load_diary_bookmark(tmp_path, "s1") is None

    def test_a_failed_pass_then_a_good_one_loses_nothing(
        self, tmp_path, transcript
    ):
        _run_extract(tmp_path, transcript, units=[_unit("x")], fail=True)
        _, second = _run_extract(tmp_path, transcript, units=[_unit("recovered")])

        joined = "\n".join(second)
        assert "first slice turn A" in joined
        assert "first slice turn B" in joined

    def test_the_checkpoint_bookmark_and_the_diary_bookmark_are_separate(
        self, tmp_path
    ):
        """They run on different cadences — a save every 30 minutes against an
        extraction once or twice a session — so sharing one field would make
        each one skip the other's slices. Different files, no shared key."""
        from lib import checkpoint as cp

        cp.save_state(tmp_path, "s1", {"last_checkpoint_ts": BASE.isoformat()})
        later = BASE + timedelta(hours=3)
        ex.save_diary_bookmark(tmp_path, "s1", later)

        assert cp.load_state(tmp_path, "s1")["last_checkpoint_ts"] == BASE.isoformat()
        assert ex.load_diary_bookmark(tmp_path, "s1") == later
        assert ex.bookmark_file(tmp_path, "s1") != cp.session_dir(tmp_path, "s1")
        assert "last_checkpoint_ts" not in ex.bookmark_file(tmp_path, "s1").read_text()


class TestSliceKey:

    def test_the_key_names_the_session_and_where_the_slice_ends(self):
        assert ex.slice_key("abc", None) == "abc:start"
        assert ex.slice_key("abc", BASE) == f"abc:{BASE.isoformat()}"

    def test_a_failed_bookmark_save_does_not_cost_the_next_slice(self, tmp_path):
        """Keyed by where the slice ENDS, not where it starts.

        ``save_diary_bookmark`` can fail (a full disk, a read-only mount) and
        its return value is advisory. The next pass then re-reads from the same
        start — so a start-keyed slice would produce the identical key, match
        the marker the first pass wrote, and drop every genuinely-new turn as a
        duplicate. Ending the key at the last turn read makes the second pass
        distinguishable, because it read further.
        """
        diary = tmp_path / "diary"
        first = ex.slice_key("s1", BASE)
        ex.write_diary_entries(
            [_unit("pass one")], diary, "s1", "/work", BASE.isoformat(),
            slice_id=first,
        )

        # The bookmark never stuck, so pass two starts from the same place —
        # but reads two hours further.
        second = ex.slice_key("s1", BASE + timedelta(hours=2))
        ex.write_diary_entries(
            [_unit("pass two")], diary, "s1", "/work", BASE.isoformat(),
            slice_id=second,
        )

        text = next(diary.glob("*.md")).read_text()
        assert "pass one" in text and "pass two" in text

    def test_a_retry_of_the_same_slice_still_dedups(self, tmp_path):
        """The guard's real job. A marker retried after its child died mid-write
        re-reads a transcript that has stopped growing, so it lands on the same
        end timestamp and writes nothing twice."""
        diary = tmp_path / "diary"
        key = ex.slice_key("s2", BASE + timedelta(hours=2))
        for _ in range(2):
            ex.write_diary_entries(
                [_unit("exactly once")], diary, "s2", "/work", BASE.isoformat(),
                slice_id=key,
            )

        assert next(diary.glob("*.md")).read_text().count("exactly once") == 1

    def test_write_diary_entries_without_a_slice_key_keeps_the_old_dedup(
        self, tmp_path
    ):
        """Back-compat: a caller that passes no slice id still gets
        one-block-per-session idempotency."""
        diary = tmp_path / "diary"
        for _ in range(2):
            ex.write_diary_entries(
                [_unit("only once")], diary, "s9", "/work", BASE.isoformat()
            )
        assert next(diary.glob("*.md")).read_text().count("## Session: s9") == 1
