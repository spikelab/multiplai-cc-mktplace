"""Risky LOW findings are verified (`verify_low: risky`), convert drops no-problem notes, and a merge
group holds at most `MERGE_GROUP_MAX` findings. Stop rows S36 and S37 of the factory plan."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import KEYWORD_USE_CITATION, high_finding, medium_finding
from review_pipeline import sdk
from review_pipeline.__main__ import build_parser
from review_pipeline.config import ReviewConfig, load_config
from review_pipeline.models import (
    Citation,
    ConvertItem,
    ConvertOutput,
    DuplicateSet,
    Finding,
    LowSelectOutput,
    MergeOutput,
    ReviewState,
    Verdict,
)
from review_pipeline.prompts.convert import build_convert
from review_pipeline.prompts.low_select import build as build_low_select
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import drop_no_problem, map_converted
from review_pipeline.stages.merge import (
    MERGE_GROUP_MAX,
    group_key,
    link_groups,
    overlap_groups,
    run_merge,
)
from review_pipeline.stages.verify import run_verify
from review_pipeline.state import STATE_FILE, load_state

LOW_SELECT = "verify:low-select"


class Fake:
    """Stands in for sdk.agent_call_structured: `answers[label]` is consumed in order; an Exception
    in it is raised. Records each call's label, prompt and tools."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[tuple[str, str, list]] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt, kwargs["allowed_tools"]))
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return (
            schema.model_validate(answer.model_dump())
            if hasattr(answer, "model_dump")
            else answer
        )

    def labels(self) -> list[str]:
        return [c[0] for c in self.calls]


def use(monkeypatch, answers) -> Fake:
    fake = Fake(answers)
    monkeypatch.setattr(sdk, "agent_call_structured", fake)
    return fake


@pytest.fixture
def ctx(target_info, tmp_path, monkeypatch) -> RunContext:
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    return RunContext(
        config=ReviewConfig(concurrency=1, verify_low="risky"),
        snapshot=snapshot,
        diff=Path(target_info.diff_path).read_text(),
        target_dir=out,
    )


def lows() -> list[Finding]:
    """Three LOW findings: a crash, a data loss and a naming nit."""
    base = high_finding()
    return [
        base.with_location(
            severity="LOW",
            claim="An empty title raises KeyError",
            failure_scenario="A rate plan with no title crashes the search with a 500.",
        ),
        base.with_location(
            severity="LOW",
            claim="A failed save drops the booking",
            failure_scenario="The save fails and the booking is lost.",
        ),
        base.with_location(
            severity="LOW",
            claim="The constant is badly named",
            failure_scenario="A reader may misread the name.",
        ),
    ]


def confirmed() -> Verdict:
    return Verdict(
        status="confirmed",
        impact="breaks-users",
        topic="code",
        reason="it does",
        citations=[KEYWORD_USE_CITATION],
        expected_behaviour="It matches.",
    )


def verify_state(target_info) -> ReviewState:
    return ReviewState(
        target=target_info, stage="find", findings=[*lows(), medium_finding()]
    )


# --- item 43: config and flag ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [("verify_low: risky\n", "risky"), ("verify_low: true\n", True), ("", False)],
)
def test_review_yaml_verify_low_reads_risky_and_keeps_true_and_false(
    tmp_path, text, want
):
    (tmp_path / "review.yaml").write_text(text)
    assert load_config(tmp_path).verify_low == want


@pytest.mark.parametrize(
    ("flag", "want"),
    [([], False), (["--verify-low"], True), (["--verify-low", "risky"], "risky")],
)
def test_the_verify_low_flag_takes_an_optional_risky(flag, want):
    args = build_parser().parse_args(
        ["--out", "/x", "--session-id", "s", "resume", "/out/r", *flag]
    )
    assert args.verify_low == want


def test_the_verify_low_flag_refuses_another_value(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--out", "/x", "--session-id", "s", "review", "--verify-low", "all"]
        )
    assert "the only value is 'risky'" in capsys.readouterr().err


# --- item 43: the selection call ------------------------------------------------------


async def test_risky_sends_only_the_chosen_lows_to_the_verifier(
    target_info, ctx, monkeypatch
):
    crash, loss, nit = lows()
    fake = use(
        monkeypatch,
        {
            LOW_SELECT: [LowSelectOutput(finding_ids=[loss.id, crash.id])],
            "verify": [confirmed()] * 3,
        },
    )
    state = await run_verify(verify_state(target_info), ctx)
    assert fake.labels() == [LOW_SELECT, "verify", "verify", "verify"]
    _, prompt, tools = fake.calls[0]
    assert tools == [] and "<untrusted-content" in prompt
    assert all(f.claim in prompt and f.failure_scenario in prompt for f in lows())
    assert medium_finding().claim not in prompt  # only LOW findings are read
    assert state.low_selected == [crash.id, loss.id]
    assert state.verdicts[crash.id].status == "confirmed"
    assert state.verdicts[loss.id].status == "confirmed"
    assert state.verdicts[nit.id].status == "unverified"
    (check,) = [c for c in state.checks if c.subject == "low-selection"]
    assert check.stage == "verify" and check.outcome.startswith("2 of 3")


async def test_the_selection_is_saved_before_any_verifier_runs(
    target_info, ctx, monkeypatch
):
    crash, _, nit = lows()
    use(
        monkeypatch,
        {
            LOW_SELECT: [LowSelectOutput(finding_ids=[crash.id])],
            "verify": [sdk.UsageLimitError("limit")],
        },
    )
    with pytest.raises(sdk.UsageLimitError):
        await run_verify(verify_state(target_info), ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert saved.low_selected == [crash.id]
    assert saved.verdicts[nit.id].status == "unverified"
    assert crash.id not in saved.verdicts


async def test_an_id_that_is_not_a_low_is_dropped_with_an_error_line(
    target_info, ctx, monkeypatch
):
    crash = lows()[0]
    medium = medium_finding()
    use(
        monkeypatch,
        {
            LOW_SELECT: [LowSelectOutput(finding_ids=[crash.id, medium.id, "nope"])],
            "verify": [confirmed()] * 2,
        },
    )
    state = await run_verify(verify_state(target_info), ctx)
    assert state.low_selected == [crash.id]
    (line,) = [e for e in state.errors if "LOW selection" in e]
    assert medium.id in line and "nope" in line


async def test_a_failed_selection_verifies_no_low_and_says_so(
    target_info, ctx, monkeypatch
):
    fake = use(
        monkeypatch,
        {
            LOW_SELECT: [sdk.AgentCallError("bad answer")],
            "verify": [confirmed()],
        },
    )
    state = await run_verify(verify_state(target_info), ctx)
    assert fake.labels() == [LOW_SELECT, "verify"]
    assert state.low_selected == []
    assert {state.verdicts[f.id].status for f in lows()} == {"unverified"}
    assert any("LOW selection failed" in e for e in state.errors)


async def test_with_verify_low_off_no_selection_call_is_made(
    target_info, ctx, monkeypatch
):
    ctx.config.verify_low = False
    fake = use(monkeypatch, {"verify": [confirmed()]})
    state = await run_verify(verify_state(target_info), ctx)
    assert fake.labels() == ["verify"] and state.low_selected is None


def test_the_selection_prompt_names_the_risks_and_fences_the_findings():
    prompt = build_low_select(lows())
    for words in ("crash", "error response", "access", "exposed", "lost"):
        assert words in prompt
    assert "do not flag" not in prompt.lower()


async def test_s36_selection_answered_but_not_saved_asks_again_once(
    target_info, ctx, monkeypatch
):
    """Row S36: stopped after the selection call answered, before the save. The saved state has no
    `low_selected`, so the resume asks again, once, and ends as a run that never stopped."""
    crash, loss, _ = lows()
    answer = LowSelectOutput(finding_ids=[crash.id, loss.id])

    # A run that never stopped.
    use(monkeypatch, {LOW_SELECT: [answer], "verify": [confirmed()] * 3})
    whole = await run_verify(verify_state(target_info), ctx)

    # The stopped run: the last checkpoint is the one before the stage; the answer was never saved.
    from review_pipeline.state import save_state

    first = verify_state(target_info)
    save_state(first, ctx.target_dir)
    killed = use(monkeypatch, {LOW_SELECT: [answer]})

    from review_pipeline.stages import verify as verify_mod

    def kill(state, ctx):
        raise KeyboardInterrupt

    monkeypatch.setattr(verify_mod, "save", kill)
    with pytest.raises(KeyboardInterrupt):
        await run_verify(first, ctx)
    assert killed.labels() == [LOW_SELECT]
    monkeypatch.undo()
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")

    saved = load_state(ctx.target_dir / STATE_FILE)
    assert saved.low_selected is None and not saved.verdicts
    resumed = use(monkeypatch, {LOW_SELECT: [answer], "verify": [confirmed()] * 3})
    state = await run_verify(saved, ctx)
    assert resumed.labels().count(LOW_SELECT) == 1
    assert state.low_selected == whole.low_selected
    assert {k: v.status for k, v in state.verdicts.items()} == {
        k: v.status for k, v in whole.verdicts.items()
    }
    assert [f.model_dump() for f in state.findings] == [
        f.model_dump() for f in whole.findings
    ]


async def test_a_resume_after_the_save_makes_no_selection_call(
    target_info, ctx, monkeypatch
):
    crash = lows()[0]
    use(
        monkeypatch,
        {
            LOW_SELECT: [LowSelectOutput(finding_ids=[crash.id])],
            "verify": [sdk.UsageLimitError("limit")],
        },
    )
    with pytest.raises(sdk.UsageLimitError):
        await run_verify(verify_state(target_info), ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    fake = use(monkeypatch, {"verify": [confirmed()] * 2})
    state = await run_verify(saved, ctx)
    assert LOW_SELECT not in fake.labels()
    assert state.verdicts[crash.id].status == "confirmed"


# --- item 44: convert drops notes that report no problem ------------------------------

# The text of finding 434ba8de0f (Dolcebot PR #37's review, 2026-10-29): a converted "finding" whose
# claim says the reviewer found no problem.
NO_PROBLEM_CLAIM = (
    "The reviewer checked the switch to crea_profilo against its callees and found no problem; "
    "the operatore fixture correctly keeps using Utente.objects.create with no client link."
)


def _item(claim: str, kind: str = "defect", **kw) -> ConvertItem:
    fields = {
        "file": "rateplan_service.py",
        "line_start": KEYWORD_USE_CITATION.line_start,
        "line_end": KEYWORD_USE_CITATION.line_end,
        "citations": [KEYWORD_USE_CITATION],
        "severity": "LOW",
    }
    fields.update(kw)
    return ConvertItem(kind=kind, claim=claim, **fields)


def test_a_defect_whose_claim_says_no_problem_was_found_is_dropped_with_an_error_line():
    real = _item("A failed save drops the booking")
    out, errors = drop_no_problem(
        "callers@1", ConvertOutput(items=[_item(NO_PROBLEM_CLAIM), real])
    )
    assert out.items == [real]
    (line,) = errors
    assert line.startswith(
        "convert callers@1: dropped a defect item that reports no problem"
    )
    items, findings = map_converted("callers@1", out)
    assert [f.claim for f in findings] == [real.claim] and len(items) == 1


@pytest.mark.parametrize(
    "claim",
    [
        "No issues were found in the migration",
        "The checker found nothing wrong with the fixture",
    ],
)
def test_other_no_problem_wordings_are_dropped(claim):
    out, errors = drop_no_problem("diff-bugs@2", ConvertOutput(items=[_item(claim)]))
    assert out.items == [] and len(errors) == 1


@pytest.mark.parametrize(
    "claim",
    [
        "The validator reports no errors for an empty file, so bad data is saved",
        "A missing title raises KeyError",
    ],
)
def test_a_defect_that_reports_a_problem_is_kept(claim):
    out, errors = drop_no_problem("diff-bugs@2", ConvertOutput(items=[_item(claim)]))
    assert len(out.items) == 1 and errors == []


def test_advice_with_no_file_is_kept_as_an_item_and_makes_no_finding():
    advice = _item(
        NO_PROBLEM_CLAIM,
        kind="advice",
        file=None,
        line_start=None,
        line_end=None,
        citations=[],
    )
    out, errors = drop_no_problem("callers@1", ConvertOutput(items=[advice]))
    assert errors == []
    items, findings = map_converted("callers@1", out)
    assert [i.kind for i in items] == ["advice"] and findings == []


def test_the_convert_prompt_marks_no_problem_notes_as_advice_with_no_file():
    prompt = build_convert("callers@1", "report")
    assert "found no problem is not a defect: it is `advice`" in prompt
    assert "do not flag" not in prompt.lower()


# --- item 45: merge groups hold at most MERGE_GROUP_MAX findings ----------------------

SHARED = Citation(path="shared.py", line_start=1, line_end=1, quote="x = 1")


def chained(file: str, n: int) -> list[Finding]:
    """*n* findings in *file* on lines 1, 4, 7, …, each citing `shared.py:1`, so all of them link."""
    return [
        Finding(
            claim=f"{file} defect {k}",
            severity="MEDIUM",
            dimension="diff-bugs",
            file=file,
            line_start=1 + 3 * k,
            line_end=1 + 3 * k,
            failure_scenario="it breaks",
            citations=[
                Citation(
                    path=file,
                    line_start=1 + 3 * k,
                    line_end=1 + 3 * k,
                    quote=f"line {k}",
                ),
                SHARED,
            ],
            finder="diff-bugs",
        )
        for k in range(n)
    ]


def test_merge_group_max_is_40():
    assert MERGE_GROUP_MAX == 40


def test_a_large_group_is_split_by_file_then_by_line_order():
    a, b, c = chained("a.py", 90), chained("b.py", 30), chained("c.py", 1)
    findings = [*a, *b, *c]
    assert len(link_groups(findings)) == 1  # without the cap: one group of 121
    groups = link_groups(findings, max_size=MERGE_GROUP_MAX)
    assert [len(g) for g in groups] == [30, 30, 30, 30]
    assert [g[0].file for g in groups] == ["a.py", "a.py", "a.py", "b.py"]
    assert [f.id for g in groups[:3] for f in g] == [f.id for f in a]  # line order
    assert all(len(g) <= MERGE_GROUP_MAX for g in groups)


def test_a_group_at_the_cap_is_not_split():
    findings = chained("a.py", MERGE_GROUP_MAX)
    assert [len(g) for g in link_groups(findings, max_size=MERGE_GROUP_MAX)] == [40]


def merge_state(target_info) -> ReviewState:
    findings = [*chained("a.py", 50), *chained("b.py", 20)]
    return ReviewState(
        target=target_info,
        stage="verify",
        findings=findings,
        verdicts={
            f.id: Verdict(finding_id=f.id, status="confirmed", reason="r")
            for f in findings
        },
    )


def answers_for(groups: list[list[Finding]]) -> list[MergeOutput]:
    """Each group's first two findings are one defect."""
    return [
        MergeOutput(
            duplicate_sets=[DuplicateSet(finding_ids=[g[0].id, g[1].id], reason="same")]
        )
        for g in groups
    ]


async def test_the_merge_stage_asks_one_agent_per_capped_group(
    target_info, ctx, monkeypatch
):
    state = merge_state(target_info)
    groups = overlap_groups(state)
    assert [len(g) for g in groups] == [25, 25, 20]
    fake = use(monkeypatch, {"merge": answers_for(groups)})
    state = await run_merge(state, ctx)
    assert fake.labels() == ["merge"] * 3
    assert len(state.merged) == 3


async def test_s37_a_resume_after_some_capped_groups_merged_asks_only_the_rest(
    target_info, ctx, monkeypatch
):
    """Row S37: stopped after the first capped group's answer was saved. The groups are computed in
    code, so the resume gets the same groups back and asks only the unanswered ones; the result
    matches a run that never stopped."""
    groups = overlap_groups(merge_state(target_info))

    use(monkeypatch, {"merge": answers_for(groups)})
    whole = await run_merge(merge_state(target_info), ctx)

    answers = answers_for(groups)
    use(monkeypatch, {"merge": [answers[0], sdk.UsageLimitError("limit")]})
    with pytest.raises(sdk.UsageLimitError):
        await run_merge(merge_state(target_info), ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert list(saved.merge_answers) == [group_key(groups[0])]
    assert [group_key(g) for g in overlap_groups(saved)] == [
        group_key(g) for g in groups
    ]

    fake = use(monkeypatch, {"merge": answers[1:]})
    state = await run_merge(saved, ctx)
    assert len(fake.calls) == 2
    assert all(group_key(groups[0]) not in p for _, p, _ in fake.calls)
    assert [f.id for f in state.findings] == [f.id for f in whole.findings]
    assert [(m.finding.id, m.into) for m in state.merged] == [
        (m.finding.id, m.into) for m in whole.merged
    ]
    assert set(state.merge_answers) == set(whole.merge_answers)
