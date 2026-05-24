"""Transcript distiller — deterministic pre-processing for JSONL transcripts.

Reduces raw Claude Code session transcripts (~multi-MB) to compact, token-
efficient text suitable for LLM extraction. Expected 10-50x size reduction.

No LLM calls — pure Python transformation pipeline:
  1. Parse JSONL; keep only user/assistant message records.
  2. Elide tool noise: stub tool_result, truncate long tool_use inputs,
     drop base64/image blocks and thinking blocks.
  3. Segment by cwd: tag each kept turn with (project, ts).
  4. Window-filter by timestamp range.
  5. Emit compact ``[ts] [project] role: text`` with turn markers.
  6. Map-reduce chunk sessions still over a token budget.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Approximate token budget per chunk (1 token ≈ 4 chars)
DEFAULT_TOKEN_BUDGET = 32_000
_CHARS_PER_TOKEN = 4

# Tool result stubs
_ELIDED_STUB = "[{tool}: {size}B elided]"
_LONG_INPUT_THRESHOLD = 2_000  # chars; truncate tool_use inputs longer than this
_B64_RE = re.compile(r'^[A-Za-z0-9+/]{100,}={0,2}$')


def _is_base64(text: str) -> bool:
    return bool(_B64_RE.match(text.strip()))


def _elide_tool_result(block: dict) -> str:
    """Stub a tool_result block."""
    tool_id = block.get("tool_use_id", "tool")
    content = block.get("content", "")
    if isinstance(content, str):
        size = len(content.encode("utf-8"))
        preview = content[:120].replace("\n", " ")
        if size > 400:
            return f"[{tool_id} → {size}B: {preview}…]"
        return f"[{tool_id} → {preview}]"
    return f"[{tool_id} → {len(str(content))}B elided]"


def _elide_tool_use(block: dict) -> str:
    """Summarise a tool_use block, truncating long inputs."""
    name = block.get("name", "tool")
    inp = block.get("input", {})
    inp_str = json.dumps(inp, ensure_ascii=False)
    if len(inp_str) > _LONG_INPUT_THRESHOLD:
        inp_str = inp_str[:_LONG_INPUT_THRESHOLD] + "…"
    return f"[call {name}({inp_str})]"


def _extract_text_from_content(content) -> str:
    """Extract human-readable text from a message content field."""
    if isinstance(content, str):
        if _is_base64(content):
            return "[base64 data elided]"
        return content

    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "")
            if _is_base64(text):
                parts.append("[base64 data elided]")
            else:
                parts.append(text)
        elif btype == "thinking":
            pass  # drop thinking blocks
        elif btype == "tool_use":
            parts.append(_elide_tool_use(block))
        elif btype == "tool_result":
            parts.append(_elide_tool_result(block))
        elif btype in ("image", "document"):
            parts.append(f"[{btype} elided]")
        else:
            text = block.get("text", "") or block.get("content", "")
            if text:
                parts.append(str(text))

    return "\n".join(p for p in parts if p.strip())


def _parse_timestamp(record: dict) -> datetime | None:
    """Parse a record's timestamp field to a UTC-aware datetime."""
    ts = record.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _derive_project(cwd: str) -> str:
    """Derive a project slug from a cwd path."""
    if not cwd:
        return "unknown"
    return Path(cwd).name or Path(cwd).parent.name or "unknown"


def iter_distilled_turns(
    jsonl_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[dict]:
    """Yield distilled turn dicts from a JSONL transcript file.

    Each yielded dict: {
        'ts': datetime,
        'role': 'user'|'assistant',
        'project': str,
        'cwd': str,
        'text': str,       # extracted + elided content
    }

    Skips records outside [since, until] window and non-message records.
    """
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        rtype = record.get("type", "")
        # Keep only assistant/user message records
        if rtype not in ("assistant", "user", ""):
            role = record.get("role", "")
            if role not in ("assistant", "user"):
                continue
            rtype = role

        role = record.get("role") or rtype
        if role not in ("assistant", "user"):
            continue

        ts = _parse_timestamp(record)

        if since and ts and ts < since:
            continue
        if until and ts and ts > until:
            continue

        # Skip Claude Code hook/meta noise
        msg_type = record.get("message", {}).get("type") if isinstance(record.get("message"), dict) else None
        if msg_type in ("system", "tool"):
            continue

        content = (
            record.get("content")
            or (record.get("message", {}) or {}).get("content")
        )
        if content is None:
            continue

        text = _extract_text_from_content(content).strip()
        if not text:
            continue

        cwd = record.get("cwd", "") or ""
        project = _derive_project(cwd)

        yield {
            "ts": ts,
            "role": role,
            "project": project,
            "cwd": cwd,
            "text": text,
        }


def distill(
    jsonl_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> list[str]:
    """Distill a JSONL transcript into compact text chunks.

    Returns a list of chunk strings; each chunk fits within token_budget.
    A single turn that exceeds the budget is truncated.
    """
    char_budget = token_budget * _CHARS_PER_TOKEN
    chunks: list[str] = []
    current_lines: list[str] = []
    current_chars = 0

    for turn in iter_distilled_turns(jsonl_path, since=since, until=until):
        ts_str = turn["ts"].isoformat() if turn["ts"] else "?"
        line = f"[{ts_str}] [{turn['project']}] {turn['role']}: {turn['text']}"

        # Truncate individual turns that are too long
        if len(line) > char_budget:
            line = line[:char_budget] + "…"

        if current_chars + len(line) > char_budget and current_lines:
            chunks.append("\n\n".join(current_lines))
            current_lines = []
            current_chars = 0

        current_lines.append(line)
        current_chars += len(line)

    if current_lines:
        chunks.append("\n\n".join(current_lines))

    return chunks


def estimate_tokens(jsonl_path: Path) -> int:
    """Fast token estimate for a JSONL file (no LLM call)."""
    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return 0
    # Raw transcript → ~10x compression ratio → /4 chars per token
    return max(1, size // 10 // _CHARS_PER_TOKEN)
