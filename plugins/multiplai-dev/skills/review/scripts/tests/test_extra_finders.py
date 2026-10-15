"""Extra finders (`--extra-finders`): one per ~600 changed lines, on a change's first review round only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from review_pipeline import budget, sdk
from review_pipeline.__main__ import main
from review_pipeline.chunks import FileChange, diff_files, finder_chunks
from review_pipeline.config import EXTRA_DIMENSION, load_config
from review_pipeline.models import (AssessOutput, Citation, Finding, FinderOutput, MergeOutput, RepeatsOutput,
                                    ReviewState, Verdict)

ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
EXTRA = f"find:{EXTRA_DIMENSION}"


# --- the chunking: the 7 sample cases of `finderChunks` in run-workflow-2026-10-15.js ------------------


def f(path: str, n: int) -> FileChange:
    return FileChange(path=path, added=n, deleted=0)


def shape(files: list[FileChange]) -> list[tuple[int, int]]:
    """(changed lines, number of files) per chunk, as the JS sample printed it."""
    return [(c.lines, len(c.files)) for c in finder_chunks(files)]


SKEWED = [f(p, 590 if i % 2 == 0 else 20) for i, p in enumerate("abcdeghijklmn")]


@pytest.mark.parametrize("files, expected", [
    pytest.param([f("a", 100), f("b", 20)], [(120, 2)], id="small 120"),
    pytest.param([f(f"f{i}", 100) for i in range(15)], [(600, 6), (600, 6), (300, 3)], id="1500 even"),
    pytest.param([f("a", 50), f("big", 900), f("c", 50)], [(50, 1), (900, 1), (50, 1)], id="one big file"),
    pytest.param([f(f"f{i}", 100) for i in range(60)], [(1000, 10)] * 6, id="6000 lines"),
    pytest.param([f("a", 100), f("uv.lock", 3000)], [(100, 1)], id="lockfile only big"),
    pytest.param(SKEWED, [(610, 2)] * 5 + [(1200, 3)], id="skewed"),
    pytest.param([], [], id="empty"),
])
def test_finder_chunks_matches_the_run_scripts_sample_cases(files, expected):
    assert shape(files) == expected


def test_chunks_keep_diff_order_and_merge_the_smallest_neighbours():
    chunks = finder_chunks(SKEWED)
    assert [c.files for c in chunks] == [["a", "b"], ["c", "d"], ["e", "g"], ["h", "i"], ["j", "k"],
                                         ["l", "m", "n"]]
    assert [c.files[0] + ".." + c.files[-1] for c in finder_chunks([f(f"f{i}", 100) for i in range(60)])] == [
        f"f{i}..f{i + 9}" for i in range(0, 60, 10)]


def test_diff_files_reads_each_files_changed_lines_in_diff_order():
    diff = (
        "diff --git a/b.py b/b.py\nindex 1..2 100644\n--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n"
        " keep\n-old\n+new\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
        "-x\n--- not a header\n"
        "diff --git a/img.png b/img.png\nnew file mode 100644\nBinary files /dev/null and b/img.png differ\n"
        "diff --git a/a.py b/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+one\n+++two\n"
    )
    files = diff_files(diff)
    assert [(c.path, c.added, c.deleted) for c in files] == [
        ("b.py", 1, 1), ("gone.py", 0, 2), ("img.png", 0, 0), ("a.py", 2, 0)]
    assert files[0].text.startswith("diff --git a/b.py b/b.py\n") and "+new\n" in files[0].text
    assert "gone.py" not in files[0].text and "a.py" not in files[2].text


# --- the find stage, through the CLI -----------------------------------------------------------------

HALF = 500  # changed lines per file: two files make two chunks
BASE = {"a.py": "A = 0\n", "b.py": "B = 0\n"}
HEAD = {"a.py": BASE["a.py"] + "".join(f"a_{i} = {i}\n" for i in range(HALF)),
        "b.py": BASE["b.py"] + "".join(f"b_{i} = {i}\n" for i in range(HALF))}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def two_chunk_repo(tmp_path) -> tuple[Path, str, str]:
    repo = tmp_path / "two"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, BASE, "base")
    head = _commit(repo, HEAD, "add 500 lines to each file")
    return repo, base, head


def chunk_finding(path: str) -> Finding:
    """What the extra finder for *path*'s chunk reports: one MEDIUM on its second added line."""
    name = path[0]
    quote = f"{name}_1 = 1"
    return Finding(claim=f"{name}_1 is set to 1 where the plan says 2", severity="MEDIUM", file=path,
                   line_start=3, line_end=3, failure_scenario=f"import {path} -> {name}_1 is 1",
                   citations=[Citation(path=path, line_start=3, line_end=3, quote=quote)])


class Agents:
    """Canned answers by stage label. Each extra finder reports one finding on its chunk's file;
    `stop_on_extra_call` raises a budget stop on that extra-finder call (1-based)."""

    def __init__(self, stop_on_extra_call: int | None = None):
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.stop_on_extra_call = stop_on_extra_call
        self.extra_calls = 0

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        self.calls.append(budget_label)
        self.prompts.append((budget_label, prompt))
        stage = budget_label.split(":")[0]
        if budget_label == EXTRA:
            self.extra_calls += 1
            if self.extra_calls == self.stop_on_extra_call:
                self.stop_on_extra_call = None
                raise budget.BudgetExceededError("circuit breaker stopped the run during find:extra")
            return FinderOutput(findings=[chunk_finding(p) for p in ("a.py", "b.py")
                                          if f"diff --git a/{p} b/{p}" in prompt])
        if stage == "find":
            return FinderOutput()
        if stage == "verify":
            path = "a.py" if "a_1 = 1" in prompt else "b.py"
            return Verdict(status="confirmed", reason="the line says so", topic="code",
                           impact="correctness-only", expected_behaviour="It is 2.",
                           citations=[Citation(path=path, line_start=3, line_end=3, quote=f"{path[0]}_1 = 1")])
        if stage == "merge":
            return MergeOutput()
        if stage == "assess":
            return AssessOutput()
        if stage == "repeats":
            return RepeatsOutput()
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> Agents:
        fake = Agents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def _review(repo: Path, base: str, head: str, out: Path, *extra: str) -> int:
    out.mkdir(exist_ok=True)
    if not (out / "review.yaml").exists():
        (out / "review.yaml").write_text("concurrency: 1\n")  # one finder at a time: the stop point is exact
    return main(["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo),
                 "--range", f"{base}..{head}", "--trust-repo", *extra])


def _state(out: Path) -> tuple[Path, ReviewState]:
    (path,) = [p for p in out.rglob("review-state.json") if "rounds" not in p.parts]
    return path.parent, ReviewState.model_validate_json(path.read_text())


def _extra_prompts(fake: Agents, since: int = 0) -> list[str]:
    return [p for label, p in fake.prompts[since:] if label == EXTRA]


def test_extra_finders_ask_one_finder_per_chunk_once(two_chunk_repo, tmp_path, agents):
    repo, base, head = two_chunk_repo
    fake = agents()
    assert _review(repo, base, head, tmp_path / "out", "--extra-finders") == 0
    _, state = _state(tmp_path / "out")
    assert state.extra_finders is True
    assert sorted(k for k in state.finder_results if k.startswith(f"{EXTRA_DIMENSION}@")) == [
        f"{EXTRA_DIMENSION}@0", f"{EXTRA_DIMENSION}@1"]
    first, second = _extra_prompts(fake)
    # Each reads only its own chunk's hunks, and is told not to read whole files.
    assert "diff --git a/a.py b/a.py" in first and "diff --git a/b.py b/b.py" not in first
    assert "diff --git a/b.py b/b.py" in second and "diff --git a/a.py b/a.py" not in second
    for text in (first, second):
        assert "enclosing function" in text and "callers and callees" in text and "whole file" in text
    # Their findings go through verify like the others'.
    assert sorted(f.file for f in state.findings) == ["a.py", "b.py"]
    assert all(f.finder == EXTRA_DIMENSION for f in state.findings)
    assert sum(label.startswith("verify") for label in fake.calls) == 2


def test_without_the_option_no_extra_finder_runs(two_chunk_repo, tmp_path, agents):
    repo, base, head = two_chunk_repo
    fake = agents()
    assert _review(repo, base, head, tmp_path / "out") == 0
    assert EXTRA not in fake.calls
    assert _state(tmp_path / "out")[1].extra_finders is False


def test_review_yaml_extra_finders_true_turns_them_on(two_chunk_repo, tmp_path, agents):
    repo, base, head = two_chunk_repo
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("concurrency: 1\nextra_finders: true\n")
    fake = agents()
    assert _review(repo, base, head, out) == 0
    assert fake.calls.count(EXTRA) == 2


def test_load_config_reads_extra_finders(tmp_path):
    assert load_config(tmp_path).extra_finders is False
    (tmp_path / "review.yaml").write_text("extra_finders: true\n")
    assert load_config(tmp_path).extra_finders is True


def test_a_resume_after_extra_0_asks_only_extra_1(two_chunk_repo, tmp_path, agents):
    """Row R5: a stop after `extra@0` returned keeps it; the resume asks only `extra@1`, and the result
    matches a run that never stopped."""
    repo, base, head = two_chunk_repo
    never_stopped = agents()
    assert _review(repo, base, head, tmp_path / "whole", "--extra-finders") == 0
    _, whole = _state(tmp_path / "whole")
    assert never_stopped.calls.count(EXTRA) == 2

    fake = agents(stop_on_extra_call=2)  # extra@0 returns, the run stops on extra@1
    assert _review(repo, base, head, tmp_path / "out", "--extra-finders") == 4
    target_dir, saved = _state(tmp_path / "out")
    assert saved.extra_finders is True
    assert f"{EXTRA_DIMENSION}@0" in saved.finder_results and f"{EXTRA_DIMENSION}@1" not in saved.finder_results
    first_run = len(fake.calls)
    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    [asked] = _extra_prompts(fake, first_run)
    assert "diff --git a/b.py b/b.py" in asked and "diff --git a/a.py b/a.py" not in asked
    _, done = _state(tmp_path / "out")
    assert done.extra_finders is True
    assert sorted(done.finder_results) == sorted(whole.finder_results)
    assert [f.id for f in done.findings] == [f.id for f in whole.findings]


def test_a_second_round_on_the_same_change_asks_no_extra_finder(two_chunk_repo, tmp_path, agents):
    repo, base, head = two_chunk_repo
    out = tmp_path / "out"
    first = agents()
    assert _review(repo, base, head, out, "--extra-finders") == 0
    assert first.calls.count(EXTRA) == 2
    second = agents()
    assert _review(repo, base, head, out, "--extra-finders") == 0
    assert EXTRA not in second.calls
    _, state = _state(out)
    assert state.extra_finders is False
    assert not any(k.startswith(f"{EXTRA_DIMENSION}@") for k in state.finder_results)


def test_a_first_review_that_stopped_before_export_does_not_use_up_the_first_round(two_chunk_repo, tmp_path,
                                                                                  agents):
    """A first review stopped by the budget is kept as a round with no findings.json; the next
    `review` (a fresh run, not a resume) is still the change's first complete round."""
    repo, base, head = two_chunk_repo
    out = tmp_path / "out"
    stopped = agents(stop_on_extra_call=1)
    assert _review(repo, base, head, out, "--extra-finders") == 4
    target_dir, _ = _state(out)
    second = agents()
    assert _review(repo, base, head, out, "--extra-finders") == 0
    kept = sorted((target_dir / "rounds").iterdir())
    assert len(kept) == 1 and not (kept[0] / "findings.json").exists()  # the stopped attempt, kept
    assert second.calls.count(EXTRA) == 2
    _, state = _state(out)
    assert state.extra_finders is True
    assert sorted(k for k in state.finder_results if k.startswith(f"{EXTRA_DIMENSION}@")) == [
        f"{EXTRA_DIMENSION}@0", f"{EXTRA_DIMENSION}@1"]
    third = agents()  # the round after the first complete one asks none
    assert _review(repo, base, head, out, "--extra-finders") == 0
    assert EXTRA not in third.calls
    assert EXTRA in stopped.calls  # the stopped attempt was a first round too


def test_extra_finders_go_with_a_change_review(two_chunk_repo, tmp_path, capsys):
    repo, _, _ = two_chunk_repo
    code = main(["--session-id", "sess-test", "--out", str(tmp_path / "out"), "review", "--repo", str(repo),
                 "--tree", "--trust-repo", "--extra-finders"])
    assert code == 2
    assert "--extra-finders goes with a change review" in capsys.readouterr().err


def test_plan_only_counts_the_extra_finders(two_chunk_repo, tmp_path, capsys):
    """`--plan-only`'s total line counts the calls the review would make, the extra finders' too."""
    repo, base, head = two_chunk_repo

    def calls(out: Path, *extra: str) -> int:
        assert main(["--out", str(out), "review", "--repo", str(repo), "--range", f"{base}..{head}",
                     "--plan-only", *extra]) == 0
        total = [line for line in capsys.readouterr().out.splitlines() if "finder calls:" in line][-1]
        return int(total.split("finder calls:")[1].split()[0])

    without = calls(tmp_path / "a")
    assert calls(tmp_path / "b", "--extra-finders") == without + 2
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "review.yaml").write_text("extra_finders: true\n")
    assert calls(tmp_path / "c") == without + 2
