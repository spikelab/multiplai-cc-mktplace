"""status.py: every stop state, the documents, the build and the epic."""

from __future__ import annotations

import fcntl
import json
import os
import socket

from helpers import ENG, PM, git, place, roles, status_fn, stop
from pipeline_viewer import config, record, status
from pipeline_viewer.places import list_changes


def test_lists_every_change_with_its_write_checkout(shared_fx):
    found = list_changes(shared_fx["repo"], shared_fx["wt"])
    assert sorted(found) == [
        "audit-log", "dark-mode", "export-csv", "help-page", "rate-limit", "search-box",
    ]  # fmt: skip
    assert found["export-csv"].write.kind == "pm-spec"
    assert found["rate-limit"].write.kind == "build"
    assert found["export-csv"].branch == "TCK-1-export-csv"


def test_another_repos_worktree_under_the_root_is_not_listed(fx, tmp_path):
    # A second repo whose worktree shares the worktrees root with fx's repo.
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", "-b", "main", str(other))
    git(other, "config", "user.name", "Other Runner")
    git(other, "config", "user.email", "other@example.org")
    git(other, "config", "commit.gpgsign", "false")
    (other / "README.md").write_text("# Other\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "initial")
    git(other, "branch", "TCK-9-elsewhere")
    wt = fx["wt"] / "pm-spec-TCK-9-elsewhere"
    git(other, "worktree", "add", "-q", "--detach", str(wt), "TCK-9-elsewhere")
    d = wt / "specs" / "changes" / "elsewhere"
    d.mkdir(parents=True)
    (d / ".change.yaml").write_text("branch: TCK-9-elsewhere\n", encoding="utf-8")

    found = list_changes(fx["repo"], fx["wt"])
    assert "elsewhere" not in found
    # fx's own detached pm-spec worktrees are still found.
    assert found["export-csv"].write.kind == "pm-spec"


def test_waiting_stop_at_each_stage(shared_fx):
    expect = {
        "export-csv": (1, ["Pat Planner"]),
        "search-box": (2, ["Pat Planner"]),
        "dark-mode": (3, ["Pat Planner", "Eli Engineer"]),
        "rate-limit": (4, ["Eli Engineer"]),
        "help-page": (5, ["Eli Engineer"]),
    }
    for name, (n, who) in expect.items():
        st = status_fn(shared_fx, name)[1]()
        assert st.current_stop == n, name
        assert st.waiting_on == who, name
        assert st.stops[n - 1].state == "waiting"
        assert all(s.state == "not_reached" for s in st.stops[n:]), name
        assert all(s.state == "approved" for s in st.stops[: n - 1]), name


def test_waiting_stop_has_revision_and_deadlines(shared_fx):
    s = stop(shared_fx, "export-csv", 1)
    assert s.revision and len(s.revision) == 40
    since = status.parse_utc(s.waiting_since)
    assert status.parse_utc(s.ack_by) - since == status.timedelta(minutes=60)
    assert status.parse_utc(s.decide_at) - since == status.timedelta(minutes=240)
    assert [d.path for d in s.docs] == ["proposal.md"]


def test_stop_4_revision_is_buildme_plan_revision(shared_fx):
    s = stop(shared_fx, "rate-limit", 4)
    assert s.revision == "planrev4"
    assert [(d.path, d.approve) for d in s.docs] == [
        ("implementation-plan.md", True),
        ("tasks.md", False),
        ("plan-approval.md", False),
    ]


def test_stop_4_falls_back_to_design_md(shared_fx):
    s = stop(shared_fx, "audit-log", 4)
    assert s.docs[0].path == "design.md"
    assert s.state == "approved"  # plan_approval saved for the current revision


def test_stop_5_has_pr_and_no_decide_time(shared_fx):
    s = stop(shared_fx, "help-page", 5)
    assert s.pr_url == "https://example.org/pulls/7"
    assert s.decide_at is None and s.ack_by is None


def test_acknowledged_holds_the_wait(fx):
    p, fresh = status_fn(fx, "export-csv")
    record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    s = fresh().stops[0]
    assert s.state == "acknowledged"
    assert s.acknowledged_by == ["Pat Planner"]
    assert s.decide_at is None and s.ack_by is not None


def test_approved_and_decided_and_rework(fx):
    p, fresh = status_fn(fx, "export-csv")
    record.approve(p, fresh, PM, 1, roles(fx, 1))
    s = fresh().stops[0]
    assert (s.state, s.approved_by, s.waiting_on) == ("approved", ["Pat Planner"], [])

    p, fresh = status_fn(fx, "search-box")
    record.decided(p, fresh, 2, "no reply in 240 minutes")
    s = fresh().stops[1]
    assert (s.state, s.decided_by) == ("decided_by_agent", "agent")
    assert s.records[-1].reason == "no reply in 240 minutes"

    p, fresh = status_fn(fx, "rate-limit")
    record.rework(
        p, fresh, ENG, 4, roles(fx, 4), "Split the bucket", "Use two buckets."
    )
    s = fresh().stops[3]
    assert (s.state, s.rework_by) == ("rework_requested", ["Eli Engineer"])


def test_stop_3_needs_both_roles(fx):
    p, fresh = status_fn(fx, "dark-mode")
    record.approve(p, fresh, PM, 3, roles(fx, 3))
    s = fresh().stops[2]
    assert s.state == "waiting"
    assert s.approved_by == ["Pat Planner"] and s.waiting_on == ["Eli Engineer"]
    record.approve(p, fresh, ENG, 3, roles(fx, 3))
    assert fresh().stops[2].state == "approved"


def test_an_old_revision_approval_does_not_count(fx):
    p, fresh = status_fn(fx, "export-csv")
    record.approve(p, fresh, PM, 1, roles(fx, 1))
    wt = fx["export-csv"]
    doc = wt / "specs/changes/export-csv/proposal.md"
    doc.write_text(doc.read_text() + "\n## Added\n\nMore.\n")
    git(wt, "commit", "-qam", "specs(export-csv): revise the proposal")
    s = fresh().stops[0]
    assert s.state == "waiting" and s.approved_by == []


def test_build_view(shared_fx):
    b = status_fn(shared_fx, "audit-log")[1]().build
    assert (b.phase, b.blocks_done, b.blocks_total, b.elapsed_s) == (
        "tdd_build",
        1,
        3,
        7200.0,
    )
    assert (b.spent_usd, b.max_usd) == (4.5, 20.0)
    one, two, _ = b.blocks
    assert one.review_rounds == ["reviews/block-01-round-1.json"]
    assert two.timed_out and two.fix_pass
    assert b.progress_tail[-1] == "Phase started"


def test_completed_build(shared_fx):
    b = status_fn(shared_fx, "help-page")[1]().build
    assert b.complete and b.pr_url


def test_epic_from_latest_summary(shared_fx, tmp_path):
    epic = tmp_path / "epic"
    for n, status_ in (("01", "failed"), ("02", "waiting")):
        (epic / "runs" / n).mkdir(parents=True)
        (epic / "runs" / n / "summary.json").write_text(
            json.dumps(
                {
                    "children": [
                        {
                            "id": "api",
                            "status": status_,
                            "pr": None,
                            "rounds": 0,
                            "reason": "",
                        }
                    ],
                    "totals": {status_: 1},
                    "spent_usd": 2.0,
                }
            )
        )
    (epic / "agents").mkdir()
    (epic / "agents" / "001-build-api.started.json").write_text(
        '{"kind": "build", "child": "api"}'
    )
    (epic / "agents" / "001-build-api.result.json").write_text('{"exit_code": 3}')
    st = status_fn(shared_fx, "audit-log", lambda repo, name: epic)[1]()
    assert st.epic.run == "02"
    assert st.epic.children[0].status == "waiting"
    assert st.epic.agents[0].model_dump() == {
        "n": 1, "kind": "build", "child": "api", "started_at": None, "exit_code": 3, "finished": True,
    }  # fmt: skip


def test_run_lock_held_means_running(fx):
    p = place(fx, "export-csv")
    lock = fx["export-csv"] / "specs/changes/export-csv/.pm-spec/run.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(
            fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
        )
        assert status.pm_spec_running(p)
    finally:
        os.close(fd)
    # Released: the file and its pid stay, but nobody holds the lock.
    assert not status.pm_spec_running(p)


def test_config_reviewers_and_overrides():
    cfg = config.parse(
        {
            "reviewers": {"pm": ["A"], "engineer": ["B"]},
            "stops": {3: {"reviewers": ["engineer"], "decide_timeout_minutes": 30}},
            "ack_timeout_minutes": 10,
            "slack": {"channel": "C1"},
        }
    )
    assert cfg.stop(3).roles == ("engineer",)
    assert cfg.stop(3).decide_timeout_minutes == 30
    assert cfg.stop(3).ack_timeout_minutes == 10
    assert cfg.stop(1).roles == ("pm",)
    assert cfg.stop(1).decide_timeout_minutes == 480
    assert [r.name for r in cfg.reviewers_of(3, {})] == ["B"]
    assert cfg.reviewers_of(3, {})[0].invited is False
    assert cfg.slack_channel == "C1"


def test_config_errors_name_the_key():
    import pytest

    for bad, word in (
        ({"stops": {7: {}}}, "stop number"),
        ({"reviewers": {"boss": []}}, "boss"),
        ({"ack_timeout_minutes": 0}, "ack_timeout_minutes"),
        ({"nope": 1}, "nope"),
    ):
        with pytest.raises(config.ConfigError, match=word):
            config.parse(bad)
