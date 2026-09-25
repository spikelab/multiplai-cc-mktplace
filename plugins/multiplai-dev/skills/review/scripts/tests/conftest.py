from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fixture_repo import build as build_repo  # noqa: E402
from fixture_repo import (  # noqa: E402
    DIRECT_BOOKING_USE_LINE, KEYWORD_LINE, KEYWORD_USE_LINE, SETTINGS_DEF_LINE,
)

from review_pipeline import budget, target  # noqa: E402
from review_pipeline.models import (  # noqa: E402
    Citation, Finding, Fix, FixCheck, Premise, Rejected, ReviewState, Verdict,
)

SCHEMA = TESTS.parents[2] / "review-viewer" / "schema" / "findings.v1.schema.json"

CLAIM_HIGH = "Rate-plan eligibility depends on the hardcoded keyword 'dolcebot'"
CLAIM_MEDIUM = "eligible_rate_plans lower-cases the title but not the keyword"
CLAIM_REFUTED = "build_payload drops the booking id"
CLAIM_REJECTED = "settings.py imports an undefined helper"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No real multiplai.conf, no inherited trust, a fresh unlimited ledger."""
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path / "no-conf"))
    monkeypatch.delenv("REVIEW_TRUST_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    budget.start(None)


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> tuple[Path, str, str]:
    repo = tmp_path_factory.mktemp("repo") / "booking-engine"
    base, head = build_repo(repo)
    return repo, base, head


@pytest.fixture
def target_info(fixture_repo, tmp_path):
    repo, base, head = fixture_repo
    resolved = target.resolve(repo, range_=f"{base}..{head}")
    info = target.build_target(resolved, tickets=["DB-2038"])
    return target.write_target_files(info, target.diff_text(resolved.repo, base, head), tmp_path / info.slug)


def cite(path: str, line: int, quote: str) -> Citation:
    return Citation(path=path, line_start=line, line_end=line, quote=quote)


DEF_CITATION = cite("settings.py", SETTINGS_DEF_LINE,
                    "CHANNEX_OC_OTA_NAME = config('CHANNEX_OC_OTA_NAME', default='DolceTech')")
USE_CITATION = cite("direct_booking.py", DIRECT_BOOKING_USE_LINE,
                    "payload['ota_name'] = getattr(settings, 'CHANNEX_OC_OTA_NAME', 'DolceTech')")
KEYWORD_CITATION = cite("rateplan_service.py", KEYWORD_LINE, "KEYWORD = 'dolcebot'")
KEYWORD_USE_CITATION = cite("rateplan_service.py", KEYWORD_USE_LINE,
                            "return [rp for rp in rate_plans if KEYWORD in rp['title'].lower()]")


def high_finding() -> Finding:
    return Finding(claim=CLAIM_HIGH, severity="HIGH", dimension="diff-bugs", file="rateplan_service.py",
                   line_start=KEYWORD_LINE, line_end=KEYWORD_LINE,
                   failure_scenario="The Open Channel is renamed; no rate plan matches and every room is unbookable.",
                   citations=[KEYWORD_CITATION, KEYWORD_USE_CITATION], finder="diff-bugs")


def medium_finding() -> Finding:
    return Finding(claim=CLAIM_MEDIUM, severity="MEDIUM", dimension="diff-bugs", file="rateplan_service.py",
                   line_start=KEYWORD_USE_LINE, line_end=KEYWORD_USE_LINE,
                   failure_scenario="KEYWORD set to 'DolceBot' never matches a lower-cased title.",
                   citations=[KEYWORD_USE_CITATION], finder="diff-bugs")


def refuted_finding() -> Finding:
    return Finding(claim=CLAIM_REFUTED, severity="LOW", dimension="callers", file="direct_booking.py",
                   line_start=5, line_end=5, failure_scenario="Payload has no id.",
                   citations=[cite("direct_booking.py", 5, "payload = {'id': booking.id}")], finder="callers")


def rejected_finding() -> Finding:
    return Finding(claim=CLAIM_REJECTED, severity="MEDIUM", dimension="diff-bugs", file="settings.py",
                   line_start=1, line_end=1, failure_scenario="ImportError at startup.",
                   citations=[cite("settings.py", 1, "from helpers import missing")], finder="diff-bugs")


def verified_fix(finding_id: str) -> Fix:
    return Fix(
        finding_id=finding_id,
        description="Make the keyword a setting; its default must be what the Open Channel is titled in Channex.",
        patch_sketch="RATE_PLAN_KEYWORD = config('RATE_PLAN_KEYWORD')",
        premises=[
            Premise(statement="CHANNEX_OC_OTA_NAME is stamped onto booking payloads as ota_name",
                    kind="in_repo", symbol="CHANNEX_OC_OTA_NAME", citation=USE_CITATION),
            Premise(statement="the Open Channel is titled DolceBot", kind="external",
                    question="What is the Open Channel titled in Channex?"),
        ],
    )


@pytest.fixture
def canned_state(target_info) -> ReviewState:
    """One of each outcome: confirmed with a fix, lowered unverifiable, refuted, gate-rejected."""
    high, medium, refuted, rejected = high_finding(), medium_finding(), refuted_finding(), rejected_finding()
    medium_lowered = medium.model_copy(update={"severity": "LOW"})
    return ReviewState(
        target=target_info.model_copy(update={"deployed_in": None}),
        stage="check_fix",
        findings=[high, medium_lowered, refuted],
        verdicts={
            high.id: Verdict(finding_id=high.id, status="confirmed", reason="the keyword is the only filter",
                             citations=[KEYWORD_USE_CITATION]),
            medium.id: Verdict(finding_id=medium.id, status="unverifiable",
                               reason="depends on the production channel title"),
            refuted.id: Verdict(finding_id=refuted.id, status="refuted", reason="line 5 sets the id",
                                citations=[refuted.citations[0]]),
        },
        fixes={high.id: verified_fix(high.id)},
        fix_checks={high.id: FixCheck(finding_id=high.id, status="confirmed", reason="nothing else reads KEYWORD")},
        rejected=[Rejected(finding=rejected, reason="citation 0 (settings.py:1-1): quote not at cited lines")],
        original_severity={medium.id: "MEDIUM"},
        budget={"cost_usd": 1.25, "calls": 9},
    )
