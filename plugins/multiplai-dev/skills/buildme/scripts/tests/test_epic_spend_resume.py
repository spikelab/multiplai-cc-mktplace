"""The epic's spend cap counts every build and review of the change
(plan-factory-merged-2026-10-29.md, items 23 and 28; rows S20 and S28).

`fixtures/epic-spend-2026-10-29/` holds the DFACCMGMT-1 epic as it stood on
2026-10-29, trimmed to what `spend.py` reads: the epic's records (build, fix
and merge results, and the marker lines of their logs, with the worktrees'
root as `{WORKTREES}`), `epic.json`, the three PRs' lines of the review
ledger (`{REVIEWS}` for the reviews' directory) and the `budget` of each
child's `.build-state.complete.json`. That day the epic counted $399.36: its
build records ($155.06) and the reviews ($244.30). The checkpoints held
$307.35 of builds, most of them run by hand, so every build and review came
to $551.65 (factory-cost-analysis-2026-10-29.md)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, records, spend, store

from .epic_row_helpers import use_workspace

FIXTURE = Path(__file__).parent / "fixtures" / "epic-spend-2026-10-29"
CHILDREN = ("account-model", "owner-sections-source", "dolcecleaning-feed-guard")
SLUG = "DolceTech-Dolcebot--pr-37"


def _line(event: str, at: str, **over) -> str:
    entry = {
        "event": event,
        "generated_at": at,
        "head_sha": "68daab621220",
        "slug": SLUG,
        "target_dir": "/reviews/pr-37",
        **over,
    }
    return json.dumps(entry)


# --- S20: a review stopped by the usage limit, then resumed -------------------------


def test_s20_a_stopped_review_and_its_resume_count_the_stopped_part_once(tmp_path):
    """The review skill (PR #465) keeps the stopped run's `run.started_at` on
    the resume's `finished` line, and that line's `cost_usd` is the stopped
    part plus the resume's calls. `spend.review_spend` counts that last line
    only: the same total as a run that never stopped."""
    stopped = tmp_path / "stopped.jsonl"
    stopped.write_text(
        "\n".join(
            [
                _line("started", "2026-10-29T04:03:19Z"),
                _line(
                    "finished",
                    "2026-10-29T04:16:10Z",
                    round="01",
                    stopped="usage-limit",
                    run={"started_at": "2026-10-29T04:03:20.494Z", "cost_usd": 19.0},
                ),
                _line("started", "2026-10-29T04:48:46Z"),
                _line(
                    "finished",
                    "2026-10-29T05:16:49Z",
                    round="01",
                    stopped=None,
                    run={"started_at": "2026-10-29T04:03:20.494Z", "cost_usd": 24.61},
                ),
            ]
        )
        + "\n"
    )
    never = tmp_path / "never.jsonl"
    never.write_text(
        "\n".join(
            [
                _line("started", "2026-10-29T04:03:19Z"),
                _line(
                    "finished",
                    "2026-10-29T05:16:49Z",
                    round="01",
                    stopped=None,
                    run={"started_at": "2026-10-29T04:03:20.494Z", "cost_usd": 24.61},
                ),
            ]
        )
        + "\n"
    )
    assert spend.review_spend(stopped, {SLUG}) == (pytest.approx(24.61), True)
    assert spend.review_spend(stopped, {SLUG}) == spend.review_spend(never, {SLUG})


# --- the 10-29 state ------------------------------------------------------------------


def _repo(root: Path) -> Path:
    project = root / "p"
    project.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "remote", "add", "origin", "https://github.com/DolceTech/Dolcebot.git"],
    ):
        subprocess.run(argv, cwd=project, check=True, capture_output=True)
    return project


def _state_path(worktrees: Path, cid: str) -> Path:
    return (
        worktrees
        / f"buildme-account-management-{cid}"
        / "specs"
        / "changes"
        / "account-management"
        / "components"
        / cid
        / ".build-state.complete.json"
    )


def _epic_1029(root: Path, monkeypatch, *, states: bool = True):
    """The 10-29 state under *root*: the epic dir, the ledger and (with
    *states*) each child's checkpoint in its worktree. A coordinator over it,
    with each child's PR as the run that counted $399 had read them."""
    data = use_workspace(monkeypatch, root)
    worktrees = root / "worktrees"
    epic_dir = data / "buildme" / "epics" / "DolceTech-Dolcebot--account-management"
    shutil.copytree(FIXTURE / "epic", epic_dir)
    for log in (epic_dir / "agents").glob("*"):
        log.write_text(log.read_text().replace("{WORKTREES}", str(worktrees)))
    (data / "reviews").mkdir(parents=True)
    (data / "reviews" / "ledger.jsonl").write_text(
        (FIXTURE / "ledger.jsonl")
        .read_text()
        .replace("{REVIEWS}", str(data / "reviews"))
    )
    for cid in CHILDREN:
        path = _state_path(worktrees, cid)
        path.parent.mkdir(parents=True)
        if states:
            shutil.copy(FIXTURE / "states" / f"{cid}.build-state.complete.json", path)
    return _coord(root, epic_dir), epic_dir, worktrees


def _coord(root: Path, epic_dir: Path) -> coordinator.Coordinator:
    project = root / "p" if (root / "p").exists() else _repo(root)
    config = BuildConfig(project_dir=project, change_name="account-management")
    config.specs_dir = project / "specs"
    config.epic = EpicSettings(merge="auto", max_usd=500.0)
    epic = store.load_epic(epic_dir)
    coord = coordinator.Coordinator(
        config, None, epic_dir, epic, "DFACCMGMT-1-account-management", nested=True
    )
    for cid, status in epic.child_status.items():
        coord.children[cid].pr = status.pr
    return coord


def test_the_epic_counts_every_build_and_review_of_the_10_29_state(
    tmp_path, monkeypatch
):
    """Done means 7: $551.65 within $1, where 0.135.0's epic counted $399."""
    coord, epic_dir, _ = _epic_1029(tmp_path, monkeypatch)
    usd, complete = coord.spent()
    assert usd == pytest.approx(551.65, abs=1.0)
    assert complete
    tries = records.attempts(epic_dir)
    assert spend.attempts_spend(tries)[0] == pytest.approx(155.06, abs=0.01)
    assert sum(spend.build_spend(tries).values()) == pytest.approx(155.06, abs=0.01)


def test_without_the_checkpoints_the_count_is_the_records_and_reviews(
    tmp_path, monkeypatch
):
    """The part the epic counted before item 28: $399.36."""
    coord, _, _ = _epic_1029(tmp_path, monkeypatch, states=False)
    assert coord.spent() == (pytest.approx(399.36, abs=0.01), True)


# --- S28: a hand-run build's checkpoint -----------------------------------------------


def test_s28_a_hand_run_builds_checkpoint_counts_once_by_child_and_path(
    tmp_path, monkeypatch
):
    """A checkpoint no epic record counts adds what it holds beyond the
    child's build records, once. A later run (a new coordinator over the same
    files) counts the same, and naming the same file twice adds nothing."""
    coord, epic_dir, worktrees = _epic_1029(tmp_path, monkeypatch)
    first = coord.spent()
    again = _coord(tmp_path, epic_dir).spent()
    assert again == first
    files = coord._state_files()
    doubled = {cid: [*paths, paths[-1]] for cid, paths in files.items()}
    counted = spend.build_spend(records.attempts(epic_dir))
    assert spend.state_spend(doubled, counted) == spend.state_spend(files, counted)
    # A child whose checkpoint holds less than its records adds nothing.
    low = _state_path(worktrees, "account-model")
    low.write_text(json.dumps({"budget": {"cost_usd": 1.0}}))
    assert coord.spent()[0] == pytest.approx(
        first[0] - (107.56263 - 46.827232), abs=0.01
    )


def test_an_unreadable_checkpoint_or_unknown_calls_make_the_spend_incomplete(
    tmp_path, monkeypatch
):
    coord, _, worktrees = _epic_1029(tmp_path, monkeypatch)
    path = _state_path(worktrees, "owner-sections-source")
    path.write_text("{not json")
    assert coord.spent()[1] is False
    path.write_text(
        json.dumps({"budget": {"cost_usd": 2.0, "unaccounted_calls": ["x"]}})
    )
    assert coord.spent()[1] is False
    path.write_text(
        json.dumps(
            {
                "budget": {
                    "cost_usd": 2.0,
                    "unaccounted_calls": ["x"],
                    "accepted_unknown_calls": 1,
                }
            }
        )
    )
    assert coord.spent()[1] is True
