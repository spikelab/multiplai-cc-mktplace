"""Reviewing code that is not a change: --tree, --tree --path, --dir, file groups, --plan-only."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import SCHEMA
from review_pipeline import orchestrator, sdk, target
from review_pipeline.__main__ import main
from review_pipeline.config import DIMENSIONS, ReviewConfig
from review_pipeline.models import Citation, Finding, FinderOutput, MergeOutput, ReviewState, Verdict
from review_pipeline.prompts import find as find_prompt
from review_pipeline.prompts import merge as merge_prompt
from review_pipeline.stages import RunContext
from review_pipeline.stages import find as find_stage
from review_pipeline.stages.find import file_groups, run_find

ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def write(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)


def tree_repo(tmp_path: Path) -> Path:
    """A repo with code in two top-level directories and one file of each kind a tree review skips."""
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "README.md", "# Shop\n")
    write(repo, "pkg/cart.py", "def total(items):\n    return sum(i.price for i in items)\n")
    write(repo, "pkg/tax.py", "RATE = 0.2\n")
    write(repo, "lib/money.py", "def cents(x):\n    return int(x * 100)\n")
    write(repo, "logo.png", b"\x89PNG\x00\x01\x02binary")
    write(repo, "uv.lock", "version = 1\n")
    write(repo, "gen/schema.py", "GENERATED = True\n")
    write(repo, ".gitattributes", "gen/schema.py linguist-generated\n")
    write(repo, "data/big.txt", "x" * (target.TREE_MAX_CHARS + 1))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "shop")
    return repo


def tree_info(repo: Path, path: str | None = None):
    resolved = target.resolve(repo, tree="HEAD", path=path)
    return resolved, target.build_target(resolved)


# --- target ----------------------------------------------------------------------


def test_tree_lists_files_at_head_and_skips_each_kind_with_its_reason(tmp_path):
    repo = tree_repo(tmp_path)
    resolved, info = tree_info(repo)
    assert (resolved.kind, resolved.base_sha, resolved.ref) == ("tree", target.EMPTY_TREE, ".")
    assert info.head_sha == git(repo, "rev-parse", "HEAD") and info.is_tree and info.commits == []
    assert info.files == [".gitattributes", "README.md", "lib/money.py", "pkg/cart.py", "pkg/tax.py"]
    assert dict(info.skipped) == {
        "logo.png": "binary",
        "data/big.txt": f"over {target.TREE_MAX_CHARS} characters",
        "uv.lock": "lockfile",
        "gen/schema.py": "linguist-generated is set",
    }
    assert info.slug == "shop--tree" and info.label.startswith("shop: whole tree at ")
    written = target.write_target_files(info, "", tmp_path / "out" / info.slug)
    out = Path(written.diff_path).parent
    assert (out / "diff.patch").read_text() == ""
    assert "uv.lock\tlockfile" in (out / "skipped.txt").read_text()


def test_tree_path_limits_the_list(tmp_path):
    repo = tree_repo(tmp_path)
    resolved, info = tree_info(repo, "./pkg/")
    assert resolved.ref == "pkg"
    assert info.files == ["pkg/cart.py", "pkg/tax.py"]
    assert info.slug == "shop--tree-pkg" and info.label.startswith("shop: pkg at ")
    assert target.target_gate(resolved, None, info.files).passed


def test_a_tree_with_nothing_left_to_review_fails_the_gate(tmp_path):
    repo = tree_repo(tmp_path)
    resolved, info = tree_info(repo, "gen")
    assert info.files == []
    gate = target.target_gate(resolved, None, info.files)
    assert not gate.passed and "no file under gen" in gate.reason
    with pytest.raises(orchestrator.ReviewError):
        orchestrator.prepare(orchestrator.TargetSpec(repo=str(repo), tree="HEAD", path="gen"), tmp_path / "out")


def test_tree_needs_exactly_one_selector_and_path_only_with_tree(tmp_path):
    repo = tree_repo(tmp_path)
    with pytest.raises(target.TargetError):
        target.resolve(repo, tree="HEAD", branch="main")
    with pytest.raises(target.TargetError):
        target.resolve(repo, range_="HEAD..HEAD", path="pkg")
    assert target.resolve(repo, tree="nope").problem == "nope does not resolve to a commit"


# --- --dir -----------------------------------------------------------------------


def _listing(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(root.rglob("*"))}


def test_dir_is_copied_and_committed_and_the_source_is_unchanged(tmp_path):
    src = tmp_path / "plain" / "notes-app"
    write(src, "app.py", "print('hi')\n")
    write(src, "lib/util.py", "X = 1\n")
    write(src, "node_modules/dep/index.js", "module.exports = 1\n")
    write(src, "secret.env", "TOKEN=1\n")
    write(src, ".gitignore", "*.env\n")
    before = _listing(src)

    state, target_dir = orchestrator.prepare(orchestrator.TargetSpec(repo="", dir=str(src)), tmp_path / "out")

    assert _listing(src) == before  # nothing written to the source, no .git there
    copy = Path(state.target.repo_path)
    slug = f"{target.dir_name(src.resolve())}--tree"
    assert copy == (tmp_path / "out" / slug / "source").resolve()
    assert git(copy, "rev-list", "--count", "HEAD") == "1"
    assert state.target.files == [".gitignore", "app.py", "lib/util.py"]  # no node_modules, no ignored file
    assert state.target.slug == slug and target_dir.name == slug and slug.startswith("notes-app-")
    assert state.target.label.startswith(f"{src}: whole tree at ")


def test_dir_inside_a_git_repository_exits_2(tmp_path, capsys):
    repo = tree_repo(tmp_path)
    code = main(["--out", str(tmp_path / "out"), "review", "--dir", str(repo / "pkg"), "--trust-repo"])
    assert code == 2
    err = capsys.readouterr().err
    assert f"--repo {repo.resolve()} --tree --path pkg" in err
    assert not (tmp_path / "out" / "pkg--tree").exists()


# --- file groups -----------------------------------------------------------------


def _tree_state(repo: Path, path: str | None = None) -> ReviewState:
    _, info = tree_info(repo, path)
    return ReviewState(target=info)


def test_file_groups_split_at_the_size_limit_and_at_each_top_level_directory(tmp_path):
    repo = tmp_path / "sized"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name in ("a/one.py", "a/two.py", "a/three.py", "b/four.py"):
        write(repo, name, "y" * 40 + "\n")  # 41 characters each
    write(repo, "a/huge.py", "z" * 200 + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sized")
    info = _tree_state(repo).target
    assert file_groups(info, max_chars=100) == [
        ["a/huge.py"],                    # larger than the limit: a group of its own
        ["a/one.py", "a/three.py"],       # two fit in 100; a third would not
        ["a/two.py"],
        ["b/four.py"],                    # a new top-level directory starts a new group
    ]


def test_file_groups_count_directories_below_the_reviewed_path(tmp_path):
    repo = tree_repo(tmp_path)
    write(repo, "pkg/sub/deep.py", "D = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "deep")
    info = _tree_state(repo, "pkg").target
    assert file_groups(info) == [["pkg/cart.py", "pkg/tax.py"], ["pkg/sub/deep.py"]]


def test_a_change_review_is_one_group_holding_every_changed_file(target_info):
    assert file_groups(target_info, max_chars=1) == [list(target_info.files)]


# --- find ------------------------------------------------------------------------


class Fake:
    """Stands in for sdk.agent_call_structured; records (label, prompt)."""

    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail or set()

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt))
        if budget_label in self.fail:
            raise sdk.AgentCallError("boom")
        if schema is FinderOutput:
            return FinderOutput()
        if schema is MergeOutput:
            return MergeOutput()
        return Verdict(status="confirmed", reason="r")


def _ctx(tmp_path: Path, dims=DIMENSIONS) -> RunContext:
    snapshot = tmp_path / "snap"
    snapshot.mkdir(exist_ok=True)
    return RunContext(config=ReviewConfig(concurrency=2, dimensions=dims), snapshot=snapshot, diff="")


async def test_a_tree_with_two_groups_runs_four_dimensions_per_group_and_no_history(tmp_path, monkeypatch):
    repo = tree_repo(tmp_path)
    state = _tree_state(repo)
    groups = file_groups(state.target)
    monkeypatch.setattr(find_stage, "file_groups", lambda t: groups[:2])
    fake = Fake()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    await run_find(state, _ctx(tmp_path))
    assert len(fake.calls) == 8
    assert not any(label == "find:history" for label, _ in fake.calls)
    assert sorted(state.finder_results) == sorted(f"{d}@{i}" for i in (0, 1) for d in DIMENSIONS if d != "history")


async def test_a_resumed_tree_review_asks_only_the_missing_pairs(tmp_path, monkeypatch):
    repo = tree_repo(tmp_path)
    state = _tree_state(repo)
    groups = file_groups(state.target)
    monkeypatch.setattr(find_stage, "file_groups", lambda t: groups[:2])
    first = Fake(fail={"find:tests"})
    monkeypatch.setattr(sdk, "agent_call_structured", first)
    await run_find(state, _ctx(tmp_path))
    assert state.finder_results["tests@0"].error and state.finder_results["tests@1"].error
    # A failed finder is kept, like a change review's, so resume asks only pairs with no result.
    for key in ("tests@0", "tests@1"):
        del state.finder_results[key]
    state.stage = "target"
    again = Fake()
    monkeypatch.setattr(sdk, "agent_call_structured", again)
    await run_find(state, _ctx(tmp_path))
    assert [label for label, _ in again.calls] == ["find:tests", "find:tests"]


async def test_a_tree_review_records_one_check_per_group_and_times_each_call(tmp_path, monkeypatch):
    repo = tree_repo(tmp_path)
    state = _tree_state(repo)
    every = file_groups(state.target)
    groups = [every[0], every[-1]]  # the last group holds pkg/cart.py
    monkeypatch.setattr(find_stage, "file_groups", lambda t: groups)
    cart = Finding(claim="total() ignores item quantity", severity="MEDIUM", file="pkg/cart.py",
                   line_start=2, line_end=2, failure_scenario="Two of one item are charged once.",
                   citations=[Citation(path="pkg/cart.py", line_start=2, line_end=2,
                                       quote="return sum(i.price for i in items)")])
    in_cart = next(i for i, g in enumerate(groups) if "pkg/cart.py" in g)

    misquoted = cart.model_copy(update={"claim": "tax is never added", "citations": [
        Citation(path="pkg/cart.py", line_start=2, line_end=2, quote="not in the file")]})

    async def agents(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "find:diff-bugs" and "- pkg/cart.py" in prompt:
            return FinderOutput(findings=[cart, misquoted])
        return FinderOutput()

    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    await run_find(state, _ctx(tmp_path))
    dims = [d for d in DIMENSIONS if d != "history"]
    assert sorted(c.subject for c in state.checks) == sorted(f"{d}@{i}" for i in (0, 1) for d in dims)
    check = next(c for c in state.checks if c.subject == f"diff-bugs@{in_cart}")
    assert check.given[0] == f"files ({len(groups[in_cart])}, group {in_cart + 1} of 2)"
    assert not any(g.startswith("diff (") for g in check.given)
    # The fates are set on the entry of the group's own call ("diff-bugs@<i>").
    assert [e["fate"] for e in check.findings] == ["kept", "rejected"]
    assert [(g.gate, g.passed) for g in state.gate_checks] == [("finding_gate", True), ("finding_gate", False)]


async def test_overlapping_calls_of_one_dimension_each_close_their_own_interval(tmp_path, monkeypatch):
    import asyncio

    repo = tree_repo(tmp_path)
    state = _tree_state(repo)
    every = file_groups(state.target)
    monkeypatch.setattr(find_stage, "file_groups", lambda t: every)
    delays = iter([0.05, 0.0, 0.0])  # the first group's call ends after the second one starts

    async def agents(prompt, schema, **kwargs):
        await asyncio.sleep(next(delays))
        return FinderOutput()

    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    await run_find(state, _ctx(tmp_path, dims=("diff-bugs",)))
    intervals = state.timings["find:diff-bugs"]
    assert len(intervals) == len(every) == 3 and all(i.ended_at for i in intervals)


async def test_a_change_review_still_makes_five_finder_calls(target_info, tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    state = ReviewState(target=target_info)
    await run_find(state, _ctx(tmp_path))
    assert sorted(label for label, _ in fake.calls) == sorted(f"find:{d}" for d in DIMENSIONS)
    assert sorted(state.finder_results) == sorted(DIMENSIONS)


# --- prompts ---------------------------------------------------------------------


def test_the_tree_prompt_lists_the_group_and_has_no_diff(tmp_path):
    repo = tree_repo(tmp_path)
    info = _tree_state(repo).target
    text = find_prompt.build(info, "conventions", "", "### CLAUDE.md\n\nrule", files=["pkg/cart.py"])
    assert "```diff" not in text and "Commits in the change" not in text and "Files changed:" not in text
    assert "- pkg/cart.py" in text and "- pkg/tax.py" not in text
    assert "Open each one with Read" in text and "### CLAUDE.md" in text
    assert "path of the file under review the bug is in" in text
    assert find_prompt.DIMENSION_TASKS_TREE["conventions"] in text
    assert "the same code independently" in merge_prompt.build(info, [])


def test_a_change_prompt_is_unchanged_by_the_tree_wording(target_info):
    text = find_prompt.build(target_info, "tests", "+x = 1\n")
    assert "```diff" in text and "Open each one with Read" not in text
    assert "the same change independently" in merge_prompt.build(target_info, [])


# --- --plan-only, and a whole tree review through the CLI --------------------------


def test_plan_only_makes_no_agent_call_and_writes_plan_txt(tmp_path, monkeypatch, capsys):
    async def no_call(*args, **kwargs):
        raise AssertionError("--plan-only called an agent")

    monkeypatch.setattr(sdk, "agent_call_structured", no_call)
    repo = tree_repo(tmp_path)
    out = tmp_path / "out"
    assert main(["--out", str(out), "review", "--repo", str(repo), "--tree", "--plan-only"]) == 0
    stdout = capsys.readouterr().out
    assert "groups: 3  finder calls: 12  files skipped: 4" in stdout
    target_dir = out / "shop--tree"
    assert stdout.strip().splitlines()[-1] == f"plan: {target_dir / 'plan.txt'}"
    assert (target_dir / "plan.txt").read_text().splitlines()[-1] == "groups: 3  finder calls: 12  files skipped: 4"
    assert (target_dir / "skipped.txt").is_file()
    assert not (target_dir / "review-state.json").exists()  # no ledger, no checkpoint


def test_a_tree_review_writes_a_findings_file_that_validates(tmp_path, monkeypatch, capsys):
    import jsonschema

    repo = tree_repo(tmp_path)
    finding = Finding(claim="total() ignores item quantity", severity="MEDIUM", file="pkg/cart.py",
                      line_start=2, line_end=2, failure_scenario="Two of one item are charged once.",
                      citations=[Citation(path="pkg/cart.py", line_start=2, line_end=2,
                                          quote="return sum(i.price for i in items)")])

    async def agents(prompt, schema, *, budget_label="", **kwargs):
        if budget_label == "find:diff-bugs" and "- pkg/cart.py" in prompt:
            return FinderOutput(findings=[finding])
        if schema is FinderOutput:
            return FinderOutput()
        return Verdict(status="confirmed", reason="sum ignores quantity",
                       citations=[finding.citations[0]], expected_behaviour="Each item counts its quantity.")

    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    out = tmp_path / "out"
    assert main(["--out", str(out), "review", "--repo", str(repo), "--tree", "--trust-repo"]) == 0
    data = json.loads((out / "shop--tree" / "findings.json").read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert data["target"]["base_sha"] == target.EMPTY_TREE
    assert data["target"]["files_changed"] == [".gitattributes", "README.md", "lib/money.py", "pkg/cart.py",
                                               "pkg/tax.py"]
    assert [f["claim"] for f in data["findings"]] == ["total() ignores item quantity"]
    summary = (out / "shop--tree" / "summary-shop--tree.md").read_text()
    assert "5 files reviewed as they stand at" in summary and "4 skipped" in summary


def _dir_review(src: Path, out: Path):
    return orchestrator.prepare(orchestrator.TargetSpec(repo="", dir=str(src)), out)


def test_a_second_dir_run_keeps_the_commit_an_earlier_review_names(tmp_path, capsys):
    src = tmp_path / "plain" / "notes-app"
    write(src, "app.py", "print('hi')\n")
    out = tmp_path / "out"
    first, target_dir = _dir_review(src, out)
    (target_dir / "progress.log").write_text("DONE review finished\n")
    copy, head = Path(first.target.repo_path), first.target.head_sha

    # --plan-only on unchanged contents: the same commit, and the last run's progress.log stays.
    code = main(["--out", str(out), "review", "--dir", str(src), "--plan-only"])
    assert code == 0 and "groups: 1" in capsys.readouterr().out
    assert git(copy, "rev-parse", "HEAD") == head and git(copy, "rev-list", "--count", "HEAD") == "1"
    assert (target_dir / "progress.log").read_text() == "DONE review finished\n"

    # Changed contents: a new commit on top, and the earlier head is still a readable commit.
    write(src, "app.py", "print('bye')\n")
    second, _ = _dir_review(src, out)
    assert second.target.head_sha != head
    assert git(copy, "rev-list", "--count", "HEAD") == "2"
    assert git(copy, "show", f"{head}:app.py") == "print('hi')"


def test_two_directories_with_one_basename_get_two_reviews(tmp_path):
    a, b = tmp_path / "one" / "app", tmp_path / "two" / "app"
    write(a, "a.py", "A = 1\n")
    write(b, "b.py", "B = 1\n")
    out = tmp_path / "out"
    first, first_dir = _dir_review(a, out)
    second, second_dir = _dir_review(b, out)
    assert first_dir != second_dir and first.target.slug != second.target.slug
    assert git(Path(first.target.repo_path), "show", f"{first.target.head_sha}:a.py") == "A = 1"
    assert second.target.files == ["b.py"]


def test_dir_with_out_inside_the_source_is_refused_and_writes_nothing_there(tmp_path, capsys):
    src = tmp_path / "plain" / "proj"
    write(src, "app.py", "x = 1\n")
    before = _listing(src)
    code = main(["--out", str(src / "reviews"), "review", "--dir", str(src), "--trust-repo"])
    assert code == 2
    assert "is inside" in capsys.readouterr().err
    assert _listing(src) == before


def test_dir_that_cannot_be_copied_exits_2_with_a_message(tmp_path, capsys):
    src = tmp_path / "plain" / "locked"
    write(src, "app.py", "x = 1\n")
    write(src, "secret/key.txt", "k\n")
    (src / "secret").chmod(0)
    try:
        if os.access(src / "secret", os.R_OK):
            pytest.skip("running with permissions that ignore chmod")
        code = main(["--out", str(tmp_path / "out"), "review", "--dir", str(src), "--trust-repo"])
    finally:
        (src / "secret").chmod(0o755)
    assert code == 2
    assert "cannot copy" in capsys.readouterr().err


def test_a_large_file_that_is_not_utf8_does_not_stop_a_tree_review(tmp_path, monkeypatch):
    repo = tmp_path / "latin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "app.py", "x = 1\n")
    write(repo, "notes.txt", ("caf\xe9 " * 20).encode("latin-1") + b"\n")  # text, not UTF-8
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "latin")
    monkeypatch.setattr(target, "TREE_MAX_CHARS", 50)
    files, skipped = target.tree_files(repo, git(repo, "rev-parse", "HEAD"))
    assert files == ["app.py"] and skipped == [("notes.txt", "over 50 characters")]
