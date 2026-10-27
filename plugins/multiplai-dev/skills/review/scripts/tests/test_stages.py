"""Each stage with `agent_call_structured` replaced by canned answers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import (
    KEYWORD_CITATION,
    KEYWORD_USE_CITATION,
    cite,
    high_finding,
    medium_finding,
    pieces_of,
)
from paths_stub import stub_result
from review_pipeline import sdk, target
from review_pipeline.chunks import piece_diff_text
from review_pipeline.config import ReviewConfig
from review_pipeline.models import (
    Citation,
    ConvertOutput,
    DuplicateSet,
    FinderOutput,
    Finding,
    MergeOutput,
    Piece,
    ReviewState,
    Verdict,
    VerifierAnswer,
)
from review_pipeline.paths_io import PATHS_FILE
from review_pipeline.stages import RunContext
from review_pipeline.stages import find as find_stage
from review_pipeline.stages import merge as merge_stage
from review_pipeline.stages.find import conventions_chain, run_find
from review_pipeline.stages.merge import (
    MERGE_LINE_GAP,
    group_key,
    overlap_groups,
    run_merge,
)
from review_pipeline.stages.verify import VerifyIncomplete, run_verify
from review_pipeline.state import STATE_FILE, load_state


class Canned:
    """Stands in for sdk.agent_call_structured. `answers[label]` is a list
    consumed in order; an Exception in it is raised."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt))
        expected = (
            ["Read", "Grep", "Glob"]
            if budget_label == "merge"
            else ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
        )
        assert kwargs["allowed_tools"] == expected
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return (
            schema.model_validate(answer.model_dump())
            if hasattr(answer, "model_dump")
            else answer
        )


@pytest.fixture
def ctx(target_info, tmp_path, monkeypatch) -> RunContext:
    monkeypatch.setenv(
        "REVIEW_TRUST_REPO", "1"
    )  # the report calls check it, as the real one does
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(
        config=ReviewConfig(concurrency=2, dimensions=("diff-bugs", "callers")),
        snapshot=snapshot,
        diff=Path(target_info.diff_path).read_text(),
    )


def change_state(target_info, **kwargs) -> ReviewState:
    """A change review's state as `prepare` leaves it, running `diff-bugs`, `callers` and `trim`."""
    return ReviewState(
        target=target_info,
        pieces=pieces_of(target_info),
        dimensions=["diff-bugs", "callers", "trim"],
        **kwargs,
    )


def use(monkeypatch, answers) -> Canned:
    canned = Canned(answers)
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return canned


# --- find ------------------------------------------------------------------------


async def test_find_dedupes_gates_and_normalises_paths(target_info, ctx, monkeypatch):
    high = high_finding()
    absolute = high.with_location(
        file=str(ctx.snapshot / "rateplan_service.py"),
        citations=[
            c.model_copy(update={"path": f"./{c.path}"}).model_dump()
            for c in high.citations
        ],
    )
    bad = medium_finding().with_location(
        citations=[cite("rateplan_service.py", 2, "KEYWORD = 'dolcebot'").model_dump()]
    )
    canned = use(
        monkeypatch,
        {
            "find:diff-bugs": [FinderOutput(findings=[absolute, bad])],
            "find:callers": [FinderOutput(findings=[high])],  # duplicate of the first
            "find:trim": [FinderOutput()],
        },
    )
    state = await run_find(change_state(target_info), ctx)
    assert [f.id for f in state.findings] == [high.id]
    assert state.findings[0].file == "rateplan_service.py"
    assert state.findings[0].citations[0].path == "rateplan_service.py"
    assert state.findings[0].finder == "diff-bugs"
    assert state.findings[0].finders == [
        "diff-bugs",
        "callers",
    ]  # the copy's finder is kept
    assert (
        len(state.rejected) == 1
        and "quote not at cited lines" in state.rejected[0].reason
    )
    assert state.stage == "find" and len(canned.calls) == 3


async def test_find_survives_one_failed_finder_but_not_all(
    target_info, ctx, monkeypatch
):
    use(
        monkeypatch,
        {
            "find:diff-bugs": [sdk.AgentCallError("x")],
            "find:callers": [FinderOutput(findings=[])],
            "find:trim": [FinderOutput()],
        },
    )
    state = await run_find(change_state(target_info), ctx)
    assert state.stage == "find" and state.errors and "diff-bugs@1" in state.errors[0]

    use(
        monkeypatch,
        {
            "find:diff-bugs": [sdk.AgentCallError("x")],
            "find:callers": [sdk.AgentCallError("y")],
            "find:trim": [sdk.AgentCallError("z")],
        },
    )
    with pytest.raises(sdk.AgentCallError):
        await run_find(change_state(target_info), ctx)


async def test_trust_error_escapes(target_info, ctx, monkeypatch):
    use(
        monkeypatch,
        {
            "find:diff-bugs": [sdk.RepoTrustError("no")],
            "find:callers": [FinderOutput()],
        },
    )
    with pytest.raises(sdk.RepoTrustError):
        await run_find(change_state(target_info), ctx)


async def test_a_budget_stop_during_find_keeps_finished_finders_and_resume_runs_the_rest(
    target_info, ctx, monkeypatch
):
    from review_pipeline import budget

    ctx.config.concurrency = 1
    state = change_state(target_info)
    use(
        monkeypatch,
        {
            "find:diff-bugs": [FinderOutput(findings=[high_finding()])],
            "find:callers": [budget.BudgetExceededError("stop")],
            "find:trim": [FinderOutput()],
        },
    )
    with pytest.raises(budget.BudgetExceededError):
        await run_find(state, ctx)
    assert state.stage == "target" and state.findings == []
    assert list(state.finder_reports) == ["diff-bugs@1", "trim@1"]
    assert [f.id for f in state.finder_reports["diff-bugs@1"].findings] == [
        high_finding().id
    ]

    canned = use(
        monkeypatch, {"find:callers": [FinderOutput(findings=[high_finding()])]}
    )
    state = await run_find(state, ctx)
    assert [label for label, _ in canned.calls] == ["find:callers"]
    assert [f.id for f in state.findings] == [high_finding().id]
    assert state.findings[0].finders == ["diff-bugs", "callers"]
    assert state.finder_reports["diff-bugs@1"].findings[0].finders == ["diff-bugs"]


async def test_a_failed_finder_is_asked_again_on_resume_and_the_others_are_not(
    target_info, ctx, monkeypatch
):
    """Row S2: a resume asks only the keys with no converted report, and a failed report left none."""
    ctx.config.concurrency = 1
    state = change_state(target_info)
    use(
        monkeypatch,
        {
            "find:diff-bugs": [sdk.AgentCallError("bad answer")],
            "find:callers": [FinderOutput()],
            "find:trim": [FinderOutput()],
        },
    )
    state = await run_find(state, ctx)
    assert "diff-bugs@1" not in state.finder_reports
    assert any(e.startswith("diff-bugs@1: ") for e in state.errors)

    state.stage = "paths"  # a resume of the stage, with the failed finder's record kept
    canned = use(monkeypatch, {"find:diff-bugs": [FinderOutput()]})
    state = await run_find(state, ctx)
    assert [label for label, _ in canned.calls] == ["find:diff-bugs"]
    assert not any(c.subject == "diff-bugs@1" for c in state.coverage)


async def test_every_finder_failing_fails_the_stage_and_keeps_nothing_to_skip(
    target_info, ctx, monkeypatch
):
    state = change_state(target_info)
    use(
        monkeypatch,
        {
            "find:diff-bugs": [sdk.AgentCallError("x")],
            "find:callers": [sdk.AgentCallError("y")],
            "find:trim": [sdk.AgentCallError("z")],
        },
    )
    with pytest.raises(sdk.AgentCallError, match="every finder failed"):
        await run_find(state, ctx)
    assert state.finder_reports == {}  # the next run asks every finder again


# --- every dimension on every piece -----------------------------------------------------------------

FIXTURE_FILES = ("direct_booking.py", "rateplan_service.py", "settings.py")
ALL_SIX = ("diff-bugs", "callers", "history", "conventions", "tests", "trim")


def one_piece_per_file(info) -> list[Piece]:
    """The fixture's three changed modules as three pieces, sized as `split_pieces` sizes them."""
    diff = Path(info.diff_path).read_text()
    out = []
    for i, f in enumerate(FIXTURE_FILES, 1):
        piece = Piece(id=i, group="", files=[f])
        out.append(
            piece.model_copy(update={"chars": len(piece_diff_text(diff, piece))})
        )
    return out


class Finders:
    """Both SDK calls of a change review's finders: a report call answers one advice line, a convert call
    one item per report; any structured call that is not a convert fails the test."""

    def __init__(self):
        self.reports: list[tuple[str, str]] = []  # (budget label, prompt)
        self.converts: list[str] = []
        self.tools: list[list[str]] = []

    async def text(self, prompt, *, budget_label="", allowed_tools=(), **kwargs):
        sdk.require_trusted_repo()
        self.reports.append((budget_label, prompt))
        self.tools.append(list(allowed_tools))
        return "settings.py:1 Kind: advice\nName the setting."

    async def structured(self, prompt, schema, *, budget_label="", **kwargs):
        assert schema is ConvertOutput, f"{budget_label} asked for {schema.__name__}"
        self.converts.append(budget_label)
        return ConvertOutput()


@pytest.fixture
def finders(monkeypatch) -> Finders:
    fake = Finders()
    monkeypatch.setattr(sdk, "agent_call_text", fake.text)
    monkeypatch.setattr(sdk, "agent_call_structured", fake.structured)
    return fake


def with_one_path(state: ReviewState, ctx: RunContext, tmp_path: Path) -> None:
    """The paths stage's output for one path, as `run_paths` leaves it."""
    ctx.target_dir = tmp_path / "out"
    ctx.target_dir.mkdir(exist_ok=True)
    result = stub_result(commit=state.target.head_sha, scoped=True, cap=21)
    (ctx.target_dir / PATHS_FILE).write_text(result.model_dump_json())
    state.paths_status, state.path_ids = "ok", ["p1"]


async def test_three_pieces_ask_every_dimension_once_each_plus_one_pair_per_path(
    target_info, ctx, finders, tmp_path
):
    state = ReviewState(
        target=target_info,
        pieces=one_piece_per_file(target_info),
        dimensions=list(ALL_SIX),
    )
    with_one_path(state, ctx, tmp_path)
    state = await run_find(state, ctx)
    dims = [label.removeprefix("find:") for label, _ in finders.reports]
    assert sorted(dims) == sorted([*ALL_SIX * 3, "path"])
    assert sorted(finders.converts) == sorted(
        [*(f"find:{d}-convert" for d in ALL_SIX * 3), "find:path-convert"]
    )
    assert sorted(state.finder_reports) == sorted(
        [*(f"{d}@{i}" for d in ALL_SIX for i in (1, 2, 3)), "path@p1"]
    )
    assert state.finder_results == {}  # no dimension answered the old JSON call


async def test_the_dimensions_key_limits_the_finders_on_every_piece(
    target_info, ctx, finders
):
    state = ReviewState(
        target=target_info,
        pieces=one_piece_per_file(target_info),
        dimensions=["diff-bugs", "callers"],
    )
    state = await run_find(state, ctx)
    assert len(finders.reports) == len(finders.converts) == 3 * 2
    assert sorted(state.finder_reports) == sorted(
        f"{d}@{i}" for d in ("diff-bugs", "callers") for i in (1, 2, 3)
    )


async def test_a_one_piece_change_asks_one_report_per_dimension(
    target_info, ctx, finders
):
    state = ReviewState(target=target_info, pieces=pieces_of(target_info))
    assert len([p for p in state.pieces if p.files]) == 1
    await run_find(state, ctx)
    assert sorted(label for label, _ in finders.reports) == sorted(
        f"find:{d}" for d in find_stage.DEFAULT_CHANGE_DIMENSIONS
    )
    assert all(t == list(sdk.FINDER_TOOLS) for t in finders.tools)


async def test_stage_already_done_makes_no_calls(target_info, ctx, monkeypatch):
    canned = use(monkeypatch, {})
    state = ReviewState(target=target_info, stage="merge")
    for fn in (run_find, run_verify, run_merge):
        assert await fn(state, ctx) is state
    assert canned.calls == []


def test_conventions_chain_reads_claude_md_at_head(target_info):
    assert conventions_chain(target_info) == ""  # the fixture has none


def rules_repo(tmp_path: Path, claude_md: str = "Use tabs.\n"):
    """A repo whose head changes pkg/x.py, with rules files at the root and in pkg/."""
    repo = tmp_path / "rules-repo"
    (repo / "pkg").mkdir(parents=True)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.com",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.com",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "CLAUDE.md").write_text(claude_md)
    (repo / "coding-standards.md").write_text("Name every constant.\n")
    (repo / "pkg" / "coding-standards.md").write_text("No mocks of the clock.\n")
    (repo / "pkg" / "CLAUDE.md").write_text("Prefer pathlib.\n")
    (repo / "pkg" / "x.py").write_text("X = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "pkg" / "x.py").write_text("X = 2\n")
    git("commit", "-q", "-am", "change x")
    resolved = target.resolve(repo, range_=f"{base}..{git('rev-parse', 'HEAD')}")
    return target.build_target(resolved)


def test_conventions_chain_puts_coding_standards_before_claude_md(tmp_path):
    chain = conventions_chain(rules_repo(tmp_path))
    headings = [line for line in chain.splitlines() if line.startswith("### ")]
    assert headings == [
        "### coding-standards.md",
        "### pkg/coding-standards.md",
        "### CLAUDE.md",
        "### pkg/CLAUDE.md",
    ]
    for rule in (
        "Name every constant.",
        "No mocks of the clock.",
        "Use tabs.",
        "Prefer pathlib.",
    ):
        assert rule in chain


def test_conventions_chain_skips_claude_md_first_when_over_the_cap(
    tmp_path, monkeypatch
):
    info = rules_repo(tmp_path, claude_md="Use tabs.\n" * 50)
    monkeypatch.setattr(find_stage, "CONVENTIONS_MAX_CHARS", 200)
    chain = conventions_chain(info)
    assert "Name every constant." in chain and "No mocks of the clock." in chain
    assert (
        "### CLAUDE.md\n\n[skipped: the rules above already fill the prompt budget]"
        in chain
    )
    assert "Use tabs." not in chain
    # A skipped block does not stop the walk: the smaller pkg/CLAUDE.md still fits after it.
    assert "### pkg/CLAUDE.md\n\nPrefer pathlib." in chain


# --- verify ----------------------------------------------------------------------


async def test_verify_downgrades_uncited_confirmation_and_keeps_refuted(
    target_info, ctx, monkeypatch
):
    high, medium = high_finding(), medium_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="looks right",
                ),  # no citation -> unverifiable
                Verdict(
                    status="refuted",
                    reason="line 6 handles it",
                    citations=[KEYWORD_USE_CITATION],
                ),
            ]
        },
    )
    ctx.config.concurrency = 1  # answers are consumed in finding order
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx
    )
    assert state.verdicts[high.id].status == "unverifiable"
    assert "without citing" in state.verdicts[high.id].reason
    assert (
        state.findings[0].severity == "MEDIUM"
        and state.original_severity[high.id] == "HIGH"
    )
    assert state.verdicts[medium.id].status == "refuted"
    # The canned unverifiable verdict has no expected behaviour, so it is counted.
    assert ctx.counts == {
        "confirmed": 0,
        "refuted": 1,
        "unverifiable": 1,
        "unverified": 0,
        "no_expected_behaviour": 1,
    }


def two_low_one_medium() -> list:
    low = [
        high_finding().with_location(severity="LOW", claim=f"Low finding {n}")
        for n in (1, 2)
    ]
    return [*low, medium_finding()]


def medium_verdict() -> Verdict:
    return Verdict(
        status="confirmed",
        impact="correctness-only",
        topic="code",
        reason="it does",
        citations=[KEYWORD_USE_CITATION],
        expected_behaviour="It matches.",
    )


async def test_verify_asks_no_verifier_about_a_low_finding(
    target_info, ctx, monkeypatch
):
    findings = two_low_one_medium()
    canned = use(monkeypatch, {"verify": [medium_verdict()]})
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=findings), ctx
    )
    assert len(canned.calls) == 1
    low, medium = findings[:2], findings[2]
    for f in low:
        v = state.verdicts[f.id]
        assert (v.status, v.reason, v.impact, v.topic) == ("unverified", "", "", "")
    assert state.verdicts[medium.id].status == "confirmed"
    # Only the verifier's call is recorded; an unverified finding has no AgentCheck.
    assert [c.subject for c in state.checks] == [medium.id]
    assert [f.severity for f in state.findings] == ["LOW", "LOW", "MEDIUM"]
    assert ctx.counts["unverified"] == 2
    assert "no_expected_behaviour" not in ctx.counts


async def test_verify_low_asks_a_verifier_about_every_finding(
    target_info, ctx, monkeypatch
):
    ctx.config.verify_low = True
    canned = use(monkeypatch, {"verify": [medium_verdict()] * 3})
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=two_low_one_medium()),
        ctx,
    )
    assert len(canned.calls) == 3
    assert {v.status for v in state.verdicts.values()} == {"confirmed"}


async def test_a_low_tests_finding_is_unverified_with_the_tests_topic_and_a_low_trim_finding_is_verified(
    target_info, ctx, monkeypatch
):
    tests_low = high_finding().with_location(
        severity="LOW", claim="A test is missing", dimension="tests", finder="tests"
    )
    trim_low = high_finding().with_location(
        severity="LOW", claim="Merge two tests", dimension="trim", finder="trim"
    )
    canned = use(monkeypatch, {"verify": [medium_verdict()]})
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[tests_low, trim_low]),
        ctx,
    )
    assert state.verdicts[tests_low.id].topic == "tests"
    assert state.verdicts[tests_low.id].status == "unverified"
    assert len(canned.calls) == 1 and state.verdicts[trim_low.id].status == "confirmed"


async def test_verify_resumed_after_the_low_verdicts_asks_only_for_the_rest(
    target_info, ctx, monkeypatch, tmp_path
):
    """Row S5: LOW findings already have their code-made verdict, so a resume makes no call for them."""
    findings = two_low_one_medium()
    ctx.target_dir = tmp_path / "out"
    ctx.target_dir.mkdir(exist_ok=True)
    use(monkeypatch, {"verify": [sdk.UsageLimitError("limit")]})
    state = ReviewState(target=target_info, stage="find", findings=findings)
    with pytest.raises(sdk.UsageLimitError):
        await run_verify(state, ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert {v.status for v in saved.verdicts.values()} == {"unverified"}
    assert len(saved.verdicts) == 2
    canned = use(monkeypatch, {"verify": [medium_verdict()]})
    state = await run_verify(saved, ctx)
    assert len(canned.calls) == 1 and len(state.verdicts) == 3
    assert state.stage == "verify"


def test_a_verifier_cannot_answer_unverified():
    from pydantic import ValidationError
    from review_pipeline.models import VerifierAnswer

    with pytest.raises(ValidationError, match="confirmed, refuted or unverifiable"):
        VerifierAnswer(status="unverified", reason="", impact="hygiene", topic="code")


async def test_verify_keeps_a_grounded_confirmation_and_its_expected_behaviour(
    target_info, ctx, monkeypatch
):
    high = high_finding()
    canned = use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                    expected_behaviour="Rate plans match the channel's title.",
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    assert (
        state.verdicts[high.id].status == "confirmed"
        and state.findings[0].severity == "HIGH"
    )
    assert (
        state.verdicts[high.id].expected_behaviour
        == "Rate plans match the channel's title."
    )
    assert "without proposing a code change" in canned.calls[0][1]
    assert state.errors == []


async def test_verify_lists_findings_that_carry_no_expected_behaviour(
    target_info, ctx, monkeypatch
):
    high = high_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    # The confirmation stands; the gap is reported, not hidden.
    assert state.verdicts[high.id].status == "confirmed"
    assert ctx.counts["no_expected_behaviour"] == 1
    assert state.errors == [f"verify: no expected behaviour for 1 finding ({high.id})"]


async def test_a_failed_verifier_is_tried_again_and_its_answer_kept(
    target_info, ctx, monkeypatch
):
    high = high_finding()
    canned = use(
        monkeypatch,
        {
            "verify": [
                sdk.AgentCallError("timeout"),
                sdk.AgentCallError("timeout"),
                Verdict(
                    status="confirmed",
                    impact="breaks-business",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                ),
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    assert [label for label, _ in canned.calls] == ["verify"] * 3
    assert (state.verdicts[high.id].status, state.verdicts[high.id].impact) == (
        "confirmed",
        "breaks-business",
    )
    assert state.stage == "verify"


async def test_a_finding_with_no_answer_after_three_tries_stops_the_run_and_resume_asks_only_it(
    target_info, ctx, monkeypatch
):
    high, medium = high_finding(), medium_finding()
    fail = sdk.AgentCallError("timeout")
    ok = Verdict(
        status="confirmed",
        impact="breaks-users",
        topic="code",
        reason="r",
        citations=[KEYWORD_CITATION],
    )
    canned = use(monkeypatch, {"verify": [ok, fail, fail, fail]})
    ctx.config.concurrency = 1
    state = ReviewState(target=target_info, stage="find", findings=[high, medium])
    with pytest.raises(VerifyIncomplete) as e:
        await run_verify(state, ctx)
    assert e.value.finding_ids == [medium.id] and "after 3 tries" in str(e.value)
    assert set(state.verdicts) == {high.id}  # no stand-in verdict for the failed one
    assert state.stage == "find"
    assert state.checks[-1].error.endswith("(3 tries)")
    canned.answers["verify"] = [
        ok.model_copy(update={"citations": [KEYWORD_USE_CITATION]})
    ]
    canned.calls.clear()
    state = await run_verify(state, ctx)
    assert len(canned.calls) == 1 and medium.claim in canned.calls[0][1]
    assert set(state.verdicts) == {high.id, medium.id} and state.stage == "verify"


async def test_an_answer_without_impact_is_not_a_verdict(target_info, ctx, monkeypatch):
    high = high_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(status="refuted", reason="r", citations=[KEYWORD_USE_CITATION])
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    assert state.verdicts[high.id].impact == ""  # refuted needs none
    with pytest.raises(ValueError, match="impact is required"):
        VerifierAnswer(status="confirmed", reason="r")
    with pytest.raises(ValueError, match="impact is required"):
        VerifierAnswer(
            status="unverifiable", reason="r", impact="nonsense"
        )  # unknown is none


def test_an_answer_without_a_known_topic_is_not_a_verdict():
    # The topic picks the section and the tests-impact cap: a blanked one would file a test gap under Code.
    with pytest.raises(ValueError, match="topic is required"):
        VerifierAnswer(status="confirmed", reason="r", impact="breaks-users")
    with pytest.raises(ValueError, match="topic is required"):
        VerifierAnswer(
            status="unverifiable", reason="r", impact="breaks-users", topic="test"
        )  # not "tests"
    assert (
        VerifierAnswer(status="refuted", reason="r").topic == ""
    )  # refuted needs none
    assert (
        VerifierAnswer(
            status="confirmed", reason="r", impact="hygiene", topic=" Tests "
        ).topic
        == "tests"
    )


async def test_a_test_finding_never_breaks_users_or_the_business(
    target_info, ctx, monkeypatch
):
    high = high_finding()
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-business",
                    topic="tests",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                )
            ]
        },
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high]), ctx
    )
    assert state.verdicts[high.id].impact == "correctness-only"
    assert (
        f"verify: {high.id} is about tests; impact breaks-business lowered to correctness-only"
        in state.errors
    )


# --- merge -----------------------------------------------------------------------


def _at(
    claim: str,
    severity: str,
    start: int,
    end: int,
    finder: str,
    *citations,
    file: str = "rateplan_service.py",
) -> Finding:
    return Finding(
        claim=claim,
        severity=severity,
        dimension=finder,
        file=file,
        line_start=start,
        line_end=end,
        failure_scenario="s",
        citations=list(citations) or [KEYWORD_CITATION],
        finder=finder,
        finders=[finder],
    )


def _verdicts(**status_by_id) -> dict[str, Verdict]:
    return {
        fid: Verdict(finding_id=fid, status=status, reason="r")
        for fid, status in status_by_id.items()
    }


def _own(claim, severity, start, end, finder, file="rateplan_service.py"):
    """A finding whose only citation is its own anchor, so only anchors can link it."""
    return _at(
        claim,
        severity,
        start,
        end,
        finder,
        Citation(path=file, line_start=start, line_end=end, quote="q"),
        file=file,
    )


def test_overlap_groups_chain_overlaps_and_near_touches_in_one_file(target_info):
    a = _own("a", "LOW", 1, 1, "diff-bugs")
    b = _own("b", "LOW", 1 + MERGE_LINE_GAP, 3, "callers")  # within the gap of a
    c = _own("c", "LOW", 10, 12, "diff-bugs")
    d = _own("d", "LOW", 11, 11, "tests")  # inside c
    far = _own(
        "far", "LOW", 12 + MERGE_LINE_GAP + 1, 16, "history"
    )  # one line past the gap
    other_file = _own("o", "LOW", 1, 1, "callers", file="settings.py")
    refuted = _own("r", "LOW", 1, 1, "history")
    findings = [a, b, c, d, far, other_file, refuted]
    state = ReviewState(
        target=target_info,
        findings=findings,
        verdicts={
            **_verdicts(**{f.id: "confirmed" for f in findings}),
            **_verdicts(**{refuted.id: "refuted"}),
        },
    )
    assert [[f.claim for f in g] for g in overlap_groups(state)] == [
        ["a", "b"],
        ["c", "d"],
    ]


def test_overlap_groups_link_findings_in_different_files_that_cite_the_same_line(
    target_info,
):
    shared = Citation(path="docs/order.md", line_start=7, line_end=7, quote="q")
    in_code = _at(
        "in code",
        "MEDIUM",
        1,
        1,
        "diff-bugs",
        Citation(path="a.py", line_start=1, line_end=1, quote="q"),
        shared,
        file="a.py",
    )
    in_test = _at(
        "in test",
        "LOW",
        40,
        40,
        "tests",
        Citation(path="t.py", line_start=40, line_end=40, quote="q"),
        shared.model_copy(
            update={"line_start": 7 + MERGE_LINE_GAP, "line_end": 7 + MERGE_LINE_GAP}
        ),
        file="t.py",
    )
    alone = _own("alone", "LOW", 1, 1, "history", file="b.py")
    state = ReviewState(
        target=target_info,
        findings=[in_code, in_test, alone],
        verdicts=_verdicts(**{f.id: "confirmed" for f in (in_code, in_test, alone)}),
    )
    assert [[f.claim for f in g] for g in overlap_groups(state)] == [
        ["in code", "in test"]
    ]


def test_overlap_groups_do_not_link_on_a_shared_web_citation(target_info):
    page = Citation(
        path="https://example.com/docs", line_start=1, line_end=1, quote="q"
    )
    a = _at(
        "a",
        "LOW",
        1,
        1,
        "diff-bugs",
        Citation(path="a.py", line_start=1, line_end=1, quote="q"),
        page,
        file="a.py",
    )
    b = _at(
        "b",
        "LOW",
        1,
        1,
        "callers",
        Citation(path="b.py", line_start=1, line_end=1, quote="q"),
        page,
        file="b.py",
    )
    state = ReviewState(
        target=target_info,
        findings=[a, b],
        verdicts=_verdicts(**{a.id: "confirmed", b.id: "confirmed"}),
    )
    assert overlap_groups(state) == []


def test_made_up_round_links_each_cross_file_pair_and_leaves_the_third_alone():
    """Invented data with the shape of a real round the merge stage once got wrong:
    two duplicate pairs, each anchored in two files but citing one line of a third."""
    from review_pipeline.stages.merge import link_groups

    data = json.loads(
        (
            Path(__file__).parent / "fixtures" / "duplicate_pairs" / "findings.json"
        ).read_text()
    )
    findings = [
        Finding.model_validate({**f, "dimension": "diff-bugs", "finder": "diff-bugs"})
        for f in data["findings"]
    ]
    groups = [[f.file for f in g] for g in link_groups(findings)]
    assert groups == [
        ["stations/README.md", "stations/reader_access.tf"],
        ["policy/reader_access.tf", "policy/tests/reader.tftest.hcl"],
    ]


def _merge_state(target_info) -> tuple[ReviewState, Finding, Finding, Finding]:
    """Three overlapping findings: a confirmed LOW, an unverifiable one lowered from HIGH, and a third."""
    keep = _at("the keyword is hardcoded", "LOW", 1, 1, "diff-bugs", KEYWORD_CITATION)
    copy = _at(
        "rate plans are matched on a literal 'dolcebot'",
        "MEDIUM",
        1,
        6,
        "callers",
        KEYWORD_CITATION,
        KEYWORD_USE_CITATION,
    )
    other = _at(
        "the match is case-sensitive", "LOW", 6, 6, "tests", KEYWORD_USE_CITATION
    )
    state = ReviewState(
        target=target_info,
        stage="verify",
        findings=[keep, copy, other],
        verdicts=_verdicts(
            **{keep.id: "confirmed", copy.id: "unverifiable", other.id: "confirmed"}
        ),
        original_severity={copy.id: "HIGH"},
    )
    return state, keep, copy, other


async def test_merge_keeps_highest_severity_and_every_citation_and_records_the_copy(
    target_info, ctx, monkeypatch
):
    state, keep, copy, other = _merge_state(target_info)
    canned = use(
        monkeypatch,
        {
            "merge": [
                MergeOutput(
                    duplicate_sets=[
                        DuplicateSet(
                            finding_ids=[copy.id, keep.id],
                            reason="both say the keyword is a literal",
                        )
                    ]
                )
            ]
        },
    )
    state = await run_merge(state, ctx)

    ((label, prompt_text),) = canned.calls
    assert label == "merge" and all(f.id in prompt_text for f in (keep, copy, other))
    assert [f.id for f in state.findings] == [keep.id, other.id]
    merged = state.findings[0]
    assert (
        merged.claim == keep.claim
    )  # the confirmed finding survives, id and verdict unchanged
    assert merged.severity == "MEDIUM"  # the highest current severity in the set
    assert merged.citations == [KEYWORD_CITATION, KEYWORD_USE_CITATION]
    assert merged.finders == ["diff-bugs", "callers"]
    (record,) = state.merged
    assert record.finding.id == copy.id and record.into == keep.id
    assert (
        keep.id in record.reason
        and "both say the keyword is a literal" in record.reason
    )
    assert ctx.counts == {"groups": 1, "merged": 1, "agent_failures": 0}
    assert (
        state.stage == "merge" and group_key([keep, copy, other]) in state.merge_answers
    )


async def test_merge_of_unverifiable_findings_keeps_the_highest_original_severity(
    target_info, ctx, monkeypatch
):
    state, keep, copy, other = _merge_state(target_info)
    state.verdicts[keep.id].status = "unverifiable"
    state.original_severity[keep.id] = "MEDIUM"
    use(
        monkeypatch,
        {
            "merge": [
                MergeOutput(
                    duplicate_sets=[DuplicateSet(finding_ids=[keep.id, copy.id])]
                )
            ]
        },
    )
    state = await run_merge(state, ctx)
    survivor = state.findings[0]
    assert survivor.id == copy.id and survivor.severity == "MEDIUM"
    assert state.original_severity[copy.id] == "HIGH"
    assert state.merged[0].into == copy.id


async def test_merge_agent_failure_leaves_the_group_unmerged(
    target_info, ctx, monkeypatch
):
    state, keep, copy, other = _merge_state(target_info)
    before = [f.model_copy() for f in state.findings]
    use(monkeypatch, {"merge": [sdk.AgentCallError("timeout\ntrace")]})
    state = await run_merge(state, ctx)
    assert state.findings == before and state.merged == []
    assert state.errors == ["merge rateplan_service.py:1-6: timeout"]
    assert ctx.counts["agent_failures"] == 1 and state.stage == "merge"


async def test_merge_ignores_ids_outside_the_group_and_ids_used_twice(
    target_info, ctx, monkeypatch
):
    state, keep, copy, other = _merge_state(target_info)
    use(
        monkeypatch,
        {
            "merge": [
                MergeOutput(
                    duplicate_sets=[
                        DuplicateSet(
                            finding_ids=[keep.id, "0123456789"]
                        ),  # one real id: not a set
                        DuplicateSet(finding_ids=[keep.id, copy.id]),
                        DuplicateSet(
                            finding_ids=[copy.id, other.id]
                        ),  # copy is already used
                    ]
                )
            ]
        },
    )
    state = await run_merge(state, ctx)
    assert [f.id for f in state.findings] == [keep.id, other.id]
    assert [m.finding.id for m in state.merged] == [copy.id]


async def test_merge_resume_reuses_stored_answers_and_a_budget_stop_keeps_the_findings(
    target_info, ctx, monkeypatch
):
    from review_pipeline import budget

    state, keep, copy, other = _merge_state(target_info)
    use(monkeypatch, {"merge": [budget.BudgetExceededError("stop")]})
    with pytest.raises(budget.BudgetExceededError):
        await run_merge(state, ctx)
    assert (
        state.stage == "verify"
        and len(state.findings) == 3
        and state.merge_answers == {}
    )

    state.merge_answers[group_key(state.findings)] = [
        DuplicateSet(finding_ids=[keep.id, copy.id])
    ]
    canned = use(monkeypatch, {"merge": []})
    state = await run_merge(state, ctx)
    assert canned.calls == [] and [f.id for f in state.findings] == [keep.id, other.id]


async def test_a_budget_stop_keeps_the_verdicts_already_paid_for(
    target_info, ctx, monkeypatch
):
    from review_pipeline import budget

    high, medium = high_finding(), medium_finding()
    state = ReviewState(target=target_info, stage="find", findings=[high, medium])
    ctx.config.concurrency = 1
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                ),
                budget.BudgetExceededError("stop"),
            ]
        },
    )
    with pytest.raises(budget.BudgetExceededError):
        await run_verify(state, ctx)
    assert list(state.verdicts) == [high.id] and state.stage == "find"


# --- a hit usage limit -------------------------------------------------------------


def _limit() -> sdk.UsageLimitError:
    return sdk.UsageLimitError(
        "the Claude session usage limit was hit; it resets 9:40am (UTC)",
        limit="session",
        resets="9:40am (UTC)",
    )


async def test_a_usage_limit_stops_verify_at_once_and_keeps_the_verdicts(
    target_info, ctx, monkeypatch
):
    """No second or third try: every call until the reset would fail the same way."""
    high, medium = high_finding(), medium_finding()
    state = ReviewState(target=target_info, stage="find", findings=[high, medium])
    ctx.config.concurrency = 1
    canned = use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                ),
                _limit(),
                _limit(),
                _limit(),
            ]
        },
    )
    with pytest.raises(sdk.UsageLimitError):
        await run_verify(state, ctx)
    assert len(canned.calls) == 2
    assert list(state.verdicts) == [high.id] and state.stage == "find"


async def test_a_usage_limit_stops_find_and_stores_nothing_for_that_finder(
    target_info, ctx, monkeypatch
):
    ctx.config.concurrency = 1
    state = change_state(target_info)
    use(
        monkeypatch,
        {
            "find:diff-bugs": [FinderOutput(findings=[high_finding()])],
            "find:callers": [_limit()],
            "find:trim": [FinderOutput()],
        },
    )
    with pytest.raises(sdk.UsageLimitError):
        await run_find(state, ctx)
    assert "callers@1" not in state.finder_reports and not state.past("find")
    assert not any(c.subject == "callers@1" for c in state.coverage)
    assert [f.id for f in state.finder_reports["diff-bugs@1"].findings] == [
        high_finding().id
    ]


async def test_a_usage_limit_stops_merge(target_info, ctx, monkeypatch):
    state, *_ = _merge_state(target_info)
    use(monkeypatch, {"merge": [_limit()]})
    with pytest.raises(sdk.UsageLimitError):
        await run_merge(state, ctx)
    assert state.merge_answers == {} and state.stage == "verify" and state.errors == []


# --- a killed run -------------------------------------------------------------------
# A RepoTrustError leaves a stage without the orchestrator saving, as a kill does, so
# only what the stage saved itself is on disk.


async def test_a_killed_find_keeps_the_finders_that_answered(
    target_info, ctx, monkeypatch, tmp_path
):
    ctx.config.concurrency = 1
    ctx.target_dir = tmp_path / "out"
    ctx.target_dir.mkdir()
    use(
        monkeypatch,
        {
            "find:diff-bugs": [FinderOutput(findings=[high_finding()])],
            "find:callers": [sdk.RepoTrustError("killed")],
        },
    )
    with pytest.raises(sdk.RepoTrustError):
        await run_find(change_state(target_info), ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert list(saved.finder_reports) == ["diff-bugs@1"]
    assert saved.finder_reports["diff-bugs@1"].converted
    assert [c.subject for c in saved.checks] == ["diff-bugs@1", "diff-bugs@1 convert"]


async def test_a_killed_verify_keeps_the_verdicts_and_their_spend(
    target_info, ctx, monkeypatch, tmp_path
):
    from review_pipeline import budget

    high, medium = high_finding(), medium_finding()
    ctx.config.concurrency = 1
    ctx.target_dir = tmp_path / "out"
    ctx.target_dir.mkdir()
    budget.start(10.0).cost_usd = 1.25  # what the calls so far cost
    use(
        monkeypatch,
        {
            "verify": [
                Verdict(
                    status="confirmed",
                    impact="breaks-users",
                    topic="code",
                    reason="r",
                    citations=[KEYWORD_CITATION],
                ),
                sdk.RepoTrustError("killed"),
            ]
        },
    )
    state = ReviewState(target=target_info, stage="find", findings=[high, medium])
    with pytest.raises(sdk.RepoTrustError):
        await run_verify(state, ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert list(saved.verdicts) == [high.id] and saved.stage == "find"
    assert saved.budget["cost_usd"] == 1.25


async def test_a_killed_merge_keeps_the_answer(target_info, ctx, monkeypatch, tmp_path):
    state, keep, copy, _ = _merge_state(target_info)
    ctx.target_dir = tmp_path / "out"
    ctx.target_dir.mkdir()
    use(
        monkeypatch,
        {
            "merge": [
                MergeOutput(
                    duplicate_sets=[DuplicateSet(finding_ids=[keep.id, copy.id])]
                )
            ]
        },
    )

    def killed(*a, **kw):
        raise sdk.RepoTrustError("killed")

    monkeypatch.setattr(merge_stage, "apply_duplicate_sets", killed)
    with pytest.raises(sdk.RepoTrustError):
        await run_merge(state, ctx)
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert list(saved.merge_answers.values()) == [
        [DuplicateSet(finding_ids=[keep.id, copy.id])]
    ]
