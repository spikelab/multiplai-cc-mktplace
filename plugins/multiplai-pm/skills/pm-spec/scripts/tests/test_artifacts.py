"""The proposal, the use cases and each component's specs.md. Adapted from
buildme's `test_spec_generator.py` (skipping finished artifacts, the
tool-free calls)."""

from __future__ import annotations

from conftest import (
    CAPS,
    TWO,
    change_dir,
    git,
    proposal,
    rewrite_answer,
    run,
    source_dir,
    specs,
    state,
    write,
)
from multiplai_core.specs import requirement_texts


def test_each_component_gets_one_specs_call_and_file(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    for cid in ("api", "ui"):
        comp = cdir / "components" / cid
        assert (comp / "specs.md").read_text() == specs()
        assert list(requirement_texts(comp)) == list(CAPS)
        prompt = model.exactly(f"spec:specs:{cid}")[0]
        assert f"## The component: {cid}\n# Plan: Build {cid}" in prompt
        assert "# Request throttling" in prompt and "# Use cases" in prompt
        assert "- prototype/mockup.html" in prompt
    assert set(state(cdir)["artifacts"]) == {
        "proposal",
        "use-cases",
        "components/api/specs",
        "components/ui/specs",
    }


def test_the_proposal_names_no_capability(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("spec:proposal")[0]
    assert "## Capabilities" not in prompt and "New Capabilities" not in prompt
    assert "Name no capabilities" in prompt


def test_artifact_calls_hold_no_tools(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    for label, _, kw in model.calls:
        if label.startswith("spec:") or label == "components":
            assert not kw["allowed_tools"], label


def test_invalid_specs_fail_the_run(repo, model, capsys):
    model["spec:specs"] = (
        "# Capability: Bad Name\n\n### Requirement: no scenario\nThe system SHALL.\n"
    )
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "Invalid specs.md for throttle" in capsys.readouterr().out
    assert not (change_dir(repo) / "specs.md").exists()


def test_an_empty_proposal_is_asked_for_again(repo, model):
    model["spec:proposal"] = ["", proposal()]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert "the proposal is empty" in model.exactly("spec:proposal")[1]


def test_files_a_person_wrote_before_the_run_are_kept(repo, model):
    """Written in the user's checkout: pm-spec copies them into its worktree."""
    cdir, hand = change_dir(repo), source_dir(repo)
    write(hand / "proposal.md", proposal(title="Hand written"))
    write(hand / "specs.md", specs(("rate-limit",), rule="the hand rule"))
    assert run(repo, "new", "throttle", "--auto") == 0
    assert "Hand written" in (cdir / "proposal.md").read_text()
    assert "the hand rule" in (cdir / "specs.md").read_text()
    assert model.calls_to("spec:proposal") == 0
    assert model.calls_to("spec:specs") == 0


def test_the_registry_reaches_the_specs_prompt_not_the_proposal(repo, model):
    write(
        repo / "specs" / "registry" / "rate-limit.md",
        "### Requirement: Existing limit\nREGISTRY-BODY\n",
    )
    git(repo, "add", "specs")
    git(repo, "commit", "-q", "-m", "registry")
    git(repo, "push", "-q", "origin", "main")
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("spec:specs:throttle")[0]
    assert "REGISTRY-BODY" in prompt
    assert "Capabilities in specs/registry: rate-limit" in prompt
    assert "takes exactly the name listed here" in prompt
    assert "rate-limit" not in model.exactly("spec:proposal")[0]
    assert (
        "`## MODIFIED Requirements` and `## REMOVED Requirements` only in a "
        "capability listed here: rate-limit"
    ) in prompt


def test_with_no_registry_the_specs_prompt_allows_only_added_requirements(repo, model):
    """With no specs/registry/ file the code's current behaviour is unrecorded,
    so a MODIFIED or REMOVED requirement has nothing to modify or remove."""
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("spec:specs:throttle")[0]
    assert "for behaviour that exists today" not in prompt
    assert "specs/registry has no capability files" in prompt
    assert "Write only `## ADDED Requirements`" in prompt


def test_every_markdown_file_the_model_writes_ends_in_one_newline(repo, model):
    """The model's text comes back with no final newline, or with several: the
    file on disk ends in exactly one, and the run records that text."""
    model["components"] = TWO
    model["spec:proposal"] = lambda prompt, label, kw: proposal().rstrip("\n")
    model["spec:use-cases"] = (
        "# Use cases\n\n## Personas\n\n### operator\n- **Who they are:** ops\n\n\n\n"
    )
    model["spec:specs"] = lambda prompt, label, kw: specs().rstrip("\n") + "  "
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    for rel in (
        "proposal.md",
        "use-cases.md",
        "interview.md",
        "components.md",
        "components/api/plan.md",
        "components/api/specs.md",
        "components/ui/specs.md",
    ):
        text = (cdir / rel).read_text()
        assert text.endswith("\n") and not text.endswith("\n\n"), rel
        assert not text.endswith(" \n"), rel


def test_a_rewritten_specs_file_ends_in_one_newline(repo, model):
    model["consistency"] = [
        {
            "findings": [
                {
                    "type": "gap",
                    "severity": "major",
                    "description": "a gap",
                    "places": [
                        {"file": "specs.md", "where": "rate-limit"},
                        {"file": "proposal.md", "where": "Why"},
                    ],
                }
            ]
        },
        {
            "findings": [],
            "prior_assessments": [
                {"finding_id": "CC-F1", "status": "resolved", "evidence": "x"}
            ],
        },
    ]
    model["rewrite"] = lambda prompt, label, kw: rewrite_answer(specs(rule="fixed"))
    assert run(repo, "new", "throttle", "--auto") == 0
    text = (change_dir(repo) / "specs.md").read_text()
    assert "fixed" in text
    assert text.endswith("\n") and not text.endswith("\n\n")
