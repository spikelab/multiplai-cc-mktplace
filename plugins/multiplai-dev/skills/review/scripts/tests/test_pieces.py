"""Piece splitting: a change review's diff cut at file boundaries into pieces of related files.

Most cases build the diff text directly, with each file's section exactly as
long as the case needs (a file's size is the length of its section, headers
included); the cases about where pieces come from build tmp git repositories
and run the review's own preparation and `--plan-only`.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest
from review_pipeline import sdk
from review_pipeline.__main__ import main
from review_pipeline.chunks import diff_files
from review_pipeline.models import (
    AssessOutput,
    FinderOutput,
    MergeOutput,
    RepeatsOutput,
    ReviewState,
)

ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
PIPELINE = Path(__file__).resolve().parents[1] / "review_pipeline"


def new(module: str, name: str):
    """A name this block adds, looked up per test so its absence fails that test rather than collection."""
    return getattr(importlib.import_module(f"review_pipeline.{module}"), name)


def split(diff: str, head=(), base=()):
    return new("chunks", "split_pieces")(diff, head_tree=frozenset(head), base_tree=frozenset(base))


# --- diff sections of an exact size ---------------------------------------------------------------------


def section(path: str, size: int = 400, *, old: str | None = None, status: str = "modified") -> str:
    """One file's part of a unified diff, exactly *size* characters long (headers included)."""
    old = old or path
    if status == "added":
        head = (f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
                f"--- /dev/null\n+++ b/{path}\n")
        hunk = "@@ -0,0 +1 @@\n+"
    elif status == "deleted":
        head = (f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex 1111111..0000000\n"
                f"--- a/{path}\n+++ /dev/null\n")
        hunk = "@@ -1 +0,0 @@\n-"
    elif status == "renamed":
        head = (f"diff --git a/{old} b/{path}\nsimilarity index 90%\nrename from {old}\nrename to {path}\n"
                f"index 1111111..2222222 100644\n--- a/{old}\n+++ b/{path}\n")
        hunk = "@@ -1 +1 @@\n+"
    else:
        head = f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
        hunk = "@@ -1 +1 @@\n+"
    pad = size - len(head) - len(hunk) - 1
    assert pad >= 1, f"{path}: {size} characters is too small for its headers"
    return head + hunk + "x" * pad + "\n"


def binary(path: str) -> str:
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
            f"Binary files /dev/null and b/{path} differ\n")


def files_of(pieces) -> list[list[str]]:
    return [p.files for p in pieces]


# --- grouping ---------------------------------------------------------------------------------------------


def test_files_of_one_skill_share_a_piece():
    diff = section("skills/review/scripts/a.py") + section("skills/review/SKILL.md")
    (piece,) = split(diff, head={"skills/review/SKILL.md", "skills/review/scripts/a.py"})
    assert (piece.id, piece.group, piece.meta) == (1, "skills/review", False)
    assert piece.files == ["skills/review/SKILL.md", "skills/review/scripts/a.py"]


def test_the_nearest_marker_wins():
    head = {"skills/review/SKILL.md", "skills/review/scripts/pyproject.toml", "skills/review/scripts/x/a.py"}
    (piece,) = split(section("skills/review/scripts/x/a.py"), head=head)
    assert piece.group == "skills/review/scripts"


@pytest.mark.parametrize("marker", ["pyproject.toml", "package.json", "Cargo.toml", "go.mod", "SKILL.md"])
def test_every_marker_makes_a_group(marker):
    (piece,) = split(section("pkg/src/a.py"), head={f"pkg/{marker}", "pkg/src/a.py"})
    assert piece.group == "pkg"


def test_files_of_two_packages_are_not_mixed():
    head = {"a/pyproject.toml", "b/pyproject.toml", "a/m.py", "b/m.py"}
    pieces = split(section("a/m.py") + section("b/m.py"), head=head)
    assert [(p.group, p.files) for p in pieces] == [("a", ["a/m.py"]), ("b", ["b/m.py"])]


def test_a_file_with_no_package_falls_in_the_root_group():
    (piece,) = split(section("tools/x.py"), head={"tools/x.py", "pkg/pyproject.toml"})
    assert piece.group == ""


def test_a_deleted_file_is_grouped_by_the_layout_at_base():
    diff = section("pkg/mod.py", status="deleted") + section("pkg/pyproject.toml", status="deleted")
    (piece,) = split(diff, head=set(), base={"pkg/mod.py", "pkg/pyproject.toml"})
    assert (piece.group, piece.files) == ("pkg", ["pkg/mod.py", "pkg/pyproject.toml"])


def test_a_renamed_file_is_grouped_by_its_head_path():
    diff = section("new/pkg/mod.py", old="old/pkg/mod.py", status="renamed")
    head = {"new/pkg/pyproject.toml", "new/pkg/mod.py"}
    base = {"old/pkg/pyproject.toml", "old/pkg/mod.py"}
    (piece,) = split(diff, head=head, base=base)
    assert (piece.group, piece.files) == ("new/pkg", ["new/pkg/mod.py"])


# --- test pairing -------------------------------------------------------------------------------------------


def test_a_test_in_another_directory_joins_its_module():
    head = {"lib/pyproject.toml", "lib/chunks.py", "tests/test_chunks.py", "lib/other.py"}
    diff = section("lib/chunks.py") + section("lib/other.py") + section("tests/test_chunks.py")
    (piece,) = split(diff, head=head)
    assert piece.group == "lib"
    assert piece.files == ["lib/chunks.py", "tests/test_chunks.py", "lib/other.py"]


@pytest.mark.parametrize("module, test", [
    ("pkg/fill.py", "pkg/tests/test_fill.py"),
    ("pkg/fill.py", "pkg/fill_test.py"),
    ("go/srv.go", "go/srv_test.go"),
    ("web/app.ts", "web/app.test.ts"),
    ("web/app.js", "web/app.spec.js"),
])
def test_each_naming_pattern_pairs_a_test_with_its_module(module, test):
    # The module and its test fill a piece together; an unrelated file in between goes to the next.
    diff = section(module, 30_000) + section("a_first.py", 30_000) + section(test, 15_000)
    pieces = split(diff)
    assert [module, test] in files_of(pieces)


def test_the_module_sharing_the_longest_directory_prefix_wins():
    diff = section("a/util.py", 30_000) + section("b/util.py", 30_000) + section("a/tests/test_util.py", 10_000)
    assert files_of(split(diff)) == [["a/util.py", "a/tests/test_util.py"], ["b/util.py"]]


def test_a_tie_goes_to_the_first_module_in_path_order():
    diff = section("b/util.py", 30_000) + section("a/util.py", 30_000) + section("test_util.py", 10_000)
    assert files_of(split(diff)) == [["a/util.py", "test_util.py"], ["b/util.py"]]


def test_a_test_without_a_changed_module_stays_in_its_own_group():
    head = {"tests/pyproject.toml", "tests/test_merge.py", "src/pyproject.toml", "src/app.py"}
    pieces = split(section("tests/test_merge.py") + section("src/app.py"), head=head)
    assert [(p.group, p.files) for p in pieces] == [("src", ["src/app.py"]), ("tests", ["tests/test_merge.py"])]


def test_a_pair_too_large_for_one_piece_puts_the_module_first():
    pieces = split(section("tests/test_big.py", 20_000) + section("big.py", 40_000))
    assert files_of(pieces) == [["big.py"], ["tests/test_big.py"]]


# --- the size budget ----------------------------------------------------------------------------------------


def test_a_group_within_the_budget_is_one_piece():
    (piece,) = split(section("a.py", 10_000) + section("b.py", 20_000))
    assert piece.chars == 30_000


def test_the_budget_is_inclusive():
    budget = new("chunks", "PIECE_MAX_CHARS")
    assert budget == 50_000
    assert len(split(section("a.py", 25_000) + section("b.py", 25_000))) == 1
    assert files_of(split(section("a.py", 25_000) + section("b.py", 25_001))) == [["a.py"], ["b.py"]]


def test_a_group_over_the_budget_splits_at_file_boundaries():
    diff = "".join(section(f"m{i}.py", 40_000) for i in range(3))
    pieces = split(diff)
    assert files_of(pieces) == [["m0.py"], ["m1.py"], ["m2.py"]]
    assert all(p.chars <= 50_000 for p in pieces)


def test_an_oversized_file_is_alone_in_its_piece_and_not_truncated():
    big = section("big.py", 80_000)
    diff = section("a.py", 1_000) + big + section("z.py", 1_000)
    pieces = split(diff)
    assert files_of(pieces) == [["a.py"], ["big.py"], ["z.py"]]
    assert pieces[1].chars == 80_000
    assert new("chunks", "piece_diff_text")(diff, pieces[1]) == big


def test_every_file_is_in_exactly_one_piece_with_all_its_hunks():
    sections = {f"g{g}/f{i}.py": section(f"g{g}/f{i}.py", 7_000 + 1_000 * i) for g in range(3) for i in range(8)}
    diff = "".join(sections.values())
    head = {f"g{g}/pyproject.toml" for g in range(3)} | set(sections)
    pieces = split(diff, head=head)
    listed = [f for p in pieces for f in p.files]
    assert sorted(listed) == sorted(sections)
    piece_diff_text = new("chunks", "piece_diff_text")
    for p in pieces:
        text = piece_diff_text(diff, p)
        assert len(text) == p.chars <= 50_000
        assert text == "".join(sections[f] for f in p.files)


def test_there_is_no_cap_on_the_number_of_pieces():
    head = {f"p{i:02d}/pyproject.toml" for i in range(20)}
    diff = "".join(section(f"p{i:02d}/m.py") for i in range(20)) + section("CHANGELOG.md")
    pieces = split(diff, head=head)
    assert len(pieces) == 21 and pieces[-1].meta
    expected = [f"p{i:02d}/m.py" for i in range(20)] + ["CHANGELOG.md"]
    assert sorted(f for p in pieces for f in p.files) == sorted(expected)


# --- meta files and lockfiles -----------------------------------------------------------------------------


def test_metadata_files_from_anywhere_share_one_meta_piece_placed_last():
    meta = ["plugins/a/CHANGELOG.md", "plugins/b/.claude-plugin/plugin.json", ".github/workflows/ci.yml",
            ".claude-plugin/marketplace.json", "VERSION", "pkg/version.txt", ".gitignore", ".gitattributes",
            "LICENSE-MIT"]
    diff = "".join(section(f) for f in meta) + section("plugins/a/x.py")
    pieces = split(diff, head={"plugins/a/SKILL.md"})
    assert [(p.meta, p.group) for p in pieces] == [(False, "plugins/a"), (True, "meta")]
    assert pieces[0].files == ["plugins/a/x.py"]
    assert sorted(pieces[1].files) == sorted(meta)


def test_no_metadata_change_means_no_meta_piece():
    assert not any(p.meta for p in split(section("src/a.py") + section("tests/test_a.py")))


def test_metadata_over_the_budget_splits_into_meta_pieces():
    diff = section("a/CHANGELOG.md", 30_000) + section("b/CHANGELOG.md", 30_000) + section("src.py")
    pieces = split(diff)
    assert [(p.meta, p.files) for p in pieces] == [
        (False, ["src.py"]), (True, ["a/CHANGELOG.md"]), (True, ["b/CHANGELOG.md"])]


def test_lockfile_text_is_left_out_and_listed_as_omitted():
    diff = section("uv.lock", 200_000) + section("src/app.py", 5_000) + section("web/package-lock.json", 9_000)
    pieces = split(diff)
    app, meta = pieces
    assert (app.files, app.chars) == (["src/app.py"], 5_000)
    assert meta.meta and meta.omitted == ["uv.lock", "web/package-lock.json"] and meta.chars == 0
    piece_diff_text = new("chunks", "piece_diff_text")
    assert all("uv.lock" not in piece_diff_text(diff, p) for p in pieces)
    assert new("chunks", "LOCKFILES") >= {"uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json",
                                          "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum"}


def test_a_lockfile_only_diff_is_one_meta_piece():
    (piece,) = split(section("package-lock.json", 3_000))
    assert (piece.id, piece.meta, piece.files, piece.omitted, piece.chars) == (1, True, [], ["package-lock.json"], 0)
    assert new("chunks", "piece_diff_text")(section("package-lock.json", 3_000), piece) == ""


# --- order, ids and edge cases --------------------------------------------------------------------------


def test_the_split_is_deterministic_with_contiguous_ids_in_group_order():
    head = {"b/pyproject.toml", "a/pyproject.toml"}
    diff = (section("b/z.py", 40_000) + section("CHANGELOG.md") + section("a/y.py") + section("root.py")
            + section("b/c.py", 40_000))
    first, second = split(diff, head=head), split(diff, head=head)
    assert first == second
    assert [p.id for p in first] == [1, 2, 3, 4, 5]
    assert [(p.group, p.files) for p in first] == [
        ("", ["root.py"]), ("a", ["a/y.py"]), ("b", ["b/c.py"]), ("b", ["b/z.py"]), ("meta", ["CHANGELOG.md"])]


def test_an_empty_diff_has_no_pieces():
    assert split("") == []


def test_a_binary_file_is_listed_in_its_piece_and_counts_zero():
    diff = section("skills/x/a.py", 1_000) + binary("skills/x/logo.png")
    (piece,) = split(diff, head={"skills/x/SKILL.md"})
    assert piece.files == ["skills/x/a.py", "skills/x/logo.png"]
    assert piece.binary == ["skills/x/logo.png"] and piece.chars == 1_000


# --- where pieces come from: preparation, resume, --plan-only ---------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


@pytest.fixture
def change_repo(tmp_path) -> tuple[Path, str, str]:
    """base -> head: a skill's script and SKILL.md change, a package module is deleted with its
    pyproject.toml, a root file and uv.lock change."""
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, {"skills/s/SKILL.md": "# s\n", "skills/s/run.py": "a = 1\n", "gone/pyproject.toml": "[p]\n",
                 "gone/mod.py": "x = 1\n", "root.py": "r = 1\n", "uv.lock": "version = 1\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    write(repo, {"skills/s/SKILL.md": "# s\nmore\n", "skills/s/run.py": "a = 2\n", "root.py": "r = 2\n",
                 "uv.lock": "version = 2\n"})
    (repo / "gone/pyproject.toml").unlink()
    (repo / "gone/mod.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def sections_of(diff: str) -> dict[str, int]:
    """Each file's section length in *diff*, by the path after `b/` in its header."""
    out: dict[str, int] = {}
    for part in diff.split("diff --git ")[1:]:
        out[part.split("\n", 1)[0].rsplit(" b/", 1)[-1]] = len("diff --git " + part)
    return out


def prepared(repo: Path, out: Path, **spec):
    from review_pipeline import orchestrator
    state, target_dir = orchestrator.prepare(orchestrator.TargetSpec(repo=str(repo), **spec), out)
    return state, target_dir


def test_preparation_stores_a_change_reviews_pieces_from_both_sides_layout(change_repo, tmp_path):
    repo, base, head = change_repo
    state, _ = prepared(repo, tmp_path / "out", range=f"{base}..{head}")
    assert [(p.id, p.group, p.meta, p.files, p.omitted) for p in state.pieces] == [
        (1, "", False, ["root.py"], []),
        (2, "gone", False, ["gone/mod.py", "gone/pyproject.toml"], []),
        (3, "skills/s", False, ["skills/s/SKILL.md", "skills/s/run.py"], []),
        (4, "meta", True, [], ["uv.lock"]),
    ]
    sizes = sections_of(Path(state.target.diff_path).read_text(encoding="utf-8"))
    assert state.pieces[2].chars == sizes["skills/s/SKILL.md"] + sizes["skills/s/run.py"]


def test_a_tree_review_computes_no_pieces(change_repo, tmp_path):
    repo, _, _ = change_repo
    state, _ = prepared(repo, tmp_path / "out", tree="HEAD")
    assert state.pieces is None
    assert ReviewState.model_validate(state.model_dump(mode="json")).pieces is None


def test_an_old_checkpoint_without_pieces_still_loads(change_repo, tmp_path):
    repo, base, head = change_repo
    state, _ = prepared(repo, tmp_path / "out", range=f"{base}..{head}")
    data = state.model_dump(mode="json")
    data.pop("pieces")
    assert ReviewState.model_validate(data).pieces is None


async def _no_finding(prompt, schema, *, budget_label="", **kwargs):
    sdk.require_trusted_repo()
    outputs = {"find": FinderOutput, "merge": MergeOutput, "assess": AssessOutput, "repeats": RepeatsOutput}
    return outputs[budget_label.split(":")[0]]()


def test_resume_reuses_the_stored_pieces_without_splitting_again(change_repo, tmp_path, monkeypatch):
    from review_pipeline import budget, chunks, orchestrator
    repo, base, head = change_repo
    out = tmp_path / "out"
    calls = {"n": 0}

    async def stop_in_find(prompt, schema, *, budget_label="", **kwargs):
        calls["n"] += 1
        raise budget.BudgetExceededError("stopped during find")

    monkeypatch.setattr(sdk, "agent_call_structured", stop_in_find)
    args = ["--session-id", "s", "--out", str(out), "review", "--repo", str(repo), "--range", f"{base}..{head}",
            "--trust-repo"]
    out.mkdir()
    (out / "review.yaml").write_text("concurrency: 1\n")
    assert main(args) == 4
    (state_file,) = [p for p in out.rglob("review-state.json") if "rounds" not in p.parts]
    stored = ReviewState.model_validate_json(state_file.read_text()).pieces
    assert stored and calls["n"] >= 1

    def no_split(*a, **k):
        raise AssertionError("resume split the diff again")

    monkeypatch.setattr(chunks, "split_pieces", no_split)
    monkeypatch.setattr(orchestrator, "split_pieces", no_split, raising=False)
    monkeypatch.setattr(sdk, "agent_call_structured", _no_finding)
    assert main(["--session-id", "s", "resume", str(state_file.parent), "--trust-repo"]) == 0
    assert ReviewState.model_validate_json(state_file.read_text()).pieces == stored


def plan(repo: Path, out: Path, *args: str, capsys) -> str:
    out.mkdir(exist_ok=True)
    assert main(["--out", str(out), "review", "--repo", str(repo), *args, "--plan-only"]) == 0
    return capsys.readouterr().out


def test_plan_only_lists_each_piece_and_the_piece_and_path_calls(change_repo, tmp_path, capsys):
    repo, base, head = change_repo
    text = plan(repo, tmp_path / "out", "--range", f"{base}..{head}", capsys=capsys)
    lines = text.splitlines()
    pieces = [line for line in lines if line.startswith("piece ") and not line.startswith("piece and")]
    assert len(pieces) == 4, text
    sizes = sections_of(git(repo, "diff", f"{base}..{head}"))
    skill = sizes["skills/s/SKILL.md"] + sizes["skills/s/run.py"]
    assert pieces[2] == f"piece 3: 2 files, {skill} characters [skills/s] skills/s/SKILL.md, skills/s/run.py"
    assert pieces[3] == "piece 4: 0 files, 0 characters [meta] omitted: uv.lock"
    assert pieces[0].startswith("piece 1: 1 file, ") and pieces[0].endswith(" [.] root.py")
    assert ("piece and path finder calls: 4 pieces + up to 21 paths (each one report call and one convert call)"
            in lines)
    # The dimension total is unchanged: the pieces are not counted in it.
    (total,) = [line for line in lines if line.startswith("groups: ")]
    assert "finder calls: 6 " in total + " "


def test_plan_only_on_a_tree_lists_no_pieces(change_repo, tmp_path, capsys):
    repo, _, _ = change_repo
    text = plan(repo, tmp_path / "out", "--tree", capsys=capsys)
    assert not any(line.startswith("piece") for line in text.splitlines())


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
