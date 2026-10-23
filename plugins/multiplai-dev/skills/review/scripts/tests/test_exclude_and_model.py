"""`review --exclude` leaves matching paths out of the change under review, and
`--model` (on `review` and `resume`) runs every stage on one model, over
review.yaml and multiplai.conf. buildme passes both to the review of its PRs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from review_pipeline import orchestrator, target
from review_pipeline.__main__ import main

SPEC = "specs/changes/word-freq/components/word-frequencies"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def change(tmp_path) -> tuple[Path, str, str]:
    """A change of one source file, one test, one doc and a pile of spec and
    buildme files, like a buildme PR."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# proj\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    files = {
        "src/words.py": "def count(text):\n    return len(text.split())\n",
        "tests/test_words.py": "from words import count\n\ndef test_count():\n    assert count('a b') == 2\n",
        "README.md": "# proj\n\nCounts words.\n",
        f"{SPEC}/design.md": "# Design\n",
        f"{SPEC}/facts.json": "{}\n",
        f"{SPEC}/.board.json": "{}\n",
        "specs/archive/2026-10-23-old/tasks.md": "- [ ] x\n",
        "specs/registry/words.md": "# Capability: words\n",
        "build-progress.md": "# Build Progress\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base, _git(repo, "rev-parse", "HEAD")


EXCLUDES = ["specs/changes/**", "specs/archive/**", "**/build-progress.md"]


def test_diff_and_files_leave_out_the_excluded_paths(change):
    repo, base, head = change
    r = target.resolve(repo, range_=f"{base}..{head}")
    diff = target.diff_text(r.repo, base, head, exclude=EXCLUDES)
    info = target.build_target(r, exclude=EXCLUDES)
    kept = [
        "README.md",
        "specs/registry/words.md",
        "src/words.py",
        "tests/test_words.py",
    ]
    assert sorted(info.files) == kept
    assert "specs/changes" not in diff and "build-progress" not in diff
    assert "src/words.py" in diff and "specs/registry/words.md" in diff
    assert info.excluded == EXCLUDES
    assert sorted(info.excluded_files) == sorted(
        [
            "build-progress.md",
            f"{SPEC}/.board.json",
            f"{SPEC}/design.md",
            f"{SPEC}/facts.json",
            "specs/archive/2026-10-23-old/tasks.md",
        ]
    )


def test_no_exclude_changes_nothing(change):
    repo, base, head = change
    r = target.resolve(repo, range_=f"{base}..{head}")
    assert target.diff_text(r.repo, base, head) == target.diff_text(
        r.repo, base, head, exclude=[]
    )
    info = target.build_target(r)
    assert len(info.files) == 9 and info.excluded == [] == info.excluded_files


def test_exclude_reads_paths_from_the_top_from_a_subdirectory(change):
    repo, base, head = change
    r = target.resolve(repo / "src", range_=f"{base}..{head}")
    info = target.build_target(r, exclude=EXCLUDES)
    assert "README.md" in info.files and info.excluded_files
    assert not [f for f in info.files if f.startswith("specs/changes")]


def test_the_cli_writes_only_the_kept_files(change, tmp_path, capsys):
    repo, base, head = change
    out = tmp_path / "out"
    argv = ["--out", str(out), "review", "--repo", str(repo)]
    argv += ["--range", f"{base}..{head}", "--plan-only"]
    for glob in EXCLUDES:
        argv += ["--exclude", glob]
    assert main(argv) == 0
    target_dir = next(out.glob("*/range-*"))
    files = (target_dir / "files.txt").read_text().split()
    assert sorted(files) == [
        "README.md",
        "specs/registry/words.md",
        "src/words.py",
        "tests/test_words.py",
    ]
    assert "specs/changes" not in (target_dir / "diff.patch").read_text()
    plan = (target_dir / "plan.txt").read_text()
    assert "facts.json" not in plan


def test_everything_excluded_fails_the_gate_with_the_reason(change, tmp_path, capsys):
    repo, base, head = change
    out = tmp_path / "out"
    argv = ["--out", str(out), "review", "--repo", str(repo)]
    argv += ["--range", f"{base}..{head}", "--plan-only", "--exclude", "**"]
    assert main(argv) == 2
    assert "--exclude" in capsys.readouterr().err


@pytest.mark.parametrize("what", [["--tree"], ["--dir", "/nonexistent"]])
def test_exclude_goes_with_a_change_review_only(tmp_path, capsys, what):
    argv = ["--out", str(tmp_path / "out"), "review"]
    argv += [] if what[0] == "--dir" else ["--repo", str(tmp_path)]
    argv += [*what, "--exclude", "specs/**"]
    assert main(argv) == 2
    assert "--exclude goes with" in capsys.readouterr().err


def _capture(monkeypatch, name):
    seen = {}

    async def fake(*args, **kwargs):
        seen["config"] = args[-1] if name == "review" else args[1]
        raise orchestrator.ReviewError("stop here")

    monkeypatch.setattr(orchestrator, name, fake)
    return seen


def test_review_model_runs_every_stage_on_it(change, tmp_path, monkeypatch):
    repo, base, head = change
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text(
        "finder_model: claude-opus-4-6\nverifier_model: claude-opus-4-6\n"
    )
    seen = _capture(monkeypatch, "review")
    argv = ["--out", str(out), "review", "--repo", str(repo)]
    argv += ["--range", f"{base}..{head}", "--trust-repo", "--model", "sonnet"]
    main(argv)
    cfg = seen["config"]
    assert (cfg.finder_model, cfg.verifier_model, cfg.merger_model) == (
        "sonnet",
        "sonnet",
        "sonnet",
    )


def test_without_model_review_yaml_still_decides(change, tmp_path, monkeypatch):
    repo, base, head = change
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("finder_model: claude-opus-4-6\n")
    seen = _capture(monkeypatch, "review")
    argv = ["--out", str(out), "review", "--repo", str(repo)]
    argv += ["--range", f"{base}..{head}", "--trust-repo"]
    main(argv)
    assert seen["config"].finder_model == "claude-opus-4-6"


def test_resume_model_runs_every_stage_on_it(tmp_path, monkeypatch):
    target_dir = tmp_path / "out" / "proj" / "pr-12"
    target_dir.mkdir(parents=True)
    seen = _capture(monkeypatch, "resume")
    main(["resume", str(target_dir), "--trust-repo", "--model", "sonnet"])
    cfg = seen["config"]
    assert (cfg.finder_model, cfg.verifier_model, cfg.merger_model) == (
        "sonnet",
        "sonnet",
        "sonnet",
    )


def test_the_review_names_what_exclude_left_out(canned_state):
    from review_pipeline.render import render_review

    canned_state.target.excluded = ["specs/changes/**"]
    canned_state.target.excluded_files = [f"{SPEC}/design.md"]
    text = render_review(canned_state)
    assert "- **Left out by --exclude (`specs/changes/**`):** 1 files" in text
    assert f"  - `{SPEC}/design.md`" in text


def test_a_review_without_exclude_says_nothing_about_it(canned_state):
    from review_pipeline.render import render_review

    assert "--exclude" not in render_review(canned_state)
