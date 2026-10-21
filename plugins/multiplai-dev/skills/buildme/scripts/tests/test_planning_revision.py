"""Requirements interruption, revision publication and budget admission regressions."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from build_pipeline import budget, sdk, spec_generator as owner
from build_pipeline.change_manager import ChangeManager, ArtifactStatus
from build_pipeline.config import BuildConfig
from build_pipeline.state import BuildState, SpecGenState

REQ = "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns one\n"


@pytest.fixture
def plan(tmp_path):
    config = BuildConfig(project_dir=tmp_path, change_name="revision", skip_explainers=True)
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    root = cm.create_change(config.change_name)
    for name, value in {"proposal.md": "## Capabilities\n- `one`\n- `two`\n",
                        "use-cases.md": "# Use cases\nRead values", "design.md": "# Design\nReturn values",
                        "tasks.md": "## 1. Values\nReturn values", "unknowns.md": "# Unknowns\nNone",
                        "rubric.md": "# Rubric\nCheck values"}.items():
        (root / name).write_text(value)
    state = BuildState(change_name=config.change_name, mode="only", tier="advanced", spec_gen=SpecGenState(),
                       state_file=str(config.state_file_path()))
    return config, cm, state


@pytest.mark.asyncio
async def test_interrupted_capability_generation_resumes_only_missing_member(plan, monkeypatch):
    config, cm, state = plan
    generate = AsyncMock(side_effect=[REQ, RuntimeError("second call interrupted")])
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", generate)
    with pytest.raises(RuntimeError):
        await owner._generate_requirements(cm, config.change_dir, config, state)
    first = (config.change_dir / "requirements/one.md").read_bytes()
    assert cm.artifact_status(config.change_dir)["tasks"] == ArtifactStatus.BLOCKED
    state = BuildState.load(config.state_file_path())
    generate = AsyncMock(return_value=REQ)
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", generate)
    await owner._generate_requirements(cm, config.change_dir, config, state)
    generate.assert_awaited_once()
    assert generate.await_args.args[1]["capability_name"] == "two"
    assert (config.change_dir / "requirements/one.md").read_bytes() == first
    assert cm.artifact_status(config.change_dir)["requirements"] == ArtifactStatus.DONE


@pytest.mark.asyncio
async def test_invalid_replacement_leaves_prior_capability_file_intact(plan, monkeypatch):
    config, cm, state = plan
    path = config.change_dir / "requirements/one.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(REQ)
    state.spec_gen.stale_artifacts = ["requirements"]
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", AsyncMock(return_value="# unfinished"))
    with pytest.raises(ValueError, match="Invalid requirements"):
        await owner._generate_requirements(cm, config.change_dir, config, state)
    assert path.read_text() == REQ


@pytest.mark.asyncio
async def test_capability_revision_invalidates_tasks_and_reviews(plan, monkeypatch):
    config, cm, state = plan
    generate = AsyncMock(return_value=REQ)
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", generate)
    await owner._generate_requirements(cm, config.change_dir, config, state)
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step("plan_review", "passed")
    (config.change_dir / "proposal.md").write_text("## Capabilities\n- `one`\n- `three`\n")
    owner.reconcile_plan(config, state)
    assert "tasks" in state.spec_gen.stale_artifacts
    assert not state.spec_gen.step_passed("plan_review")
    await owner._generate_requirements(cm, config.change_dir, config, state)
    assert state.spec_gen.expected_capabilities == ["one", "three"]
    assert (config.change_dir / "requirements/three.md").is_file()


@pytest.mark.asyncio
async def test_interrupted_publication_recovers_one_complete_revision(plan, monkeypatch):
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step("plan_review", "passed")
    old_revision = state.spec_gen.revision
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", AsyncMock(side_effect=["# New design", "## 1. New values\nReturn two"]))
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.run_tasks_audit", AsyncMock(return_value=[]))
    monkeypatch.setattr(owner, "generate_rubric", AsyncMock(return_value="# New rubric"))
    original = owner.atomic_write
    def interrupted(path, content):
        if path == config.change_dir / "tasks.md":
            raise OSError("crash during publication")
        original(path, content)
    monkeypatch.setattr(owner, "atomic_write", interrupted)
    with pytest.raises(OSError, match="crash"):
        await owner.rewrite_plan(config, state, "change values", design=True)
    assert (config.change_dir / ".plan-publication.json").exists()
    monkeypatch.setattr(owner, "atomic_write", original)
    owner.reconcile_plan(config, state)
    assert not (config.change_dir / ".plan-publication.json").exists()
    assert (config.change_dir / "tasks.md").read_text() == "## 1. New values\nReturn two"
    assert (config.change_dir / "rubric.md").read_text() == "# New rubric"
    assert state.spec_gen.revision != old_revision
    assert not state.spec_gen.step_passed("plan_review")
    assert not state.spec_gen.stale_artifacts


def test_publication_recovery_refuses_external_edits(plan):
    config, _, state = plan
    path = config.change_dir / "tasks.md"
    journal = config.change_dir / ".plan-publication.json"
    journal.write_text(json.dumps({"tasks.md": {"before": path.read_text(), "after": "## 1. Replacement"}}))
    path.write_text("my intervening edits")
    with pytest.raises(RuntimeError, match="changed"):
        owner.reconcile_plan(config, state)
    assert path.read_text() == "my intervening edits"
    assert journal.exists()


def test_future_checkpoint_fails_without_rewriting(plan):
    config, _, state = plan
    state.checkpoint(config.state_file_path())
    raw = json.loads(config.state_file_path().read_text())
    raw["schema_version"] = 999
    config.state_file_path().write_text(json.dumps(raw))
    before = config.state_file_path().read_bytes()
    with pytest.raises(ValueError, match="newer"):
        BuildState.load(config.state_file_path())
    assert config.state_file_path().read_bytes() == before


@pytest.mark.asyncio
async def test_spec_generation_and_resume_admit_no_call_after_cap(plan, monkeypatch):
    config, _, state = plan
    config.budget_max_usd = .1
    usage = SimpleNamespace(cost_usd=.2, input_tokens=20, output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0)
    provider = AsyncMock(return_value=SimpleNamespace(text="answer", usage=usage))
    monkeypatch.setattr(sdk, "run_agent", provider)
    async def plan_calls(*args):
        await sdk.llm_call("first planning call")
        await sdk.llm_call("must not dispatch")
    monkeypatch.setattr(owner, "_generate_all_artifacts", plan_calls)
    for attempt in range(2):
        with pytest.raises(budget.BudgetExceededError):
            await owner.run_spec_generator(config)
        saved = BuildState.load(config.state_file_path())
        assert saved.budget["cost_usd"] == .2
    assert provider.await_count == 1

@pytest.mark.asyncio
async def test_cancelled_standalone_planning_persists_partial_usage(plan, monkeypatch):
    import asyncio
    config, _, _ = plan
    cancelled = asyncio.CancelledError()
    cancelled.partial = SimpleNamespace(usage=SimpleNamespace(cost_usd=.3, input_tokens=30))
    monkeypatch.setattr(sdk, "run_agent", AsyncMock(side_effect=cancelled))
    async def interrupted(*args):
        await sdk.llm_call("planning")
    monkeypatch.setattr(owner, "_generate_all_artifacts", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await owner.run_spec_generator(config)
    assert BuildState.load(config.state_file_path()).budget["cost_usd"] == .3


def test_new_plan_revision_invalidates_size_acceptance(plan):
    config, _, state = plan
    owner.reconcile_plan(config, state)
    state.size_accepted = True
    owner.reconcile_plan(config, state)
    assert state.size_accepted
    (config.change_dir / "tasks.md").write_text("## 1. Larger plan\nMore work")
    owner.reconcile_plan(config, state)
    assert not state.size_accepted


def test_repair_admission_and_outcome_share_one_ledger():
    sg = SpecGenState.model_validate({'tasks_audit_regen_done': True, 'tasks_audit_done': True})
    assert sg.repair_attempts('tasks_audit') == 1
    assert not sg.step_passed('tasks_audit')
    sg.record_step('tasks_audit')
    saved = sg.model_dump()
    assert saved['steps']['tasks_audit']['repair_attempts'] == 1
    assert not any(key.endswith('_done') for key in saved)


@pytest.mark.asyncio
async def test_tasks_shape_repair_failure_keeps_published_plan_intact(plan, monkeypatch):
    config, cm, state = plan
    before = (config.change_dir / 'tasks.md').read_bytes()
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[
        {'severity': 'major', 'description': 'horizontal breakdown'}]))
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact', AsyncMock(return_value='## 1. Replacement\nReturn two'))
    # The fixture deliberately lacks complete requirements, so publication must fail.
    with pytest.raises(ValueError, match='incomplete capability'):
        await owner._audit_tasks_shape(config.change_dir, cm.artifact_context(config.change_dir, 'tasks'),
                                      config, config.change_dir / 'tasks.md', state)
    assert (config.change_dir / 'tasks.md').read_bytes() == before
    assert state.spec_gen.repair_attempts('tasks_audit') == 1
    assert not state.spec_gen.step_passed('tasks_audit')

@pytest.mark.parametrize('repair', [False, True])
def test_tasks_prompt_includes_proposal_and_use_cases_from_artifact_context(plan, repair):
    from build_pipeline.llm_steps.spec_steps import _build_prompt
    config, cm, _ = plan
    (config.change_dir / 'proposal.md').write_text('PROPOSAL_SENTINEL_631f\n## Capabilities\n- `one`\n')
    (config.change_dir / 'use-cases.md').write_text('USE_CASE_SENTINEL_952d')
    context = cm.artifact_context(config.change_dir, 'tasks')
    prompt = _build_prompt('tasks', context, config, audit_findings='repair this' if repair else '')
    assert 'PROPOSAL_SENTINEL_631f' in prompt
    assert 'USE_CASE_SENTINEL_952d' in prompt


def test_requirements_change_invalidates_design(plan):
    config, _, state = plan
    req = config.change_dir / 'requirements/one.md'
    req.parent.mkdir(exist_ok=True)
    req.write_text(REQ)
    owner.reconcile_plan(config, state)
    req.write_text(REQ.replace('one', 'two'))
    owner.reconcile_plan(config, state)
    assert 'design' in state.spec_gen.stale_artifacts


def test_legacy_repair_flag_cannot_be_lost_when_step_evidence_already_exists():
    sg = SpecGenState.model_validate({'design_audit_regen_done': True, 'steps': {
        'design_audit': {'revision': 'accepted', 'outcome': 'passed'}}})
    assert sg.repair_attempts('design_audit') == 1
    assert sg.steps['design_audit'].revision == 'accepted'
    with pytest.raises(RuntimeError, match='already attempted'):
        sg.admit_repair('design_audit')

@pytest.mark.asyncio
async def test_staged_repair_rejects_placeholders_even_when_model_audit_is_clean(plan, monkeypatch):
    config, _, state = plan
    (config.change_dir / 'requirements').mkdir()
    for cap in ('one', 'two'):
        (config.change_dir / f'requirements/{cap}.md').write_text(REQ)
    before = (config.change_dir / 'tasks.md').read_bytes()
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact',
                        AsyncMock(return_value='## 1. Values\nError handling: TBD\nReturn values'))
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[]))
    monkeypatch.setattr(owner, 'generate_rubric', AsyncMock(return_value='# Rubric'))
    with pytest.raises(ValueError, match='placeholder'):
        await owner.rewrite_plan(config, state, 'Remove placeholders')
    assert (config.change_dir / 'tasks.md').read_bytes() == before
    assert not state.spec_gen.step_passed('tasks_audit')


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review'])
async def test_blocked_findings_survive_resume_without_repaying_same_review(plan, monkeypatch, stage):
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult
    config, _, state = plan
    owner.reconcile_plan(config, state)
    state.spec_gen.admit_repair(stage)
    sentinel = 'Missing rejection of the unique invalid account 891'
    if stage == 'design_audit':
        review = AsyncMock(return_value=[{'severity': 'critical', 'category': 'correctness', 'description': sentinel}])
        monkeypatch.setattr(owner, '_run_audit', review)
        run = owner.run_design_audit_stage
    else:
        review = AsyncMock(return_value=PlanReviewResult(findings=[PlanReviewFinding(
            category='correctness', severity='critical', claim=sentinel, reason='Add a rejection scenario')]))
        monkeypatch.setattr(plan_review_steps, 'run_plan_review', review)
        run = plan_review_steps.run_plan_review_stage
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await run(config.change_dir, config, state, config.state_file_path())
        state = BuildState.load(config.state_file_path())
        assert sentinel in state.spec_gen.steps[stage].reason
        assert sentinel in config.progress_file_path().read_text()
    assert review.await_count == 1


@pytest.mark.asyncio
async def test_repaired_unknowns_keep_their_accepted_evidence_on_resume(plan, monkeypatch):
    config, cm, state = plan
    (config.change_dir / 'requirements').mkdir(exist_ok=True)
    for cap in ('one', 'two'):
        (config.change_dir / f'requirements/{cap}.md').write_text(REQ)
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step('prototype', 'skipped', 'no UI')
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact', AsyncMock(side_effect=[
        '# New design', '## 1. Values\nReturn two']))
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[]))
    monkeypatch.setattr(owner, 'generate_rubric', AsyncMock(return_value='# New rubric'))
    monkeypatch.setattr(owner, '_commit_change_dir', lambda *a: None)
    await owner.rewrite_plan(config, state, 'repair', design=True)
    saved = BuildState.load(config.state_file_path())
    assert saved.spec_gen.step_passed('explainers')
    assert saved.spec_gen.step_passed('prototype')
    unknowns = (config.change_dir / 'unknowns.md').read_bytes()
    provider = AsyncMock(side_effect=AssertionError('unchanged resume must not call a model'))
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact', provider)
    await owner._generate_all_artifacts(cm, config.change_dir, config, saved)
    assert (config.change_dir / 'unknowns.md').read_bytes() == unknowns
    provider.assert_not_awaited()


def test_proposal_edit_invalidates_preimplementation_prototype_skip(plan):
    from build_pipeline.orchestrator import _prototype_finished
    config, _, state = plan
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step('prototype', 'skipped', 'no UI')
    assert _prototype_finished(state)
    (config.change_dir / 'proposal.md').write_text('## Capabilities\n- `one`\n- `two`\n## UI\nAdd a dashboard')
    owner.reconcile_plan(config, state)
    assert not _prototype_finished(state)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review'])
@pytest.mark.parametrize('failure', ['audit', 'commit'])
async def test_interrupted_post_repair_audit_resumes_on_the_new_revision(plan, monkeypatch, stage, failure):
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.llm_steps.spec_steps import AuditFindings
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult, PriorAssessment
    config, _, state = plan
    (config.change_dir / 'requirements').mkdir(exist_ok=True)
    for cap in ('one', 'two'):
        (config.change_dir / f'requirements/{cap}.md').write_text(REQ)
    owner.reconcile_plan(config, state)
    before = state.spec_gen.revision
    async def generate(name, *a, **kw):
        return '# Repaired design' if name == 'design' else '## 1. Values\nReturn repaired values'
    writer = AsyncMock(side_effect=generate)
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact', writer)
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[]))
    monkeypatch.setattr(owner, 'generate_rubric', AsyncMock(return_value='# New rubric'))
    monkeypatch.setattr(owner, '_commit_change_dir', Mock(side_effect=RuntimeError('commit interrupted'))
                        if failure == 'commit' else lambda *a: None)
    if stage == 'design_audit':
        review = AsyncMock(side_effect=[
            [{'severity': 'critical', 'category': 'correctness', 'description': 'old gap'}],
            *([RuntimeError('recheck interrupted')] if failure == 'audit' else []),
            AuditFindings([], [PriorAssessment(finding_id='DA-F1', status='resolved')])])
        monkeypatch.setattr(owner, '_run_audit', review)
        run = owner.run_design_audit_stage
    else:
        review = AsyncMock(side_effect=[PlanReviewResult(findings=[PlanReviewFinding(
            severity='critical', category='correctness', claim='old gap', reason='fix it')]),
            *([RuntimeError('recheck interrupted')] if failure == 'audit' else []),
            PlanReviewResult(prior_assessments=[PriorAssessment(finding_id='PR-F1', status='resolved')])])
        monkeypatch.setattr(plan_review_steps, 'run_plan_review', review)
        run = plan_review_steps.run_plan_review_stage
    with pytest.raises(RuntimeError, match='interrupted'):
        await run(config.change_dir, config, state, config.state_file_path())
    saved = BuildState.load(config.state_file_path())
    assert saved.spec_gen.revision != before
    assert saved.spec_gen.repair_attempts(stage) == 1
    writes = writer.await_count
    await run(config.change_dir, config, saved, config.state_file_path())
    assert saved.spec_gen.step_passed(stage)
    assert review.await_count == (3 if failure == 'audit' else 2)
    assert writer.await_count == writes
