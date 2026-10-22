"""The scope step: Python files only, review's skip rules, ast symbols, tests as evidence,
`--pr` scoped from a fixed base, and the refusals."""

from __future__ import annotations

from conftest import commit_file, git, make_repo, read_state, run, write
from spec_extract_pipeline import scope as scope_mod
from spec_extract_pipeline.target import Target

API = '''\
"""An API module."""
import os


def charge(amount):
    return amount


async def refund(amount):
    return -amount


def _helper():
    return 1


class Ledger:
    def add(self, x):
        return x

    async def flush(self):
        return None

    def _secret(self):
        return 2


class _Hidden:
    def visible_but_hidden_class(self):
        return 3
'''


def _tree(repo, path: str = "") -> Target:
    return Target(
        kind="tree", repo=str(repo), head_sha=git(repo, "rev-parse", "HEAD"), path=path
    )


def test_python_files_symbols_and_skips(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    write(repo / "pkg" / "api.py", API)
    write(
        repo / "pkg" / "tests" / "test_api.py", "def test_charge():\n    assert True\n"
    )
    write(repo / "pkg" / "api_test.py", "def test_more():\n    pass\n")
    write(repo / "pkg" / "test_top.py", "def helper_in_test():\n    pass\n")
    write(repo / "pkg" / "bad.py", "def broken(:\n    pass\n")
    (repo / "pkg" / "blob.py").write_bytes(b"x = 1\n\0\0binary\n")
    write(repo / "pkg" / "big.py", "# " + "x" * 200_001 + "\n")
    write(repo / "pkg" / "gen.py", "def generated():\n    pass\n")
    write(repo / "pkg" / "vendored.py", "def vendored():\n    pass\n")
    write(
        repo / ".gitattributes",
        "pkg/gen.py linguist-generated\npkg/vendored.py linguist-vendored=true\n",
    )
    write(repo / "README.md", "# not python\n")
    (repo / "pkg" / "link.py").symlink_to("api.py")
    git(repo, "add", "-A")
    sub = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},pkg/sub.py")
    git(repo, "commit", "-q", "-m", "more")

    s = scope_mod.compute(_tree(repo))

    assert s.files == [
        "pkg/api.py",
        "pkg/api_test.py",
        "pkg/test_top.py",
        "pkg/tests/test_api.py",
        "src/a/one.py",
        "src/billing/charge.py",
        "src_a/two.py",
    ]
    assert [(x.path, x.reason) for x in s.skipped] == [
        ("pkg/big.py", "over 200000 characters"),
        ("pkg/blob.py", "binary"),
        ("pkg/gen.py", "linguist-generated is set"),
        ("pkg/link.py", "symbolic link"),
        ("pkg/sub.py", "submodule"),
        ("pkg/vendored.py", "linguist-vendored is set"),
    ]
    assert [x.path for x in s.unparsed] == ["pkg/bad.py"]
    assert (
        s.unparsed[0].reason.startswith("ast could not parse it:")
        and "(line 1)" in s.unparsed[0].reason
    )
    # Public top-level functions and classes, and public methods of public classes; no test file's.
    assert [
        (x.path, x.name, x.kind, x.line) for x in s.symbols if x.path == "pkg/api.py"
    ] == [
        ("pkg/api.py", "charge", "function", 5),
        ("pkg/api.py", "refund", "function", 9),
        ("pkg/api.py", "Ledger", "class", 17),
        ("pkg/api.py", "Ledger.add", "method", 18),
        ("pkg/api.py", "Ledger.flush", "method", 21),
    ]
    assert not [x for x in s.symbols if scope_mod.is_test_file(x.path)]
    assert {x.path for x in s.symbols} == {
        "pkg/api.py",
        "src/a/one.py",
        "src/billing/charge.py",
        "src_a/two.py",
    }
    # Units come from code_facts.partition over the files, in its order.
    assert [f for u in s.units for f in u.files] == s.files


def test_is_test_file():
    assert scope_mod.is_test_file("test_x.py")
    assert scope_mod.is_test_file("a/b_test.py")
    assert scope_mod.is_test_file("tests/helpers.py")
    assert scope_mod.is_test_file("pkg/tests/sub/conftest.py")
    assert not scope_mod.is_test_file("pkg/testing.py")
    assert not scope_mod.is_test_file("tests.py")


def test_path_limits_the_scope(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    s = scope_mod.compute(_tree(repo, "src/a"))
    assert s.files == ["src/a/one.py"]
    assert [x.name for x in s.symbols] == ["one"]


def test_pr_scope_from_a_fixed_base(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    write(repo / "keep.py", "def keep():\n    pass\n")
    write(repo / "gone.py", "def gone():\n    pass\n")
    write(
        repo / "old_name.py",
        "def moved():\n    return 'a long enough body to be a rename'\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    write(
        repo / "src" / "billing" / "charge.py",
        "def charge(amount):\n    return amount * 2\n",
    )
    write(repo / "new.py", "def new():\n    pass\n")
    write(repo / "notes.md", "not python\n")
    git(repo, "rm", "-q", "gone.py")
    git(repo, "mv", "old_name.py", "new_name.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    # The base branch moves on afterwards; the scope reads only the saved base_sha.
    commit_file(repo, "keep.py", "def keep():\n    return 'moved on'\n")

    t = Target(kind="pr", repo=str(repo), head_sha=head, base_sha=base, pr=7)
    s = scope_mod.compute(t)
    # Changed, existing at head, whole files; a rename is a delete plus an add (--no-renames).
    assert s.files == ["new.py", "new_name.py", "src/billing/charge.py"]
    assert {x.name for x in s.symbols} == {"new", "moved", "charge"}


def test_no_python_file_is_refused(tmp_path, capsys):
    repo = tmp_path / "docs"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / "README.md", "# hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 2
    out = capsys.readouterr()
    assert "SPEC_EXTRACT:NO_PYTHON" in out.out
    assert "no Python file" in out.err
    (folder,) = (root / "docs").glob("tree@*")
    assert read_state(folder)["steps_done"] == ["resolve"]
    assert read_state(folder)["scope"] is None


def test_too_many_units_is_refused_naming_the_count(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    body = "".join(f"x{i} = {i}\n" for i in range(3500))
    write(repo / "one" / "a.py", body)
    write(repo / "two" / "b.py", body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "big")
    root = tmp_path / "out"
    assert (
        run("run", "--repo", repo, "--tree", "HEAD", "--out", root, "--max-units", "1")
        == 2
    )
    err = capsys.readouterr().err
    assert "2 explore units" in err and "--max-units 1" in err and "--path" in err
    assert not fake_call.calls
    # The default (20) takes it.
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0


def test_scope_is_saved_and_read_back(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    (folder,) = (root / "widgets").glob("tree@*")
    state = read_state(folder)
    assert state["steps_done"][:2] == ["resolve", "scope"]
    assert state["scope"]["files"] == [
        "src/a/one.py",
        "src/billing/charge.py",
        "src_a/two.py",
    ]
    assert (folder / "plan.txt").is_file()


def test_a_failed_check_attr_warns_and_skips_nothing(tmp_path, caplog):
    """An old git without `check-attr --source`, or one that fails, keeps every file and says so."""
    repo = make_repo(tmp_path / "widgets")
    write(repo / "gen.py", "def generated():\n    pass\n")
    write(repo / ".gitattributes", "gen.py linguist-generated\n")
    commit_file(repo, "gen.py", "def generated():\n    pass\n", "generated")
    with caplog.at_level("WARNING", logger=scope_mod.log.name):
        assert scope_mod._check_attrs(str(repo), "no-such-ref", ["gen.py"]) == {}
    assert "git check-attr --source failed; not skipping generated files" in caplog.text
    assert scope_mod._check_attrs(
        str(repo), git(repo, "rev-parse", "HEAD"), ["gen.py"]
    ) == {"gen.py": "linguist-generated"}


def test_the_size_limit_counts_characters_not_bytes(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    # 300,002 bytes, 150,002 characters: kept. Over 200,000 characters: skipped.
    write(repo / "wide.py", "# " + "é" * 150_000 + "\n")
    commit_file(repo, "long.py", "# " + "é" * 200_000 + "\n", "wide and long")
    s = scope_mod.compute(_tree(repo))
    assert "wide.py" in s.files
    assert [(x.path, x.reason) for x in s.skipped] == [
        ("long.py", "over 200000 characters")
    ]
