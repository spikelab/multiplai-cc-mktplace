from __future__ import annotations

import json

from conftest import CLAIM_HIGH, CLAIM_MEDIUM, CLAIM_REFUTED, CLAIM_REJECTED, EXPECTED_HIGH
from review_pipeline import post as post_mod
from review_pipeline.__main__ import main
from review_pipeline.export import to_findings_file, write_findings_file
from review_pipeline.models import Merged
from review_pipeline.render import render_review, render_summary, write_review, write_rollups
from review_pipeline.state import save_state


def test_expected_behaviour_is_shown_and_no_fix_is(canned_state):
    text = render_review(canned_state)
    assert f"**Expected behaviour:** {EXPECTED_HIGH}" in text
    assert text.count("**Expected behaviour:**") == 2  # the confirmed and the unverifiable finding
    assert "**Fix:**" not in text and "Premises:" not in text


def test_merged_findings_are_listed_in_the_appendix_and_finders_on_the_survivor(canned_state):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"finders": ["diff-bugs", "callers"]})
    copy = high.with_location(claim="Rate plans match a literal keyword", finder="callers")
    canned_state.merged.append(Merged(finding=copy, into=high.id,
                                      reason=f"the same defect as {high.id} (rateplan_service.py:1): same literal"))
    text = render_review(canned_state)
    assert "**Reported by:** diff-bugs, callers" in text
    appendix = text.split("## Appendix — not listed above", 1)[1]
    assert "**merged** (HIGH) `rateplan_service.py:1-1` — Rate plans match a literal keyword" in appendix
    assert f"Merged into `{high.id}`: the same defect as {high.id}" in appendix
    assert "1 merged into another finding as duplicates." in render_summary(canned_state)


def test_header_and_sections(canned_state):
    text = render_review(canned_state)
    t = canned_state.target
    assert text.startswith(f"# Review — {t.label}")
    assert "- **Tickets:** DB-2038" in text
    assert f"- **Base commit:** [{t.base_sha[:10]}](https://github.com/example/booking-engine/commit/{t.base_sha})" in text
    assert "- **Files changed:** 3" in text
    assert f"### HIGH — rateplan_service.py:1 — {CLAIM_HIGH}" in text
    assert "unverifiable (lowered from MEDIUM)" in text


def test_citations_are_full_sha_github_links(canned_state):
    head = canned_state.target.head_sha
    text = render_review(canned_state)
    assert f"(https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L1-L1)" in text
    assert f"(https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L6-L6)" in text


def test_citations_without_github_remote_are_path_lines(canned_state):
    canned_state.target.remote_url = "https://gitlab.com/example/booking-engine.git"
    text = render_review(canned_state)
    assert "`rateplan_service.py:1`" in text and "github.com" not in text


def test_header_counts_only_listed_findings(canned_state):
    text = render_review(canned_state)
    assert ("- **Findings:** Code 1 HIGH, 0 MEDIUM, 1 LOW; Tests none; Docs none "
            "(what was left out is only in the appendix)") in text
    body = text.split("## Appendix", 1)[0]
    assert CLAIM_REFUTED not in body and CLAIM_REJECTED not in body


def test_appendix_lists_rejected_and_refuted_with_reasons(canned_state):
    appendix = render_review(canned_state).split("## Appendix — not listed above", 1)[1]
    assert CLAIM_REFUTED in appendix and "line 5 sets the id" in appendix
    assert CLAIM_REJECTED in appendix and "quote not at cited lines" in appendix
    assert CLAIM_HIGH not in appendix


def test_summary_is_short_and_says_how_the_review_went(canned_state):
    text = render_summary(canned_state)
    lines = text.splitlines()
    assert len(lines) <= 25
    assert "Cost $1.25 over 9 agent calls, 0 tokens, 0s wall time." in text
    assert "Findings: Code 1 HIGH, 0 MEDIUM, 1 LOW; Tests none; Docs none." in text
    (high_line,) = [l for l in lines if l.startswith("- HIGH `rateplan_service.py")]
    assert high_line.endswith("(confirmed)")
    assert lines.index("## Code") < lines.index(high_line)
    assert "- 1 LOW in the full review." in text
    # Refuted and gate-rejected findings are neither listed nor counted here.
    assert "refuted" not in text.lower() and "rejected" not in text.lower() and "Dropped" not in text
    assert CLAIM_REFUTED.split(". ")[0][:40] not in text and CLAIM_REJECTED[:40] not in text
    assert CLAIM_MEDIUM not in text  # lowered to LOW: counted, not listed
    assert lines[-1] == f"Full review, with every reason and what was left out: `review-{canned_state.target.slug}.md`"


def test_summary_truncates_long_claims_and_lists_agent_failures(canned_state):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"claim": "x" * 500})
    canned_state.errors = ["verify: timeout on finding abc"]
    text = render_summary(canned_state)
    assert "x" * 119 + "…" in text and "x" * 121 not in text
    assert "Agent failures: verify: timeout on finding abc" in text


def test_write_review_also_writes_the_summary(canned_state, tmp_path):
    write_review(canned_state, tmp_path)
    assert (tmp_path / f"summary-{canned_state.target.slug}.md").read_text() == render_summary(canned_state)


def test_deployed_line(canned_state):
    canned_state.target.deployed_in = "staging"
    assert "- **Deployed in staging:** yes" in render_review(canned_state, deployed="yes")


def test_rollups_come_from_findings_json(canned_state, tmp_path):
    out = tmp_path / "out"
    first = out / canned_state.target.slug
    first.mkdir(parents=True)
    write_findings_file(canned_state, first)
    second_state = canned_state.model_copy(deep=True)
    second_state.target.slug, second_state.target.label = "other--x", "other x"
    (out / "other--x").mkdir()
    write_findings_file(second_state, out / "other--x")
    (first / "review-decoy.md").write_text("### HIGH — decoy.py:1 — scraped from markdown\n")

    written = write_rollups(out, [first / "findings.json", out / "other--x" / "findings.json"])
    assert [p.name for p in written] == ["HIGH-only.md", "MEDIUM-only.md", "LOW-only.md"]
    high = (out / "HIGH-only.md").read_text()
    assert high.startswith("# HIGH findings — 2 across 2 of 2 targets")
    assert high.index(canned_state.target.label) < high.index("other x")
    assert "decoy" not in high
    low = (out / "LOW-only.md").read_text()
    assert CLAIM_MEDIUM in low  # lowered to LOW, still shown
    assert CLAIM_REFUTED not in low  # refuted never reaches a rollup
    assert CLAIM_REJECTED not in (out / "MEDIUM-only.md").read_text()


# --- post ------------------------------------------------------------------------


def test_post_with_decisions_selects_exactly_the_accepted(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    high = state.findings[0]
    medium = state.findings[1]
    decisions = target_dir / "viewer" / "decisions.json"
    decisions.parent.mkdir()
    decisions.write_text(json.dumps({
        high.id: {"decision": "accept", "note": "", "ts": "2026-09-25T10:00:00Z"},
        medium.id: {"decision": "reject", "note": "", "ts": "2026-09-25T10:01:00Z"},
    }))
    assert main(["post", str(target_dir), "--decisions", str(decisions)]) == 0
    (argv, payload), = fake_gh.posts()
    assert argv[1:5] == ["api", "-X", "POST", "repos/{owner}/{repo}/pulls/812/reviews"]
    text = fake_gh.posted_text()
    assert len(payload["comments"]) == 1 and "\n1. " not in payload["body"]
    assert CLAIM_HIGH in text and CLAIM_MEDIUM not in text
    assert f"Expected behaviour: {EXPECTED_HIGH}" in text and "Suggested fix" not in text
    assert "posted 1 accepted findings to PR #812" in capsys.readouterr().out


def test_post_without_decisions_takes_high_and_medium(pr_review, fake_gh):
    state, target_dir = pr_review
    state.findings[1].severity = "MEDIUM"
    save_state(state, target_dir)
    write_findings_file(state, target_dir)
    assert main(["post", str(target_dir)]) == 0
    text = fake_gh.posted_text()
    assert CLAIM_HIGH in text and CLAIM_MEDIUM in text and CLAIM_REFUTED not in text


def test_post_missing_decisions_file_exits_2(pr_review, fake_gh, capsys):
    _, target_dir = pr_review
    missing = target_dir / "viewer" / "decisions.json"
    assert main(["post", str(target_dir), "--decisions", str(missing)]) == 2
    assert str(missing) in capsys.readouterr().err
    assert fake_gh == []


def test_post_refuses_a_non_pr_target(canned_state, tmp_path, fake_gh, capsys):
    target_dir = tmp_path / canned_state.target.slug
    target_dir.mkdir(exist_ok=True)
    save_state(canned_state, target_dir)
    write_findings_file(canned_state, target_dir)
    assert main(["post", str(target_dir)]) == 2
    assert "needs a PR target" in capsys.readouterr().err
    assert fake_gh == []


def test_web_citation_renders_as_a_link_not_a_code_line():
    from review_pipeline import render
    fd = {"id": "x", "severity": "LOW", "status": "confirmed", "file": "a.py", "line_start": 1, "line_end": 1,
          "claim": "c", "failure_scenario": "f", "verdict_reason": "", "expected_behaviour": "",
          "citations": [{"path": "a.py", "line_start": 1, "line_end": 1, "quote": "q"},
                        {"path": "https://example.com/doc", "line_start": 1, "line_end": 1, "quote": "a duration"}]}
    text = render.finding_section(fd, web_base=None, head_sha="0" * 40)
    assert "web source: <https://example.com/doc>" in text and "a duration" in text


def test_the_summary_and_review_cost_lines_come_from_the_run_object(canned_state):
    from review_pipeline.export import to_findings_file
    from review_pipeline.models import Interval
    from review_pipeline.render import render_review

    canned_state.budget = {"calls": 3, "cost_usd": 4.105, "input_tokens": 1000, "output_tokens": 234,
                           "cache_read_tokens": 1_400_000, "cache_creation_tokens": 0}
    canned_state.timings = {"run": [Interval(started_at="2026-10-01T10:00:00.000Z",
                                             ended_at="2026-10-01T10:07:12.000Z")]}
    run = to_findings_file(canned_state)["run"]
    line = "$4.11 over 3 agent calls, 1,401,234 tokens, 7m 12s wall time"
    assert f"${run['cost_usd']:.2f}" == "$4.11"
    assert f"Cost {line}." in render_summary(canned_state)
    assert f"- **Model cost:** {line}" in render_review(canned_state)


def test_duration_format():
    from review_pipeline.render import duration

    assert (duration(0), duration(59.4), duration(60), duration(432)) == ("0s", "59s", "1m 0s", "7m 12s")


# --- sections and critical mode ------------------------------------------------


def _rated(canned_state, mode: str, **by_claim: tuple[str, str]) -> ReviewState:
    """canned_state with topic and impact set on the verdicts of the findings whose claim starts with a key."""
    state = canned_state.model_copy(deep=True)
    state.mode = mode
    for f in state.findings:
        for start, (topic, impact) in by_claim.items():
            if f.claim.startswith(start):
                state.verdicts[f.id] = state.verdicts[f.id].model_copy(update={"topic": topic, "impact": impact})
    return state


def test_findings_are_listed_in_code_tests_and_docs_sections_with_their_impact(canned_state):
    state = _rated(canned_state, "full", **{CLAIM_HIGH[:20]: ("code", "breaks-users"),
                                            CLAIM_MEDIUM[:20]: ("tests", "correctness-only")})
    text = render_review(state)
    assert "- **Findings:** Code 1 HIGH, 0 MEDIUM, 0 LOW; Tests 0 HIGH, 0 MEDIUM, 1 LOW; Docs none" in text
    assert text.index("## Code") < text.index(CLAIM_HIGH) < text.index("## Tests") < text.index(CLAIM_MEDIUM)
    assert "**Impact:** breaks-users" in text and "**Impact:** correctness-only" in text
    assert "Mode:" not in text
    summary = render_summary(state)
    lines = summary.splitlines()
    assert lines.index("## Code") < lines.index("## Tests")
    assert "- 1 LOW in the full review." in summary


def test_critical_mode_lists_only_findings_that_break_users_or_the_business(canned_state, tmp_path):
    state = _rated(canned_state, "critical", **{CLAIM_HIGH[:20]: ("code", "breaks-business"),
                                                CLAIM_MEDIUM[:20]: ("code", "hygiene")})
    text = render_review(state)
    body, appendix = text.split("## Appendix", 1)
    assert "- **Mode:** critical." in text
    assert "- **Findings:** Code 1 HIGH, 0 MEDIUM, 0 LOW; Tests none; Docs none" in text
    assert CLAIM_HIGH in body and CLAIM_MEDIUM not in body
    assert "- **not critical** (LOW)" in appendix and "Impact: hygiene; Code section." in appendix
    summary = render_summary(state)
    assert "Critical mode: only findings" in summary and CLAIM_MEDIUM[:30] not in summary
    out = tmp_path / "out"
    (out / "t").mkdir(parents=True)
    write_findings_file(state, out / "t")
    assert json.loads((out / "t" / "findings.json").read_text())["mode"] == "critical"
    write_rollups(out)
    assert "No LOW findings to act on." in (out / "LOW-only.md").read_text()
    assert CLAIM_HIGH in (out / "HIGH-only.md").read_text()


def test_post_without_decisions_takes_what_the_review_lists(canned_state):
    state = _rated(canned_state, "critical", **{CLAIM_HIGH[:20]: ("code", "hygiene")})
    data = to_findings_file(state)
    assert post_mod.select(data["findings"], None, data["mode"]) == []
    # The LOW finding has no topic, so it is in Code, which posts every severity.
    assert [f["claim"] for f in post_mod.select(data["findings"], None, "full")] == [CLAIM_HIGH, CLAIM_MEDIUM]


def test_a_dependency_citation_is_not_linked_into_the_reviewed_repository():
    from review_pipeline.render import code_link

    path = f".review-deps/lib@{'a' * 12}/lib/runner.py"
    assert code_link("https://github.com/o/r", "f" * 40, path, 3, 4) == f"`{path}:3-4`"


# --- earlier rounds: statuses, Recurring, the summary line ---------------------------------

PAST = "1" * 40
PAST2 = "2" * 40


def _status(fid: str, status: str, lineage: str | None = None, **kw):
    from review_pipeline.models import Citation, EarlierStatus

    cited = [Citation(path="rateplan_service.py", line_start=1, line_end=1, quote="KEYWORD = 'dolcebot'")]
    return EarlierStatus(earlier_id=fid, lineage=lineage or fid, round=PAST, status=status,
                         reason=kw.pop("reason", f"{status} at head"), citations=cited, **kw)


def _round(head: str, when: str, findings: list[dict], earlier: list[dict] | None = None) -> dict:
    data = {"generated_at": when, "target": {"head_sha": head}, "findings": findings, "mode": "full"}
    return {**data, "earlier": earlier} if earlier is not None else data


def _listed(fid: str, claim: str, file: str = "a.py", **extra) -> dict:
    return {"id": fid, "status": "confirmed", "severity": "MEDIUM", "claim": claim, "file": file, **extra}


def test_the_review_lists_each_earlier_status_with_its_lines_and_counts_new_apart_from_still_open(canned_state):
    canned_state.earlier_status = [_status("aaaaaaaaaa", "still_present"),
                                   _status("bbbbbbbbbb", "cannot_verify", reason="not rechecked")]
    past = [_round(PAST, "2026-01-01T00:00:00Z", [_listed("aaaaaaaaaa", "the keyword is hardcoded"),
                                                    _listed("bbbbbbbbbb", "the helper is never called")])]
    review = render_review(canned_state, earlier_rounds=past)
    section = review[review.index("## Findings from earlier rounds"):review.index("## Appendix")]
    assert "- **still_present** `aaaaaaaaaa` (round 111111111111) — the keyword is hardcoded" in section
    assert "- **cannot_verify** `bbbbbbbbbb` (round 111111111111) — the helper is never called" in section
    assert "  not rechecked" in section
    assert "rateplan_service.py:1" in section
    assert "## Recurring" not in review  # two rounds only
    line = "2 new, 1 still open, 0 earlier resolved, 1 earlier cannot verify"
    assert f"- **Against earlier rounds:** {line}" in review
    assert f"Against earlier rounds: {line}." in render_summary(canned_state)
    # The count line is unchanged.
    assert "- **Findings:** Code 1 HIGH, 0 MEDIUM, 1 LOW; Tests none; Docs none" in review


def test_a_first_round_has_no_earlier_sections_or_line(canned_state):
    review = render_review(canned_state)
    assert "## Findings from earlier rounds" not in review and "Against earlier rounds" not in review
    assert "Against earlier rounds" not in render_summary(canned_state)


def test_recurring_names_a_lineage_open_in_exactly_three_rounds(canned_state):
    first = _round(PAST2, "2026-01-01T00:00:00Z", [_listed("cccccccccc", "the keyword is hardcoded")])
    second = _round(PAST, "2026-01-02T00:00:00Z",
                    [_listed("dddddddddd", "the literal keyword again", file="b.py", lineage="cccccccccc")])
    canned_state.earlier_status = [_status("dddddddddd", "partially_resolved", lineage="cccccccccc")]
    review = render_review(canned_state, earlier_rounds=[first, second])
    section = review[review.index("## Recurring"):review.index("## Appendix")]
    assert "- `cccccccccc` open in 3 rounds (`222222222222`, `111111111111`, " in section
    assert "Files: `a.py`, `b.py`, `rateplan_service.py`" in section
    assert "the literal keyword again" in section
    assert "fix" not in section.lower()  # it states what recurred; it proposes nothing
    # Two rounds are not enough, and a resolved status does not count as open.
    assert "## Recurring" not in render_review(canned_state, earlier_rounds=[second])
    canned_state.earlier_status = [_status("dddddddddd", "resolved", lineage="cccccccccc")]
    assert "## Recurring" not in render_review(canned_state, earlier_rounds=[first, second])


def test_write_review_reads_the_kept_rounds_for_recurring(canned_state, tmp_path):
    for i, head in enumerate((PAST2, PAST), 1):
        d = tmp_path / "rounds" / f"0{i}-{head[:12]}"
        d.mkdir(parents=True)
        (d / "findings.json").write_text(json.dumps(_round(head, f"2026-01-0{i}T00:00:00Z",
                                                           [_listed("eeeeeeeeee", "the same defect")])))
    canned_state.earlier_status = [_status("eeeeeeeeee", "still_present")]
    path = write_review(canned_state, tmp_path)
    assert "- `eeeeeeeeee` open in 3 rounds" in path.read_text()
