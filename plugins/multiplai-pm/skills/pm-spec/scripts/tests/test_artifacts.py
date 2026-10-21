"""The proposal, the use cases and the requirements, one capability at a time.
Adapted from buildme's `test_spec_generator.py` (capability harvesting,
skipping finished artifacts, the tool-free use-cases call, superseded
requirements moved aside)."""

from __future__ import annotations

from conftest import CAPS, change_dir, proposal, requirements, run, state, write


def test_each_capability_gets_its_own_call_and_file(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    for cap in CAPS:
        assert (cdir / "requirements" / f"{cap}.md").read_text() == requirements(cap)
        prompt = model.prompts(f"spec:requirements:{cap}")[0]
        assert f"`{cap}`" in prompt or cap in prompt
    assert set(state(cdir)["artifacts"]) == {"proposal", "use-cases", *(f"requirements/{c}" for c in CAPS)}


def test_artifact_calls_hold_no_tools(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    for label, _, kw in model.calls:
        if label.startswith("spec:"):
            assert not kw["allowed_tools"], label


def test_invalid_requirements_fail_the_run(repo, model, capsys):
    model["spec:requirements:retry-policy"] = "### Requirement: no scenario\nThe system SHALL.\n"
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "Invalid requirements for retry-policy" in capsys.readouterr().out
    assert not (change_dir(repo) / "requirements" / "retry-policy.md").exists()


def test_a_proposal_with_no_capability_is_asked_for_again(repo, model):
    model["spec:proposal"] = ["# T\n\n## Why\n\nx\n", proposal()]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert "names no capability" in model.prompts("spec:proposal")[1]


def test_files_a_person_wrote_before_the_run_are_adopted(repo, model):
    cdir = change_dir(repo)
    write(cdir / "proposal.md", proposal(caps=("rate-limit",), title="Hand written"))
    write(cdir / "requirements" / "rate-limit.md", requirements("rate-limit", rule="the hand rule"))
    assert run(repo, "new", "throttle", "--auto") == 0
    assert "Hand written" in (cdir / "proposal.md").read_text()
    assert "the hand rule" in (cdir / "requirements" / "rate-limit.md").read_text()
    assert model.calls_to("spec:proposal") == 0
    assert model.calls_to("spec:requirements") == 0


def test_requirements_the_proposal_no_longer_names_are_moved_aside(repo, model):
    cdir = change_dir(repo)
    write(cdir / "requirements" / "old-cap.md", requirements("old-cap"))
    assert run(repo, "new", "throttle", "--auto") == 0
    assert not (cdir / "requirements" / "old-cap.md").exists()
    moved = list((cdir / "superseded-requirements").glob("old-cap-*.md"))
    assert len(moved) == 1 and moved[0].read_text() == requirements("old-cap")


def test_the_registry_text_reaches_the_requirements_prompt(repo, model):
    write(repo / "specs" / "registry" / "rate-limit.md", "### Requirement: Existing limit\nREGISTRY-BODY\n")
    assert run(repo, "new", "throttle", "--auto") == 0
    assert "REGISTRY-BODY" in model.prompts("spec:requirements:rate-limit")[0]
    assert "REGISTRY-BODY" not in model.prompts("spec:requirements:retry-policy")[0]
    assert "rate-limit" in model.prompts("spec:proposal")[0]


def test_ticket_children_are_cited_in_sources(repo, model, tmp_path):
    ticket = write(tmp_path / "t.md", "Throttle the API.\n")
    assert run(repo, "new", "throttle", "--auto", "--no-epic-split", "--ticket-file", str(ticket),
               "--ticket-ref", "EPIC-9", "--ticket-child", "T-1", "--ticket-child", "T-2") == 0
    assert "ticket <ref>" in model.prompts("spec:proposal")[0]
    from pm_spec_pipeline.artifacts import sources
    cited = sources((change_dir(repo) / "proposal.md").read_text())
    assert cited["rate-limit"]["tickets"] == ["T-1"] and cited["retry-policy"]["tickets"] == ["T-2"]
