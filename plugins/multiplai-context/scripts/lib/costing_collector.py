"""Transcript cost collector — incremental scan of Claude Code session JSONLs.

Walks ``$CLAUDE_CONFIG_DIR/projects/**/*.jsonl``, prices every assistant
message via :mod:`multiplai_core.costing`, and appends one ledger record per
API call. Designed to run repeatedly and cheaply:

- **Incremental**: a state file records per-transcript byte offsets; a pass
  reads only bytes appended since the last one. A shrunken file (session
  rewrite) is rescanned from zero.
- **Idempotent**: records are deduped against the ledger's ``session→msg_id``
  index, so rescans and the 2-3× duplicated entries that streaming rewrites
  leave in transcripts never double-bill. Duplicate entries are byte-identical
  in usage (verified 2026-07-06), so first-wins is exact.
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
) -> tuple[list[dict], dict]:
    """Collect new cost records from one transcript.

    Returns ``(records, new_file_state)``. *known_ids* is the set of msg_ids
    already in the ledger for this session — mutated in place as records are
    emitted so intra-pass duplicates dedup too.
    """
    session = path.stem
    size = path.stat().st_size
    offset = 0
    tracker = SpanTracker()
    if file_state and 0 < file_state.get("offset", 0) <= size:
        offset = file_state["offset"]
        tracker.stack = list(file_state.get("spans") or [])
    records: list[dict] = []

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
            etype = entry.get("type")
            if etype == "user":
                if not entry.get("isSidechain"):
                    tracker.on_user(entry)
                continue
            if etype != "assistant":
                continue
            message = entry.get("message") or {}
            sidechain = bool(entry.get("isSidechain"))
            if not sidechain:
                tracker.on_assistant(message)
            usage = message.get("usage")
            model = message.get("model") or ""
            msg_id = message.get("id") or ""
            if not usage or not msg_id or model in ("", "<synthetic>"):
                continue
            if msg_id in known_ids:
                continue
            known_ids.add(msg_id)
            records.append(build_record(
                ts=entry.get("timestamp") or "",
                source="transcript",
                session=session,
                project=project,
                model=model,
                msg_id=msg_id,
                sidechain=sidechain,
                span=tracker.current(sidechain=sidechain),
                tokens=_tokens_from_usage(usage),
            ))

    return records, {"size": size, "offset": offset, "spans": tracker.stack}


def run_collect(
    config_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """One collection pass over every transcript. Returns summary stats."""
    state = load_state(state_path)
    files_state: dict[str, dict] = state["files"]
    known = session_msg_index()
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
        session = path.stem
        records, new_state = collect_file(
            path,
            project=path.parent.name,
            known_ids=known.setdefault(session, set()),
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
