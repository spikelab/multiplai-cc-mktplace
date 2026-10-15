"""At most 3 reviews per epic child PR, and the decision after each review
the coordinator records: another round, a final fix, or a park
(plan-review-rounds-and-finders-2026-10-15.md, items 1-2). The build,
`gh`, the review skill and the fixer are fakes first on PATH
(`epic_row_helpers.Fakes`); git is real."""

from __future__ import annotations

import json

import pytest

from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import review


def medium(fid: str, impact: str | None = "correctness-only") -> dict:
    f = {"id": fid, "severity": "MEDIUM", "status": "confirmed", "claim": f"{fid} is wrong",
         "file": "app.py", "line_start": 1}
    return {**f, "impact": impact} if impact else f


def high(fid: str, impact: str = "breaks-users") -> dict:
    return {**medium(fid, impact), "severity": "HIGH"}


# --- config ----------------------------------------------------------------------

@pytest.mark.parametrize("yaml_text, want", [
    ("", "breaks"),
    ("epic:\n  round_worth: any\n", "any"),
    ("epic:\n  round_worth: always\n", "breaks"),
])
def test_round_worth_defaults_to_breaks_and_falls_back(tmp_path, yaml_text, want):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text("context: x\n" + yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert (config.epic.round_worth, config.epic.max_review_rounds) == (want, 3)
    assert EpicSettings().round_worth == "breaks"


# --- the decision ------------------------------------------------------------------

@pytest.mark.parametrize("found, pr_round, worth, want", [
    ([medium("m1"), medium("m2", "hygiene")], 1, "breaks", "final_fix"),
    ([medium("m1"), medium("m2", "breaks-users")], 1, "breaks", "round"),
    ([medium("m1", "breaks-business")], 2, "breaks", "round"),
    ([medium("m1", None)], 1, "breaks", "round"),          # not rated: not known to be harmless
    ([high("h1", "correctness-only")], 1, "breaks", "round"),
    ([medium("m1")], 1, "any", "round"),
    ([medium("m1", "breaks-users")], 3, "breaks", "final_fix"),     # at the cap only MEDIUMs: final fix
    ([medium("m1")], 3, "any", "final_fix"),
    ([high("h1"), medium("m1")], 3, "breaks", "park"),
    ([high("h1")], 4, "breaks", "park"),
], ids=["only-correctness-only", "one-breaks-users", "breaks-business", "unrated", "high",
        "any-earns-a-round", "cap-mediums", "cap-mediums-any", "cap-high", "past-cap-high"])
def test_decide(found, pr_round, worth, want):
    got, why = review.decide(found, pr_round, 3, worth)
    assert got == want and why


def test_blocking_findings_are_the_listed_code_highs_and_mediums(tmp_path):
    doc = {"mode": None, "findings": [medium("m1"), high("h1"), {**medium("low"), "severity": "LOW"},
                                      {**medium("t1"), "section": "tests"}, {**medium("r1"), "status": "refuted"}]}
    (tmp_path / "findings.json").write_text(json.dumps(doc))
    assert [f["id"] for f in review.blocking_findings(tmp_path)] == ["m1", "h1"]
    assert [b.split()[:2] for b in review.blockers(tmp_path)] == [["MEDIUM", "m1"], ["HIGH", "h1"]]
    assert review.blocking_findings(tmp_path / "missing") is None
