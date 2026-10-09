"""Needs: information the review could not get, each with a command for a person."""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import jsonschema
import pytest
from conftest import KEYWORD_CITATION, REAL_BRANCH_RULES, SCHEMA, high_finding, medium_finding

from review_pipeline import orchestrator, sdk, target
from review_pipeline.config import ReviewConfig
from review_pipeline.export import to_findings_file
from review_pipeline.gates import gated_need, need_gate
from review_pipeline.models import FinderOutput, Merged, Need, NeedAsk, ReviewState, Verdict
from review_pipeline.render import render_review, render_summary
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import run_find
from review_pipeline.stages.verify import run_verify, unverifiable_severity

TAVILY_NEED = NeedAsk(what="The tavily-python 0.8.4 changelog, to see whether search() still takes depth.",
                      cause="unreachable", command="pip download tavily-python==0.8.4 --no-deps")


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(concurrency=1, dimensions=("diff-bugs", "callers")),
                      snapshot=snapshot, diff=Path(target_info.diff_path).read_text())


def use(monkeypatch, answers: dict[str, list]) -> list[str]:
    """Replace agent_call_structured with canned answers per budget label; return the prompts sent."""
    prompts: list[str] = []

    async def canned(prompt, schema, *, budget_label="", **kwargs):
        prompts.append(prompt)
        return schema.model_validate(answers[budget_label].pop(0).model_dump())

    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return prompts


def _failing_gh(monkeypatch, *, returncode=1, stdout="", stderr="HTTP 403: Resource not accessible\nmore"):
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(target.subprocess, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr))


# --- target: the pipeline's own lookups -----------------------------------------


def test_failed_branch_rules_lookup_is_a_need_with_the_exact_command(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch)
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) is None
    assert len(needs) == 1
    need = needs[0]
    assert (need.cause, need.source, need.blocks) == ("lookup-failed", "pipeline", "review")
    # The call the pipeline made, placeholders and all: gh fills them from the repository it runs in,
    # so the command reads the same repository even in a fork clone or after `gh repo set-default`.
    assert need.command == "gh api repos/{owner}/{repo}/rules/branches/main"
    assert f"Run the command in `{repo}`" in need.what
    assert "HTTP 403: Resource not accessible" in need.what and "more" not in need.what


def test_branch_rules_lookup_without_json_is_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch, returncode=0, stdout="<html>", stderr="")
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) is None
    assert [n.command for n in needs] == ["gh api repos/{owner}/{repo}/rules/branches/main"]
    assert "no JSON" in needs[0].what


def test_branch_rules_that_read_fine_add_no_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch, returncode=0, stdout="[]", stderr="")
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) == []
    assert needs == []


def test_pr_view_with_empty_title_and_base_is_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(target, "_pr_view", lambda repo_path, number: {
        "headRefOid": head, "baseRefOid": base, "headRefName": "x", "baseRefName": "", "title": "",
        "body": ""})
    resolved = target.resolve(repo, pr=7)
    needs: list[Need] = []
    target.build_target(resolved, needs=needs)
    assert len(needs) == 1
    assert needs[0].command == "gh pr view 7 --json title,baseRefName"  # as `_pr_view` ran it, no --repo
    assert str(repo) in needs[0].what
    assert needs[0].cause == "lookup-failed" and needs[0].source == "pipeline"


def test_pr_view_with_only_an_empty_body_is_not_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(target, "_pr_view", lambda repo_path, number: {
        "headRefOid": head, "baseRefOid": base, "headRefName": "x", "baseRefName": "main",
        "title": "A title", "body": ""})
    needs: list[Need] = []
    target.build_target(target.resolve(repo, pr=7), needs=needs)
    assert needs == []


def test_prepare_puts_the_pipelines_failed_lookups_into_the_state(fixture_repo, tmp_path, monkeypatch):
    """`prepare` passes build_target's needs through gated_need into the ReviewState it returns."""
    repo, base, head = fixture_repo
    monkeypatch.setattr(target, "_pr_view", lambda repo_path, number: {
        "headRefOid": head, "baseRefOid": base, "headRefName": "x", "baseRefName": "main",
        "title": "A title", "body": ""})

    def failing_rules(repo_path, branch, needs=None):
        needs.append(Need(what=" GitHub's rules on main. ", cause="lookup-failed", source="pipeline",
                          command=" gh api repos/{owner}/{repo}/rules/branches/main "))
        needs.append(Need(what="A lookup with a bad command.", cause="lookup-failed", source="pipeline",
                          command="gh api repos/o/r -X PATCH"))
        return None

    monkeypatch.setattr(target, "branch_rules", failing_rules)
    state, _ = orchestrator.prepare(orchestrator.TargetSpec(repo=str(repo), pr=7), tmp_path / "out")
    assert [(n.what, n.command) for n in state.needs] == [
        ("GitHub's rules on main.", "gh api repos/{owner}/{repo}/rules/branches/main"),  # stripped
        ("A lookup with a bad command.", ""),  # need_gate blanked it, the need stays
    ]
    exported = to_findings_file(state)
    assert [n["command"] for n in exported["needs"]] == ["gh api repos/{owner}/{repo}/rules/branches/main", ""]


# --- verify ------------------------------------------------------------------------


async def test_unverifiable_answer_with_a_need_is_stored_and_exported(target_info, ctx, monkeypatch):
    medium = medium_finding()
    prompts = use(monkeypatch, {"verify": [Verdict(status="unverifiable", reason="depends on tavily 0.8.4",
                                                   needs=[TAVILY_NEED])]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[medium]), ctx)
    assert "fill `needs`" in prompts[0] and "pip download tavily-python==0.8.4 --no-deps" in prompts[0]
    assert state.needs == [Need(what=TAVILY_NEED.what, blocks=medium.id, cause="unreachable",
                                command=TAVILY_NEED.command, source="verifier")]
    data = to_findings_file(state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    row = next(f for f in data["findings"] if f["id"] == medium.id)
    assert row["needs"] == [{"what": TAVILY_NEED.what, "blocks": medium.id, "cause": "unreachable",
                             "command": TAVILY_NEED.command, "source": "verifier"}]
    assert data["needs"] == row["needs"]


async def test_needs_on_a_confirmed_answer_are_not_kept(target_info, ctx, monkeypatch):
    high = high_finding()
    use(monkeypatch, {"verify": [Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION],
                                         expected_behaviour="e", needs=[TAVILY_NEED])]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    assert state.needs == []


# --- severity ----------------------------------------------------------------------


async def test_unverifiable_finding_with_a_need_stays_at_medium(target_info, ctx, monkeypatch):
    medium, high = medium_finding(), high_finding()
    use(monkeypatch, {"verify": [
        Verdict(status="unverifiable", reason="needs the vendor's settings", needs=[TAVILY_NEED]),
        Verdict(status="unverifiable", reason="needs the vendor's settings", needs=[TAVILY_NEED]),
    ]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[medium, high]), ctx)
    by_id = {f.id: f.severity for f in state.findings}
    assert by_id[medium.id] == "MEDIUM"  # not lowered to LOW
    assert by_id[high.id] == "MEDIUM"  # still lowered one step
    assert state.original_severity == {medium.id: "MEDIUM", high.id: "HIGH"}


async def test_unverifiable_finding_without_a_need_is_lowered_as_before(target_info, ctx, monkeypatch):
    medium = medium_finding()
    use(monkeypatch, {"verify": [Verdict(status="unverifiable", reason="the code does not settle it")]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[medium]), ctx)
    assert state.findings[0].severity == "LOW"


def test_unverifiable_severity_table():
    assert [unverifiable_severity(s, has_need=True) for s in ("HIGH", "MEDIUM", "LOW")] == ["MEDIUM", "MEDIUM", "LOW"]
    assert [unverifiable_severity(s, has_need=False) for s in ("HIGH", "MEDIUM", "LOW")] == ["MEDIUM", "LOW", "LOW"]


# --- find --------------------------------------------------------------------------


async def test_finder_needs_are_stored_as_blocking_the_review(target_info, ctx, monkeypatch):
    prompts = use(monkeypatch, {
        "find:diff-bugs": [FinderOutput(findings=[], needs=[TAVILY_NEED])],
        "find:callers": [FinderOutput(findings=[])],
    })
    state = await run_find(ReviewState(target=target_info), ctx)
    assert "Never write a finding to say you could not check something" in prompts[0]
    assert state.needs == [Need(what=TAVILY_NEED.what, blocks="review", cause="unreachable",
                                command=TAVILY_NEED.command, source="finder")]
    assert state.finder_results["diff-bugs"].needs == [TAVILY_NEED]


def test_finder_answer_with_needs_parses_and_an_unknown_cause_becomes_no_access():
    out = FinderOutput.model_validate({"findings": [], "needs": [{"what": "w", "cause": "forbidden"}]})
    assert out.needs == [NeedAsk(what="w", cause="no-access", command="")]


# --- need_gate ---------------------------------------------------------------------


@pytest.mark.parametrize("command, rule", [
    ("gh api repos/o/r\ngh api user", "more than one line"),
    ("gh api " + "x" * 300, "300 characters"),
    ("gh api repos/o/r; rm notes.txt", "';'"),
    ("gh api repos/o/r && rm notes.txt", "'&'"),
    ("gh api repos/o/r || true", "'|'"),
    ("gh api repos/o/r | sh", "'|'"),
    ("gh api repos/o/r > out.json", "'>'"),
    ("psql < dump.sql", "'<'"),
    ("gh api `whoami`", "'`'"),
    ("gh api $(whoami)", "'$('"),
    ("rm notes.txt", "not a known read-only CLI"),
    ("terraform apply", "terraform apply is not a read-only form"),
    ("terraform", "terraform (no verb) is not a read-only form"),
    ("terraform state mv a b", "terraform mv is not a read-only form"),
    ("git push origin main", "git push is not a read-only form"),
    ("git -C repo reset --hard", "git reset is not a read-only form"),
    ("pip install requests", "pip install is not a read-only form"),
    ("npm install left-pad", "npm install is not a read-only form"),
    ("uv pip install requests", "uv install is not a read-only form"),
    ("uv version 2.0.0", "uv version is not a read-only form"),
    ("kubectl scale deploy api --replicas 0", "kubectl scale is not a read-only form"),
    ("kubectl -n prod apply -f x.yaml", "kubectl apply is not a read-only form"),
    ("gh pr merge 7", "gh merge is not a read-only form"),
    ("gh api -X PUT repos/o/r/topics", "gh api with method PUT"),
    ("gh api --method=POST repos/o/r/issues", "gh api with method POST"),
    ("gh api repos/o/r/issues -f title=x", "gh api with -f sends a request body"),
    ("gh auth status", "gh auth can print a token"),
    ("curl -X POST https://ex.com/hook", "curl with method POST"),
    ("curl -XPATCH https://ex.com/x", "curl with method PATCH"),
    ("curl -d a=1 https://ex.com/hook", "curl with -d sends or writes data"),
    ("curl -o out.bin https://ex.com/x", "curl with -o sends or writes data"),
    ("aws s3 cp a.txt s3://bucket/key", "aws cp is not a describe, list or get operation"),
    ("aws ec2 stop-instances --instance-ids i-1", "aws stop-instances is not"),
    ("gcloud run deploy api --image x", "gcloud without one of"),
    ("az vm stop -n x -g y", "az without one of"),
    ("psql -c 'INSERT INTO bookings VALUES (1)'", "psql with INSERT can change state"),
    ("bq query 'UPDATE d.t SET a = 1 WHERE b = 2'", "bq with UPDATE can change state"),
])
def test_need_gate_blanks_each_kind_of_bad_command_and_keeps_the_need(command, rule):
    need = Need(what="The base branch's rules.", blocks="review", cause="lookup-failed", command=command)
    result = need_gate(need)
    assert not result.passed and rule in result.reason
    kept = gated_need(need)
    assert (kept.command, kept.what, kept.blocks) == ("", "The base branch's rules.", "review")


@pytest.mark.parametrize("command", [
    "gh api repos/o/r/rules/branches/main",
    "gcloud run services describe api --region europe-west1 --format json",
    "pip download tavily-python==0.8.4 --no-deps",
    "terraform state list",
    "terraform show -json",
    "gh api repos/{owner}/{repo}/rules/branches/main",
    "gh api -X GET repos/o/r/actions/runs",
    "gh pr view 7 --repo o/r --json title,baseRefName",
    "gh run list --limit 5",
    "git -C repo log --oneline -5",
    "git ls-remote origin",
    "kubectl -n prod get pods -o json",
    "aws s3 ls s3://bucket/prefix",
    "aws ecs describe-services --cluster c --services s",
    "aws sts get-caller-identity",
    "az account show",
    "gcloud logging read resource.type=cloud_run_revision --limit 20",
    "bq --format json show d.t",
    "bq query 'SELECT count(*) FROM d.t'",
    "psql -c 'SELECT 1'",
    "curl -sS https://example.com/api/status",
    "curl -I https://example.com",
    "npm view left-pad versions",
    "uv pip show requests",
    "uv tree",
    "pip index versions tavily-python",
    "",
])
def test_need_gate_passes_short_read_only_commands(command):
    need = Need(what="w", command=command)
    assert need_gate(need).passed
    assert gated_need(need).command == command


# --- export and render -------------------------------------------------------------


def _with_needs(state: ReviewState) -> ReviewState:
    medium_id = next(f.id for f in state.findings if f.severity == "LOW")  # the lowered unverifiable one
    return state.model_copy(update={"needs": [
        Need(what="GitHub's rules on main.", blocks="review", cause="lookup-failed",
             command="gh api repos/example/booking-engine/rules/branches/main", source="pipeline"),
        Need(what="The production channel title.", blocks=medium_id, cause="no-access", command="",
             source="verifier"),
    ]})


def test_findings_file_with_and_without_needs_validates(canned_state):
    schema = json.loads(SCHEMA.read_text())
    without = to_findings_file(canned_state)
    jsonschema.validate(without, schema)
    assert "needs" not in without and all("needs" not in f for f in without["findings"])
    with_needs = to_findings_file(_with_needs(canned_state))
    jsonschema.validate(with_needs, schema)
    assert with_needs["needs"][0]["blocks"] == "review"
    assert sum(1 for f in with_needs["findings"] if "needs" in f) == 1


def test_a_need_on_a_merged_away_finding_moves_to_the_survivor(canned_state):
    high, medium = canned_state.findings[0], canned_state.findings[1]
    state = canned_state.model_copy(update={
        "findings": [high],
        "merged": [Merged(finding=medium, into=high.id, reason="same")],
        "needs": [Need(what="w", blocks=medium.id, cause="no-access", source="verifier")],
    })
    data = to_findings_file(state)
    assert data["needs"][0]["blocks"] == high.id
    assert next(f for f in data["findings"] if f["id"] == high.id)["needs"][0]["what"] == "w"


def test_summary_has_a_needs_you_section_after_the_counts(canned_state):
    text = render_summary(_with_needs(canned_state))
    lines = text.splitlines()
    counts = next(i for i, line in enumerate(lines) if line.startswith("Findings:"))
    assert lines[counts + 2] == "## Needs you"
    assert "Run: `gh api repos/example/booking-engine/rules/branches/main`" in text
    assert "Blocks the review; a lookup failed." in text
    assert "Blocks `rateplan_service.py:" in text and "No command is known." in text
    assert "## Needs you" not in render_summary(canned_state)


def test_review_lists_each_findings_needs_under_it_and_the_reviews_own(canned_state):
    text = render_review(_with_needs(canned_state))
    assert "## Needs you" in text and text.index("## Needs you") < text.index("## Findings")
    assert "The production channel title. Why: the review has no access. No command is known." in text
