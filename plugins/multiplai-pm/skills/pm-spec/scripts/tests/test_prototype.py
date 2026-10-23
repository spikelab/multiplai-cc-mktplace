"""The prototype, the first step of stage 3. Adapted from buildme's
`test_prototype_steps.py` (the gate, the retry, BLOCKED without a retry, the
write boundary)."""

from __future__ import annotations

from conftest import (
    TWO,
    agent,
    change_dir,
    git,
    proposal,
    run,
    source_dir,
    state,
    write,
)
from pm_spec_pipeline import prototype as P


def test_the_prototype_runs_after_the_components_and_before_the_specs(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    labels = model.labels()
    assert (
        labels.index("components")
        < labels.index("prototype")
        < labels.index("spec:specs:api")
    )
    assert (cdir / "prototype" / "mockup.html").is_file() and (
        cdir / "prototype" / "NOTES.md"
    ).is_file()
    assert state(cdir)["steps"]["prototype"]["outcome"] == "passed"
    assert not any(":rewrite:" in label for label in labels)
    prompt, kw = next((p, kw) for label, p, kw in model.calls if label == "prototype")
    assert kw["allowed_tools"] == ["Read", "Write", "Glob", "Grep"]
    assert kw["cwd"] == str(cdir / "prototype")
    # It reads the change and the plans, and shows the shape, never behaviour.
    assert "# Request throttling" in prompt and "### components/ui/plan.md" in prompt
    assert "copied from the" in prompt and "No behaviour and no runnable code" in prompt


def test_a_backend_change_gets_one_too(repo, model):
    """Whatever helps picture the change: endpoint signatures, for an API."""
    model["spec:proposal"] = lambda *a: proposal(
        extra=" Tune the database queue worker."
    )
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("prototype") == 1


def test_files_already_in_prototype_are_kept_and_no_agent_runs(repo, model):
    write(source_dir(repo) / "prototype" / "settings.html", "<html>the PM's</html>\n")
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert "prototype" not in model.labels()
    assert (
        cdir / "prototype" / "settings.html"
    ).read_text() == "<html>the PM's</html>\n"
    assert not (cdir / "prototype" / "NOTES.md").exists()
    assert "- prototype/settings.html" in model.exactly("spec:specs:throttle")[0]
    assert (
        git(change_dir(repo).parents[2], "status", "--porcelain", "--", "specs") == ""
    )


def test_no_prototype_turns_it_off(repo, model):
    assert run(repo, "new", "throttle", "--auto", "--no-prototype") == 0
    assert "prototype" not in model.labels()
    assert state(change_dir(repo))["steps"]["prototype"]["outcome"] == "skipped"
    assert "## Prototype\n(none)" in model.exactly("spec:specs:throttle")[0]


def test_the_specs_config_can_turn_it_off(repo, model):
    write(repo / "specs" / "config.yaml", "prototype:\n  enabled: false\n")
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("prototype") == 0


def test_the_gate_retries_once_then_fails(repo, model, capsys):
    model["prototype"] = agent(notes=None)
    assert run(repo, "new", "throttle", "--auto") == 1
    assert model.calls_to("prototype") == 2
    assert "NOTES.md missing" in capsys.readouterr().out


def test_blocked_fails_without_a_retry(repo, model):
    model["prototype"] = agent("- mockup.html: nothing\nSTATUS: BLOCKED\n")
    assert run(repo, "new", "throttle", "--auto") == 1
    assert model.calls_to("prototype") == 1


def test_writes_outside_the_prototype_dir_fail_the_stage(repo, model, tmp_path, capsys):
    model["prototype"] = agent(outside=tmp_path / "elsewhere.txt")
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "wrote outside" in capsys.readouterr().out


def test_the_gate_reads_the_status_with_markdown_decoration(tmp_path):
    write(tmp_path / "a.md", "x\n")
    write(tmp_path / "NOTES.md", "**STATUS**: needs_context\n")
    assert P.prototype_gate(tmp_path).action == "blocked"
    write(tmp_path / "NOTES.md", "STATUS: DONE\n")
    assert P.prototype_gate(tmp_path).passed


def test_relative_paths_inside_the_dir_are_allowed(tmp_path):
    assert (
        P.files_outside(["mockup.html", str(tmp_path / "x" / "a")], tmp_path / "x")
        == []
    )
    assert P.files_outside(["../a"], tmp_path / "x") == [
        str((tmp_path / "a").resolve())
    ]


def test_the_reviewers_notes_lose_the_status_line_the_gate_read(repo, model):
    """`STATUS:` is for the gate; the NOTES.md the reviewers get ends with the
    agent's questions."""
    model["prototype"] = agent(
        "- mockup.html: the limits page\n\nQUESTIONS: none\n\n```\nSTATUS: DONE\n```\n"
    )
    assert run(repo, "new", "throttle", "--auto") == 0
    notes = (change_dir(repo) / "prototype" / "NOTES.md").read_text()
    assert notes == "- mockup.html: the limits page\n\nQUESTIONS: none\n"
    assert state(change_dir(repo))["steps"]["prototype"]["outcome"] == "passed"


def test_the_prompt_keeps_pipeline_remarks_out_of_the_notes(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("prototype")[0]
    assert "NOTES.md is for the reviewers" in prompt
    assert "only when you found some" in prompt


def test_reviewer_notes_drops_every_status_line_and_its_empty_fence():
    assert P.reviewer_notes("a\n\n**STATUS**: done_with_concerns\n") == "a\n"
    assert P.reviewer_notes("STATUS: DONE\n\n# Files\n- a\n") == "# Files\n- a\n"
    assert P.reviewer_notes("a\n```\nSTATUS: DONE\n```\n\n") == "a\n"
    assert P.reviewer_notes("a\n```\ncode\n```\n") == "a\n```\ncode\n```\n"
    assert P.reviewer_notes("a\nThe status: fine\n") == "a\nThe status: fine\n"
