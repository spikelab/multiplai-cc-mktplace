"""Rows S14-S16 of the Stop and resume states table in
plan-multiplai-dev-pipeline-viewer-2026-10-28.md (chunk G): buildme's plan is
`implementation-plan.md`, and a build planned as `design.md` before the
rename resumes with no new plan and the same revision.

`fixtures/build-state-pre-implementation-plan.json` is the checkpoint the
version before the rename saved for `CHANGE_FILES` after its plan check
passed. Its `spec_gen.revision` and step revisions are that version's."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from build_pipeline import spec_generator as owner
from build_pipeline.change_manager import (
    OLD_PLAN_FILE,
    PLAN_FILE,
    PLAN_HASH_KEY,
    ChangeManager,
    plan_path,
)
from build_pipeline.config import BuildConfig
from build_pipeline.models import PlanEdit, PlanEdits
from build_pipeline.state import BuildState

FIXTURE = (
    Path(__file__).parent / "fixtures" / "build-state-pre-implementation-plan.json"
)

# The change the fixture's checkpoint was saved for, byte for byte.
CHANGE_FILES = {
    "proposal.md": "# Proposal\n\n## Why\nExport the audit log nightly.\n\n"
    "## Capabilities\n\n### New Capabilities\n- `export`\n",
    "use-cases.md": "# Use cases\n\n## Personas\n- Admin: reads the audit log\n\n"
    "## Use cases\n- Admin wants a nightly export so that they can archive it\n",
    "requirements/export.md": "## ADDED Requirements\n\n### Requirement: Export\n"
    "The system SHALL write the audit log to a file each night.\n\n"
    "#### Scenario: Nightly\n- **WHEN** the night job runs\n"
    "- **THEN** the export file holds every audit row\n",
    "design.md": "# Design\n\n## Context\nThe audit log lives in one table.\n\n"
    "## Decisions\nWrite a CSV with the standard library.\n\n"
    "## Risks / Trade-offs\nNone.\n",
    "unknowns.md": "# Unknowns — what we are about to depend on\n"
    "No dependencies new to this project.\n",
    "tasks.md": "## 1. Export\n\nWrite the audit rows to a CSV file.\n\nSatisfies: export\n",
    "rubric.md": "# Rubric\n\nThe export holds every row.\n",
}
NEW_PLAN = "# Implementation plan\n\n## Decisions\nWrite a CSV with the csv module.\n"


@pytest.fixture
def legacy(tmp_path):
    """The change and checkpoint a build planned before the rename left."""
    config = BuildConfig(
        project_dir=tmp_path, change_name="legacy-plan", skip_explainers=True
    )
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    root = cm.create_change(config.change_name)
    for name, text in CHANGE_FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    shutil.copy(FIXTURE, config.state_file_path())
    return config, cm, BuildState.load(config.state_file_path())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _no_model(monkeypatch):
    provider = AsyncMock(side_effect=AssertionError("resume must not call a model"))
    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", provider
    )
    monkeypatch.setattr(owner, "generate_rubric", provider)
    return provider


def test_the_fixture_is_an_old_name_plan_with_its_hashes():
    saved = json.loads(FIXTURE.read_text())["spec_gen"]
    assert saved["artifact_hashes"][OLD_PLAN_FILE] == _sha(CHANGE_FILES[OLD_PLAN_FILE])
    assert PLAN_FILE not in saved["artifact_hashes"]


@pytest.mark.asyncio
async def test_s14_a_build_in_flight_reads_design_md_with_the_same_revision(
    legacy, monkeypatch
):
    """S14: no regeneration, no stale plan, same revision as before."""
    config, cm, state = legacy
    saved = json.loads(FIXTURE.read_text())["spec_gen"]
    provider = _no_model(monkeypatch)

    await owner._generate_all_artifacts(cm, config.change_dir, config, state)

    provider.assert_not_awaited()
    sg = state.spec_gen
    assert sg.revision == saved["revision"]
    assert sg.artifact_hashes == saved["artifact_hashes"]
    assert sg.stale_artifacts == []
    assert sg.step_passed("plan_review") and sg.step_passed("explainers")
    for name, text in CHANGE_FILES.items():
        assert (config.change_dir / name).read_text() == text
    assert not (config.change_dir / PLAN_FILE).exists()
    assert config.design_path == config.change_dir / OLD_PLAN_FILE
    owner.assert_plan_current(config, state)


def test_s15_with_both_files_the_new_one_is_the_plan_and_the_old_one_is_named_once(
    legacy, caplog
):
    config, _, state = legacy
    (config.change_dir / PLAN_FILE).write_text(NEW_PLAN)
    with caplog.at_level(logging.WARNING):
        assert plan_path(config.change_dir) == config.change_dir / PLAN_FILE
        assert config.design_path == config.change_dir / PLAN_FILE
        owner.reconcile_plan(config, state)
    unused = [r for r in caplog.records if OLD_PLAN_FILE in r.getMessage()]
    assert len(unused) == 1 and "unused" in unused[0].getMessage()
    assert state.spec_gen.artifact_hashes[PLAN_HASH_KEY] == _sha(NEW_PLAN)
    assert (config.change_dir / OLD_PLAN_FILE).read_text() == CHANGE_FILES["design.md"]


@pytest.mark.asyncio
async def test_s16_a_regenerated_old_name_plan_is_written_under_the_new_name(
    legacy, monkeypatch, caplog
):
    """S16: the stale plan is written as implementation-plan.md, design.md
    stays, and the build then behaves as S15."""
    config, cm, state = legacy
    saved = json.loads(FIXTURE.read_text())["spec_gen"]
    (config.change_dir / "proposal.md").write_text(
        CHANGE_FILES["proposal.md"] + "\nEdited by the PM.\n"
    )
    texts = {"design": NEW_PLAN, "tasks": CHANGE_FILES["tasks.md"]}

    async def generate(artifact_id, context, config, **kwargs):
        return texts[artifact_id]

    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", generate
    )
    monkeypatch.setattr(owner, "generate_rubric", AsyncMock(return_value="# Rubric\n"))

    with caplog.at_level(logging.WARNING):
        await owner._generate_all_artifacts(cm, config.change_dir, config, state)

    assert (config.change_dir / PLAN_FILE).read_text() == NEW_PLAN
    assert (config.change_dir / OLD_PLAN_FILE).read_text() == CHANGE_FILES["design.md"]
    assert plan_path(config.change_dir) == config.change_dir / PLAN_FILE
    assert any(
        OLD_PLAN_FILE in r.getMessage() and "unused" in r.getMessage()
        for r in caplog.records
    )
    sg = state.spec_gen
    assert sg.stale_artifacts == []
    assert sg.artifact_hashes[PLAN_HASH_KEY] == _sha(NEW_PLAN)
    assert sg.revision != saved["revision"]


@pytest.mark.asyncio
async def test_a_rewrite_of_an_old_name_plan_publishes_the_new_name(
    legacy, monkeypatch
):
    """G3: the plan check's rewrite edits the plan as read from design.md and
    publishes it as implementation-plan.md; design.md is left in place."""
    from build_pipeline.llm_steps import plan_review_steps

    config, _, state = legacy
    seen = {}

    async def edit(change_dir, config, findings):
        seen["plan"] = plan_path(change_dir).read_text()
        return PlanEdits(
            edits=[
                PlanEdit(
                    file=PLAN_FILE,
                    old_str="Write a CSV with the standard library.",
                    new_str="Write a CSV with the csv module.",
                )
            ]
        )

    monkeypatch.setattr(plan_review_steps, "run_plan_edit", AsyncMock(side_effect=edit))
    monkeypatch.setattr(owner, "generate_rubric", AsyncMock(return_value="# Rubric\n"))
    monkeypatch.setattr(owner, "_commit_change_dir", lambda *a: None)
    finding = {
        "finding_id": "PR-F1",
        "severity": "critical",
        "category": "correctness",
        "text": "name the module",
    }

    assert await owner.rewrite_plan(config, state, [finding]) == 2

    assert seen["plan"] == CHANGE_FILES["design.md"]
    published = (config.change_dir / PLAN_FILE).read_text()
    assert published == CHANGE_FILES["design.md"].replace(
        "the standard library", "the csv module"
    )
    assert (config.change_dir / OLD_PLAN_FILE).read_text() == CHANGE_FILES["design.md"]
    assert not (config.change_dir / ".plan-publication.json").exists()
    diff = (config.change_dir / "plan-edits-1.diff").read_text()
    assert "-Write a CSV with the standard library." in diff
    assert "+Write a CSV with the csv module." in diff
    assert state.spec_gen.artifact_hashes[PLAN_HASH_KEY] == _sha(published)


def test_a_publication_journal_from_before_the_rename_still_finishes(legacy):
    """G2: an interrupted publication the old version journalled names
    design.md; resuming finishes it there and counts it as the plan."""
    config, _, _ = legacy
    after = CHANGE_FILES["design.md"] + "\nOne more decision.\n"
    (config.change_dir / ".plan-publication.json").write_text(
        json.dumps(
            {OLD_PLAN_FILE: {"before": CHANGE_FILES["design.md"], "after": after}}
        )
    )
    assert owner._finish_plan_publication(config) == {"design"}
    assert (config.change_dir / OLD_PLAN_FILE).read_text() == after
