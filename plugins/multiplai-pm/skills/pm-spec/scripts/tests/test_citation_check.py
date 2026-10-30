"""Plan item 51: the citation check in code, and row S58 (the dependency digest a
block saves, so that an edit to `depends_on` or `components.md` alone reopens
the check)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    BRANCH,
    change_dir,
    component,
    git,
    rewrite_answer,
    run,
    specs,
    state,
    write,
)
from pm_spec_pipeline import consistency, layout
from pm_spec_pipeline.config import SpecConfig
from pm_spec_pipeline.run import Run
from pm_spec_pipeline.state import RunState
from test_resume_rows import FLOW, RESUME, clone, go

FIXTURE = Path(__file__).parent / "fixtures" / "dfaccmgmt_citations.json"

# Item 3's list in INBOX/db2291/design-questions-2026-10-29.md, section 4:
# (citing component, cited component, line of the citing specs.md).
DESIGN_QUESTIONS_PAIRS = [
    ("account-model", "dolcecleaning-feed-guard", 69),
    ("account-model", "owner-permissions-core", 275),
    ("account-model", "owner-permissions-frontend", 69),
    ("dolcecleaning-feed-guard", "account-model", 35),
    ("dolcecleaning-feed-guard", "account-model", 182),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 161),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 161),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 161),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 168),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 172),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 173),
    ("dolcecleaning-feed-guard", "owner-permissions-core", 174),
    ("dolcecleaning-users", "dolceops-account-create", 110),
    ("dolcecleaning-users", "dolceops-account-team", 156),
    ("dolceops-account-team", "dolceops-account-create", 79),
    ("owner-permissions-core", "account-model", 327),
    ("owner-permissions-finer-checks", "dashboard-rendimenti", 25),
    ("owner-permissions-frontend", "account-model", 256),
    ("owner-permissions-frontend", "dashboard-rendimenti", 129),
    ("owner-sections-bookings", "owner-sections-property-pages", 90),
    ("owner-sections-bookings", "owner-sections-property-pages", 105),
]


def _fixture_change(root: Path) -> Path:
    """The DFACCMGMT change rebuilt from the fixture: every component's
    `depends_on`, and every specs line that names a component id, at its line
    number (the other lines are blank)."""
    cdir = root / "specs" / "changes" / "account-management"
    for cid, data in json.loads(FIXTURE.read_text()).items():
        comp = cdir / "components" / cid
        write(comp / "plan.md", "# Plan\n")
        write(comp / ".change.yaml", json.dumps({"depends_on": data["depends_on"]}))
        lines = [""] * data["line_count"]
        for n, text in data["lines"].items():
            lines[int(n) - 1] = text
        write(comp / "specs.md", "\n".join(lines) + "\n")
    return cdir


def _run_for(cdir: Path) -> Run:
    config = SpecConfig(project_dir=cdir.parents[2], change_name=cdir.name)
    return Run(config=config, state=RunState(change=cdir.name))


def _pair(finding: dict) -> tuple[str, str, int]:
    citing, where = finding["places"][0]["file"], finding["places"][0]["where"]
    return (
        Path(citing).parent.name,
        consistency.citation_pair(finding)[1],
        int(where.removeprefix("line ")),
    )


def test_the_citation_check_flags_the_21_pairs_of_the_design_questions(tmp_path):
    cdir = _fixture_change(tmp_path)
    found = consistency.citation_findings(_run_for(cdir))
    assert sorted(_pair(f) for f in found) == sorted(DESIGN_QUESTIONS_PAIRS)
    first = found[0]
    assert first["category"] == "gap" and first["severity"] == "major"
    assert ".change.yaml" in first["suggestion"]
    assert "components.md" in first["suggestion"]


def test_a_transitive_dependency_either_way_is_not_flagged(tmp_path):
    cdir = tmp_path / "specs" / "changes" / "c"
    for cid, deps, text in (
        ("a", [], "Nothing cited.\n"),
        ("b", ["a"], "Uses `c` and `a`.\n"),
        ("c", ["b"], "Uses `a`, and `a-b` is not an id.\n"),
    ):
        write(cdir / "components" / cid / "plan.md", "# Plan\n")
        write(cdir / "components" / cid / ".change.yaml", f"depends_on: {deps}\n")
        write(cdir / "components" / cid / "specs.md", text)
    assert consistency.citation_findings(_run_for(cdir)) == []


def test_a_change_with_one_component_has_no_citation_findings(tmp_path):
    cdir = tmp_path / "specs" / "changes" / "c"
    write(cdir / "specs.md", specs())
    assert consistency.citation_findings(_run_for(cdir)) == []


# --- S58 --------------------------------------------------------------------------

UNORDERED = {
    "reason": "Each is a pull request on its own.",
    "components": [component("api"), component("ui")],
}


def _ui_cites_api(model) -> None:
    model["components"] = UNORDERED
    model["spec:specs"] = lambda prompt, label, kw: rewrite_answer(
        specs(rule="the limit `api` sets" if label.endswith(":ui") else "")
    )


def _add_dependency(repo: Path, at: Path) -> None:
    """A person adds `api` to ui's `depends_on` and to components.md, and pushes;
    no file the spec revision hashes changes."""
    mine = clone(repo, at)
    cdir = mine / "specs" / "changes" / "throttle"
    meta = cdir / "components" / "ui" / ".change.yaml"
    meta.write_text(meta.read_text().replace("depends_on: []", "depends_on:\n- api"))
    write(
        cdir / "components.md",
        (cdir / "components.md").read_text() + "\nui needs api.\n",
    )
    git(mine, "commit", "-qam", "order ui after api")
    git(mine, "push", "-q", "origin", BRANCH)


def test_S58_a_block_then_only_depends_on_edited_reopens_the_check(repo, model):
    _ui_cites_api(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "blocked"
    assert step["blocked_dependencies"]
    assert any("cites `api`" in f["text"] for f in step["blocking_findings"])
    revision = step["blocked_revision"]

    _add_dependency(repo, repo.parent / "reviewer")
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert len(model.calls) == calls + 1  # checked again, not refused
    assert layout.revision(change_dir(repo)) == revision  # no hashed file changed
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "passed" and step["blocked_dependencies"] is None


def test_S58_unchanged_files_and_dependencies_are_still_refused(repo, model, capsys):
    _ui_cites_api(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    assert len(model.calls) == calls
    assert "blocked at this revision" in capsys.readouterr().out


def test_S58_a_state_saved_before_0_14_0_with_no_digest_behaves_as_before(repo, model):
    _ui_cites_api(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    path = change_dir(repo) / ".pm-spec" / "state.json"
    saved = json.loads(path.read_text())
    del saved["steps"]["consistency"]["blocked_dependencies"]
    path.write_text(json.dumps(saved))

    # Unchanged files: refused with no model call, as in 0.13.3.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    assert len(model.calls) == calls
    # A depends_on edit alone is not seen by a state with no digest (0.13.3's
    # behaviour): still refused.
    _add_dependency(repo, repo.parent / "reviewer")
    assert run(repo, *RESUME) == 4
    assert len(model.calls) == calls


def test_with_auto_the_one_rewrite_removes_the_reliance_and_the_check_passes(
    repo, model
):
    _ui_cites_api(model)
    assert run(repo, "new", "throttle", "--auto") == 0
    rewrites = [label for label in model.labels() if ":rewrite:" in label]
    assert rewrites == ["consistency:rewrite:ui"]
    assert "`api`" not in (change_dir(repo) / "components/ui/specs.md").read_text()


def test_with_auto_a_rewrite_that_keeps_the_reliance_blocks(repo, model):
    _ui_cites_api(model)
    model["rewrite"] = lambda prompt, label, kw: rewrite_answer(
        specs(rule="the limit `api` sets")
    )
    assert run(repo, "new", "throttle", "--auto") == 4
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "blocked" and step["blocked_dependencies"]
    statuses = {f["status"] for f in step["blocking_findings"]}
    assert statuses == {"still_present"}
