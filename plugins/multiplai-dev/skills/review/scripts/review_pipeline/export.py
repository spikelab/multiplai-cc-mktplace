"""Map a `ReviewState` onto the `findings.json` v1 contract.

The contract is `plugins/multiplai-dev/skills/review-viewer/schema/findings.v1.schema.json`.
Every dict here is built key by key, because the contract rejects unknown keys
and the pipeline's models carry more than it allows.

| pipeline outcome                     | v1 status      |
|--------------------------------------|----------------|
| confirmed                            | `confirmed`    |
| unverifiable (lowered severity kept) | `unverifiable` |
| refuted                              | `refuted`      |
| gate-rejected                        | `rejected`     |

A finding merged into another by the merge stage is not exported: it is gone
from `state.findings`, and the finding it went into carries its citations.
`expected_behaviour` comes from the verifier and is written for confirmed and
unverifiable findings only; a refuted or rejected finding has none.

`needs` (what the review could not get, each with a command for a person) is
written only when there are some: at the top level every need, and on each
finding the needs that block it. A need that blocked a finding merged away
moves to the finding it was merged into. A file without needs has neither
key, as before.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .models import SEVERITIES, Citation, Finding, Need, ReviewState

log = logging.getLogger(__name__)

PLUGIN_JSON = Path(__file__).resolve().parents[4] / ".claude-plugin" / "plugin.json"


def plugin_version() -> str:
    try:
        return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def producer() -> str:
    return f"multiplai-dev:review {plugin_version()}"


def _citation(c: Citation) -> dict:
    return {"path": c.path, "line_start": c.line_start, "line_end": c.line_end, "quote": c.quote}


def _finding(f: Finding, status: str, reason: str | None, expected: str | None) -> dict:
    return {
        "id": f.id,
        "severity": f.severity,
        "status": status,
        "claim": f.claim,
        "file": f.file,
        "line_start": f.line_start,
        "line_end": f.line_end,
        "failure_scenario": f.failure_scenario,
        "citations": [_citation(c) for c in f.citations],
        "verdict_reason": reason,
        "expected_behaviour": expected or None,
    }


def _need(n: Need) -> dict:
    return {"what": n.what, "blocks": n.blocks, "cause": n.cause, "command": n.command, "source": n.source}


def exported_needs(state: ReviewState) -> list[Need]:
    """`state.needs` with `blocks` following merges, each need once."""
    into = {m.finding.id: m.into for m in state.merged}
    out, seen = [], set()
    for n in state.needs:
        blocks = n.blocks
        while blocks in into:
            blocks = into[blocks]
        n = n.model_copy(update={"blocks": blocks})
        key = (n.what, n.blocks, n.command)
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def to_findings_file(state: ReviewState, *, generated_at: datetime | None = None) -> dict:
    t = state.target
    rows: list[dict] = []
    for f in state.findings:
        verdict = state.verdicts.get(f.id)
        status = verdict.status if verdict else "unverifiable"
        reason = verdict.reason if verdict else "not verified"
        expected = verdict.expected_behaviour if verdict and status in ("confirmed", "unverifiable") else None
        rows.append(_finding(f, status, reason, expected))
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    order = {"confirmed": 0, "unverifiable": 1, "refuted": 2}
    rows.sort(key=lambda r: (order[r["status"]], rank[r["severity"]]))
    for r in state.rejected:
        rows.append(_finding(r.finding, "rejected", r.reason, None))

    seen, unique = set(), []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        unique.append(row)

    needs = exported_needs(state)
    for row in unique:
        mine = [_need(n) for n in needs if n.blocks == row["id"]]
        if mine:
            row["needs"] = mine

    when = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    data = {
        "schema_version": 1,
        "generated_at": when.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "producer": producer(),
        "target": {
            "slug": t.slug,
            "label": t.label or t.slug,
            "repo_path": t.repo_path,
            "remote_url": t.remote_url,
            "base_sha": t.base_sha,
            "head_sha": t.head_sha,
            "files_changed": list(t.files),
        },
        "findings": unique,
    }
    if needs:
        data["needs"] = [_need(n) for n in needs]
    return data


def write_findings_file(state: ReviewState, target_dir: Path) -> Path:
    path = target_dir / "findings.json"
    data = to_findings_file(state)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
