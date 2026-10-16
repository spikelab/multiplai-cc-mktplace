from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fixture_repo import build as build_repo  # noqa: E402
from fixture_repo import (  # noqa: E402
    KEYWORD_LINE, KEYWORD_USE_LINE, SETTINGS_DEF_LINE,
)

from review_pipeline import budget, deps, ledger, target  # noqa: E402
from review_pipeline.models import Citation, Finding, Rejected, ReviewState, Verdict  # noqa: E402

SCHEMA = TESTS.parents[2] / "review-viewer" / "schema" / "findings.v1.schema.json"

# The real function, for the tests that exercise it; every other test gets
# the stub below so nothing here calls `gh`.
REAL_BRANCH_RULES = target.branch_rules
# The real data directory lookup, for the test that exercises it; every other
# test writes its reviews and ledger under its own tmp_path.
REAL_DATA_DIR = ledger.data_dir
# The real lookup of the default output directory before 0.45 (`INBOX/reviews`,
# else `~/.multiplai/reviews`), for the tests that exercise it; every other
# test gets an empty one, so reviews a real run left in the home directory are never read.
REAL_LEGACY_REVIEWS_ROOT = ledger.legacy_reviews_root


@pytest.fixture(autouse=True)
def no_gh_branch_rules(monkeypatch):
    monkeypatch.setattr(target, "branch_rules", lambda repo, branch, needs=None: None)

CLAIM_HIGH = "Rate-plan eligibility depends on the hardcoded keyword 'dolcebot'"
CLAIM_MEDIUM = "eligible_rate_plans lower-cases the title but not the keyword"
CLAIM_REFUTED = "build_payload drops the booking id"
CLAIM_REJECTED = "settings.py imports an undefined helper"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No real multiplai.conf, no inherited trust, a fresh unlimited ledger, no real data directory."""
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path / "no-conf"))
    monkeypatch.setattr(ledger, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(ledger, "legacy_reviews_root", lambda: tmp_path / "no-legacy-reviews")
    monkeypatch.delenv("REVIEW_TRUST_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(deps, "cache_root", lambda: tmp_path / "dep-cache")
    budget.start(None)


@pytest.fixture
def allow_file_urls(monkeypatch):
    """Let the dependency code clone the tests' local bare repositories (file:// URLs)."""
    monkeypatch.setattr(deps, "ALLOWED_SCHEMES", {*deps.ALLOWED_SCHEMES, "file"})


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


EXPECTED_HIGH = "Rate plans are matched on whatever the Open Channel is titled in Channex, not a fixed word."
EXPECTED_MEDIUM = "A keyword in any case matches a title in any case."


@pytest.fixture
def canned_state(target_info) -> ReviewState:
    """One of each outcome: confirmed, lowered unverifiable, refuted, gate-rejected."""
    high, medium, refuted, rejected = high_finding(), medium_finding(), refuted_finding(), rejected_finding()
    medium_lowered = medium.model_copy(update={"severity": "LOW"})
    return ReviewState(
        target=target_info.model_copy(update={"deployed_in": None}),
        stage="merge",
        findings=[high, medium_lowered, refuted],
        verdicts={
            high.id: Verdict(finding_id=high.id, status="confirmed", reason="the keyword is the only filter",
                             citations=[KEYWORD_USE_CITATION], expected_behaviour=EXPECTED_HIGH),
            medium.id: Verdict(finding_id=medium.id, status="unverifiable",
                               reason="depends on the production channel title", expected_behaviour=EXPECTED_MEDIUM),
            refuted.id: Verdict(finding_id=refuted.id, status="refuted", reason="line 5 sets the id",
                                citations=[refuted.citations[0]], expected_behaviour="should not be exported"),
        },
        rejected=[Rejected(finding=rejected, reason="citation 0 (settings.py:1-1): quote not at cited lines")],
        original_severity={medium.id: "MEDIUM"},
        budget={"cost_usd": 1.25, "calls": 9},
    )


# --- post: a PR review and a fake `gh` ---------------------------------------------


@pytest.fixture
def pr_review(canned_state, tmp_path):
    from review_pipeline.export import write_findings_file
    from review_pipeline.state import save_state

    canned_state.target.pr, canned_state.target.kind = 812, "pr"
    target_dir = tmp_path / canned_state.target.slug
    target_dir.mkdir(exist_ok=True)
    save_state(canned_state, target_dir)
    write_findings_file(canned_state, target_dir)
    return canned_state, target_dir


class FakeGh(list):
    """The `gh` calls `post` makes, as `(argv, stdin)`. Any other command (git) runs for real.

    `respond(fragment, ...)` sets the result of every gh call whose argv joined
    by spaces contains *fragment*; the two GETs answer `[]` until told otherwise.
    """

    def __init__(self, real_run):
        super().__init__()
        self._real_run = real_run
        self._responses: list[tuple[str, int, str, str]] = []

    def respond(self, fragment: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self._responses.insert(0, (fragment, returncode, stdout, stderr))

    def __call__(self, argv, **kwargs):
        if argv[0] != "/usr/bin/gh":
            return self._real_run(argv, **kwargs)
        self.append((argv, kwargs.get("input")))
        joined = " ".join(argv)
        for fragment, code, out, err in self._responses:
            if fragment in joined:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "[]" if "--paginate" in argv else "{}", "")

    def posts(self) -> list[tuple[list[str], dict]]:
        """The POST calls, with their JSON payload parsed."""
        return [(argv, json.loads(stdin)) for argv, stdin in self if "POST" in argv]

    def posted_text(self) -> str:
        """Every body in the one POST: the review body, then each comment's."""
        (_, payload), = self.posts()
        return "\n".join([payload["body"], *(c["body"] for c in payload["comments"])])


@pytest.fixture
def fake_gh(monkeypatch):
    from review_pipeline import post as post_mod

    fake = FakeGh(subprocess.run)
    monkeypatch.setattr(post_mod.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(post_mod.subprocess, "run", fake)
    return fake


# multiplai-core 0.21: a step with no configured model or effort runs on
# MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the Claude Code session's own
# (multiplai_core.session). Run inside a session, the developer's values would
# set a test's model, so clear them and hide the developer's multiplai.conf
# from that lookup. A test that wants one sets it with monkeypatch.
_SESSION_DEFAULT_VARS = (
    "MULTIPLAI_MODEL",
    "MULTIPLAI_EFFORT",
    "MULTIPLAI_SESSION_MODEL",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_session_defaults(monkeypatch):
    for key in _SESSION_DEFAULT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("multiplai_core.session.load_multiplai_conf",
                        lambda: {"_sections": {}})
