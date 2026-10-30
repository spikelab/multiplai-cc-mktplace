"""The review markdown ordered for a reader (plan-factory-merged-2026-10-29, items 38-41).

`pre_existing` is a field, the review opens with one list of findings by impact, the LOW findings
no verifier checked go to `review-<slug>-low.md`, and a citation inside another one of the same
file is not printed. Stop rows S34 (export stopped between the two files) and S35 (a state saved
with `dimension: "pre-existing"`) are tested against a run that never stopped.
"""

from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import CLAIM_HIGH, SCHEMA, cite, high_finding, medium_finding
from review_pipeline import gates, render
from review_pipeline.export import to_findings_file
from review_pipeline.models import ConvertItem, ConvertOutput, Finding, Verdict
from review_pipeline.render import (
    finding_section,
    render_low_review,
    render_review,
    write_review,
)
from review_pipeline.stages.find import map_converted
from review_pipeline.stages.verify import unverified_verdict
from review_pipeline.state import load_state, save_state

UNCHANGED = "docs/notes.py"  # not a changed file of the fixture change


def _low(claim: str = "A LOW one nobody checked") -> Finding:
    return high_finding().with_location(severity="LOW", claim=claim)


def _with_low(state):
    low = _low()
    return state.model_copy(
        update={
            "findings": [*state.findings, low],
            "verdicts": {**state.verdicts, low.id: unverified_verdict(low)},
        }
    ), low


# --- item 38: pre-existing is a field ---------------------------------------------


def test_convert_sets_pre_existing_and_keeps_the_finders_kind_as_dimension():
    out = ConvertOutput(
        items=[
            ConvertItem(
                severity="MEDIUM",
                file="a.py",
                line_start=2,
                claim="the old code drops the id",
                citations=[cite("a.py", 2, "x = 1")],
                pre_existing=True,
            ),
            ConvertItem(
                severity="LOW",
                file="a.py",
                line_start=3,
                claim="the new code is slow",
                citations=[cite("a.py", 3, "y = 2")],
            ),
        ]
    )
    _, (old, new) = map_converted("callers@2", out)
    assert (old.dimension, old.pre_existing) == ("callers", True)
    assert (new.dimension, new.pre_existing) == ("callers", False)


def test_finding_gate_lets_a_pre_existing_finding_name_an_unchanged_file(target_info):
    f = Finding(
        claim="the helper was always wrong",
        severity="LOW",
        file=UNCHANGED,
        line_start=1,
        line_end=1,
        failure_scenario="x",
        citations=[cite("rateplan_service.py", 1, "anything")],
    )
    assert gates.finding_gate(target_info, f).reason.endswith("is not a changed file")
    gated = gates.finding_gate(target_info, f.model_copy(update={"pre_existing": True}))
    assert "is not a changed file" not in (gated.reason or "")


def test_findings_json_and_the_markdown_show_pre_existing(canned_state):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"pre_existing": True})
    data = to_findings_file(canned_state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    rows = {f["id"]: f for f in data["findings"]}
    assert rows[high.id]["pre_existing"] is True
    assert all("pre_existing" not in f for i, f in rows.items() if i != high.id)
    text = render_review(canned_state, findings_file=data)
    assert "**Pre-existing:** the defect was in the code before this change." in text
    top = text.split("## Findings", 1)[1].split("\n## ", 1)[0]
    assert f"HIGH, impact not rated, pre-existing — [{CLAIM_HIGH}]" in top
    assert ", introduced — " in top  # the other listed finding


def test_s35_a_state_saved_with_dimension_pre_existing_loads_with_pre_existing_true(
    canned_state, tmp_path
):
    """S35: a state saved before R3b marks a pre-existing finding by its dimension."""
    new = canned_state.model_copy(deep=True)
    new.findings[0] = new.findings[0].model_copy(update={"pre_existing": True})
    never_stopped = tmp_path / "new"
    never_stopped.mkdir()
    save_state(new, never_stopped)

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    save_state(canned_state, old_dir)
    raw = json.loads((old_dir / "review-state.json").read_text())
    raw["findings"][0]["dimension"] = "pre-existing"
    del raw["findings"][0]["pre_existing"]
    (old_dir / "review-state.json").write_text(json.dumps(raw))

    loaded = load_state(old_dir / "review-state.json")
    assert loaded.findings[0].pre_existing is True
    assert loaded.findings[0].dimension == "diff-bugs"  # the finder's kind
    assert loaded.findings[0].id == canned_state.findings[0].id
    assert loaded == load_state(never_stopped / "review-state.json")


# --- item 39: the review opens with one list ----------------------------------------


def _rated(state, ratings: dict[str, tuple[str, str]]):
    """*state* with the verdict of each finding id set to (status, impact)."""
    state = state.model_copy(deep=True)
    for fid, (status, impact) in ratings.items():
        state.verdicts[fid] = Verdict(
            finding_id=fid, status=status, reason="r", topic="code", impact=impact
        )
    return state


def test_the_review_opens_with_every_listed_finding_ordered_by_impact_then_severity(
    canned_state,
):
    hygiene_high = high_finding()
    breaking_medium = medium_finding()
    breaking_high = high_finding().with_location(
        claim="Bookings are charged twice", line_start=6, line_end=6
    )
    state = canned_state.model_copy(
        update={"findings": [hygiene_high, breaking_medium, breaking_high]}
    )
    state = _rated(
        state,
        {
            hygiene_high.id: ("confirmed", "hygiene"),
            breaking_medium.id: ("confirmed", "breaks-users"),
            breaking_high.id: ("confirmed", "breaks-business"),
        },
    )
    state.original_severity = {}
    state, low = _with_low(state)
    data = to_findings_file(state)
    data["findings"][0]["earlier_decision"] = {
        "id": "B03-R1-F1",
        "decision": "rejected",
        "reason": "out of scope",
    }
    text = render_review(state, findings_file=data)
    headings = [s for s in text.splitlines() if s.startswith("## ")]
    assert headings[0] == "## Findings"
    assert text.index("## Findings") < text.index("## Code")
    top = text.split("## Findings", 1)[1].split("\n## ", 1)[0]
    lines = [s for s in top.splitlines() if s.startswith("- ")]
    slug = state.target.slug
    assert [line.split(" — ", 1)[0] for line in lines] == [
        "- MEDIUM, breaks-users, introduced",
        "- HIGH, breaks-business, introduced",
        "- HIGH, hygiene, introduced",
        "- LOW, impact not rated, introduced, not verified",
    ]
    assert lines[1] == (
        f"- HIGH, breaks-business, introduced — [Bookings are charged twice](#finding-{breaking_high.id}) "
        "`rateplan_service.py:6`"
    )
    assert f"(review-{slug}-low.md#finding-{low.id})" in lines[3]
    first = next(f for f in data["findings"] if f.get("earlier_decision"))
    line = next(s for s in lines if f"#finding-{first['id']})" in s)
    assert line.endswith("; earlier decision `B03-R1-F1`: rejected")
    # Each link lands on an anchor.
    assert f'<a id="finding-{breaking_high.id}"></a>' in text
    assert f'<a id="finding-{low.id}"></a>' in render_low_review(
        state, findings_file=data
    )


def test_the_commit_and_file_lists_are_at_the_end(canned_state):
    text = render_review(canned_state)
    t = canned_state.target
    header = text.split("## Findings", 1)[0]
    assert f"- **Commits:** {len(t.commits)}" in header
    assert f"- **Files changed:** {len(t.files)}" in header
    assert all(f"`{f}`" not in header for f in t.files)
    end = text.split("## Commits and files", 1)[1]
    assert text.index("## Appendix") < text.index("## Commits and files")
    assert all(f"- `{f}`" in end for f in t.files)
    assert all(subject in end for _, subject in t.commits)


def test_a_tree_review_says_nothing_of_introduced_and_has_no_commit_list(canned_state):
    canned_state.target.kind = "tree"
    text = render_review(canned_state)
    top = text.split("## Findings", 1)[1].split("\n## ", 1)[0]
    assert "introduced" not in top and "pre-existing" not in top
    assert "## Commits and files" not in text


# --- item 40: unverified LOWs in their own file -------------------------------------


def test_an_unverifiable_medium_stays_in_the_main_file_and_only_lows_move(
    canned_state,
):
    state, low = _with_low(canned_state)
    medium = medium_finding()
    state.findings[1] = medium
    state.verdicts[medium.id] = state.verdicts[medium.id].model_copy(
        update={"status": "unverifiable"}
    )
    state.original_severity = {}
    text = render_review(state)
    low_text = render_low_review(state)
    assert "### MEDIUM — rateplan_service.py:" in text
    assert "### MEDIUM" not in low_text
    assert f"### LOW — rateplan_service.py:1 — {low.claim}" in low_text
    assert low_text.startswith("# Not verified (LOW) — ")
    assert f"[review-{state.target.slug}.md](review-{state.target.slug}.md)" in low_text
    assert text.rstrip().split("\n## ")[-1].startswith("Not verified (LOW)")


def test_without_unverified_lows_there_is_no_low_file_and_a_stale_one_is_removed(
    canned_state, tmp_path
):
    stale = tmp_path / f"review-{canned_state.target.slug}-low.md"
    stale.write_text("an earlier run's\n")
    write_review(canned_state, tmp_path)
    assert render_low_review(canned_state) is None
    assert not stale.exists()
    assert (
        "## Not verified (LOW)"
        not in (tmp_path / f"review-{canned_state.target.slug}.md").read_text()
    )


def test_s34_export_stopped_after_the_main_file_writes_both_on_the_next_run(
    canned_state, tmp_path, monkeypatch
):
    """S34: stopped after `review-<slug>.md`, before `review-<slug>-low.md`."""
    state, _ = _with_low(canned_state)
    slug = state.target.slug
    never = tmp_path / "never"
    never.mkdir()
    write_review(state, never)

    stopped = tmp_path / "stopped"
    stopped.mkdir()
    real = render._write

    def stop_at_low(path, text):
        if path.name.endswith("-low.md"):
            raise KeyboardInterrupt
        real(path, text)

    monkeypatch.setattr(render, "_write", stop_at_low)
    state.stage = "export"  # what the orchestrator saved before render
    with pytest.raises(KeyboardInterrupt):
        write_review(state, stopped)
    assert (stopped / f"review-{slug}.md").exists()
    assert not (stopped / f"review-{slug}-low.md").exists()
    assert not state.past("render")  # so the next run renders again

    monkeypatch.setattr(render, "_write", real)
    write_review(state, stopped)
    for name in (f"review-{slug}.md", f"review-{slug}-low.md", f"summary-{slug}.md"):
        assert (stopped / name).read_text() == (never / name).read_text()


# --- item 41: fewer repeated citations ----------------------------------------------


def test_a_citation_inside_another_of_the_same_file_is_not_printed(canned_state):
    wide = cite("a.py", 2, "x").model_copy(update={"line_end": 9})
    inside = cite("a.py", 4, "y")
    same = cite("a.py", 2, "x").model_copy(update={"line_end": 9})
    other_file = cite("b.py", 4, "z")
    f = high_finding().model_copy(
        update={"citations": [inside, wide, same, other_file]}
    )
    state = canned_state.model_copy(update={"findings": [f]})
    state.verdicts = {
        f.id: Verdict(finding_id=f.id, status="confirmed", reason="r", impact="hygiene")
    }
    row = to_findings_file(state)["findings"][0]
    assert len(row["citations"]) == 4  # findings.json keeps every citation
    text = finding_section(row, web_base=None, head_sha="abc")
    assert "`a.py:4`" not in text
    assert text.count("`a.py:2-9`") == 1
    assert "`b.py:4`" in text
