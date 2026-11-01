"""Stop 4 for a change with components: one implementation plan for the
whole change, on its feature branch, approved before any component builds
(buildme's `epic.change_plan`)."""

from __future__ import annotations

import pytest
from fixture_repo import CHANGE_PLAN, change_plan_change, git
from helpers import ENG, PM, roles, status_fn
from pipeline_viewer import record, status


def _stop4(fresh):
    return fresh().stops[3]


def test_the_change_plan_waits_at_stop_4_with_its_documents(fx):
    feature = change_plan_change(fx)
    p, fresh = status_fn(fx, "billing")
    assert p.feature() is not None and p.feature().checkout == feature
    s = _stop4(fresh)
    assert s.state == "waiting"
    assert s.revision == "changerev1"
    assert [(d.path, d.approve, d.exists) for d in s.docs] == [
        ("implementation-plan.md", True, True),
        ("components.md", False, True),
        ("plan-approval.md", False, True),
    ]
    assert s.waiting_on == ["Eli Engineer"]
    assert s.waiting_since is not None
    assert fresh().current_stop == 4


def test_pm_specs_worktree_is_older_than_the_plan_and_is_not_read_for_it(fx):
    """pm-spec's worktree was made before buildme pushed the plan; the stop
    is read from buildme's feature worktree."""
    change_plan_change(fx)
    p, _ = status_fn(fx, "billing")
    assert p.write.kind == "pm-spec"
    assert not (p.write.change_dir("billing") / "implementation-plan.md").exists()
    assert status.change_plan_dir(p) == p.feature().change_dir("billing")


def test_approving_commits_on_the_feature_branch_with_the_requested_revision(fx):
    change_plan_change(fx)
    p, fresh = status_fn(fx, "billing")
    res = record.approve(p, fresh, ENG, 4, roles(fx, 4))
    assert res.pushed
    trailers = git(fx["origin"], "log", "-1", "--format=%(trailers)", "TCK-3-billing")
    assert "Stop: 4" in trailers
    assert "Revision: changerev1" in trailers
    assert "Approved-by: Eli Engineer" in trailers
    # The write checkout was brought up to the plan's commit before it.
    assert git(fx["origin"], "rev-parse", "TCK-3-billing^") != ""
    assert (p.write.change_dir("billing") / "implementation-plan.md").read_text() == (
        CHANGE_PLAN
    )
    assert _stop4(fresh).state == "approved"


def test_an_approval_buildme_committed_in_its_feature_worktree_counts(fx):
    feature = change_plan_change(fx)
    git(
        feature, "-c", "user.name=Eli Engineer", "-c", "user.email=eli@example.org",
        "commit", "-q", "--allow-empty",
        "-m", "review(billing): Eli Engineer approved the implementation plan",
        "--trailer", "Stop: 4", "--trailer", "Revision: changerev1",
        "--trailer", "Approved-by: Eli Engineer",
    )  # fmt: skip
    _, fresh = status_fn(fx, "billing")
    assert _stop4(fresh).state == "approved"


def test_an_approval_of_an_older_revision_does_not_count(fx):
    feature = change_plan_change(fx)
    git(
        feature, "-c", "user.name=Eli Engineer", "-c", "user.email=eli@example.org",
        "commit", "-q", "--allow-empty",
        "-m", "review(billing): Eli Engineer approved the implementation plan",
        "--trailer", "Stop: 4", "--trailer", "Revision: changerev0",
        "--trailer", "Approved-by: Eli Engineer",
    )  # fmt: skip
    _, fresh = status_fn(fx, "billing")
    assert _stop4(fresh).state == "waiting"


def test_rework_writes_the_request_into_the_change(fx):
    change_plan_change(fx)
    p, fresh = status_fn(fx, "billing")
    res = record.rework(
        p, fresh, ENG, 4, roles(fx, 4), "Split the store", "One store per region."
    )
    assert res.path == "specs/changes/billing/review/stop-4.md"
    text = git(fx["origin"], "show", f"TCK-3-billing:{res.path}")
    assert "Split the store" in text and "> One store per region." in text
    assert _stop4(fresh).state == "rework_requested"


def test_a_plan_written_under_auto_has_passed_stop_4(fx):
    change_plan_change(fx, request=False)
    _, fresh = status_fn(fx, "billing")
    s = _stop4(fresh)
    assert s.state == "approved" and s.approved_by == []


def test_a_change_with_components_and_no_plan_has_not_reached_stop_4(fx):
    _, fresh = status_fn(fx, "dark-mode")
    assert _stop4(fresh).state == "not_reached"


def test_a_pm_cannot_approve_stop_4(fx):
    change_plan_change(fx)
    p, fresh = status_fn(fx, "billing")
    with pytest.raises(record.Refused):
        record.approve(p, fresh, PM, 4, roles(fx, 4))
