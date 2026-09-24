"""The `findings.json` v1 contract and the mailbox rows.

These pydantic models are the source of truth. `export-schema` writes them to
`schema/findings.v1.schema.json`, which is committed; producers (the review
skill) validate against that file. Every model forbids unknown keys, so a
producer that drifts fails validation instead of losing data silently.

Line numbers are 1-based and refer to the file at `head_sha`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "findings.v1.schema.json"

SHA_PATTERN = r"^[0-9a-f]{40}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_Strict):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str


class Premise(_Strict):
    statement: str
    kind: Literal["in_repo", "external"]
    citation: Citation | None = None


class Fix(_Strict):
    description: str
    patch_sketch: str | None = None
    premises: list[Premise] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Finding(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{10}$")
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    status: Literal["confirmed", "unverifiable", "refuted", "rejected"]
    claim: str
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    failure_scenario: str
    citations: list[Citation] = Field(min_length=1)
    verdict_reason: str | None = None
    fix: Fix | None = None


class Target(_Strict):
    slug: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    label: str
    repo_path: str
    remote_url: str | None = None
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    files_changed: list[str]


class FindingsFile(_Strict):
    schema_version: Literal[1]
    generated_at: datetime
    producer: str
    target: Target
    findings: list[Finding]


def finding_id(file: str, line_start: int, claim: str) -> str:
    """Stable id for a finding: survives re-renders of the same review."""
    raw = f"{file}\0{line_start}\0{claim}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


def load_findings(path: str | Path) -> FindingsFile:
    """Parse and validate a findings file. Raises pydantic.ValidationError."""
    text = Path(path).read_text(encoding="utf-8")
    return FindingsFile.model_validate_json(text)


def schema_text() -> str:
    """The JSON Schema for FindingsFile, in its committed form."""
    schema = FindingsFile.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "findings.v1.schema.json"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


# --- mailbox rows ------------------------------------------------------------


class Anchor(_Strict):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class InboxRow(_Strict):
    v: Literal[1] = 1
    id: str
    ts: str
    target: str
    kind: Literal["question", "decision"]
    finding_id: str | None = None
    anchor: Anchor | None = None
    text: str
    decision: Literal["accept", "reject", "defer"] | None = None


class OutboxRow(_Strict):
    v: Literal[1] = 1
    reply_to: str
    ts: str
    text: str
    done: bool


class Decision(_Strict):
    decision: Literal["accept", "reject", "defer"]
    note: str = ""
    ts: str
