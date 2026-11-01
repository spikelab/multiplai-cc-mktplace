"""The component check: one test per row S1-S10 of its plan's "Stop and resume
states" table, and the block, accept and revise rules.

Each row test stops the run at its row's point (or applies its perturbation)
with the fake model, resumes, and compares the change dir, origin's commits
and refs, the run state and the `COMPONENT_CHECK` progress lines with a run of
the same inputs that never stopped (`baseline`), except where the row's
"Resume does" says otherwise. The run state is compared without its spend
record (`budget`) where a row resumes with another `--max-usd`.

The change has two components, `api` and `ui`, and `ui` depends on `api`
(`TWO`), so `ui`'s check reads `api`'s specs and `api`'s does not read `ui`'s.
The check runs in the consistency step, at the last `resume` of `FLOW`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    NO_FINDINGS,
    FakeModel,
    Killed,
    change_dir,
    git,
    ready_line,
    run,
    specs,
    state,
    worktree,
    write,
)
from multiplai_core.specs import set_spec_issues
from pm_spec_pipeline import component_check as CK
from pm_spec_pipeline import layout, runlock
from pm_spec_pipeline.run import MARKER_BLOCKED, MARKER_WARN
from test_resume_rows import (
    FLOW,
    RESUME,
    baseline,
    go,
    killer,
    push_edit,
    pushed,
    run_dir_files,
    run_state,
    same_as,
    two,
)

API_GAP = {
    "type": "contradiction",
    "severity": "major",
    "description": "ui refuses an owner the api spec lets through",
    "places": [
        {"file": "components/ui/specs.md", "where": "rate-limit: Rate limit holds"},
        {"file": "components/api/specs.md", "where": "rate-limit: Rate limit holds"},
    ],
    "suggestion": "say in ui's requirement which owners are refused, as api does",
}


def answer(*findings, **statuses):
    return {
        "findings": list(findings),
        "prior_assessments": [
            {"finding_id": k.replace("_", "-"), "status": v, "evidence": "the lines"}
            for k, v in statuses.items()
        ],
    }


def lines(cdir: Path) -> list[str]:
    """The `COMPONENT_CHECK` lines of the progress file."""
    path = cdir / ".pm-spec" / "progress.md"
    text = path.read_text() if path.is_file() else ""
    return [line for line in text.splitlines() if line.startswith("COMPONENT_CHECK ")]


def without_budget(cdir: Path) -> dict:
    data = run_state(cdir)
    data.pop("budget", None)
    return data


def spent(cdir: Path) -> float:
    return float(state(cdir)["budget"]["cost_usd"])


def commits_but_meta(repo: Path) -> list[str]:
    """`pushed` without `.change.yaml` paths: `mark_ready` stamps the current
    second into each one, so whether a component's `.change.yaml` is in a
    revise commit depends on whether that run crossed a second, not on the stop."""
    return [line for line in pushed(repo) if not line.endswith(".change.yaml")]


def kill_next(then):
    """A handler that kills the run on its next call and answers *then* after:
    `killer` counts the label's calls from the start of the test."""
    calls = []

    def handler(prompt, label, kw):
        calls.append(label)
        if len(calls) == 1:
            raise Killed(label)
        return then

    return handler


def checks_since(model: FakeModel, calls: int) -> list[str]:
    return [label for label in model.labels()[calls:] if label.startswith(CK.NAME)]


# --- the check itself ---------------------------------------------------------------


def test_each_check_reads_its_specs_its_dependencies_and_the_others_outlines(
    repo, model
):
    two(model)
    go(repo, FLOW)
    ui, api = (model.exactly(f"component_check:{c}")[0] for c in ("ui", "api"))
    cdir = change_dir(repo)
    assert "### components/ui/plan.md" in ui and "#### components/ui/specs.md" in ui
    # ui depends on api: api's specs in full. api does not: ui's outline only.
    assert "### components/api/specs.md\n# Capability:" in ui
    assert "it depends on no other component" in api
    assert "### components/ui/specs.md\n# Capability: rate-limit\n## ADDED" in api
    assert (
        "The system SHALL"
        not in api.split("## Every other component")[1].split("## What to do")[0]
    )
    assert "Untrusted data" in ui
    # Read-only tools, in the repository.
    kwargs = next(kw for label, _, kw in model.calls if label == "component_check:ui")
    assert kwargs["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert kwargs["cwd"] == str(worktree(repo))
    saved = state(cdir)["component_checks"]
    assert set(saved) == {"api", "ui"} and saved["ui"]["cost_usd"] == 0.01
    assert lines(cdir) == [
        "COMPONENT_CHECK api findings=0 cost=0.0100",
        "COMPONENT_CHECK ui findings=0 cost=0.0100",
    ]


def test_a_change_with_no_components_is_one_check(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("component_check") == 1
    prompt = model.exactly("component_check:throttle")[0]
    assert "#### specs.md" in prompt and "### plan.md" in prompt


def test_max_parallel_caps_the_checks_running_at_once(repo, model, monkeypatch):
    import asyncio

    write(repo / "specs" / "config.yaml", "component_check:\n  max_parallel: 1\n")
    git(repo, "add", "specs/config.yaml")
    git(repo, "commit", "-qm", "config")
    two(model)
    go(repo, FLOW[:4])
    running, most = 0, 0

    async def slow(prompt, *, label="", **kwargs):
        nonlocal running, most
        if label.startswith(CK.NAME):
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.01)
            running -= 1
        return await model(prompt, label=label, **kwargs)

    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", slow)
    assert run(repo, *RESUME) == 0
    assert most == 1 and model.calls_to(CK.NAME) == 2


# --- block, accept (criterion 3) --------------------------------------------------


def test_a_spec_contradicting_its_dependency_blocks_and_accept_hands_off(
    repo, model, capsys
):
    two(model)
    model["component_check:ui"] = answer(API_GAP)
    go(repo, FLOW[:4])
    capsys.readouterr()
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert f"{MARKER_BLOCKED}:{cdir}" in out and "SPEC:READY" not in out
    assert "[component `ui`, finding 1] ui refuses an owner" in out
    step = state(cdir)["steps"]["consistency"]
    assert [(f["finding_id"], f["component"]) for f in step["blocking_findings"]] == [
        ("CC-F1", "ui")
    ]
    calls = len(model.calls)
    assert (
        run(repo, *RESUME, "--accept", "CC-F1", "--accept-reason", "owners differ") == 0
    )
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert ready_line(out) == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    assert f"{MARKER_WARN}:{cdir}" in out and "ui refuses an owner" in out


def test_a_minor_component_finding_is_advisory(repo, model, capsys):
    two(model)
    model["component_check:api"] = answer({**API_GAP, "severity": "minor"})
    go(repo, FLOW[:4])
    capsys.readouterr()
    assert run(repo, *RESUME) == 0
    assert "advisory findings" in capsys.readouterr().out


def test_infeasible_is_a_finding_type(repo, model, capsys):
    two(model)
    model["component_check:api"] = answer(
        {
            **API_GAP,
            "type": "infeasible",
            "places": [
                {"file": "components/api/specs.md", "where": "rate-limit"},
                {"file": "server/limits.py", "where": "Limiter.allow"},
            ],
        }
    )
    go(repo, FLOW[:4])
    assert run(repo, *RESUME) == 4
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["blocking_findings"][0]["category"] == "infeasible"


def test_the_auto_rewrite_rewrites_the_spec_a_component_finding_cites(repo, model):
    two(model)
    model["component_check:ui"] = [answer(API_GAP), answer(CC_F1="resolved")]
    model["consistency"] = [NO_FINDINGS, answer(CC_F1="resolved")]
    assert run(repo, "new", "throttle", "--auto") == 0
    rewrites = [label for label in model.labels() if ":rewrite:" in label]
    assert sorted(rewrites) == ["consistency:rewrite:api", "consistency:rewrite:ui"]
    assert "ui refuses an owner" in model.prompts("consistency:rewrite:ui")[0]
    # The recheck asks ui's check about CC-F1, not the consistency agent.
    assert "CC-F1" in model.exactly("component_check:ui")[-1]
    assert "CC-F1" not in model.exactly("consistency")[-1]
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "passed"
    assert step["repair_findings"][0]["status"] == "resolved"


# --- S1 -----------------------------------------------------------------------------


def test_S1_killed_after_some_component_results_were_saved(
    repo, model, tmp_path, monkeypatch
):
    two(model)
    model["component_check:ui"] = killer(NO_FINDINGS)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    cdir = change_dir(repo)
    saved = state(cdir)
    assert set(saved["component_checks"]) == {"api"}
    assert saved["steps"].get("consistency", {}).get("outcome", "pending") == "pending"
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert checks_since(model, calls) == ["component_check:ui"]
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)
    assert lines(cdir) == lines(change_dir(other))


# --- S2 -----------------------------------------------------------------------------


def test_S2_the_spend_cap_stops_the_checks_part_way(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    go(repo, FLOW[:4])
    cdir = change_dir(repo)
    # One more call fits under the cap: api's check; ui's is stopped.
    cap = f"{spent(cdir) + 0.005:.4f}"
    capsys.readouterr()
    assert run(repo, *RESUME, "--max-usd", cap) == 1
    assert "higher --max-usd" in capsys.readouterr().out
    saved = state(cdir)
    assert set(saved["component_checks"]) == {"api"}
    assert saved["steps"].get("consistency", {}).get("outcome", "pending") == "pending"
    assert saved["steps"]["specs"]["outcome"] == "passed"
    calls = len(model.calls)
    assert run(repo, *RESUME, "--max-usd", "100") == 0
    assert checks_since(model, calls) == ["component_check:ui"]
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other, run_state_too=False)
    assert without_budget(cdir) == without_budget(change_dir(other))
    assert lines(cdir) == lines(change_dir(other))


# --- S3 -----------------------------------------------------------------------------


def test_S3_every_result_saved_then_killed_before_the_consistency_agent(
    repo, model, tmp_path, monkeypatch
):
    two(model)
    model["consistency"] = killer(NO_FINDINGS)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    cdir = change_dir(repo)
    assert set(state(cdir)["component_checks"]) == {"api", "ui"}
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert model.labels()[calls:] == ["consistency"]
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)
    assert lines(cdir) == lines(change_dir(other))


# --- S4 -----------------------------------------------------------------------------


def test_S4_a_block_refuses_unchanged_and_rechecks_only_the_edited_component(
    repo, model, tmp_path, monkeypatch, capsys
):
    def configure(m: FakeModel) -> None:
        two(m)
        m["component_check:ui"] = [answer(API_GAP), answer(CC_F1="resolved")]

    configure(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    cdir = change_dir(repo)
    capsys.readouterr()
    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert len(model.calls) == calls and "ui refuses an owner" in out

    fixed = specs(rule="the owners api lets through")
    push_edit(repo, tmp_path / "reviewer", "components/ui/specs.md", fixed)
    assert run(repo, *RESUME) == 0
    assert model.labels()[calls:] == ["component_check:ui", "consistency"]
    assert "CC-F1" in model.exactly("component_check:ui")[-1]
    assert "CC-F1" not in model.exactly("consistency")[-1]
    step = state(cdir)["steps"]["consistency"]
    assert step["outcome"] == "passed"

    # The same edit made at the review stop, in a run that never blocked.
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW[:4])
    push_edit(other, tmp_path / "reviewer-2", "components/ui/specs.md", fixed)
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", FakeModel())
    assert run(other, *RESUME) == 0
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", model)
    # Files, commits and refs as if it never blocked; the state keeps the history.
    same_as(repo, other, run_state_too=False)


# --- S5 -----------------------------------------------------------------------------


def test_S5_an_edit_between_stop_and_resume_reruns_that_component_and_its_dependents(
    repo, model, tmp_path, monkeypatch
):
    two(model)
    model["consistency"] = killer(NO_FINDINGS, times=2)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    cdir = change_dir(repo)

    # api's plan: only api's check reads it.
    plan = (cdir / "components" / "api" / "plan.md").read_text() + "\nEdited.\n"
    push_edit(repo, tmp_path / "r1", "components/api/plan.md", plan)
    calls = len(model.calls)
    with pytest.raises(Killed):
        run(repo, *RESUME)
    assert checks_since(model, calls) == ["component_check:api"]

    # api's specs: api's check and ui's (ui depends on api) read them.
    text = specs(rule="api's edited rule")
    push_edit(repo, tmp_path / "r2", "components/api/specs.md", text)
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert checks_since(model, calls) == ["component_check:api", "component_check:ui"]

    # The same edits at the review stop, in a run that never stopped.
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW[:4])
    push_edit(other, tmp_path / "o1", "components/api/plan.md", plan)
    push_edit(other, tmp_path / "o2", "components/api/specs.md", text)
    fresh = FakeModel()
    two(fresh)
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", fresh)
    assert run(other, *RESUME) == 0
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", model)
    same_as(repo, other, run_state_too=False)
    mine, theirs = without_budget(cdir), without_budget(change_dir(other))
    assert mine["component_checks"] == theirs["component_checks"]
    assert mine["steps"] == theirs["steps"]


# --- S6 -----------------------------------------------------------------------------


def test_S6_origin_moved_fast_forward_is_as_S5(repo, model, tmp_path):
    two(model)
    model["component_check:ui"] = killer(NO_FINDINGS)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    # Someone else pushes an edit to api's specs: pulled, then api (stale
    # digest) and ui (no result) are checked.
    push_edit(repo, tmp_path / "r", "components/api/specs.md", specs(rule="theirs"))
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert checks_since(model, calls) == ["component_check:api", "component_check:ui"]
    assert "theirs" in model.exactly("component_check:ui")[-1]


def test_S6_a_diverged_branch_is_refused_and_nothing_changes(
    repo, model, tmp_path, capsys
):
    two(model)
    model["component_check:ui"] = killer(NO_FINDINGS)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    wt, cdir = worktree(repo), change_dir(repo)
    write(cdir / "notes.md", "local\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "mine")
    push_edit(repo, tmp_path / "other", "proposal.md", "# Request throttling\n\nX.\n")
    before, calls = run_dir_files(cdir), len(model.calls)
    capsys.readouterr()
    assert run(repo, *RESUME) == 2
    assert "Nothing was changed" in capsys.readouterr().out
    assert run_dir_files(cdir) == before and len(model.calls) == calls


# --- S7 -----------------------------------------------------------------------------


def test_S7_stopped_twice_at_the_same_point(repo, model, tmp_path, monkeypatch):
    two(model)
    model["component_check:ui"] = killer(NO_FINDINGS, times=2)
    go(repo, FLOW[:4])
    for _ in range(2):
        with pytest.raises(Killed):
            run(repo, *RESUME)
    assert run(repo, *RESUME) == 0
    assert model.calls_to("component_check:api") == 1
    assert model.calls_to("component_check:ui") == 3
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)
    cdir = change_dir(repo)
    assert lines(cdir) == lines(change_dir(other))
    assert len(lines(cdir)) == 2


# --- S8 -----------------------------------------------------------------------------


def test_S8_a_live_process_holds_the_lock(repo, model, tmp_path, monkeypatch):
    two(model)
    model["component_check:ui"] = killer(NO_FINDINGS)
    go(repo, FLOW[:4])
    with pytest.raises(Killed):
        run(repo, *RESUME)
    cdir = change_dir(repo)
    before, calls = run_dir_files(cdir), len(model.calls)
    with runlock.hold(cdir / ".pm-spec"):
        assert run(repo, *RESUME) == 5
    assert run_dir_files(cdir) == before and len(model.calls) == calls
    assert run(repo, *RESUME) == 0
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- revise: S9, S10 and its blocking rule (criteria 4 and 5) -----------------------

ISSUES = """\
# Spec issues

## Owners refused twice

components/{cid}/specs.md refuses owners the build cannot tell apart.
"""


def ready_change(repo, model, cid="ui") -> Path:
    two(model)
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES.format(cid=cid))
    set_spec_issues(cdir, cdir / "spec-issues.md")
    return cdir


RESOLVED = answer(RV_F1="resolved")


def test_revise_blocks_on_a_new_major_finding_on_the_component_it_rewrote(
    repo, model, capsys
):
    cdir = ready_change(repo, model)
    model["consistency"] = RESOLVED
    model["component_check:ui"] = answer(API_GAP)
    calls = len(model.calls)
    capsys.readouterr()
    assert run(repo, "revise", "throttle") == 4
    out = capsys.readouterr().out
    # ui was rewritten: ui is checked; api is not (nothing it reads changed).
    assert checks_since(model, calls) == ["component_check:ui"]
    assert f"{MARKER_BLOCKED}:{cdir}" in out and "ui refuses an owner" in out
    step = state(cdir)["steps"]["revise"]
    assert step["outcome"] == "blocked"
    assert [f["component"] for f in step["blocking_findings"]] == ["ui"]
    assert (cdir / "spec-issues.md").is_file()


def test_revise_keeps_a_new_major_finding_on_an_untouched_component_advisory(
    repo, model, capsys
):
    cdir = ready_change(repo, model)
    # The consistency agent raises a new major finding on api, which the
    # rewrite did not touch: today's rule, advisory.
    on_api = {
        **API_GAP,
        "description": "api's limit is vague",
        "places": [
            {"file": "components/api/specs.md", "where": "rate-limit"},
            {"file": "proposal.md", "where": "What Changes"},
        ],
    }
    model["consistency"] = answer(on_api, RV_F1="resolved")
    calls = len(model.calls)
    capsys.readouterr()
    assert run(repo, "revise", "throttle") == 0
    out = capsys.readouterr().out
    assert checks_since(model, calls) == ["component_check:ui"]
    assert "api's limit is vague" in out and f"{MARKER_WARN}:{cdir}" in out
    assert ready_line(out)


def test_revise_checks_the_dependents_of_the_component_it_rewrote(repo, model):
    ready_change(repo, model, cid="api")
    model["consistency"] = RESOLVED
    calls = len(model.calls)
    assert run(repo, "revise", "throttle") == 0
    assert checks_since(model, calls) == ["component_check:api", "component_check:ui"]


def test_S9_a_spend_stop_before_the_rewrite_published_keeps_the_rewrite(
    repo, model, tmp_path, monkeypatch, capsys
):
    cdir = ready_change(repo, model)
    model["consistency"] = RESOLVED
    before = state(cdir)["steps"].get("revise", {}).get("repair_attempts", 0)
    capsys.readouterr()
    assert run(repo, "revise", "throttle", "--max-usd", f"{spent(cdir):.4f}") == 1
    assert "higher --max-usd" in capsys.readouterr().out
    step = state(cdir)["steps"]["revise"]
    assert step["repair_attempts"] == before == 0
    assert step["rewrite_published"] is False
    assert not any(":rewrite:" in label for label in model.labels())

    assert run(repo, "revise", "throttle", "--max-usd", "100") == 0
    assert model.calls_to("revise:rewrite:ui") == 1
    assert not (cdir / "spec-issues.md").exists()

    def configure(m: FakeModel) -> None:
        two(m)

    other, fake = baseline(
        tmp_path, monkeypatch, configure, [(["new", "throttle", "--auto"], 0)]
    )
    theirs = change_dir(other)
    write(theirs / "spec-issues.md", ISSUES.format(cid="ui"))
    set_spec_issues(theirs, theirs / "spec-issues.md")
    fake["consistency"] = RESOLVED
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", fake)
    assert run(other, "revise", "throttle") == 0
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", model)
    same_as(repo, other, commits=False, run_state_too=False)
    assert commits_but_meta(repo) == commits_but_meta(other)
    mine, ours = without_budget(cdir), without_budget(theirs)
    assert mine["steps"]["revise"] == ours["steps"]["revise"]


def test_S10_killed_after_the_rewrite_published_part_way_through_its_checks(
    repo, model, tmp_path, monkeypatch
):
    cdir = ready_change(repo, model, cid="api")
    model["consistency"] = RESOLVED
    model["component_check:ui"] = kill_next(NO_FINDINGS)
    with pytest.raises(Killed):
        run(repo, "revise", "throttle")
    step = state(cdir)["steps"]["revise"]
    assert step["rewrite_published"] is True
    rewritten_api = (cdir / "components" / "api" / "specs.md").read_text()
    assert "the rewritten api" in rewritten_api
    saved = state(cdir)["component_checks"]
    api = CK.digest(
        _run_for(cdir), next(t for t in CK.targets(_run_for(cdir)) if t.id == "api")
    )
    assert saved["api"]["digest"] == api  # saved after the rewrite
    calls = len(model.calls)
    assert run(repo, "revise", "throttle") == 0
    assert not any(":rewrite:" in label for label in model.labels()[calls:])
    assert checks_since(model, calls) == ["component_check:ui"]
    assert model.labels()[calls:][-1] == "consistency"

    def configure(m: FakeModel) -> None:
        two(m)

    other, fake = baseline(
        tmp_path, monkeypatch, configure, [(["new", "throttle", "--auto"], 0)]
    )
    theirs = change_dir(other)
    write(theirs / "spec-issues.md", ISSUES.format(cid="api"))
    set_spec_issues(theirs, theirs / "spec-issues.md")
    fake["consistency"] = RESOLVED
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", fake)
    assert run(other, "revise", "throttle") == 0
    monkeypatch.setattr("pm_spec_pipeline.sdk.run_agent", model)
    same_as(repo, other, commits=False)
    assert commits_but_meta(repo) == commits_but_meta(other)
    assert lines(cdir) == lines(theirs)


def _run_for(cdir: Path):
    """A `Run` over the change dir *cdir*, to compute digests the way the check does."""
    from pm_spec_pipeline.config import SpecConfig
    from pm_spec_pipeline.run import Run
    from pm_spec_pipeline.state import RunState

    project = cdir.parents[2]
    data = json.loads((cdir / ".pm-spec" / "state.json").read_text())
    return Run(SpecConfig(project, "throttle"), RunState.model_validate(data))
