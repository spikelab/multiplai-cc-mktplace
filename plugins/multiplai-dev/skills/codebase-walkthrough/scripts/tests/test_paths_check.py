"""`check` on path hops: each hop link re-read at the commit (or HEAD), failures named by hop id, Markdown only."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import fixture_repo
import pytest
from code_facts import E2EListing, PathsResult, TracedPath
from fakes import FakeAgents
from test_paths_render import CHARGE, COVERED, _entry, _hop, _render, _result, _state
from walkthrough_pipeline import check, sdk
from walkthrough_pipeline.__main__ import main

NONE = E2EListing(e2e=[], mocks_hop_2=[])
# Hop 2 of path p2 calls into later.py; hop 3 is `charge`.
LATER = _hop("cli/later.py", 1, 2, "def later():", "later")
SYNC = _hop("cli/main.py", 6, 8, "def sync():", "sync")


def _path(n: int, hops, name="sync") -> TracedPath:
    return TracedPath(id=f"p{n}", entry=_entry("click", name, "cli/main.py", 6), hops=hops, stops_at=None,
                      break_reason=None, changed_lines_covered=COVERED, tests=NONE)


def _paths(*paths: TracedPath) -> PathsResult:
    return _result().model_copy(update={"paths": list(paths)})


def _doc(tmp_path: Path, result: PathsResult | None, *, extra: str = "", error: str | None = None) -> Path:
    """A walkthrough whose Paths section renders `result`, with one plain link so it is never empty."""
    state = _state(tmp_path, result)
    if error:
        state = state.model_copy(update={"paths_error": error})
    out = tmp_path / "out"
    body = _render(state, out)
    sha, repo = state.target.head_sha, state.repos["shop"].path
    md = out / "billing-walkthrough.md"
    md.write_text("# billing — codebase walkthrough\n\n## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n"
                  f"| shop | {repo} | `{sha}` |\n\n## Overview\n\nThe export command is declared at "
                  '[`shop/cli/tool.py:10`](x "parser.add_parser(\'export\')").\n\n'
                  f"## Paths\n\n{body}\n{extra}")
    return md


def _check(md: Path, capsys, *args: str) -> tuple[int, list[str], str, str]:
    capsys.readouterr()
    code = main(["check", str(md), *args])
    out, err = capsys.readouterr()
    return code, [line for line in out.splitlines() if line.startswith("FAIL ")], out, err


def _repo(md: Path) -> Path:
    return Path(re.search(r"^\| shop \| (\S+) \|", md.read_text(), re.MULTILINE).group(1))


def _three_hop_doc(tmp_path: Path) -> Path:
    return _doc(tmp_path, _paths(_path(1, [SYNC, CHARGE]), _path(2, [SYNC, LATER, CHARGE], "later")))


# --- passing sections ---------------------------------------------------------------------------------


def test_an_unchanged_paths_section_passes_and_counts_its_hops_on_stderr(tmp_path, capsys):
    md = _doc(tmp_path, _paths(_path(1, [SYNC, CHARGE]), _path(2, [LATER], "later")))
    code, fails, out, err = _check(md, capsys)
    assert code == 0 and fails == []
    assert re.search(r"^check: \d+ checked, 0 failed$", out, re.MULTILINE)
    assert "check: 3 hops checked" in err
    code, fails, _, err = _check(md, capsys, "--at-head")
    assert code == 0 and "check: 3 hops checked" in err


def test_a_21_path_section_reports_its_anchor_count(tmp_path, capsys):
    md = _doc(tmp_path, _paths(*(_path(n, [SYNC, CHARGE]) for n in range(1, 22))))
    anchors = len(check.HOP_ANCHOR.findall(md.read_text()))
    assert anchors == 42
    code, _, _, err = _check(md, capsys)
    assert code == 0 and f"check: {anchors} hops checked" in err


# --- failing hops -----------------------------------------------------------------------------------------


def test_an_edited_line_fails_its_hop_by_id(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    later = _repo(md) / "cli" / "later.py"
    later.write_text("# moved\n# down\n" + later.read_text())
    fixture_repo.commit(_repo(md), "move later")
    code, fails, out, _ = _check(md, capsys, "--at-head")
    assert code == 1
    assert fails == ["FAIL p2h2 shop/cli/later.py:1-2: quote not at cited lines"]
    assert re.search(r"^check: \d+ checked, 1 failed$", out, re.MULTILINE)


def test_a_moved_hop_three_is_named(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    core = _repo(md) / "billing" / "core.py"
    core.write_text("# the\n# rates\n" + core.read_text())
    fixture_repo.commit(_repo(md), "move charge")
    code, fails, _, _ = _check(md, capsys, "--at-head")
    assert code == 1
    assert "FAIL p2h3 shop/billing/core.py:4-5: quote not at cited lines" in fails
    assert "FAIL p1h2 shop/billing/core.py:4-5: quote not at cited lines" in fails


def test_a_deleted_file_names_the_hop(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    (_repo(md) / "cli" / "main.py").unlink()
    fixture_repo.commit(_repo(md), "drop the cli")
    code, fails, _, _ = _check(md, capsys, "--at-head")
    assert code == 1
    assert any(f.startswith("FAIL p1h1 shop/cli/main.py:6-8: ") for f in fails)
    assert not any("later.py" in f or "core.py" in f for f in fails)


def test_a_line_past_the_end_of_the_file_fails(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    md.write_text(md.read_text().replace("`shop/cli/later.py:1-2`](", "`shop/cli/later.py:90-91`](", 1))
    code, fails, _, _ = _check(md, capsys)
    assert code == 1 and fails == ["FAIL p2h2 shop/cli/later.py:90-91: quote not at cited lines"]


def test_one_bad_hop_among_many_is_the_only_one_reported(tmp_path, capsys):
    md = _doc(tmp_path, _paths(*(_path(n, [SYNC, CHARGE]) for n in range(1, 8))))
    text = md.read_text()
    anchor = '<a id="p5h2"></a>`p5h2` [`shop/billing/core.py:4-5`]('
    assert anchor in text
    md.write_text(text.replace(anchor, '<a id="p5h2"></a>`p5h2` [`shop/billing/core.py:1-2`]('))
    code, fails, _, _ = _check(md, capsys)
    assert code == 1 and fails == ["FAIL p5h2 shop/billing/core.py:1-2: quote not at cited lines"]


# --- citations of hops ----------------------------------------------------------------------------------


def test_an_unresolved_hop_citation_fails_alongside_a_resolved_one(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    md.write_text(md.read_text() + "\nSee hop [p4h1](#p4h1) and hop [p1h2](#p1h2).\n")
    assert check.unresolved_hop_citations(md.read_text()) == ["p4h1"]
    code, fails, out, _ = _check(md, capsys)
    assert code == 1 and fails == ["FAIL unresolved hop citation p4h1"]
    assert re.search(r"^check: \d+ checked, 1 failed$", out, re.MULTILINE)


def test_a_dangling_backticked_hop_id_outside_the_paths_section_fails(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    md.write_text(md.read_text() + "\n## Gotchas\n\nThe break is at `p4h1`, after `p1h2`; see also [p4h1](#p4h1)"
                  " and `p5h2`.\n")
    assert check.unresolved_hop_citations(md.read_text()) == ["p4h1", "p5h2"]
    code, fails, _, _ = _check(md, capsys)
    assert code == 1 and fails == ["FAIL unresolved hop citation p4h1", "FAIL unresolved hop citation p5h2"]


def test_a_hop_id_inside_a_snippet_or_a_link_quote_is_not_a_citation(tmp_path):
    text = ('<!-- snippet shop/cli/main.py:1-1 -->\n[`shop/cli/main.py:1`](x "q = `p9h9`")\n\n'
            "```python\nname = `p8h8`\n```\n")
    assert check.unresolved_hop_citations(text) == []


# --- what check does not need -----------------------------------------------------------------------------


def test_a_missing_or_corrupted_paths_file_changes_nothing(tmp_path, capsys):
    md = _three_hop_doc(tmp_path)
    first = _check(md, capsys, "--at-head")
    beside = md.with_name("billing-paths.json")
    beside.write_text("{ not json")
    assert _check(md, capsys, "--at-head") == first
    beside.unlink()
    assert _check(md, capsys, "--at-head") == first


def test_a_fresh_clone_with_no_run_dir_and_no_model_checks_hops(tmp_path, capsys, monkeypatch):
    md = _three_hop_doc(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(_repo(md)), str(clone)], check=True, capture_output=True)

    async def no_model(*a, **kw):
        raise AssertionError("check called a model")

    monkeypatch.setattr(sdk, "agent_call_structured", no_model)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, fails, _, err = _check(md, capsys, "--at-head", "--repo", f"shop={clone}")
    assert code == 0 and fails == [] and "check: 5 hops checked" in err


@pytest.mark.parametrize("result, error, lead", [
    (_result().model_copy(update={"paths": []}), None, "No entry points reaching this module were found."),
    (None, "RuntimeError: no index", "Path finding failed: RuntimeError: no index"),
])
def test_a_paths_section_with_no_hops_passes(tmp_path, capsys, result, error, lead):
    md = _doc(tmp_path, result, error=error)
    assert lead in md.read_text()
    code, fails, _, err = _check(md, capsys)
    assert code == 0 and fails == []
    assert "hops checked" not in err


LEGACY = ("\n## End-to-end scenarios\n\n### 1. A command runs\n\n"
          '1. `sync` — the command [`shop/cli/main.py:6-8`](x "def sync():")\n'
          '2. `charge` — the rate [`shop/billing/core.py:4-5`](x "def charge(amount):")\n')


def test_a_legacy_scenarios_walkthrough_passes_and_fails_as_before(tmp_path, capsys):
    md = _doc(tmp_path, None)
    md.write_text(md.read_text().split("\n## Paths")[0] + LEGACY)
    code, fails, _, err = _check(md, capsys)
    assert code == 0 and "hops checked" not in err
    core = _repo(md) / "billing" / "core.py"
    core.write_text("# the\n# rates\n" + core.read_text())
    fixture_repo.commit(_repo(md), "move charge")
    code, fails, _, _ = _check(md, capsys, "--at-head")
    assert code == 1 and fails == ["FAIL shop/billing/core.py:4-5: quote not at cited lines"]


def test_a_scenario_hop_and_a_path_hop_on_the_same_lines_get_the_same_verdict(tmp_path, capsys):
    md = _doc(tmp_path, _paths(_path(1, [SYNC, CHARGE])), extra=LEGACY)
    assert _check(md, capsys)[0] == 0
    core = _repo(md) / "billing" / "core.py"
    core.write_text("# the\n# rates\n" + core.read_text())
    fixture_repo.commit(_repo(md), "move charge")
    _, fails, _, _ = _check(md, capsys, "--at-head")
    assert sorted(fails) == ["FAIL p1h2 shop/billing/core.py:4-5: quote not at cited lines",
                             "FAIL shop/billing/core.py:4-5: quote not at cited lines"]


def test_an_agent_mode_walkthrough_has_no_hop_line(ws, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    assert main(["run", str(ws["engine"] / "bookings"), "--trust-repo", "--runs-dir", str(tmp_path / "runs"),
                 "--output", str(tmp_path / "out"), "--mode", "agent", "--lsp-timeout", "0"]) == 0
    code, fails, out, err = _check(tmp_path / "out" / "bookings-walkthrough.md", capsys)
    assert code == 0 and fails == [] and "hops checked" not in err
    assert re.search(r"^check: \d+ checked, 0 failed$", out, re.MULTILINE)


# --- the suite catches a regression ------------------------------------------------------------------------


def _moved_hop_is_named(tmp_path: Path, capsys) -> bool:
    md = _three_hop_doc(tmp_path)
    later = _repo(md) / "cli" / "later.py"
    later.write_text("# moved\n# down\n" + later.read_text())
    fixture_repo.commit(_repo(md), "move later")
    _, fails, _, _ = _check(md, capsys, "--at-head")
    return fails == ["FAIL p2h2 shop/cli/later.py:1-2: quote not at cited lines"]


def test_disabling_hop_anchors_makes_the_moved_hop_test_fail(tmp_path, capsys, monkeypatch):
    assert _moved_hop_is_named(tmp_path / "a", capsys)
    monkeypatch.setattr(check, "HOP_ANCHOR", re.compile(r"(?!)"))
    assert not _moved_hop_is_named(tmp_path / "b", capsys)

