"""Tests for quality gates — pure code assertions."""

import pytest
from pathlib import Path

from build_pipeline.gates import (
    agent_status_gate,
    parse_agent_status,
    parse_prototype_notes,
    prototype_gate,
    prototype_required,
    slot_has_content,
    parse_implementation_note,
    red_gate,
    review_score_gate,
    review_iteration_gate,
    run_test_suite,
    wiring_task_gate,
    baseline_test_gate,
    integration_gate,
)
from build_pipeline.models import ReviewResult, ReviewScore


class TestReviewScoreGate:
    def test_passes_above_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=4, evidence=""),
            ReviewScore(dimension="B", weight=1, score=4, evidence=""),
        ])
        result = review_score_gate(r)
        assert result.passed

    def test_fails_below_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=2, evidence=""),
            ReviewScore(dimension="B", weight=1, score=3, evidence=""),
        ])
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_low_scores"

    def test_fails_with_dimension_at_1(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=5, evidence=""),
            ReviewScore(dimension="B", weight=1, score=1, evidence=""),
        ])
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_critical_dimension"
        assert "B" in result.metadata["failing_dimensions"]

    def test_fails_on_spec_verdict_despite_high_scores(self):
        r = ReviewResult(
            scores=[ReviewScore(dimension="A", weight=2, score=5, evidence="")],
            missing=["WHEN empty input THEN 400"],
            misunderstood=["retry semantics"],
        )
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_spec_compliance"
        assert "WHEN empty input THEN 400" in result.reason
        assert result.metadata["missing"] == ["WHEN empty input THEN 400"]

    def test_extra_alone_does_not_trip_spec_verdict(self):
        r = ReviewResult(
            scores=[ReviewScore(dimension="A", weight=2, score=4, evidence="")],
            extra=["bonus flag"],
        )
        assert review_score_gate(r).passed


class TestReviewIterationGate:
    def test_within_limit(self):
        assert review_iteration_gate(0).passed
        assert review_iteration_gate(1).passed
        assert review_iteration_gate(2).passed

    def test_at_limit(self):
        result = review_iteration_gate(3)
        assert not result.passed
        assert result.action == "halt_build"

    def test_custom_limit(self):
        assert review_iteration_gate(4, max_iterations=5).passed
        assert not review_iteration_gate(5, max_iterations=5).passed


class TestWiringTaskGate:
    def test_not_app_passes(self, tmp_path):
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 1. Setup\n- [ ] 1.1 Create module\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert result.passed
        assert "Not detected as app" in result.reason

    def test_app_with_wiring_task_passes(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__main__.py").write_text("pass")
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 8. Wiring\n- [ ] Wire entry point\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert result.passed

    def test_app_without_wiring_task_fails(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__main__.py").write_text("pass")
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 1. Setup\n- [ ] 1.1 Create module\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert not result.passed
        assert "wiring" in result.reason.lower()


@pytest.fixture
def trust_repo(monkeypatch):
    """Gates that execute the repo's test_command require an explicit trust opt-in."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


class TestBaselineTestGate:
    def test_no_test_command_passes(self, tmp_path):
        result = baseline_test_gate("", tmp_path)
        assert result.passed

    def test_passing_tests(self, tmp_path, trust_repo):
        result = baseline_test_gate("true", tmp_path)  # 'true' command always exits 0
        assert result.passed

    def test_failing_tests(self, tmp_path, trust_repo):
        result = baseline_test_gate("false", tmp_path)  # 'false' command always exits 1
        assert not result.passed

    def test_untrusted_repo_refuses_to_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        result = baseline_test_gate("true", tmp_path)
        assert not result.passed
        assert "not trusted" in result.reason


class TestRedGate:
    """RED gate: tests must fail for the right reason before implementation."""

    def test_passes_on_assertion_failure(self):
        output = "FAILED tests/test_auth.py::test_login - AssertionError: expected token"
        result = red_gate(output, 1)
        assert result.passed
        assert "right reason" in result.reason

    def test_passes_on_not_implemented(self):
        output = "FAILED tests/test_auth.py::test_login - NotImplementedError"
        assert red_gate(output, 1).passed

    def test_passes_on_missing_attribute(self):
        output = (
            "FAILED tests/test_auth.py::test_login - "
            "AttributeError: module 'auth' has no attribute 'login'"
        )
        assert red_gate(output, 1).passed

    def test_suite_passing_means_rewrite_tests(self):
        result = red_gate("5 passed in 0.3s", 0)
        assert not result.passed
        assert result.action == "rewrite_tests"

    def test_collection_error_means_fix_tests(self):
        output = "ERROR collecting tests/test_auth.py\nSyntaxError: invalid syntax"
        result = red_gate(output, 2)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_syntax_error_means_fix_tests(self):
        output = "E   SyntaxError: invalid syntax (test_auth.py, line 12)"
        result = red_gate(output, 2)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_unrecognized_failure_means_fix_tests(self):
        result = red_gate("something exploded unrecognizably", 1)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_unrunnable_suite_means_fix_tests(self):
        """exit_code=None (untrusted repo / missing binary) is not RED proof."""
        result = red_gate("Repo not trusted", None)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_passes_on_terse_lowercase_summary(self):
        """pytest -q --tb=no emits only a summary count — no FAILED lines."""
        assert red_gate("1 failed, 3 passed in 0.12s", 1).passed

    def test_passes_on_jest_style_output(self):
        """Jest/Vitest print `FAIL <file>` and a lowercase `N failed` summary —
        no AssertionError, no uppercase FAILED."""
        output = (
            "FAIL src/auth.test.js\n"
            "  ● login › returns a token\n"
            "Tests: 1 failed, 2 passed, 3 total\n"
        )
        assert red_gate(output, 1).passed

    def test_zero_failed_summary_is_not_red_proof(self):
        result = red_gate("0 failed, 5 passed", 1)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_lowercase_fail_in_prose_is_not_red_proof(self):
        """`fail` in ordinary prose (vs the uppercase FAIL marker) proves nothing."""
        result = red_gate("warning: flaky tests may fail intermittently", 1)
        assert not result.passed
        assert result.action == "fix_tests"


class TestRunTestSuite:
    def test_returns_exit_code_and_output(self, tmp_path, trust_repo):
        code, output = run_test_suite("true", tmp_path)
        assert code == 0

    def test_nonzero_exit(self, tmp_path, trust_repo):
        code, _ = run_test_suite("false", tmp_path)
        assert code == 1

    def test_untrusted_repo_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        code, output = run_test_suite("true", tmp_path)
        assert code is None
        assert "not trusted" in output

    def test_missing_binary_returns_none(self, tmp_path, trust_repo):
        code, output = run_test_suite("definitely-not-a-command-xyz", tmp_path)
        assert code is None


class TestIntegrationGate:
    def test_passing(self, tmp_path, trust_repo):
        result = integration_gate("true", tmp_path)
        assert result.passed

    def test_failing(self, tmp_path, trust_repo):
        result = integration_gate("false", tmp_path)
        assert not result.passed
        assert result.action == "spawn_fix_agent"

    def test_untrusted_repo_refuses_to_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        result = integration_gate("true", tmp_path)
        assert not result.passed
        assert "not trusted" in result.reason


class TestParseAgentStatus:
    """Agents close their report with a REQUIRED STATUS slot (Item 7)."""

    def test_parses_plain_slot(self):
        assert parse_agent_status("Wrote tests.\n\nSTATUS: DONE\nTESTS_RUN: pytest\n") == "DONE"

    def test_parses_underscored_variant(self):
        assert parse_agent_status("STATUS: DONE_WITH_CONCERNS\n") == "DONE_WITH_CONCERNS"
        assert parse_agent_status("STATUS: NEEDS_CONTEXT\n") == "NEEDS_CONTEXT"
        assert parse_agent_status("STATUS: BLOCKED\n") == "BLOCKED"

    def test_parses_bold_and_bulleted_markdown(self):
        assert parse_agent_status("**STATUS:** DONE\n") == "DONE"
        assert parse_agent_status("- STATUS: BLOCKED\n") == "BLOCKED"

    def test_lowercase_is_normalized(self):
        assert parse_agent_status("status: blocked\n") == "BLOCKED"

    def test_last_occurrence_wins(self):
        """A status quoted mid-report never outranks the agent's final verdict."""
        out = "I was told to report STATUS: DONE when finished.\n\nSTATUS: BLOCKED\n"
        assert parse_agent_status(out) == "BLOCKED"

    def test_missing_slot_returns_none(self):
        assert parse_agent_status("All finished, tests pass.") is None
        assert parse_agent_status("") is None


class TestAgentStatusGate:
    def test_done_passes(self):
        r = agent_status_gate("STATUS: DONE\n", "Implementer")
        assert r.passed
        assert r.metadata["status"] == "DONE"

    def test_done_with_concerns_passes(self):
        r = agent_status_gate("STATUS: DONE_WITH_CONCERNS\nMock is thin.\n", "Implementer")
        assert r.passed
        assert r.metadata["status"] == "DONE_WITH_CONCERNS"

    def test_needs_context_fails_and_surfaces_reason(self):
        out = "The spec names Widget.render() which does not exist.\n\nSTATUS: NEEDS_CONTEXT\n"
        r = agent_status_gate(out, "TestWriter")
        assert not r.passed
        assert r.metadata["status"] == "NEEDS_CONTEXT"
        assert "TestWriter" in r.reason
        # The agent's own words reach the operator, not just the status token
        assert "Widget.render()" in r.reason
        assert r.action == "escalate_to_human"

    def test_blocked_fails(self):
        r = agent_status_gate("STATUS: BLOCKED\n", "Implementer")
        assert not r.passed
        assert r.metadata["status"] == "BLOCKED"

    def test_missing_slot_passes_but_is_reported(self):
        """Deterministic gates are the real verification — a missing slot is
        reported, not fatal."""
        r = agent_status_gate("Done, all tests pass.", "Implementer")
        assert r.passed
        assert r.metadata["status"] is None
        assert "no STATUS slot" in r.reason


# --- Unknowns / explainer gate (B1) -------------------------------------------

COMPLETE_SECTION = """\
## mlx-whisper

### What it is
A local Whisper implementation on Apple MLX.

### The contract we rely on
`transcribe(path) -> {"text": str, "segments": list}`.

### Edge cases & failure modes
- Pure silence: returns hallucinated caption text such as "thanks for watching",
  not an empty string.
- Malformed audio: raises RuntimeError from the decoder.
- Oversized input: memory grows with clip length; >30min OOMs on 16GB.
- Concurrent use: the model object is not thread-safe.
- Offline: first run downloads weights and fails without network.

### Assumptions we are making
- Every clip we pass is under 30 minutes.
- Weights are cached before the first build runs.

### How we would find out cheaply
Transcribe a 5-second silent WAV and print the result.
"""

EMPTY_EDGE_CASES_SECTION = """\
## mlx-whisper

### What it is
A local Whisper implementation on Apple MLX.

### The contract we rely on
`transcribe(path) -> dict`.

### Edge cases & failure modes

### Assumptions we are making
- Every clip we pass is under 30 minutes.

### How we would find out cheaply
Transcribe a silent WAV.
"""


def _unknowns_doc(*sections: str) -> str:
    return "# Unknowns — what we are about to depend on\n\n" + "\n\n".join(sections)


class TestUnknownsGate:
    def test_passes_on_complete_section(self):
        from build_pipeline.gates import unknowns_gate
        r = unknowns_gate(_unknowns_doc(COMPLETE_SECTION), ["mlx-whisper"])
        assert r.passed

    def test_no_dependencies_passes(self):
        """"Nothing new to this project" is a recorded finding, not a failure."""
        from build_pipeline.gates import unknowns_gate
        r = unknowns_gate("# Unknowns\n\nNo dependencies new to this project.\n", [])
        assert r.passed
        assert r.metadata["dependencies"] == []

    def test_missing_section_fails(self):
        from build_pipeline.gates import unknowns_gate
        r = unknowns_gate(_unknowns_doc(COMPLETE_SECTION), ["mlx-whisper", "polars"])
        assert not r.passed
        assert r.action == "regenerate_unknowns"
        assert any("polars" in f and "no `## polars` section" in f
                   for f in r.metadata["findings"])

    def test_empty_edge_cases_list_fails(self):
        from build_pipeline.gates import unknowns_gate
        r = unknowns_gate(_unknowns_doc(EMPTY_EDGE_CASES_SECTION), ["mlx-whisper"])
        assert not r.passed
        assert any("Edge cases" in f for f in r.metadata["findings"])

    def test_empty_assumptions_list_fails(self):
        from build_pipeline.gates import unknowns_gate
        text = _unknowns_doc(
            COMPLETE_SECTION.replace(
                "- Every clip we pass is under 30 minutes.\n"
                "- Weights are cached before the first build runs.\n",
                "",
            )
        )
        r = unknowns_gate(text, ["mlx-whisper"])
        assert not r.passed
        assert any("Assumptions" in f for f in r.metadata["findings"])

    def test_unfilled_template_placeholders_do_not_count_as_content(self):
        """A section left at the template's `- <case>: <what happens>` bullets
        is an unwritten explainer, not a written one."""
        from build_pipeline.gates import unknowns_gate
        section = COMPLETE_SECTION.replace(
            "- Pure silence: returns hallucinated caption text such as "
            '"thanks for watching",\n  not an empty string.',
            "- <case>: <what actually happens>",
        )
        # Drop the remaining real bullets so only the placeholder is left.
        section = "\n".join(
            line for line in section.splitlines()
            if not line.startswith(("- Malformed", "- Oversized", "- Concurrent",
                                    "- Offline", "  not an empty"))
        )
        r = unknowns_gate(_unknowns_doc(section), ["mlx-whisper"])
        assert not r.passed

    def test_accepts_new_dependency_objects(self):
        from build_pipeline.dependencies import NewDependency
        from build_pipeline.gates import unknowns_gate
        dep = NewDependency(name="mlx-whisper", mentioned_in=["proposal.md § Impact"])
        assert unknowns_gate(_unknowns_doc(COMPLETE_SECTION), [dep]).passed

    def test_dep_cannot_ride_on_another_deps_heading(self):
        """`react` must not pass via a `## react-query` section — every
        dependency needs its own heading."""
        from build_pipeline.gates import unknowns_gate
        section = COMPLETE_SECTION.replace("## mlx-whisper", "## react-query")
        r = unknowns_gate(_unknowns_doc(section), ["react", "react-query"])
        assert not r.passed
        assert any("no `## react` section" in f for f in r.metadata["findings"])

    def test_decorated_heading_still_matches(self):
        """Backticks or a trailing clause around the name are fine — the match
        is whole-token, not exact-string."""
        from build_pipeline.gates import unknowns_gate
        section = COMPLETE_SECTION.replace(
            "## mlx-whisper", "## `mlx-whisper` — local transcription"
        )
        assert unknowns_gate(_unknowns_doc(section), ["mlx-whisper"]).passed


# --- The gate's single-regeneration-pass wiring (Done-means criterion 3) ------
# Named test_unknowns_gate_* per the plan's acceptance criteria.

def _audit_setup(tmp_path, unknowns_text):
    from build_pipeline.change_manager import ChangeManager
    from build_pipeline.dependencies import NewDependency
    from unittest.mock import MagicMock

    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    cm = ChangeManager(specs_dir)
    cm.init_specs()
    change_dir = cm.create_change("explainer-test")
    output_path = change_dir / "unknowns.md"
    output_path.write_text(unknowns_text)

    config = MagicMock()
    config.model = "test-model"
    config.change_dir = change_dir
    config.project_dir = tmp_path
    config.project_description = ""

    context = cm.artifact_context(change_dir, "unknowns")
    deps = [NewDependency(name="mlx-whisper")]
    return change_dir, context, config, output_path, deps


@pytest.mark.asyncio
async def test_unknowns_gate_empty_edge_cases_triggers_exactly_one_regeneration(tmp_path):
    from unittest.mock import AsyncMock, patch
    from build_pipeline.spec_generator import _audit_unknowns

    change_dir, context, config, output_path, deps = _audit_setup(
        tmp_path, _unknowns_doc(EMPTY_EDGE_CASES_SECTION)
    )

    with patch(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", new_callable=AsyncMock
    ) as mock_gen:
        mock_gen.return_value = _unknowns_doc(COMPLETE_SECTION)
        await _audit_unknowns(change_dir, context, config, output_path, deps)

    assert mock_gen.await_count == 1
    # The findings reach the regeneration call rather than being logged only.
    findings_text = mock_gen.await_args.kwargs["audit_findings"]
    assert "Edge cases" in findings_text
    # The regenerated document is what lands on disk.
    assert "thanks for watching" in output_path.read_text()


@pytest.mark.asyncio
async def test_unknowns_gate_second_failure_does_not_loop(tmp_path):
    """A regenerated document that STILL fails the gate is accepted and logged.
    One pass, never a retry loop — the cost of a stubborn explainer is bounded."""
    from unittest.mock import AsyncMock, patch
    from build_pipeline.spec_generator import _audit_unknowns

    change_dir, context, config, output_path, deps = _audit_setup(
        tmp_path, _unknowns_doc(EMPTY_EDGE_CASES_SECTION)
    )

    with patch(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", new_callable=AsyncMock
    ) as mock_gen:
        # Still incomplete on the second pass.
        mock_gen.return_value = _unknowns_doc(EMPTY_EDGE_CASES_SECTION)
        await _audit_unknowns(change_dir, context, config, output_path, deps)

    assert mock_gen.await_count == 1


@pytest.mark.asyncio
async def test_unknowns_gate_passing_document_triggers_no_regeneration(tmp_path):
    from unittest.mock import AsyncMock, patch
    from build_pipeline.spec_generator import _audit_unknowns

    change_dir, context, config, output_path, deps = _audit_setup(
        tmp_path, _unknowns_doc(COMPLETE_SECTION)
    )

    with patch(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", new_callable=AsyncMock
    ) as mock_gen:
        await _audit_unknowns(change_dir, context, config, output_path, deps)

    assert mock_gen.await_count == 0


@pytest.mark.asyncio
async def test_unknowns_gate_regeneration_failure_leaves_first_pass_standing(tmp_path):
    from unittest.mock import AsyncMock, patch
    from build_pipeline.spec_generator import _audit_unknowns

    change_dir, context, config, output_path, deps = _audit_setup(
        tmp_path, _unknowns_doc(EMPTY_EDGE_CASES_SECTION)
    )
    before = output_path.read_text()

    with patch(
        "build_pipeline.llm_steps.spec_steps.generate_artifact", new_callable=AsyncMock
    ) as mock_gen:
        mock_gen.side_effect = RuntimeError("model unavailable")
        await _audit_unknowns(change_dir, context, config, output_path, deps)

    assert mock_gen.await_count == 1
    assert output_path.read_text() == before


# --- Prototype-first stage ---

GOOD_NOTES = """\
# Prototype notes

PROVES: The settings page fits in one column with four grouped toggles.
DISPROVES: none
OPEN_QUESTIONS: Should the reset button live above or below the toggles?
STATUS: DONE
"""


def _write_change(change_dir: Path, proposal: str, design: str = "") -> Path:
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(proposal)
    (change_dir / "design.md").write_text(design or "## Decisions\nNone yet.")
    return change_dir


def _write_prototype(proto_dir: Path, notes: str, artifact: str = "mockup.html") -> Path:
    proto_dir.mkdir(parents=True, exist_ok=True)
    if artifact:
        (proto_dir / artifact).write_text("<html><body>hi</body></html>")
    (proto_dir / "NOTES.md").write_text(notes)
    return proto_dir


class TestPrototypeRequired:
    def test_frontend_change_requires_prototype(self, tmp_path):
        _write_change(
            tmp_path / "change",
            "## Why\nUsers need a settings page.\n\n"
            "## What Changes\nA new React component renders the form in the "
            "browser with Tailwind CSS. The UI has a button per toggle and a "
            "page-level layout in the DOM.\n",
        )
        assert prototype_required(tmp_path / "change") is True

    def test_plain_backend_change_does_not(self, tmp_path):
        _write_change(
            tmp_path / "change",
            "## Why\nThe worker queue drops jobs under load.\n\n"
            "## What Changes\nAdd a retry column to the jobs database table, a "
            "migration for it, and a celery worker that re-enqueues failed "
            "jobs via the internal API endpoint.\n",
        )
        assert prototype_required(tmp_path / "change") is False

    def test_backend_change_with_user_visible_output_requires_prototype(self, tmp_path):
        _write_change(
            tmp_path / "change",
            "## Why\nOps need a weekly summary.\n\n"
            "## What Changes\nA cron worker queries the database and writes a "
            "weekly report as a CSV export.\n",
        )
        assert prototype_required(tmp_path / "change") is True

    def test_output_format_mentioned_only_in_design(self, tmp_path):
        _write_change(
            tmp_path / "change",
            "## Why\nBetter diagnostics.\n\n## What Changes\nAn API endpoint.\n",
            design="## Decisions\nThe command prints its findings as CLI output "
                   "in a fixed column layout.\n",
        )
        assert prototype_required(tmp_path / "change") is True

    def test_missing_artifacts_do_not_crash(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert prototype_required(empty) is False


class TestParsePrototypeNotes:
    def test_parses_all_slots(self):
        notes = parse_prototype_notes(GOOD_NOTES)
        assert notes["proves"].startswith("The settings page")
        assert notes["disproves"] == "none"
        assert notes["open_questions"].startswith("Should the reset button")
        assert notes["status"] == "DONE"

    def test_multiline_slot_value(self):
        text = (
            "PROVES: one\ncontinued on the next line\n"
            "DISPROVES: none\nOPEN_QUESTIONS: none\nSTATUS: DONE\n"
        )
        notes = parse_prototype_notes(text)
        assert "continued on the next line" in notes["proves"]

    def test_slot_has_content_treats_none_as_empty(self):
        assert not slot_has_content("none")
        assert not slot_has_content("  N/A ")
        assert not slot_has_content(None)
        assert slot_has_content("the header wraps at 320px")


class TestPrototypeGate:
    def test_passes_with_artifact_and_complete_notes(self, tmp_path):
        proto = _write_prototype(tmp_path / "prototype", GOOD_NOTES)
        r = prototype_gate(proto)
        assert r.passed, r.reason
        assert r.metadata["status"] == "DONE"

    def test_fails_on_empty_proves_slot(self, tmp_path):
        notes = "PROVES:\nDISPROVES: none\nOPEN_QUESTIONS: none\nSTATUS: DONE\n"
        proto = _write_prototype(tmp_path / "prototype", notes)
        r = prototype_gate(proto)
        assert not r.passed
        assert "PROVES" in r.reason
        assert r.action == "retry_prototype"

    def test_fails_on_empty_open_questions_slot(self, tmp_path):
        notes = "PROVES: the layout\nDISPROVES: none\nOPEN_QUESTIONS:\nSTATUS: DONE\n"
        proto = _write_prototype(tmp_path / "prototype", notes)
        r = prototype_gate(proto)
        assert not r.passed
        assert "OPEN_QUESTIONS" in r.reason

    def test_fails_when_only_notes_exist(self, tmp_path):
        proto = _write_prototype(tmp_path / "prototype", GOOD_NOTES, artifact="")
        r = prototype_gate(proto)
        assert not r.passed
        assert "besides NOTES.md" in r.reason

    def test_fails_on_missing_notes(self, tmp_path):
        proto = tmp_path / "prototype"
        proto.mkdir()
        (proto / "mockup.html").write_text("<html></html>")
        r = prototype_gate(proto)
        assert not r.passed
        assert "NOTES.md missing" in r.reason

    def test_fails_on_missing_directory(self, tmp_path):
        r = prototype_gate(tmp_path / "nope")
        assert not r.passed
        assert "not created" in r.reason

    def test_fails_on_missing_slots(self, tmp_path):
        proto = _write_prototype(tmp_path / "prototype", "PROVES: a layout\n")
        r = prototype_gate(proto)
        assert not r.passed
        assert "REQUIRED slots" in r.reason

    def test_blocked_status_fails_immediately_with_the_stated_blocker(self, tmp_path):
        """An honest BLOCKED (usually NOTES.md only, no artifact) must surface
        the agent's stated blocker, not a misleading empty-slot failure."""
        notes = (
            "The proposal never says which fields the report carries, so no "
            "shape can be drawn.\n"
            "PROVES: none\nDISPROVES: none\nOPEN_QUESTIONS: none\n"
            "STATUS: BLOCKED\n"
        )
        proto = _write_prototype(tmp_path / "prototype", notes, artifact="")
        r = prototype_gate(proto)
        assert not r.passed
        assert r.action == "prototype_blocked"
        assert r.metadata["status"] == "BLOCKED"
        assert "STATUS: BLOCKED" in r.reason
        assert "never says which fields" in r.reason

    def test_needs_context_status_fails_like_blocked(self, tmp_path):
        notes = (
            "PROVES: none\nDISPROVES: none\nOPEN_QUESTIONS: none\n"
            "STATUS: NEEDS_CONTEXT — the design has no Decisions section\n"
        )
        proto = _write_prototype(tmp_path / "prototype", notes)
        r = prototype_gate(proto)
        assert not r.passed
        assert r.action == "prototype_blocked"
        assert r.metadata["status"] == "NEEDS_CONTEXT"


class TestParseImplementationNote:
    """The SURPRISES:/SPEC_IMPACT: slots that feed the respec loop."""

    IMPL_REPORT = """\
Implemented the uploader.

STATUS: DONE
TESTS_RUN: pytest -xvs
GREEN: 42 passed in 3.1s
FILES: uploader.py
SURPRISES: The storage client raises on timeout; the design assumed a
return code. I wrapped the call to keep the block's tests green.
SPEC_IMPACT: contradicts
"""

    def _parse(self, output, role="implementer"):
        return parse_implementation_note(
            output, block_number=3, block_name="Uploader", role=role,
        )

    def test_parses_contradiction_with_multiline_surprises(self):
        note = self._parse(self.IMPL_REPORT)
        assert note is not None
        assert note.spec_impact == "contradicts"
        assert note.contradicts
        assert note.block_number == 3
        assert note.block_name == "Uploader"
        assert note.role == "implementer"
        assert "raises on timeout" in note.surprises
        assert "return code" in note.surprises
        # The surprises text stops at the next REQUIRED slot.
        assert "SPEC_IMPACT" not in note.surprises

    def test_none_on_both_slots_records_nothing(self):
        note = self._parse(
            "STATUS: DONE\nFILES: a.py\nSURPRISES: none\nSPEC_IMPACT: none\n"
        )
        assert note is None

    def test_no_slots_at_all_records_nothing(self):
        assert self._parse("STATUS: DONE\nFILES: a.py\n") is None

    def test_clarify_is_recorded(self):
        note = self._parse(
            "SURPRISES: Token TTL was unspecified; I used 15 minutes.\n"
            "SPEC_IMPACT: clarify\n"
        )
        assert note is not None
        assert note.spec_impact == "clarify"
        assert "15 minutes" in note.surprises

    def test_surprises_without_impact_slot_defaults_to_none_but_is_kept(self):
        note = self._parse("SURPRISES: The API paginates; the design did not say so.\n")
        assert note is not None
        assert note.spec_impact == "none"
        assert "paginates" in note.surprises

    def test_echoed_template_placeholder_is_not_a_finding(self):
        note = self._parse(
            "SURPRISES: <what did not match the spec/design, or \"none\">\n"
            "SPEC_IMPACT: none\n"
        )
        assert note is None

    def test_last_occurrence_wins_over_quoted_instructions(self):
        """A report that restates the template before answering must not have
        the template outrank the agent's own answer."""
        output = (
            "I was asked to end with:\n"
            "SURPRISES: <what did not match the spec/design, or \"none\">\n"
            "SPEC_IMPACT: none\n"
            "\nHere is my report.\n"
            "STATUS: DONE\n"
            "SURPRISES: The queue drops duplicates silently.\n"
            "SPEC_IMPACT: contradicts\n"
        )
        note = self._parse(output)
        assert note is not None
        assert note.spec_impact == "contradicts"
        assert "drops duplicates" in note.surprises

    def test_markdown_decorated_slots_are_parsed(self):
        note = self._parse(
            "- **SURPRISES:** The CLI writes to stderr, not stdout.\n"
            "- **SPEC_IMPACT:** clarify\n"
        )
        assert note is not None
        assert note.spec_impact == "clarify"
        assert "stderr" in note.surprises

    def test_role_is_recorded_for_the_test_writer(self):
        note = self._parse(
            "SURPRISES: The scenario names a type that does not exist.\n"
            "SPEC_IMPACT: contradicts\n",
            role="test_writer",
        )
        assert note.role == "test_writer"

    def test_colonless_coaching_echo_never_outranks_the_real_slot(self):
        """A real SURPRISES: followed by an echo of the prompt's coaching
        sentence ("SURPRISES and SPEC_IMPACT close the loop...") must keep the
        real finding — the colon is what makes a line a slot."""
        note = self._parse(
            "SURPRISES: The queue drops duplicates silently.\n"
            "SPEC_IMPACT: contradicts\n"
            "\nSURPRISES and SPEC_IMPACT close the loop between the spec and "
            "what the build learned.\n"
        )
        assert note is not None
        assert "drops duplicates" in note.surprises
        assert "close the loop" not in note.surprises
        assert note.spec_impact == "contradicts"

    def test_colonless_prose_mention_records_nothing(self):
        assert self._parse(
            "STATUS: DONE\nFILES: a.py\n"
            "Surprises were minimal this block.\n"
        ) is None

    def test_continuation_line_starting_with_a_slot_word_is_kept(self):
        """A multiline value whose continuation line opens with a slot word
        (no colon) must not be truncated at that line."""
        note = self._parse(
            "SURPRISES: The migration assumes a clean schema, but the\n"
            "STATUS quo in the repo differs: two tables already exist.\n"
            "SPEC_IMPACT: clarify\n"
        )
        assert note is not None
        assert "STATUS quo in the repo differs" in note.surprises
        assert note.spec_impact == "clarify"

    def test_leading_bullet_of_the_value_is_not_eaten(self):
        note = self._parse(
            "SURPRISES:\n- The API paginates.\n- Tokens expire hourly.\n"
            "SPEC_IMPACT: clarify\n"
        )
        assert note is not None
        assert "- The API paginates." in note.surprises
        assert "- Tokens expire hourly." in note.surprises
