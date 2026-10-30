"""The agents' snapshot is a git work tree that search sees, and `round-metrics --counts`.

The layout copies the one that hid every file from the agents' Grep: the
snapshot sits under a data directory whose `.gitignore` is `*`, inside an outer
git repository. ripgrep follows ignore files of parent directories up to the
repository's root, so before the fix a directory search there listed nothing.

Rows S2-S5 of the plan's stop-and-resume table are tested here, each against a
snapshot made by a run that never stopped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fixture_repo import _ENV
from review_pipeline import round_metrics, target
from review_pipeline.models import TargetInfo

NEEDLE = "SNAPSHOT_SEARCH_NEEDLE"
needs_rg = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep not installed"
)


def _git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        **_ENV,
        GIT_AUTHOR_DATE="2026-10-29T10:00:00Z",
        GIT_COMMITTER_DATE="2026-10-29T10:00:00Z",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


@pytest.fixture
def layout(tmp_path):
    """An outer repository with `data/.gitignore` = `*`, and a target repository with one tracked
    file, a tracked `build.log` its own `.gitignore` ignores, and a second commit as head."""
    outer = tmp_path / "outer"
    (outer / "data").mkdir(parents=True)
    _git(outer, "init", "-q", "-b", "main")
    (outer / "data" / ".gitignore").write_text("*\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "add", "app.py", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text(f"x = 1\n{NEEDLE} = 2\n", encoding="utf-8")
    (repo / "build.log").write_text(f"{NEEDLE}\n", encoding="utf-8")
    _git(repo, "add", "-f", "app.py", "build.log")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    info = TargetInfo(
        repo_path=str(repo),
        base_sha=base,
        head_sha=head,
        slug="x",
        files=["app.py", "build.log"],
    )
    return outer / "data" / "reviews", info


def _files(root: Path) -> list[str]:
    """Every path under *root* but those inside `.git`, which `git init` fills with its own files."""
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
    )


def _uninterrupted(reviews: Path, info: TargetInfo) -> Path:
    return target.snapshot_head(info, reviews / "fresh" / "tree")


def _same_as_uninterrupted(tree: Path, reviews: Path, info: TargetInfo) -> None:
    fresh = _uninterrupted(reviews, info)
    assert _files(tree) == _files(fresh)
    assert (tree / ".git").is_dir()
    assert (tree / ".review-head").read_text() == (fresh / ".review-head").read_text()
    for rel in _files(fresh):
        if (fresh / rel).is_file():
            assert (tree / rel).read_bytes() == (fresh / rel).read_bytes()


def _rg_files(tree: Path) -> str:
    return subprocess.run(
        ["rg", "--files", str(tree)], capture_output=True, text=True, check=False
    ).stdout


@needs_rg
def test_the_layout_hides_a_plain_extract_from_ripgrep(layout):
    # the bug, reproduced: without a repository of its own the tree lists no file
    reviews, _ = layout
    tree = reviews / "x" / "tree"
    tree.mkdir(parents=True)
    (tree / "app.py").write_text(f"{NEEDLE}\n", encoding="utf-8")
    assert _rg_files(tree) == ""


@needs_rg
def test_the_snapshot_is_a_git_work_tree_that_ripgrep_searches(layout):
    reviews, info = layout
    tree = target.snapshot_head(info, reviews / "x" / "tree")

    assert (tree / ".git").is_dir()
    assert (tree / ".review-head").read_text() == info.head_sha + "\n"
    found = subprocess.run(
        ["rg", "-l", NEEDLE, str(tree)], capture_output=True, text=True, check=False
    ).stdout.split()
    # the tracked file is found, and the snapshot's own .gitignore still hides build.log
    assert found == [str(tree / "app.py")]


def test_the_base_snapshot_is_a_git_work_tree(layout):
    reviews, info = layout
    base = target.snapshot_base(info, reviews / "x" / "base-tree", ["app.py"])
    assert (base / ".git").is_dir()
    assert (base / "app.py").read_text() == "x = 1\n"


def test_without_ripgrep_one_line_is_written_and_the_snapshot_is_made(
    layout, monkeypatch
):
    reviews, info = layout
    monkeypatch.setattr(target.shutil, "which", lambda name: None)
    lines: list[str] = []
    tree = target.snapshot_head(info, reviews / "x" / "tree", note=lines.append)
    assert len(lines) == 1 and "ripgrep (rg) not found" in lines[0]
    assert (tree / ".git").is_dir() and (tree / ".review-head").exists()


def test_s2_stopped_after_extract_before_git_init(layout):
    # S2: tree/ files, no .git, no marker -> deletes tree/, extracts again, inits, writes the marker
    reviews, info = layout
    tree = target.snapshot_head(info, reviews / "x" / "tree")
    shutil.rmtree(tree / ".git")
    (tree / ".review-head").unlink()
    (tree / "left-over.txt").write_text("from the stopped run\n", encoding="utf-8")

    target.snapshot_head(info, tree)

    assert not (tree / "left-over.txt").exists()  # extracted again
    _same_as_uninterrupted(tree, reviews, info)


def test_s3_stopped_after_git_init_before_the_marker(layout):
    # S3: tree/ with .git, no marker -> as S2
    reviews, info = layout
    tree = target.snapshot_head(info, reviews / "x" / "tree")
    (tree / ".review-head").unlink()
    (tree / "left-over.txt").write_text("from the stopped run\n", encoding="utf-8")

    target.snapshot_head(info, tree)

    assert not (tree / "left-over.txt").exists()
    _same_as_uninterrupted(tree, reviews, info)


def test_s4_a_marker_and_no_git_dir_gets_git_init_only(layout, monkeypatch):
    # S4: the marker and no .git (made before R1, or .git deleted) -> git init only; no new extract
    reviews, info = layout
    tree = target.snapshot_head(info, reviews / "x" / "tree")
    shutil.rmtree(tree / ".git")
    real_run = target.subprocess.run
    argvs: list[list[str]] = []

    def run(argv, *a, **kw):
        argvs.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(target.subprocess, "run", run)
    target.snapshot_head(info, tree)
    monkeypatch.undo()

    assert not any("archive" in argv for argv in argvs)
    assert any("init" in argv for argv in argvs)
    _same_as_uninterrupted(tree, reviews, info)


@pytest.mark.parametrize("fails", ["git init", "rg --files lists nothing"])
def test_s5_init_or_search_check_fails_then_the_next_review_starts_over(
    layout, monkeypatch, tmp_path, fails
):
    # S5: tree/, no marker; the run ends as a failed git archive does, and the next review starts over
    reviews, info = layout
    tree = reviews / "x" / "tree"
    if fails == "git init":
        real_run = target.subprocess.run

        def run(argv, *a, **kw):
            if "init" in argv:
                return subprocess.CompletedProcess(
                    argv, 128, b"", b"fatal: cannot init"
                )
            return real_run(argv, *a, **kw)

        monkeypatch.setattr(target.subprocess, "run", run)
        expected = "git init"
    else:
        fake = tmp_path / "bin" / "rg"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setattr(target.shutil, "which", lambda name: str(fake))
        expected = "rg --files lists nothing"

    with pytest.raises(target.TargetError, match=expected) as err:
        target.snapshot_head(info, tree)
    if fails != "git init":
        # it names the ignore file that hides the directory
        assert "ignored by `*` at data/.gitignore:1" in str(err.value)
    assert (tree / "app.py").exists() and not (tree / ".review-head").exists()

    monkeypatch.undo()
    target.snapshot_head(info, tree)
    _same_as_uninterrupted(tree, reviews, info)


# --- round-metrics --counts -------------------------------------------------

# Sentences from finder reports of the 2026-10-29 reviews, and ones that state no empty search.
EMPTY = [
    "Directory-wide Grep in this snapshot returns nothing even for strings that are there.",
    "an earlier repo-wide grep came back empty, so checking paths directly",
    "Grep isn't returning matches, so I'm checking whether the tool works.",
    "Directory search in Grep doesn't work in this snapshot.",
    "Grep keeps coming back empty, so I'm checking whether the tool works at all.",
    "Grep seems unable to search this snapshot, so I'm reading the files directly.",
    "A grep for `G32` across the snapshot returns no match.",
]
NOT_EMPTY = [
    "Searching the frontend for callers of the resolve endpoint.",
    "Grep finds `sys_models` only on line 17.",
    "I found no defect in piece 4 that would make a test fail today.",
]


@pytest.mark.parametrize("text", EMPTY)
def test_the_empty_search_pattern_matches_reports_that_say_search_returned_nothing(
    text,
):
    assert round_metrics.EMPTY_SEARCH_RE.search(text)


@pytest.mark.parametrize("text", NOT_EMPTY)
def test_the_empty_search_pattern_leaves_other_sentences(text):
    assert not round_metrics.EMPTY_SEARCH_RE.search(text)


def _finding(fid: str, status: str, severity: str, impact: str, topic: str) -> dict:
    return {
        "id": fid,
        "file": "app.py",
        "line_start": 1,
        "line_end": 1,
        "claim": fid,
        "severity": severity,
        "status": status,
        "impact": impact,
        "topic": topic,
        "citations": [],
    }


def _write_round(folder: Path, head: str, at: str, findings: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "generated_at": at,
        "mode": "full",
        "run": {"started_at": at},
        "target": {
            "slug": "s",
            "repo_path": "",
            "base_sha": "0" * 40,
            "head_sha": head,
        },
        "findings": findings,
    }
    (folder / "findings.json").write_text(json.dumps(data), encoding="utf-8")


def test_counts_print_confirmed_findings_and_empty_searches_per_round(tmp_path, capsys):
    from review_pipeline.__main__ import main

    pr = tmp_path / "reviews" / "app" / "pr-1"
    first = pr / "rounds" / ("01-" + "a" * 12)
    _write_round(
        first,
        "a" * 40,
        "2026-10-29T04:00:00Z",
        [
            _finding("f1", "confirmed", "MEDIUM", "correctness-only", "tests"),
            _finding("f2", "confirmed", "MEDIUM", "correctness-only", "code"),
            _finding("f3", "confirmed", "HIGH", "breaks-users", "security"),
            _finding("f4", "refuted", "MEDIUM", "hygiene", "docs"),
            _finding("f5", "unverified", "LOW", "", ""),
        ],
    )
    for n, text in enumerate([EMPTY[0], EMPTY[3], NOT_EMPTY[0]], start=1):
        (first / f"report-diff-bugs-{n}.md").write_text(text, encoding="utf-8")
    _write_round(
        pr,
        "b" * 40,
        "2026-10-29T05:00:00Z",
        [_finding("g1", "confirmed", "MEDIUM", "hygiene", "docs")],
    )
    (pr / "report-callers-1.md").write_text(NOT_EMPTY[1], encoding="utf-8")

    assert main(["round-metrics", str(tmp_path / "reviews"), "--counts"]) == 0
    text = capsys.readouterr().out
    assert (
        "| 1 | `aaaaaaaaaaaa` | 3 | 1 | 2 | 0 | correctness-only 2, breaks-users 1 "
        "| code 1, security 1, tests 1 | 3 | 2 of 3 (67%) |"
    ) in text
    assert (
        "| 2 | `bbbbbbbbbbbb` | 1 | 0 | 1 | 0 | hygiene 1 | docs 1 | 1 | 0 of 1 (0%) |"
        in text
    )
    assert (
        "**Counts** (2 rounds): finder reports that state a search returned nothing: "
        "2 of 4 (50%); confirmed findings summed over the rounds (a finding confirmed in two "
        "rounds counts twice): 4 (HIGH 1, MEDIUM 3, LOW 0)."
    ) in text


def test_without_counts_the_report_is_unchanged(tmp_path, capsys):
    from review_pipeline.__main__ import main

    pr = tmp_path / "reviews" / "app" / "pr-1"
    _write_round(pr, "a" * 40, "2026-10-29T04:00:00Z", [])
    (pr / "report-callers-1.md").write_text(EMPTY[0], encoding="utf-8")
    assert main(["round-metrics", str(tmp_path / "reviews")]) == 0
    text = capsys.readouterr().out
    assert "Counts per round" not in text and "**Counts**" not in text
