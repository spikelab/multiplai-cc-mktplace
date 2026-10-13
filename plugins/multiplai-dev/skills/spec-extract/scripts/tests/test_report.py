"""`report.md`: sections as data, the target and its commit, lists in their order,
no timestamp, written through a temp file."""

from __future__ import annotations

import pytest

from conftest import FIXED, git, read_state, run, write
from spec_extract_pipeline import report as report_mod
from spec_extract_pipeline.scope import Skipped, TargetScope
from spec_extract_pipeline.target import Target

# These tests are about resolve, scope and explore: the run ends after explore, as it did
# before the group and write steps existed.
pytestmark = pytest.mark.usefixtures("stops_after_explore")

SHA = "a" * 40


def test_render_sections_in_order_with_none_for_empty():
    t = Target(kind="tree", repo="/r", head_sha=SHA, path="src/billing")
    text = report_mod.render(t, [report_mod.Section("First", ["- one"]), report_mod.Section("Second")])
    assert text == (f"# spec-extract report\n\nTarget: commit {SHA}, path src/billing\nCommit: {SHA}\n\n"
                    "## First\n\n- one\n\n## Second\n\nNone.\n")


def test_describe_each_kind():
    assert report_mod.describe(Target(kind="tree", repo="/r", head_sha=SHA)) == f"commit {SHA}"
    pr = Target(kind="pr", repo="/r", head_sha=SHA, base_sha="b" * 40, pr=12)
    assert report_mod.describe(pr) == f"PR #12 at head {SHA} (base {'b' * 40})"
    d = Target(kind="dir", repo="/run/source", head_sha=SHA, source="/x/y")
    assert report_mod.describe(d) == f"directory /x/y, imported as commit {SHA}"


def test_scope_sections_order():
    scope = TargetScope(files=[], symbols=[], units=[],
                        skipped=[Skipped(path="a.py", reason="binary"), Skipped(path="b.py", reason="submodule")],
                        unparsed=[Skipped(path="c.py", reason="ast could not parse it: x")])
    skipped, not_analysed = report_mod.scope_sections(scope, ["z.py", "d.py"])
    assert skipped.lines == ["- `a.py`: binary", "- `b.py`: submodule"]
    # Unparsed files first, then the unanswered units' files in the order given (unit, then file).
    assert not_analysed.lines == ["- `c.py`: not analysed: ast could not parse it: x",
                                  "- `z.py`: not analysed: explore gave no answer in 2 attempts",
                                  "- `d.py`: not analysed: explore gave no answer in 2 attempts"]


def test_write_output_replaces_a_leftover_temp_file(tmp_path):
    (tmp_path / "report.md.tmp").write_text("half written")
    (tmp_path / "report.md").write_text("old")
    report_mod.write_output(tmp_path, "report.md", "new\n")
    assert (tmp_path / "report.md").read_text() == "new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.md"]


def test_report_of_a_run_has_no_timestamp_or_spend(tmp_path):
    repo = tmp_path / "widgets"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / "z.py", "def z():\n    pass\n")
    write(repo / "bad.py", "def broken(:\n")
    (repo / "blob.py").write_bytes(b"\0\0")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    (folder,) = (root / "widgets").glob("tree@*")
    text = (folder / "report.md").read_text()
    sha = git(repo, "rev-parse", "HEAD")
    assert f"Commit: {sha}" in text
    assert "- `blob.py`: binary" in text
    assert "- `bad.py`: not analysed: ast could not parse it" in text
    assert FIXED.strftime("%Y") not in text and "$" not in text and "cost" not in text.lower()
    assert read_state(folder)["budget"]["cost_usd"] == 1.0
