"""What the dimension and path finders are expected to cost: the plan line, the budget WARNING and the default
NOTICE.

The estimate is arithmetic on the stored pieces and path ids (`costs.py`): it
makes no model call, no `map_paths` call and no ledger entry, and nothing it
prints stops a review. Only the ledger stops one.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import code_facts
import pytest
from review_pipeline import budget, sdk
from review_pipeline.__main__ import main
from review_pipeline.models import FinderReport, Piece, ReviewState
from test_reported_finders import git, mapped, model, review, saved  # noqa: F401

SKILL_MD = Path(__file__).resolve().parents[2] / "SKILL.md"
# Groups: pieces, dimensions per piece, paths, path count, total.
COST_LINE = re.compile(
    r"cost \(approximate\): (?:pieces \$(\d+) \((\d+) dimensions each\) \+ )?paths \$(\d+) "
    r"\(up to (\d+) paths\) = \$(\d+); excludes verification and paths mapping"
)
WARNING = re.compile(
    r"WARNING: estimated \$(\d+) for the dimension and path finders, plus verification and mapping, is "
    r"expected to exceed the remaining budget of \$(\d+); the run will stop partway; continue with resume "
    r"and a higher --max-cost-usd\."
)
NOTICE = (
    "NOTICE: the default 150 USD budget can stop a large change review partway, because verification adds "
    "to the finders' cost; resume with a higher --max-cost-usd continues without losing paid results."
)
# What a change review runs on every piece by default: tests and trim are off unless review.yaml asks.
DIMENSIONS = ("diff-bugs", "callers", "history", "conventions")
D = len(DIMENSIONS)


def costs():
    return importlib.import_module("review_pipeline.costs")


# --- states ----------------------------------------------------------------------------------------------------


def state_of(
    target_info,
    chars: list[int],
    *,
    tree: bool = False,
    paths_enabled: bool = False,
    path_ids: list[str] | None = None,
    spent: float = 0.0,
) -> ReviewState:
    pieces = [
        Piece(id=i, group=f"g{i}", files=[f"g{i}/m.py"], omitted=[], binary=[], chars=c)
        for i, c in enumerate(chars, 1)
    ]
    info = target_info.model_copy(update={"kind": "tree"} if tree else {})
    state = ReviewState(
        target=info,
        pieces=None if tree else pieces,
        paths_enabled=paths_enabled,
        budget={"cost_usd": spent} if spent else {},
    )
    if path_ids is not None:
        state.paths_status, state.path_ids = ("ok" if path_ids else "empty"), path_ids
    return state


def stored(state: ReviewState, *keys: str, converted: bool = True) -> None:
    for key in keys:
        state.finder_reports[key] = FinderReport(
            key=key, text="NO FINDINGS", converted=converted
        )


def lines(
    state, *, budget_usd: float, default: bool = False, resuming: bool = False
) -> list[str]:
    return costs().cost_lines(
        state, budget=budget_usd, budget_is_default=default, resuming=resuming
    )


def warned(out: list[str]) -> tuple[int, int] | None:
    found = [WARNING.fullmatch(line) for line in out]
    found = [m for m in found if m]
    assert len(found) <= 1
    return (int(found[0].group(1)), int(found[0].group(2))) if found else None


# --- the arithmetic ---------------------------------------------------------------------------------------------


def test_the_reference_diff_costs_about_14_per_dimension_24_for_paths_and_80_together(
    target_info,
):
    est = costs().estimate(state_of(target_info, [100_000, 127_000]))
    assert est.pieces_usd == pytest.approx(14 * D, abs=1)
    assert est.paths_usd == pytest.approx(24, abs=1) and est.path_count == 21
    assert (
        est.total == pytest.approx(80, abs=2)
        and est.total == est.pieces_usd + est.paths_usd
    )


def test_half_the_diff_costs_about_half_for_pieces(target_info):
    est = costs().estimate(state_of(target_info, [113_000]))
    assert est.pieces_usd == pytest.approx(D * costs().piece_usd(227_000) / 2, abs=1)


def test_three_pieces_price_one_report_and_one_convert_per_dimension_and_path(
    target_info,
):
    est = costs().estimate(state_of(target_info, [1_000, 2_000, 3_000]))
    assert est.calls == 2 * (3 * D + 21) == 66
    state = state_of(target_info, [1_000, 2_000, 3_000])
    state.dimensions = ["diff-bugs", "callers"]
    assert costs().estimate(state).calls == 2 * (3 * 2 + 21)
    assert costs().estimate(state).pieces_usd == pytest.approx(
        2 * costs().piece_usd(6_000)
    )


def test_a_split_piece_is_priced_by_its_halves_for_the_dimension_it_was_split_for(
    target_info,
):
    state = state_of(target_info, [1_000])
    state.dimensions = ["diff-bugs", "callers"]
    whole = state.pieces[0]
    whole.split_into = [2, 3]
    state.pieces += [
        Piece(
            id=2,
            group="g1",
            files=["g1/m.py"],
            chars=400,
            split_from=1,
            split_for=["callers"],
        ),
        Piece(
            id=3,
            group="g1",
            files=["g1/m.py"],
            chars=600,
            split_from=1,
            split_for=["callers"],
        ),
    ]
    est = costs().estimate(state)
    assert est.calls == 2 * (1 + 2 + 21)  # diff-bugs@1, callers@2, callers@3, the paths
    assert est.pieces_usd == pytest.approx(2 * costs().piece_usd(1_000))


def test_the_constants_are_the_calibration_the_design_gives():
    c = costs()
    assert (c.PIECE_REF_USD, c.PIECE_REF_CHARS, c.PATH_REF_USD, c.PATH_REF_COUNT) == (
        14.0,
        227_000,
        24.0,
        21,
    )
    assert (c.OVERHEAD_FACTOR, c.CONVERT_FRACTION) == (1.35, 0.15)
    assert (
        c.piece_usd(227_000) == 14.0 and c.path_usd(21) == 24.0 and c.path_usd(0) == 0
    )


def test_after_mapping_the_path_estimate_uses_the_mapped_paths(target_info):
    est = costs().estimate(state_of(target_info, [0], path_ids=["p1", "p2", "p3"]))
    assert est.path_count == 3 and est.paths_usd == pytest.approx(costs().path_usd(3))


def test_a_tree_review_without_paths_has_no_estimate_and_with_paths_only_the_path_one(
    target_info,
):
    assert (
        lines(state_of(target_info, [], tree=True), budget_usd=50, default=True) == []
    )
    with_paths = lines(
        state_of(target_info, [], tree=True, paths_enabled=True),
        budget_usd=50,
        default=True,
    )
    (line,) = [m for m in map(COST_LINE.fullmatch, with_paths) if m]
    assert line.group(1) is None and line.groups()[2:] == ("24", "21", "24")
    assert (
        NOTICE not in with_paths
    )  # a tree review's estimate is paths only: no default notice


# --- the WARNING ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "path_ids", "budget_usd", "warns"),
    [
        ([227_000], None, 20, (80, 20)),  # 80 vs 20
        ([162_143], [], 100, None),  # 40 vs 100
        (
            [227_000],
            None,
            150,
            None,
        ),  # the reference under the default: 80 * 1.35 < 150
        (
            [227_000, 227_000],
            None,
            150,
            (136, 150),
        ),  # twice the reference under the default: 136 * 1.35 >= 150
        ([227_000], None, 0, None),  # no ceiling
    ],
)
def test_the_warning_compares_the_estimate_with_overhead_to_the_budget(
    target_info, chars, path_ids, budget_usd, warns
):
    out = lines(state_of(target_info, chars, path_ids=path_ids), budget_usd=budget_usd)
    assert warned(out) == warns
    assert any(COST_LINE.fullmatch(line) for line in out)


def test_on_resume_only_what_is_not_stored_counts_and_a_stored_unconverted_key_counts_its_convert(
    target_info,
):
    state = state_of(
        target_info, [227_000], path_ids=[f"p{i}" for i in range(1, 21)], spent=50.0
    )
    stored(
        state, *(f"{d}@1" for d in DIMENSIONS), *(f"path@p{i}" for i in range(1, 11))
    )
    left = costs().remaining(state)
    assert left.pieces_usd == 0 and left.paths_usd == pytest.approx(
        costs().path_usd(10)
    )
    assert left.total == pytest.approx(12, abs=1)
    stored(state, "path@p11", converted=False)
    assert costs().remaining(state).paths_usd == pytest.approx(
        costs().path_usd(9) + 0.15 * costs().path_usd(1)
    )


def test_a_resume_with_enough_budget_is_silent_and_never_repeats_the_notice(
    target_info,
):
    state = state_of(
        target_info, [227_000], path_ids=[f"p{i}" for i in range(1, 21)], spent=50.0
    )
    stored(
        state, *(f"{d}@1" for d in DIMENSIONS), *(f"path@p{i}" for i in range(1, 11))
    )
    out = lines(state, budget_usd=100, default=True, resuming=True)
    assert warned(out) is None and NOTICE not in out


def test_a_resume_with_too_little_budget_warns_with_what_is_left(target_info):
    state = state_of(
        target_info, [227_000], path_ids=[f"p{i}" for i in range(1, 22)], spent=50.0
    )
    stored(state, *(f"{d}@1" for d in DIMENSIONS))
    assert warned(lines(state, budget_usd=55, resuming=True)) == (24, 5)
    stored(
        state, "conventions@1", converted=False
    )  # its convert is left: a share of one finder's price
    assert costs().remaining(state).pieces_usd == pytest.approx(
        0.15 * costs().piece_usd(227_000)
    )


# --- the NOTICE -----------------------------------------------------------------------------------------------------


def test_the_default_budget_notice_prints_on_a_change_review_even_under_the_budget(
    target_info,
):
    small = state_of(target_info, [1_000], path_ids=[])
    assert NOTICE in lines(small, budget_usd=150, default=True)
    assert NOTICE not in lines(small, budget_usd=150, default=False)
    assert NOTICE == costs().NOTICE


# --- through the command line ------------------------------------------------------------------------------------


def big_repo(
    tmp_path: Path, code_chars: int, lock_chars: int = 0
) -> tuple[Path, str, str]:
    """base -> head: `p01/m.py` gains about *code_chars* characters of diff, `uv.lock` *lock_chars*."""
    repo = tmp_path / "big"
    (repo / "p01").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "p01" / "m.py").write_text("A = 0\n")
    (repo / "uv.lock").write_text("version = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    line = "value_%06d = %06d  # padding to make the line longer than most\n"
    (repo / "p01" / "m.py").write_text(
        "A = 0\n"
        + "".join(line % (i, i) for i in range(code_chars // (len(line % (0, 0)) + 1)))
    )
    if lock_chars:
        (repo / "uv.lock").write_text(
            "version = 1\n"
            + "".join(f"pkg-{i:06d} = 1\n" for i in range(lock_chars // 15))
        )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


@pytest.fixture
def no_spend(monkeypatch):
    """Every way to spend raises: plan-only must reach none of them."""

    def boom(*args, **kwargs):
        raise AssertionError("plan-only must not call this")

    for owner, name in (
        (sdk, "agent_call_text"),
        (sdk, "agent_call_structured"),
        (sdk, "run_agent"),
        (code_facts, "map_paths"),
    ):
        monkeypatch.setattr(owner, name, boom, raising=False)
    monkeypatch.setattr(
        importlib.import_module("review_pipeline.stages.paths"), "map_paths", boom
    )


def plan(repo: Path, base: str, head: str, out: Path, *extra: str) -> int:
    return main(
        [
            "--session-id",
            "sess-test",
            "--out",
            str(out),
            "review",
            "--repo",
            str(repo),
            "--range",
            f"{base}..{head}",
            "--plan-only",
            *extra,
        ]
    )


def test_plan_only_prints_the_reference_estimate_and_spends_nothing(
    tmp_path, capsys, no_spend
):
    repo, base, head = big_repo(tmp_path, 227_000)
    assert plan(repo, base, head, tmp_path / "out", "--max-cost-usd", "20") == 0
    err = capsys.readouterr().err.splitlines()
    (line,) = [m for m in map(COST_LINE.fullmatch, err) if m]
    assert (
        abs(int(line.group(1)) - 14 * D) <= D
        and line.group(2) == str(D)
        and line.group(3) == "24"
        and line.group(4) == "21"
    )
    assert abs(int(line.group(5)) - 80) <= D + 1
    assert warned(err) is not None and warned(err)[1] == 20
    assert NOTICE not in err
    assert budget.get_budget().calls == 0 and budget.get_budget().cost_usd == 0


def test_a_lockfile_does_not_count_towards_the_estimate(tmp_path, capsys, no_spend):
    repo, base, head = big_repo(tmp_path, 50_000, lock_chars=100_000)
    assert plan(repo, base, head, tmp_path / "out") == 0
    (line,) = [
        m for m in map(COST_LINE.fullmatch, capsys.readouterr().err.splitlines()) if m
    ]
    assert abs(int(line.group(1)) - D * costs().piece_usd(50_000)) <= 1


def test_plan_only_shows_the_notice_under_the_default_and_exits_the_same_with_or_without_a_warning(
    tmp_path, capsys, no_spend
):
    repo, base, head = big_repo(tmp_path, 2_000)
    assert plan(repo, base, head, tmp_path / "a") == 0
    err = capsys.readouterr().err.splitlines()
    assert NOTICE in err and warned(err) is None
    assert plan(repo, base, head, tmp_path / "b", "--max-cost-usd", "120") == 0
    err = capsys.readouterr().err.splitlines()
    assert NOTICE not in err and warned(err) is None
    assert plan(repo, base, head, tmp_path / "c", "--max-cost-usd", "1") == 0
    assert warned(capsys.readouterr().err.splitlines()) is not None


def test_plan_only_on_a_tree_review(tmp_path, capsys, no_spend):
    repo, _, _ = big_repo(tmp_path, 2_000)
    common = [
        "--session-id",
        "sess-test",
        "--out",
        str(tmp_path / "out"),
        "review",
        "--repo",
        str(repo),
        "--tree",
        "--plan-only",
    ]
    assert main(common) == 0
    assert not any(
        COST_LINE.fullmatch(line) for line in capsys.readouterr().err.splitlines()
    )
    assert main([*common, "--paths"]) == 0
    (line,) = [
        m for m in map(COST_LINE.fullmatch, capsys.readouterr().err.splitlines()) if m
    ]
    assert line.group(1) is None and line.group(3) == "24"


def test_a_5_usd_run_warns_starts_and_stops_only_at_the_ledger(
    tmp_path,
    capsys,
    model,  # noqa: F811
    mapped,  # noqa: F811
    monkeypatch,
):
    repo, base, head = big_repo(tmp_path, 227_000)
    fake = model()
    mapped(1)

    async def charged(prompt, **kwargs):
        budget.record(
            SimpleNamespace(cost_usd=3.0), label=kwargs.get("budget_label", "")
        )
        return await fake.text(prompt, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_text", charged)
    assert review(repo, base, head, tmp_path / "out", "--max-cost-usd", "5") == 4
    err = capsys.readouterr().err
    assert warned(err.splitlines()) is not None
    target_dir, state = saved(tmp_path / "out")
    assert (
        state.budget_stops == 1 and state.paths_status != "not-run"
    )  # it ran the paths stage, unasked
    kept = len(state.finder_reports)
    converted = sum(r.converted for r in state.finder_reports.values())
    assert (
        f"kept {kept} finder reports ({converted} converted); resume with a higher --max-cost-usd to "
        "continue without paying for them again."
    ) in err
    assert kept >= 1


def test_the_warning_prints_before_the_paths_stage(
    tmp_path,
    capsys,
    model,  # noqa: F811
    mapped,  # noqa: F811
    monkeypatch,
):
    repo, base, head = big_repo(tmp_path, 227_000)
    model()
    mapped(0)
    orchestrator = importlib.import_module("review_pipeline.orchestrator")
    real = dict(orchestrator.STAGE_FUNCTIONS)["paths"]
    seen: list[str] = []

    async def spy(state, ctx):
        seen.append(capsys.readouterr().err)  # what was printed before the stage began
        return await real(state, ctx)

    monkeypatch.setattr(
        orchestrator,
        "STAGE_FUNCTIONS",
        [(n, spy if n == "paths" else f) for n, f in orchestrator.STAGE_FUNCTIONS],
    )
    assert review(repo, base, head, tmp_path / "out", "--max-cost-usd", "20") == 0
    assert len(seen) == 1 and warned(seen[0].splitlines())[1] == 20


# --- SKILL.md ---------------------------------------------------------------------------------------------------------


def test_skill_md_states_the_reference_figures_the_constants_hold():
    text = SKILL_MD.read_text(encoding="utf-8")
    section = " ".join(
        text.split("\n## Cost\n", 1)[1].split("\n## ", 1)[0].split()
    )  # prose: lines rewrap
    piece = re.search(r"about \$(\d+) for each dimension finder", section)
    path = re.search(r"about \$(\d+) for the path finders", section)
    assert piece and path and "227 KB" in section
    assert int(piece.group(1)) == round(costs().PIECE_REF_USD) and int(
        path.group(1)
    ) == round(costs().PATH_REF_USD)
    assert "before verification" in section
    assert (
        re.search(r"150 USD default .*stop", section)
        and "resume" in section
        and "--max-cost-usd" in section
    )
    assert "never refuses to start" in section
