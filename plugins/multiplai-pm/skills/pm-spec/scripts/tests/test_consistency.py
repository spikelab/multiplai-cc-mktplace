"""The consistency check across the proposal, the use cases and the requirements (item 12)."""

from __future__ import annotations

from conftest import CAPS, change_dir, meta, run, state


def finding(type_="contradiction", severity="major", files=("requirements/rate-limit.md",
                                                            "requirements/retry-policy.md")):
    return {"type": type_, "severity": severity, "description": f"a {type_} between the two",
            "places": [{"file": f, "where": "Requirement"} for f in files], "suggestion": "make them agree"}


def recheck(status="resolved", *new):
    return {"findings": list(new), "prior_assessments": [{"finding_id": "CC-F1", "status": status,
                                                          "evidence": "the lines"}]}


def test_the_agent_is_read_only_and_reads_every_spec_file(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt, kw = [(p, kw) for label, p, kw in model.calls if label == "consistency"][0]
    assert kw["allowed_tools"] == ["Read", "Glob", "Grep"]
    for text in ("## proposal.md", "## use-cases.md", *(f"requirements/{c}.md" for c in CAPS)):
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


def test_a_major_finding_rewrites_only_the_cited_requirements_once_then_rechecks(repo, model):
    model["consistency"] = [{"findings": [finding("duplicate", files=("requirements/rate-limit.md",
                                                                     "proposal.md"))]}, recheck()]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert [label for label in model.labels() if ":rewrite:" in label] == ["consistency:rewrite:rate-limit"]
    assert "CC-F1" in model.prompts("consistency:rewrite:rate-limit")[0]
    recheck_prompt = model.exactly("consistency")[1]
    assert "Findings the rewrite was asked to fix" in recheck_prompt and "CC-F1" in recheck_prompt
    cdir = change_dir(repo)
    assert "the rewritten rate-limit" in (cdir / "requirements" / "rate-limit.md").read_text()
    step = state(cdir)["steps"]["consistency"]
    assert step["outcome"] == "passed" and step["repair_findings"][0]["status"] == "resolved"
    assert meta(cdir)["spec_status"] == "ready"


def test_a_finding_that_cites_no_requirements_file_rewrites_them_all(repo, model):
    model["consistency"] = [{"findings": [finding("gap", files=("proposal.md", "use-cases.md"))]}, recheck()]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert sorted(label for label in model.labels() if ":rewrite:" in label) == \
        [f"consistency:rewrite:{c}" for c in CAPS]


def test_a_listed_finding_not_resolved_blocks(repo, model, capsys):
    model["consistency"] = [{"findings": [finding()]}, recheck("partially_resolved")]
    assert run(repo, "new", "throttle", "--auto") == 4
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert f"SPEC:BLOCKED:{cdir}" in out and "CC-F1 partially_resolved" in out
    assert meta(cdir)["spec_status"] == "draft"
    assert not (cdir / "tickets.json").exists()


def test_an_unknown_status_is_not_resolved(repo, model):
    model["consistency"] = [{"findings": [finding()]}, recheck("fixed-ish")]
    assert run(repo, "new", "throttle", "--auto") == 4
    assert state(change_dir(repo))["steps"]["consistency"]["blocking_findings"][0]["status"] == "cannot_verify"


def test_a_new_critical_finding_on_the_recheck_blocks_a_new_major_does_not(repo, model, tmp_path):
    model["consistency"] = [{"findings": [finding()]}, recheck("resolved", finding("gap", "major"))]
    assert run(repo, "new", "throttle", "--auto") == 0
    other = tmp_path / "other"
    other.mkdir()
    from conftest import git, write
    git(other, "init", "-q", "-b", "main")
    write(other / "a", "a")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "i")
    model["consistency"] = [{"findings": [finding()]}, recheck("resolved", finding("gap", "critical"))]
    model.count.clear()
    assert run(other, "new", "throttle", "--auto") == 4


def test_a_rewrite_that_breaks_the_format_blocks(repo, model, capsys):
    model["consistency"] = {"findings": [finding()]}
    model["consistency:rewrite"] = "not a requirements file"
    assert run(repo, "new", "throttle", "--auto") == 4
    assert "is not a requirements file" in capsys.readouterr().out
    assert len(model.exactly("consistency")) == 1  # no recheck of a broken rewrite


def test_a_finding_needs_two_places(repo, model):
    one_place = finding()
    one_place["places"] = one_place["places"][:1]
    model["consistency"] = {"findings": [one_place]}
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "consistency:reformat" in model.labels()
