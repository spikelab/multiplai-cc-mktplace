"""The consistency check across the proposal, the use cases, the plans and the specs."""

from __future__ import annotations

from conftest import (
    TWO,
    change_dir,
    meta,
    proposal,
    run,
    source_dir,
    specs,
    state,
    write,
)
from pm_spec_pipeline.rewrite import cited_units


def finding(
    type_="contradiction",
    severity="major",
    files=("components/api/specs.md", "components/ui/specs.md"),
):
    return {
        "type": type_,
        "severity": severity,
        "description": f"a {type_} between the two",
        "places": [{"file": f, "where": "Requirement"} for f in files],
        "suggestion": "make them agree",
    }


def recheck(status="resolved", *new):
    return {
        "findings": list(new),
        "prior_assessments": [
            {"finding_id": "CC-F1", "status": status, "evidence": "the lines"}
        ],
    }


def test_the_agent_is_read_only_and_reads_every_spec_file(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt, kw = [(p, kw) for label, p, kw in model.calls if label == "consistency"][0]
    assert kw["allowed_tools"] == ["Read", "Glob", "Grep"]
    for text in (
        "## proposal.md",
        "## use-cases.md",
        "### components/api/plan.md",
        "### components/ui/plan.md",
        "### components/api/specs.md\n# Capability: rate-limit",
        "### components/ui/specs.md",
    ):
        assert text in prompt
    assert all(t in prompt for t in ("`contradiction`", "`gap`", "`duplicate`"))


def test_minor_findings_are_advisory_and_pass(repo, model, capsys):
    model["consistency"] = {"findings": [finding(severity="minor")]}
    assert run(repo, "new", "throttle", "--auto") == 0
    out = capsys.readouterr().out
    assert "advisory findings" in out and "SPEC:READY:" in out
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "passed" and step["repair_attempts"] == 0
    assert step["advisory_findings"][0]["category"] == "contradiction"


def test_a_major_finding_rewrites_only_the_cited_specs_once_then_rechecks(repo, model):
    model["components"] = TWO
    model["consistency"] = [
        {
            "findings": [
                finding("duplicate", files=("components/api/specs.md", "proposal.md"))
            ]
        },
        recheck(),
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "consistency:rewrite:api"
    ]
    rewrite_prompt = model.prompts("consistency:rewrite:api")[0]
    assert (
        "CC-F1" in rewrite_prompt
        and "## The file to revise: components/api/specs.md" in rewrite_prompt
    )
    recheck_prompt = model.exactly("consistency")[1]
    assert (
        "Findings the rewrite was asked to fix" in recheck_prompt
        and "CC-F1" in recheck_prompt
    )
    cdir = change_dir(repo)
    assert "the rewritten api" in (cdir / "components" / "api" / "specs.md").read_text()
    assert "the rewritten" not in (cdir / "components" / "ui" / "specs.md").read_text()
    step = state(cdir)["steps"]["consistency"]
    assert (
        step["outcome"] == "passed"
        and step["repair_findings"][0]["status"] == "resolved"
    )
    assert meta(cdir)["spec_status"] == "ready"
    assert meta(cdir / "components" / "api")["spec_status"] == "ready"


def test_a_finding_that_cites_no_specs_file_rewrites_them_all(repo, model):
    model["components"] = TWO
    model["consistency"] = [
        {"findings": [finding("gap", files=("proposal.md", "use-cases.md"))]},
        recheck(),
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert sorted(label for label in model.labels() if ":rewrite:" in label) == [
        "consistency:rewrite:api",
        "consistency:rewrite:ui",
    ]


def test_a_listed_finding_not_resolved_blocks(repo, model, capsys):
    model["consistency"] = [{"findings": [finding()]}, recheck("partially_resolved")]
    assert run(repo, "new", "throttle", "--auto") == 4
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert f"SPEC:BLOCKED:{cdir}" in out and "CC-F1 partially_resolved" in out
    assert meta(cdir)["spec_status"] == "draft"
    assert state(cdir)["handoff"] is None


def test_an_unknown_status_is_not_resolved(repo, model):
    model["consistency"] = [{"findings": [finding()]}, recheck("fixed-ish")]
    assert run(repo, "new", "throttle", "--auto") == 4
    assert (
        state(change_dir(repo))["steps"]["consistency"]["blocking_findings"][0][
            "status"
        ]
        == "cannot_verify"
    )


def test_a_new_critical_finding_on_the_recheck_blocks_a_new_major_does_not(
    repo, model, tmp_path
):
    model["consistency"] = [
        {"findings": [finding()]},
        recheck("resolved", finding("gap", "major")),
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    from conftest import make_repo

    other = make_repo(tmp_path / "other" / "proj")
    model["consistency"] = [
        {"findings": [finding()]},
        recheck("resolved", finding("gap", "critical")),
    ]
    model.count.clear()
    assert run(other, "new", "throttle", "--auto") == 4


def test_a_rewrite_that_breaks_the_format_blocks(repo, model, capsys):
    model["consistency"] = {"findings": [finding()]}
    model["components"] = TWO
    model["consistency:rewrite"] = "not a specs file"
    assert run(repo, "new", "throttle", "--auto") == 4
    assert "is not a specs file" in capsys.readouterr().out
    assert len(model.exactly("consistency")) == 1  # no recheck of a broken rewrite


def test_a_finding_needs_two_places(repo, model):
    one_place = finding()
    one_place["places"] = one_place["places"][:1]
    model["consistency"] = {"findings": [one_place]}
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "consistency:reformat" in model.labels()


def test_a_broken_specs_file_is_a_finding_not_a_crash(repo, model):
    """A specs.md someone wrote with a capability name that is not kebab-case:
    kept as written, then the check reports it and its one rewrite fixes it."""
    hand = source_dir(repo)
    write(hand / "proposal.md", proposal())
    write(
        hand / "specs.md",
        specs().replace("# Capability: rate-limit", "# Capability: Rate Limit"),
    )
    model["consistency"] = [
        {"findings": []},
        recheck("resolved"),
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("spec:specs") == 0
    cdir = change_dir(repo)
    listed = state(cdir)["steps"]["consistency"]["repair_findings"]
    assert listed[0]["category"] == "format" and listed[0]["finding_id"] == "CC-F1"
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "consistency:rewrite:throttle"
    ]
    assert "# Capability: rate-limit" in (cdir / "specs.md").read_text()


def test_a_finding_names_its_specs_file_by_path_or_by_capability(repo, model):
    model["components"] = TWO
    model["spec:specs:ui"] = specs(("ui-page",))
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert cited_units(cdir, {"places": [{"file": "components/ui/specs.md"}]}) == [
        "components/ui/specs.md"
    ]
    assert cited_units(
        cdir, {"places": [{"file": "specs/changes/throttle/components/api/specs.md"}]}
    ) == ["components/api/specs.md"]
    assert cited_units(cdir, {"description": "`ui-page` contradicts itself"}) == [
        "components/ui/specs.md"
    ]
    assert cited_units(
        cdir, {"places": [{"file": "specs.md", "where": "ui-page: Requirement"}]}
    ) == ["components/ui/specs.md"]
    assert cited_units(cdir, {"places": [{"file": "proposal.md"}]}) == []
