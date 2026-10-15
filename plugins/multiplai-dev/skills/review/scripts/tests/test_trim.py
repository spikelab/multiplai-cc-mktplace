"""The trim finder: tests and comments a change adds that add little, in an advisory Trim section."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import SCHEMA, high_finding
from review_pipeline import budget, post, sdk
from review_pipeline.__main__ import main
from review_pipeline.config import DIMENSIONS, TRIM_DIMENSION, ReviewConfig, load_config
from review_pipeline.export import to_findings_file
from review_pipeline.models import (AssessOutput, Citation, Finding, FinderOutput, MergeOutput, RepeatsOutput,
                                    ReviewState, Verdict, section_of)
from review_pipeline.prompts import find as find_prompt
from review_pipeline.prompts import verify as verify_prompt
from review_pipeline.render import render_review, section
from review_pipeline.stages import RunContext
from review_pipeline.stages.assess import run_assess
from review_pipeline.stages.find import critical_paths, finder_dimensions
from review_pipeline.stages.merge import linked
from review_pipeline.stages.verify import run_verify
from review_pipeline import target as target_mod

ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

COMMENT = [f"    # line {i} of a comment that says what the next line does" for i in range(1, 11)]
BASE_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "billing/charge.py": "def charge(cents):\n    return max(cents, 0)\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "tests/test_charge.py": "from billing.charge import charge\n",
    "coding-standards.md": "# Rules\n\n## Critical paths\n\n- `billing/*`\n\n## Other\n\n- `docs/*`\n",
}
HEAD_FILES = {
    **BASE_FILES,
    "calc.py": BASE_FILES["calc.py"] + "\n\ndef double(x):\n" + "\n".join(COMMENT) + "\n    return x * 2\n",
    "tests/test_calc.py": BASE_FILES["tests/test_calc.py"] + "\n\ndef test_add_again():\n    assert add(1, 2) == 3\n",
    "tests/test_charge.py": BASE_FILES["tests/test_charge.py"]
    + "\n\ndef test_negative_is_zero():\n    assert charge(-5) == 0\n",
}
# Line numbers at head.
ADD_AGAIN_LINE = 8     # tests/test_calc.py: def test_add_again():
TEST_ADD_LINE = 4      # tests/test_calc.py: def test_add():
COMMENT_LINE = 6       # calc.py: the first comment line inside double()
NEGATIVE_LINE = 4      # tests/test_charge.py: def test_negative_is_zero():


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def trim_repo(tmp_path) -> tuple[Path, str, str]:
    """A change that adds a test duplicating another, a ten-line comment over one line of code, and a
    test of a path listed under `## Critical paths`."""
    repo = tmp_path / "calc"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, BASE_FILES, "base")
    head = _commit(repo, HEAD_FILES, "add double() and more tests")
    return repo, base, head


def cite(path: str, line: int, quote: str) -> Citation:
    return Citation(path=path, line_start=line, line_end=line, quote=quote)


def merge_test_finding() -> Finding:
    return Finding(
        claim="Merge test_add_again into test_add (test_add already checks the same behaviour)",
        severity="LOW", file="tests/test_calc.py", line_start=ADD_AGAIN_LINE, line_end=ADD_AGAIN_LINE + 1,
        failure_scenario="nothing: test_add catches every change test_add_again catches",
        citations=[cite("tests/test_calc.py", ADD_AGAIN_LINE, "def test_add_again():"),
                   cite("tests/test_calc.py", TEST_ADD_LINE, "def test_add():"),
                   cite("calc.py", 2, "return a + b")])


def comment_finding() -> Finding:
    return Finding(
        claim="Shorten the comment at calc.py:6 (10 lines over 1 line of code)",
        severity="MEDIUM", file="calc.py", line_start=COMMENT_LINE, line_end=COMMENT_LINE + 9,
        failure_scenario="nothing breaks; a reader reads ten lines to learn what one line does",
        citations=[cite("calc.py", COMMENT_LINE, COMMENT[0].strip())])


def critical_test_finding() -> Finding:
    return Finding(
        claim="Remove test_negative_is_zero (it checks what max() already guarantees)",
        severity="LOW", file="tests/test_charge.py", line_start=NEGATIVE_LINE, line_end=NEGATIVE_LINE + 1,
        failure_scenario="nothing: max(cents, 0) cannot return a negative",
        citations=[cite("tests/test_charge.py", NEGATIVE_LINE, "def test_negative_is_zero():"),
                   cite("billing/charge.py", 2, "return max(cents, 0)")])


def trimmed(finding: Finding) -> Finding:
    """*finding* as the find stage stores a trim finder's result."""
    return finding.with_location(dimension=TRIM_DIMENSION, finder=TRIM_DIMENSION, finders=[TRIM_DIMENSION])


def confirmed(topic: str, impact: str, citation: Citation) -> Verdict:
    return Verdict(status="confirmed", topic=topic, impact=impact, reason="read both tests",
                   citations=[citation], expected_behaviour="One test per behaviour.")


class TrimAgents:
    """Canned answers by stage label. The trim finder returns *trim*; every other finder nothing."""

    def __init__(self, trim: list[Finding] | None = None, verdicts: dict[str, Verdict] | None = None,
                 stop_on_trim_call: int | None = None):
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.trim = trim if trim is not None else [merge_test_finding(), comment_finding()]
        self.verdicts = verdicts or {}
        self.stop_on_trim_call = stop_on_trim_call
        self.trim_calls = 0

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        self.calls.append(budget_label)
        self.prompts.append((budget_label, prompt))
        stage = budget_label.split(":")[0]
        if budget_label == f"find:{TRIM_DIMENSION}":
            self.trim_calls += 1
            if self.trim_calls == self.stop_on_trim_call:
                self.stop_on_trim_call = None
                raise budget.BudgetExceededError("circuit breaker stopped the run during find:trim")
            return FinderOutput(findings=self.trim)
        if stage == "find":
            return FinderOutput()
        if stage == "verify":
            for claim, verdict in self.verdicts.items():
                if claim in prompt:
                    return verdict
            if "test_add_again" in prompt:
                return confirmed("tests", "correctness-only", cite("tests/test_calc.py", TEST_ADD_LINE,
                                                                     "def test_add():"))
            return confirmed("docs", "hygiene", cite("calc.py", COMMENT_LINE, COMMENT[0].strip()))
        if stage == "merge":
            return MergeOutput()
        if stage == "assess":
            return AssessOutput()
        if stage == "repeats":
            return RepeatsOutput()
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> TrimAgents:
        fake = TrimAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def _review(repo: Path, base: str, head: str, out: Path, *extra: str) -> int:
    return main(["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo),
                 "--range", f"{base}..{head}", "--trust-repo", *extra])


def _target_dir(out: Path) -> Path:
    (path,) = list(out.rglob("findings.json"))
    return path.parent


# --- Done means 2: a change review lists trim findings under Trim, LOW, never posted ---------------


def test_a_change_review_lists_trim_findings_in_their_own_advisory_section(trim_repo, tmp_path, agents):
    import jsonschema

    repo, base, head = trim_repo
    out = tmp_path / "out"
    fake = agents()
    assert _review(repo, base, head, out) == 0
    assert fake.calls.count(f"find:{TRIM_DIMENSION}") == 1
    target_dir = _target_dir(out)
    data = json.loads((target_dir / "findings.json").read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    rows = {f["claim"]: f for f in data["findings"]}
    for claim in (merge_test_finding().claim, comment_finding().claim):
        assert rows[claim]["section"] == "trim"
        assert rows[claim]["severity"] == "LOW"  # the comment finding came back MEDIUM
    text = next(target_dir.glob("review-*.md")).read_text()
    trim_part = text.split("## Trim (advisory: never blocks a merge)", 1)[1].split("## Appendix", 1)[0]
    assert merge_test_finding().claim in trim_part and comment_finding().claim in trim_part
    assert "## Code" not in text
    assert "Trim 0 HIGH, 0 MEDIUM, 2 LOW" in text
    assert post.select(data["findings"], None) == []
    # Accepted in a decisions file, a trim finding is posted.
    accepted = {rows[merge_test_finding().claim]["id"]: {"decision": "accept"}}
    assert [f["claim"] for f in post.select(data["findings"], accepted)] == [merge_test_finding().claim]


def test_review_yaml_trim_false_runs_no_trim_finder(trim_repo, tmp_path, agents):
    repo, base, head = trim_repo
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.yaml").write_text("trim: false\n")
    fake = agents()
    assert _review(repo, base, head, out) == 0
    assert f"find:{TRIM_DIMENSION}" not in fake.calls
    data = json.loads((_target_dir(out) / "findings.json").read_text())
    assert data["findings"] == []


def test_load_config_reads_trim(tmp_path):
    assert load_config(tmp_path).trim is True
    (tmp_path / "review.yaml").write_text("trim: no\n")
    assert load_config(tmp_path).trim is False


# --- Done means 3: an important test is never suggested for removal --------------------------------


async def test_a_trim_finding_on_an_important_test_is_labelled_important_and_not_listed(trim_repo, tmp_path,
                                                                                       monkeypatch):
    repo, base, head = trim_repo
    info = target_mod.build_target(target_mod.resolve(repo, range_=f"{base}..{head}"))
    merge, critical, comment = trimmed(merge_test_finding()), trimmed(critical_test_finding()), trimmed(
        comment_finding())
    state = ReviewState(target=info, findings=[merge, critical, comment])
    answers = {
        merge.id: confirmed("tests", "breaks-users", cite("tests/test_calc.py", TEST_ADD_LINE, "def test_add():")),
        critical.id: confirmed("tests", "correctness-only",
                               cite("billing/charge.py", 2, "return max(cents, 0)")),
        comment.id: confirmed("docs", "hygiene", cite("calc.py", COMMENT_LINE, COMMENT[0].strip())),
    }

    async def verifier(prompt, schema, **kwargs):
        return next(v for fid, v in answers.items() if
                    next(f for f in state.findings if f.id == fid).claim in prompt)

    monkeypatch.setattr(sdk, "agent_call_structured", verifier)
    snapshot = target_mod.snapshot_head(info, tmp_path / "snap")
    state = await run_verify(state, RunContext(config=ReviewConfig(concurrency=1), snapshot=snapshot, diff=""))
    # A trim finding's breaks-* impact is what removing the test risks: it is kept, not lowered.
    assert state.verdicts[merge.id].impact == "breaks-users"
    assert state.assessments[merge.id].label == "important"
    assert "breaks-users" in state.assessments[merge.id].reason
    assert state.assessments[critical.id].label == "important"
    assert "billing/*" in state.assessments[critical.id].reason
    assert comment.id not in state.assessments
    assert all(state.verdicts[f.id].status == "confirmed" for f in state.findings)  # status stays

    text = render_review(state)
    listed_part, appendix = text.split("## Appendix", 1)
    assert comment.claim in listed_part
    assert merge.claim not in listed_part and critical.claim not in listed_part
    assert merge.claim in appendix and critical.claim in appendix
    assert "**important**" in appendix
    # The label survives the assess stage, which shows the agent no trim finding.
    state = await run_assess(state, RunContext(config=ReviewConfig(), snapshot=snapshot, diff=""))
    assert state.assessments[merge.id].label == "important"
    assert state.assessments[critical.id].label == "important"
    assert comment.id not in state.assessments


def test_critical_paths_reads_one_backticked_glob_per_bullet_under_the_heading(trim_repo):
    repo, base, head = trim_repo
    info = target_mod.build_target(target_mod.resolve(repo, range_=f"{base}..{head}"))
    assert critical_paths(info, ["tests/test_charge.py"]) == [("billing/*", "coding-standards.md")]


# --- Done means 5: trim findings stay apart from the others -----------------------------------------


def test_the_merge_stage_never_links_a_trim_finding_with_a_non_trim_one():
    code = high_finding()
    trim = trimmed(code.with_location(claim="Shorten the comment over the keyword"))
    assert linked(code, code.with_location(claim="another finding on the same line"))
    assert not linked(code, trim)
    assert not linked(trim, code)
    assert linked(trim, trimmed(code.with_location(claim="Remove the comment over the keyword")))


async def test_the_assess_agent_is_never_shown_a_trim_finding(target_info, tmp_path, monkeypatch):
    code = high_finding()
    other = code.with_location(claim="a second code finding", line_start=1, line_end=1)
    trim = trimmed(code.with_location(claim="Shorten the comment over the keyword"))
    state = ReviewState(target=target_info, stage="repeats", findings=[code, other, trim],
                        verdicts={f.id: confirmed("code", "hygiene", f.citations[0]) for f in (code, other, trim)})
    prompts: list[str] = []

    async def assess_agent(prompt, schema, **kwargs):
        prompts.append(prompt)
        return AssessOutput()

    monkeypatch.setattr(sdk, "agent_call_structured", assess_agent)
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    state = await run_assess(state, RunContext(config=ReviewConfig(), snapshot=snapshot, diff=""))
    assert len(prompts) == 1
    assert code.id in prompts[0] and trim.id not in prompts[0] and trim.claim not in prompts[0]
    assert trim.id not in state.assessments


# --- sections ----------------------------------------------------------------------------------------


def test_section_of_files_a_finding_only_trim_reported_under_trim():
    assert section_of("code", [TRIM_DIMENSION]) == "trim"
    assert section_of("tests", [TRIM_DIMENSION, TRIM_DIMENSION]) == "trim"
    assert section_of("code", ["diff-bugs", TRIM_DIMENSION]) == "code"
    assert section_of("tests", ["tests"]) == "tests"
    assert section_of(None) == "code"


def test_findings_json_carries_the_section_and_old_files_fall_back_to_the_topic(canned_state):
    trim = trimmed(comment_finding())
    canned_state.findings.append(trim)
    canned_state.verdicts[trim.id] = confirmed("code", "hygiene", trim.citations[0]).model_copy(
        update={"finding_id": trim.id})
    rows = to_findings_file(canned_state)["findings"]
    assert all("section" in r for r in rows)
    by_id = {r["id"]: r for r in rows}
    assert by_id[trim.id]["section"] == "trim" and section(by_id[trim.id]) == "trim"
    old = {k: v for k, v in by_id[trim.id].items() if k != "section"}
    assert section(old) == "code"  # a file written before 0.71 has no section


# --- verify --------------------------------------------------------------------------------------------


def test_the_verifier_gets_the_trim_rules_only_for_a_trim_finding(target_info):
    assert verify_prompt.TRIM_RULES in verify_prompt.build(target_info, trimmed(high_finding()))
    assert verify_prompt.TRIM_RULES not in verify_prompt.build(target_info, high_finding())


def test_the_trim_finder_has_change_and_tree_wording():
    assert "adds or edits" in find_prompt.DIMENSION_TASKS[TRIM_DIMENSION]
    assert TRIM_DIMENSION in find_prompt.DIMENSION_TASKS_TREE
    assert TRIM_DIMENSION not in DIMENSIONS


# --- finders: which reviews run trim -------------------------------------------------------------------


def test_finder_dimensions_runs_trim_on_a_change_by_default_and_on_a_tree_only_when_asked(target_info):
    tree = target_info.model_copy(update={"kind": "tree"})
    assert finder_dimensions(target_info, ReviewConfig())[-1] == TRIM_DIMENSION
    assert TRIM_DIMENSION not in finder_dimensions(target_info, ReviewConfig(trim=False))
    assert TRIM_DIMENSION not in finder_dimensions(tree, ReviewConfig())
    assert finder_dimensions(tree, ReviewConfig(), tree_trim=True)[-1] == TRIM_DIMENSION


def test_trim_goes_with_tree_or_dir(trim_repo, tmp_path, capsys):
    repo, base, head = trim_repo
    assert _review(repo, base, head, tmp_path / "out", "--trim") == 2
    assert "--trim goes with --tree or --dir" in capsys.readouterr().err


# --- row S4: a tree review with --trim, stopped and resumed without the flag -----------------------------


def _tree(repo: Path, out: Path, *extra: str) -> int:
    out.mkdir(exist_ok=True)
    (out / "review.yaml").write_text("concurrency: 1\n")  # one finder at a time: the stop point is exact
    return main(["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo), "--tree",
                 "--trust-repo", *extra])


def test_a_stopped_tree_review_with_trim_resumes_only_the_missing_trim_keys(trim_repo, tmp_path, agents):
    repo, _, _ = trim_repo
    never_stopped = agents(trim=[])
    assert _tree(repo, tmp_path / "whole", "--trim") == 0
    whole_dir = _target_dir(tmp_path / "whole")
    whole = ReviewState.model_validate_json((whole_dir / "review-state.json").read_text())
    groups = sorted(k for k in whole.finder_results if k.startswith(f"{TRIM_DIMENSION}@"))
    assert len(groups) >= 2 and never_stopped.calls.count(f"find:{TRIM_DIMENSION}") == len(groups)

    fake = agents(trim=[], stop_on_trim_call=2)  # trim@0 returns, the run stops on trim@1
    assert _tree(repo, tmp_path / "out", "--trim") == 4
    target_dir = _target_dir_of_state(tmp_path / "out")
    saved = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert saved.trim is True
    assert f"{TRIM_DIMENSION}@0" in saved.finder_results and f"{TRIM_DIMENSION}@1" not in saved.finder_results
    missing = [k for k in groups if k not in saved.finder_results]
    assert missing == [f"{TRIM_DIMENSION}@1"]  # which later finders also finished depends on the task group
    first_run = len(fake.calls)
    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    trim_prompts = [p for label, p in fake.prompts[first_run:] if label == f"find:{TRIM_DIMENSION}"]
    assert len(trim_prompts) == len(missing)
    # Group 0 holds the files directly in the repository, group 1 billing/: only group 1 is asked again.
    assert "- billing/charge.py" in trim_prompts[0] and "- calc.py" not in trim_prompts[0]
    done = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert done.trim is True
    assert sorted(done.finder_results) == sorted(whole.finder_results)
    assert [f.id for f in done.findings] == [f.id for f in whole.findings]


def _target_dir_of_state(out: Path) -> Path:
    (path,) = list(out.rglob("review-state.json"))
    return path.parent
