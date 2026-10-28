"""The review buildme launches of its PR (the findings loop's and the epic
coordinator's) reads the change's code, tests, docs and config, not the
specs and runtime files buildme commits with it, and runs on the review model
specs/config.yaml names.

The word-freq run (2026-10-23) reviewed a 2,090-line diff of which ~30 lines
were code, on Opus although `models.review: sonnet` was set, for $9.22."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.epic import review as epic_review
from build_pipeline.findings import loop
from multiplai_core.env import CURRENT_MODEL

# Every file of github-tests PR #12, the word-freq component's PR.
PR_12 = [
    "specs/changes/word-freq/.change.yaml",
    "specs/changes/word-freq/components/top-words/.change.yaml",
    "specs/changes/word-freq/components/word-frequencies/.board.json",
    "specs/changes/word-freq/components/word-frequencies/.build-state.json",
    "specs/changes/word-freq/components/word-frequencies/.change.yaml",
    "specs/changes/word-freq/components/word-frequencies/codebase-analysis.md",
    "specs/changes/word-freq/components/word-frequencies/implementation-plan.md",
    "specs/changes/word-freq/components/word-frequencies/facts.json",
    "specs/changes/word-freq/components/word-frequencies/implementation-notes.md",
    "specs/changes/word-freq/components/word-frequencies/mutation.md",
    "specs/changes/word-freq/components/word-frequencies/respec.md",
    "specs/changes/word-freq/components/word-frequencies/reviews/block-01-round-1.json",
    "specs/changes/word-freq/components/word-frequencies/rubric.md",
    "specs/changes/word-freq/components/word-frequencies/spec-issues.resolved-404e58453218.md",
    "specs/changes/word-freq/components/word-frequencies/specs.md",
    "specs/changes/word-freq/components/word-frequencies/tasks.md",
    "specs/changes/word-freq/components/word-frequencies/unknowns.md",
    "src/textkit/words.py",
    "tests/test_words.py",
]
# Files a buildme PR may also carry that the review must still read.
KEPT = [
    "README.md",
    "docs/usage.md",
    "pyproject.toml",
    "specs/registry/words.md",
    "src/textkit/words.py",
    "tests/test_words.py",
]
LEFT_OUT = [
    "build-progress.md",
    "specs/archive/2026-10-23-word-freq/implementation-plan.md",
    "specs/changes/word-freq/components/word-frequencies/pr-sections/worktree.md",
    "specs/changes/word-freq/components/word-frequencies/.findings-r1-state.json",
]


@pytest.fixture
def no_conf(tmp_path):
    home = tmp_path / "multiplai-home"
    home.mkdir()
    (home / "multiplai.conf").write_text("")
    with patch.dict(
        os.environ,
        {"CLAUDE_MULTIPLAI_HOME": str(home), "PATH": os.environ["PATH"]},
        clear=True,
    ):
        yield


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(root: Path, files: list[str]) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"{rel}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return root, base


def _config(project: Path, yaml_text: str = "", **fields) -> BuildConfig:
    specs = project / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    if yaml_text:
        (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=project, **fields)
    config.specs_dir = specs
    config._load_specs_config()
    return config


def _values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def _reviewed(repo: Path, base: str, globs: list[str], cwd: Path) -> list[str]:
    """The files the review skill keeps for these `--exclude` globs: the same
    pathspec `review_pipeline.target.exclude_pathspec` builds, run from *cwd*."""
    spec = [":(top)", *(f":(top,exclude,glob){g}" for g in globs)]
    out = subprocess.run(
        ["git", "-C", str(cwd), "diff", "--name-only", f"{base}..HEAD", "--", *spec],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(out.split())


def _run(config: BuildConfig) -> loop.FindingsRun:
    return loop.FindingsRun(
        config=config,
        pr=12,
        repo="o/r",
        branch="b",
        change_dir=Path(config.specs_dir) / "changes" / "c",
        trust_repo=True,
    )


def test_the_review_of_pr_12_reads_only_its_code_and_tests(tmp_path, no_conf):
    repo, base = _repo(tmp_path / "proj", PR_12)
    argv = loop.review_argv(_run(_config(repo)))
    assert _reviewed(repo, base, _values(argv, "--exclude"), repo) == [
        "src/textkit/words.py",
        "tests/test_words.py",
    ]


def test_docs_config_and_the_registry_stay_and_runtime_files_go(tmp_path, no_conf):
    repo, base = _repo(tmp_path / "proj", KEPT + LEFT_OUT)
    globs = loop.review_excludes(_config(repo))
    assert _reviewed(repo, base, globs, repo) == sorted(KEPT)


def test_a_project_in_a_subdirectory_leaves_out_its_own_specs(tmp_path, no_conf):
    files = [f"packages/app/{f}" for f in PR_12] + ["specs/changes/other/x.md"]
    repo, base = _repo(tmp_path / "mono", files)
    globs = loop.review_excludes(_config(repo / "packages" / "app"))
    assert "packages/app/specs/changes/**" in globs
    assert _reviewed(repo, base, globs, repo / "packages" / "app") == [
        "packages/app/src/textkit/words.py",
        "packages/app/tests/test_words.py",
        "specs/changes/other/x.md",
    ]


def test_models_review_is_passed_to_the_review_and_its_resume(tmp_path, no_conf):
    repo, _ = _repo(tmp_path / "proj", ["a.py"])
    config = _config(repo, "models:\n  spec: sonnet\n  review: sonnet\n")
    run = _run(config)
    want = [CURRENT_MODEL["sonnet"]]
    assert _values(loop.review_argv(run), "--model") == want
    assert _values(loop.resume_argv(run, "/out/r/pr-12"), "--model") == want
    assert _values(loop.resume_argv(run, "/out/r/pr-12"), "--exclude") == []
    assert _values(epic_review.review_argv(repo, 12, config), "--model") == want
    assert _values(epic_review.resume_argv("/out/r/pr-12", config), "--model") == want


def test_the_review_model_field_is_passed(tmp_path, no_conf):
    repo, _ = _repo(tmp_path / "proj", ["a.py"])
    config = _config(repo, review_model="claude-haiku-4-5")
    assert _values(loop.review_argv(_run(config)), "--model") == ["claude-haiku-4-5"]


@pytest.mark.parametrize(
    "yaml_text, fields",
    [
        ("", {}),
        ("models:\n  default: sonnet\n  agent: sonnet\n", {"model": "model-x"}),
    ],
    ids=["nothing", "only_buildme_defaults"],
)
def test_without_a_review_model_the_review_skill_decides(
    tmp_path, no_conf, yaml_text, fields
):
    """`models.default`, `[buildme] MODEL=` and the session's model are
    buildme's own defaults: they never override the review skill's
    review.yaml or multiplai.conf."""
    repo, _ = _repo(tmp_path / "proj", ["a.py"])
    os.environ["MULTIPLAI_SESSION_MODEL"] = "claude-test-session"
    config = _config(repo, yaml_text, **fields)
    assert config.model_for("review") is not None
    assert config.configured_model_for("review") is None
    assert "--model" not in loop.review_argv(_run(config))
    assert "--model" not in epic_review.review_argv(repo, 12, config)


def test_the_epic_review_leaves_out_the_same_files(tmp_path, no_conf):
    repo, _ = _repo(tmp_path / "proj", ["a.py"])
    config = _config(repo)
    assert _values(epic_review.review_argv(repo, 12, config), "--exclude") == (
        loop.review_excludes(config)
    )
