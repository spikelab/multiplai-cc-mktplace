from __future__ import annotations

from conftest import (
    DEF_CITATION, KEYWORD_CITATION, KEYWORD_USE_CITATION, USE_CITATION, cite, high_finding,
)
from review_pipeline import gates
from review_pipeline.models import Citation, Fix, Premise, Verdict

OTA_STATEMENT = "CHANNEX_OC_OTA_NAME is the channel title"


# --- the four DB2038 regression tests (plan item 9) ------------------------------


def test_regression_premise_citing_the_definition_fails_and_names_the_consumer(target_info):
    premise = Premise(kind="in_repo", statement=OTA_STATEMENT, symbol="CHANNEX_OC_OTA_NAME", citation=DEF_CITATION)
    assert gates.premise_gate(target_info, premise).passed  # the quote itself is real
    result = gates.symbol_consumer_gate(target_info, premise)
    assert not result.passed
    assert "premise cites the definition of CHANNEX_OC_OTA_NAME, not a consumer" in result.reason
    assert "direct_booking.py:6" in result.reason


def test_regression_premise_citing_a_consumer_passes(target_info):
    premise = Premise(kind="in_repo", statement=OTA_STATEMENT, symbol="CHANNEX_OC_OTA_NAME", citation=USE_CITATION)
    # Provenance, not truth: the statement is still wrong; check_fix judges that.
    assert gates.symbol_consumer_gate(target_info, premise).passed
    assert gates.fix_gate(target_info, Fix(description="d", premises=[premise])).passed


def test_regression_external_premise_passes(target_info):
    premise = Premise(kind="external", statement="the Open Channel is titled DolceBot")
    assert gates.premise_gate(target_info, premise).passed
    assert gates.symbol_consumer_gate(target_info, premise).passed
    assert gates.fix_gate(target_info, Fix(description="d", premises=[premise])).passed


def test_regression_citation_moved_by_one_line_fails(target_info):
    assert gates.citation_gate(target_info, DEF_CITATION).passed
    moved = DEF_CITATION.model_copy(update={"line_start": DEF_CITATION.line_start + 1,
                                            "line_end": DEF_CITATION.line_end + 1})
    result = gates.citation_gate(target_info, moved)
    assert not result.passed and result.reason == "quote not at cited lines"


# --- citation_gate ---------------------------------------------------------------


def test_citation_whitespace_is_normalised(target_info):
    c = Citation(path="rateplan_service.py", line_start=4, line_end=6,
                 quote="def eligible_rate_plans(rate_plans):\n      \"\"\"Rate plans   the direct-booking modal may offer.\"\"\"")
    assert gates.citation_gate(target_info, c).passed


def test_citation_path_not_at_head(target_info):
    result = gates.citation_gate(target_info, cite("nope.py", 1, "x"))
    assert result.reason == "path not at head"


def test_citation_empty_quote_fails(target_info):
    assert gates.citation_gate(target_info, cite("settings.py", 1, "   ")).reason == "empty quote"


def test_citation_past_end_of_file_fails(target_info):
    assert not gates.citation_gate(target_info, cite("settings.py", 40, "CHANNEX")).passed


# --- finding_gate ----------------------------------------------------------------


def test_finding_gate_passes_a_grounded_finding(target_info):
    assert gates.finding_gate(target_info, high_finding()).passed


def test_finding_gate_rejects_a_bad_citation(target_info):
    f = high_finding().with_location(citations=[cite("rateplan_service.py", 2, "KEYWORD = 'dolcebot'").model_dump()])
    result = gates.finding_gate(target_info, f)
    assert not result.passed and "quote not at cited lines" in result.reason


def test_finding_gate_requires_a_changed_file_unless_pre_existing(target_info):
    f = high_finding().with_location(file="README.md", citations=[cite("README.md", 1, "# Booking engine").model_dump()])
    assert "not a changed file" in gates.finding_gate(target_info, f).reason
    assert gates.finding_gate(target_info, f.with_location(dimension="pre-existing")).passed


# --- premise_gate / symbol_consumer_gate -----------------------------------------


def test_in_repo_premise_needs_a_citation(target_info):
    result = gates.premise_gate(target_info, Premise(kind="in_repo", statement="x"))
    assert not result.passed


def test_external_premise_must_not_cite(target_info):
    result = gates.premise_gate(target_info, Premise(kind="external", statement="x", citation=USE_CITATION))
    assert not result.passed


def test_symbol_is_extracted_from_the_statement_when_blank(target_info):
    premise = Premise(kind="in_repo", statement=OTA_STATEMENT, citation=DEF_CITATION)
    assert gates.premise_symbols(target_info, premise) == ["CHANNEX_OC_OTA_NAME"]
    assert not gates.symbol_consumer_gate(target_info, premise).passed


def test_undefined_all_caps_words_are_not_symbols(target_info):
    premise = Premise(kind="in_repo", statement="The JSON payload carries the booking id",
                      citation=cite("direct_booking.py", 5, "payload = {'id': booking.id}"))
    assert gates.premise_symbols(target_info, premise) == []
    assert gates.symbol_consumer_gate(target_info, premise).passed


def test_citation_that_touches_neither_use_nor_definition_fails(target_info):
    premise = Premise(kind="in_repo", statement="x", symbol="CHANNEX_OC_OTA_NAME", citation=KEYWORD_CITATION)
    result = gates.symbol_consumer_gate(target_info, premise)
    assert not result.passed and "not a line that uses it" in result.reason


def test_constant_use_in_same_file_counts_as_consumer(target_info):
    premise = Premise(kind="in_repo", statement="KEYWORD filters rate plans by title", symbol="KEYWORD",
                      citation=KEYWORD_USE_CITATION)
    assert gates.symbol_consumer_gate(target_info, premise).passed
    at_def = premise.model_copy(update={"citation": KEYWORD_CITATION})
    assert "definition of KEYWORD" in gates.symbol_consumer_gate(target_info, at_def).reason


def test_is_definition():
    assert gates.is_definition("X_Y", "X_Y = 1")
    assert gates.is_definition("X_Y", "    X_Y: int = 1")
    assert gates.is_definition("X_Y", "v = config('X_Y', default=1)")
    assert gates.is_definition("X_Y", 'v = os.getenv("X_Y")')
    assert not gates.is_definition("X_Y", "if settings.X_Y == 1:")


# --- fix_gate / verdict_gate -----------------------------------------------------


def test_fix_gate_names_the_failing_premise(target_info):
    fix = Fix(description="d", premises=[
        Premise(kind="external", statement="the Open Channel is titled DolceBot"),
        Premise(kind="in_repo", statement=OTA_STATEMENT, symbol="CHANNEX_OC_OTA_NAME", citation=DEF_CITATION),
    ])
    result = gates.fix_gate(target_info, fix)
    assert not result.passed and result.reason.startswith("premise 1 ")
    assert "definition of CHANNEX_OC_OTA_NAME" in result.reason


def test_fix_gate_rejects_a_fix_without_premises(target_info):
    assert not gates.fix_gate(target_info, Fix(description="d")).passed


def test_verdict_gate(target_info):
    ok = Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION])
    assert gates.verdict_gate(target_info, ok).passed
    bare = Verdict(status="confirmed", reason="r")
    assert gates.verdict_gate(target_info, bare).action == "downgrade"
    wrong = Verdict(status="confirmed", reason="r", citations=[cite("settings.py", 1, "nothing like this")])
    assert not gates.verdict_gate(target_info, wrong).passed
    assert gates.verdict_gate(target_info, Verdict(status="refuted", reason="r")).passed


def test_no_gate_name_starts_with_test():
    assert not [n for n in dir(gates) if n.startswith("test")]
