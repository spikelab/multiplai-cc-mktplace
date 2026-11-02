"""The replay eval's counts (`eval_score`), with no model call: they run in the
ordinary suite, unlike `test_replay.py`."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eval_score as S


def lab(id_, verdict, source=S.TRIAGE, **kw):
    return {
        "id": id_,
        "commit": "c1",
        "component": "x",
        "verdict": verdict,
        "source": source,
        "description": id_,
        **kw,
    }


LABELS = [
    lab("T1", "real", n=1),
    lab("T2", "false", n=2),
    lab("T3", "real", n=3),
    lab("P1", "real", source="matched-2026-11-01", problem="P1", n=4),
]


def test_the_labels_file_has_the_triage_counts():
    labels = S.load_labels(HERE / "labels.yaml")
    tri = [x for x in labels if x["source"] == S.TRIAGE]
    assert sum(x["verdict"] == "real" for x in tri) == 39
    assert sum(x["verdict"] == "false" for x in tri) == 18
    assert sum(x["source"] == "matched-2026-11-01" for x in labels) == 8
    assert len({x["id"] for x in labels}) == len(labels)


def test_assign_maps_l_numbers_and_ignores_bad_ones():
    answer = S.LabelMap(
        matches=[
            S.LabelMatch(finding=1, label="L2"),
            S.LabelMatch(finding=2, label="L9"),
            S.LabelMatch(finding=3, label=None),
        ]
    )
    assert S.assign(3, answer, LABELS, "r") == ["T2", None, None]


def test_a_p_match_takes_the_problems_label_when_the_judge_named_none():
    assert S.assign(2, None, LABELS, "r", {2: "P1"}) == [None, "P1"]
    assert S.assign(1, None, LABELS[:3], "r", {1: "P7"}) == ["problem:P7"]


def test_a_label_adjudicated_from_this_run_names_its_finding():
    labels = [*LABELS, lab("A1", "false", source="adjudicated-r", run="r", n=2)]
    answer = S.LabelMap(matches=[S.LabelMatch(finding=2, label="L1")])
    assert S.assign(2, answer, labels, "r") == [None, "A1"]
    assert S.assign(2, answer, labels, "other") == [None, "T1"]


def test_summarize_counts_each_label_once_and_unlabelled_apart():
    s = S.summarize(LABELS, {("c1", "x"): ["T1", "T1", "T2", None, "P1"]})
    assert (s["real_kept"], s["real_labelled"]) == (2, 3)
    assert (s["triage_real_kept"], s["triage_real"]) == (1, 2)
    assert (s["false_raised"], s["false_labelled"]) == (1, 1)
    assert s["unlabelled"] == 1
    assert s["findings"] == 5


def test_summarize_leaves_out_labels_of_checks_that_did_not_run():
    s = S.summarize(LABELS, {("c2", "x"): [None]})
    assert s["real_labelled"] == 0 and s["unlabelled"] == 1
