"""Needs: information the review could not get, each with a command for a person."""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import jsonschema
import pytest
from conftest import (
    KEYWORD_CITATION,
    REAL_BRANCH_RULES,
    SCHEMA,
    high_finding,
    medium_finding,
    pieces_of,
)
from review_pipeline import orchestrator, sdk, target
from review_pipeline.config import ReviewConfig
from review_pipeline.export import (
    need_commands,
    need_coverage,
    to_checks_file,
    to_findings_file,
)
from review_pipeline.gates import gated_need, need_gate
from review_pipeline.models import (
    FinderOutput,
    Merged,
    Need,
    NeedAsk,
    ReviewState,
    Verdict,
)
from review_pipeline.prompts import convert as convert_prompt
from review_pipeline.render import render_review, render_summary
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import run_find
from review_pipeline.stages.verify import run_verify, unverifiable_severity

CHECKS_SCHEMA = SCHEMA.with_name("checks.v1.schema.json")

TAVILY_NEED = NeedAsk(
    what="The tavily-python 0.8.4 changelog, to see whether search() still takes depth.",
    cause="unreachable",
    command="pip download tavily-python==0.8.4 --no-deps",
)


@pytest.fixture
def ctx(target_info, tmp_path, monkeypatch) -> RunContext:
    monkeypatch.setenv(
        "REVIEW_TRUST_REPO", "1"
    )  # the report calls check it, as the real one does
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(
        config=ReviewConfig(concurrency=1, dimensions=("diff-bugs", "callers")),
        snapshot=snapshot,
        diff=Path(target_info.diff_path).read_text(),
    )


def use(monkeypatch, answers: dict[str, list]) -> list[str]:
    """Replace agent_call_structured with canned answers per budget label; return the prompts sent."""
    prompts: list[str] = []

    async def canned(prompt, schema, *, budget_label="", **kwargs):
        prompts.append(prompt)
        return schema.model_validate(answers[budget_label].pop(0).model_dump())

    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return prompts


def _failing_gh(
    monkeypatch,
    *,
    returncode=1,
    stdout="",
    stderr="HTTP 403: Resource not accessible\nmore",
):
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")
    monkeypatch.setattr(
        target.subprocess,
        "run",
        lambda argv, **kw: sp.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        ),
    )


# --- target: the pipeline's own lookups -----------------------------------------


def test_failed_branch_rules_lookup_is_a_need_with_the_exact_command(
    fixture_repo, monkeypatch
):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch)
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) is None
    assert len(needs) == 1
    need = needs[0]
    assert (need.cause, need.source, need.blocks) == (
        "lookup-failed",
        "pipeline",
        "review",
    )
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
    assert [n.command for n in needs] == [
        "gh api repos/{owner}/{repo}/rules/branches/main"
    ]
    assert "no JSON" in needs[0].what


def test_branch_rules_that_read_fine_add_no_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _failing_gh(monkeypatch, returncode=0, stdout="[]", stderr="")
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) == []
    assert needs == []


def _gh_by_endpoint(monkeypatch, answers: dict[str, tuple[int, str]]):
    """`gh api <endpoint>` answers per endpoint suffix: (returncode, stdout)."""
    monkeypatch.setattr(target.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(target, "remote_url", lambda repo: "https://github.com/o/r.git")

    def run(argv, **kw):
        code, out = next(v for k, v in answers.items() if argv[-1].endswith(k))
        return sp.CompletedProcess(argv, code, stdout=out, stderr="HTTP 404: Not Found")

    monkeypatch.setattr(target.subprocess, "run", run)


def test_checks_required_by_classic_branch_protection_are_a_rule(
    fixture_repo, monkeypatch
):
    # rules/branches lists rulesets only; classic protection is on the branch itself.
    repo, base, head = fixture_repo
    protection = {"protection": {"required_status_checks": {"contexts": ["ci"]}}}
    _gh_by_endpoint(
        monkeypatch,
        {
            "rules/branches/main": (0, "[]"),
            "/branches/main": (0, json.dumps(protection)),
        },
    )
    needs: list[Need] = []
    rules = REAL_BRANCH_RULES(repo, "main", needs)
    assert rules == [
        {
            "type": "required_status_checks",
            "source": "classic branch protection",
            "parameters": {"required_status_checks": [{"context": "ci"}]},
        }
    ]
    assert needs == []


def test_unreadable_classic_protection_is_marked_and_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    _gh_by_endpoint(
        monkeypatch,
        {"rules/branches/main": (0, "[]"), "/branches/main": (1, "")},
    )
    needs: list[Need] = []
    assert REAL_BRANCH_RULES(repo, "main", needs) == [{"type": target.CLASSIC_UNREAD}]
    assert [n.command for n in needs] == ["gh api repos/{owner}/{repo}/branches/main"]


def test_pr_view_with_empty_title_and_base_is_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(
        target,
        "_pr_view",
        lambda repo_path, number: {
            "headRefOid": head,
            "baseRefOid": base,
            "headRefName": "x",
            "baseRefName": "",
            "title": "",
            "body": "",
        },
    )
    resolved = target.resolve(repo, pr=7)
    needs: list[Need] = []
    target.build_target(resolved, needs=needs)
    assert len(needs) == 1
    assert (
        needs[0].command == "gh pr view 7 --json title,baseRefName"
    )  # as `_pr_view` ran it, no --repo
    assert str(repo) in needs[0].what
    assert needs[0].cause == "lookup-failed" and needs[0].source == "pipeline"


def test_pr_view_with_only_an_empty_body_is_not_a_need(fixture_repo, monkeypatch):
    repo, base, head = fixture_repo
    monkeypatch.setattr(
        target,
        "_pr_view",
        lambda repo_path, number: {
            "headRefOid": head,
            "baseRefOid": base,
            "headRefName": "x",
            "baseRefName": "main",
            "title": "A title",
            "body": "",
        },
    )
    needs: list[Need] = []
    target.build_target(target.resolve(repo, pr=7), needs=needs)
    assert needs == []


def test_prepare_puts_the_pipelines_failed_lookups_into_the_state(
    fixture_repo, tmp_path, monkeypatch
):
    """`prepare` passes build_target's needs through gated_need into the ReviewState it returns."""
    repo, base, head = fixture_repo
    monkeypatch.setattr(
        target,
        "_pr_view",
        lambda repo_path, number: {
            "headRefOid": head,
            "baseRefOid": base,
            "headRefName": "x",
            "baseRefName": "main",
            "title": "A title",
            "body": "",
        },
    )

    def failing_rules(repo_path, branch, needs=None):
        needs.append(
            Need(
                what=" GitHub's rules on main. ",
                cause="lookup-failed",
                source="pipeline",
                command=" gh api repos/{owner}/{repo}/rules/branches/main ",
            )
        )
        needs.append(
            Need(
                what="A lookup with a bad command.",
                cause="lookup-failed",
                source="pipeline",
                command="gh api repos/o/r -X PATCH",
            )
        )

    monkeypatch.setattr(target, "branch_rules", failing_rules)
    state, _ = orchestrator.prepare(
        orchestrator.TargetSpec(repo=str(repo), pr=7), tmp_path / "out"
    )
    assert [(n.what, n.command) for n in state.needs] == [
        (
            "GitHub's rules on main.",
            "gh api repos/{owner}/{repo}/rules/branches/main",
        ),  # stripped
        ("A lookup with a bad command.", ""),  # need_gate blanked it, the need stays
    ]
    exported = to_findings_file(state)
    assert [n["command"] for n in exported["needs"]] == [
        "gh api repos/{owner}/{repo}/rules/branches/main",
        "",
    ]


# --- verify ------------------------------------------------------------------------


async def test_unverifiable_answer_with_a_need_is_stored_and_exported(
    target_info, ctx, monkeypatch
):
    medium = medium_finding()
    prompts = use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="unverifiable",
                    impact="breaks-users",
                    topic="code",
                    reason="depends on tavily 0.8.4",
                    needs=[TAVILY_NEED],
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium]), ctx
    )
    assert (
        "fill `needs`" in prompts[0]
        and "pip download tavily-python==0.8.4 --no-deps" in prompts[0]
    )
    assert state.needs == [
        Need(
            what=TAVILY_NEED.what,
            blocks=medium.id,
            cause="unreachable",
            command=TAVILY_NEED.command,
            source="verifier",
        )
    ]
    data = to_findings_file(state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    row = next(f for f in data["findings"] if f["id"] == medium.id)
    assert row["needs"] == [
        {
            "what": TAVILY_NEED.what,
            "blocks": medium.id,
            "cause": "unreachable",
            "command": TAVILY_NEED.command,
            "where": "",
            "source": "verifier",
        }
    ]
    assert data["needs"] == row["needs"]


async def test_needs_on_a_confirmed_answer_are_not_kept(target_info, ctx, monkeypatch):
    high = high_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                    expected_behaviour="e",
                    needs=[TAVILY_NEED],
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    assert state.needs == []


# --- severity ----------------------------------------------------------------------


async def test_unverifiable_finding_with_a_need_stays_at_medium(
    target_info, ctx, monkeypatch
):
    medium, high = medium_finding(), high_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="unverifiable",
                    impact="breaks-users",
                    topic="code",
                    reason="needs the vendor's settings",
                    needs=[TAVILY_NEED],
                ),
                Verdict(
                    status="unverifiable",
                    impact="breaks-users",
                    topic="code",
                    reason="needs the vendor's settings",
                    needs=[TAVILY_NEED],
                ),
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium, high]), ctx
    )
    by_id = {f.id: f.severity for f in state.findings}
    assert by_id[medium.id] == "MEDIUM"  # not lowered to LOW
    assert by_id[high.id] == "MEDIUM"  # still lowered one step
    assert state.original_severity == {medium.id: "MEDIUM", high.id: "HIGH"}


async def test_unverifiable_finding_without_a_need_is_lowered_as_before(
    target_info, ctx, monkeypatch
):
    medium = medium_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="unverifiable",
                    impact="breaks-users",
                    topic="code",
                    reason="the code does not settle it",
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium]), ctx
    )
    assert state.findings[0].severity == "LOW"


def test_unverifiable_severity_table():
    assert [
        unverifiable_severity(s, has_need=True) for s in ("HIGH", "MEDIUM", "LOW")
    ] == ["MEDIUM", "MEDIUM", "LOW"]
    assert [
        unverifiable_severity(s, has_need=False) for s in ("HIGH", "MEDIUM", "LOW")
    ] == ["MEDIUM", "LOW", "LOW"]


# --- find --------------------------------------------------------------------------


async def test_finder_needs_are_stored_as_blocking_the_review(
    target_info, ctx, monkeypatch
):
    prompts = use(
        monkeypatch,
        {
            "find:diff-bugs": [FinderOutput(findings=[], needs=[TAVILY_NEED])],
            "find:callers": [FinderOutput(findings=[])],
            "find:trim": [FinderOutput(findings=[])],
        },
    )
    state = ReviewState(
        target=target_info,
        pieces=pieces_of(target_info),
        dimensions=["diff-bugs", "callers", "trim"],
    )
    state = await run_find(state, ctx)
    # The report asks for one `Could not check:` paragraph, which the convert call turns into a need.
    assert "`Could not check:` instead of guessing" in prompts[0]
    assert "Each paragraph of the report starting `Could not check:`" in (
        convert_prompt.build_convert("diff-bugs@1", "report")
    )
    assert state.needs == [
        Need(
            what=TAVILY_NEED.what,
            blocks="review",
            cause="unreachable",
            command=TAVILY_NEED.command,
            source="finder",
        )
    ]
    assert state.finder_reports["diff-bugs@1"].needs == [TAVILY_NEED]


async def test_a_need_without_a_command_keeps_where_in_the_file_and_is_not_checked_on_its_finding(
    target_info, ctx, monkeypatch
):
    medium = medium_finding()
    ask = NeedAsk(
        what="The Dataform workflow invocation history.",
        cause="no-access",
        where=" Console > Dataform > DolceDataform > Workflow execution logs ",
    )
    prompts = use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="unverifiable",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    needs=[ask],
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium]), ctx
    )
    assert "cannot be trusted on" in prompts[0]
    assert (
        state.needs[0].where
        == "Console > Dataform > DolceDataform > Workflow execution logs"
    )
    data = to_findings_file(state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert (
        data["needs"][0]["where"]
        == "Console > Dataform > DolceDataform > Workflow execution logs"
    )
    # With no command it is no task for a person: the finding says what it was not checked on.
    assert data["findings"][0]["unchecked"] == [
        "The Dataform workflow invocation history."
    ]
    text = render_review(state)
    assert "**Not checked:** The Dataform workflow invocation history." in text
    assert "## Needs you" not in text and "named no command" not in text


def test_finder_answer_with_needs_parses_and_an_unknown_cause_becomes_no_access():
    out = FinderOutput.model_validate(
        {"findings": [], "needs": [{"what": "w", "cause": "forbidden"}]}
    )
    assert out.needs == [NeedAsk(what="w", cause="no-access", command="")]


# --- need_gate ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, rule",
    [
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
        (
            "kubectl scale deploy api --replicas 0",
            "kubectl scale is not a read-only form",
        ),
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
        (
            "aws s3 cp a.txt s3://bucket/key",
            "aws cp is not a describe, list or get operation",
        ),
        ("aws ec2 stop-instances --instance-ids i-1", "aws stop-instances is not"),
        ("gcloud run deploy api --image x", "gcloud without one of"),
        ("az vm stop -n x -g y", "az without one of"),
        (
            "psql -c 'INSERT INTO bookings VALUES (1)'",
            "psql with INSERT can change state",
        ),
        (
            "bq query 'UPDATE d.t SET a = 1 WHERE b = 2'",
            "bq with UPDATE can change state",
        ),
    ],
)
def test_need_gate_blanks_each_kind_of_bad_command_and_keeps_the_need(command, rule):
    need = Need(
        what="The base branch's rules.",
        blocks="review",
        cause="lookup-failed",
        command=command,
    )
    result = need_gate(need)
    assert not result.passed and rule in result.reason
    kept = gated_need(need)
    assert (kept.command, kept.what, kept.blocks) == (
        "",
        "The base branch's rules.",
        "review",
    )


@pytest.mark.parametrize(
    "command",
    [
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
    ],
)
def test_need_gate_passes_short_read_only_commands(command):
    need = Need(what="w", command=command)
    assert need_gate(need).passed
    assert gated_need(need).command == command


# --- export and render -------------------------------------------------------------


def _with_needs(state: ReviewState) -> ReviewState:
    medium_id = next(
        f.id for f in state.findings if f.severity == "LOW"
    )  # the lowered unverifiable one
    return state.model_copy(
        update={
            "needs": [
                Need(
                    what="GitHub's rules on main.",
                    blocks="review",
                    cause="lookup-failed",
                    command="gh api repos/example/booking-engine/rules/branches/main",
                    source="pipeline",
                ),
                Need(
                    what="The production channel title.",
                    blocks=medium_id,
                    cause="no-access",
                    command="",
                    source="verifier",
                ),
            ]
        }
    )


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
    state = canned_state.model_copy(
        update={
            "findings": [high],
            "merged": [Merged(finding=medium, into=high.id, reason="same")],
            "needs": [
                Need(what="w", blocks=medium.id, cause="no-access", source="verifier")
            ],
        }
    )
    data = to_findings_file(state)
    assert data["needs"][0]["blocks"] == high.id
    assert (
        next(f for f in data["findings"] if f["id"] == high.id)["needs"][0]["what"]
        == "w"
    )


def test_summary_has_a_needs_you_section_after_the_counts(canned_state):
    text = render_summary(_with_needs(canned_state))
    lines = text.splitlines()
    counts = next(i for i, line in enumerate(lines) if line.startswith("Findings:"))
    assert lines[counts + 2] == "## Needs you"
    assert (
        "- **N1** `gh api repos/example/booking-engine/rules/branches/main` — "
        "GitHub's rules on main. Bears on the review."
    ) in text
    assert "`answers/<id>.txt`" in text
    assert "python -m review_pipeline answers <dir> --trust-repo" in text
    # A need with no command is no row: its finding is listed with "Not checked" in the review.
    assert "The production channel title." not in text
    assert "## Needs you" not in render_summary(canned_state)


def test_needs_you_names_no_need_without_a_command(canned_state):
    for text in (
        render_review(_with_needs(canned_state)),
        render_summary(_with_needs(canned_state)),
    ):
        assert (
            "named no command" not in text and "a place to look, or neither" not in text
        )


def test_a_command_bearing_only_on_a_finding_not_listed_is_left_out_of_the_summary(
    canned_state,
):
    # Critical mode leaves out the unrated finding the command bears on; the review's appendix keeps it.
    state = _with_needs(canned_state)
    hidden = next(f.id for f in state.findings if f.severity == "LOW")
    state.needs.append(
        Need(
            what="Its title.",
            blocks=hidden,
            command="gh api repos/o/r",
            source="verifier",
        )
    )
    state = state.model_copy(update={"mode": "critical"})
    review, summary = render_review(state), render_summary(state)
    appendix = review[review.index("## Appendix") :]
    assert "  - Not checked: The production channel title." in appendix
    assert "  - Needs you: N2" in appendix
    assert "`gh api repos/o/r`" not in summary
    assert (
        "1 more command bears only on findings not listed; the full review's Needs you has them."
        in summary
    )
    assert "more command" not in render_summary(
        state.model_copy(update={"mode": "full"})
    )  # full mode lists that finding


def test_review_lists_commands_before_the_findings_and_names_them_under_each(
    canned_state,
):
    state = _with_needs(canned_state)
    medium = next(f.id for f in state.findings if f.severity == "LOW")
    state.needs.append(
        Need(
            what="Its title.",
            blocks=medium,
            command="gh api repos/o/r",
            source="verifier",
        )
    )
    text = render_review(state)
    # The opening list of findings comes first, then Needs you, then each finding in full.
    assert (
        text.index("## Findings") < text.index("## Needs you") < text.index("## Code")
    )
    assert "- **N2** `gh api repos/o/r` — Its title. Bears on `" in text
    assert "**Not checked:** The production channel title." in text
    assert "**Needs you:** N2 (under Needs you above)" in text


def test_a_need_of_the_review_with_no_command_is_under_not_checked(canned_state):
    state = canned_state.model_copy(
        update={"needs": [Need(what="The wiki page.", source="finder")], "coverage": []}
    )
    need_coverage(state)
    review, summary = render_review(state), render_summary(state)
    assert "## Not checked\n\nWhat the review could not check" in review
    assert "- The wiki page." in review and "## Needs you" not in review
    assert "1 thing the review could not check and knows no command for" in summary


# --- needs grouped by command ---------------------------------------------------

CACHE = "aws elasticache describe-cache-clusters --cache-cluster-id example-cache"
DOCS = "gh api repos/example/app/contents/docs/api.md"
FLAGS = "kubectl get configmap flags --namespace example -o yaml"


def _eleven_needs(finding_id: str) -> list[Need]:
    """The shape of a real review's 11 needs: 3 commands, each worded differently by each agent, and 4 with none."""

    def n(what, command="", blocks="review", source="finder", where=""):
        return Need(
            what=what, blocks=blocks, command=command, source=source, where=where
        )

    return [
        n("Whether example-cache really runs Redis 7, as the comment says.", CACHE),
        n("Which page size the API docs give.", DOCS),
        n("Whether the API docs still allow 500 items a page.", DOCS),
        n("Whether the version comment is true.", CACHE),
        n("How the billing team reads the nightly export."),
        n("Whether the two repository ids are the app's.", where="GitHub settings"),
        n("Whether the vendor still retries on a 429."),
        n(
            "The engine version of example-cache, which config.py:12 claims is 7.",
            CACHE,
        ),
        n("Whether the checkout flag is on.", FLAGS),
        n("Whether the wiki page the README links still exists."),
        n(
            "The page size the API docs give.",
            DOCS,
            blocks=finding_id,
            source="verifier",
        ),
    ]


def test_need_commands_group_needs_by_command_in_first_seen_order():
    needs = _eleven_needs("abcdef0123")
    groups = need_commands(needs)
    assert [(g.id, g.command) for g in groups] == [
        ("N1", CACHE),
        ("N2", DOCS),
        ("N3", FLAGS),
    ]
    cache = groups[0]
    assert cache.why == "Whether the version comment is true."  # the shortest `what`
    assert len(cache.whats) == 3 and cache.blocks == ["review"]
    # "review" is dropped once a group names a finding.
    assert groups[1].blocks == ["abcdef0123"] and len(groups[1].whats) == 3


def test_a_real_reviews_eleven_needs_give_three_commands():
    # The 4 needs with no command form no group: they are not tasks for a person.
    assert len(need_commands(_eleven_needs("abcdef0123"))) == 3


def test_need_commands_normalise_whitespace_and_keep_the_first_where():
    needs = [
        Need(what="a longer what", command="  gh api  repos/o/r\t--jq .id "),
        Need(what="short", command="gh api repos/o/r --jq .id", where="Settings"),
        Need(what="other", command="gh api repos/o/r", where="ignored", blocks="f2"),
        Need(what="b", command="gh api repos/o/r", blocks="f1"),
    ]
    first, second = need_commands(needs)
    assert first.command == "gh api repos/o/r --jq .id"
    assert first.whats == ["a longer what", "short"] and first.where == "Settings"
    assert second.blocks == ["f1", "f2"] and second.where == "ignored"


def test_a_no_command_need_on_a_finding_is_exported_as_unchecked(canned_state):
    high = canned_state.findings[0]
    state = canned_state.model_copy(
        update={
            "needs": [
                Need(what="The production title.", blocks=high.id, source="verifier"),
                Need(what="Rules.", blocks=high.id, command="gh api repos/o/r/rules"),
            ]
        }
    )
    data = to_findings_file(state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    row = next(f for f in data["findings"] if f["id"] == high.id)
    assert row["unchecked"] == ["The production title."]
    assert data["need_commands"] == [
        {
            "id": "N1",
            "command": "gh api repos/o/r/rules",
            "why": "Rules.",
            "blocks": [high.id],
            "where": "",
            "whats": ["Rules."],
        }
    ]
    assert all("unchecked" not in f for f in data["findings"] if f["id"] != high.id)


def test_a_no_command_need_of_the_review_is_a_coverage_record_once(canned_state):
    state = canned_state.model_copy(
        update={
            "needs": [
                Need(what="The wiki page.", source="finder"),
                Need(what="The bucket policy.", source="verifier"),
                Need(
                    what="Rules.", command="gh api repos/o/r/rules", source="pipeline"
                ),
            ],
            "coverage": [],
            "errors": [],
        }
    )
    need_coverage(state)
    need_coverage(state)  # again, as a resumed export does: nothing is added twice
    assert [(c.stage, c.reason, c.level, c.detail) for c in state.coverage] == [
        ("find", "no-command", "info", "The wiki page."),
        ("verify", "no-command", "info", "The bucket policy."),
    ]
    assert state.errors == []
    checks = to_checks_file(state)
    jsonschema.validate(checks, json.loads(CHECKS_SCHEMA.read_text()))
    assert [c["detail"] for c in checks["coverage"]] == [
        "The wiki page.",
        "The bucket policy.",
    ]
    assert "need_commands" in to_findings_file(state)
