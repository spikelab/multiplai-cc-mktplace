"""Unit tests for the transcript cost collector."""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import costing_collector as cc  # noqa: E402
from multiplai_core.costing import iter_ledger  # noqa: E402


# ----------------------------------------------------------------------
# Fixture transcript builders
# ----------------------------------------------------------------------

def _usage(inp=100, out=50, cw5m=0, cw1h=0, cr=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw5m + cw1h,
        "cache_read_input_tokens": cr,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cw5m,
            "ephemeral_1h_input_tokens": cw1h,
        },
    }


def _assistant(msg_id, *, ts="2026-07-01T10:00:00Z", model="claude-opus-4-8",
               usage=None, content=None, sidechain=False,
               git_branch=None, cwd=None):
    entry = {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": usage or _usage(),
            "content": content if content is not None else [{"type": "text", "text": "hi"}],
        },
    }
    if git_branch is not None:
        entry["gitBranch"] = git_branch
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def _user(text=None, *, tool_result=False, meta=False, git_branch=None, cwd=None):
    if tool_result:
        content = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    else:
        content = text or "hello"
    entry = {"type": "user", "timestamp": "2026-07-01T10:00:00Z",
             "isMeta": meta or None, "message": {"content": content}}
    if git_branch is not None:
        entry["gitBranch"] = git_branch
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def _tool_use(name, inp):
    return {"type": "tool_use", "id": "toolu_x", "name": name, "input": inp}


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture(autouse=True)
def _workspace(monkeypatch, tmp_path):
    """Isolated workspace so ledger writes land in tmp."""
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_PLUGIN_OPTION_workspace_dir", "CLAUDE_PLUGIN_OPTION_data_dir"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    yield
    _reset_cache()


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "claude-config" / "projects" / "-Users-x-proj"
    d.mkdir(parents=True)
    return tmp_path / "claude-config"


# ----------------------------------------------------------------------
# collect_file
# ----------------------------------------------------------------------

def test_basic_collection_and_dedup(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("hello"),
        _assistant("m1"),
        _assistant("m1"),  # streaming rewrite duplicate
        _assistant("m2", usage=_usage(inp=2, out=1388, cw1h=919, cr=36499)),
    ])
    records, state = cc.collect_file(f, project="-Users-x-proj", known_ids=set())
    assert [r["msg_id"] for r in records] == ["m1", "m2"]
    assert state["offset"] == f.stat().st_size
    r2 = records[1]
    assert r2["tokens"] == {"in": 2, "out": 1388, "cw5m": 0, "cw1h": 919, "cr": 36499}
    expected = (2 * 5 + 1388 * 25 + 919 * 2 * 5 + 36499 * 0.1 * 5) / 1e6
    assert r2["cost_usd"] == pytest.approx(expected, abs=1e-6)


def test_synthetic_and_missing_usage_skipped(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", model="<synthetic>"),
        {"type": "assistant", "timestamp": "t", "message": {"id": "m2", "model": "claude-opus-4-8"}},
        _assistant("m3"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert [r["msg_id"] for r in records] == ["m3"]


def test_incremental_offset_resume(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_user("a"), _assistant("m1")])
    known: set[str] = set()
    records, state = cc.collect_file(f, project="p", known_ids=known)
    assert len(records) == 1
    # Append more, resume from state — only the new entry is read.
    with f.open("a") as fh:
        fh.write(json.dumps(_assistant("m2")) + "\n")
    records2, state2 = cc.collect_file(f, project="p", known_ids=known, file_state=state)
    assert [r["msg_id"] for r in records2] == ["m2"]
    assert state2["offset"] == f.stat().st_size


def test_shrunken_file_rescans_without_duplicates(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_assistant("m1"), _assistant("m2")])
    known: set[str] = set()
    _, state = cc.collect_file(f, project="p", known_ids=known)
    _write(f, [_assistant("m1")])  # rewritten shorter
    records, state2 = cc.collect_file(f, project="p", known_ids=known, file_state=state)
    assert records == []  # m1 already known; rescan added nothing
    assert state2["offset"] == f.stat().st_size


def test_torn_tail_reread_next_pass(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_assistant("m1")])
    with f.open("a") as fh:
        fh.write('{"type":"assistant","message":{"id":"m2"')  # no newline
    known: set[str] = set()
    records, state = cc.collect_file(f, project="p", known_ids=known)
    assert [r["msg_id"] for r in records] == ["m1"]
    # Complete the line; resume picks it up.
    with f.open("a") as fh:
        fh.write(',"model":"claude-opus-4-8","usage":' + json.dumps(_usage()) +
                 ',"content":[]},"timestamp":"2026-07-01T10:05:00Z"}\n')
    records2, _ = cc.collect_file(f, project="p", known_ids=known, file_state=state)
    assert [r["msg_id"] for r in records2] == ["m2"]


# ----------------------------------------------------------------------
# Span attribution
# ----------------------------------------------------------------------

def test_skill_span_opens_and_closes_on_user_prompt(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("run the review"),
        _assistant("m1", content=[_tool_use("Skill", {"skill": "code-review"})]),
        _user(tool_result=True),          # skill launch result — span stays open
        _assistant("m2"),                  # inside skill span
        _user("thanks, now something else"),  # user speaks — span closes
        _assistant("m3"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    by_id = {r["msg_id"]: r for r in records}
    # m1 carries the span it opened (cost of deciding to invoke counts toward it)
    assert by_id["m1"]["span"] == {"kind": "skill", "name": "code-review"}
    assert by_id["m2"]["span"] == {"kind": "skill", "name": "code-review"}
    assert by_id["m3"]["span"] is None


def test_sidechain_attributed_to_agent_span(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("go"),
        _assistant("m1", content=[_tool_use("Agent", {"subagent_type": "Explore", "description": "scan repo"})]),
        _assistant("s1", sidechain=True),
        _assistant("s2", sidechain=True),
        _user(tool_result=True),
        _assistant("m2"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    by_id = {r["msg_id"]: r for r in records}
    assert by_id["s1"]["sidechain"] is True
    assert by_id["s1"]["span"] == {"kind": "agent", "name": "Explore"}
    assert by_id["m2"]["sidechain"] is False


def test_legacy_task_tool_is_agent_span(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", content=[_tool_use("Task", {"description": "old style", "prompt": "x"})]),
        _assistant("s1", sidechain=True),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert records[1]["span"]["kind"] == "agent"


def test_command_invocation_opens_skill_span(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("<command-name>/multiplai-context:dream</command-name> <command-args></command-args>"),
        _assistant("m1"),
        _user("ok done"),
        _assistant("m2"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    by_id = {r["msg_id"]: r for r in records}
    assert by_id["m1"]["span"]["name"] == "multiplai-context:dream"
    assert by_id["m2"]["span"] is None


def test_mechanical_commands_do_not_open_spans(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("<command-name>/model</command-name>"),
        _assistant("m1"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert records[0]["span"] is None


def test_nested_spans_flagged_approx(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", content=[_tool_use("Skill", {"skill": "deep-research"})]),
        _assistant("m2", content=[_tool_use("Workflow", {"name": "research-sweep"})]),
        _assistant("m3"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    by_id = {r["msg_id"]: r for r in records}
    assert by_id["m3"]["span"]["kind"] == "workflow"
    assert by_id["m3"]["span"]["confidence"] == "approx"


def test_span_state_survives_offset_resume(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_assistant("m1", content=[_tool_use("Skill", {"skill": "code-review"})])])
    known: set[str] = set()
    _, state = cc.collect_file(f, project="p", known_ids=known)
    assert state["spans"] == [{"kind": "skill", "name": "code-review"}]
    with f.open("a") as fh:
        fh.write(json.dumps(_assistant("m2")) + "\n")
    records, _ = cc.collect_file(f, project="p", known_ids=known, file_state=state)
    assert records[0]["span"]["name"] == "code-review"


# ----------------------------------------------------------------------
# run_collect end-to-end
# ----------------------------------------------------------------------

def test_run_collect_idempotent(config_dir, tmp_path):
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_user("a"), _assistant("m1"), _assistant("m2")])
    _write(proj / "sess-2.jsonl", [_user("b"), _assistant("m3")])
    state_path = tmp_path / "state.json"

    stats = cc.run_collect(config_dir, state_path)
    assert stats["records"] == 3
    assert len(list(iter_ledger())) == 3

    # Second pass: nothing new, nothing re-read.
    stats2 = cc.run_collect(config_dir, state_path)
    assert stats2["records"] == 0
    assert stats2["files_read"] == 0
    assert len(list(iter_ledger())) == 3

    # Fresh state (simulates state loss): still no duplicates — ledger dedup.
    stats3 = cc.run_collect(config_dir, tmp_path / "state2.json")
    assert stats3["records"] == 0
    assert len(list(iter_ledger())) == 3


def test_run_collect_dry_run_writes_nothing(config_dir, tmp_path):
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_assistant("m1")])
    state_path = tmp_path / "state.json"
    stats = cc.run_collect(config_dir, state_path, dry_run=True)
    assert stats["records"] == 1
    assert not state_path.exists()
    assert list(iter_ledger()) == []


# ----------------------------------------------------------------------
# Transcript path classification (nested subagent/workflow files)
# ----------------------------------------------------------------------

class TestClassifyTranscript:
    def _projects(self, tmp_path):
        p = tmp_path / "projects"
        p.mkdir(exist_ok=True)
        return p

    def test_main_session_file(self, tmp_path):
        projects = self._projects(tmp_path)
        f = projects / "-Users-x-proj" / "abc-123.jsonl"
        f.parent.mkdir(parents=True)
        f.touch()
        ctx = cc.classify_transcript(projects, f)
        assert ctx == {"project": "-Users-x-proj", "session": "abc-123",
                       "sidechain": False, "base_span": None}

    def test_subagent_file_reads_meta_sidecar(self, tmp_path):
        projects = self._projects(tmp_path)
        d = projects / "-Users-x-proj" / "sess-1" / "subagents"
        d.mkdir(parents=True)
        f = d / "agent-a1b2.jsonl"
        f.touch()
        (d / "agent-a1b2.meta.json").write_text('{"agentType": "Explore"}')
        ctx = cc.classify_transcript(projects, f)
        assert ctx["project"] == "-Users-x-proj"
        assert ctx["session"] == "sess-1"
        assert ctx["sidechain"] is True
        assert ctx["base_span"] == {"kind": "agent", "name": "Explore"}

    def test_subagent_file_without_meta_uses_stem(self, tmp_path):
        projects = self._projects(tmp_path)
        d = projects / "proj" / "sess-1" / "subagents"
        d.mkdir(parents=True)
        f = d / "agent-a1b2.jsonl"
        f.touch()
        ctx = cc.classify_transcript(projects, f)
        assert ctx["base_span"] == {"kind": "agent", "name": "agent-a1b2"}

    def test_workflow_agent_file(self, tmp_path):
        projects = self._projects(tmp_path)
        d = projects / "proj" / "sess-1" / "subagents" / "workflows" / "wf_38f6e617"
        d.mkdir(parents=True)
        f = d / "agent-a2c.jsonl"
        f.touch()
        ctx = cc.classify_transcript(projects, f)
        assert ctx["session"] == "sess-1"
        assert ctx["project"] == "proj"
        assert ctx["base_span"] == {"kind": "workflow", "name": "wf_38f6e617"}

    def test_migration_tmp_prefix(self, tmp_path):
        projects = self._projects(tmp_path)
        d = projects / "projects_migration_tmp" / "old-proj" / "sess-9" / "subagents"
        d.mkdir(parents=True)
        f = d / "agent-a9.jsonl"
        f.touch()
        ctx = cc.classify_transcript(projects, f)
        assert ctx["project"] == "old-proj"
        assert ctx["session"] == "sess-9"


def test_run_collect_attributes_nested_subagent_costs(config_dir, tmp_path):
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_user("go"), _assistant("m1")])
    sub = proj / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.meta.json").write_text('{"agentType": "Explore"}')
    _write(sub / "agent-a1.jsonl", [_assistant("s1"), _assistant("s2")])

    stats = cc.run_collect(config_dir, tmp_path / "state.json")
    assert stats["records"] == 3
    recs = {r["msg_id"]: r for r in iter_ledger()}
    assert recs["s1"]["session"] == "sess-1"          # same session as main
    assert recs["s1"]["project"] == "-Users-x-proj"   # not "subagents"
    assert recs["s1"]["sidechain"] is True
    assert recs["s1"]["span"] == {"kind": "agent", "name": "Explore"}
    assert recs["m1"]["sidechain"] is False


def test_streaming_snapshots_keep_max_output(config_dir):
    """Duplicate entries of one message are streaming snapshots whose
    output_tokens grow — the final (largest) count must win."""
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", usage=_usage(inp=100, out=50)),
        _assistant("m1", usage=_usage(inp=100, out=700)),   # later snapshot
        _assistant("m1", usage=_usage(inp=100, out=1388)),  # final
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert len(records) == 1
    assert records[0]["tokens"]["out"] == 1388
    expected = (100 * 5 + 1388 * 25) / 1e6
    assert records[0]["cost_usd"] == pytest.approx(expected, abs=1e-6)


def test_history_copied_to_forked_session_not_double_billed(config_dir, tmp_path):
    """A resumed/forked session copies history lines into a new file — the
    same msg_id across files must be billed once (global dedup)."""
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_assistant("m1"), _assistant("m2")])
    _write(proj / "sess-2.jsonl", [_assistant("m1"), _assistant("m3")])  # fork
    stats = cc.run_collect(config_dir, tmp_path / "state.json")
    assert stats["records"] == 3  # m1 once, m2, m3


# ----------------------------------------------------------------------
# Branch / cwd attribution
# ----------------------------------------------------------------------

def test_record_carries_branch_and_cwd(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _user("go", git_branch="main", cwd="/w/proj"),
        _assistant("m1", git_branch="main", cwd="/w/proj"),
    ])
    records, state = cc.collect_file(f, project="p", known_ids=set())
    assert records[0]["branch"] == "main"
    assert records[0]["cwd"] == "/w/proj"
    assert state["branch"] == "main"
    assert state["cwd"] == "/w/proj"


def test_mid_file_branch_switch_splits_records(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", git_branch="main", cwd="/w"),
        _assistant("m2", git_branch="feat/x", cwd="/w"),
        _assistant("m3", git_branch="feat/x", cwd="/w"),
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert [r["branch"] for r in records] == ["main", "feat/x", "feat/x"]


def test_missing_branch_falls_back_to_last_seen(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [
        _assistant("m1", git_branch="main", cwd="/w"),
        _assistant("m2"),  # old-style entry without the fields
    ])
    records, _ = cc.collect_file(f, project="p", known_ids=set())
    assert records[1]["branch"] == "main"
    assert records[1]["cwd"] == "/w"


def test_no_branch_anywhere_omits_keys(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_user("a"), _assistant("m1")])
    records, state = cc.collect_file(f, project="p", known_ids=set())
    assert "branch" not in records[0]
    assert "cwd" not in records[0]
    assert state["branch"] == ""


def test_fallback_branch_survives_offset_resume(config_dir):
    f = config_dir / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    _write(f, [_assistant("m1", git_branch="feat/y", cwd="/w/y")])
    known: set[str] = set()
    _, state = cc.collect_file(f, project="p", known_ids=known)
    assert state["branch"] == "feat/y"
    with f.open("a") as fh:
        fh.write(json.dumps(_assistant("m2")) + "\n")  # no gitBranch
    records, _ = cc.collect_file(f, project="p", known_ids=known, file_state=state)
    assert records[0]["branch"] == "feat/y"
    assert records[0]["cwd"] == "/w/y"


def test_subagent_file_branch_from_own_entries(config_dir, tmp_path):
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_user("go", git_branch="main"),
                                   _assistant("m1", git_branch="main")])
    sub = proj / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    _write(sub / "agent-a1.jsonl", [_assistant("s1", git_branch="feat/z", cwd="/w/z")])
    cc.run_collect(config_dir, tmp_path / "state.json")
    recs = {r["msg_id"]: r for r in iter_ledger()}
    assert recs["m1"]["branch"] == "main"
    assert recs["s1"]["branch"] == "feat/z"
    assert recs["s1"]["cwd"] == "/w/z"


# ----------------------------------------------------------------------
# --backfill-branches
# ----------------------------------------------------------------------

def test_backfill_enriches_idempotently(config_dir, tmp_path):
    from multiplai_core.costing import ledger_file

    proj = config_dir / "projects" / "-Users-x-proj"
    transcript = [
        _assistant("m1", git_branch="main", cwd="/w"),
        _assistant("m2", git_branch="feat/x", cwd="/w"),
        _assistant("m3"),  # missing fields → falls back to feat/x
    ]
    f = proj / "sess-1.jsonl"
    _write(f, transcript)

    # Simulate a pre-feature ledger: collect, then strip branch/cwd.
    cc.run_collect(config_dir, tmp_path / "state.json")
    path = ledger_file("2026-07")
    stripped = []
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        rec.pop("branch", None)
        rec.pop("cwd", None)
        stripped.append(json.dumps(rec, separators=(",", ":")))
    path.write_text("".join(l + "\n" for l in stripped))

    # Add one record the transcripts can't match (e.g. SDK-sourced).
    with path.open("a") as fh:
        fh.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "msg_id": "sdk-1",
                             "session": "x", "cost_usd": 0.1},
                            separators=(",", ":")) + "\n")

    stats = cc.run_backfill_branches(config_dir)
    assert stats["examined"] == 4
    assert stats["enriched"] == 3
    assert stats["unmatched"] == 1

    recs = {r["msg_id"]: r for r in iter_ledger()}
    assert recs["m1"]["branch"] == "main"
    assert recs["m2"]["branch"] == "feat/x"
    assert recs["m3"]["branch"] == "feat/x"  # fallback semantics match live path
    assert recs["m1"]["cwd"] == "/w"
    assert "branch" not in recs["sdk-1"]

    # Second run: nothing new, already-enriched records untouched.
    before = path.read_bytes()
    stats2 = cc.run_backfill_branches(config_dir)
    assert stats2["enriched"] == 0
    assert stats2["unmatched"] == 1
    assert path.read_bytes() == before


def test_backfill_ignores_collector_offsets(config_dir, tmp_path):
    """Backfill reads transcripts from byte 0 even when the collector state
    says the file was fully consumed — and never touches that state."""
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_assistant("m1", git_branch="main")])
    state_path = tmp_path / "state.json"
    cc.run_collect(config_dir, state_path)  # offsets now at EOF

    from multiplai_core.costing import ledger_file
    path = ledger_file("2026-07")
    rec = json.loads(path.read_text())
    rec.pop("branch", None)
    path.write_text(json.dumps(rec, separators=(",", ":")) + "\n")

    state_before = state_path.read_bytes()
    stats = cc.run_backfill_branches(config_dir)
    assert stats["enriched"] == 1
    assert state_path.read_bytes() == state_before
    assert json.loads(path.read_text())["branch"] == "main"


def test_backfill_preserves_torn_tail_verbatim(config_dir, tmp_path):
    """A torn tail (crashed append, no trailing newline) survives the rewrite
    byte-identically — the tail copy carries over everything past the last
    complete line, which is also what protects a concurrent append."""
    proj = config_dir / "projects" / "-Users-x-proj"
    _write(proj / "sess-1.jsonl", [_assistant("m1", git_branch="main", cwd="/w")])
    cc.run_collect(config_dir, tmp_path / "state.json")

    from multiplai_core.costing import ledger_file
    path = ledger_file("2026-07")
    rec = json.loads(path.read_text())
    rec.pop("branch", None)
    rec.pop("cwd", None)
    torn = b'{"ts":"2026-07-01T00:00:01Z","msg_id":"half-writ'
    path.write_bytes(json.dumps(rec, separators=(",", ":")).encode() + b"\n" + torn)

    stats = cc.run_backfill_branches(config_dir)
    assert stats["enriched"] == 1
    content = path.read_bytes()
    assert content.endswith(torn)  # verbatim, no newline appended
    assert json.loads(content.splitlines()[0])["branch"] == "main"
