"""What the dimension and path finders are expected to cost, before or during a run. Arithmetic only.

Calibrated on one reference run (PR #295: a 227 KB diff, about $14 for one
finder on every piece and $24 for 21 path finders); the estimate prices each
piece once per dimension that runs on it, by the characters it holds
(lockfiles and binary files hold none), and the path figure by the number of
paths. It leaves out verification and the paths mapping, so the budget
WARNING multiplies it by OVERHEAD_FACTOR, a conservative guess, not a
measurement. Nothing here calls a model, maps a path or writes to the ledger,
and nothing it prints stops a run: only the ledger does.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ReviewState
from .stages.find import change_dimensions, covering, top_pieces
from .stages.paths import PATH_CAP

PIECE_REF_USD = 14.0
PIECE_REF_CHARS = 227_000
PATH_REF_USD = 24.0
PATH_REF_COUNT = 21
OVERHEAD_FACTOR = 1.35  # verification + mapping, not in the estimate
CONVERT_FRACTION = 0.15  # the share of a key's estimate that is its convert call

NOTICE = (
    "NOTICE: the default 150 USD budget can stop a large change review partway, because verification adds "
    "to the finders' cost; resume with a higher --max-cost-usd continues without losing paid results."
)


@dataclass(frozen=True)
class Estimate:
    pieces_usd: float
    paths_usd: float
    path_count: int
    total: float
    calls: int = 0  # report and convert calls priced, at most


def piece_usd(chars: int) -> float:
    return chars * PIECE_REF_USD / PIECE_REF_CHARS


def path_usd(n: int) -> float:
    return n * PATH_REF_USD / PATH_REF_COUNT


def _paths_run(state: ReviewState) -> bool:
    return not state.target.is_tree or state.paths_enabled


def _path_keys(state: ReviewState) -> list[str]:
    """The path finder keys: the mapped ids once the paths stage ran, else the cap's worth."""
    if not _paths_run(state):
        return []
    if state.paths_status == "not-run":
        return [f"path@p{i}" for i in range(1, PATH_CAP + 1)]
    return [f"path@{pid}" for pid in state.path_ids]


def _piece_keys(state: ReviewState) -> list[tuple[str, int]]:
    """(key, characters) of every dimension finder on a piece; none on a tree review."""
    if state.target.is_tree:
        return []
    return [
        (f"{d}@{part.id}", part.chars)
        for p in top_pieces(state)
        for d in change_dimensions(state)
        for part in covering(state, p, d)
    ]


def _make(pieces: float, paths: float, count: int, calls: int) -> Estimate:
    return Estimate(
        pieces_usd=pieces,
        paths_usd=paths,
        path_count=count,
        total=pieces + paths,
        calls=calls,
    )


def estimate(state: ReviewState) -> Estimate:
    """A whole run's dimension and path finders."""
    keys = _path_keys(state)
    pieces = _piece_keys(state)
    return _make(
        sum(piece_usd(chars) for _, chars in pieces),
        path_usd(len(keys)),
        len(keys),
        2 * (len(pieces) + len(keys)),
    )


def _left(state: ReviewState, key: str, full: float) -> float:
    report = state.finder_reports.get(key)
    if report is None:
        return full
    return 0.0 if report.converted else full * CONVERT_FRACTION


def remaining(state: ReviewState) -> Estimate:
    """What a resume still pays for: a key with no stored report at full price, one stored but not
    converted at CONVERT_FRACTION, a converted one nothing."""
    keys = _path_keys(state)
    pieces = _piece_keys(state)
    left = sum(_left(state, key, piece_usd(chars)) for key, chars in pieces)
    paths = sum(_left(state, key, path_usd(1)) for key in keys)
    return _make(left, paths, len(keys), 2 * (len(pieces) + len(keys)))


def _usd(amount: float) -> str:
    return f"${amount:.0f}"


def cost_lines(
    state: ReviewState, *, budget: float, budget_is_default: bool, resuming: bool
) -> list[str]:
    """The lines to print on stderr: the estimate, then the WARNING and the NOTICE when they apply.

    A tree review without `--paths` runs no path finder and gets
    none. On *resuming* only the WARNING is evaluated, from `remaining` and
    the budget left after the spend already recorded. *budget* 0 is no ceiling.
    """
    if not _paths_run(state):
        return []
    est = remaining(state) if resuming else estimate(state)
    out = []
    if not resuming:
        pieces = (
            ""
            if state.target.is_tree
            else f"pieces {_usd(est.pieces_usd)} ({len(change_dimensions(state))} dimensions each) + "
        )
        out.append(
            f"cost (approximate): {pieces}paths {_usd(est.paths_usd)} (up to {est.path_count} paths) = "
            f"{_usd(est.total)}; excludes verification and paths mapping"
        )
    left = budget - float((state.budget or {}).get("cost_usd", 0.0)) if budget else 0.0
    if budget and budget > 0 and est.total * OVERHEAD_FACTOR >= left:
        out.append(
            f"WARNING: estimated {_usd(est.total)} for the dimension and path finders, plus "
            f"verification and mapping, is expected to exceed the remaining budget of {_usd(max(left, 0))}; "
            f"the run will stop partway; continue with resume and a higher --max-cost-usd."
        )
    if budget_is_default and not resuming and not state.target.is_tree:
        out.append(NOTICE)
    return out


def kept_line(state: ReviewState) -> str:
    """The sentence a budget stop adds: how many finder reports the checkpoint keeps."""
    reports = state.finder_reports.values()
    return (
        f"kept {len(reports)} finder reports ({sum(r.converted for r in reports)} converted); resume with a "
        f"higher --max-cost-usd to continue without paying for them again."
    )
