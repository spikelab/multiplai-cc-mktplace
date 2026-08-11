"""Child-session and re-entry guards on the hook entry points (M7, P4).

Every hook fires for SDK child sessions too (subagents, nested hook
sessions). The checkpoint halves have always skipped them; these tests pin
the guards on the paths that did NOT: session_start's spawns and drains,
context_manager's per-prompt router, session_notification's registry stamp,
and the extraction-marker halves of session_end and pre_compact — the gap
that queued a full LLM extraction of a subagent transcript into the user's
diary.

Plus the Stop re-entry flag: when another Stop hook blocks, the harness
re-runs the chain with ``stop_hook_active`` set, and this plugin's Stop hook
must not repeat its passes (P4).
"""

import io
import json
import sys

import pytest
from conftest import PLUGIN_ROOT, import_script


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _stdin(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _child_payload(tmp_path, sid="child-1"):
    sub = tmp_path / "subagents"
    sub.mkdir(exist_ok=True)
    transcript = sub / "t.jsonl"
    transcript.write_text("{}\n")
    return {
        "session_id": sid,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "prompt": "how do I configure python logging",
    }


class TestChildSessionGuards:
    def test_session_start_does_nothing_for_a_child(
        self, tmp_path, data_env, monkeypatch
    ):
        session_start = import_script("session_start_guards", "session_start.py")
        monkeypatch.setenv("_HOOK_CHILD_SESSION", "1")
        _stdin(monkeypatch, _child_payload(tmp_path))

        session_start.main()

        # No registration, no state, no drains: the data dir stays untouched.
        assert not (data_env / "session_state.json").exists()
        assert not (data_env / "sessions").exists()

    def test_session_start_skips_a_subagent_transcript_without_the_env(
        self, tmp_path, data_env, monkeypatch
    ):
        session_start = import_script("session_start_guards2", "session_start.py")
        monkeypatch.delenv("_HOOK_CHILD_SESSION", raising=False)
        _stdin(monkeypatch, _child_payload(tmp_path))

        session_start.main()

        assert not (data_env / "session_state.json").exists()

    def test_context_manager_emits_empty_context_for_a_child(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        context_manager = import_script("context_manager_guards", "context_manager.py")
        monkeypatch.setenv("_HOOK_CHILD_SESSION", "1")
        _stdin(monkeypatch, _child_payload(tmp_path))

        context_manager.main()

        out = capsys.readouterr().out.strip().splitlines()
        payload = json.loads(out[-1])
        # The stdout contract holds — valid JSON, empty context, no routing.
        assert payload["hookSpecificOutput"]["additionalContext"] == ""

    def test_session_notification_skips_the_registry_for_a_child(
        self, tmp_path, data_env, monkeypatch
    ):
        session_notification = import_script(
            "session_notification_guards", "session_notification.py"
        )
        monkeypatch.setenv("_HOOK_CHILD_SESSION", "1")
        _stdin(monkeypatch, _child_payload(tmp_path))

        session_notification.main()

        assert not (data_env / "sessions").exists()

    def test_session_end_queues_no_extraction_for_a_child_transcript(
        self, tmp_path, data_env, monkeypatch
    ):
        session_end = import_script("session_end_guards", "session_end.py")
        monkeypatch.delenv("_HOOK_CHILD_SESSION", raising=False)
        payload = _child_payload(tmp_path)
        payload["reason"] = "other"
        _stdin(monkeypatch, payload)

        session_end.main()

        pending = data_env / "pending_extractions"
        assert not pending.exists() or not list(pending.glob("*.json")), (
            "a subagent transcript must never be queued for diary extraction"
        )

    def test_pre_compact_queues_no_extraction_for_a_child_transcript(
        self, tmp_path, data_env, monkeypatch
    ):
        pre_compact = import_script("pre_compact_guards", "pre_compact.py")
        monkeypatch.delenv("_HOOK_CHILD_SESSION", raising=False)
        _stdin(monkeypatch, _child_payload(tmp_path))

        pre_compact.main()

        pending = data_env / "pending_extractions"
        assert not pending.exists() or not list(pending.glob("*.json"))


class TestStopHookActive:
    def test_second_pass_is_a_no_op(self, tmp_path, data_env, monkeypatch):
        """P4: on the harness's stop_hook_active re-run, this hook already did
        its passes — repeating them costs a transcript read and a discarded
        systemMessage."""
        session_stop = import_script("session_stop_guards", "session_stop.py")
        data_env.mkdir(parents=True, exist_ok=True)
        state = {"session_id": "s1"}
        (data_env / "session_state.json").write_text(json.dumps(state))
        payload = _child_payload(tmp_path, sid="s1")
        payload["stop_hook_active"] = True
        _stdin(monkeypatch, payload)

        session_stop.main()

        after = json.loads((data_env / "session_state.json").read_text())
        assert "last_stop" not in after, (
            "the re-entered Stop hook must not re-stamp liveness"
        )
