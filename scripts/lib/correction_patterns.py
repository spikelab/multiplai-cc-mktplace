"""Correction-signal detection for the plugin.

Loads regex patterns from ``scripts/correction-patterns.yaml`` and
matches them against user turns in a session transcript. Unlike the
LLM-based extractor, detection is deterministic: if Spike wrote "nope"
the pattern fires, every time. The result is used by
``extract_learnings`` to tag any learnings from a session that
contained corrections with ``type: CORRECTION, trust: verified`` so
``/process-learnings`` can prioritize them.

Ported from the kit's context-router correction-detection helpers,
stripped of the UserPromptSubmit nudge logic which doesn't apply in
the Stop-hook learning-extraction path.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

CORRECTION_PATTERNS_FILE = Path(__file__).resolve().parent.parent / "correction-patterns.yaml"

# Categories whose matches should NOT be returned as corrections.
_NON_CORRECTION_CATEGORIES = frozenset({"approval"})


@lru_cache(maxsize=1)
def _load_compiled() -> dict:
    """Load and compile correction patterns from YAML. Cached for process lifetime.

    Returns a dict shaped as::

        {
          "settings": {...},
          "categories": {
             "explicit_corrections": {
                "confidence": "high",
                "description": "...",
                "patterns": [compiled_re, ...],
             },
             ...
          }
        }
    """
    if not CORRECTION_PATTERNS_FILE.exists():
        return {"settings": {}, "categories": {}}

    try:
        with open(CORRECTION_PATTERNS_FILE) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {"settings": {}, "categories": {}}

    compiled: dict = {
        "settings": raw.get("correction_nudge", {}),
        "categories": {},
    }

    for cat_name, cat_data in (raw.get("categories") or {}).items():
        patterns = []
        for pat in cat_data.get("patterns", []) or []:
            try:
                patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue  # Skip malformed patterns silently
        compiled["categories"][cat_name] = {
            "confidence": cat_data.get("confidence", "low"),
            "description": cat_data.get("description", ""),
            "patterns": patterns,
        }

    return compiled


def detect_correction(text: str) -> list[dict]:
    """Scan *text* for correction signals.

    Returns a list of matches sorted by confidence (high first)::

        [{"category": "explicit_corrections", "confidence": "high",
          "pattern": "^nope\\b", "excerpt": "..."}]

    ``approval`` category is excluded — it exists only for baseline
    tracking and must not be tagged as a correction.
    """
    if not text:
        return []
    config = _load_compiled()
    categories = config.get("categories") or {}
    if not categories:
        return []

    matches: list[dict] = []
    confidence_order = {"high": 0, "medium": 1, "low": 2}

    for cat_name, cat_data in categories.items():
        if cat_name in _NON_CORRECTION_CATEGORIES:
            continue
        for pat in cat_data["patterns"]:
            m = pat.search(text)
            if m:
                matches.append({
                    "category": cat_name,
                    "confidence": cat_data["confidence"],
                    "pattern": pat.pattern,
                    "excerpt": m.group(0)[:80],
                })
                break  # One match per category is enough

    matches.sort(key=lambda m: confidence_order.get(m["confidence"], 99))
    return matches


def extract_user_turns(transcript: str) -> list[str]:
    """Pull user-role message text out of a Claude Code transcript.

    Accepts either JSONL (one Claude Code message per line) or a raw
    text transcript. For JSONL, reads ``role == "user"`` entries and
    flattens string content plus ``type == "text"`` blocks. For
    non-JSONL input, returns the whole text as a single pseudo-turn so
    correction detection can still run against free-form transcripts.
    """
    turns: list[str] = []
    any_parsed = False
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        any_parsed = True
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "user" and entry.get("type") != "user":
            continue
        content = entry.get("content") or entry.get("message") or ""
        if isinstance(content, str):
            if content.strip():
                turns.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        turns.append(text)

    if not any_parsed and transcript.strip():
        return [transcript]
    return turns


def detect_corrections_in_transcript(transcript: str) -> list[dict]:
    """Detect correction signals across all user turns in a transcript.

    Returns a deduplicated list of matches keyed by (category, pattern)
    so multiple user turns matching the same pattern collapse into a
    single entry — the downstream CORRECTION tag cares that it fired,
    not how often.
    """
    seen: dict[tuple[str, str], dict] = {}
    for turn in extract_user_turns(transcript):
        for match in detect_correction(turn):
            key = (match["category"], match["pattern"])
            seen.setdefault(key, match)
    return list(seen.values())


def format_correction_block(matches: Iterable[dict]) -> str:
    """Render detected corrections as a markdown block for the learnings file."""
    lines: list[str] = []
    for match in matches:
        excerpt = match.get("excerpt", "").replace("\n", " ").strip()
        lines.append(
            f"- **[trust: verified]** CORRECTION "
            f"({match['category']} / {match['confidence']} confidence) — "
            f"{excerpt!r}"
        )
    return "\n".join(lines)
