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
