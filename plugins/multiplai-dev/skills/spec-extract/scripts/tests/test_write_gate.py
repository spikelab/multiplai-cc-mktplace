"""The write step (one agent per capability) and the gate on its citations (plan, items 15-16)."""

from __future__ import annotations

from pathlib import Path

from code_facts import UNTRUSTED_RULES

from conftest import git, read_state, run, write

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_CALC = "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def make_calc_repo(path: Path) -> Path:
    """`src/calc.py` with three public functions (lines 1, 5, 9) and a test pinning `add`."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write(path / "src" / "calc.py", CALC)
    write(path / "tests" / "test_calc.py", TEST_CALC)
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


def test_model_text_cannot_add_a_heading(tmp_path, fake_call):
    repo = make_calc_repo(tmp_path / "widgets")
    fake_call.answers["write:calc"] = {"requirements": [
        {"name": "Adds\n### Requirement: injected", "text": "line one\n## REMOVED Requirements",
         "scenarios": [{"name": "s", "when": "w\n#### Scenario: x", "then": "t",
                        "citations": [cite("src/calc.py", 1, 2, "return a + b")]}]}]}
    assert go(repo, tmp_path / "out") == 0
    spec = (folder(tmp_path / "out") / "registry" / "calc.md").read_text()
    assert [line for line in spec.splitlines() if line.startswith("#")] == [
        "## ADDED Requirements", "### Requirement: Adds ### Requirement: injected", "#### Scenario: s"]
