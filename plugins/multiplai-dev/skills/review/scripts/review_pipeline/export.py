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
unverifiable findings only; a refuted or rejected finding has none. The same
goes for `assessment`, the assess stage's label (`useful`, `still-open`,
`low-value`, or `repeat` of a finding rejected in an earlier round), which is
optional: files written before the stage existed have none.
`verifier_citations` (optional in v1) holds the lines the verifier read; a
finding with no verdict, such as a gate-rejected one, has no such key.

`checks.json` (the `checks.v1` contract beside it) is the record of every
agent call: `AgentCheck` and `GateCheck` from the state, mapped the same way.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .models import SEVERITIES, AgentCheck, Assessment, Citation, Finding, ReviewState

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


def _assessment(a: Assessment) -> dict:
    return {
        "label": a.label,
        "reason": a.reason,
        "earlier_id": a.earlier_id or None,
        "earlier_round": a.earlier_round or None,
        "earlier_decision": a.earlier_decision or None,
        "earlier_note": a.earlier_note or None,
    }


def _target(state: ReviewState) -> dict:
    t = state.target
    return {
        "slug": t.slug,
        "label": t.label or t.slug,
        "repo_path": t.repo_path,
        "remote_url": t.remote_url,
        "base_sha": t.base_sha,
        "head_sha": t.head_sha,
        "files_changed": list(t.files),
    }


def _timestamp(generated_at: datetime | None) -> str:
    when = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def to_findings_file(state: ReviewState, *, generated_at: datetime | None = None) -> dict:
    rows: list[dict] = []
    for f in state.findings:
        verdict = state.verdicts.get(f.id)
        status = verdict.status if verdict else "unverifiable"
        reason = verdict.reason if verdict else "not verified"
        expected = verdict.expected_behaviour if verdict and status in ("confirmed", "unverifiable") else None
        row = _finding(f, status, reason, expected)
        if verdict:
            row["verifier_citations"] = [_citation(c) for c in verdict.citations]
        assessment = state.assessments.get(f.id)
        if assessment is not None and status in ("confirmed", "unverifiable"):
            row["assessment"] = _assessment(assessment)
        rows.append(row)
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

    return {
        "schema_version": 1,
        "generated_at": _timestamp(generated_at),
        "producer": producer(),
        "target": _target(state),
        "findings": unique,
    }


def _write(data: dict, path: Path) -> Path:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def write_findings_file(state: ReviewState, target_dir: Path) -> Path:
    return _write(to_findings_file(state), target_dir / "findings.json")


# --- checks.json ------------------------------------------------------------------


def _marked(c: dict) -> dict:
    return {"path": c["path"], "line_start": c["line_start"], "line_end": c["line_end"],
            "quote": c["quote"], "gate": c["gate"], "seen": c["seen"]}


def _checked_finding(e: dict) -> dict:
    return {"id": e["id"], "claim": e["claim"], "severity": e["severity"],
            "failure_scenario": e["failure_scenario"], "citations": [_marked(c) for c in e["citations"]],
            "fate": e["fate"], "into": e.get("into"), "rule": e.get("rule")}


def _agent(check: AgentCheck) -> dict:
    verdict = None
    if check.verdict is not None:
        v = check.verdict
        verdict = {"status": v["status"], "reason": v["reason"],
                   "citations": [_marked(c) for c in v["citations"]], "lowered": bool(v["lowered"])}
    return {
        "stage": check.stage, "subject": check.subject, "given": list(check.given),
        "calls": [{"tool": c["tool"], "target": c["target"], "detail": c.get("detail", "")} for c in check.calls],
        "outcome": check.outcome, "turns": check.turns, "cost_usd": round(check.cost_usd, 6),
        "started_at": check.started_at, "ended_at": check.ended_at, "error": check.error,
        "findings": [_checked_finding(e) for e in check.findings], "verdict": verdict,
    }


def to_checks_file(state: ReviewState, *, generated_at: datetime | None = None) -> dict:
    """The `checks.v1` record: every agent in the order it started, then every gate result."""
    agents = sorted(state.checks, key=lambda c: c.started_at)  # stable: ties keep return order
    return {
        "schema_version": 1,
        "generated_at": _timestamp(generated_at),
        "producer": producer(),
        "target": _target(state),
        "agents": [_agent(c) for c in agents],
        "gates": [{"finding_id": g.finding_id, "gate": g.gate, "passed": g.passed, "rule": g.rule}
                  for g in state.gate_checks],
    }


def write_checks_file(state: ReviewState, target_dir: Path) -> Path:
    return _write(to_checks_file(state), target_dir / "checks.json")
