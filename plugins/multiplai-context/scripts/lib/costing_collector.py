"""Transcript cost collector — incremental scan of Claude Code session JSONLs.

Walks ``$CLAUDE_CONFIG_DIR/projects/**/*.jsonl``, prices every assistant
message via :mod:`multiplai_core.costing`, and appends one ledger record per
API call. Designed to run repeatedly and cheaply:

- **Incremental**: a state file records per-transcript byte offsets; a pass
  reads only bytes appended since the last one. A shrunken file (session
  rewrite) is rescanned from zero.
- **Idempotent**: records are deduped against the ledger's global msg_id
  index, so rescans never double-bill. Transcripts contain duplicated
  assistant entries (streaming snapshots of one API call) whose
  ``output_tokens`` *grow* across occurrences — within a pass the snapshot
  with the largest output wins, so one record carries the call's final
  usage. Message ids are globally unique, so dedup is global (a resumed or
  forked session copies history lines into a new file; those are not new
  API calls). Known edge: if a collection pass runs while a message is
  still streaming, the ledger keeps that message's snapshot-so-far and
  later, larger snapshots are dropped — bounded to the one live turn.
- **Attributed**: a span tracker follows Skill / Agent (Task) / Workflow
  ``tool_use`` blocks and ``<command-name>`` slash invocations, so records
  carry the skill/agent context they were generated under. Sidechain entries
  (subagent traffic) are flagged and attributed to the innermost agent span.

Span semantics: a span opens at the invoking ``tool_use`` (or command
message) and closes when the user next speaks (a real text prompt — not a
``tool_result`` payload). This is approximate by construction for skills
(a skill is prompt injection, not a separate API context); records inside
nested spans get ``span.confidence = "approx"``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.costing import (
    TokenCounts,
    append_records,
    build_record,
    costs_dir,
    session_msg_index,
)

logger = logging.getLogger(__name__)

STATE_VERSION = 1

# tool_use names that open an attribution span. "Task" is the legacy name of
# the subagent tool; "Agent" the current one.
_SPAN_KINDS = {"Skill": "skill", "Agent": "agent", "Task": "agent", "Workflow": "workflow"}

_COMMAND_RE = re.compile(r"<command-name>/?([^<]+)</command-name>")
# Skills invoked as slash commands that are session mechanics, not work — a
# span for these would swallow the whole conversation that follows.
_COMMAND_IGNORE = {"model", "goal", "clear", "cost", "config", "login", "logout", "help"}


def default_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def find_transcripts(config_dir: Path) -> list[Path]:
    """All session transcripts under ``config_dir/projects/``, sorted."""
    projects = config_dir / "projects"
    if not projects.is_dir():
        return []
    return sorted(projects.glob("**/*.jsonl"))


def classify_transcript(projects_dir: Path, path: Path) -> dict:
    """Classify a transcript path into project/session/attribution context.

    Layout (optionally under a ``projects_migration_tmp/`` prefix)::

        <proj>/<sess>.jsonl                                       main session
        <proj>/<sess>/subagents/agent-<id>.jsonl                  subagent
        <proj>/<sess>/subagents/workflows/wf_<id>/agent-*.jsonl   workflow agent

    Nested files are subagent traffic: ``sidechain`` is forced true and
    ``base_span`` carries the agent/workflow identity (agent name read from
    the ``agent-<id>.meta.json`` sidecar when present).
    """
    parts = path.relative_to(projects_dir).parts
    if "subagents" in parts[:-1]:
        idx = parts.index("subagents")
        session = parts[idx - 1] if idx >= 1 else path.stem
        project = parts[idx - 2] if idx >= 2 else ""
        rest = parts[idx + 1:-1]  # dirs between subagents/ and the file
        if rest and rest[0] == "workflows" and len(rest) > 1 and rest[1].startswith("wf_"):
            span = {"kind": "workflow", "name": rest[1]}
        else:
            name = path.stem
            meta = path.with_suffix(".meta.json")
            try:
                name = json.loads(meta.read_text()).get("agentType") or name
            except (OSError, json.JSONDecodeError):
                pass
            span = {"kind": "agent", "name": name}
        return {"project": project, "session": session,
                "sidechain": True, "base_span": span}
    return {"project": parts[-2] if len(parts) >= 2 else "",
            "session": path.stem, "sidechain": False, "base_span": None}


# ----------------------------------------------------------------------
# Span tracking
# ----------------------------------------------------------------------

@dataclass
class SpanTracker:
    """Tracks the active Skill/Agent/Workflow attribution spans in one file."""

    stack: list[dict] = field(default_factory=list)

    def on_assistant(self, message: dict) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            kind = _SPAN_KINDS.get(block.get("name", ""))
            if kind is None:
                continue
            inp = block.get("input") or {}
            name = (
                inp.get("skill")
                or inp.get("subagent_type")
                or inp.get("name")
                or inp.get("description")
                or block.get("name", "")
            )
            self.stack.append({"kind": kind, "name": str(name)[:120]})

    def on_user(self, entry: dict) -> None:
        """Close spans when the user speaks; open one for slash commands."""
        text = _user_text(entry)
        if text is None:
            return  # tool_result payload or meta — not a turn boundary
        self.stack.clear()
        m = _COMMAND_RE.search(text)
        if m and m.group(1).strip() not in _COMMAND_IGNORE:
            self.stack.append({"kind": "skill", "name": m.group(1).strip()[:120], "via": "command"})

    def current(self, *, sidechain: bool) -> dict | None:
        if not self.stack:
            return None
        if sidechain:
            # Attribute subagent traffic to the innermost agent-like span.
            for span in reversed(self.stack):
                if span["kind"] in ("agent", "workflow"):
                    return dict(span)
            return None
        span = dict(self.stack[-1])
        if len(self.stack) > 1:
            span["confidence"] = "approx"
        return span


def _user_text(entry: dict) -> str | None:
    """The text of a real user prompt, or ``None`` if this user entry is a
    tool_result payload / meta caveat rather than the user speaking."""
    if entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        if texts:
            return "\n".join(texts)
    return None


# ----------------------------------------------------------------------
# State file
# ----------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        if state.get("version") == STATE_VERSION:
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "files": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(path)


# ----------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------

def _tokens_from_usage(usage: dict) -> TokenCounts:
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        cw5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
        cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    else:
        cw5m = int(usage.get("cache_creation_input_tokens") or 0)
        cw1h = 0
    return TokenCounts(
        input=int(usage.get("input_tokens") or 0),
        output=int(usage.get("output_tokens") or 0),
        cw5m=cw5m,
        cw1h=cw1h,
        cr=int(usage.get("cache_read_input_tokens") or 0),
    )


def collect_file(
    path: Path,
    *,
    project: str,
    known_ids: set[str],
    file_state: dict | None = None,
    session: str | None = None,
    sidechain: bool = False,
    base_span: dict | None = None,
) -> tuple[list[dict], dict]:
    """Collect new cost records from one transcript.

    Returns ``(records, new_file_state)``. *known_ids* is the global set of
    msg_ids already in the ledger — mutated in place as records are emitted.
    Within the pass, duplicate snapshots of one message keep the occurrence
    with the largest ``output_tokens`` (first occurrence's attribution).
    *sidechain*/*base_span* set the attribution context for nested
    subagent/workflow files (see :func:`classify_transcript`); main files
    track spans live instead.

    Records carry ``branch``/``cwd`` from the entry's ``gitBranch``/``cwd``
    fields (per-entry, so a mid-session checkout attributes correctly).
    Entries missing the fields fall back to the last non-empty value seen in
    the file; that fallback pair is persisted in the file state so an
    incremental resume mid-file still attributes correctly. No value at all
    (non-git cwd, ancient transcripts) → key omitted, never guessed.
    """
    session = session or path.stem
    size = path.stat().st_size
    offset = 0
    tracker = SpanTracker()
    last_branch = ""
    last_cwd = ""
    if file_state and 0 < file_state.get("offset", 0) <= size:
        offset = file_state["offset"]
        tracker.stack = list(file_state.get("spans") or [])
        last_branch = file_state.get("branch") or ""
        last_cwd = file_state.get("cwd") or ""
    pending: dict[str, dict] = {}  # msg_id -> best record this pass

    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # torn tail — re-read next pass
            offset += len(raw)
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("gitBranch"):
                last_branch = entry["gitBranch"]
            if entry.get("cwd"):
                last_cwd = entry["cwd"]
            etype = entry.get("type")
            if etype == "user":
                if not entry.get("isSidechain"):
                    tracker.on_user(entry)
                continue
            if etype != "assistant":
                continue
            message = entry.get("message") or {}
            entry_sidechain = sidechain or bool(entry.get("isSidechain"))
            if not entry_sidechain:
                tracker.on_assistant(message)
            usage = message.get("usage")
            model = message.get("model") or ""
            msg_id = message.get("id") or ""
            if not usage or not msg_id or model in ("", "<synthetic>"):
                continue
            if msg_id in known_ids:
                continue
            tokens = _tokens_from_usage(usage)
            prior = pending.get(msg_id)
            if prior is not None:
                # Streaming snapshot of a message already seen this pass —
                # keep the larger (later) output count, first attribution.
                if tokens.output > prior["tokens"].output:
                    prior["tokens"] = dataclasses.replace(
                        prior["tokens"], output=tokens.output
                    )
                continue
            span = base_span if base_span is not None else tracker.current(
                sidechain=entry_sidechain
            )
            pending[msg_id] = {
                "ts": entry.get("timestamp") or "",
                "model": model,
                "sidechain": entry_sidechain,
                "span": dict(span) if span else None,
                "tokens": tokens,
                "branch": last_branch,
                "cwd": last_cwd,
            }

    records: list[dict] = []
    for msg_id, p in pending.items():
        known_ids.add(msg_id)
        # Stamped here rather than in build_record — the ledger is schemaless
        # JSONL and reports read with .get(), so no multiplai-core change.
        rec = build_record(
            ts=p["ts"], source="transcript", session=session, project=project,
            model=p["model"], msg_id=msg_id, sidechain=p["sidechain"],
            span=p["span"], tokens=p["tokens"],
        )
        if p["branch"]:
            rec["branch"] = p["branch"]
        if p["cwd"]:
            rec["cwd"] = p["cwd"]
        records.append(rec)

    return records, {"size": size, "offset": offset, "spans": tracker.stack,
                     "branch": last_branch, "cwd": last_cwd}


def run_collect(
    config_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """One collection pass over every transcript. Returns summary stats."""
    state = load_state(state_path)
    files_state: dict[str, dict] = state["files"]
    # Message ids are globally unique — one global dedup set (a resumed or
    # forked session copies history lines into a new session file).
    known: set[str] = set()
    for ids in session_msg_index().values():
        known.update(ids)
    stats = {"files_seen": 0, "files_read": 0, "records": 0, "cost_usd": 0.0}

    for path in find_transcripts(config_dir):
        stats["files_seen"] += 1
        key = str(path)
        prior = files_state.get(key)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if prior and prior.get("offset") == size and prior.get("size") == size:
            continue  # nothing new
        ctx = classify_transcript(config_dir / "projects", path)
        records, new_state = collect_file(
            path,
            project=ctx["project"],
            session=ctx["session"],
            sidechain=ctx["sidechain"],
            base_span=ctx["base_span"],
            known_ids=known,
            file_state=prior,
        )
        stats["files_read"] += 1
        if records and not dry_run:
            append_records(records)
        stats["records"] += len(records)
        stats["cost_usd"] += sum(r["cost_usd"] for r in records)
        files_state[key] = new_state

    if not dry_run:
        save_state(state_path, state)
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


# ----------------------------------------------------------------------
# One-time branch backfill
# ----------------------------------------------------------------------

def build_branch_index(config_dir: Path) -> dict[str, tuple[str, str]]:
    """``msg_id -> (branch, cwd)`` from every transcript, read from byte 0.

    Ignores collection offsets entirely. Same fallback semantics as
    :func:`collect_file`: an assistant entry missing ``gitBranch``/``cwd``
    inherits the last non-empty value seen earlier in its file. First
    occurrence of a msg_id wins (streaming snapshots share attribution).
    """
    index: dict[str, tuple[str, str]] = {}
    for path in find_transcripts(config_dir):
        last_branch = ""
        last_cwd = ""
        try:
            fh = path.open("rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("gitBranch"):
                    last_branch = entry["gitBranch"]
                if entry.get("cwd"):
                    last_cwd = entry["cwd"]
                if entry.get("type") != "assistant":
                    continue
                msg_id = (entry.get("message") or {}).get("id") or ""
                if not msg_id or (not last_branch and not last_cwd):
                    continue
                index.setdefault(msg_id, (last_branch, last_cwd))
    return index


def run_backfill_branches(config_dir: Path) -> dict:
    """Enrich existing ledger records with ``branch``/``cwd`` in place.

    Rewrites each monthly ledger atomically (``*.tmp`` + ``os.replace``).
    Appends nothing and never touches the offset state — the caller holds
    the collector flock to keep writing passes out. Idempotent: records
    already carrying ``branch`` or ``cwd`` are left byte-identical, as are
    malformed lines. Returns ``examined/enriched/unmatched`` counts.
    """
    index = build_branch_index(config_dir)
    stats = {"examined": 0, "enriched": 0, "unmatched": 0}
    directory = costs_dir()
    if not directory.is_dir():
        return stats
    for path in sorted(directory.glob("ledger-*.jsonl")):
        out_lines: list[str] = []
        changed = False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    out_lines.append(stripped)  # preserve torn lines verbatim
                    continue
                stats["examined"] += 1
                if "branch" not in rec and "cwd" not in rec:
                    hit = index.get(rec.get("msg_id") or "")
                    if hit:
                        branch, cwd = hit
                        if branch:
                            rec["branch"] = branch
                        if cwd:
                            rec["cwd"] = cwd
                        stats["enriched"] += 1
                        changed = True
                        out_lines.append(json.dumps(rec, separators=(",", ":")))
                        continue
                    stats["unmatched"] += 1
                out_lines.append(stripped)
        if changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(l + "\n" for l in out_lines), encoding="utf-8")
            os.replace(tmp, path)
            logger.info("backfilled branches into %s", path.name)
    return stats
