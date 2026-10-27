"""The consistency check's later rounds after the specs review: the recheck
rule, accepted findings, the round cap and the warning on unfixed findings."""

from __future__ import annotations

from conftest import change_dir, git, run, state, write
from pm_spec_pipeline import git_ops, layout
from test_resume_rows import CONTRADICTION, FLOW, NIT, RESUME, go, reviewer_edit, two

GAP = {
    **CONTRADICTION,
    "type": "gap",
    "description": "nothing says what the ui shows at the limit",
}
CRITICAL = {
    **CONTRADICTION,
    "severity": "critical",
    "description": "api refuses at 60, ui retries forever",
}


def answer(findings=(), **statuses):
    """The consistency agent's answer: *findings*, and a status per listed id
    (`CC_F1="resolved"`)."""
    return {
        "findings": list(findings),
        "prior_assessments": [
            {"finding_id": k.replace("_", "-"), "status": v, "evidence": "the lines"}
            for k, v in statuses.items()
        ],
    }


def blocked_once(repo, model, *later):
    """Two components, through the review stops, blocked by CONTRADICTION
    (CC-F1); the agent then answers *later* in order."""
    two(model)
    model["consistency"] = [answer([CONTRADICTION]), *later]
    go(repo, [*FLOW[:4], (RESUME, 4)])


def test_a_later_round_rechecks_the_listed_findings(repo, model, capsys):
    blocked_once(repo, model, answer(CC_F1="resolved"))
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    capsys.readouterr()
    assert run(repo, *RESUME) == 0
    prompt = model.exactly("consistency")[-1]
    assert "CC-F1 [major] (contradiction) api says 60 a minute, ui says 100" in prompt
    assert "SPEC:READY:" in capsys.readouterr().out


def test_a_new_major_in_a_later_round_is_advisory_and_warned_loudly(
    repo, model, capsys
):
    blocked_once(repo, model, answer([GAP], CC_F1="resolved"))
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    capsys.readouterr()
    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert "WARNING" in out and "1 critical or major finding" in out
    assert "nothing says what the ui shows at the limit" in out
    assert f"SPEC:WARN:{cdir}" in out
    assert out.index("SPEC:WARN") < out.index("SPEC:READY")


def test_a_pass_with_only_minor_findings_has_no_warning(repo, model, capsys):
    blocked_once(repo, model, answer([NIT], CC_F1="resolved"))
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    capsys.readouterr()
    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    assert "SPEC:WARN" not in out and "SPEC:READY:" in out


def test_a_new_critical_in_a_later_round_blocks_with_the_next_id(repo, model, capsys):
    blocked_once(repo, model, answer([CRITICAL], CC_F1="resolved"))
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    capsys.readouterr()
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert "0 still open from earlier rounds, 1 new" in out
    step = state(change_dir(repo))["steps"]["consistency"]
    assert [f["finding_id"] for f in step["blocking_findings"]] == ["CC-F2"]


def test_a_listed_finding_still_present_blocks_and_counts_as_carried(
    repo, model, capsys
):
    blocked_once(repo, model, answer([GAP], CC_F1="still_present"))
    reviewer_edit(repo, repo.parent / "reviewer", "still 100 in the ui")
    capsys.readouterr()
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert "1 still open from earlier rounds, 0 new" in out
    step = state(change_dir(repo))["steps"]["consistency"]
    assert [f["finding_id"] for f in step["blocking_findings"]] == ["CC-F1"]
    assert [f["severity"] for f in step["advisory_findings"]] == ["major"]


def test_accepting_every_blocking_finding_passes_with_no_model_call(
    repo, model, capsys
):
    blocked_once(repo, model)
    cdir = change_dir(repo)
    capsys.readouterr()
    calls = len(model.calls)
    code = run(
        repo, *RESUME, "--accept", "CC-F1", "--accept-reason", "the ui is advisory"
    )
    assert code == 0
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert f"SPEC:WARN:{cdir}" in out and "SPEC:READY:" in out
    accepted = (cdir / "accepted-findings.md").read_text()
    assert "CC-F1" in accepted and "the ui is advisory" in accepted
    assert "api says 60 a minute, ui says 100" in accepted
    tracked = git(cdir, "ls-files", "accepted-findings.md")
    assert tracked.strip()
    assert state(cdir)["accepted_findings"][0]["finding_id"] == "CC-F1"


def test_accepted_findings_are_in_later_prompts(repo, model):
    two(model)
    model["consistency"] = [
        answer([CONTRADICTION, GAP]),
        answer(CC_F2="resolved"),
    ]
    go(repo, [*FLOW[:4], (RESUME, 4)])
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    assert (
        run(repo, *RESUME, "--accept", "CC-F1", "--accept-reason", "known, tracked")
        == 0
    )
    prompt = model.exactly("consistency")[-1]
    accepted = prompt[prompt.index("## Findings a person accepted") :]
    assert "CC-F1" in accepted and "api says 60 a minute" in accepted
    listed = prompt[prompt.index("## Findings the edit was asked to fix") :]
    assert "CC-F2" in listed.split("## Findings a person accepted")[0]


def test_accept_refuses_an_id_that_is_not_blocking(repo, model, capsys):
    blocked_once(repo, model)
    capsys.readouterr()
    assert run(repo, *RESUME, "--accept", "CC-F9", "--accept-reason", "x") == 2
    out = capsys.readouterr().out
    assert "CC-F9" in out and "CC-F1" in out
    assert (
        "accepted_findings" not in state(change_dir(repo))
        or not state(change_dir(repo))["accepted_findings"]
    )


def test_accept_needs_a_reason(repo, model, capsys):
    blocked_once(repo, model)
    capsys.readouterr()
    assert run(repo, *RESUME, "--accept", "CC-F1") == 2
    assert "--accept-reason" in capsys.readouterr().out


def test_accept_is_owned_and_committed_by_pm_spec():
    assert git_ops.owned("accepted-findings.md")


def test_the_round_cap_stops_checking_until_accept_or_check_again(repo, model, capsys):
    write(repo / "specs" / "config.yaml", "consistency:\n  max_rounds: 2\n")
    git(repo, "add", "specs/config.yaml")
    git(repo, "commit", "-qm", "config")
    blocked_once(
        repo,
        model,
        answer(CC_F1="still_present"),
        answer(CC_F1="resolved"),
    )
    reviewer_edit(repo, repo.parent / "reviewer", "still 100 in the ui")
    assert run(repo, *RESUME) == 4  # the second blocked round
    reviewer_edit(repo, repo.parent / "reviewer-2", "60 a minute everywhere")
    capsys.readouterr()
    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert "2 rounds" in out and "--accept" in out and "--check-again" in out
    assert "SPEC:BLOCKED:" in out
    assert run(repo, *RESUME, "--check-again") == 0
    assert len(model.calls) == calls + 1
    cdir = change_dir(repo)
    assert layout.ready(cdir) == (True, "")


def test_past_the_round_cap_accepting_every_open_finding_checks_the_edit(repo, model):
    write(repo / "specs" / "config.yaml", "consistency:\n  max_rounds: 1\n")
    git(repo, "add", "specs/config.yaml")
    git(repo, "commit", "-qm", "config")
    blocked_once(repo, model, answer())
    reviewer_edit(repo, repo.parent / "reviewer", "60 a minute everywhere")
    calls = len(model.calls)
    code = run(repo, *RESUME, "--accept", "CC-F1", "--accept-reason", "fine as is")
    assert code == 0
    assert len(model.calls) == calls + 1
