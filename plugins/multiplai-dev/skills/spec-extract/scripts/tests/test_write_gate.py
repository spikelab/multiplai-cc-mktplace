"""The write step (one agent per capability) and the gate on its citations (plan, items 15-16)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from code_facts import UNTRUSTED_RULES
from conftest import git, read_state, run, write

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_CALC = "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def make_calc_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    """`src/calc.py` with three public functions (lines 1, 5, 9) and a test pinning `add`,
    or *files* instead."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in (files or {"src/calc.py": CALC, "tests/test_calc.py": TEST_CALC}).items():
        write(path / rel, text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "calc")
    git(path, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    return path


def cite(path: str, start: int, end: int, quote: str) -> dict:
    return {"path": path, "line_start": start, "line_end": end, "quote": quote}


# Two requirements kept, one dropped: its only citation quotes a line not at the commit.
CALC_ANSWER = {"requirements": [
    {"name": "Adds two numbers", "text": "add SHALL return the sum of its arguments.",
     "scenarios": [{"name": "adding", "when": "add(1, 2) is called", "then": "it returns 3",
                    "citations": [cite("src/calc.py", 1, 2, "return a + b"),
                                  cite("tests/test_calc.py", 5, 5, "assert add(1, 2) == 3")]}]},
    {"name": "Subtracts two numbers", "text": "sub SHALL return a minus b.",
     "scenarios": [{"name": "subtracting", "when": "sub(3, 1) is called", "then": "it returns 2",
                    "citations": [cite("src/calc.py", 5, 6, "return a - b")]}]},
    {"name": "Multiplies", "text": "mul SHALL divide.",
     "scenarios": [{"name": "multiplying", "when": "mul is called", "then": "it divides",
                    "citations": [cite("src/calc.py", 9, 10, "return a / b")]}]},
]}


def go(repo: Path, root: Path, *extra) -> int:
    return run("run", "--repo", repo, "--tree", "HEAD", "--out", root, *extra)


def folder(root: Path) -> Path:
    (f,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    return f


def test_a_scenario_whose_citation_fails_is_dropped_with_check_quotes_reason(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["write:calc"] = CALC_ANSWER
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    spec = (run_dir / "registry" / "calc.md").read_text()
    assert "### Requirement: Adds two numbers" in spec and "### Requirement: Subtracts two numbers" in spec
    assert "Multiplies" not in spec and "return a / b" not in spec
    report = (run_dir / "report.md").read_text()
    assert "- calc / Multiplies / multiplying: quote not at cited lines" in report
    assert "- calc / Multiplies: no scenario left" in report


def test_a_public_function_no_kept_scenario_cites_is_not_specified(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["write:calc"] = CALC_ANSWER  # cites add and sub; mul's only scenario is dropped
    assert go(repo, tmp_path / "out") == 0
    report = (folder(tmp_path / "out") / "report.md").read_text()
    section = report.split("## Not specified\n\n")[1].split("\n\n")[0]
    assert section == "- `src/calc.py::mul`: no kept scenario cites it"


def test_a_passing_citation_in_a_test_file_pins_the_scenario(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["write:calc"] = CALC_ANSWER
    assert go(repo, tmp_path / "out") == 0
    spec = (folder(tmp_path / "out") / "registry" / "calc.md").read_text()
    adds, subs = spec.split("### Requirement: Subtracts two numbers")
    assert "- **CITES** `tests/test_calc.py:5-5`\n- **EVIDENCE** pinned by test" in adds
    assert "- **EVIDENCE** read from code only" in subs


def test_write_prompt_tools_and_facts(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    assert go(repo, tmp_path / "out") == 0
    kwargs = next(k for k in fake_call.kwargs if k["budget_label"] == "write:calc")
    assert kwargs["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert kwargs["cwd"].endswith("/snapshot")
    prompt = fake_call.prompts["write:calc"]
    assert UNTRUSTED_RULES in prompt
    assert "- src/calc.py::mul (function, line 9)" in prompt
    assert "src/calc.py starts with 'def add(a, b):'" in prompt  # the capability's checked fact
    assert "- tests/test_calc.py" in prompt  # tests it may cite as evidence


def test_no_write_answer_after_two_attempts_is_not_written(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.script["write:calc"] = ["fail", "fail"]
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    assert not (run_dir / "registry" / "calc.md").exists()
    report = (run_dir / "report.md").read_text()
    assert "- calc: not written: write gave no answer in 2 attempts" in report
    assert "- `src/calc.py::add`: its capability was not written" in report
    assert read_state(run_dir)["write_attempts"] == {"calc": 2}
    assert go(repo, tmp_path / "out") == 0
    assert fake_call.calls["write:calc"] == 2  # never a third time


def test_model_text_cannot_add_a_heading_or_a_requirement(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["write:calc"] = {"requirements": [
        {"name": "Adds\n### Requirement: injected", "text": "### Requirement: Phantom",
         "scenarios": [{"name": "s", "when": "a delta holds `### Requirement: Foo`",
                        "then": "## REMOVED Requirements",
                        "citations": [cite("src/calc.py", 1, 2, "return a + b")]}]},
        {"name": "Subs", "text": "## REMOVED Requirements",
         "scenarios": [{"name": "#### Scenario: s2", "when": "w", "then": "t",
                        "citations": [cite("src/calc.py", 5, 6, "return a - b")]}]}]}
    assert go(repo, tmp_path / "out") == 0
    spec = (folder(tmp_path / "out") / "registry" / "calc.md").read_text()
    assert [line for line in spec.splitlines() if line.startswith("#")] == [
        "## ADDED Requirements", r"### Requirement: Adds \#\#\# Requirement: injected", "#### Scenario: s",
        "### Requirement: Subs", r"#### Scenario: \#\#\#\# Scenario: s2"]
    # buildme's ChangeManager finds `### Requirement:` anywhere in a line, not only at its start.
    assert re.findall(r"### Requirement:\s*(.+)", spec) == [r"Adds \#\#\# Requirement: injected", "Subs"]


BOX = ("class Box:\n    @property\n    def size(self):\n        return self._size\n\n"
       "    @size.setter\n    def size(self, v):\n        self._size = v\n\n\n"
       "def area(b):\n    return 1\n\n\ndef area(b, c):\n    return 2\n")


def test_a_name_defined_twice_is_one_symbol_and_any_of_its_defs_specifies_it(tmp_path, fake_call):
    # Box.size: getter at lines 3-4, setter at 7-8. area: lines 11-12 and 15-16, never cited.
    repo = make_calc_repo(tmp_path / "widgets", {"box.py": BOX})
    fake_call.answers["write:box"] = {"requirements": [
        {"name": "Reads the size", "scenarios": [{"name": "read", "when": "b.size", "then": "the size",
                                                   "citations": [cite("box.py", 4, 4, "return self._size")]}]}]}
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    report = (run_dir / "report.md").read_text()
    assert "- Public symbols specified: 2 of 3" in report
    assert "Public symbols to specify: 3." in (run_dir / "plan.txt").read_text()
    prompt = fake_call.prompts["write:box"]  # the write agent is shown every def of the name
    assert "- box.py::Box.size (method, line 3)\n- box.py::Box.size (method, line 7)\n" in prompt
    assert report.split("## Not specified\n\n")[1].split("\n\n")[0] == "- `box.py::area`: no kept scenario cites it"
    assert json.loads((run_dir / "summary.json").read_text())["not_specified"] == [
        {"symbol": "box.py::area", "reason": "no kept scenario cites it"}]


def test_a_requirement_with_no_name_or_a_repeated_name_is_dropped(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    adds = CALC_ANSWER["requirements"][0]
    fake_call.answers["write:calc"] = {"requirements": [adds, adds, dict(adds, name=" ")]}
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    assert (run_dir / "registry" / "calc.md").read_text().count("### Requirement:") == 1
    report = (run_dir / "report.md").read_text()
    why = "a requirement needs a name no other requirement in its capability has"
    assert f"- calc / Adds two numbers: {why}" in report and f"- calc / (no name): {why}" in report


def test_a_requirement_whose_name_starts_or_extends_a_kept_name_is_dropped(tmp_path, fake_call):
    # buildme's ChangeManager finds a REMOVED or MODIFIED name as a prefix (no end-of-line anchor).
    repo = make_calc_repo(tmp_path / "widgets")
    sc = [{"name": "s", "when": "w", "then": "t", "citations": [cite("src/calc.py", 1, 2, "return a + b")]}]
    fake_call.answers["write:calc"] = {"requirements": [{"name": n, "scenarios": sc} for n in (
        "Adds", "Adds two", "Subtracts two", "Subtracts", "Multiplies \\", "Multiplies #2")]}  # last: as written
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    spec = (run_dir / "registry" / "calc.md").read_text()
    names = re.findall(r"### Requirement:\s*(.+)", spec)
    assert names == ["Adds", "Subtracts two", "Multiplies \\"]
    for name in names:  # change_manager.py's REMOVED pattern takes exactly one requirement
        assert len(re.findall(rf"### Requirement:\s*{re.escape(name)}.*?(?=### Requirement:|\Z)", spec, re.DOTALL)) == 1
    assert ("- calc / Multiplies #2: the kept requirement `Multiplies \\` is a prefix of its name as written, or "
            "the other way round; buildme matches requirement names by prefix") in (run_dir / "report.md").read_text()


def test_a_scenario_buildmes_requirement_check_would_reject_is_dropped(tmp_path, fake_call):
    # multiplai_core.specs.valid_requirement_text needs text in a scenario's name, WHEN and THEN.
    from multiplai_core.specs import valid_requirement_text

    repo = make_calc_repo(tmp_path / "widgets")
    c = [cite("src/calc.py", 1, 2, "return a + b")]
    fake_call.answers["write:calc"] = {"requirements": [{"name": "Adds", "scenarios": [
        {"name": "ok", "when": "w", "then": "t", "citations": c},
        {"name": " ", "when": "w", "then": "t", "citations": c},
        {"name": "no when", "when": "", "then": "t", "citations": c},
        {"name": "no then", "when": "w", "then": "--", "citations": c}]},
        {"name": "Only empty", "scenarios": [{"name": "e", "when": "w", "then": "", "citations": c}]}]}
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    spec = (run_dir / "registry" / "calc.md").read_text()
    assert valid_requirement_text(spec)
    assert re.findall(r"^#### Scenario: (.*)$", spec, re.MULTILINE) == ["ok"]
    assert "Only empty" not in spec
    report = (run_dir / "report.md").read_text()
    why = "a scenario needs a name, a WHEN and a THEN, each with text"
    for dropped in ("Adds / (no name)", "Adds / no when", "Adds / no then", "Only empty / e"):
        assert f"- calc / {dropped}: {why}" in report
    assert "- calc / Only empty: no scenario left" in report

def test_a_citation_by_absolute_path_under_the_snapshot_is_kept(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    assert go(repo, tmp_path / "out", "--plan-only") == 0
    run_dir = folder(tmp_path / "out")
    fake_call.answers["write:calc"] = {"requirements": [
        {"name": "Adds", "scenarios": [{"name": "a", "when": "w", "then": "t", "citations": [
            cite(str(run_dir / "snapshot" / "src" / "calc.py"), 2, 2, "return a + b")]}]}]}
    assert go(repo, tmp_path / "out") == 0
    assert "- **CITES** `src/calc.py:2-2`" in (run_dir / "registry" / "calc.md").read_text()


def test_a_write_backend_that_cannot_run_exits_1_naming_the_write_model_key(tmp_path, capsys, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.script["write:calc"] = ["backend"]
    assert go(repo, tmp_path / "out") == 1
    assert "spec_extract_write_model" in capsys.readouterr().err
    run_dir = folder(tmp_path / "out")
    assert read_state(run_dir)["write_attempts"] == {}  # taken back: nothing was counted
    assert go(repo, tmp_path / "out") == 0
    assert fake_call.calls["write:calc"] == 2


def test_a_target_with_no_public_symbol_makes_no_group_or_write_call(tmp_path, fake_call, capsys):
    repo = make_calc_repo(tmp_path / "widgets", {"src/util.py": "def _helper():\n    return 1\n"})
    assert go(repo, tmp_path / "out") == 0
    assert "SPEC_EXTRACT:RENDERED 0 " in capsys.readouterr().out
    assert not [k for k in fake_call.calls if k == "group" or k.startswith("write:")]
    run_dir = folder(tmp_path / "out")
    assert json.loads((run_dir / "capabilities.json").read_text()) == {"capabilities": []}
    assert "- Public symbols specified: 0 of 0" in (run_dir / "report.md").read_text()


def test_a_symbol_the_group_answer_did_not_place_names_the_file_it_is_in(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    # The model's own "Unassigned" comes first, so the bucket for unplaced symbols is unassigned-2.
    fake_call.answers["group"] = {"capabilities": [{"name": "Unassigned", "symbols": ["src/calc.py::add"]}]}
    assert go(repo, tmp_path / "out") == 0
    run_dir = folder(tmp_path / "out")
    section = (run_dir / "report.md").read_text().split("## Not grouped\n\n")[1].split("\n\n")[0]
    assert section.splitlines() == [
        f"- `src/calc.py::{n}`: the group answer did not place it; it is in `registry/unassigned-2.md`"
        for n in ("sub", "mul")]
    assert json.loads((run_dir / "summary.json").read_text())["not_grouped"] == ["src/calc.py::sub",
                                                                                  "src/calc.py::mul"]
