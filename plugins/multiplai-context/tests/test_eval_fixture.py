"""Guards the committed synthetic eval fixture (fake persona, no real data).

The fixture exists so eval_router.py runs for CI / PR reviewers without any
user-supplied golden set. These tests keep it valid and keep its clean-intent
cases passing, so a scoring change that breaks basic routing is caught here.
The single-token NONE trap is intentionally NOT asserted to pass — it encodes
the known false-positive bug (see plan-router-scoring-quality) and is expected
to fail until that fix lands.
"""
import json
from pathlib import Path

_EVALS = Path(__file__).resolve().parent.parent / "evals"


def _load_catalog():
    data = json.loads((_EVALS / "synthetic-fixture-catalog.json").read_text())
    return data["entries"]


def _load_cases():
    lines = (_EVALS / "synthetic-cases.jsonl").read_text().splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def test_fixture_files_are_valid():
    cat = _load_catalog()
    cases = _load_cases()
    assert len(cat) >= 5
    assert {"cooking.md", "gardening.md", "taxes.md"} <= {e["source"] for e in cat}
    assert len(cases) >= 6
    for c in cases:
        assert {"id", "prompt", "expected_files", "expected_none"} <= set(c)


def test_clean_intent_cases_route_correctly():
    """Every non-NONE case retrieves its expected file(s), none of its unexpected."""
    from lib.memory_router import TokenOverlapRouter

    cat = _load_catalog()
    router = TokenOverlapRouter()  # default keep_ratio
    for c in _load_cases():
        if c["expected_none"]:
            continue
        picks = router.select_multi(
            c["prompt"], None, {"memory": cat, "skills": [], "resources": []}
        )["memory"]
        got = set(picks)
        assert set(c["expected_files"]) <= got, f"{c['id']}: missed {c['expected_files']}, got {sorted(got)}"
        assert not (set(c["unexpected_files"]) & got), f"{c['id']}: pulled unexpected {sorted(set(c['unexpected_files']) & got)}"


def test_evaluate_scores_anchored_picks_at_file_level():
    """A pick of ``file.md#Section`` satisfies an expected ``file.md``.

    Since P1 the router returns section-anchored picks; golden labels are
    file-level. Exact string comparison scored every anchored pick as a
    miss (recall 49 -> 3.7 on the same router, same cases).
    """
    from eval_router import Metrics, _evaluate

    case = {
        "id": "anchored",
        "expected_files": ["life.md"],
        "expected_none": False,
        "unexpected_files": ["multiplai.md#Some Section"],
    }
    m = Metrics()
    ok = _evaluate(m, case, ["life.md#Long-Term Plan", "life.md#Travel"], 2, 10)
    assert ok, m.failures
    assert m.recall_num == 1 and m.recall_den == 1
    # Two sections of one file are one file-level retrieval.
    assert m.precision_total == 1

    m2 = Metrics()
    ok2 = _evaluate(m2, case, ["multiplai.md#Other Section"], 1, 10)
    assert not ok2  # unexpected file hit at file level, expected missed
    assert m2.fp_hits == 1
