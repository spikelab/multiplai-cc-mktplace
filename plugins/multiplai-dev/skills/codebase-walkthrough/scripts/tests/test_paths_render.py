"""The Paths body, rendered by code from a `code_facts.PathsResult`: lead, entry, hops, stop and e2e lines.

The result is built by hand from the real paths.v1 models, over a committed
git fixture repo, so every hop link points at real lines.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

import fixture_repo
from code_facts import E2EListing, E2ETest, Location, MockedE2ETest, PathEntry, PathHop, PathsResult, TracedPath
from code_facts.paths import EntryKind, FallbackStatus
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.models import RepoInfo, TargetInfo, WalkState

ANCHOR = re.compile(r'<a id="(p\d+h\d+)"></a>')
HOP_ID = re.compile(r"p\d+h\d+")

TOOL = "\n".join(["# pad"] * 9 + ["parser.add_parser('export')"] + ["# pad"] * 9
                 + ["def export(args):", "    rows = load()", "    return write(rows)"]) + "\n"
FILES = {
    "cli/main.py": "import click\n\nfrom billing.core import charge\n\n\n@click.command()\ndef sync():\n"
                   "    return charge(1)\n",
    "cli/tool.py": TOOL,
    "cli/later.py": "def later():\n    return 1\n",
    "billing/__init__.py": "",
    "billing/core.py": "RATE = 2\n\n\ndef charge(amount):\n    return amount * RATE\n",
}


def _entry(kind, name, path, line):
    return PathEntry(kind=kind, name=name, location=Location(path=path, line=line), handler=None, invocation=None,
                     invoked_by=f"a person or a script running {name}", also_invoked_by=[])


def _hop(path, a, b, quote, called="f"):
    return PathHop(symbol_called=called, path=path, line_start=a, line_end=b, quote=quote)


def _e2e(node, cls=E2ETest, **extra):
    file = node.split("::")[0]
    return cls(node_id=node, file=file, line=3, match="cli_runner", invocation_line=4, **extra)


CHARGE = _hop("billing/core.py", 4, 5, "def charge(amount):", "charge")
COVERED = [Location(path="billing/core.py", line=4)]


def _result() -> PathsResult:
    p1 = TracedPath(  # hop 1 holds the entry line: the entry bullet points at p1h1
        id="p1", entry=_entry("click", "sync", "cli/main.py", 6),
        hops=[_hop("cli/main.py", 6, 8, "def sync():", "sync"), CHARGE],
        stops_at=None, break_reason=None, changed_lines_covered=COVERED,
        tests=E2EListing(e2e=[_e2e("tests/e2e/test_sync.py::test_sync"),
                              _e2e("tests/e2e/test_sync.py::test_sync_dry")], mocks_hop_2=[]))
    p2 = TracedPath(  # hop 1 is elsewhere: the entry bullet links the declaration
        id="p2", entry=_entry("argparse", "export", "cli/tool.py", 10),
        hops=[_hop("cli/tool.py", 20, 22, "def export(args):", "export"), CHARGE],
        stops_at=2, break_reason="unconnected_hop", changed_lines_covered=COVERED,
        tests=E2EListing(e2e=[], mocks_hop_2=[
            _e2e("tests/e2e/test_export.py::test_offline", MockedE2ETest,
                 patch_file="tests/e2e/conftest.py", patch_line=7)]))
    p3 = TracedPath(
        id="p3", entry=_entry("main", "later", "cli/later.py", 1),
        hops=[_hop("cli/later.py", 1, 2, "def later():", "later")],
        stops_at=None, break_reason=None, changed_lines_covered=[],
        tests=E2EListing(e2e=[], mocks_hop_2=[]))
    return PathsResult(schema_version="paths.v1", commit="c0ffee", scoped=True, cap=21,
                       fallback=FallbackStatus(status="ok", reason=None), paths=[p1, p2, p3], dropped=[],
                       diagnostics=[], test_listing_skips=[])


def _state(tmp_path: Path, result: PathsResult | None) -> WalkState:
    repo = tmp_path / "ws" / "shop"
    sha = fixture_repo.make_repo(repo, FILES)
    target = TargetInfo(repo_key="shop", repo_path=str(repo), subpath="billing", head_sha=sha, name="billing",
                        package="billing", files=["billing/__init__.py", "billing/core.py"],
                        file_lines={"billing/__init__.py": 0, "billing/core.py": 5})
    return WalkState(target=target, repos={"shop": RepoInfo(key="shop", path=str(repo), head_sha=sha)},
                     stage="trace", paths=result)


def _render(state: WalkState, out_dir: Path) -> str:
    from walkthrough_pipeline.render import Linker, render_paths_section
    out_dir.mkdir(parents=True, exist_ok=True)
    return render_paths_section(state, Linker(state, out_dir))


def _lines(body: str) -> list[str]:
    return [line.strip() for line in body.splitlines() if line.strip()]


def _blocks(body: str) -> dict[str, list[str]]:
    """Lines per path, keyed by `p{n}`, from each `**p{n}** — ` heading to the next."""
    out: dict[str, list[str]] = {}
    current = None
    for line in _lines(body):
        m = re.match(r"\*\*(p\d+)\*\* — ", line)
        if m:
            current = m.group(1)
        if current:
            out.setdefault(current, []).append(line)
    return out


def test_paths_body_lists_each_path_with_entry_hops_stop_and_tests(tmp_path):
    body = _render(_state(tmp_path, _result()), tmp_path / "out")
    lines = _lines(body)
    assert lines[0] == "3 paths reach this module."
    blocks = _blocks(body)
    assert list(blocks) == ["p1", "p2", "p3"]
    assert blocks["p1"][0] == "**p1** — `sync` (click command)"
    assert blocks["p2"][0] == "**p2** — `export` (argparse command)"

    entry1 = next(line for line in blocks["p1"] if line.startswith("- Entry: invoked by "))
    assert re.fullmatch(r"- Entry: invoked by `a person or a script running sync`,? at `p1h1`", entry1)
    entry2 = next(line for line in blocks["p2"] if line.startswith("- Entry: invoked by "))
    assert entry2.startswith("- Entry: invoked by `a person or a script running export`")
    assert re.search(r'\[`shop/cli/tool\.py:10`\]\(\S+ "[^"]*"\)$', entry2)

    hop = re.search(r'<a id="p1h1"></a>`p1h1` \[`shop/cli/main\.py:6-8`\]\((\S+) "def sync\(\):"\)', body)
    assert hop and hop.group(1).endswith("cli/main.py#L6-L8")
    assert re.search(r'<a id="p2h2"></a>`p2h2` \[`shop/billing/core\.py:4-5`\]\(\S+ "def charge\(amount\):"\)',
                     body)
    assert ANCHOR.findall(body) == ["p1h1", "p1h2", "p2h1", "p2h2", "p3h1"]

    assert "- Ends at `p1h2` (trace complete)." in blocks["p1"]
    assert "- Stops after `p2h2`: `unconnected_hop`" in blocks["p2"]
    assert "- Ends at `p3h1` (trace complete)." in blocks["p3"]


def test_e2e_lines_count_e2e_tests_and_label_mocked_ones_not_counted(tmp_path):
    blocks = _blocks(_render(_state(tmp_path, _result()), tmp_path / "out"))
    p1 = "\n".join(blocks["p1"])
    assert "`tests/e2e/test_sync.py::test_sync`" in p1 and "`tests/e2e/test_sync.py::test_sync_dry`" in p1
    assert "no e2e test" not in p1 and "not counted" not in p1
    # A mocked test does not count: the path still says it has no e2e test.
    mocked = next(line for line in blocks["p2"] if "no e2e test" in line)
    assert "not counted: `tests/e2e/test_export.py::test_offline` (mocks `p2h2`, not counted)" in mocked
    assert any("no e2e test" in line for line in blocks["p3"])


def test_an_unknown_kind_renders_verbatim(tmp_path):
    result = _result()
    p3 = result.paths[2]
    odd = p3.model_copy(update={"entry": p3.entry.model_copy(update={"kind": "future_kind"})})
    result = result.model_copy(update={"paths": [*result.paths[:2], odd]})
    blocks = _blocks(_render(_state(tmp_path, result), tmp_path / "out"))
    assert blocks["p3"][0] == "**p3** — `later` (future_kind)"


def test_kind_labels_cover_only_paths_v1_kinds():
    from walkthrough_pipeline.render import KIND_LABELS
    assert set(get_args(EntryKind)) >= {"click", "skill_command", "model"}    # the paths.v1 kind values
    assert set(KIND_LABELS) <= set(get_args(EntryKind))
    assert KIND_LABELS["click"] == "click command"
    assert KIND_LABELS["skill_command"] == "skill command"


def test_the_invoker_is_always_present(tmp_path):
    # paths.v1 requires `invoked_by`, so the "unknown invoker" case cannot reach the renderer.
    for bad in ({}, {"invoked_by": ""}):
        with pytest.raises(ValidationError):
            PathEntry(kind="click", name="x", location=Location(path="a.py", line=1), handler=None,
                      invocation=None, also_invoked_by=[], **bad)
    body = _render(_state(tmp_path, _result()), tmp_path / "out")
    entries = [line for line in _lines(body) if line.startswith("- Entry: invoked by ")]
    assert len(entries) == 3
    assert all(re.match(r"- Entry: invoked by `[^`\s][^`]*`", line) for line in entries)


def test_two_renders_of_one_result_assign_the_same_hop_ids(tmp_path):
    state = _state(tmp_path, _result())
    first = _render(state, tmp_path / "out")
    again = _render(state.model_copy(deep=True), tmp_path / "out")
    assert first == again
    assert HOP_ID.findall(first)


def test_no_entries_and_no_drops_says_none_were_found(tmp_path):
    empty = _result().model_copy(update={"paths": []})
    body = _render(_state(tmp_path, empty), tmp_path / "out")
    assert _lines(body)[0] == "No entry points reaching this module were found."
    assert not HOP_ID.search(body)


def test_coverage_counts_paths_traced_and_cut_short(tmp_path):
    from walkthrough_pipeline.render import coverage_lines
    table = "\n".join(coverage_lines(_state(tmp_path, _result())))
    assert re.search(r"^\| Paths traced \| 3\b", table, re.M)
    assert re.search(r"^\| Paths cut short \| 1\b", table, re.M)
    assert "Scenarios cut short" not in table


def test_every_link_in_the_paths_body_re_reads_at_the_commit(tmp_path, capsys):
    # Five hop links and the one entry link of p2 (p1's and p3's entry lines sit inside their hop 1).
    state = _state(tmp_path, _result())
    out = tmp_path / "out"
    body = _render(state, out)
    md = out / "x-walkthrough.md"
    md.write_text("# x\n\n## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n"
                  f"| shop | {state.repos['shop'].path} | `{state.target.head_sha}` |\n\n## Paths\n\n{body}")
    capsys.readouterr()
    assert main(["check", str(md)]) == 0
    assert "check: 6 checked, 0 failed" in capsys.readouterr().out
