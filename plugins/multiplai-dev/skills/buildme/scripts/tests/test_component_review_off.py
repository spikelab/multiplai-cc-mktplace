"""With `code_review.component_review` off (the default), the per-block
review runs exactly as it did before the setting existed.

The test runs the real `_run_integration_and_review` against a real git repo,
through two review rounds, a review fix and a fix on pass, and captures every
prompt the reviewer, the adjudicator and the fix agent are given (the fix
agent's context bundle included), the round records and
`unfixed-findings.md`. It compares them with
`fixtures/component-review-off-golden.json`, which was captured from the
code before the setting existed (main at 845ff594). Temporary paths, commit
ids and timestamps are replaced before the comparison.

Regenerate the golden only on purpose: `REGEN_COMPONENT_REVIEW_GOLDEN=1`.
"""

import json
import os
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    FindingAdjudication,
    PriorAssessment,
    ReviewFinding,
    ReviewResult,
    ReviewScore,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import _run_integration_and_review

GOLDEN = Path(__file__).parent / "fixtures" / "component-review-off-golden.json"

BUGGY = "def add(a, b):\n    return a + b + 1\n"
FIXED = "def add(a, b):\n    return a + b\n"


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _finding(severity: str, claim: str) -> ReviewFinding:
    return ReviewFinding(
        claim=claim,
        severity=severity,
        confidence=0.9,
        file_path="app.py",
        line=2,
        evidence="returns a + b + 1",
    )


def _review(score: int, findings, prior=()) -> ReviewResult:
    return ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=score, evidence="e")],
        findings=list(findings),
        prior_assessments=list(prior),
    )


def _project(base: Path, settings: dict | None):
    project_dir = base / "project"
    change = project_dir / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (project_dir / "app.py").write_text(BUGGY)
    (change / "rubric.md").write_text("# Rubric\n\n- Q (weight 2): correct\n")
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-qm", "init")
    baseline = _git(project_dir, "rev-parse", "HEAD")
    config = BuildConfig(
        project_dir=project_dir,
        change_name="test",
        tier="advanced",
        test_command=f'{sys.executable} -c "import sys; sys.exit(0)"',
        config_dir=base / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = project_dir / "specs"
    for key, value in (settings or {}).items():
        setattr(config, key, value)
    block = BlockInfo(
        number=1,
        name="Adder",
        description="add two numbers",
        status=BlockStatus.IMPLEMENTING,
        baseline_commit=baseline,
    )
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(blocks=[block], baseline_tests_pass=True),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(base / "progress.md")
    progress.initialize("test", "advanced", 1)
    return config, state, block, progress


async def _capture(base: Path, settings: dict | None = None) -> dict:
    """Run one block's review loop and return everything it handed a model
    and wrote, with volatile values replaced."""
    config, state, block, progress = _project(base, settings)
    reviews = [
        _review(
            2,
            [
                _finding("Major", "off-by-one in add()"),
                _finding("Minor", "add() has no docstring"),
                _finding("Note", "add() could be a lambda"),
            ],
        ),
        _review(
            5,
            [_finding("Minor", "the name add is vague")],
            [PriorAssessment(finding_id="B01-R1-F1", status="resolved")],
        ),
        _review(5, []),
    ]
    reviewer_prompts: list[str] = []
    judge_prompts: list[str] = []
    fixer_calls: list[dict] = []

    async def reviewer(prompt, *args, **kwargs):
        reviewer_prompts.append(prompt)
        return reviews[len(reviewer_prompts) - 1]

    async def judge(prompt, *args, **kwargs):
        judge_prompts.append(prompt)
        return FindingAdjudication()

    async def fixer(**kwargs):
        fixer_calls.append({k: v for k, v in kwargs.items() if k != "cwd"})
        (config.project_dir / "app.py").write_text(FIXED)
        return AgentResult(success=True, output="fixed\nSEARCHED:\n- app.py")

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "build_pipeline.llm_steps.review_steps.agent_call_structured",
                new=AsyncMock(side_effect=reviewer),
            )
        )
        stack.enter_context(
            patch(
                "build_pipeline.tdd_engine.llm_call_structured",
                new=AsyncMock(side_effect=judge),
            )
        )
        stack.enter_context(
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new=AsyncMock(side_effect=fixer),
            )
        )
        ok = await _run_integration_and_review(block, config, state, progress)

    records = {}
    for path in sorted((config.change_dir / "reviews").glob("*.json")):
        records[path.name] = json.loads(path.read_text())
    unfixed = config.change_dir / "unfixed-findings.md"
    captured = {
        "ok": ok,
        "status": str(block.status),
        "reviewer_prompts": reviewer_prompts,
        "judge_prompts": judge_prompts,
        "fixer_calls": fixer_calls,
        "records": records,
        "unfixed": unfixed.read_text() if unfixed.exists() else None,
    }
    text = json.dumps(captured, indent=1, sort_keys=True, default=str)
    text = text.replace(str(base), "<TMP>")
    text = re.sub(r"\b[0-9a-f]{40}\b", "<SHA>", text)
    text = re.sub(r"\b[0-9a-f]{7,12}\b", "<SHORT>", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+\+00:00", "<TIME>", text)
    return json.loads(text)


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


@pytest.mark.asyncio
async def test_default_per_block_review_matches_the_golden(tmp_path):
    captured = await _capture(tmp_path)
    if os.environ.get("REGEN_COMPONENT_REVIEW_GOLDEN"):
        GOLDEN.write_text(json.dumps(captured, indent=1, sort_keys=True) + "\n")
    assert captured == json.loads(GOLDEN.read_text())
