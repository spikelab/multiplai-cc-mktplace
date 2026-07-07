"""End-to-end long-horizon simulation: one logical chat spanning >700K context
tokens across multiple checkpoint/rebuild cycles.

This drives the REAL hook code end to end — session_stop's checkpoint pass,
the real checkpoint_writer distillation pipeline (only the nested model call
is faked), pending-marker handoff, and session_start's rebuild injection —
over a multi-session simulated conversation full of tool_use/tool_result
records.

The goal condition it demonstrates:
  * a logical chat consuming >700K cumulative context tokens survives
    repeated physical-window handoffs,
  * state carries across every rebuild (each seed embeds the previous
    sessions' work),
  * no hook ever crashes, blocks a Stop (no "decision" key — /goal safe),
    or interferes with subagent transcripts,
  * tool blocks flow through distillation without breakage.
"""

import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import import_script
from lib import checkpoint as cp

session_stop = import_script("e2e_session_stop", "session_stop.py")
session_start = import_script("e2e_session_start", "session_start.py")
checkpoint_writer = import_script("e2e_checkpoint_writer", "checkpoint_writer.py")

# Per-session turn schedule: context tokens reported after each turn.
# 110K crosses band 1; 210K crosses band 2 == handoff threshold (200K).
TURN_TOKENS = [60_000, 110_000, 160_000, 210_000]
N_SESSIONS = 4


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_data_dir", str(data_dir))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _fake_run_agent_factory(calls):
    """Fake model call: builds a valid checkpoint that embeds breadcrumbs of
    everything it saw (previous checkpoint carry-over + latest work marker),
    mimicking the update-in-place merge a real model performs."""

    async def fake_run_agent(prompt, **kwargs):
        calls.append(prompt)
        # Carry forward breadcrumbs from the previous checkpoint (merge
        # semantics) and add one for the newest work item in the segment.
        breadcrumbs = [
            line
            for line in prompt.splitlines()
            if line.startswith("- breadcrumb:")
        ]
        for line in prompt.splitlines():
            if ("work item" in line or "goal step" in line) and "user:" in line:
                item = line.split("user:", 1)[1].strip()
                if item.startswith("["):
                    continue  # tool_result stub, not a work item
                crumb = f"- breadcrumb: {item}"
                if crumb not in breadcrumbs:
                    breadcrumbs.append(crumb)
        sections = []
        for s in cp.CHECKPOINT_SECTIONS:
            body = "\n".join(breadcrumbs) if s == "Task tree" else f"- {s.lower()} state"
            sections.append(f"## {s}\n{body}")
        return SimpleNamespace(text="\n".join(sections))

    return fake_run_agent


class LongChatHarness:
    """Simulates the Claude Code side: transcript growth + hook invocations."""

    def __init__(self, tmp_path, data_dir, monkeypatch, capsys, run_agent):
        self.tmp_path = tmp_path
        self.data_dir = data_dir
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.cwd = str(tmp_path / "bigproject")
        self.clock = datetime.now(timezone.utc)
        self.total_context_tokens = 0
        self.handoffs = 0
        self.rebuild_seeds: list[str] = []
        self.stop_outputs: list[str] = []
        self.spawned: list[dict] = []
        self._run_agent = run_agent
        # Route _spawn_writer to a synchronous in-process writer run, like the
        # detached subprocess but deterministic for the test.
        monkeypatch.setattr(session_stop, "_spawn_writer", self._sync_writer)

    # -- simulated detached writer ------------------------------------------
    def _sync_writer(self, payload):
        self.spawned.append(payload)
        self.monkeypatch.setattr(checkpoint_writer, "run_agent", self._run_agent)
        try:
            asyncio.run(checkpoint_writer.write_checkpoint(payload))
        finally:
            cp.release_writer(self.data_dir, payload["session_id"])
        return True

    # -- transcript building -------------------------------------------------
    def _tick(self):
        self.clock += timedelta(seconds=30)
        return self.clock.isoformat()

    def append_turn(self, transcript, user_text, ctx_tokens):
        """One user→assistant turn including tool_use + tool_result records."""
        records = [
            {
                "type": "user",
                "timestamp": self._tick(),
                "cwd": self.cwd,
                "message": {"role": "user", "content": user_text},
            },
            {
                "type": "assistant",
                "timestamp": self._tick(),
                "cwd": self.cwd,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"working on: {user_text}"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Edit",
                            "input": {"file_path": f"{self.cwd}/src/main.py"},
                        },
                    ],
                    "usage": {
                        "input_tokens": 1_500,
                        "cache_read_input_tokens": max(0, ctx_tokens - 1_500),
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 400,
                    },
                },
            },
            {
                "type": "user",
                "timestamp": self._tick(),
                "cwd": self.cwd,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "edit applied OK\n" + "diff line\n" * 40,
                        }
                    ],
                },
            },
        ]
        with transcript.open("a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        self.total_context_tokens += ctx_tokens

    # -- hook invocations ------------------------------------------------------
    def run_stop_hook(self, session_id, transcript):
        payload = {
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": self.cwd,
        }
        self.monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        session_stop.main()  # must never raise
        out = self.capsys.readouterr().out
        self.stop_outputs.append(out)
        return out

    def run_session_start(self, session_id, transcript, source="clear"):
        """Only the rebuild-injection step (the rest of SessionStart is
        orthogonal to checkpointing and needs live infra). Simulates the
        user continuing via /clear (source="clear" — the only manual source
        that inherits the parked checkpoint). Like Claude Code, the injected
        additionalContext becomes part of the new session's context —
        mirrored here by appending it to the transcript."""
        injected = session_start._inject_checkpoint_recovery(
            self.data_dir, self.cwd, session_id, source=source
        )
        out = self.capsys.readouterr().out
        if injected:
            self.rebuild_seeds.append(out)
            self.handoffs += 1
            with transcript.open("a") as f:
                f.write(json.dumps({
                    "type": "user",
                    "timestamp": self._tick(),
                    "cwd": self.cwd,
                    "message": {"role": "user", "content": out},
                }) + "\n")
        return injected, out


class TestLongHorizonChat:
    def test_700k_chat_survives_rebuild_cycles(self, tmp_path, data_env, monkeypatch, capsys):
        model_calls: list[str] = []
        harness = LongChatHarness(
            tmp_path, data_env, monkeypatch, capsys,
            _fake_run_agent_factory(model_calls),
        )

        for s in range(1, N_SESSIONS + 1):
            session_id = f"sess-{s}"
            transcript = tmp_path / "transcripts" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("")

            # A fresh session after a handoff must get the rebuild seed.
            injected, seed = harness.run_session_start(session_id, transcript)
            if s > 1:
                assert injected, f"session {s} did not receive rebuild seed"
                assert "CONTEXT REBUILD" in seed
                # Continuity: work items from EVERY earlier session survive
                # the chain of rebuilds (carried through checkpoint merges).
                for earlier in range(1, s):
                    assert f"work item {earlier}-2" in seed, (
                        f"session {s} seed lost breadcrumb of session {earlier}"
                    )
            else:
                assert not injected

            for turn_idx, ctx in enumerate(TURN_TOKENS, start=1):
                harness.append_turn(
                    transcript, f"work item {s}-{turn_idx}", ctx
                )
                harness.run_stop_hook(session_id, transcript)

            # Handoff threshold reached at the last turn: pending marker set,
            # user was advised via systemMessage.
            assert any(
                "systemMessage" in out and "/clear" in out
                for out in harness.stop_outputs[-len(TURN_TOKENS):]
            ), f"session {s}: no handoff advisory emitted"

        # ---- Volume: the logical chat consumed >700K context tokens ----
        assert harness.total_context_tokens > 700_000, harness.total_context_tokens

        # ---- Rebuilds actually happened ----
        assert harness.handoffs == N_SESSIONS - 1
        # Two writer runs per session (band 1 + band 2/handoff)
        assert len(harness.spawned) == N_SESSIONS * 2
        assert {p["reason"] for p in harness.spawned} == {"band"}

        # ---- Goal-safety: no Stop was ever blocked ----
        for out in harness.stop_outputs:
            assert '"decision"' not in out, "Stop hook emitted a blocking decision"
            if out.strip():
                frame = json.loads(out)  # any output must be valid hook JSON
                assert set(frame.keys()) <= {"systemMessage"}

        # ---- Tool traffic flowed through distillation into checkpoints ----
        assert model_calls, "writer never invoked"
        assert any("[call Edit(" in p for p in model_calls), (
            "tool_use blocks never reached the checkpoint writer"
        )
        assert any("edit applied OK" in p for p in model_calls), (
            "tool_result content never reached the checkpoint writer"
        )

    def test_subagent_transcripts_never_checkpoint(self, tmp_path, data_env, monkeypatch, capsys):
        """A research subagent's huge transcript must not trigger anything."""
        harness = LongChatHarness(
            tmp_path, data_env, monkeypatch, capsys, _fake_run_agent_factory([])
        )
        sub_dir = tmp_path / "transcripts" / "subagents"
        sub_dir.mkdir(parents=True)
        transcript = sub_dir / "agent-1.jsonl"
        transcript.write_text("")
        harness.append_turn(transcript, "deep research sweep", 450_000)
        out = harness.run_stop_hook("agent-1", transcript)
        assert harness.spawned == []
        assert out.strip() == ""
        assert not cp.checkpoints_root(data_env).exists() or not any(
            cp.checkpoints_root(data_env).iterdir()
        )

    def test_automatic_compact_rebuild_cycle(self, tmp_path, data_env, monkeypatch, capsys):
        """The fully-automatic path: steered auto-compaction fires near the
        handoff threshold, SessionStart(source=compact) re-injects the
        checkpoint into the SAME session (id unchanged, /goal hooks survive),
        counters reset, and the next window checkpoints again. No user
        action anywhere in the loop."""
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "90")  # trigger 207K
        model_calls: list[str] = []
        harness = LongChatHarness(
            tmp_path, data_env, monkeypatch, capsys,
            _fake_run_agent_factory(model_calls),
        )
        session_id = "auto-sess"
        transcript = tmp_path / "transcripts" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("")

        total = 0
        # Three compact cycles, same session id throughout.
        for cycle in range(1, 4):
            for turn_idx, ctx in enumerate(TURN_TOKENS, start=1):
                harness.append_turn(
                    transcript, f"work item {cycle}-{turn_idx}", ctx
                )
                out = harness.run_stop_hook(session_id, transcript)
                # AUTO MODE: no /clear nagging, ever — compaction handles it.
                assert "systemMessage" not in out
                total += ctx

            # Native auto-compaction fires (~200K); Claude Code then runs
            # SessionStart with source="compact" and the SAME session id.
            injected = session_start._inject_checkpoint_recovery(
                data_env, harness.cwd, session_id, source="compact"
            )
            seed = capsys.readouterr().out
            assert injected, f"cycle {cycle}: compact rebuild did not inject"
            assert "CONTEXT REBUILD" in seed
            for earlier in range(1, cycle + 1):
                assert f"work item {earlier}-2" in seed, (
                    f"cycle {cycle}: lost breadcrumb of cycle {earlier}"
                )
            # Post-compact, the same-session window restarts small; mirror
            # the injected seed into the transcript like Claude Code does.
            with transcript.open("a") as f:
                f.write(json.dumps({
                    "type": "user",
                    "timestamp": harness._tick(),
                    "cwd": harness.cwd,
                    "message": {"role": "user", "content": seed},
                }) + "\n")

        assert total > 700_000, total
        # Counters reset each cycle → every cycle re-checkpoints both bands.
        assert len(harness.spawned) == 3 * 2
        for out in harness.stop_outputs:
            assert '"decision"' not in out

    def test_marathon_goal_session_keeps_checkpoint_fresh(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """A /goal-style autonomous session that never /clears: the checkpoint
        must keep refreshing above the handoff threshold, and no Stop is ever
        blocked."""
        model_calls: list[str] = []
        harness = LongChatHarness(
            tmp_path, data_env, monkeypatch, capsys,
            _fake_run_agent_factory(model_calls),
        )
        transcript = tmp_path / "transcripts" / "marathon.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("")

        # Grows straight past the handoff threshold and keeps going to 330K.
        schedule = [110_000, 210_000, 240_000, 270_000, 300_000, 330_000]
        for turn_idx, ctx in enumerate(schedule, start=1):
            harness.append_turn(transcript, f"goal step {turn_idx}", ctx)
            harness.run_stop_hook("marathon", transcript)

        # band@110K, band@210K, then refresh every +25K step afterwards
        reasons = [p["reason"] for p in harness.spawned]
        assert reasons[:2] == ["band", "band"]
        assert reasons.count("refresh") == 4
        state = cp.load_state(data_env, "marathon")
        assert state["last_checkpoint_tokens"] == 330_000

        # The latest checkpoint carries the latest work.
        cp_text = cp.checkpoint_file(data_env, "marathon").read_text()
        assert "goal step 6" in cp_text
        for out in harness.stop_outputs:
            assert '"decision"' not in out
