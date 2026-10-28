"""Map a `ReviewState` onto the `findings.json` v1 contract.

The contract is `plugins/multiplai-dev/skills/review-viewer/schema/findings.v1.schema.json`.
Every dict here is built key by key, because the contract rejects unknown keys
and the pipeline's models carry more than it allows.

| pipeline outcome                     | v1 status      |
|--------------------------------------|----------------|
| confirmed                            | `confirmed`    |
| unverifiable (lowered severity kept) | `unverifiable` |
| LOW, no verifier asked (0.112+)      | `unverified`   |
| refuted                              | `refuted`      |
| gate-rejected                        | `rejected`     |

A finding merged into another by the merge or assess stage is not exported:
it is gone from `state.findings`, and the finding it went into carries its
citations. That finding's `absorbed` (optional, 0.94+) lists the id and claim
of every finding merged into it, also through a finding merged into it in
turn, in `state.merged` order; it is written only when there are some.
`expected_behaviour` comes from the verifier and is written for confirmed and
unverifiable findings only; a refuted or rejected finding has none. The same
goes for `assessment`, the assess stage's label (`useful`, `still-open`,
`low-value`, or `repeat` of a finding rejected in an earlier round), which is
optional: files written before the stage existed have none.
`verifier_citations` (optional in v1) holds the lines the verifier read; a
finding with no verdict, such as a gate-rejected one, has no such key.
`topic` (optional) is what the verifier says the finding is about, one of
`models.TOPICS`; it is left out when the verifier gave none. `impact`
(optional, 0.32+) is what the verifier says breaks in production if the
change is merged as is, one of `models.IMPACTS`; a refuted finding has none.
`mode` (top level, optional, 0.32+) is the mode the review ran in: `full` or
`critical`. `section` (0.71+) is the section the finding is listed in
(`models.section_of`: `trim` for what only the trim finder reported, else by
topic), written on every finding so readers need not repeat the rule; files
written before it have none, and readers fall back to the topic rule.

`checks.json` (the `checks.v1` contract beside it) is the record of every
agent call: `AgentCheck` and `GateCheck` from the state, mapped the same way.

`title` and `what_happens` (optional, 0.113+) are the write stage's plain-language
title and text of a shown finding (`state.write_answers`); a finding the stage gave up on
has neither, and readers show its `claim`.

`lineage` (optional, 0.94+) is the id of the finding's first appearance in an
earlier round, through a `still-open` or `repeat` label; it is written only
when it differs from `id`. `earlier` (top level, optional, 0.94+) lists the
final status of each finding an earlier round left open
(`state.earlier_status`), written only when there are some.

`needs` (what the review could not get, each with a command for a person) is
written only when there are some: at the top level every need, and on each
finding the needs that block it. A need that blocked a finding merged away
moves to the finding it was merged into. A file without needs has neither
key, as before.

`need_commands` (top level, optional, 0.124+) is the needs grouped by command
(`need_commands()`): one entry per distinct command, with an id (`N1`, …), the
shortest `what` as its `why`, and the finding ids it bears on. A need with no
command is in no group. On the finding it blocks it is exported as `unchecked`
(optional, 0.124+), a list of its `what`s; one that blocks the review becomes
a coverage record (`need_coverage()`), so it reaches `checks.json`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from . import timings
from .budget import TOKEN_FIELDS, empty_stage
from .checks import add_coverage
from .config import DIMENSIONS
from .models import (
    SEVERITIES,
    STAGES,
    AgentCheck,
    Assessment,
    Citation,
    CoverageRecord,
    EarlierStatus,
    Finding,
    Need,
    NeedCommand,
    ReviewState,
    section_of,
)

log = logging.getLogger(__name__)

# The statuses a finding is shown with; only these carry `expected_behaviour`, `impact` and `assessment`.
SHOWN = ("confirmed", "unverifiable", "unverified")

PLUGIN_JSON = Path(__file__).resolve().parents[4] / ".claude-plugin" / "plugin.json"


def plugin_version() -> str:
    try:
        return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def producer() -> str:
    return f"multiplai-dev:review {plugin_version()}"


def _citation(c: Citation) -> dict:
    return {
        "path": c.path,
        "line_start": c.line_start,
        "line_end": c.line_end,
        "quote": c.quote,
    }


def _finding(f: Finding, status: str, reason: str | None, expected: str | None) -> dict:
    # `paths` only when the finding lies on a mapped path: never null, never [].
    on_paths = {"paths": list(f.paths)} if f.paths else {}
    # `defect_class` and `other_locations` (0.95+) only when the finder named them.
    kind = {"defect_class": f.defect_class} if f.defect_class else {}
    elsewhere = (
        {"other_locations": list(f.other_locations)} if f.other_locations else {}
    )
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
        **on_paths,
        **kind,
        **elsewhere,
        # (0.115+) only on a finding an incremental round copied from an earlier one.
        **({"carried_from": f.carried_from} if f.carried_from else {}),
    }


def absorbed(state: ReviewState) -> dict[str, list[dict]]:
    """The id and claim of every finding merged away, by the id of the finding it ended up in."""
    into = {m.finding.id: m.into for m in state.merged}
    out: dict[str, list[dict]] = {}
    for m in state.merged:
        top = m.into
        while top in into:
            top = into[top]
        out.setdefault(top, []).append({"id": m.finding.id, "claim": m.finding.claim})
    return out


def _need(n: Need) -> dict:
    return {
        "what": n.what,
        "blocks": n.blocks,
        "cause": n.cause,
        "command": n.command,
        "where": n.where,
        "source": n.source,
    }


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


def normalised_command(command: str) -> str:
    """*command* with each run of whitespace made one space, and stripped."""
    return " ".join(command.split())


def need_commands(needs: list[Need]) -> list[NeedCommand]:
    """*needs* grouped by their normalised command, one `NeedCommand` per command, in first-seen order.

    A need with an empty command is in no group.
    """
    groups: dict[str, list[Need]] = {}
    for n in needs:
        command = normalised_command(n.command)
        if command:
            groups.setdefault(command, []).append(n)
    out = []
    for i, (command, group) in enumerate(groups.items(), start=1):
        ids = sorted({n.blocks for n in group} - {"review"})
        out.append(
            NeedCommand(
                id=f"N{i}",
                command=command,
                why=min((n.what for n in group), key=len),
                blocks=ids or ["review"],
                where=next((n.where for n in group if n.where), ""),
                whats=[n.what for n in group],
            )
        )
    return out


NO_COMMAND = (
    "no-command"  # the coverage reason of a need of the review that has no command
)


def need_coverage(state: ReviewState) -> None:
    """Record each need of the review with no command as a coverage record, once.

    Such a need is no task for a person; it says what the review could not
    check. A verifier's need is recorded under `verify`, any other under `find`.
    """
    have = {(c.stage, c.detail) for c in state.coverage if c.reason == NO_COMMAND}
    for n in exported_needs(state):
        if n.blocks != "review" or normalised_command(n.command):
            continue
        stage = "verify" if n.source == "verifier" else "find"
        if (stage, n.what) in have:
            continue
        have.add((stage, n.what))
        add_coverage(
            state,
            CoverageRecord(
                stage=stage,
                subject="need",
                level="info",
                reason=NO_COMMAND,
                detail=n.what,
            ),
        )


def _need_command(c: NeedCommand) -> dict:
    return {
        "id": c.id,
        "command": c.command,
        "why": c.why,
        "blocks": list(c.blocks),
        "where": c.where,
        "whats": list(c.whats),
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
    names -= {
        n for n in names if ":" not in n and any(m.startswith(n + ":") for m in names)
    }
    configured = (state.run_config or {}).get("stages", {})
    stages = []
    for name in sorted(names, key=_stage_order):
        rec = by_stage.get(name) or empty_stage()
        stage = name.partition(":")[0]
        cfg = configured.get(stage, {})
        stages.append(
            {
                "name": name,
                "stage": stage,
                "calls": int(rec.get("calls", 0) or 0),
                "tokens": _tokens(rec),
                "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
                "wall_seconds": round(
                    timings.wall_seconds(state.timings.get(name, [])), 3
                ),
                "model": cfg.get("model", "unknown"),
                "effort": cfg.get("effort", "unknown"),
            }
        )
    runs = state.timings.get("run", [])
    ended = [i.ended_at for i in runs if i.ended_at]
    statuses = [getattr(state.verdicts.get(f.id), "status", "") for f in state.findings]
    labels = [a.label for a in state.assessments.values()]
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
            "unverified": statuses.count("unverified"),
            "merged": len(state.merged),
            "repeats": labels.count("repeat"),
            "low_value": labels.count("low-value"),
        },
        "errors": len(state.errors),
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


def _earlier(e: EarlierStatus) -> dict:
    return {
        "earlier_id": e.earlier_id,
        "lineage": e.lineage,
        "round": e.round,
        "status": e.status,
        "reason": e.reason,
        "citations": [_citation(c) for c in e.citations],
        "error": e.error,
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
    when = (generated_at or datetime.now(UTC)).astimezone(UTC)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def to_findings_file(
    state: ReviewState, *, generated_at: datetime | None = None
) -> dict:
    rows: list[dict] = []
    merged_into = absorbed(state)
    for f in state.findings:
        verdict = state.verdicts.get(f.id)
        status = verdict.status if verdict else "unverifiable"
        reason = verdict.reason if verdict else "not verified"
        expected = verdict.expected_behaviour if verdict and status in SHOWN else None
        row = _finding(f, status, reason, expected)
        if verdict:
            row["verifier_citations"] = [_citation(c) for c in verdict.citations]
            if verdict.topic:
                row["topic"] = verdict.topic
            if verdict.impact and status in SHOWN:
                row["impact"] = verdict.impact
        row["section"] = section_of(verdict.topic if verdict else None, f.finders)
        if f.id in merged_into:
            row["absorbed"] = merged_into[f.id]
        if state.lineage.get(f.id, f.id) != f.id:
            row["lineage"] = state.lineage[f.id]
        written = state.write_answers.get(f.id)
        if written is not None and status in SHOWN:
            row["title"] = written.title
            row["what_happens"] = written.what_happens
        assessment = state.assessments.get(f.id)
        if assessment is not None and status in SHOWN:
            row["assessment"] = _assessment(assessment)
        rows.append(row)
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    order = {"confirmed": 0, "unverifiable": 1, "unverified": 2, "refuted": 3}
    rows.sort(key=lambda r: (order[r["status"]], rank[r["severity"]]))
    for r in state.rejected:
        rows.append(
            {
                **_finding(r.finding, "rejected", r.reason, None),
                "section": section_of(None, r.finding.finders),
            }
        )

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
        # (0.124+) what this finding could not be checked on, with no command to get it.
        unchecked = [m["what"] for m in mine if not normalised_command(m["command"])]
        if unchecked:
            row["unchecked"] = unchecked

    data = {
        "schema_version": 1,
        "generated_at": _timestamp(generated_at),
        "producer": producer(),
        "target": _target(state),
        "findings": unique,
        "run": run_record(state),
        "mode": state.mode,
    }
    if needs:
        data["needs"] = [_need(n) for n in needs]
    commands = need_commands(needs)
    if commands:
        data["need_commands"] = [_need_command(c) for c in commands]
    if state.earlier_status:
        data["earlier"] = [_earlier(e) for e in state.earlier_status]
    if state.scope is not None and state.scope.kind == "incremental":
        # (0.115+) only on an incremental round: what it re-reviewed and what it carried.
        sc = state.scope
        data["scope"] = {
            "kind": sc.kind,
            "since_round": sc.since_round,
            "since_number": sc.since_number,
            "since_head": sc.since_head,
            "reviewed_files": list(sc.reviewed_files),
            "total_files": sc.total_files,
            "carried_ids": list(sc.carried_ids),
        }
    return data


def _write(data: dict, path: Path) -> Path:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def write_findings_file(state: ReviewState, target_dir: Path) -> Path:
    return _write(to_findings_file(state), target_dir / "findings.json")


# --- checks.json ------------------------------------------------------------------


def _marked(c: dict) -> dict:
    return {
        "path": c["path"],
        "line_start": c["line_start"],
        "line_end": c["line_end"],
        "quote": c["quote"],
        "gate": c["gate"],
        "seen": c["seen"],
    }


def _checked_finding(e: dict) -> dict:
    return {
        "id": e["id"],
        "claim": e["claim"],
        "severity": e["severity"],
        "failure_scenario": e["failure_scenario"],
        "citations": [_marked(c) for c in e["citations"]],
        "fate": e["fate"],
        "into": e.get("into"),
        "rule": e.get("rule"),
    }


def _agent(check: AgentCheck) -> dict:
    verdict = None
    if check.verdict is not None:
        v = check.verdict
        verdict = {
            "status": v["status"],
            "reason": v["reason"],
            "citations": [_marked(c) for c in v["citations"]],
            "lowered": bool(v["lowered"]),
        }
    # A convert call's items (kind, severity, finding id): the only place an item's kind is written.
    items = (
        {}
        if check.items is None
        else {
            "items": [
                {
                    "kind": i["kind"],
                    "severity": i["severity"],
                    "finding_id": i.get("finding_id"),
                }
                for i in check.items
            ]
        }
    )
    return {
        "stage": check.stage,
        "subject": check.subject,
        "given": list(check.given),
        "calls": [
            {"tool": c["tool"], "target": c["target"], "detail": c.get("detail", "")}
            for c in check.calls
        ],
        "outcome": check.outcome,
        "turns": check.turns,
        "cost_usd": round(check.cost_usd, 6),
        "started_at": check.started_at,
        "ended_at": check.ended_at,
        "error": check.error,
        "findings": [_checked_finding(e) for e in check.findings],
        "verdict": verdict,
        **items,
    }


def to_checks_file(state: ReviewState, *, generated_at: datetime | None = None) -> dict:
    """The `checks.v1` record: every agent in the order it started, then every gate result."""
    agents = sorted(
        state.checks, key=lambda c: c.started_at
    )  # stable: ties keep return order
    return {
        "schema_version": 1,
        "generated_at": _timestamp(generated_at),
        "producer": producer(),
        "target": _target(state),
        "agents": [_agent(c) for c in agents],
        "gates": [
            {
                "finding_id": g.finding_id,
                "gate": g.gate,
                "passed": g.passed,
                "rule": g.rule,
            }
            for g in state.gate_checks
        ],
        # What a stage could not cover (`checks.add_coverage`), in the order it was recorded.
        "coverage": [c.model_dump(mode="json") for c in state.coverage],
    }


def write_checks_file(state: ReviewState, target_dir: Path) -> Path:
    return _write(to_checks_file(state), target_dir / "checks.json")
