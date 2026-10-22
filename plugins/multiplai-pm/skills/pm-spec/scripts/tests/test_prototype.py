"""The UI or output-format prototype. Adapted from buildme's
`test_prototype_steps.py` (the gate, the retry, BLOCKED without a retry, the
write boundary, findings from DISPROVES and OPEN_QUESTIONS) and its
`prototype_required` cases."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CAPS, change_dir, proposal, run, state, write
from pm_spec_pipeline import prototype as P

NOTES_CLEAN = "PROVES: the layout\nDISPROVES: none\nOPEN_QUESTIONS: none\nSTATUS: DONE\n"
NOTES_FINDINGS = ("PROVES: the layout\nDISPROVES: a per-minute counter does not fit the header\n"
                  "OPEN_QUESTIONS: show the retry time in seconds or as a clock time?\nSTATUS: DONE\n")


def agent(notes=NOTES_CLEAN, *, artifact="mockup.html", outside: Path | None = None):
    """A prototype agent stand-in: writes its files where it was told to."""
    def handler(prompt, label, kw):
        root = Path(kw["cwd"])
        files = []
        if artifact:
            files.append(str(write(root / artifact, "<html>sample</html>\n")))
        if notes is not None:
            files.append(str(write(root / "NOTES.md", notes)))
        if outside is not None:
            files.append(str(write(outside, "oops\n")))
        return "done", files
    return handler


def ui_proposal(prompt=""):
    return proposal(extra=" Show the limits on a dashboard page.")


def test_a_ui_change_runs_the_prototype_and_a_clean_one_changes_nothing(repo, model):
    model["spec:proposal"] = lambda *a: ui_proposal()
    model["prototype"] = agent()
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert (cdir / "prototype" / "mockup.html").is_file()
    assert state(cdir)["steps"]["prototype"]["outcome"] == "passed"
    assert not any(":rewrite:" in label for label in model.labels())
    kw = {label: kw for label, _, kw in model.calls}["prototype"]
    assert kw["allowed_tools"] == ["Read", "Write", "Glob", "Grep"]
    assert kw["cwd"] == str(cdir / "prototype")


def test_a_backend_change_skips_it(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    assert state(change_dir(repo))["steps"]["prototype"]["outcome"] == "skipped"
    assert "prototype" not in model.labels()


def test_no_prototype_wins_over_a_ui_change(repo, model):
    model["spec:proposal"] = lambda *a: ui_proposal()
    assert run(repo, "new", "throttle", "--auto", "--no-prototype") == 0
    assert "prototype" not in model.labels()


def test_prototype_forces_it_on(repo, model):
    model["prototype"] = agent()
    assert run(repo, "new", "throttle", "--auto", "--prototype") == 0
    assert model.calls_to("prototype") == 1


def test_the_specs_config_can_force_it_on(repo, model):
    write(repo / "specs" / "config.yaml", "prototype:\n  enabled: true\n")
    model["prototype"] = agent()
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("prototype") == 1


def test_disproves_and_open_questions_rewrite_the_proposal_and_requirements(repo, model):
    model["spec:proposal"] = lambda *a: ui_proposal()
    model["prototype"] = agent(NOTES_FINDINGS)
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    rewrites = [label for label in model.labels() if ":rewrite:" in label]
    assert rewrites == ["prototype:rewrite:proposal", *(f"prototype:rewrite:{c}" for c in CAPS)]
    prompt = model.prompts("prototype:rewrite:proposal")[0]
    assert "PT-F1" in prompt and "per-minute counter" in prompt and "PT-F2" in prompt
    for cap in CAPS:
        assert f"the rewritten {cap}" in (cdir / "requirements" / f"{cap}.md").read_text()
    step = state(cdir)["steps"]["prototype"]
    assert step["outcome"] == "passed" and step["repair_attempts"] == 1
    assert [f["finding_id"] for f in step["repair_findings"]] == ["PT-F1", "PT-F2"]
    assert not (cdir / "design.md").exists()


def test_a_rewrite_that_drops_a_capability_moves_its_requirements_aside(repo, model):
    model["spec:proposal"] = lambda *a: ui_proposal()
    model["prototype"] = agent(NOTES_FINDINGS)
    model["prototype:rewrite:proposal"] = proposal(caps=("rate-limit", "retry-clock"))
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert sorted(p.stem for p in (cdir / "requirements").glob("*.md")) == ["rate-limit", "retry-clock"]
    assert list((cdir / "superseded-requirements").glob("retry-policy-*.md"))


def test_the_gate_retries_once_then_fails(repo, model, capsys):
    model["prototype"] = agent(notes=None)
    assert run(repo, "new", "throttle", "--auto", "--prototype") == 1
    assert model.calls_to("prototype") == 2
    assert "NOTES.md missing" in capsys.readouterr().out


def test_blocked_fails_without_a_retry(repo, model):
    model["prototype"] = agent("PROVES: x\nDISPROVES: none\nOPEN_QUESTIONS: none\nSTATUS: BLOCKED\n")
    assert run(repo, "new", "throttle", "--auto", "--prototype") == 1
    assert model.calls_to("prototype") == 1


def test_writes_outside_the_prototype_dir_fail_the_stage(repo, model, tmp_path, capsys):
    model["prototype"] = agent(outside=tmp_path / "elsewhere.txt")
    assert run(repo, "new", "throttle", "--auto", "--prototype") == 1
    assert "wrote outside" in capsys.readouterr().out


class TestPrototypeRequired:
    @pytest.mark.parametrize("extra,expected", [
        (" Add a settings page with a form and a button.", True),   # frontend
        (" Export a monthly PDF report.", True),                     # an output format
        (" Change the CLI output of `limits show`.", True),
        (" Tune the database queue worker.", False),                 # backend
    ])
    def test_ui_and_output_formats(self, tmp_path, extra, expected):
        cdir = tmp_path / "c"
        write(cdir / "proposal.md", proposal(extra=extra))
        assert P.prototype_required(cdir) is expected


class TestNotes:
    def test_the_slots_are_parsed_with_markdown_decoration(self):
        notes = P.parse_prototype_notes("**PROVES**: a\n## DISPROVES: b\nOPEN_QUESTIONS: c\nSTATUS: DONE")
        assert notes == {"proves": "a", "disproves": "b", "open_questions": "c", "status": "DONE"}

    def test_findings_are_empty_for_clean_notes(self):
        assert P.prototype_findings(P.parse_prototype_notes(NOTES_CLEAN)) == []

    def test_disproves_is_major_and_open_questions_minor(self):
        found = P.prototype_findings(P.parse_prototype_notes(NOTES_FINDINGS))
        assert [f["severity"] for f in found] == ["major", "minor"]

    def test_relative_paths_inside_the_dir_are_allowed(self, tmp_path):
        assert P.files_outside(["mockup.html", str(tmp_path / "x" / "a")], tmp_path / "x") == []
        assert P.files_outside(["../a"], tmp_path / "x") == [str((tmp_path / "a").resolve())]
