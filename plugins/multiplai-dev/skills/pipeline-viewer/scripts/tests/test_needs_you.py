"""status.needs_you, github.review_threads and GET /api/needs-you: one list
of everything waiting on a person in the server's repo."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest
from conftest import PM_TOKEN, people_map
from helpers import place
from pipeline_viewer import buildme, config, github, places, status
from test_server import _serve

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
PR = "https://github.com/acme/shop/pull/{}"


def _epic(root, runs: dict[str, list[dict]], finished="2026-01-09T08:00:00Z"):
    for n, children in runs.items():
        (root / "runs" / n).mkdir(parents=True)
        (root / "runs" / n / "summary.json").write_text(
            json.dumps({"children": children, "finished_at": finished})
        )
    (root / "agents").mkdir(exist_ok=True)
    return root


def _review(root, n: int, child: str, exit_code=0, blocking=()):
    (root / "agents" / f"{n:03d}-review-{child}.started.json").write_text(
        json.dumps({"kind": "review", "child": child})
    )
    (root / "agents" / f"{n:03d}-review-{child}.result.json").write_text(
        json.dumps({"exit_code": exit_code, "blocking": list(blocking)})
    )


def _items(fx):
    cfg = config.load(fx["repo"])
    out = []
    for p in places.list_changes(fx["repo"], fx["wt"]).values():
        out.append((p, status.change_status(p, cfg, people_map())))
    return out, cfg


def _needs(fx, epic=None, merge="manual"):
    items, cfg = _items(fx)
    return status.needs_you(
        items,
        cfg,
        people_map(),
        epic_dir_for=(lambda repo, name: epic) if epic else None,
        cause_of=buildme.cause_of,
        merge_mode=lambda repo, branch: merge,
        now=NOW,
    )


def test_stops_waiting_for_approval(shared_fx):
    rows = [r for r in _needs(shared_fx).rows if r.kind == "stop"]
    assert sorted((r.change, r.stop) for r in rows) == [
        ("dark-mode", 3),
        ("export-csv", 1),
        ("rate-limit", 4),
        ("search-box", 2),
    ]
    one = next(r for r in rows if r.change == "export-csv")
    assert [(w.name, w.role) for w in one.waiting_on] == [("Pat Planner", "pm")]
    assert one.link == "#change=export-csv&stop=1"
    # The fixture's commits are made now; NOW is a fixed date, so the wait is
    # whatever lies between them.
    since = datetime.fromisoformat(one.since)
    assert one.waited_s == (NOW - since).total_seconds()
    three = next(r for r in rows if r.change == "dark-mode")
    assert {w.role for w in three.waiting_on} == {"pm", "engineer"}


def test_parked_children_of_the_latest_run(shared_fx, tmp_path):
    epic = _epic(
        tmp_path / "epic",
        {
            "01": [{"id": "old", "status": "parked", "reason": "exit 1"}],
            "02": [
                {"id": "api", "status": "merged", "pr": PR.format(1)},
                {
                    "id": "ui",
                    "status": "parked",
                    "pr": PR.format(2),
                    "reason": "merging origin/f conflicts in a.py, which",
                },
                {
                    "id": "docs",
                    "status": "parked",
                    "reason": "SPEC:ISSUES:/x/spec-issues.md",
                    "cause": "spec-issues",
                },
                {"id": "cli", "status": "skipped", "reason": "dependency ui parked"},
            ],
        },
    )
    rows = [r for r in _needs(shared_fx, epic).rows if r.kind == "parked"]
    # Every change of the fixture reads the same epic dir here: one per change.
    audit = sorted(
        (r.child, r.cause, r.inferred) for r in rows if r.change == "audit-log"
    )
    assert audit == [("docs", "spec-issues", False), ("ui", "merge-conflict", True)]
    ui = next(r for r in rows if r.change == "audit-log" and r.child == "ui")
    assert ui.url == PR.format(2) and ui.since == "2026-01-09T08:00:00Z"
    assert ui.reason.startswith("merging origin/f")
    assert ui.link == "#change=audit-log"
    assert {w.role for w in ui.waiting_on} == {"engineer"}
    docs = next(r for r in rows if r.change == "audit-log" and r.child == "docs")
    assert {w.role for w in docs.waiting_on} == {"pm"}


def test_child_pr_waiting_for_a_manual_merge(shared_fx, tmp_path):
    epic = _epic(
        tmp_path / "epic",
        {
            "01": [
                {"id": "api", "status": "built", "pr": PR.format(3)},
                {"id": "ui", "status": "built", "pr": PR.format(4)},
            ]
        },
    )
    _review(epic, 1, "api")
    _review(epic, 2, "ui", exit_code=0)
    _review(epic, 3, "ui", exit_code=0, blocking=["abc"])  # the last one has a blocker
    rows = [r for r in _needs(shared_fx, epic).rows if r.kind == "merge"]
    assert {(r.change, r.child, r.url) for r in rows if r.change == "audit-log"} == {
        ("audit-log", "api", PR.format(3))
    }
    assert not [
        r for r in _needs(shared_fx, epic, merge="auto").rows if r.kind == "merge"
    ]


def test_open_spec_issues(fx):
    d = place(fx, "export-csv").read_dir()
    (d / "spec-issues.md").write_text("# Spec issues\n\n## A thing is unclear\n")
    meta = (d / ".change.yaml").read_text()
    (d / ".change.yaml").write_text(meta + "spec_issues: spec-issues.md\n")
    rows = [r for r in _needs(fx).rows if r.kind == "spec-issues"]
    assert [(r.change, [w.role for w in r.waiting_on]) for r in rows] == [
        ("export-csv", ["pm"])
    ]
    assert "pm-spec revise" in rows[0].what


def test_oldest_first(shared_fx):
    rows = _needs(shared_fx).rows
    since = [r.since for r in rows if r.since]
    assert since == sorted(since)


def test_child_prs_to_read_on_github(shared_fx, tmp_path):
    epic = _epic(
        tmp_path / "epic",
        {
            "01": [
                {"id": "api", "status": "merged", "pr": PR.format(1)},
                {"id": "ui", "status": "parked", "pr": PR.format(2)},
                {"id": "cli", "status": "built", "pr": "7"},
            ]
        },
    )
    items, _ = _items(shared_fx)
    prs = status.child_prs(items, lambda repo, name: epic)
    assert {(c, child, pr) for c, child, pr in prs if c == "audit-log"} == {
        ("audit-log", "ui", PR.format(2)),
        ("audit-log", "cli", "7"),
    }


# --- GitHub review threads ----------------------------------------------------


def _threads(*resolved_and_dates):
    nodes = [
        {
            "isResolved": done,
            "comments": {"nodes": [{"createdAt": at, "url": "https://x/c"}]},
        }
        for done, at in resolved_and_dates
    ]
    return json.dumps(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
    )


class Runner:
    def __init__(self, out="", code=0, exc=None):
        self.calls, self.out, self.code, self.exc = [], out, code, exc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, self.out, "boom")


def test_unresolved_threads_counted_with_the_oldest():
    run = Runner(
        _threads(
            (False, "2026-01-08T10:00:00Z"),
            (True, "2026-01-01T10:00:00Z"),
            (False, "2026-01-07T10:00:00Z"),
        )
    )
    reader = github.ThreadReader(run=run, clock=lambda: 100.0)
    res = reader.unresolved(PR.format(5), default_repo=None)
    assert (res.count, res.oldest) == (2, "2026-01-07T10:00:00Z")
    assert run.calls[0][:3] == ["gh", "api", "graphql"]
    assert "owner=acme" in run.calls[0] and "number=5" in run.calls[0]


def test_threads_cached_for_60_seconds():
    now = [100.0]
    run = Runner(_threads((False, "2026-01-08T10:00:00Z")))
    reader = github.ThreadReader(run=run, clock=lambda: now[0])
    reader.unresolved(PR.format(5), default_repo=None)
    now[0] = 159.0
    reader.unresolved(PR.format(5), default_repo=None)
    assert len(run.calls) == 1
    now[0] = 161.0
    reader.unresolved(PR.format(5), default_repo=None)
    assert len(run.calls) == 2


def test_a_bare_number_uses_the_repo_of_origin():
    run = Runner(_threads())
    reader = github.ThreadReader(run=run, clock=lambda: 0.0)
    assert reader.unresolved("7", default_repo=("acme", "shop")).count == 0
    assert "number=7" in run.calls[0] and "name=shop" in run.calls[0]
    with pytest.raises(github.GitHubError):
        reader.unresolved("7", default_repo=None)


@pytest.mark.parametrize(
    "runner",
    [Runner(code=1), Runner(out="not json"), Runner(exc=FileNotFoundError("gh"))],
)
def test_github_failure_is_an_error_not_a_crash(runner):
    reader = github.ThreadReader(run=runner, clock=lambda: 0.0)
    with pytest.raises(github.GitHubError):
        reader.unresolved(PR.format(5), default_repo=None)


def test_thread_rows_and_the_list_without_github(shared_fx, tmp_path):
    epic = _epic(
        tmp_path / "epic",
        {"01": [{"id": "ui", "status": "parked", "pr": PR.format(2)}]},
    )
    items, cfg = _items(shared_fx)
    needs = _needs(shared_fx, epic)
    ok = github.ThreadReader(
        run=Runner(_threads((False, "2026-01-08T10:00:00Z"))), clock=lambda: 0.0
    )
    github.add_thread_rows(
        needs,
        status.child_prs(items, lambda r, n: epic),
        ok,
        cfg,
        people_map(),
        None,
        NOW,
    )
    rows = [r for r in needs.rows if r.kind == "review-threads"]
    assert rows and rows[0].count == 1 and rows[0].url == PR.format(2)
    assert needs.github.checked and needs.github.error is None
    bad = _needs(shared_fx, epic)
    github.add_thread_rows(
        bad,
        status.child_prs(items, lambda r, n: epic),
        github.ThreadReader(run=Runner(code=1), clock=lambda: 0.0),
        cfg,
        people_map(),
        None,
        NOW,
    )
    assert not bad.github.checked and bad.github.error
    assert [r.kind for r in bad.rows].count("parked") == len(
        [r for r in needs.rows if r.kind == "parked"]
    )


# --- the route ---------------------------------------------------------------


def test_needs_you_route(shared_fx):
    viewer, client = _serve(shared_fx)
    viewer.thread_reader = github.ThreadReader(
        run=Runner(_threads()), clock=lambda: 0.0
    )
    try:
        code, body = client.get("/api/needs-you", token=PM_TOKEN)
        assert code == 200
        assert {r["kind"] for r in body["rows"]} == {"stop"}
        assert body["repo"] == shared_fx["repo"].name
        assert body["github"]["checked"] is True and body["generated_at"]
        code, _ = client.post("/api/needs-you", {}, token=PM_TOKEN)
        assert code == 405
    finally:
        viewer.httpd.shutdown()
        viewer.httpd.server_close()


def test_merge_mode_as_buildme_decides(fx):
    repo = fx["repo"]
    assert buildme.merge_mode(repo, "main") == "manual"  # origin's default branch
    assert buildme.merge_mode(repo, "TCK-9-feature") == "auto"
    cfg = repo / "specs" / "config.yaml"
    cfg.write_text(cfg.read_text() + "epic:\n  merge: manual\n")
    assert buildme.merge_mode(repo, "TCK-9-feature") == "manual"
