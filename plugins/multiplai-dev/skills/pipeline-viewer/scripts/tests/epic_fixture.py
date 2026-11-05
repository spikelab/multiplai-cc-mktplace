"""A scratch data dir with buildme epic records for one fixture repo: two
epics of mixed buildme versions and causes, and the shared `runs.jsonl`.
All content is invented.

    <data>/buildme/epics/<owner>-<repo>--shop/   the old folder (`legacy_epic_dir`)
        runs/01  no version; a merged, b parked (review cap), c skipped (dependency)
        runs/02  no version; b parked (merge conflict), c skipped again, d failed (exit 1)
        runs/03  0.156.0, causes recorded; b merged, c parked (spec issues), d failed as in 02
    <data>/changes/<repo>/TCK-9-cart/buildme/    the new folder (`change_folder`)
        runs/01  0.157.0; x parked (merge tests failed), also a row in runs.jsonl
    <data>/runs.jsonl
        the epic row of cart run 01; a failed single build of help-page (0.157.0);
        a review row, an epic row of another repo (both left out); a cut line
        and a row of an unknown row_version (both counted as skipped)
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline_viewer import buildme

ROW_PRODUCER = "multiplai-dev:buildme 0.157.0"


def _child(cid, status, reason="", pr=None, cause=None, recorded=False) -> dict:
    c = {"id": cid, "status": status, "pr": pr, "rounds": 0, "reason": reason}
    if recorded:
        c["cause"] = cause
    return c


def _summary(run_dir: Path, children: list[dict], spent: float, **extra) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    for c in children:
        totals[c["status"]] = totals.get(c["status"], 0) + 1
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "children": children,
                "totals": totals,
                "spent_usd": spent,
                "spend_complete": True,
                **extra,
            }
        )
    )


def legacy_dir(data: Path, repo: Path, change: str) -> Path:
    return data / "buildme" / "epics" / f"{buildme.legacy_prefix(repo)}{change}"


def new_dir(data: Path, repo: Path, branch: str) -> Path:
    return data / "changes" / buildme.repo_folder(repo) / branch / "buildme"


def build(data: Path, repo: Path) -> dict:
    """Write the records above under *data*; returns their folders."""
    shop = legacy_dir(data, repo, "shop")
    shop.mkdir(parents=True)
    (shop / "epic.json").write_text(json.dumps({"name": "shop", "children": []}))
    _summary(
        shop / "runs" / "01",
        [
            _child("a", "merged", pr=1),
            _child("b", "parked", "review round 3 of 3 left 2 blocker(s): MEDIUM x", 2),
            _child("c", "skipped", "dependency b parked"),
        ],
        10.0,
    )
    _summary(
        shop / "runs" / "02",
        [
            _child("a", "merged", pr=1),
            _child("b", "parked", "merging origin/f conflicts in app.py, which", 2),
            _child("c", "skipped", "dependency b parked"),
            _child("d", "failed", "exit 1"),
        ],
        25.0,
    )
    _summary(
        shop / "runs" / "03",
        [
            _child("a", "merged", pr=1, recorded=True),
            _child("b", "merged", pr=2, recorded=True),
            _child("c", "parked", "SPEC:ISSUES:/x", None, "spec-issues", True),
            _child("d", "failed", "exit 1", None, "exit-code", True),
        ],
        40.0,
        buildme_version="0.156.0",
        started_at="2026-01-06T09:00:00Z",
        finished_at="2026-01-06T10:00:00Z",
    )
    cart = new_dir(data, repo, "TCK-9-cart")
    cart.mkdir(parents=True)
    (cart / "epic.json").write_text(json.dumps({"name": "cart", "children": []}))
    cart_children = [
        _child(
            "x", "parked", "merge tests failed: pytest", 5, "merge-tests-failed", True
        )
    ]
    _summary(
        cart / "runs" / "01",
        cart_children,
        7.0,
        buildme_version="0.157.0",
        started_at="2026-01-07T09:00:00Z",
        finished_at="2026-01-07T09:30:00Z",
    )
    name = buildme.repo_folder(repo)
    rows = [
        {
            "row_version": 1,
            "producer": ROW_PRODUCER,
            "kind": "epic",
            "target": {"repo": name, "branch": "TCK-9-cart", "change": "cart"},
            "folder": cart.relative_to(data).as_posix(),
            "requested_by": None,
            "started_at": "2026-01-07T09:00:00Z",
            "finished_at": "2026-01-07T09:30:00Z",
            "outcome": "parked",
            "cause": None,
            "spend_usd": 7.0,
            "run": {
                "run": "01",
                "children": cart_children,
                "totals": {"parked": 1},
                "spent_usd_total": 7.0,
                "spend_complete": True,
            },
        },
        {
            "row_version": 1,
            "producer": ROW_PRODUCER,
            "kind": "build",
            "target": {
                "repo": name,
                "branch": "TCK-3-help-page",
                "change": "help-page",
            },
            "folder": None,
            "requested_by": None,
            "started_at": "2026-01-08T09:00:00Z",
            "finished_at": "2026-01-08T09:05:00Z",
            "outcome": "failed",
            "cause": "SPEC:NOT_READY",
            "spend_usd": 3.0,
            "run": {"exit_code": 1, "spend_complete": True},
        },
        {
            "row_version": 1,
            "producer": "multiplai-dev:review 0.47.0",
            "kind": "review",
            "target": {"repo": name, "pr": 5},
            "finished_at": "2026-01-08T10:00:00Z",
            "outcome": "succeeded",
            "run": {},
        },
        {
            "row_version": 1,
            "producer": ROW_PRODUCER,
            "kind": "epic",
            "target": {"repo": "someone-else", "change": "cart"},
            "folder": "changes/someone-else/TCK-1-cart/buildme",
            "finished_at": "2026-01-08T10:00:00Z",
            "outcome": "failed",
            "run": {"run": "01", "children": []},
        },
    ]
    lines = [json.dumps(r) for r in rows]
    lines.append('{"row_version": 2, "producer": "multiplai-dev:buildme 9.0.0"}')
    lines.append('{"row_version": 1, "produc')
    (data / "runs.jsonl").write_text("\n".join(lines) + "\n")
    return {"shop": shop, "cart": cart, "data": data}
