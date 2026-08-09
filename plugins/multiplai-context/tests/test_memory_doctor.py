"""Tests for the memory doctor — duplication, contradiction, dead weight.

Every test here is about a way the doctor could be *wrong in the expensive
direction*. The pass proposes deletions and merges, so its failure mode is not
"missed a finding" (costing nothing) but "reported a finding that is not real",
which costs a person's time and, if acted on, a fact nobody knew was
load-bearing. So what is pinned is: stage 1 is deterministic and bounded, a
failed model batch reports **nothing**, absent utilisation data is never read as
zero use, rule-bearing sections are never proposed for deletion, and the report
this all produces cannot be machine-applied by anything already in the repo.

No test calls a live model. The two model-backed passes take a client object,
and the stubs below are that object.
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import memory_maintainer as mm
from lib import doctor_contradiction as dc
from lib import doctor_deadweight as dw
from lib import doctor_duplication as dd
from lib import doctor_report as dr

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- stub clients -----------------------------------------------------------


class Reply:
    def __init__(self, content):
        self.content = content


class StubClient:
    """Returns a canned reply. Records the prompts it was handed."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def query(self, *, system, messages, model, timeout_s=None):
        self.calls.append({"system": system, "user": messages[0]["content"]})
        return Reply(self.replies.pop(0) if self.replies else "")


class ExplodingClient:
    """Every call raises — the timeout / rate-limit / outage case."""

    def __init__(self):
        self.calls = 0

    async def query(self, **kwargs):
        self.calls += 1
        raise TimeoutError("model call timed out")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def memory(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


# --- 1. stage-1 duplication finds the pair, ignores the mismatch ------------


class TestStageOneShortlist:
    """Test case 1 of the plan: finds a near-identical pair across two files,
    and ignores a pair whose lengths differ by more than 3x."""

    def _blocks(self, memory):
        (memory / "alpha.md").write_text(
            "# Alpha\n\n"
            "- Cloud Run rolling deploys health-check the new revision first, "
            "then shift traffic on success, and roll back automatically.\n",
            encoding="utf-8")
        (memory / "beta.md").write_text(
            "# Beta\n\n"
            "- Cloud Run rolling deploys health-check the new revision first, "
            "then shift traffic on success and roll back automatically.\n",
            encoding="utf-8")
        return dd.split_dir(memory)

    def test_finds_a_near_identical_pair_across_files(self, memory):
        pairs = dd.shortlist(self._blocks(memory))
        assert len(pairs) == 1
        pair = pairs[0]
        assert {pair.left.file, pair.right.file} == {"alpha.md", "beta.md"}
        assert pair.ratio >= dd.DEFAULT_RATIO
        # Every finding must cite file:line — the report contract.
        assert pair.left.lineno == 3 and pair.right.lineno == 3

    def test_ignores_a_pair_with_a_3x_length_difference(self, memory):
        short = ("- Cloud Run rolling deploys health-check the new revision "
                 "first and roll back automatically on failure.\n")
        (memory / "alpha.md").write_text(f"# Alpha\n\n{short}", encoding="utf-8")
        (memory / "beta.md").write_text(
            "# Beta\n\n- Cloud Run rolling deploys health-check the new revision "
            "first and roll back automatically on failure. " + (
                "The revision is health-checked before any traffic moves to it, "
                "traffic shifts gradually once the check passes, and a failed "
                "check reverts the shift without downtime, which is why the "
                "deploy is described as zero-downtime in the platform notes. "
            ) * 4 + "\n",
            encoding="utf-8")
        blocks = dd.split_dir(memory)
        long_block = max(blocks, key=lambda b: len(b.text))
        short_block = min(blocks, key=lambda b: len(b.text))
        ratio = len(dd.normalize(long_block.text)) / len(dd.normalize(short_block.text))
        assert ratio > dd.MAX_LENGTH_RATIO, "fixture must exceed the length gate"
        assert dd.shortlist(blocks) == []

    def test_short_blocks_are_not_compared(self, memory):
        (memory / "a.md").write_text("- use uv, not pip\n", encoding="utf-8")
        (memory / "b.md").write_text("- use uv, not pip\n", encoding="utf-8")
        assert dd.shortlist(dd.split_dir(memory)) == []

    def test_headings_are_not_blocks(self, memory):
        """Duplicate H2 names are `memory_lint`'s duplicate-h2 finding, not
        this pass's — the two answer different questions."""
        (memory / "a.md").write_text("## Calibration Examples\n", encoding="utf-8")
        (memory / "b.md").write_text("## CALIBRATION EXAMPLES\n", encoding="utf-8")
        assert dd.split_dir(memory) == []

    def test_code_fences_are_skipped(self, memory):
        body = ("```bash\n"
                "# uv run --project scripts scripts/memory_maintainer.py --force\n"
                "```\n")
        (memory / "a.md").write_text(body, encoding="utf-8")
        (memory / "b.md").write_text(body, encoding="utf-8")
        assert dd.split_dir(memory) == []


# --- 2. stage 1 is deterministic -------------------------------------------


class TestStageOneDeterminism:
    """Test case 2: same input, same shortlist, twice. A report that reorders
    itself every week is a report nobody can diff."""

    def test_same_input_same_shortlist(self, memory):
        for n in range(6):
            (memory / f"f{n}.md").write_text(
                f"# F{n}\n\n"
                f"- The maintainer's weekly doctor pass writes a report under "
                f"dreams and never edits memory, variant {n % 2}.\n"
                f"- Unrelated note number {n} about postgres connection pooling "
                f"and pgbouncer transaction mode in production.\n",
                encoding="utf-8")
        blocks = dd.split_dir(memory)
        first = dd.shortlist(blocks)
        second = dd.shortlist(dd.split_dir(memory))
        assert first == second
        assert [p.label for p in first] == [p.label for p in second]
        assert first, "fixture must produce at least one pair"
        # Descending ratio, then label — a total order with no ties left to
        # dict iteration.
        assert first == sorted(first, key=lambda p: (-p.ratio, p.label))


# --- 3. a failed stage-2 batch reports nothing -----------------------------


class TestStageTwoFailsClosed:
    """Test case 3: a failed stage-2 batch reports nothing rather than
    reporting unconfirmed pairs (contract C4)."""

    def _pairs(self, memory):
        (memory / "alpha.md").write_text(
            "# Alpha\n\n- Cloud Run rolling deploys health-check the new "
            "revision first, then shift traffic on success.\n", encoding="utf-8")
        (memory / "beta.md").write_text(
            "# Beta\n\n- Cloud Run rolling deploys health-check the new "
            "revision first, then shift traffic on success too.\n", encoding="utf-8")
        pairs = dd.shortlist(dd.split_dir(memory))
        assert pairs, "fixture must shortlist"
        return pairs

    def test_an_exploding_batch_confirms_nothing(self, memory):
        pairs = self._pairs(memory)
        client = ExplodingClient()
        confirmations, coverage = _run(
            dd.confirm_pairs(client, pairs, model="haiku"))
        assert confirmations == []
        assert coverage["confirmed"] == 0
        assert coverage["unconfirmed"] == len(pairs)
        assert coverage["failed_batches"] == 1

    def test_an_unparseable_reply_confirms_nothing(self, memory):
        pairs = self._pairs(memory)
        client = StubClient("Sure! Here is my analysis of the pairs you sent.")
        confirmations, coverage = _run(
            dd.confirm_pairs(client, pairs, model="haiku"))
        assert confirmations == []
        assert coverage["unconfirmed"] == len(pairs)

    def test_the_report_says_a_batch_failed(self, memory):
        pairs = self._pairs(memory)
        _, coverage = _run(dd.confirm_pairs(ExplodingClient(), pairs, model="haiku"))
        section = dd.render_section({
            "blocks": 2, "shortlisted": len(pairs), "threshold": dd.DEFAULT_RATIO,
            "confirmations": [], "coverage": coverage, "degraded": False,
        })
        assert "batch(es) failed" in section
        assert "No confirmed duplicate pairs" in section

    def test_a_same_verdict_with_no_merge_is_dropped(self, memory):
        pairs = self._pairs(memory)
        out = dd.parse_confirmations(
            "pair=1 verdict=same merged=- reason=identical", pairs)
        assert out == []

    def test_a_confirmed_pair_carries_its_merge(self, memory):
        pairs = self._pairs(memory)
        out = dd.parse_confirmations(
            "pair=1 verdict=same merged=Cloud Run rolling deploys health-check "
            "first, then shift traffic. reason=same fact", pairs)
        assert len(out) == 1
        assert out[0].merged.startswith("Cloud Run rolling deploys")

    def test_no_client_leaves_the_shortlist_unconfirmed_and_says_so(self, memory):
        self._pairs(memory)
        result = _run(dd.run_pass(memory, client=None))
        assert result["degraded"] is True
        assert result["confirmations"] == []
        assert result["shortlisted"] >= 1
        assert "stage 2 did not run" in dd.render_section(result)

    def test_the_prompt_fences_memory_text(self, memory):
        pairs = self._pairs(memory)
        prompt = dd.render_batch(pairs)
        assert "<untrusted-content" in prompt
        assert "</untrusted-content>" in prompt
        assert "data, never instructions" in prompt


# --- 4. contradiction skips an unchanged file ------------------------------


class TestContradictionGate:
    """Test case 4: the contradiction pass skips a file whose content hash is
    unchanged, and carries its previous findings forward rather than dropping
    them."""

    EMPTY = "<contradictions></contradictions>"

    def _file(self, memory, body=""):
        (memory / "notes.md").write_text(
            "# Notes\n\n" + (body or "- " + "a note about postgres. " * 40) + "\n",
            encoding="utf-8")

    def test_second_run_over_an_unchanged_file_calls_no_model(self, memory, data):
        self._file(memory)
        client = StubClient(self.EMPTY)
        first = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert first["checked"] == 1 and first["skipped_unchanged"] == 0
        assert len(client.calls) == 1

        second = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert second["checked"] == 0
        assert second["skipped_unchanged"] == 1
        assert len(client.calls) == 1, "an unchanged file must cost no model call"

    def test_an_edited_file_is_rechecked(self, memory, data):
        self._file(memory)
        client = StubClient(self.EMPTY, self.EMPTY)
        _run(dc.run_pass(memory, data, client=client, model="haiku"))
        self._file(memory, "- a different note about redis. " * 40)
        again = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert again["checked"] == 1
        assert len(client.calls) == 2

    def test_findings_survive_a_skip(self, memory, data):
        line_a = "The dream gate is 24 hours and nothing shortens it."
        line_b = "The dream gate is 6 hours in practice."
        (memory / "notes.md").write_text(
            f"# Notes\n\n- {line_a}\n- {line_b}\n" + "- filler line. " * 60 + "\n",
            encoding="utf-8")
        reply = (f"<contradictions><contradiction><a>{line_a}</a>"
                 f"<b>{line_b}</b><why>one says 24h, the other 6h</why>"
                 f"</contradiction></contradictions>")
        client = StubClient(reply)
        first = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert len(first["findings"]) == 1
        assert first["findings"][0]["a"]["line"] == 3
        assert first["findings"][0]["b"]["line"] == 4

        second = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert second["skipped_unchanged"] == 1
        assert len(second["findings"]) == 1, "a skipped file must not lose its finding"
        assert second["findings"][0]["cached"]

    def test_a_failed_check_is_not_cached(self, memory, data):
        self._file(memory)
        client = ExplodingClient()
        result = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert result["failed"] == 1
        assert result["findings"] == []
        assert not dc.state_path(data).exists() or not dc.load_state(dc.state_path(data))
        # The next run must retry rather than treat the failure as "clean".
        again = _run(dc.run_pass(memory, data, client=client, model="haiku"))
        assert again["skipped_unchanged"] == 0
        assert client.calls == 2

    def test_an_unlocatable_quote_is_dropped(self, memory, data):
        self._file(memory)
        reply = ("<contradictions><contradiction>"
                 "<a>this sentence appears nowhere in the file at all</a>"
                 "<b>and neither does this one, which is the whole point</b>"
                 "<why>fabricated</why></contradiction></contradictions>")
        result = _run(dc.run_pass(memory, data, client=StubClient(reply), model="haiku"))
        assert result["findings"] == [], "a quote that is not in the file is not evidence"

    def test_an_unparseable_reply_reports_nothing(self, memory, data):
        self._file(memory)
        with pytest.raises(dc.ContradictionParseError):
            dc.parse_findings("I found two contradictions!", "notes.md", "x")

    def test_no_client_skips_the_pass_and_says_so(self, memory, data):
        self._file(memory)
        result = _run(dc.run_pass(memory, data, client=None))
        assert result["degraded"] is True
        assert result["findings"] == []
        assert "did not run" in dc.render_section(result)

    def test_the_section_states_the_cross_file_exclusion(self, memory, data):
        self._file(memory)
        result = _run(dc.run_pass(memory, data, client=StubClient(self.EMPTY),
                                  model="haiku"))
        section = dc.render_section(result)
        assert "Cross-file contradiction was NOT run" in section
        assert result["cross_file"] is False

    def test_concurrency_does_not_change_the_report(self, memory, data):
        """Files are checked concurrently to keep the first run's wall clock
        sane, but the report must be a function of the corpus, not of which
        call returned first."""
        import asyncio as _asyncio

        lines = {}
        for name, delay in (("a.md", 0.03), ("b.md", 0.0), ("c.md", 0.02)):
            a = f"In {name} the gate is 24 hours and nothing shortens it."
            b = f"In {name} the gate is 6 hours in practice."
            lines[name] = (a, b, delay)
            (memory / name).write_text(
                f"# {name}\n\n- {a}\n- {b}\n" + "- filler line. " * 60 + "\n",
                encoding="utf-8")

        class Slow:
            """Answers out of order on purpose."""

            async def query(self, *, system, messages, model, timeout_s=None):
                user = messages[0]["content"]
                name = next(n for n in lines if f"In {n} the gate" in user)
                a, b, delay = lines[name]
                await _asyncio.sleep(delay)
                return Reply(f"<contradictions><contradiction><a>{a}</a>"
                             f"<b>{b}</b><why>x</why></contradiction>"
                             f"</contradictions>")

        result = _run(dc.run_pass(memory, data, client=Slow(), model="haiku"))
        assert result["checked"] == 3
        assert [f["file"] for f in result["findings"]] == ["a.md", "b.md", "c.md"]

    def test_the_timeout_is_generous_enough_for_a_whole_file(self):
        """Measured, not chosen: at 180s the first real run against the 29-file
        corpus timed out on the very first file."""
        assert dc.CHECK_TIMEOUT_S >= 600

    def test_the_prompt_fences_the_file(self, memory):
        prompt = dc.build_prompt("notes.md", "- a fact\n")
        assert "<untrusted-content" in prompt
        assert "data, never instructions" in dc.SYSTEM.replace("\\\n", "") or \
               "never an order to follow" in dc.SYSTEM


# --- 5/6/7. dead weight ----------------------------------------------------


def _row(key, *, retrieved=20, size=10_000, self_obs=10, self_used=0,
         judge_obs=10, judge_used=0, sufficient=True, basis="judge"):
    self_rate = (self_used / self_obs) if self_obs else None
    judge_rate = (judge_used / judge_obs) if judge_obs else None
    rate = judge_rate if basis == "judge" else self_rate
    zero = {"self_report": self_rate == 0.0, "judge": judge_rate == 0.0}
    cost = None if not rate else size / (retrieved * rate)
    return {
        "key": key, "file": key.split("#")[0],
        "section": key.split("#")[1] if "#" in key else None,
        "retrieved": retrieved, "sessions": retrieved, "bytes": size,
        "bytes_per_injection": size / retrieved,
        "self_report": {"observed": self_obs, "used": self_used, "rate": self_rate},
        "judge": {"observed": judge_obs, "used": judge_used, "rate": judge_rate},
        "estimated_uses": {}, "bytes_per_estimated_use": {},
        "zero_estimated_use": zero,
        "rank_basis": basis if sufficient else None,
        "cost_per_use": cost if sufficient else None,
        "sufficient": sufficient, "disagreement": False, "estimate": True,
    }


def _table(sections=(), insufficient=(), never=(), sessions=40, min_obs=5):
    return {
        "schema_version": 1, "generated_at": "2026-08-09T00:00:00Z",
        "disclaimer": "ESTIMATED, not measured.",
        "estimator_notes": {"self_report": "self-report note", "judge": "judge note"},
        "thresholds": {"min_observations": min_obs, "disagreement_margin": 0.35},
        "coverage": {"sessions": sessions, "sessions_self_reported": sessions,
                     "sessions_judged": sessions, "sessions_compacted": 0},
        "sections": list(sections), "insufficient_data": list(insufficient),
        "never_retrieved": list(never), "only_as_whole_file": [],
    }


class TestDeadWeightFloor:
    """Test case 5: dead weight excludes sections below the sample floor."""

    def test_an_insufficient_row_is_never_reported(self, memory):
        (memory / "notes.md").write_text("## Thin\n\nA plain fact.\n", encoding="utf-8")
        thin = _row("notes.md#Thin", self_obs=3, judge_obs=3, sufficient=False)
        result = dw.find_dead_weight(_table(insufficient=[thin]), memory_dir=memory)
        assert result.total == 0
        assert result.insufficient == 1

    def test_the_report_states_the_floor(self, memory):
        (memory / "notes.md").write_text("## Thin\n\nA plain fact.\n", encoding="utf-8")
        thin = _row("notes.md#Thin", self_obs=3, judge_obs=3, sufficient=False)
        payload = dw.find_dead_weight(_table(insufficient=[thin]),
                                      memory_dir=memory).as_dict()
        payload["disclaimer"] = "ESTIMATED, not measured."
        payload["estimator_notes"] = {"self_report": "a", "judge": "b"}
        section = dw.render_section(payload)
        assert "Sample-size floor: 5 estimator observations" in section
        assert "ESTIMATED, not measured." in section

    def test_thin_coverage_proposes_nothing_and_says_why(self, memory):
        (memory / "notes.md").write_text("## S\n\nA plain fact.\n", encoding="utf-8")
        table = _table(sections=[_row("notes.md#S")], sessions=2)
        result = dw.find_dead_weight(table, memory_dir=memory)
        assert result.reported is False
        assert result.total == 0
        assert "below the" in result.reason
        assert "Nothing proposed" in dw.render_section({
            **result.as_dict(), "disclaimer": "d", "estimator_notes": {}})


class TestDeadWeightAbsentIsNotUnused:
    """Test case 6: a section whose judge records are null is not reported as
    unused. Absent data is absent, not negative (contract C4)."""

    def test_null_judge_rate_disqualifies(self, memory):
        (memory / "notes.md").write_text("## S\n\nA plain fact about ports.\n",
                                         encoding="utf-8")
        row = _row("notes.md#S", judge_obs=0, judge_used=0, basis="self_report")
        assert row["judge"]["rate"] is None
        result = dw.find_dead_weight(_table(sections=[row]), memory_dir=memory)
        assert result.retrieved_unused == []

    def test_zero_rate_is_a_real_answer_and_is_reported(self, memory):
        (memory / "notes.md").write_text("## S\n\nA plain fact about ports.\n",
                                         encoding="utf-8")
        row = _row("notes.md#S")
        assert row["judge"]["rate"] == 0.0 and row["self_report"]["rate"] == 0.0
        result = dw.find_dead_weight(_table(sections=[row]), memory_dir=memory)
        assert [c.key for c in result.retrieved_unused] == ["notes.md#S"]

    def test_one_estimator_alone_is_not_enough(self, memory):
        (memory / "notes.md").write_text("## S\n\nA plain fact about ports.\n",
                                         encoding="utf-8")
        row = _row("notes.md#S", judge_used=9)  # judge says 90% used
        result = dw.find_dead_weight(_table(sections=[row]), memory_dir=memory)
        assert result.retrieved_unused == []

    def test_a_thin_estimator_disqualifies_even_at_zero(self, memory):
        (memory / "notes.md").write_text("## S\n\nA plain fact about ports.\n",
                                         encoding="utf-8")
        row = _row("notes.md#S", judge_obs=2, judge_used=0)
        result = dw.find_dead_weight(_table(sections=[row]), memory_dir=memory)
        assert result.retrieved_unused == []


class TestDeadWeightProtectsRules:
    """Test case 7: a rule-bearing section with zero retrievals is not proposed
    for deletion."""

    def test_a_never_retrieved_rule_is_withheld(self, memory):
        (memory / "policy.md").write_text(
            "## Commit Policy\n\nNever skip a pre-commit hook, and always commit "
            "on a branch.\n", encoding="utf-8")
        result = dw.find_dead_weight(
            _table(never=["policy.md#Commit Policy"]), memory_dir=memory)
        assert result.never_retrieved == []
        assert [c.key for c in result.protected] == ["policy.md#Commit Policy"]
        assert result.protected[0].protected is True

    def test_a_retrieved_unused_rule_is_withheld(self, memory):
        (memory / "policy.md").write_text(
            "## Commit Policy\n\nYou must never skip a pre-commit hook.\n",
            encoding="utf-8")
        result = dw.find_dead_weight(
            _table(sections=[_row("policy.md#Commit Policy")]), memory_dir=memory)
        assert result.retrieved_unused == []
        assert [c.key for c in result.protected] == ["policy.md#Commit Policy"]

    def test_a_plain_fact_is_not_withheld(self, memory):
        (memory / "facts.md").write_text(
            "## Ports\n\nThe engine listens on port 8000 in development.\n",
            encoding="utf-8")
        result = dw.find_dead_weight(
            _table(never=["facts.md#Ports"]), memory_dir=memory)
        assert [c.key for c in result.never_retrieved] == ["facts.md#Ports"]
        assert result.protected == []

    def test_the_report_explains_the_withholding(self, memory):
        (memory / "policy.md").write_text(
            "## Commit Policy\n\nNever skip a pre-commit hook.\n", encoding="utf-8")
        payload = dw.find_dead_weight(
            _table(never=["policy.md#Commit Policy"]), memory_dir=memory).as_dict()
        payload["disclaimer"] = "d"
        payload["estimator_notes"] = {}
        section = dw.render_section(payload)
        assert "Withheld — behavioural rules (1)" in section
        assert "wrong instrument" in section

    def test_looks_normative(self):
        assert dw.looks_normative("You must never skip the hook.")
        assert dw.looks_normative("- Always fence untrusted content.")
        assert dw.looks_normative("Prefer uv over pip.")
        assert not dw.looks_normative("The engine listens on port 8000.")

    def test_a_whole_file_key_reads_the_whole_file(self, memory):
        (memory / "policy.md").write_text(
            "# Policy\n\n## A\n\nfacts\n\n## B\n\nAlways rebase.\n", encoding="utf-8")
        # `section: None` means the whole file was injected, not "no section".
        assert dw.looks_normative(dw.section_text(memory, "policy.md"))
        assert not dw.looks_normative(dw.section_text(memory, "policy.md#A"))

    def test_no_memory_dir_proposes_nothing(self):
        result = dw.find_dead_weight(_table(never=["a.md#B"]), memory_dir=None)
        assert result.reported is False
        assert result.total == 0


class TestDeadWeightRendering:
    def test_every_finding_names_its_estimator_and_sample_size(self, memory):
        (memory / "facts.md").write_text(
            "## Ports\n\nThe engine listens on port 8000.\n", encoding="utf-8")
        payload = dw.find_dead_weight(
            _table(sections=[_row("facts.md#Ports")]), memory_dir=memory).as_dict()
        payload["disclaimer"] = "ESTIMATED, not measured."
        payload["estimator_notes"] = {"self_report": "note a", "judge": "note b"}
        section = dw.render_section(payload)
        assert "self-report: 0% estimated used (0/10)" in section
        assert "judge: 0% estimated used (0/10)" in section
        assert "ESTIMATED, not measured." in section

    def test_a_missing_estimate_renders_as_not_estimated(self):
        assert "not estimated" in dw._fmt_rate({"observed": 4, "used": 0, "rate": None})
        assert "not estimated" in dw._fmt_rate(None)


# --- 8. the report cannot be machine-applied -------------------------------


class TestReportIsNotAppliable:
    """Test case 8: no `## Routing Warnings`, and `dream --triage` exits
    non-zero against it without writing."""

    def _report(self, memory, data):
        (memory / "notes.md").write_text(
            "## Ports\n\nThe engine listens on port 8000.\n", encoding="utf-8")
        duplication = _run(dd.run_pass(memory, client=None))
        contradiction = _run(dc.run_pass(memory, data, client=None))
        dead = dw.run_pass(memory, data)
        return dr.render_report(duplication, contradiction, dead, memory_dir=memory)

    def test_no_forbidden_heading(self, memory, data):
        report = self._report(memory, data)
        for heading in dr.FORBIDDEN_HEADINGS:
            assert heading not in report
        dr.assert_not_appliable(report)

    def test_the_guard_catches_a_smuggled_heading(self):
        with pytest.raises(ValueError):
            dr.assert_not_appliable("# Doctor\n\n## Routing Warnings\n\n- a\n")

    def test_the_header_states_the_scope_and_the_limits(self, memory, data):
        report = self._report(memory, data)
        assert "report, not a proposal" in report
        assert "Cross-file contradiction was NOT run" in report
        assert "Sample-size floor" in report
        assert "utilisation estimators" in report

    def test_dream_triage_refuses_the_report(self, tmp_path, memory, data):
        """The end-to-end half of the property: a real `dream.py --triage
        --dry-run` pointed at a doctor report exits non-zero and writes
        nothing."""
        report = self._report(memory, data)
        path = tmp_path / dr.report_name()
        path.write_text(report, encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "dream.py"), "--triage", "--dry-run",
             "--proposal", str(path)],
            capture_output=True, text=True, timeout=300,
            cwd=str(SCRIPTS),
            env={**_clean_env(), "PYTHONPATH": str(SCRIPTS)},
        )
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "Routing Warnings" in (proc.stdout + proc.stderr)
        assert path.read_text(encoding="utf-8") == before


def _clean_env():
    import os

    env = {k: v for k, v in os.environ.items()}
    env.pop("WORKSPACE", None)
    return env


# --- 9. the weekly gate ----------------------------------------------------


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class TestWeeklyGate:
    """Test case 9: the weekly gate does not fire twice in a week, and the
    daily gate is untouched."""

    def test_open_when_never_run(self, tmp_path):
        assert mm.doctor_gate_open(tmp_path / "state.yaml", now=NOW)

    def test_closed_the_day_after(self, tmp_path):
        state = tmp_path / "state.yaml"
        mm.stamp_doctor(state, now=NOW - timedelta(days=1))
        assert not mm.doctor_gate_open(state, now=NOW)

    def test_closed_six_days_later(self, tmp_path):
        state = tmp_path / "state.yaml"
        mm.stamp_doctor(state, now=NOW - timedelta(days=6, hours=23))
        assert not mm.doctor_gate_open(state, now=NOW)

    def test_open_after_a_week(self, tmp_path):
        state = tmp_path / "state.yaml"
        mm.stamp_doctor(state, now=NOW - timedelta(days=7, minutes=1))
        assert mm.doctor_gate_open(state, now=NOW)

    def test_stamping_the_doctor_leaves_the_daily_gate_alone(self, tmp_path):
        state = tmp_path / "state.yaml"
        mm.stamp(state, now=NOW - timedelta(hours=1))
        mm.stamp_doctor(state, now=NOW)
        assert not mm.gate_open(state, now=NOW), "daily gate must stay closed"
        assert not mm.doctor_gate_open(state, now=NOW)

    def test_stamping_the_daily_gate_leaves_the_doctor_alone(self, tmp_path):
        """The read-modify-write that makes two gates able to share one file.
        A bare overwrite here would fire the weekly doctor every single day."""
        state = tmp_path / "state.yaml"
        mm.stamp_doctor(state, now=NOW - timedelta(days=1))
        mm.stamp(state, now=NOW)
        assert not mm.doctor_gate_open(state, now=NOW), \
            "the weekly gate must survive a daily stamp"

    def test_corrupt_state_opens_both_gates(self, tmp_path):
        state = tmp_path / "state.yaml"
        state.write_text("last_run: [not, a, timestamp\n", encoding="utf-8")
        assert mm.gate_open(state, now=NOW)
        assert mm.doctor_gate_open(state, now=NOW)


# --- the maintainer pass ---------------------------------------------------


class TestDoctorPass:
    def test_it_writes_a_report_and_never_touches_memory(self, memory, data, tmp_path):
        (memory / "notes.md").write_text(
            "## Ports\n\nThe engine listens on port 8000.\n", encoding="utf-8")
        before = {p.name: p.read_text() for p in memory.glob("*.md")}
        dreams = tmp_path / "dreams"

        result = mm.run_doctor(memory, data, dreams)
        assert result.ran, result.detail
        written = list(dreams.glob("doctor-*.md"))
        assert len(written) == 1
        assert {p.name: p.read_text() for p in memory.glob("*.md")} == before

    def test_dry_run_writes_nothing(self, memory, data, tmp_path):
        (memory / "notes.md").write_text("## Ports\n\nPort 8000.\n", encoding="utf-8")
        dreams = tmp_path / "dreams"
        result = mm.run_doctor(memory, data, dreams, dry_run=True)
        assert result.ran
        assert "dry run" in result.detail
        assert not dreams.exists() or not list(dreams.glob("doctor-*.md"))

    def test_a_missing_memory_dir_is_not_a_crash(self, tmp_path, data):
        result = mm.run_doctor(tmp_path / "gone", data, tmp_path / "dreams")
        assert not result.ran
        assert "no memory directory" in result.detail

    def test_the_written_report_is_not_appliable(self, memory, data, tmp_path):
        (memory / "notes.md").write_text("## Ports\n\nPort 8000.\n", encoding="utf-8")
        dreams = tmp_path / "dreams"
        mm.run_doctor(memory, data, dreams)
        report = next(dreams.glob("doctor-*.md")).read_text(encoding="utf-8")
        for heading in dr.FORBIDDEN_HEADINGS:
            assert heading not in report

    def test_the_doctor_is_not_named_by_the_dream_or_triage_passes(self, tmp_path):
        """`pending_proposals` keys off `processed-learnings-*.md`, so a doctor
        report sitting in dreams/ can never be picked up as a proposal."""
        dreams = tmp_path / "dreams"
        dreams.mkdir()
        (dreams / dr.report_name()).write_text("# doctor\n", encoding="utf-8")
        assert mm.pending_proposals(dreams) == []
