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


# --- The recheck after a plan rewrite (plan-buildme-plan-recheck-2026-10-21.md) ---

def test_number_findings_gives_each_step_its_own_ids_and_text():
    found = [{'severity': 'major', 'category': 'coverage', 'description': 'gap', 'suggestion': 'add it'},
             {'description': 'no severity'}]
    numbered = owner.number_findings('design_audit', found)
    assert [f['finding_id'] for f in numbered] == ['DA-F1', 'DA-F2']
    assert numbered[0]['text'] == 'gap Fix: add it'
    assert (numbered[1]['severity'], numbered[1]['category']) == ('?', '')
    assert 'finding_id' not in found[0]
    assert [owner.number_findings(step, found[:1])[0]['finding_id'] for step in
            ('tasks_audit', 'plan_review', 'prototype')] == ['TA-F1', 'PR-F1', 'PT-F1']


LISTED = [{'finding_id': f'DA-F{n}', 'severity': 'major', 'category': 'coverage', 'text': f'gap {n}'}
          for n in (1, 2)]


@pytest.mark.parametrize('status,blocks', [('resolved', False), ('partially_resolved', True),
                                           ('still_present', True), ('cannot_verify', True)])
def test_recheck_verdict_blocks_on_every_listed_status_but_resolved(status, blocks):
    from build_pipeline.models import PriorAssessment
    given = [PriorAssessment(finding_id='DA-F1', status=status, evidence='design.md:3'),
             PriorAssessment(finding_id='DA-F2', status='resolved')]
    blocking, advisory = owner.recheck_verdict(LISTED, given, [], deterministic=[])
    assert [(f['finding_id'], f['status']) for f in blocking] == ([('DA-F1', status)] if blocks else [])
    assert advisory == []


def test_recheck_verdict_reads_a_listed_finding_with_no_status_as_cannot_verify():
    from build_pipeline.models import PriorAssessment
    blocking, _ = owner.recheck_verdict(LISTED, [PriorAssessment(finding_id='DA-F1', status='resolved')], [],
                                        deterministic=[])
    assert [(f['finding_id'], f['status'], f['evidence']) for f in blocking] == [('DA-F2', 'cannot_verify', '')]


@pytest.mark.parametrize('finding,blocks', [
    ({'severity': 'critical', 'category': 'correctness', 'description': 'crash'}, True),
    ({'severity': 'major', 'category': 'correctness', 'description': 'gap'}, False),
    ({'severity': 'minor', 'category': 'style', 'description': 'nit'}, False),
    ({'severity': 'minor', 'category': 'no-new-behaviour', 'description': 'block 2 adds nothing'}, True),
])
def test_recheck_verdict_blocks_a_new_finding_only_when_critical_or_no_new_behaviour(finding, blocks):
    blocking, advisory = owner.recheck_verdict([], [], [finding], deterministic=[])
    assert (len(blocking), len(advisory)) == ((1, 0) if blocks else (0, 1))
    assert (blocking or advisory)[0]['status'] == 'new'
    assert (blocking or advisory)[0]['text'] == finding['description']


def test_recheck_verdict_always_blocks_on_the_deterministic_scans():
    scan = {'severity': 'minor', 'category': 'placeholder', 'description': 'TBD in block 1'}
    blocking, advisory = owner.recheck_verdict([], [], [], deterministic=[scan])
    assert [f['text'] for f in blocking] == ['TBD in block 1']
    assert advisory == []


@pytest.mark.asyncio
async def test_rejected_tasks_repair_keeps_blocking_findings_and_a_progress_line(plan, monkeypatch):
    """Done means 5: a tasks repair whose recheck still finds the listed
    finding saves it as blocking and writes TASKS_AUDIT_BLOCKED."""
    from build_pipeline.llm_steps.spec_steps import AuditFindings
    from build_pipeline.models import PriorAssessment
    config, cm, state = _ready(plan)
    _writer(monkeypatch)
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(side_effect=[
        [{'severity': 'major', 'category': 'horizontal', 'description': 'HORIZONTAL-MARKER'}],
        AuditFindings([], [PriorAssessment(finding_id='TA-F1', status='still_present', evidence='block 1')])]))
    with pytest.raises(owner.PlanRepairRejected, match='Rewritten tasks still have blocking shape findings'):
        await _tasks_stage(config, cm, state)
    step = BuildState.load(config.state_file_path()).spec_gen.steps['tasks_audit']
    assert 'TA-F1 still_present' in step.blocking_findings
    assert 'HORIZONTAL-MARKER' in step.blocking_findings
    progress = config.progress_file_path().read_text()
    assert 'TASKS_AUDIT_BLOCKED' in progress and 'HORIZONTAL-MARKER' in progress


# Stop and resume rows S1-S11. A stop is a SIGKILL: the test puts back the
# checkpoint and progress file the process had written when it died, so no
# exception handler's record survives it.

class Killed(BaseException):
    """Stands in for the process dying at the point the test names."""


IDS = {'design_audit': 'DA-F1', 'plan_review': 'PR-F1', 'tasks_audit': 'TA-F1'}
PLAN_FILES = ('proposal.md', 'design.md', 'tasks.md', 'unknowns.md', 'rubric.md')


def _ready(plan):
    config, cm, state = plan
    (config.change_dir / 'requirements').mkdir(exist_ok=True)
    for cap in ('one', 'two'):
        (config.change_dir / f'requirements/{cap}.md').write_text(REQ)
    owner.reconcile_plan(config, state)
    state.checkpoint(config.state_file_path())
    return config, cm, state


def _writer(monkeypatch):
    async def generate(name, *a, **kw):
        return '# Repaired design' if name == 'design' else '## 1. Values\nReturn repaired values'
    writer = AsyncMock(side_effect=generate)
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.generate_artifact', writer)
    monkeypatch.setattr(owner, 'generate_rubric', AsyncMock(return_value='# New rubric'))
    monkeypatch.setattr(owner, '_commit_change_dir', lambda *a: None)
    return writer


def _plan_files(config):
    return {name: (config.change_dir / name).read_bytes() for name in PLAN_FILES}


async def _tasks_stage(config, cm, state, *_):
    await owner._audit_tasks_shape(config.change_dir, cm.artifact_context(config.change_dir, 'tasks'),
                                   config, config.change_dir / 'tasks.md', state)


def _first(stage, *gaps):
    """A first audit's answer, in the stage's shape: (severity, text) per finding."""
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult
    if stage == 'plan_review':
        return PlanReviewResult(findings=[PlanReviewFinding(category='correctness', severity=s, claim=t,
                                                            reason='fix it') for s, t in gaps])
    return [{'severity': s, 'category': 'correctness', 'description': t} for s, t in gaps]


def _recheck(stage, status, *gaps):
    """A recheck's answer: `status` for the stage's first listed finding, plus new findings."""
    from build_pipeline.llm_steps.spec_steps import AuditFindings
    from build_pipeline.models import PlanReviewResult, PriorAssessment
    given = [PriorAssessment(finding_id=IDS[stage], status=status, evidence='tasks.md block 1')]
    if stage == 'plan_review':
        return PlanReviewResult(findings=_first(stage, *gaps).findings, prior_assessments=given)
    return AuditFindings(_first(stage, *gaps), given)


def _install(monkeypatch, stage, answers):
    """Stub the stage's audit with these answers in order; the staged tasks
    recheck inside `rewrite_plan` answers clean unless the stage is the tasks
    audit, whose answers include it. An answer that is a function is called
    (a `_Kill.die`). Returns (run, audit stub)."""
    from build_pipeline.llm_steps import plan_review_steps
    queue = iter(answers)
    async def answer(*a, **kw):
        item = next(queue)
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item
    review = AsyncMock(side_effect=answer)
    if stage == 'tasks_audit':
        monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', review)
        return _tasks_stage, review
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[]))
    if stage == 'design_audit':
        monkeypatch.setattr(owner, '_run_audit', review)
        return owner.run_design_audit_stage, review
    monkeypatch.setattr(plan_review_steps, 'run_plan_review', review)
    return plan_review_steps.run_plan_review_stage, review


async def _run(run, config, cm, state):
    if run is _tasks_stage:
        return await run(config, cm, state)
    return await run(config.change_dir, config, state, config.state_file_path())


class _Kill:
    """Kills the run where its `die` is called: saves the checkpoint and
    progress file as they are at that moment and puts them back after."""

    def __init__(self, config):
        self.config = config
        self.saved = None

    def die(self, *a, **kw):
        progress = self.config.progress_file_path()
        self.saved = (self.config.state_file_path().read_bytes(),
                      progress.read_bytes() if progress.exists() else None)
        raise Killed()

    def restore(self):
        state_bytes, progress = self.saved
        self.config.state_file_path().write_bytes(state_bytes)
        path = self.config.progress_file_path()
        if progress is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(progress)


async def _killed(run, config, cm, state, kill):
    with pytest.raises(Killed):
        await _run(run, config, cm, state)
    kill.restore()
    return BuildState.load(config.state_file_path())


def _no_rewrite(monkeypatch):
    monkeypatch.setattr(owner, 'rewrite_plan', AsyncMock(side_effect=AssertionError('second rewrite')))


async def _block_after_recheck(monkeypatch, plan, stage):
    """Run the stage to S4: the rewrite publishes, then the recheck says the
    listed finding is still present. Returns (config, cm, state, run, audit)."""
    config, cm, state = _ready(plan)
    _writer(monkeypatch)
    run, review = _install(monkeypatch, stage, [
        _first(stage, ('critical', 'LISTED-GAP')), _recheck(stage, 'still_present')])
    with pytest.raises((owner.PlanBlocked, owner.PlanRepairRejected)):
        await _run(run, config, cm, state)
    return config, cm, BuildState.load(config.state_file_path()), run, review


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
@pytest.mark.parametrize('resumed', ['clean', 'findings'])
async def test_S1_stop_after_admission_re_audits_without_a_second_rewrite(plan, monkeypatch, capsys, stage, resumed):
    """S1: stopped after the admission checkpoint, before the journal. The
    resume re-audits the unchanged plan, passes when it is clean, and
    otherwise blocks on what it found with no second rewrite."""
    config, cm, state = _ready(plan)
    before = _plan_files(config)
    kill = _Kill(config)
    monkeypatch.setattr(owner, 'rewrite_plan', kill.die)
    again = _first(stage, ('critical', 'SECOND-GAP')) if resumed == 'findings' else _first(stage)
    run, review = _install(monkeypatch, stage, [_first(stage, ('critical', 'FIRST-GAP')), again])
    saved = await _killed(run, config, cm, state, kill)
    step = saved.spec_gen.steps[stage]
    assert (step.outcome, step.reason, step.blocked_revision) == ('blocked', 'Repair admitted; validation pending', None)
    assert [f['finding_id'] for f in step.repair_findings] == [IDS[stage]]
    assert not (config.change_dir / '.plan-publication.json').exists()
    _no_rewrite(monkeypatch)
    capsys.readouterr()
    if resumed == 'clean':
        await _run(run, config, cm, saved)
        if stage != 'tasks_audit':  # the tasks audit's caller records its pass
            assert saved.spec_gen.step_passed(stage)
    else:
        with pytest.raises(owner.PlanBlocked):
            await _run(run, config, cm, saved)
        step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
        assert step.outcome == 'blocked'
        assert step.blocked_revision == saved.spec_gen.step_revision(stage)
        assert 'SECOND-GAP' in step.blocking_findings
        assert 'SECOND-GAP' in capsys.readouterr().out
    assert review.await_count == 2
    assert _plan_files(config) == before


async def _reference_run(plan_dir, monkeypatch, stage):
    """A run of the stage that never stopped: first audit finds one gap, the
    rewrite publishes, the recheck resolves it. Returns its plan files and step."""
    config = BuildConfig(project_dir=plan_dir, change_name='revision', skip_explainers=True)
    config.specs_dir = plan_dir / 'specs'
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    root = cm.create_change(config.change_name)
    for name, value in {'proposal.md': '## Capabilities\n- `one`\n- `two`\n',
                        'use-cases.md': '# Use cases\nRead values', 'design.md': '# Design\nReturn values',
                        'tasks.md': '## 1. Values\nReturn values', 'unknowns.md': '# Unknowns\nNone',
                        'rubric.md': '# Rubric\nCheck values'}.items():
        (root / name).write_text(value)
    state = BuildState(change_name=config.change_name, mode='only', tier='advanced', spec_gen=SpecGenState(),
                       state_file=str(config.state_file_path()))
    config, cm, state = _ready((config, cm, state))
    run, _ = _install(monkeypatch, stage, [_first(stage, ('critical', 'FIRST-GAP')), _recheck(stage, 'resolved')])
    await _run(run, config, cm, state)
    return _plan_files(config), BuildState.load(config.state_file_path()).spec_gen.steps[stage]


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review'])
async def test_S2_stop_during_publication_finishes_it_then_rechecks(plan, monkeypatch, tmp_path_factory, stage):
    """S2: stopped with the journal written and tasks.md not yet replaced.
    The resume finishes the publication, then runs S3: the recheck with the
    listed findings and no new rewrite, ending as a run that never stopped."""
    config, cm, state = _ready(plan)
    writer = _writer(monkeypatch)
    kill = _Kill(config)
    original = owner.atomic_write
    def crash(path, content):
        if path == config.change_dir / 'tasks.md':
            kill.die()
        original(path, content)
    monkeypatch.setattr(owner, 'atomic_write', crash)
    run, review = _install(monkeypatch, stage, [_first(stage, ('critical', 'FIRST-GAP')), _recheck(stage, 'resolved')])
    saved = await _killed(run, config, cm, state, kill)
    assert (config.change_dir / '.plan-publication.json').exists()
    monkeypatch.setattr(owner, 'atomic_write', original)
    writes = writer.await_count
    await _run(run, config, cm, saved)
    assert writer.await_count == writes
    assert [f['finding_id'] for f in review.await_args.kwargs['prior']] == [IDS[stage]]
    files, step = await _reference_run(tmp_path_factory.mktemp('reference'), monkeypatch, stage)
    resumed = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert not (config.change_dir / '.plan-publication.json').exists()
    assert _plan_files(config) == files
    assert resumed.model_dump(exclude={'revision'}) == step.model_dump(exclude={'revision'})
    assert saved.spec_gen.step_passed(stage)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review'])
async def test_S3_stop_during_the_recheck_reruns_it_with_the_listed_findings(plan, monkeypatch, tmp_path_factory, stage):
    """S3: stopped after the publication, while the recheck runs. The resume
    reruns the recheck with `prior=repair_findings` and no new rewrite; the
    step ends as in a run that never stopped."""
    config, cm, state = _ready(plan)
    writer = _writer(monkeypatch)
    kill = _Kill(config)
    run, review = _install(monkeypatch, stage, [_first(stage, ('critical', 'FIRST-GAP')), kill.die,
                                                _recheck(stage, 'resolved')])
    saved = await _killed(run, config, cm, state, kill)
    step = saved.spec_gen.steps[stage]
    assert (step.repair_attempts, step.blocked_revision) == (1, None)
    owner.reopen_if_edited(saved, stage, config.state_file_path())
    assert saved.spec_gen.steps[stage].repair_attempts == 1
    writes = writer.await_count
    await _run(run, config, cm, saved)
    assert writer.await_count == writes
    assert [f['finding_id'] for f in review.await_args.kwargs['prior']] == [IDS[stage]]
    files, reference = await _reference_run(tmp_path_factory.mktemp('reference'), monkeypatch, stage)
    resumed = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert _plan_files(config) == files
    assert resumed.model_dump(exclude={'revision'}) == reference.model_dump(exclude={'revision'})
    assert resumed.repair_findings[0]['status'] == 'resolved'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
async def test_S4_a_blocking_recheck_refuses_the_resume_with_its_findings(plan, monkeypatch, capsys, stage):
    """S4: the recheck blocks. The checkpoint, the progress file and the
    console show the listed finding and its status; a resume at the same
    revision refuses with no model call and ends with the edit line."""
    config, cm, state, run, review = await _block_after_recheck(monkeypatch, plan, stage)
    step = state.spec_gen.steps[stage]
    assert step.outcome == 'blocked'
    assert step.blocked_revision == state.spec_gen.step_revision(stage)
    assert f'{IDS[stage]} still_present' in step.blocking_findings
    assert [(f['finding_id'], f['status']) for f in step.repair_findings] == [(IDS[stage], 'still_present')]
    assert f'{stage.upper()}_BLOCKED' in config.progress_file_path().read_text()
    assert 'LISTED-GAP' in capsys.readouterr().out
    calls, before = review.await_count, _plan_files(config)
    with pytest.raises(owner.PlanBlocked) as refused:
        await _run(run, config, cm, state)
    assert str(refused.value).endswith(owner.REOPEN_LINE)
    assert f'{IDS[stage]} still_present' in str(refused.value)
    assert owner.REOPEN_LINE in capsys.readouterr().out
    assert review.await_count == calls
    assert _plan_files(config) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
async def test_S5_an_edited_plan_gets_its_one_rewrite_back(plan, monkeypatch, caplog, stage):
    """S5: the plan was edited after the block. The resume reopens the step
    (count reset, both new fields cleared, one log line), then runs it as a
    first audit, which may rewrite once more."""
    config, cm, state, run, review = await _block_after_recheck(monkeypatch, plan, stage)
    (config.change_dir / 'design.md').write_text('# Design\nEdited by the user')
    owner.reconcile_plan(config, state)
    writer = _writer(monkeypatch)
    run, review = _install(monkeypatch, stage, [_first(stage, ('critical', 'AFTER-EDIT')), _recheck(stage, 'resolved')])
    with caplog.at_level('WARNING'):
        await _run(run, config, cm, state)
    assert f'{stage}: plan changed since it was blocked; one rewrite allowed again' in caplog.text
    assert writer.await_count >= 1
    assert review.await_args_list[0].kwargs.get('prior') is None
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert step.outcome == 'passed' and step.blocked_revision is None
    assert [(f['finding_id'], f['status']) for f in step.repair_findings] == [(IDS[stage], 'resolved')]
    assert 'AFTER-EDIT' in step.repair_findings[0]['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
async def test_S6_repeated_resumes_refuse_alike_and_a_new_block_replaces_the_old(plan, monkeypatch, stage):
    """S6: each resume after S4 refuses the same way with no spend and no
    write. After an S5 reopen blocks again, the new record replaces the old."""
    config, cm, state, run, review = await _block_after_recheck(monkeypatch, plan, stage)
    checkpoint, calls = config.state_file_path().read_bytes(), review.await_count
    messages = []
    for _ in range(2):
        with pytest.raises(owner.PlanBlocked) as refused:
            await _run(run, config, cm, BuildState.load(config.state_file_path()))
        messages.append(str(refused.value))
    assert messages[0] == messages[1]
    assert config.state_file_path().read_bytes() == checkpoint
    assert review.await_count == calls
    (config.change_dir / 'design.md').write_text('# Design\nEdited by the user')
    _writer(monkeypatch)
    run, review = _install(monkeypatch, stage, [_first(stage, ('critical', 'NEW-GAP')), _recheck(stage, 'still_present')])
    state = BuildState.load(config.state_file_path())
    with pytest.raises((owner.PlanBlocked, owner.PlanRepairRejected)):
        await _run(run, config, cm, state)
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert step.blocked_revision == state.spec_gen.step_revision(stage)
    assert 'NEW-GAP' in step.repair_findings[0]['text']
    assert 'LISTED-GAP' not in step.blocking_findings


@pytest.mark.asyncio
async def test_S7_this_change_adds_no_lock_against_a_second_run(plan, monkeypatch):
    """S7: buildme has no guard against two builds of one change outside an
    epic (the epic lock, epic/lock.py, covers only the epic directory). This
    PR adds none: a second run started while the first waits on its audit
    reaches its own audit call, and no lock file is left behind."""
    import asyncio
    config, cm, state = _ready(plan)
    release, started = asyncio.Event(), []
    async def audit(*a, **kw):
        started.append(1)
        await release.wait()
        return []
    monkeypatch.setattr(owner, '_run_audit', audit)
    first = asyncio.create_task(owner.run_design_audit_stage(config.change_dir, config, state, config.state_file_path()))
    while not started:
        await asyncio.sleep(0)
    second = asyncio.create_task(owner.run_design_audit_stage(
        config.change_dir, config, BuildState.load(config.state_file_path()), config.state_file_path()))
    while len(started) < 2:
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert not list(config.project_dir.rglob('*.lock'))


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
async def test_S8_moved_git_refs_do_not_change_the_resume(plan, monkeypatch, stage):
    """S8: origin or the base branch moved between stop and resume. Step
    revisions hash plan files, not refs, so the resume still refuses as S4."""
    import subprocess
    config, cm, state, run, review = await _block_after_recheck(monkeypatch, plan, stage)
    revision = state.spec_gen.step_revision(stage)
    git = ['git', '-C', str(config.project_dir), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'base'], check=True)
    subprocess.run([*git, 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
    (config.project_dir / 'README.md').write_text('moved')
    subprocess.run([*git, 'add', 'README.md'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'moved'], check=True)
    subprocess.run([*git, 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
    state = BuildState.load(config.state_file_path())
    owner.reconcile_plan(config, state)
    assert state.spec_gen.step_revision(stage) == revision
    calls = review.await_count
    with pytest.raises(owner.PlanBlocked, match='Edit the plan'):
        await _run(run, config, cm, state)
    assert review.await_count == calls


def _legacy_checkpoint(config, state, stage, **step):
    """Write a checkpoint as a build before this change saved it: the step has
    no `repair_findings` or `blocked_revision` key."""
    state.checkpoint(config.state_file_path())
    raw = json.loads(config.state_file_path().read_text())
    raw['spec_gen']['steps'][stage] = step
    config.state_file_path().write_text(json.dumps(raw))
    return BuildState.load(config.state_file_path())


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review', 'tasks_audit'])
async def test_S9_an_old_blocked_checkpoint_loads_and_refuses_as_before(plan, monkeypatch, capsys, stage):
    """S9: a checkpoint from before this change, blocked on findings. It loads
    with the defaults, the reopen does nothing, and the refusal prints the
    saved blocking findings."""
    config, cm, state = _ready(plan)
    saved = _legacy_checkpoint(config, state, stage, revision=state.spec_gen.step_revision(stage),
                               outcome='blocked', reason='still blocked', repair_attempts=1,
                               blocking_findings='- [critical] OLD-FINDING')
    step = saved.spec_gen.steps[stage]
    assert (step.repair_findings, step.blocked_revision) == ([], None)
    owner.reopen_if_edited(saved, stage, config.state_file_path())
    assert saved.spec_gen.steps[stage].repair_attempts == 1
    run, review = _install(monkeypatch, stage, [AssertionError('no model call')])
    with pytest.raises(owner.PlanBlocked, match='OLD-FINDING'):
        await _run(run, config, cm, saved)
    assert 'OLD-FINDING' in capsys.readouterr().out
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['design_audit', 'plan_review'])
async def test_S10_an_old_mid_rewrite_checkpoint_rechecks_as_a_first_audit(plan, monkeypatch, stage):
    """S10: a checkpoint from before this change, stopped mid-rewrite after
    the publication (S1 shape, no `repair_findings`). The re-audit runs with
    no listed findings and blocks on major findings as before, with no second
    rewrite."""
    config, cm, state = _ready(plan)
    saved = _legacy_checkpoint(config, state, stage, revision='revision-before-the-rewrite', outcome='blocked',
                               reason='Repair admitted; validation pending', repair_attempts=1)
    assert not owner.rewrite_published(saved.spec_gen, stage)
    _no_rewrite(monkeypatch)
    run, review = _install(monkeypatch, stage, [_first(stage, ('major', 'MAJOR-GAP'))])
    with pytest.raises(owner.PlanBlocked):
        await _run(run, config, cm, saved)
    assert review.await_args.kwargs.get('prior') is None
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert 'MAJOR-GAP' in step.blocking_findings
    assert step.blocked_revision == saved.spec_gen.step_revision(stage)


def _prototype(plan, monkeypatch):
    from build_pipeline import orchestrator
    from build_pipeline.llm_steps import prototype_steps
    from build_pipeline.models import GateResult
    config, cm, state = _ready(plan)
    config.prototype_dir.mkdir(parents=True, exist_ok=True)
    (config.prototype_dir / 'NOTES.md').write_text(
        'PROVES: one page\nDISPROVES: PROTO-GAP the total needs the tax rate\nOPEN_QUESTIONS: none\nSTATUS: DONE\n')
    monkeypatch.setattr(orchestrator, 'prototype_decision', lambda config: (True, 'ui'))
    agent = AsyncMock(return_value=GateResult(passed=True, reason="built"))
    monkeypatch.setattr(prototype_steps, 'run_prototype', agent)
    monkeypatch.setattr('build_pipeline.llm_steps.spec_steps.run_tasks_audit', AsyncMock(return_value=[]))
    progress = Mock()

    async def run(state):
        await orchestrator._run_prototype_phase(config, state, progress, config.state_file_path())
    return config, state, run, agent


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['before_publish', 'after_publish'])
async def test_S11_a_stopped_prototype_rewrite_is_never_repeated(plan, monkeypatch, stop):
    """S11: stopped after `admit_repair("prototype")`. Before the publication,
    the resume re-runs the prototype agent but makes no second rewrite and
    blocks on the saved findings. After it, the step records passed."""
    config, state, run, agent = _prototype(plan, monkeypatch)
    writer = _writer(monkeypatch)
    kill = _Kill(config)
    if stop == 'before_publish':
        monkeypatch.setattr(owner, 'rewrite_plan', kill.die)
    else:
        original = owner.rewrite_plan
        async def publish_then_die(*a, **kw):
            await original(*a, **kw)
            kill.die()
        monkeypatch.setattr(owner, 'rewrite_plan', publish_then_die)
    with pytest.raises(Killed):
        await run(state)
    kill.restore()
    saved = BuildState.load(config.state_file_path())
    assert saved.spec_gen.repair_attempts('prototype') == 1
    _no_rewrite(monkeypatch)
    files = _plan_files(config)
    if stop == 'before_publish':
        with pytest.raises(owner.PlanBlocked):
            await run(saved)
        assert agent.await_count == 2
        step = BuildState.load(config.state_file_path()).spec_gen.steps['prototype']
        assert 'PT-F1' in step.blocking_findings and 'PROTO-GAP' in step.blocking_findings
        assert step.blocked_revision == saved.spec_gen.step_revision('prototype')
    else:
        await run(saved)
        assert agent.await_count == 1
        assert BuildState.load(config.state_file_path()).spec_gen.step_passed('prototype')
        assert writer.await_count  # the stopped run published its rewrite
    assert _plan_files(config) == files
