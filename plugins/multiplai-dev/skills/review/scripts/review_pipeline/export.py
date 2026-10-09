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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import timings
from .budget import TOKEN_FIELDS, empty_stage
from .config import DIMENSIONS
from .models import SEVERITIES, STAGES, Citation, Finding, ReviewState

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


def _tokens(rec: dict) -> dict:
    values = {f: int(rec.get(f, 0) or 0) for f in TOKEN_FIELDS}
    return {
        "input": values["input_tokens"],
        "output": values["output_tokens"],
        "cache_read": values["cache_read_tokens"],
        "cache_write": values["cache_creation_tokens"],
        "total": sum(values.values()),
    }


def _stage_order(name: str) -> tuple[int, int, str]:
    stage, _, dimension = name.partition(":")
    stage_i = STAGES.index(stage) if stage in STAGES else len(STAGES)
    dim_i = DIMENSIONS.index(dimension) if dimension in DIMENSIONS else len(DIMENSIONS)
    return (stage_i, dim_i, name)


def run_record(state: ReviewState) -> dict:
    """The `run` object: cost, tokens, time and models, from the final state only.

    One row per budget label (`find:<dimension>`, `verify`, `merge`). A stage's
    own interval stands in for its rows' time unless the stage has per-label
    intervals, as the finders do.
    """
    ledger = state.budget or {}
    by_stage: dict[str, dict] = ledger.get("by_stage") or {}
    names = set(by_stage) | {k for k in state.timings if k != "run"}
    # `find` has a row per finder; its stage-level interval would repeat them.
    names -= {n for n in names if ":" not in n and any(m.startswith(n + ":") for m in names)}
    configured = (state.run_config or {}).get("stages", {})
    stages = []
    for name in sorted(names, key=_stage_order):
        rec = by_stage.get(name) or empty_stage()
        stage = name.partition(":")[0]
        cfg = configured.get(stage, {})
        stages.append({
            "name": name,
            "stage": stage,
            "calls": int(rec.get("calls", 0) or 0),
            "tokens": _tokens(rec),
            "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
            "wall_seconds": round(timings.wall_seconds(state.timings.get(name, [])), 3),
            "model": cfg.get("model", "unknown"),
            "effort": cfg.get("effort", "unknown"),
        })
    runs = state.timings.get("run", [])
    ended = [i.ended_at for i in runs if i.ended_at]
    statuses = [getattr(state.verdicts.get(f.id), "status", "") for f in state.findings]
    return {
        "started_at": runs[0].started_at if runs else None,
        "ended_at": ended[-1] if ended else None,
        "wall_seconds": round(timings.wall_seconds(runs), 3),
        "calls": int(ledger.get("calls", 0) or 0),
        "tokens": _tokens(ledger),
        "cost_usd": round(float(ledger.get("cost_usd", 0.0) or 0.0), 6),
        "max_usd": ledger.get("max_usd"),
        "stopped_by_budget": state.budget_stops > 0,
        "stages": stages,
        "counts": {
            "found": len(state.findings) + len(state.merged) + len(state.rejected),
            "rejected": len(state.rejected),
            "refuted": statuses.count("refuted"),
            "unverifiable": statuses.count("unverifiable"),
            "merged": len(state.merged),
        },
        "errors": len(state.errors),
    }


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

    when = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
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
        "run": run_record(state),
    }


def write_findings_file(state: ReviewState, target_dir: Path) -> Path:
    path = target_dir / "findings.json"
    data = to_findings_file(state)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
