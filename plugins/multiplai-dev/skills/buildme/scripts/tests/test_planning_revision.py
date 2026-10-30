"""Planning revision publication, input invalidation and budget admission regressions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from build_pipeline import budget, sdk
from build_pipeline import spec_generator as owner
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.state import BuildState, SpecGenState

REQ = "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns one\n"
# The findings a direct `rewrite_plan` call is asked to fix.
FIX = [
    {
        "finding_id": "PR-F1",
        "severity": "critical",
        "category": "correctness",
        "text": "change values",
    }
]


def _rewrite_to(monkeypatch, *, design=None, tasks=None):
    """Fake the rewrite's edit call: it replaces the whole of implementation-plan.md and
    tasks.md, as the staged copy holds them, with these texts (a file given
    no text is not edited). Returns the stub."""
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanEdit, PlanEdits

    async def edit(change_dir, config, findings):
        edits = []
        for name, text in (("implementation-plan.md", design), ("tasks.md", tasks)):
            if text is not None:
                old = (change_dir / name).read_text()
                edits.append(PlanEdit(file=name, old_str=old, new_str=text))
        return PlanEdits(edits=edits)

    stub = AsyncMock(side_effect=edit)
    monkeypatch.setattr(plan_review_steps, "run_plan_edit", stub)
    return stub


@pytest.fixture
def plan(tmp_path):
    config = BuildConfig(
        project_dir=tmp_path, change_name="revision", skip_explainers=True
    )
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    root = cm.create_change(config.change_name)
    for name, value in {
        "proposal.md": "## Capabilities\n- `one`\n- `two`\n",
        "use-cases.md": "# Use cases\nRead values",
        "implementation-plan.md": "# Design\nReturn values",
        "tasks.md": "## 1. Values\nReturn values",
        "unknowns.md": "# Unknowns\nNone",
        "rubric.md": "# Rubric\nCheck values",
    }.items():
        (root / name).write_text(value)
    state = BuildState(
        change_name=config.change_name,
        tier="advanced",
        spec_gen=SpecGenState(),
        state_file=str(config.state_file_path()),
    )
    return config, cm, state


def test_an_input_edit_marks_buildmes_artifacts_stale_and_never_touches_the_inputs(
    plan,
):
    """Item 19: the proposal, use cases and requirements are hashed, so an
    edit to one marks design and the stages after it stale; reconcile_plan
    never regenerates them or moves a requirements file the proposal no
    longer names."""
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step("plan_review", "passed")
    assert state.spec_gen.stale_artifacts == []
    (config.change_dir / "proposal.md").write_text("## Capabilities\n- `one`\n")
    owner.reconcile_plan(config, state)
    assert set(state.spec_gen.stale_artifacts) == {
        "design",
        "unknowns",
        "tasks",
        "rubric",
    }
    assert not state.spec_gen.step_passed("plan_review")
    assert (config.change_dir / "requirements/two.md").read_text() == REQ
    assert not (config.change_dir / "superseded-requirements").exists()


def test_a_use_cases_edit_marks_only_tasks_and_rubric_stale(plan):
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    (config.change_dir / "use-cases.md").write_text("# Use cases\nRead two values")
    owner.reconcile_plan(config, state)
    assert set(state.spec_gen.stale_artifacts) == {"tasks", "rubric"}


@pytest.mark.asyncio
async def test_interrupted_publication_recovers_one_complete_revision(
    plan, monkeypatch
):
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    state.spec_gen.record_step("plan_review", "passed")
    old_revision = state.spec_gen.revision
    _rewrite_to(
        monkeypatch, design="# New design", tasks="## 1. New values\nReturn two"
    )
    monkeypatch.setattr(
        owner, "generate_rubric", AsyncMock(return_value="# New rubric")
    )
    original = owner.atomic_write

    def interrupted(path, content):
        if path == config.change_dir / "tasks.md":
            raise OSError("crash during publication")
        original(path, content)

    monkeypatch.setattr(owner, "atomic_write", interrupted)
    with pytest.raises(OSError, match="crash"):
        await owner.rewrite_plan(config, state, FIX)
    assert (config.change_dir / ".plan-publication.json").exists()
    monkeypatch.setattr(owner, "atomic_write", original)
    owner.reconcile_plan(config, state)
    assert not (config.change_dir / ".plan-publication.json").exists()
    assert (
        config.change_dir / "tasks.md"
    ).read_text() == "## 1. New values\nReturn two"
    assert (config.change_dir / "rubric.md").read_text() == "# New rubric"
    assert state.spec_gen.revision != old_revision
    assert not state.spec_gen.step_passed("plan_review")
    assert not state.spec_gen.stale_artifacts


def test_publication_recovery_refuses_external_edits(plan):
    config, _, state = plan
    path = config.change_dir / "tasks.md"
    journal = config.change_dir / ".plan-publication.json"
    journal.write_text(
        json.dumps(
            {"tasks.md": {"before": path.read_text(), "after": "## 1. Replacement"}}
        )
    )
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
    from tests.spec_fixtures import ready_specs

    config, _, state = plan
    ready_specs(config.change_dir)
    config.budget_max_usd = 0.1
    usage = SimpleNamespace(
        cost_usd=0.2,
        input_tokens=20,
        output_tokens=2,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )
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
        assert saved.budget["cost_usd"] == 0.2
    assert provider.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_standalone_planning_persists_partial_usage(plan, monkeypatch):
    import asyncio

    from tests.spec_fixtures import ready_specs

    config, _, _ = plan
    ready_specs(config.change_dir)
    cancelled = asyncio.CancelledError()
    cancelled.partial = SimpleNamespace(
        usage=SimpleNamespace(cost_usd=0.3, input_tokens=30)
    )
    monkeypatch.setattr(sdk, "run_agent", AsyncMock(side_effect=cancelled))

    async def interrupted(*args):
        await sdk.llm_call("planning")

    monkeypatch.setattr(owner, "_generate_all_artifacts", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await owner.run_spec_generator(config)
    assert BuildState.load(config.state_file_path()).budget["cost_usd"] == 0.3


def test_new_plan_revision_invalidates_size_acceptance(plan):
    config, _, state = plan
    owner.reconcile_plan(config, state)
    state.size_accepted = True
    owner.reconcile_plan(config, state)
    assert state.size_accepted
    (config.change_dir / "tasks.md").write_text("## 1. Larger plan\nMore work")
    owner.reconcile_plan(config, state)
    assert not state.size_accepted


def test_new_plan_revision_invalidates_plan_approval(plan):
    """Item E3: an approval names one plan revision; an edit to tasks.md
    (or the plan file) clears it, as it clears `size_accepted`."""
    from build_pipeline.state import PlanApproval

    config, _, state = plan
    owner.reconcile_plan(config, state)
    approval = PlanApproval(
        revision=state.spec_gen.revision, by="Ada Lovelace", commit="a" * 40
    )
    state.plan_approval = approval
    owner.reconcile_plan(config, state)
    assert state.plan_approval == approval
    (config.change_dir / "tasks.md").write_text("## 1. Larger plan\nMore work")
    owner.reconcile_plan(config, state)
    assert state.plan_approval is None


def test_repair_admission_and_outcome_share_one_ledger():
    sg = SpecGenState.model_validate(
        {"plan_review_regen_done": True, "plan_review_done": True}
    )
    assert sg.repair_attempts("plan_review") == 1
    assert not sg.step_passed("plan_review")
    sg.record_step("plan_review")
    saved = sg.model_dump()
    assert saved["steps"]["plan_review"]["repair_attempts"] == 1
    assert not any(key.endswith("_done") for key in saved)


@pytest.mark.asyncio
async def test_a_rewrite_that_fails_publication_keeps_the_plan_intact(
    plan, monkeypatch
):
    config, cm, state = plan
    before = (config.change_dir / "tasks.md").read_bytes()
    _rewrite_to(monkeypatch, tasks="## 1. Replacement\nReturn two")
    monkeypatch.setattr(owner, "generate_rubric", AsyncMock(return_value="# Rubric"))
    # The fixture deliberately lacks complete requirements, so publication must fail.
    with pytest.raises(ValueError, match="incomplete capability"):
        await owner.rewrite_plan(config, state, FIX)
    assert (config.change_dir / "tasks.md").read_bytes() == before
    assert not (config.change_dir / ".plan-publication.json").exists()


@pytest.mark.parametrize("repair", [False, True])
def test_tasks_prompt_includes_proposal_and_use_cases_from_artifact_context(
    plan, repair
):
    from build_pipeline.llm_steps.spec_steps import _build_prompt

    config, cm, _ = plan
    (config.change_dir / "proposal.md").write_text(
        "PROPOSAL_SENTINEL_631f\n## Capabilities\n- `one`\n"
    )
    (config.change_dir / "use-cases.md").write_text("USE_CASE_SENTINEL_952d")
    context = cm.artifact_context(config.change_dir, "tasks")
    prompt = _build_prompt(
        "tasks", context, config, audit_findings="repair this" if repair else ""
    )
    assert "PROPOSAL_SENTINEL_631f" in prompt
    assert "USE_CASE_SENTINEL_952d" in prompt


def test_requirements_change_invalidates_design(plan):
    config, _, state = plan
    req = config.change_dir / "requirements/one.md"
    req.parent.mkdir(exist_ok=True)
    req.write_text(REQ)
    owner.reconcile_plan(config, state)
    req.write_text(REQ.replace("one", "two"))
    owner.reconcile_plan(config, state)
    assert "design" in state.spec_gen.stale_artifacts


def test_legacy_repair_flag_cannot_be_lost_when_step_evidence_already_exists():
    sg = SpecGenState.model_validate(
        {
            "plan_review_regen_done": True,
            "steps": {"plan_review": {"revision": "accepted", "outcome": "passed"}},
        }
    )
    assert sg.repair_attempts("plan_review") == 1
    assert sg.steps["plan_review"].revision == "accepted"
    with pytest.raises(RuntimeError, match="already attempted"):
        sg.admit_repair("plan_review")


@pytest.mark.asyncio
async def test_staged_repair_rejects_placeholders_even_when_model_audit_is_clean(
    plan, monkeypatch
):
    config, _, state = plan
    (config.change_dir / "requirements").mkdir()
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    before = (config.change_dir / "tasks.md").read_bytes()
    _rewrite_to(monkeypatch, tasks="## 1. Values\nError handling: TBD\nReturn values")
    monkeypatch.setattr(owner, "generate_rubric", AsyncMock(return_value="# Rubric"))
    with pytest.raises(ValueError, match="placeholder"):
        await owner.rewrite_plan(config, state, FIX)
    assert (config.change_dir / "tasks.md").read_bytes() == before
    assert not state.spec_gen.step_passed("plan_review")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_blocked_findings_survive_resume_without_repaying_same_review(
    plan, monkeypatch, stage
):
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult

    config, _, state = plan
    config.planning_max_rewrites = 1
    owner.reconcile_plan(config, state)
    state.spec_gen.admit_repair(stage)
    sentinel = "Missing rejection of the unique invalid account 891"
    review = AsyncMock(
        return_value=PlanReviewResult(
            findings=[
                PlanReviewFinding(
                    category="correctness",
                    severity="critical",
                    claim=sentinel,
                    reason="Add a rejection scenario",
                )
            ]
        )
    )
    monkeypatch.setattr(plan_review_steps, "run_plan_review", review)
    run = plan_review_steps.run_plan_review_stage
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await run(config.change_dir, config, state, config.state_file_path())
        state = BuildState.load(config.state_file_path())
        assert sentinel in state.spec_gen.steps[stage].reason
        assert sentinel in config.progress_file_path().read_text()
    assert review.await_count == 1


@pytest.mark.asyncio
async def test_repaired_unknowns_keep_their_accepted_evidence_on_resume(
    plan, monkeypatch
):
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    _rewrite_to(monkeypatch, design="# New design", tasks="## 1. Values\nReturn two")
    monkeypatch.setattr(
        owner, "generate_rubric", AsyncMock(return_value="# New rubric")
    )
    monkeypatch.setattr(owner, "_commit_change_dir", lambda *a: None)
    await owner.rewrite_plan(config, state, FIX)
    saved = BuildState.load(config.state_file_path())
    assert saved.spec_gen.step_passed("explainers")
    unknowns = (config.change_dir / "unknowns.md").read_bytes()
    provider = AsyncMock(
        side_effect=AssertionError("unchanged resume must not call a model")
    )
    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", provider
    )
    await owner._generate_all_artifacts(cm, config.change_dir, config, saved)
    assert (config.change_dir / "unknowns.md").read_bytes() == unknowns
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
@pytest.mark.parametrize("failure", ["audit", "commit"])
async def test_interrupted_post_repair_audit_resumes_on_the_new_revision(
    plan, monkeypatch, stage, failure
):
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import (
        PlanReviewFinding,
        PlanReviewResult,
        PriorAssessment,
    )

    config, _, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    before = state.spec_gen.revision

    writer = _rewrite_to(
        monkeypatch,
        design="# Repaired design",
        tasks="## 1. Values\nReturn repaired values",
    )
    monkeypatch.setattr(
        owner, "generate_rubric", AsyncMock(return_value="# New rubric")
    )
    monkeypatch.setattr(
        owner,
        "_commit_change_dir",
        Mock(side_effect=RuntimeError("commit interrupted"))
        if failure == "commit"
        else lambda *a: None,
    )
    review = AsyncMock(
        side_effect=[
            PlanReviewResult(
                findings=[
                    PlanReviewFinding(
                        severity="critical",
                        category="correctness",
                        claim="old gap",
                        reason="fix it",
                    )
                ]
            ),
            *([RuntimeError("recheck interrupted")] if failure == "audit" else []),
            PlanReviewResult(
                prior_assessments=[
                    PriorAssessment(finding_id="PR-F1", status="resolved")
                ]
            ),
        ]
    )
    monkeypatch.setattr(plan_review_steps, "run_plan_review", review)
    run = plan_review_steps.run_plan_review_stage
    with pytest.raises(RuntimeError, match="interrupted"):
        await run(config.change_dir, config, state, config.state_file_path())
    saved = BuildState.load(config.state_file_path())
    assert saved.spec_gen.revision != before
    assert saved.spec_gen.repair_attempts(stage) == 1
    writes = writer.await_count
    await run(config.change_dir, config, saved, config.state_file_path())
    assert saved.spec_gen.step_passed(stage)
    assert review.await_count == (3 if failure == "audit" else 2)
    assert writer.await_count == writes


# --- The recheck after a plan rewrite (plan-buildme-plan-recheck-2026-10-21.md) ---


def test_number_findings_gives_each_finding_an_id_and_text():
    found = [
        {
            "severity": "major",
            "category": "coverage",
            "description": "gap",
            "suggestion": "add it",
        },
        {"description": "no severity"},
    ]
    numbered = owner.number_findings("plan_review", found)
    assert [f["finding_id"] for f in numbered] == ["PR-F1", "PR-F2"]
    assert numbered[0]["text"] == "gap Fix: add it"
    assert (numbered[1]["severity"], numbered[1]["category"]) == ("?", "")
    assert "finding_id" not in found[0]


LISTED = [
    {
        "finding_id": f"DA-F{n}",
        "severity": "major",
        "category": "coverage",
        "text": f"gap {n}",
    }
    for n in (1, 2)
]


@pytest.mark.parametrize(
    "status,blocks",
    [
        ("resolved", None),
        ("partially_resolved", False),
        ("still_present", True),
        ("cannot_verify", False),
    ],
)
def test_recheck_verdict_blocks_on_a_listed_major_only_when_still_present(
    status, blocks
):
    """A listed major blocks only when `still_present`; partly resolved or
    unverifiable, it is advisory. `test_recheck_verdict_matrix` covers every
    severity."""
    from build_pipeline.models import PriorAssessment

    given = [
        PriorAssessment(
            finding_id="DA-F1", status=status, evidence="implementation-plan.md:3"
        ),
        PriorAssessment(finding_id="DA-F2", status="resolved"),
    ]
    blocking, advisory = owner.recheck_verdict(LISTED, given, [], deterministic=[])
    found = [("DA-F1", status)]
    assert [(f["finding_id"], f["status"]) for f in blocking] == (
        found if blocks else []
    )
    assert [(f["finding_id"], f["status"]) for f in advisory] == (
        found if blocks is False else []
    )


def test_recheck_verdict_reads_a_listed_finding_with_no_status_as_cannot_verify():
    from build_pipeline.models import PriorAssessment

    _, advisory = owner.recheck_verdict(
        LISTED,
        [PriorAssessment(finding_id="DA-F1", status="resolved")],
        [],
        deterministic=[],
    )
    assert [(f["finding_id"], f["status"], f["evidence"]) for f in advisory] == [
        ("DA-F2", "cannot_verify", "")
    ]


@pytest.mark.parametrize(
    "finding,blocks",
    [
        (
            {"severity": "critical", "category": "correctness", "description": "crash"},
            True,
        ),
        ({"severity": "major", "category": "correctness", "description": "gap"}, False),
        ({"severity": "minor", "category": "style", "description": "nit"}, False),
        # SKILL.md: a re-audit stops on a new critical problem; new major and
        # minor findings are reported only, whatever their category.
        (
            {
                "severity": "minor",
                "category": "no-new-behaviour",
                "description": "block 2 adds nothing",
            },
            False,
        ),
        (
            {
                "severity": "major",
                "category": "no-new-behaviour",
                "description": "block 3 adds nothing",
            },
            False,
        ),
        (
            {
                "severity": "critical",
                "category": "no-new-behaviour",
                "description": "block 4 adds nothing",
            },
            True,
        ),
    ],
)
def test_recheck_verdict_blocks_a_new_finding_only_when_critical(finding, blocks):
    blocking, advisory = owner.recheck_verdict([], [], [finding], deterministic=[])
    assert (len(blocking), len(advisory)) == ((1, 0) if blocks else (0, 1))
    assert (blocking or advisory)[0]["status"] == "new"
    assert (blocking or advisory)[0]["text"] == finding["description"]


def test_recheck_verdict_always_blocks_on_the_deterministic_scans():
    scan = {
        "severity": "minor",
        "category": "placeholder",
        "description": "TBD in block 1",
    }
    blocking, advisory = owner.recheck_verdict([], [], [], deterministic=[scan])
    assert [f["text"] for f in blocking] == ["TBD in block 1"]
    assert advisory == []


# Stop and resume rows S1-S11. A stop is a SIGKILL: the test puts back the
# checkpoint and progress file the process had written when it died, so no
# exception handler's record survives it.


class Killed(BaseException):
    """Stands in for the process dying at the point the test names."""


IDS = {"plan_review": "PR-F1"}
PLAN_FILES = (
    "proposal.md",
    "implementation-plan.md",
    "tasks.md",
    "unknowns.md",
    "rubric.md",
)


def _ready(plan):
    config, cm, state = plan
    (config.change_dir / "requirements").mkdir(exist_ok=True)
    for cap in ("one", "two"):
        (config.change_dir / f"requirements/{cap}.md").write_text(REQ)
    owner.reconcile_plan(config, state)
    state.checkpoint(config.state_file_path())
    return config, cm, state


def _writer(monkeypatch):
    writer = _rewrite_to(
        monkeypatch,
        design="# Repaired design",
        tasks="## 1. Values\nReturn repaired values",
    )
    monkeypatch.setattr(
        owner, "generate_rubric", AsyncMock(return_value="# New rubric")
    )
    monkeypatch.setattr(owner, "_commit_change_dir", lambda *a: None)
    return writer


def _plan_files(config):
    return {name: (config.change_dir / name).read_bytes() for name in PLAN_FILES}


def _first(stage, *gaps):
    """A first check's answer: (severity, text) per finding."""
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult

    return PlanReviewResult(
        findings=[
            PlanReviewFinding(
                category="correctness", severity=s, claim=t, reason="fix it"
            )
            for s, t in gaps
        ]
    )


def _recheck(stage, status, *gaps):
    """A recheck's answer: `status` for the first listed finding, plus new findings."""
    from build_pipeline.models import PlanReviewResult, PriorAssessment

    given = [
        PriorAssessment(
            finding_id=IDS[stage], status=status, evidence="tasks.md block 1"
        )
    ]
    return PlanReviewResult(
        findings=_first(stage, *gaps).findings, prior_assessments=given
    )


def _install(monkeypatch, stage, answers):
    """Stub the plan check with these answers in order. An answer that is a
    function is called (a `_Kill.die`). Returns (run, check stub)."""
    from build_pipeline.llm_steps import plan_review_steps

    queue = iter(answers)

    async def answer(*a, **kw):
        item = next(queue)
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item

    review = AsyncMock(side_effect=answer)
    monkeypatch.setattr(plan_review_steps, "run_plan_review", review)
    return plan_review_steps.run_plan_review_stage, review


async def _run(run, config, cm, state):
    return await run(config.change_dir, config, state, config.state_file_path())


class _Kill:
    """Kills the run where its `die` is called: saves the checkpoint and
    progress file as they are at that moment and puts them back after."""

    def __init__(self, config):
        self.config = config
        self.saved = None

    def die(self, *a, **kw):
        progress = self.config.progress_file_path()
        self.saved = (
            self.config.state_file_path().read_bytes(),
            progress.read_bytes() if progress.exists() else None,
        )
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
    monkeypatch.setattr(
        owner, "rewrite_plan", AsyncMock(side_effect=AssertionError("second rewrite"))
    )


async def _block_after_recheck(monkeypatch, plan, stage):
    """Run the stage to S4: the rewrite publishes, then the recheck says the
    listed finding is still present. Returns (config, cm, state, run, audit)."""
    config, cm, state = _ready(plan)
    _writer(monkeypatch)
    run, review = _install(
        monkeypatch,
        stage,
        [_first(stage, ("critical", "LISTED-GAP")), _recheck(stage, "still_present")],
    )
    with pytest.raises((owner.PlanBlocked, owner.PlanRepairRejected)):
        await _run(run, config, cm, state)
    return config, cm, BuildState.load(config.state_file_path()), run, review


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
@pytest.mark.parametrize("resumed", ["clean", "findings"])
async def test_S1_stop_after_admission_re_audits_without_a_second_rewrite(
    plan, monkeypatch, capsys, stage, resumed
):
    """S1: stopped after the admission checkpoint, before the journal. The
    resume re-audits the unchanged plan, passes when it is clean, and
    otherwise blocks on what it found with no second rewrite when one is the
    limit (row S3 of plan-buildme-planning-checks-2026-10-27.md)."""
    config, cm, state = _ready(plan)
    config.planning_max_rewrites = 1
    before = _plan_files(config)
    kill = _Kill(config)
    monkeypatch.setattr(owner, "rewrite_plan", kill.die)
    again = (
        _first(stage, ("critical", "SECOND-GAP"))
        if resumed == "findings"
        else _first(stage)
    )
    run, review = _install(
        monkeypatch, stage, [_first(stage, ("critical", "FIRST-GAP")), again]
    )
    saved = await _killed(run, config, cm, state, kill)
    step = saved.spec_gen.steps[stage]
    assert (step.outcome, step.reason, step.blocked_revision) == (
        "blocked",
        "Repair admitted; validation pending",
        None,
    )
    assert [f["finding_id"] for f in step.repair_findings] == [IDS[stage]]
    assert not (config.change_dir / ".plan-publication.json").exists()
    _no_rewrite(monkeypatch)
    capsys.readouterr()
    if resumed == "clean":
        await _run(run, config, cm, saved)
        assert saved.spec_gen.step_passed(stage)
    else:
        with pytest.raises(owner.PlanBlocked):
            await _run(run, config, cm, saved)
        step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
        assert step.outcome == "blocked"
        assert step.blocked_revision == saved.spec_gen.step_revision(stage)
        assert "SECOND-GAP" in step.blocking_findings
        assert "SECOND-GAP" in capsys.readouterr().out
    assert review.await_count == 2
    assert _plan_files(config) == before


async def _reference_run(plan_dir, monkeypatch, stage):
    """A run of the stage that never stopped: first audit finds one gap, the
    rewrite publishes, the recheck resolves it. Returns its plan files and step."""
    config = BuildConfig(
        project_dir=plan_dir, change_name="revision", skip_explainers=True
    )
    config.specs_dir = plan_dir / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    root = cm.create_change(config.change_name)
    for name, value in {
        "proposal.md": "## Capabilities\n- `one`\n- `two`\n",
        "use-cases.md": "# Use cases\nRead values",
        "implementation-plan.md": "# Design\nReturn values",
        "tasks.md": "## 1. Values\nReturn values",
        "unknowns.md": "# Unknowns\nNone",
        "rubric.md": "# Rubric\nCheck values",
    }.items():
        (root / name).write_text(value)
    state = BuildState(
        change_name=config.change_name,
        tier="advanced",
        spec_gen=SpecGenState(),
        state_file=str(config.state_file_path()),
    )
    config, cm, state = _ready((config, cm, state))
    run, _ = _install(
        monkeypatch,
        stage,
        [_first(stage, ("critical", "FIRST-GAP")), _recheck(stage, "resolved")],
    )
    await _run(run, config, cm, state)
    return _plan_files(config), BuildState.load(
        config.state_file_path()
    ).spec_gen.steps[stage]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S2_stop_during_publication_finishes_it_then_rechecks(
    plan, monkeypatch, tmp_path_factory, stage
):
    """S2: stopped with the journal written and tasks.md not yet replaced.
    The resume finishes the publication, then runs S3: the recheck with the
    listed findings and no new rewrite, ending as a run that never stopped."""
    config, cm, state = _ready(plan)
    writer = _writer(monkeypatch)
    kill = _Kill(config)
    original = owner.atomic_write

    def crash(path, content):
        if path == config.change_dir / "tasks.md":
            kill.die()
        original(path, content)

    monkeypatch.setattr(owner, "atomic_write", crash)
    run, review = _install(
        monkeypatch,
        stage,
        [_first(stage, ("critical", "FIRST-GAP")), _recheck(stage, "resolved")],
    )
    saved = await _killed(run, config, cm, state, kill)
    assert (config.change_dir / ".plan-publication.json").exists()
    monkeypatch.setattr(owner, "atomic_write", original)
    writes = writer.await_count
    await _run(run, config, cm, saved)
    assert writer.await_count == writes
    assert [f["finding_id"] for f in review.await_args.kwargs["prior"]] == [IDS[stage]]
    files, step = await _reference_run(
        tmp_path_factory.mktemp("reference"), monkeypatch, stage
    )
    resumed = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert not (config.change_dir / ".plan-publication.json").exists()
    assert _plan_files(config) == files
    assert resumed.model_dump(exclude={"revision"}) == step.model_dump(
        exclude={"revision"}
    )
    assert saved.spec_gen.step_passed(stage)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S3_stop_during_the_recheck_reruns_it_with_the_listed_findings(
    plan, monkeypatch, tmp_path_factory, stage
):
    """S3: stopped after the publication, while the recheck runs. The resume
    reruns the recheck with `prior=repair_findings` and no new rewrite; the
    step ends as in a run that never stopped."""
    config, cm, state = _ready(plan)
    writer = _writer(monkeypatch)
    kill = _Kill(config)
    run, review = _install(
        monkeypatch,
        stage,
        [
            _first(stage, ("critical", "FIRST-GAP")),
            kill.die,
            _recheck(stage, "resolved"),
        ],
    )
    saved = await _killed(run, config, cm, state, kill)
    step = saved.spec_gen.steps[stage]
    assert (step.repair_attempts, step.blocked_revision) == (1, None)
    owner.reopen_if_edited(saved, stage, config.state_file_path())
    assert saved.spec_gen.steps[stage].repair_attempts == 1
    writes = writer.await_count
    await _run(run, config, cm, saved)
    assert writer.await_count == writes
    assert [f["finding_id"] for f in review.await_args.kwargs["prior"]] == [IDS[stage]]
    files, reference = await _reference_run(
        tmp_path_factory.mktemp("reference"), monkeypatch, stage
    )
    resumed = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert _plan_files(config) == files
    assert resumed.model_dump(exclude={"revision"}) == reference.model_dump(
        exclude={"revision"}
    )
    assert resumed.repair_findings[0]["status"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S4_a_blocking_recheck_refuses_the_resume_with_its_findings(
    plan, monkeypatch, capsys, stage
):
    """S4: the recheck blocks. The checkpoint, the progress file and the
    console show the listed finding and its status; a resume at the same
    revision refuses with no model call and ends with the edit line."""
    config, cm, state, run, review = await _block_after_recheck(
        monkeypatch, plan, stage
    )
    step = state.spec_gen.steps[stage]
    assert step.outcome == "blocked"
    assert step.blocked_revision == state.spec_gen.step_revision(stage)
    assert f"{IDS[stage]} still_present" in step.blocking_findings
    assert [(f["finding_id"], f["status"]) for f in step.repair_findings] == [
        (IDS[stage], "still_present")
    ]
    assert f"{stage.upper()}_BLOCKED" in config.progress_file_path().read_text()
    assert "LISTED-GAP" in capsys.readouterr().out
    calls, before = review.await_count, _plan_files(config)
    with pytest.raises(owner.PlanBlocked) as refused:
        await _run(run, config, cm, state)
    assert str(refused.value).endswith(owner.REOPEN_LINE)
    assert f"{IDS[stage]} still_present" in str(refused.value)
    assert owner.REOPEN_LINE in capsys.readouterr().out
    assert review.await_count == calls
    assert _plan_files(config) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S5_an_edited_plan_gets_its_rewrites_back(
    plan, monkeypatch, caplog, stage
):
    """S5: the plan was edited after the block. The resume reopens the step
    (count reset, both new fields cleared, one log line), then runs it as a
    first audit, whose rewrites are counted from zero."""
    config, cm, state, run, review = await _block_after_recheck(
        monkeypatch, plan, stage
    )
    (config.change_dir / "implementation-plan.md").write_text(
        "# Design\nEdited by the user"
    )
    owner.reconcile_plan(config, state)
    writer = _writer(monkeypatch)
    run, review = _install(
        monkeypatch,
        stage,
        [_first(stage, ("critical", "AFTER-EDIT")), _recheck(stage, "resolved")],
    )
    with caplog.at_level("WARNING"):
        await _run(run, config, cm, state)
    assert (
        f"{stage}: plan changed since it was blocked; its rewrites are counted again"
        in caplog.text
    )
    assert writer.await_count >= 1
    assert review.await_args_list[0].kwargs.get("prior") is None
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert step.outcome == "passed" and step.blocked_revision is None
    assert [(f["finding_id"], f["status"]) for f in step.repair_findings] == [
        (IDS[stage], "resolved")
    ]
    assert "AFTER-EDIT" in step.repair_findings[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S6_repeated_resumes_refuse_alike_and_a_new_block_replaces_the_old(
    plan, monkeypatch, stage
):
    """S6: each resume after S4 refuses the same way with no spend and no
    write. After an S5 reopen blocks again, the new record replaces the old."""
    config, cm, state, run, review = await _block_after_recheck(
        monkeypatch, plan, stage
    )
    checkpoint, calls = config.state_file_path().read_bytes(), review.await_count
    messages = []
    for _ in range(2):
        with pytest.raises(owner.PlanBlocked) as refused:
            await _run(run, config, cm, BuildState.load(config.state_file_path()))
        messages.append(str(refused.value))
    assert messages[0] == messages[1]
    assert config.state_file_path().read_bytes() == checkpoint
    assert review.await_count == calls
    (config.change_dir / "implementation-plan.md").write_text(
        "# Design\nEdited by the user"
    )
    _writer(monkeypatch)
    run, review = _install(
        monkeypatch,
        stage,
        [_first(stage, ("critical", "NEW-GAP")), _recheck(stage, "still_present")],
    )
    state = BuildState.load(config.state_file_path())
    with pytest.raises((owner.PlanBlocked, owner.PlanRepairRejected)):
        await _run(run, config, cm, state)
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert step.blocked_revision == state.spec_gen.step_revision(stage)
    assert "NEW-GAP" in step.repair_findings[0]["text"]
    assert "LISTED-GAP" not in step.blocking_findings


@pytest.mark.asyncio
async def test_S7_this_change_adds_no_lock_against_a_second_run(plan, monkeypatch):
    """S7: buildme has no guard against two builds of one change outside an
    epic (the epic lock, epic/lock.py, covers only the epic directory). This
    PR adds none: a second run started while the first waits on its check
    reaches its own check call, and no lock file is left behind."""
    import asyncio

    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanReviewResult

    config, cm, state = _ready(plan)
    release, started = asyncio.Event(), []

    async def check(*a, **kw):
        started.append(1)
        await release.wait()
        return PlanReviewResult()

    monkeypatch.setattr(plan_review_steps, "run_plan_review", check)
    first = asyncio.create_task(
        plan_review_steps.run_plan_review_stage(
            config.change_dir, config, state, config.state_file_path()
        )
    )
    while not started:
        await asyncio.sleep(0)
    second = asyncio.create_task(
        plan_review_steps.run_plan_review_stage(
            config.change_dir,
            config,
            BuildState.load(config.state_file_path()),
            config.state_file_path(),
        )
    )
    while len(started) < 2:
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert not list(config.project_dir.rglob("*.lock"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S8_moved_git_refs_do_not_change_the_resume(plan, monkeypatch, stage):
    """S8: origin or the base branch moved between stop and resume. Step
    revisions hash plan files, not refs, so the resume still refuses as S4."""
    import subprocess

    config, cm, state, run, review = await _block_after_recheck(
        monkeypatch, plan, stage
    )
    revision = state.spec_gen.step_revision(stage)
    git = [
        "git",
        "-C",
        str(config.project_dir),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
    ]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    (config.project_dir / "README.md").write_text("moved")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "moved"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    state = BuildState.load(config.state_file_path())
    owner.reconcile_plan(config, state)
    assert state.spec_gen.step_revision(stage) == revision
    calls = review.await_count
    with pytest.raises(owner.PlanBlocked, match="Edit the plan"):
        await _run(run, config, cm, state)
    assert review.await_count == calls


def _legacy_checkpoint(config, state, stage, **step):
    """Write a checkpoint as a build before this change saved it: the step has
    no `repair_findings` or `blocked_revision` key."""
    state.checkpoint(config.state_file_path())
    raw = json.loads(config.state_file_path().read_text())
    raw["spec_gen"]["steps"][stage] = step
    config.state_file_path().write_text(json.dumps(raw))
    return BuildState.load(config.state_file_path())


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S9_an_old_blocked_checkpoint_reopens_once_under_the_new_rule(
    plan, monkeypatch, stage
):
    """A checkpoint from before `rules_version`, blocked on findings, loads
    with the defaults; the reopen clears its rewrite count and saved findings
    once (row S9 of plan-buildme-planning-checks-2026-10-27.md, which replaces
    this test's earlier "refuses as before"), so the next run checks again."""
    config, cm, state = _ready(plan)
    saved = _legacy_checkpoint(
        config,
        state,
        stage,
        revision=state.spec_gen.step_revision(stage),
        outcome="blocked",
        reason="still blocked",
        repair_attempts=1,
        blocking_findings="- [critical] OLD-FINDING",
    )
    step = saved.spec_gen.steps[stage]
    assert (step.repair_findings, step.blocked_revision, step.rules_version) == (
        [],
        None,
        0,
    )
    owner.reopen_if_edited(saved, stage, config.state_file_path())
    step = saved.spec_gen.steps[stage]
    assert (step.repair_attempts, step.blocking_findings, step.rules_version) == (
        0,
        "",
        owner.PLAN_RULES_VERSION,
    )
    owner.refuse_if_blocked(saved.spec_gen, stage)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan_review"])
async def test_S10_an_old_mid_rewrite_checkpoint_rechecks_as_a_first_audit(
    plan, monkeypatch, stage
):
    """S10: a checkpoint from before this change, stopped mid-rewrite after
    the publication (S1 shape, no `repair_findings`). Recorded before
    `rules_version`, it is reopened once (row S9 of
    plan-buildme-planning-checks-2026-10-27.md): the check runs with no listed
    findings, and a major finding earns a fresh rewrite."""
    config, cm, state = _ready(plan)
    saved = _legacy_checkpoint(
        config,
        state,
        stage,
        revision="revision-before-the-rewrite",
        outcome="blocked",
        reason="Repair admitted; validation pending",
        repair_attempts=1,
    )
    assert not owner.rewrite_published(saved.spec_gen, stage)
    rewrite = AsyncMock(side_effect=Killed())
    monkeypatch.setattr(owner, "rewrite_plan", rewrite)
    run, review = _install(monkeypatch, stage, [_first(stage, ("major", "MAJOR-GAP"))])
    with pytest.raises(Killed):
        await _run(run, config, cm, saved)
    assert review.await_args_list[0].kwargs.get("prior") is None
    rewrite.assert_awaited_once()
    step = BuildState.load(config.state_file_path()).spec_gen.steps[stage]
    assert step.repair_attempts == 1


@pytest.mark.asyncio
async def test_a_blocking_recheck_keeps_its_findings_and_a_progress_line(
    plan, monkeypatch
):
    """A rewrite whose recheck still finds the listed finding saves it as
    blocking and writes PLAN_REVIEW_BLOCKED."""
    config, cm, state, *_ = await _block_after_recheck(monkeypatch, plan, "plan_review")
    step = state.spec_gen.steps["plan_review"]
    assert "PR-F1 still_present" in step.blocking_findings
    assert "LISTED-GAP" in step.blocking_findings
    progress = config.progress_file_path().read_text()
    assert "PLAN_REVIEW_BLOCKED" in progress and "LISTED-GAP" in progress


def _old_audit_steps(config, state, **plan_review):
    """A checkpoint saved before the one plan check: tasks_audit and
    design_audit entries, and a plan_review tied to the whole plan revision."""
    state.checkpoint(config.state_file_path())
    raw = json.loads(config.state_file_path().read_text())
    steps = raw["spec_gen"]["steps"]
    for name in ("tasks_audit", "design_audit"):
        steps[name] = {
            "revision": raw["spec_gen"]["revision"],
            "outcome": "passed",
            "repair_attempts": 1,
        }
    if plan_review:
        steps["plan_review"] = plan_review
    config.state_file_path().write_text(json.dumps(raw))
    return BuildState.load(config.state_file_path())


@pytest.mark.asyncio
async def test_S10_old_audit_steps_are_dropped_and_a_passed_check_needs_no_call(
    plan, monkeypatch
):
    """Row S10 of plan-buildme-planning-checks-2026-10-27.md: the old steps
    are dropped on load, and a plan_review that passed at the current plan
    stays passed, so the resume makes no model call."""
    from build_pipeline.llm_steps import plan_review_steps

    config, cm, state = _ready(plan)
    saved = _old_audit_steps(
        config,
        state,
        revision=state.spec_gen.revision,
        outcome="passed",
        rules_version=owner.PLAN_RULES_VERSION,
    )
    assert set(saved.spec_gen.steps) & {"tasks_audit", "design_audit"} == set()
    assert saved.spec_gen.step_passed("plan_review")
    monkeypatch.setattr(
        plan_review_steps,
        "run_plan_review",
        AsyncMock(side_effect=AssertionError("a passed check is not paid again")),
    )
    await plan_review_steps.run_plan_review_stage(
        config.change_dir, config, saved, config.state_file_path()
    )


@pytest.mark.asyncio
async def test_S10_old_audit_steps_with_no_plan_review_get_a_fresh_check(
    plan, monkeypatch
):
    config, cm, state = _ready(plan)
    saved = _old_audit_steps(config, state)
    run, review = _install(monkeypatch, "plan_review", [_first("plan_review")])
    await _run(run, config, cm, saved)
    assert review.await_count == 1
    assert review.await_args.kwargs.get("prior") is None
    assert saved.spec_gen.step_passed("plan_review")


@pytest.mark.asyncio
async def test_S11_an_old_rewrite_stopped_mid_publication_finishes_then_checks(
    plan, monkeypatch
):
    """Row S11: the old code's journal is finished as in S5, the old steps are
    dropped (S10), and the one check runs fresh."""
    config, cm, state = _ready(plan)
    _writer(monkeypatch)
    original = owner.atomic_write

    def interrupted(path, content):
        if path == config.change_dir / "tasks.md":
            raise OSError("crash during publication")
        original(path, content)

    monkeypatch.setattr(owner, "atomic_write", interrupted)
    with pytest.raises(OSError, match="crash"):
        await owner.rewrite_plan(config, state, FIX, step="plan_review")
    monkeypatch.setattr(owner, "atomic_write", original)
    # The old code's journal named only the four plan files.
    path = config.change_dir / ".plan-publication.json"
    journal = json.loads(path.read_text())
    journal.pop("plan-edits-1.diff")
    path.write_text(json.dumps(journal))
    assert set(journal) <= {
        "implementation-plan.md",
        "tasks.md",
        "unknowns.md",
        "rubric.md",
    }
    saved = _old_audit_steps(
        config,
        BuildState.load(config.state_file_path()),
    )
    run, review = _install(monkeypatch, "plan_review", [_first("plan_review")])
    await _run(run, config, cm, saved)
    assert not (config.change_dir / ".plan-publication.json").exists()
    assert (config.change_dir / "tasks.md").read_text() == (
        "## 1. Values\nReturn repaired values"
    )
    assert set(saved.spec_gen.steps) & {"tasks_audit", "design_audit"} == set()
    assert review.await_count == 1
    assert saved.spec_gen.step_passed("plan_review")


# --- config.yaml sections no planning step reads (rows S1, S3, S4 of
# plan-buildme-subdir-narrow-tests-2026-10-28.md) -----------------------------

CONFIG = (
    "project:\n  name: x\n"
    "tdd:\n  test_command: pytest\n"
    "epic:\n  max_parallel: 2\n"
    "git:\n  push: true\n"
)


def _in_tdd(plan):
    """A plan reconciled once, with a build in TDD."""
    from build_pipeline.state import TDDState

    config, _, state = plan
    (config.specs_dir / "config.yaml").write_text(CONFIG)
    owner.reconcile_plan(config, state)
    state.tdd = TDDState()
    return config, state


def test_S3_editing_tdd_epic_or_git_keeps_the_revision_and_marks_nothing_stale(plan):
    from build_pipeline.orchestrator import reconcile_kept_implementation

    config, state = _in_tdd(plan)
    revision, stale = state.spec_gen.revision, list(state.spec_gen.stale_artifacts)
    (config.specs_dir / "config.yaml").write_text(
        CONFIG.replace("test_command: pytest", "test_command: pytest -x")
        .replace("max_parallel: 2", "max_parallel: 3")
        .replace("push: true", "push: false")
    )
    reconcile_kept_implementation(config, state)
    assert state.spec_gen.revision == revision
    assert state.spec_gen.stale_artifacts == stale


def test_S3_adding_pipeline_viewer_settings_keeps_the_revision(plan):
    config, state = _in_tdd(plan)
    revision = state.spec_gen.revision
    (config.specs_dir / "config.yaml").write_text(
        CONFIG + "pipeline_viewer:\n  slack:\n    channel: dev-updates\n"
    )
    owner.reconcile_plan(config, state)
    assert state.spec_gen.revision == revision


@pytest.mark.parametrize(
    "section",
    [
        "findings:\n  authors: [someone]\n",
        "code_review:\n  fix_on_pass: false\n  per_block: false\n",
        "test_quality:\n  enabled: false\n",
    ],
)
def test_adding_settings_of_the_steps_after_planning_keeps_the_revision(plan, section):
    """G25 (plan-factory-merged-2026-10-29.md, item 31): `findings:`,
    `code_review:` and `test_quality:` are read after planning."""
    config, state = _in_tdd(plan)
    revision, stale = state.spec_gen.revision, list(state.spec_gen.stale_artifacts)
    (config.specs_dir / "config.yaml").write_text(CONFIG + section)
    owner.reconcile_plan(config, state)
    assert state.spec_gen.revision == revision
    assert state.spec_gen.stale_artifacts == stale


def test_editing_memory_files_still_changes_the_revision(plan):
    """`memory_files.stacks` picks the reference docs planning inlines, so it
    stays in the plan's hash (item 31)."""
    config, state = _in_tdd(plan)
    revision = state.spec_gen.revision
    (config.specs_dir / "config.yaml").write_text(
        CONFIG + "memory_files:\n  stacks:\n    django: [django.md]\n"
    )
    owner.reconcile_plan(config, state)
    assert state.spec_gen.revision != revision


def test_S3_adding_narrow_commands_keeps_the_revision(plan):
    config, state = _in_tdd(plan)
    revision = state.spec_gen.revision
    (config.specs_dir / "config.yaml").write_text(
        CONFIG.replace(
            "tdd:\n",
            "tdd:\n  narrow_commands:\n    - dir: backend\n      command: pytest\n",
        )
    )
    owner.reconcile_plan(config, state)
    assert state.spec_gen.revision == revision


def test_S4_editing_a_key_outside_those_sections_still_stops_the_build(plan):
    from build_pipeline.orchestrator import reconcile_kept_implementation

    config, state = _in_tdd(plan)
    revision = state.spec_gen.revision
    (config.specs_dir / "config.yaml").write_text(CONFIG.replace("name: x", "name: y"))
    with pytest.raises(
        RuntimeError, match="Planning inputs changed after implementation"
    ):
        reconcile_kept_implementation(config, state)
    assert state.spec_gen.revision != revision
    assert "design" in state.spec_gen.stale_artifacts


def test_S1_a_revision_saved_by_an_older_version_stops_the_build_once(plan):
    """A build planned on 0.124.0 or earlier hashed the tdd/epic/git text into
    its revision; resuming it in TDD raises the existing error."""
    import hashlib

    from build_pipeline.orchestrator import reconcile_kept_implementation

    config, state = _in_tdd(plan)
    sg = state.spec_gen
    old = dict(sg.artifact_hashes)
    old["config.yaml"] = hashlib.sha256(CONFIG.encode()).hexdigest()
    sg.artifact_hashes = old
    sg.revision = hashlib.sha256(
        json.dumps(
            {k: v for k, v in old.items() if k != owner.PLANNING_KEY}, sort_keys=True
        ).encode()
    ).hexdigest()
    with pytest.raises(
        RuntimeError,
        match="Planning inputs changed after implementation; start a new build "
        "to preserve completed work",
    ):
        reconcile_kept_implementation(config, state)


def test_split_sections_cuts_each_named_top_level_section():
    text = "a: 1\ntdd:\n  x: 1\n\nb: 2\ngit:\n  push: false\nplanning:\n  max_usd: 9\n"
    rest, sections = owner._split_sections(text, ("planning", "tdd", "git"))
    assert rest == "a: 1\nb: 2\n"
    assert sections == {
        "tdd": "tdd:\n  x: 1\n\n",
        "git": "git:\n  push: false\n",
        "planning": "planning:\n  max_usd: 9\n",
    }


def test_S1_the_printed_values_rebind_an_older_build(plan):
    """`plan-revision` prints what an older build's checkpoint needs; written
    in, the build resumes in TDD with its plan check still passed."""
    import hashlib

    from build_pipeline.orchestrator import reconcile_kept_implementation
    from build_pipeline.state import _inputs_revision

    config, state = _in_tdd(plan)
    sg = state.spec_gen
    old = dict(sg.artifact_hashes)
    old["config.yaml"] = hashlib.sha256(CONFIG.encode()).hexdigest()
    sg.artifact_hashes = old
    sg.revision = owner._revision(old)
    sg.record_step(
        "plan_review", "passed", revision=_inputs_revision("plan_review", old)
    )
    state.checkpoint(config.state_file_path())
    before = config.state_file_path().read_bytes()

    values = owner.rebound_values(config)
    assert config.state_file_path().read_bytes() == before  # writes nothing
    assert set(values) == {
        "artifact_hashes.config.yaml",
        "revision",
        "steps.plan_review.revision",
    }
    sg.artifact_hashes["config.yaml"] = values["artifact_hashes.config.yaml"]
    sg.revision = values["revision"]
    sg.steps["plan_review"].revision = values["steps.plan_review.revision"]
    reconcile_kept_implementation(config, state)
    assert sg.step_passed("plan_review")
