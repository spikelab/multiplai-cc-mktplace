"""Tests for quality gates — pure code assertions."""

from pathlib import Path

import pytest
from build_pipeline.gates import (
    agent_status_gate,
    baseline_test_gate,
    integration_gate,
    parse_agent_status,
    parse_implementation_note,
    parse_test_change_claims,
    red_gate,
    review_iteration_gate,
    review_score_gate,
    run_test_suite,
    unchanged_tests_gate,
    wiring_task_gate,
)
from build_pipeline.models import ReviewGatePolicy, ReviewResult, ReviewScore


class TestReviewScoreGate:
    def test_passes_above_threshold(self):
        r = ReviewResult(
            scores=[
                ReviewScore(dimension="A", weight=2, score=4, evidence=""),
                ReviewScore(dimension="B", weight=1, score=4, evidence=""),
            ]
        )
        result = review_score_gate(r)
        assert result.passed

    def test_fails_below_threshold(self):
        r = ReviewResult(
            scores=[
                ReviewScore(dimension="A", weight=2, score=2, evidence=""),
                ReviewScore(dimension="B", weight=1, score=3, evidence=""),
            ]
        )
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_low_scores"

    def test_fails_with_dimension_at_1(self):
        r = ReviewResult(
            scores=[
                ReviewScore(dimension="A", weight=2, score=5, evidence=""),
                ReviewScore(dimension="B", weight=1, score=1, evidence=""),
            ]
        )
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
        output = (
            "FAILED tests/test_auth.py::test_login - AssertionError: expected token"
        )
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

    # The run that rejected a test writer twice (2026-10-23): five NameErrors
    # and two `pytest.fail()` calls, printed by `uv run pytest -q`.
    PYTEST_FAIL_OUTPUT = (
        "FFFFFFF...                                                [100%]\n"
        "=================================== FAILURES ===================\n"
        "E       NameError: name '_has_local_leakage' is not defined\n"
        "\n"
        "tests/test_repo_hygiene.py:143: NameError\n"
        ">           pytest.fail(\n"
        "E           Failed: _repo_root must call pytest.skip(...) instead of "
        "raising TypeError: _repo_root() got an unexpected keyword argument 'cwd'\n"
        "\n"
        "tests/test_repo_hygiene.py:202: Failed\n"
        "=========================== short test summary info ============\n"
        "FAILED tests/test_repo_hygiene.py::test_local_leakage_detector_flags_session_uuids\n"
        "FAILED tests/test_repo_hygiene.py::test_repo_root_skips_cleanly_outside_a_git_worktree\n"
        "7 failed, 3 passed in 0.06s\n"
    )

    def test_passes_on_pytest_fail(self):
        result = red_gate(self.PYTEST_FAIL_OUTPUT, 1)
        assert result.passed
        assert result.metadata["classification"] == "behavioral"

    @pytest.mark.parametrize(
        "output",
        [
            # --tb=short / long: pytest.raises and pytest.warns that saw nothing
            "FAILED tests/test_a.py::test_x\nE   Failed: DID NOT RAISE ValueError\n",
            "FAILED tests/test_a.py::test_x\nE   Failed: DID NOT WARN. No warnings\n",
            # --tb=line: `path:line: message`, no `E` line for the summary
            (
                "/p/tests/test_a.py:7: Failed: not implemented yet\n"
                "FAILED tests/test_a.py::test_x\n"
            ),
            "/p/tests/test_a.py:4: assert 1 == 2\nFAILED tests/test_a.py::test_x\n",
            # --tb=no -rf: only the short summary carries the message
            "FAILED tests/test_a.py::test_x - Failed: not implemented yet\n",
            "FAILED tests/test_a.py::test_x - assert 1 == 2\n",
        ],
        ids=[
            "did_not_raise",
            "did_not_warn",
            "tb_line_failed",
            "tb_line_assert",
            "summary_failed",
            "summary_assert",
        ],
    )
    def test_passes_on_other_pytest_failure_formats(self, output):
        result = red_gate(output, 1)
        assert result.passed
        assert result.metadata["classification"] == "behavioral"

    @pytest.mark.parametrize(
        "output",
        [
            # pytest.fail() at module level is a collection error
            (
                "ERROR collecting tests/test_a.py\nE   Failed: boom\n"
                "FAILED tests/test_a.py::test_x\n"
            ),
            # pytest.fail() in a fixture is a setup error
            (
                "ERROR at setup of test_x\nE   Failed: boom\n"
                "FAILED tests/test_a.py::test_x\n"
            ),
        ],
        ids=["collection", "setup"],
    )
    def test_pytest_fail_outside_a_test_body_is_still_rejected(self, output):
        result = red_gate(output, 1)
        assert not result.passed
        assert result.action == "fix_tests"

    @pytest.mark.parametrize(
        "output",
        [
            "FAILED tests/test_a.py::test_x\nE   NameError: name 'f' is not defined\n",
            "FAILED tests/test_a.py::test_x\nE   TypeError: f() takes 0 arguments\n",
            "FAILED tests/test_a.py::test_x - NameError: name 'f' is not defined\n",
            # prose that merely contains the word
            "FAILED tests/test_a.py::test_x\nE   ValueError: Failed: to parse\n",
        ],
        ids=["name_error", "type_error", "summary_name_error", "failed_in_prose"],
    )
    def test_errors_in_test_code_are_still_not_right_reason(self, output):
        result = red_gate(output, 1)
        assert not result.passed
        assert result.action == "fix_tests"

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

    def test_unrecognized_pytest_exit_one_is_unverified(self):
        result = red_gate("something exploded unrecognizably", 1)
        assert result.passed
        assert result.metadata["classification"] == "unverified"

    def test_unrunnable_suite_means_fix_tests(self):
        """exit_code=None (untrusted repo / missing binary) is not RED proof."""
        result = red_gate("Repo not trusted", None)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_passes_on_terse_lowercase_summary(self):
        """pytest -q --tb=no emits only a summary count — no FAILED lines."""
        result = red_gate("1 failed, 3 passed in 0.12s", 1)
        assert result.passed
        assert result.metadata["classification"] == "unverified"

    def test_passes_on_jest_style_output(self):
        """Jest/Vitest print `FAIL <file>` and a lowercase `N failed` summary —
        no AssertionError, no uppercase FAILED."""
        output = (
            "FAIL src/auth.test.js\n"
            "  ● login › returns a token\n"
            "Tests: 1 failed, 2 passed, 3 total\n"
        )
        result = red_gate(output, 1, runner="jest")
        assert result.passed
        assert result.metadata["classification"] == "unverified"

    def test_zero_failed_summary_with_exit_one_is_only_unverified(self):
        result = red_gate("0 failed, 5 passed", 1)
        assert result.passed
        assert result.metadata["classification"] == "unverified"

    def test_lowercase_fail_in_prose_with_exit_one_is_only_unverified(self):
        """Prose supplies no behavioral evidence; exit 1 permits only unverified RED."""
        result = red_gate("warning: flaky tests may fail intermittently", 1)
        assert result.passed
        assert result.metadata["classification"] == "unverified"


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


class TestTestGatesShareOneRunner:
    """`run_test_suite` owns the trust guard and the subprocess mechanics; the
    baseline and integration gates only translate its result into a
    GateResult. Three copies of the same subprocess block is how the trust
    guard came to be written three times."""

    def test_neither_gate_runs_the_suite_itself(self):
        import inspect

        from build_pipeline import gates as gates_mod

        for gate in (gates_mod.baseline_test_gate, gates_mod.integration_gate):
            src = inspect.getsource(gate)
            assert "subprocess.run" not in src, (
                f"{gate.__name__} runs the suite directly"
            )
            assert "_suite_run(" in src, f"{gate.__name__} does not use _suite_run"
        src = inspect.getsource(gates_mod._suite_run)
        assert "subprocess.run" not in src and "run_test_suite(" in src

    def test_both_gates_delegate_at_runtime(self, tmp_path, trust_repo):
        from unittest.mock import patch

        for gate in (baseline_test_gate, integration_gate):
            with patch(
                "build_pipeline.gates.run_test_suite", return_value=(0, "5 passed")
            ) as runner:
                assert gate("pytest -q", tmp_path).passed
            runner.assert_called_once()

    def test_an_unverifiable_suite_never_reads_as_a_pass(self, tmp_path, trust_repo):
        """exit_code None means 'could not verify' — both gates must fail."""
        from unittest.mock import patch

        for gate in (baseline_test_gate, integration_gate):
            with patch(
                "build_pipeline.gates.run_test_suite",
                return_value=(None, "Test command failed to run: boom"),
            ):
                result = gate("pytest -q", tmp_path)
            assert not result.passed
            assert "boom" in result.reason


class TestParseAgentStatus:
    """Agents close their report with a REQUIRED STATUS slot (Item 7)."""

    def test_parses_plain_slot(self):
        assert (
            parse_agent_status("Wrote tests.\n\nSTATUS: DONE\nTESTS_RUN: pytest\n")
            == "DONE"
        )

    def test_parses_underscored_variant(self):
        assert (
            parse_agent_status("STATUS: DONE_WITH_CONCERNS\n") == "DONE_WITH_CONCERNS"
        )
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
        r = agent_status_gate(
            "STATUS: DONE_WITH_CONCERNS\nMock is thin.\n", "Implementer"
        )
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


class TestTestIntegrityGate:
    """The tests that gate a block must be the tests that gated it."""

    def test_unchanged_tests_pass(self):
        snap = {"tests/test_a.py": "abc", "tests/test_b.py": "def"}
        r = unchanged_tests_gate(snap, dict(snap))
        assert r.passed
        assert r.metadata["checked"] is True

    def test_mutated_test_file_fails(self):
        r = unchanged_tests_gate(
            {"tests/test_a.py": "abc"}, {"tests/test_a.py": "MUTATED"}
        )
        assert not r.passed
        assert r.action == "restore_tests"
        assert "tests/test_a.py" in r.reason

    def test_deleted_test_file_fails(self):
        r = unchanged_tests_gate({"tests/test_a.py": "abc"}, {})
        assert not r.passed
        assert "deleted" in r.reason

    def test_no_snapshot_reports_not_run_rather_than_failing(self):
        """An unverifiable gate must not fail a block on absent evidence."""
        r = unchanged_tests_gate({}, {"tests/test_a.py": "abc"})
        assert r.passed
        assert r.metadata["checked"] is False

    def test_declared_change_flags_instead_of_failing(self):
        r = unchanged_tests_gate(
            {"tests/test_a.py": "abc"},
            {"tests/test_a.py": "changed"},
            implementer_report="TEST CHANGE REQUIRED: the test asserted the wrong error type",
        )
        assert r.passed
        assert r.action == "flag_test_change_claim"
        assert r.metadata["flagged"] is True
        assert r.metadata["claims"] == ["the test asserted the wrong error type"]

    def test_declaration_is_a_claim_not_a_licence(self):
        """The declared reason reaches the reviewer as an unverified claim."""
        r = unchanged_tests_gate(
            {"tests/test_a.py": "abc"},
            {"tests/test_a.py": "changed"},
            implementer_report="TEST CHANGE REQUIRED: because",
        )
        assert "unverified claim" in r.reason

    def test_new_test_files_are_not_a_violation(self):
        """Adding tests is fine; only changing the gating ones is not."""
        r = unchanged_tests_gate(
            {"tests/test_a.py": "abc"},
            {"tests/test_a.py": "abc", "tests/test_new.py": "xyz"},
        )
        assert r.passed


class TestParseTestChangeClaims:
    def test_extracts_each_declaration(self):
        report = (
            "Did the work.\n"
            "TEST CHANGE REQUIRED: fixture path was wrong\n"
            "More prose.\n"
            "TEST CHANGE REQUIRED: assertion contradicted the spec\n"
        )
        assert parse_test_change_claims(report) == [
            "fixture path was wrong",
            "assertion contradicted the spec",
        ]

    def test_empty_report(self):
        assert parse_test_change_claims("") == []
        assert parse_test_change_claims("no marker here") == []


class TestGradedReviewGate:
    def _scores(self, *specs):
        return [
            ReviewScore(dimension=d, weight=w, score=s, evidence="e", confidence=c)
            for d, w, s, c in specs
        ]

    def test_default_confidence_reproduces_binary_behavior(self):
        """Every existing fixture scores exactly as it did before grading."""
        r = ReviewResult(scores=self._scores(("A", 2, 4, 1.0), ("B", 1, 3, 1.0)))
        gate = review_score_gate(r)
        assert gate.passed
        assert gate.metadata["graded"] is False

    def test_low_confidence_bad_score_stops_counting(self):
        """Low confidence is weak evidence, not strong evidence of badness."""
        r = ReviewResult(scores=self._scores(("A", 2, 1, 0.0)))
        gate = review_score_gate(r)
        assert gate.passed  # shrunk all the way to the neutral 3.5
        assert gate.metadata["graded"] is True

    def test_confident_critical_still_fails(self):
        r = ReviewResult(scores=self._scores(("A", 2, 1, 1.0)))
        gate = review_score_gate(r)
        assert not gate.passed
        assert gate.action == "fix_critical_dimension"

    def test_spec_compliance_is_a_hard_floor_regardless_of_scores(self):
        r = ReviewResult(
            scores=self._scores(("A", 2, 5, 1.0)),
            missing=["the retry path is not implemented"],
        )
        gate = review_score_gate(r)
        assert not gate.passed
        assert gate.action == "fix_spec_compliance"

    def test_policy_thresholds_override_defaults(self):
        r = ReviewResult(scores=self._scores(("A", 2, 3, 1.0)))
        assert not review_score_gate(r).passed
        assert review_score_gate(r, ReviewGatePolicy(min_weighted_average=3.0)).passed


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
        """ "Nothing new to this project" is a recorded finding, not a failure."""
        from build_pipeline.gates import unknowns_gate

        r = unknowns_gate("# Unknowns\n\nNo dependencies new to this project.\n", [])
        assert r.passed
        assert r.metadata["dependencies"] == []

    def test_missing_section_fails(self):
        from build_pipeline.gates import unknowns_gate

        r = unknowns_gate(_unknowns_doc(COMPLETE_SECTION), ["mlx-whisper", "polars"])
        assert not r.passed
        assert r.action == "regenerate_unknowns"
        assert any(
            "polars" in f and "no `## polars` section" in f
            for f in r.metadata["findings"]
        )

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
            line
            for line in section.splitlines()
            if not line.startswith(
                (
                    "- Malformed",
                    "- Oversized",
                    "- Concurrent",
                    "- Offline",
                    "  not an empty",
                )
            )
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
    from unittest.mock import MagicMock

    from build_pipeline.change_manager import ChangeManager
    from build_pipeline.dependencies import NewDependency

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
async def test_unknowns_gate_empty_edge_cases_triggers_exactly_one_regeneration(
    tmp_path,
):
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
            output,
            block_number=3,
            block_name="Uploader",
            role=role,
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
            'SURPRISES: <what did not match the spec/design, or "none">\n'
            "SPEC_IMPACT: none\n"
        )
        assert note is None

    def test_last_occurrence_wins_over_quoted_instructions(self):
        """A report that restates the template before answering must not have
        the template outrank the agent's own answer."""
        output = (
            "I was asked to end with:\n"
            'SURPRISES: <what did not match the spec/design, or "none">\n'
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
        assert (
            self._parse(
                "STATUS: DONE\nFILES: a.py\nSurprises were minimal this block.\n"
            )
            is None
        )

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


# --- Documentation phase (DOCS_IMPACT: slot + freshness warning) ----------

from build_pipeline.gates import (
    docs_freshness_gate,
    is_doc_path,
    parse_diff_paths,
    parse_docs_impact,
    source_paths_in_diff,
)


def _diff_for(*paths: str) -> str:
    return "\n".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new"
        for p in paths
    )


class TestParseDocsImpact:
    def test_none_is_an_answer_and_parses_to_an_empty_list(self):
        assert parse_docs_impact("DOCS_IMPACT: none\nSTATUS: DONE\n") == []

    def test_missing_slot_is_distinguishable_from_none(self):
        assert parse_docs_impact("STATUS: DONE\n") is None

    def test_single_file(self):
        assert parse_docs_impact("DOCS_IMPACT: README.md\n") == ["README.md"]

    def test_comma_separated_list_is_split_and_stripped(self):
        assert parse_docs_impact(
            "DOCS_IMPACT: README.md, CHANGELOG.md , docs/usage.md\nSTATUS: DONE\n"
        ) == ["README.md", "CHANGELOG.md", "docs/usage.md"]

    def test_bold_bulleted_slot_label_is_accepted(self):
        assert parse_docs_impact("- **DOCS_IMPACT:** `README.md`\n") == ["README.md"]

    def test_last_occurrence_wins_over_a_quoted_instruction(self):
        out = parse_docs_impact(
            "The report must end with DOCS_IMPACT: none as instructed.\n"
            "DOCS_IMPACT: docs/board.md\n"
        )
        assert out == ["docs/board.md"]

    def test_echoed_template_placeholder_is_not_an_answer(self):
        """The literal shipped template, not a hand-shortened stand-in — a
        stand-in once masked the wrapped-placeholder case below."""
        from build_pipeline.prompts.docs_update import DOCS_UPDATE_PROMPT

        echoed = DOCS_UPDATE_PROMPT.format(
            project_dir="/tmp/project",
            change_name="feat",
            diff="",
            notes="",
        )
        assert parse_docs_impact(echoed) is None

    def test_the_prompt_keeps_the_no_changelog_note_out_of_the_slot(self):
        """DOCS_IMPACT is parsed as the list of files to commit, so the prompt
        must not ask for a free-text note there."""
        from build_pipeline.prompts.docs_update import DOCS_UPDATE_PROMPT

        assert "record that under DOCS_IMPACT" not in DOCS_UPDATE_PROMPT
        assert "only file paths" in DOCS_UPDATE_PROMPT

    def test_echoed_placeholder_wrapping_onto_a_second_line_is_not_an_answer(self):
        """The regression this guards: the capture regex takes one line, so an
        echo of a wrapped placeholder starts with `<` but never reaches the
        closing `>` — a guard requiring both brackets let it through as a
        garbage path list."""
        assert (
            parse_docs_impact(
                "DOCS_IMPACT: <none, or a comma-separated list of the documentation files you\n"
                "              changed, as paths relative to the project root>\n"
            )
            is None
        )

    def test_echoed_single_line_placeholder_is_not_an_answer(self):
        assert (
            parse_docs_impact("DOCS_IMPACT: <none, or the files you changed>\n") is None
        )


class TestDiffPathHelpers:
    def test_parses_every_touched_path_once(self):
        assert parse_diff_paths(_diff_for("src/a.py", "README.md")) == [
            "src/a.py",
            "README.md",
        ]

    def test_renames_report_both_sides(self):
        diff = "diff --git a/old.py b/new.py\n"
        assert parse_diff_paths(diff) == ["old.py", "new.py"]

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "readme.rst",
            "CHANGELOG.md",
            "CHANGELOG",
            "docs/usage.md",
            "plugins/x/docs/design.md",
            "doc/index.md",
        ],
    )
    def test_documentation_paths_are_recognised(self, path):
        assert is_doc_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/app.py",
            "tests/test_app.py",
            "package.json",
            "src/readme_parser.py",
            "src/docs_builder.py",
            # Living under docs/ does not make a file a document. Every path this
            # matches stops counting as source in the freshness warning, so code
            # that happens to sit in a docs tree must not shrink the evidence.
            "src/docs/generator.py",
            "docs/conf.py",
            "docs/_static/theme.css",
            "doc/scripts/build_index.ts",
        ],
    )
    def test_source_paths_are_not_documentation(self, path):
        assert not is_doc_path(path)

    def test_a_docs_tree_full_of_code_still_warns_about_staleness(self, tmp_path):
        """The regression this guards: with `docs/**` matched by directory
        alone, a change to docs/generator.py counted as documentation, the
        source set came out empty, and the gate reported 'nothing to
        document' on a build that changed real code."""
        (tmp_path / "CHANGELOG.md").write_text("# Changelog")
        diff = _diff_for("docs/generator.py", "docs/usage.md")
        assert source_paths_in_diff(diff) == ["docs/generator.py"]
        assert docs_freshness_gate(diff, [], tmp_path).action == "docs_may_be_stale"

    def test_specs_are_the_builds_paperwork_not_source(self):
        diff = _diff_for(
            "specs/changes/c/implementation-plan.md", "src/app.py", "README.md"
        )
        assert source_paths_in_diff(diff) == ["src/app.py"]


class TestDocsFreshnessGate:
    """The warn gate: it reports, it never fails a finished build."""

    def _project(self, tmp_path, *files: str) -> Path:
        for name in files:
            (tmp_path / name).write_text("x")
        return tmp_path

    def test_code_changed_with_a_changelog_and_no_docs_warns(self, tmp_path):
        project = self._project(tmp_path, "CHANGELOG.md")
        gate = docs_freshness_gate(_diff_for("src/app.py"), [], project)

        assert gate.passed is True  # never fails
        assert gate.action == "docs_may_be_stale"
        assert gate.metadata["warning"] is True
        assert gate.metadata["changelog"] == "CHANGELOG.md"
        assert gate.metadata["source_files"] == ["src/app.py"]
        assert "CHANGELOG.md" in gate.reason

    def test_a_missing_slot_warns_the_same_as_none(self, tmp_path):
        project = self._project(tmp_path, "CHANGELOG.md")
        gate = docs_freshness_gate(_diff_for("src/app.py"), None, project)
        assert gate.passed is True
        assert gate.action == "docs_may_be_stale"

    def test_never_fails_even_when_it_warns(self, tmp_path):
        """The whole point of the gate: some changes have no user-visible
        delta, so a documentation judgment must not fail a built change."""
        project = self._project(tmp_path, "CHANGELOG.md")
        for impact in ([], None):
            assert docs_freshness_gate(_diff_for("src/a.py"), impact, project).passed

    def test_docs_updated_means_no_warning(self, tmp_path):
        project = self._project(tmp_path, "CHANGELOG.md")
        gate = docs_freshness_gate(
            _diff_for("src/app.py"),
            ["CHANGELOG.md"],
            project,
        )
        assert gate.passed is True
        assert gate.action is None
        assert gate.metadata["docs_impact"] == ["CHANGELOG.md"]

    def test_no_changelog_means_no_warning(self, tmp_path):
        gate = docs_freshness_gate(_diff_for("src/app.py"), [], self._project(tmp_path))
        assert gate.passed is True
        assert gate.action is None
        assert "keeps no changelog" in gate.reason

    def test_changelog_is_matched_case_insensitively(self, tmp_path):
        project = self._project(tmp_path, "changelog.rst")
        gate = docs_freshness_gate(_diff_for("src/app.py"), [], project)
        assert gate.action == "docs_may_be_stale"
        assert gate.metadata["changelog"] == "changelog.rst"

    def test_docs_only_diff_has_nothing_to_document(self, tmp_path):
        project = self._project(tmp_path, "CHANGELOG.md")
        gate = docs_freshness_gate(_diff_for("README.md"), [], project)
        assert gate.passed is True
        assert gate.action is None
        assert gate.metadata["source_files"] == []

    def test_empty_diff_never_warns(self, tmp_path):
        project = self._project(tmp_path, "CHANGELOG.md")
        assert docs_freshness_gate("", [], project).action is None

    def test_a_missing_project_dir_never_raises(self, tmp_path):
        gate = docs_freshness_gate(_diff_for("src/a.py"), [], tmp_path / "gone")
        assert gate.passed is True
        assert gate.action is None


class TestParallelFullSuite:
    """Item 4 of plan-review-rounds-and-finders-2026-10-15.md: a full-suite
    pytest run gets `-n auto` when pytest-xdist imports in the environment
    pytest runs in, and never under `tdd.parallel: off`."""

    @staticmethod
    def _venv(tmp_path, *, xdist: bool) -> Path:
        """A project whose `.venv/bin/pytest` echoes its arguments and whose
        `.venv/bin/python` imports xdist or fails to."""
        bin_dir = tmp_path / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "pytest").write_text('#!/bin/sh\necho "ARGS: $*"\n')
        (bin_dir / "python").write_text(
            '#!/bin/sh\n[ "$1" = "-m" ] && shift 2 && echo "ARGS: $*" && exit 0\n'
            f'[ "$*" = "-c import xdist" ] && exit {0 if xdist else 1}\nexit 2\n'
        )
        for name in ("pytest", "python"):
            (bin_dir / name).chmod(0o755)
        return tmp_path

    @pytest.mark.parametrize("gate", [baseline_test_gate, integration_gate])
    def test_xdist_importable_runs_the_suite_with_n_auto(
        self, tmp_path, trust_repo, gate
    ):
        p = self._venv(tmp_path, xdist=True)
        result = gate(".venv/bin/pytest -q", p, parallel="auto")
        assert result.passed
        assert "ARGS: -q -n auto\n" in result.metadata["output"]

    @pytest.mark.parametrize("gate", [baseline_test_gate, integration_gate])
    def test_xdist_missing_runs_the_command_as_written(
        self, tmp_path, trust_repo, gate
    ):
        p = self._venv(tmp_path, xdist=False)
        result = gate(".venv/bin/pytest -q", p, parallel="auto")
        assert "ARGS: -q\n" in result.metadata["output"]

    @pytest.mark.parametrize("gate", [baseline_test_gate, integration_gate])
    def test_parallel_off_never_adds_workers(self, tmp_path, trust_repo, gate):
        p = self._venv(tmp_path, xdist=True)
        assert (
            "ARGS: -q\n"
            in gate(".venv/bin/pytest -q", p, parallel="off").metadata["output"]
        )
        assert "ARGS: -q\n" in gate(".venv/bin/pytest -q", p).metadata["output"]

    def test_python_dash_m_pytest_probes_that_python(self, tmp_path, trust_repo):
        p = self._venv(tmp_path, xdist=True)
        result = integration_gate(".venv/bin/python -m pytest -q", p, parallel="auto")
        assert "ARGS: -q -n auto\n" in result.metadata["output"]

    @pytest.mark.parametrize(
        "command",
        [
            ".venv/bin/pytest -n 2",
            ".venv/bin/pytest -n4",
            ".venv/bin/pytest --numprocesses=2",
            ".venv/bin/pytest -p no:xdist",
            ".venv/bin/pytest --pdb",
            ".venv/bin/pytest -q -- tests",
        ],
    )
    def test_a_command_that_chose_its_workers_is_kept(self, tmp_path, command):
        from build_pipeline.gates import parallel_command

        p = self._venv(tmp_path, xdist=True)
        assert parallel_command(command, p, "auto") == command

    def test_addopts_that_set_workers_are_kept(self, tmp_path, monkeypatch):
        from build_pipeline.gates import parallel_command

        p = self._venv(tmp_path, xdist=True)
        (p / "pytest.ini").write_text("[pytest]\naddopts = -n 4\n")
        assert (
            parallel_command(".venv/bin/pytest -q", p, "auto") == ".venv/bin/pytest -q"
        )
        (p / "pytest.ini").unlink()
        monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:xdist")
        assert (
            parallel_command(".venv/bin/pytest -q", p, "auto") == ".venv/bin/pytest -q"
        )

    @pytest.mark.parametrize(
        "env, command",
        [
            ({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, ".venv/bin/pytest -q"),
            (
                {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0"},
                ".venv/bin/pytest -q",
            ),  # any value turns it off
            (
                {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": "xdist"},
                ".venv/bin/pytest -q",
            ),
            ({}, ".venv/bin/pytest -q --disable-plugin-autoload"),
        ],
    )
    def test_xdist_not_loaded_without_autoload_is_kept(
        self, tmp_path, monkeypatch, env, command
    ):
        """With plugin autoloading off, pytest loads xdist only when `-p` or
        `PYTEST_PLUGINS` names it, and rejects `-n` otherwise (exit 4)."""
        from build_pipeline.gates import parallel_command

        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert (
            parallel_command(command, self._venv(tmp_path, xdist=True), "auto")
            == command
        )

    @pytest.mark.parametrize(
        "env, command",
        [
            ({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, ".venv/bin/pytest -q -p xdist"),
            (
                {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                ".venv/bin/pytest -q -pxdist.plugin",
            ),
            (
                {
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTEST_PLUGINS": "cov,xdist.plugin",
                },
                ".venv/bin/pytest -q",
            ),
            (
                {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-p xdist"},
                ".venv/bin/pytest -q",
            ),
            ({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""}, ".venv/bin/pytest -q"),
        ],
    )
    def test_xdist_named_without_autoload_gets_workers(
        self, tmp_path, monkeypatch, env, command
    ):
        from build_pipeline.gates import parallel_command

        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert (
            parallel_command(command, self._venv(tmp_path, xdist=True), "auto")
            == f"{command} -n auto"
        )

    @pytest.mark.parametrize("gate", [baseline_test_gate, integration_gate])
    def test_a_probe_that_times_out_runs_the_command_as_written(
        self, tmp_path, trust_repo, monkeypatch, gate
    ):
        """A probe that hangs (a first `uv run` sync) or cannot start leaves
        the command as written instead of raising out of the gate."""
        from build_pipeline import gates

        p = self._venv(tmp_path, xdist=True)
        python = p / ".venv" / "bin" / "python"
        python.write_text(
            '#!/bin/sh\n[ "$1" = "-m" ] && shift 2 && echo "ARGS: $*" && exit 0\nexec sleep 5\n'
        )
        monkeypatch.setattr(gates, "XDIST_PROBE_TIMEOUT", 0.2)
        assert (
            "ARGS: -q\n"
            in gate(".venv/bin/pytest -q", p, parallel="auto").metadata["output"]
        )

    def test_a_probe_that_cannot_start_runs_the_command_as_written(
        self, tmp_path, trust_repo
    ):
        p = self._venv(tmp_path, xdist=True)
        (p / ".venv" / "bin" / "python").chmod(0o644)  # PermissionError, an OSError
        assert (
            "ARGS: -q\n"
            in integration_gate(".venv/bin/pytest -q", p, parallel="auto").metadata[
                "output"
            ]
        )

    def test_autoload_off_runs_the_gate_as_written(
        self, tmp_path, trust_repo, monkeypatch
    ):
        monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        p = self._venv(tmp_path, xdist=True)
        assert (
            "ARGS: -q\n"
            in integration_gate(".venv/bin/pytest -q", p, parallel="auto").metadata[
                "output"
            ]
        )

    @pytest.mark.parametrize(
        "command",
        ["npm test", "make test", "tox -e py", "pytest -q && echo x", "cargo test"],
    )
    def test_other_runners_are_kept(self, tmp_path, command):
        from build_pipeline.gates import parallel_command

        assert (
            parallel_command(command, self._venv(tmp_path, xdist=True), "auto")
            == command
        )

    @pytest.mark.parametrize(
        "command, probe",
        [
            ("uv run pytest -q", ["uv", "run", "python", "-c", "import xdist"]),
            ("poetry run pytest", ["poetry", "run", "python", "-c", "import xdist"]),
            ("uv run python -m pytest", ["uv", "run", "python", "-c", "import xdist"]),
            ("python3 -m pytest", ["python3", "-c", "import xdist"]),
            ("env X=1 pytest", None),
        ],
    )
    def test_the_probe_runs_in_the_environment_pytest_runs_in(
        self, tmp_path, command, probe
    ):
        import shlex

        from build_pipeline.gates import _xdist_probe

        assert _xdist_probe(shlex.split(command), tmp_path) == probe

    def test_a_bare_pytest_probes_the_python_beside_it_on_path(
        self, tmp_path, monkeypatch
    ):
        import shlex

        from build_pipeline.gates import _xdist_probe

        p = self._venv(tmp_path, xdist=True)
        monkeypatch.setenv("PATH", f"{p / '.venv' / 'bin'}:/usr/bin:/bin")
        assert _xdist_probe(shlex.split("pytest -xvs"), p) == [
            str(p / ".venv" / "bin" / "python"),
            "-c",
            "import xdist",
        ]

    def test_an_untrusted_repo_is_not_probed(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        p = self._venv(tmp_path, xdist=True)
        with patch("build_pipeline.gates.parallel_command") as probe:
            assert not integration_gate(
                ".venv/bin/pytest -q", p, parallel="auto"
            ).passed
        probe.assert_not_called()

    def test_every_full_suite_gate_in_the_engine_passes_the_setting(self):
        """`tdd.parallel` reaches every integration and baseline gate call
        that runs the whole suite. A call given the block's own tests
        (`_block_test_command(...)`) or the change's (`_change_test_command(...)`)
        is not one: the setting is for the whole suite.

        The calls are found by AST, not by text, so a call the formatter has
        wrapped across lines is still seen.
        """
        import ast
        import inspect

        from build_pipeline import tdd_engine

        gates = ("integration_gate", "baseline_test_gate")
        calls = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(tdd_engine)))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) in gates
            and not (
                node.args
                and isinstance(node.args[0], ast.Call)
                and getattr(node.args[0].func, "id", None)
                in ("_block_test_command", "_change_test_command")
            )
        ]
        assert calls  # every call must pass the setting; adding a gate is allowed
        missing = [
            ast.unparse(c)
            for c in calls
            if not any(
                k.arg == "parallel" and ast.unparse(k.value) == "config.parallel_tests"
                for k in c.keywords
            )
        ]
        assert not missing, missing


class TestFinalVerification:
    def test_requires_a_test_command(self, tmp_path):
        from build_pipeline.gates import final_verification

        assert not final_verification("", tmp_path).passed

    def test_rejects_test_command_that_rewrites_tracked_input(
        self, tmp_path, monkeypatch
    ):
        import subprocess
        import sys

        from build_pipeline.gates import final_verification

        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "input.txt").write_text("before")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
        command = f'''{sys.executable} -c "from pathlib import Path; Path('input.txt').write_text('after')"'''
        result = final_verification(command, tmp_path)
        assert not result.passed
        assert "changed" in result.reason

    def test_pass_records_command_and_revision(self, tmp_path, monkeypatch):
        import subprocess
        import sys

        from build_pipeline.gates import final_verification, verification_inputs

        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "base"], cwd=tmp_path, check=True
        )
        command = f'{sys.executable} -c "print(123)"'
        result = final_verification(command, tmp_path, plan_revision="plan-one")
        assert result.passed
        assert result.metadata["inputs"] == verification_inputs(
            command, tmp_path, plan_revision="plan-one"
        )
        assert "123" in result.metadata["output"]


def _git(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()


def _python(code):
    import shlex
    import sys

    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


def _repo_with_one_file(repo, text):
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", text.strip())


@pytest.fixture
def with_submodule(tmp_path, trust_repo):
    """A project with the checked-out submodule `vendor`, one commit ahead of `older`."""
    source, project = tmp_path / "source", tmp_path / "project"
    _repo_with_one_file(source, "first\n")
    older = _git(source, "rev-parse", "HEAD")
    (source / "a.txt").write_text("second\n")
    _git(source, "commit", "-qam", "second")
    _repo_with_one_file(project, "project\n")
    _git(
        project,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(source),
        "vendor",
    )
    _git(project, "commit", "-qm", "add vendor")
    return project, older


class TestFinalVerificationSubmodules:
    """A submodule's directory is checked as a checkout of its own, never read as a file."""

    PASS = _python("pass")

    def test_a_clean_submodule_passes_and_its_inputs_are_stable(self, with_submodule):
        from build_pipeline.gates import final_verification, verification_inputs

        project, _ = with_submodule
        result = final_verification(self.PASS, project)
        assert result.passed, result.reason
        first = verification_inputs(self.PASS, project, include_files=True)
        assert verification_inputs(self.PASS, project, include_files=True) == first
        assert first["files"]["vendor"].startswith(
            "checkout:" + _git(project / "vendor", "rev-parse", "HEAD")
        )

    def test_reading_the_inputs_changes_nothing_in_the_submodule(self, with_submodule):
        from build_pipeline.gates import verification_inputs

        project, _ = with_submodule
        git_dir = Path(_git(project / "vendor", "rev-parse", "--absolute-git-dir"))
        (project / "vendor" / "a.txt").write_text("edited\n")
        before = {p: p.read_bytes() for p in git_dir.rglob("*") if p.is_file()}
        verification_inputs(self.PASS, project)
        assert {p: p.read_bytes() for p in git_dir.rglob("*") if p.is_file()} == before

    @pytest.mark.parametrize("mutation", ["head", "tracked", "index", "untracked"])
    def test_a_submodule_changed_by_the_command_fails_and_is_named(
        self, with_submodule, mutation
    ):
        from build_pipeline.gates import final_verification

        project, older = with_submodule
        # The parent's ignore setting hides the submodule from the parent's own status.
        _git(project, "config", "submodule.vendor.ignore", "all")
        blob = _git(project / "vendor", "rev-parse", f"{older}:a.txt")
        code = {
            "head": f"import subprocess; subprocess.run(['git', '-C', 'vendor', 'checkout', '-q', '--detach', {older!r}], check=True)",
            "tracked": "from pathlib import Path; Path('vendor/a.txt').write_text('changed')",
            "index": "import subprocess; subprocess.run(['git', '-C', 'vendor', 'update-index', "
            f"'--cacheinfo', '100644,{blob},a.txt'], check=True)",
            "untracked": "from pathlib import Path; Path('vendor/new.txt').write_text('new')",
        }[mutation]
        result = final_verification(_python(code), project)
        assert not result.passed
        assert result.reason.startswith(
            "Inputs changed during final verification: vendor."
        )
        assert (
            "vendor is a submodule or nested repository: run git -C vendor status"
            in result.reason
        )
        assert result.metadata["changed_files"] == ["vendor"]

    def test_a_changed_file_gets_no_submodule_advice(self, with_submodule):
        from build_pipeline.gates import final_verification

        project, _ = with_submodule
        result = final_verification(
            _python("from pathlib import Path; Path('a.txt').write_text('x')"), project
        )
        assert result.metadata["changed_files"] == ["a.txt"]
        assert "submodule" not in result.reason

    def test_a_submodule_edited_before_the_check_and_left_alone_passes(
        self, with_submodule
    ):
        from build_pipeline.gates import final_verification

        project, _ = with_submodule
        (project / "vendor" / "a.txt").write_text("edited\n")
        (project / "vendor" / "new.txt").write_text("new\n")
        result = final_verification(self.PASS, project)
        assert result.passed, result.reason

    def test_a_tracked_file_git_is_told_to_assume_unchanged_is_still_read(
        self, with_submodule
    ):
        from build_pipeline.gates import final_verification

        project, _ = with_submodule
        _git(project / "vendor", "update-index", "--assume-unchanged", "a.txt")
        result = final_verification(
            _python(
                "from pathlib import Path; Path('vendor/a.txt').write_text('changed')"
            ),
            project,
        )
        assert not result.passed
        assert result.metadata["changed_files"] == ["vendor"]

    @staticmethod
    def _nest(project, tmp_path):
        inner = tmp_path / "inner"
        _repo_with_one_file(inner, "inner\n")
        vendor = project / "vendor"
        _git(
            vendor,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner),
            "inner",
        )
        _git(vendor, "commit", "-qm", "add inner")
        _git(project, "add", "vendor")
        _git(project, "commit", "-qm", "move vendor")

    def test_a_changed_file_in_a_nested_submodule_is_named(
        self, with_submodule, tmp_path
    ):
        from build_pipeline.gates import final_verification

        project, _ = with_submodule
        self._nest(project, tmp_path)
        assert final_verification(self.PASS, project).passed
        code = (
            "from pathlib import Path; Path('vendor/inner/a.txt').write_text('changed')"
        )
        result = final_verification(_python(code), project)
        assert not result.passed
        assert result.metadata["changed_files"] == ["vendor"]

    @pytest.mark.parametrize(
        "command",
        [
            "deinit -f inner",
            "update --init inner",
        ],
    )
    def test_a_nested_submodule_removed_or_checked_out_by_the_command_is_named(
        self, with_submodule, tmp_path, command
    ):
        from build_pipeline.gates import final_verification, verification_inputs

        project, _ = with_submodule
        self._nest(project, tmp_path)
        if command.startswith("update"):
            _git(project / "vendor", "submodule", "deinit", "-q", "-f", "inner")
        assert verification_inputs(self.PASS, project) == verification_inputs(
            self.PASS, project
        )
        assert final_verification(self.PASS, project).passed
        argv = [
            "git",
            "-C",
            "vendor",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            *command.split(),
        ]
        result = final_verification(
            _python(f"import subprocess; subprocess.run({argv!r}, check=True)"), project
        )
        assert (project / "vendor/inner/a.txt").is_file() == command.startswith(
            "update"
        )
        assert not result.passed
        assert result.metadata["changed_files"] == ["vendor"]

    def test_an_uninitialized_submodule_passes_the_same_way_every_time(
        self, with_submodule
    ):
        from build_pipeline.gates import final_verification, verification_inputs

        project, _ = with_submodule
        _git(project, "submodule", "deinit", "-q", "-f", "vendor")
        first, second = (
            final_verification(self.PASS, project),
            final_verification(self.PASS, project),
        )
        assert first.passed and second.passed, first.reason
        assert (
            verification_inputs(self.PASS, project, include_files=True)["files"][
                "vendor"
            ]
            == "gitlink:uninitialized"
        )

    def test_a_gitlink_with_no_checkout_passes(self, tmp_path, trust_repo):
        from build_pipeline.gates import final_verification

        project = tmp_path / "project"
        _repo_with_one_file(project, "project\n")
        (project / "vendor").mkdir()
        commit = _git(project, "rev-parse", "HEAD")
        _git(project, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
        _git(project, "commit", "-qm", "gitlink")
        result = final_verification(self.PASS, project)
        assert result.passed, result.reason

    def test_a_gitlink_directory_with_files_but_no_checkout_is_unavailable(
        self, with_submodule
    ):
        from build_pipeline.gates import final_verification

        project, _ = with_submodule
        _git(project, "submodule", "deinit", "-q", "-f", "vendor")
        (project / "vendor" / "stray.txt").write_text("stray\n")
        result = final_verification(self.PASS, project)
        assert not result.passed
        assert result.reason.startswith("Final verification unavailable: vendor ")
        assert "Is a directory" not in result.reason

    def test_a_file_where_the_submodule_was_is_read_as_a_file(self, with_submodule):
        import shutil

        from build_pipeline.gates import verification_inputs

        project, _ = with_submodule
        shutil.rmtree(project / "vendor")
        (project / "vendor").write_text("now a file\n")
        files = verification_inputs(self.PASS, project, include_files=True)["files"]
        assert files["vendor"].startswith("0:")

    def test_an_untracked_nested_repository_is_checked_like_a_submodule(
        self, tmp_path, trust_repo
    ):
        from build_pipeline.gates import final_verification

        project = tmp_path / "project"
        _repo_with_one_file(project, "project\n")
        _repo_with_one_file(project / "nested", "nested\n")
        assert final_verification(self.PASS, project).passed
        code = "from pathlib import Path; Path('nested/a.txt').write_text('changed')"
        result = final_verification(_python(code), project)
        assert not result.passed
        assert result.metadata["changed_files"] == ["nested/"]


def test_red_rejects_missing_fixture_file_even_with_failed_summary():
    from build_pipeline.gates import red_gate

    gate = red_gate(
        "FileNotFoundError: missing-fixture.json\nFAILED tests/test_app.py::test_read\n1 failed",
        1,
    )
    assert not gate.passed


def test_red_rejects_setup_errors_even_with_assertion_failures():
    from build_pipeline.gates import red_gate

    assert not red_gate(
        "ERROR at setup of test_app\nAssertionError\n1 failed, 1 error", 1
    ).passed


@pytest.mark.parametrize(
    "body,expected",
    [
        ("def test_behavior():\n    assert 1 == 2\n", True),
        ("def test_behavior(missing_fixture):\n    assert 1 == 2\n", False),
        ("def test_behavior():\n    open('missing-input')\n", False),
        ("import missing_buildme_test_module\n", False),
    ],
)
def test_real_pytest_red_classifies_behavior_and_attributes_changed_test(
    tmp_path, body, expected
):
    import subprocess
    import sys

    (tmp_path / "test_behavior.py").write_text(body)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_behavior.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    gate = red_gate(
        result.stdout + result.stderr,
        result.returncode,
        expected_tests=["test_behavior.py"],
    )
    assert gate.passed is expected, gate.reason


@pytest.mark.parametrize("runner", [None, "jest", "pytest"])
def test_red_never_accepts_collection_errors_from_wrapped_runners(tmp_path, runner):
    import subprocess
    import sys

    (tmp_path / "test_missing.py").write_text("import nonexistent_buildme_dependency\n")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    gate = red_gate(run.stdout + run.stderr, run.returncode, runner=runner)
    assert not gate.passed


@pytest.mark.parametrize(
    "exception", ["FileNotFoundError", "ImportError", "PermissionError"]
)
def test_red_classifies_exception_diagnostics_not_displayed_source(tmp_path, exception):
    import subprocess
    import sys

    (tmp_path / "test_behavior.py").write_text(
        "from unittest.mock import patch\n\n"
        "def test_behavior():\n"
        f"    with patch('builtins.open', side_effect={exception}):\n"
        "        assert 1 == 2\n"
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    gate = red_gate(
        run.stdout + run.stderr, run.returncode, expected_tests=["test_behavior.py"]
    )
    assert gate.passed, gate.reason


def test_unknown_runner_nonzero_is_explicitly_unverified():
    for output, code in [
        ("command not found: node", 127),
        ("Error: Cannot find module 'jest'", 1),
    ]:
        result = red_gate(output, code, runner=None)
        assert result.passed and result.metadata["classification"] == "unverified"


def test_new_module_can_reach_red_without_collection_failure(tmp_path):
    import subprocess
    import sys

    (tmp_path / "test_new.py").write_text(
        "import importlib.util\n\ndef test_api():\n"
        "    assert importlib.util.find_spec('new_buildme_feature') is not None\n"
        "    from new_buildme_feature import answer\n    assert answer() == 42\n"
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert red_gate(
        run.stdout + run.stderr, run.returncode, expected_tests=["test_new.py"]
    ).passed


def test_jest_collection_failure_is_not_behavioral_red():
    output = "FAIL tests/a.test.js\n  ● Test suite failed to run\n\n    Cannot find module 'not-installed' from 'tests/a.test.js'\n\nTest Suites: 1 failed, 1 total\nTests: 0 total"
    assert not red_gate(output, 1, runner="jest").passed
