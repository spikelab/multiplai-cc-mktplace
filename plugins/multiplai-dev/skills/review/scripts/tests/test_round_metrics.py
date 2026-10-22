"""`round-metrics`: rounds per target, the labels per round, and where a later round's findings were written.

The repository is built here with fixed dates (as `fixture_repo.py` does), so
its shas are stable: three commits of one file, each adding a line.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fixture_repo import _ENV
from review_pipeline import round_metrics
from review_pipeline.round_metrics import NO_LINE, PRESENT, UNKNOWN, WRITTEN

REMOTE = "https://github.com/example/app.git"
LINES = ["OLD_1 = 1", "OLD_2 = 2", "FIX_3 = 3", "FIX_4 = 4"]


def _git(repo: Path, *args: str, date: str = "2026-10-01T10:00:00Z") -> str:
    env = dict(os.environ, **_ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _commit(repo: Path, lines: list[str], message: str, date: str) -> str:
    (repo / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    """A repository whose round 1 head has lines 1-2; round 2's head adds line 3, round 3's line 4."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", REMOTE)
    h1 = _commit(repo, LINES[:2], "first", "2026-10-01T10:00:00Z")
    h2 = _commit(repo, LINES[:3], "fix round 1", "2026-10-01T11:00:00Z")
    h3 = _commit(repo, LINES[:4], "fix round 2", "2026-10-01T12:00:00Z")
    return repo, (h1, h2, h3)


def _cite(line: int, path: str = "app.py") -> dict:
    return {"path": path, "line_start": line, "line_end": line, "quote": "x"}


def _finding(fid: str, line: int, *, status: str = "confirmed", label: str = "", impact: str = "",
             citations: list[dict] | None = None) -> dict:
    f = {"id": fid, "file": "app.py", "line_start": line, "line_end": line, "claim": fid,
         "severity": "MEDIUM", "status": status,
         "citations": citations if citations is not None else [_cite(line)]}
    if label:
        f["assessment"] = {"label": label, "reason": "r"}
    if impact:
        f["impact"] = impact
    return f


def _round(head: str, at: str, findings: list[dict], repo: Path | str, mode: str = "full") -> dict:
    return {"schema_version": 1, "generated_at": at, "mode": mode, "run": {"started_at": at},
            "target": {"slug": "example-app--pr", "repo_path": str(repo), "remote_url": REMOTE,
                       "base_sha": "0" * 40, "head_sha": head},
            "findings": findings}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def reviews(tmp_path, history):
    """`<out>` with three targets.

    - `app/pr-1`: one round in critical mode, whose top-level `findings.json` is also kept in `rounds/`.
    - `app/pr-2`: three rounds; the kept rounds' directory names sort against `generated_at`, and a
      run that stopped before export is kept with no `findings.json`.
    - `gone/pr-3`: two rounds whose recorded repository no longer exists.
    """
    repo, (h1, h2, h3) = history
    out = tmp_path / "reviews"

    one = _round(h1, "2026-10-01T10:30:00Z", [_finding("c1", 1, impact="breaks-users"),
                                             _finding("c2", 2, impact="correctness-only")], repo, "critical")
    _write(out / "app" / "pr-1" / "findings.json", one)
    _write(out / "app" / "pr-1" / "rounds" / f"01-{h1[:12]}" / "findings.json", one)

    pr2 = out / "app" / "pr-2"
    _write(pr2 / "rounds" / f"99-{h1[:12]}" / "findings.json",
           _round(h1, "2026-10-01T10:30:00Z", [_finding("r1a", 1), _finding("r1b", 2)], repo))
    _write(pr2 / "rounds" / f"01-{h2[:12]}" / "findings.json", _round(h2, "2026-10-01T11:30:00Z", [
        _finding("old", 1),                                   # present at first review
        _finding("new", 3),                                   # written since
        _finding("still", 2, label="still-open"),             # present at first review
        _finding("rep", 3, label="repeat"),                   # not listed
        _finding("low", 3, label="low-value"),                # not listed
        _finding("refuted", 3, status="refuted"),             # not listed
        _finding("web", 1, citations=[{"path": "https://example.com/doc", "line_start": 1,
                                       "line_end": 1, "quote": "x"}]),  # no repo citation
        _finding("dep", 3, citations=[_cite(9, ".review-deps/lib@abc/x.py"), _cite(3)]),  # written since
    ], repo))
    (pr2 / "rounds" / f"02-{h2[:12]}").mkdir(parents=True)
    (pr2 / "rounds" / f"02-{h2[:12]}" / "review-state.json").write_text("{}", encoding="utf-8")
    _write(pr2 / "findings.json", _round(h3, "2026-10-01T12:30:00Z", [_finding("late", 4)], repo))

    gone = out / "gone" / "pr-3"
    missing = tmp_path / "deleted-worktree"
    _write(gone / "rounds" / f"01-{h1[:12]}" / "findings.json",
           _round(h1, "2026-10-01T10:30:00Z", [_finding("g1", 1)], missing))
    _write(gone / "findings.json", _round(h2, "2026-10-01T11:30:00Z", [_finding("g2", 3)], missing))
    return out, repo


def _by_name(metrics):
    return {f"{t.target_dir.parent.name}/{t.target_dir.name}": t for t in metrics}


def test_rounds_are_read_once_and_sorted_by_generated_at(reviews):
    out, _ = reviews
    targets = _by_name(round_metrics.measure(out))
    assert sorted(targets) == ["app/pr-1", "app/pr-2", "gone/pr-3"]
    assert len(targets["app/pr-1"].rounds) == 1  # the top-level copy of a kept round counts once
    pr2 = targets["app/pr-2"]
    assert [r.generated_at for r in pr2.rounds] == [
        "2026-10-01T10:30:00Z", "2026-10-01T11:30:00Z", "2026-10-01T12:30:00Z"]
    assert pr2.stopped == 1  # the kept run with no findings.json is left out, and counted


def test_labels_per_round_use_the_rounds_own_mode(reviews):
    out, _ = reviews
    targets = _by_name(round_metrics.measure(out))
    (only,) = targets["app/pr-1"].rounds
    assert only.listed == 1  # critical mode lists only the breaks-users finding
    r2 = targets["app/pr-2"].rounds[1]
    assert (r2.listed, r2.repeat, r2.still_open, r2.low_value) == (5, 1, 1, 1)


def test_a_later_rounds_finding_is_traced_to_the_first_rounds_head(reviews):
    out, _ = reviews
    pr2 = _by_name(round_metrics.measure(out))["app/pr-2"]
    first, second, third = pr2.rounds
    assert sum(first.origins.values()) == 0  # round 1 is not traced
    assert second.origins == {PRESENT: 2, WRITTEN: 2, NO_LINE: 1}
    assert third.origins == {WRITTEN: 1}


def test_a_missing_repository_gives_unknown(reviews):
    out, _ = reviews
    pr3 = _by_name(round_metrics.measure(out))["gone/pr-3"]
    assert pr3.rounds[1].origins == {UNKNOWN: 1}


def test_a_checkout_of_the_same_remote_stands_in_for_a_missing_repository(reviews, tmp_path):
    out, repo = reviews
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "remote", "add", "origin", "https://github.com/example/not-app.git")
    pr3 = _by_name(round_metrics.measure(out, repos=[other, repo]))["gone/pr-3"]
    assert pr3.rounds[1].origins == {WRITTEN: 1}
    assert pr3.rounds[1].repo == repo.resolve() and pr3.rounds[1].repo_stand_in
    # A recorded repository that exists is used as it is.
    pr2 = _by_name(round_metrics.measure(out, repos=[other]))["app/pr-2"]
    assert pr2.rounds[1].repo == repo and not pr2.rounds[1].repo_stand_in


def test_line_origin_reads_blame_then_ancestry(history):
    repo, (h1, h2, h3) = history
    assert round_metrics.line_origin(repo, "app.py", 1, h3, h1) == PRESENT
    assert round_metrics.line_origin(repo, "app.py", 3, h3, h1) == WRITTEN
    assert round_metrics.line_origin(repo, "app.py", 99, h3, h1) == UNKNOWN  # past the file's end
    assert round_metrics.line_origin(repo, "nope.py", 1, h3, h1) == UNKNOWN
    assert round_metrics.line_origin(repo, "app.py", 1, h3, "f" * 40) == UNKNOWN  # an unknown first head
    assert round_metrics.line_origin(repo.parent / "absent", "app.py", 1, h3, h1) == UNKNOWN
    assert round_metrics.line_origin(None, "app.py", 1, h3, h1) == UNKNOWN


def test_render_gives_a_table_per_target_and_the_totals_with_their_counts(reviews):
    out, _ = reviews
    text = round_metrics.render(round_metrics.measure(out), out)
    assert f"Scanned: `{out}`" in text
    assert "## `app/pr-2`" in text and "## `gone/pr-3`" in text
    assert "3 rounds (1 kept run that stopped before export left out)" in text
    assert "| 2 | " in text
    assert "**Totals** (3 targets, 6 rounds)" in text
    assert "mean rounds per target with 2 or more rounds: 2.50 (over 2 targets)" in text
    # Round 2+ listed findings: pr-2 round 2 (5) and round 3 (1), pr-3 round 2 (1).
    assert "present at first review: 2 of 5 (40%) with a known origin" in text
    assert "(3 written since, 1 unknown, 1 with no repo citation)" in text
    assert "still-open: 1 of 7 (14%) round-2+ listed findings" in text


def test_render_with_nothing_found(tmp_path):
    text = round_metrics.render([], tmp_path)
    assert "No review rounds found." in text


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(root.rglob("*"))}


def test_cli_prints_the_report_and_writes_nothing(reviews, tmp_path, capsys):
    from review_pipeline.__main__ import main

    out, repo = reviews
    before = _tree_state(tmp_path)
    assert main(["round-metrics", str(out), "--repo", str(repo)]) == 0
    assert _tree_state(tmp_path) == before
    text = capsys.readouterr().out
    assert text.startswith("# Review round metrics")
    assert "**Totals** (3 targets, 6 rounds)" in text
    assert "present at first review: 2 of 6 (33%) with a known origin" in text  # pr-3 traced via --repo


def test_cli_defaults_to_the_reviews_root(reviews, monkeypatch, capsys):
    from review_pipeline import ledger
    from review_pipeline.__main__ import main

    out, _ = reviews
    monkeypatch.setattr(ledger, "reviews_root", lambda: out)
    assert main(["round-metrics"]) == 0
    assert f"Scanned: `{out.resolve()}`" in capsys.readouterr().out


def test_cli_refuses_a_missing_directory(tmp_path, capsys):
    from review_pipeline.__main__ import main

    assert main(["round-metrics", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_help_exits_0():
    from review_pipeline.__main__ import main

    with pytest.raises(SystemExit) as e:
        main(["round-metrics", "--help"])
    assert e.value.code == 0
