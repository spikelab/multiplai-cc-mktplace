"""Doctor pass 3 — the pruning arm, and the one that must be hardest to please.

Reads P3's utilisation table (:func:`lib.utilisation.load_table`) and reports
three *different* findings, kept apart because they mean different things:

======================  ==============================================
**Never retrieved**     no injection record at all — never reached a
                        prompt, so there is no evidence either way
**Retrieved, unused**   injected enough times to count, and estimated
                        used at or below the threshold **on both
                        estimators independently**
**Expensive**           high bytes per estimated use, ranked
======================  ==============================================

Pure and model-free. Everything here is arithmetic over a table and a regex over
memory text; nothing calls a model, so nothing here can fail open.

## Four rules, and every one of them is a consequence of P3's honesty

**1. Nothing below the sample floor is ever reported.** A section seen three
times is not evidence, and P3 puts exactly those rows in ``insufficient_data``
with ``rank_basis`` and ``cost_per_use`` set to ``null``. This module never
reads that bucket for candidates. The floor is stated in the report.

**2. A missing judge value is not "unused".** ``rate: None`` means *not
estimated*; zero use is ``rate: 0.0`` with ``zero_estimated_use`` set. Treating
the first as the second would mark the whole corpus dead during a judge outage —
a failure that *widens* what gets pruned, which contract C4 forbids. So a
candidate needs an actual rate from each estimator, not the absence of one.

**3. A rule-bearing section is never proposed for deletion on utilisation
grounds.** Behavioural rules are retrieved rarely *by construction* — they apply
to a narrow situation and are silent otherwise — and they are precisely the
content whose absence nobody notices, because what you lose is a behaviour, not
a fact you go looking for. Utilisation is the wrong instrument for them.

  The detection is :func:`looks_normative`, a regex — the same *shape* of gate
  P4 deleted from ``dream_triage`` for being semantically wrong. It is correct
  here because the failure direction is inverted. There, a false positive sent a
  true fact to a human and cost 75% of the review burden. Here, a false positive
  merely declines to propose one pruning candidate, which costs nothing at all.
  A gate is only as good or bad as what its errors do.

**4. Everything is labelled an estimate** (master plan, decision 9). Every
finding names its estimator, shows ``used/observed``, and says the number is
estimated. A dead-weight proposal that hides its ``n`` is a deletion argued from
an authority the data does not have.

## The common case is "no data yet"

The judge accrues at ~5 sampled sessions a day and self-report needs a session
to have run at all. For a long while after this ships, the honest output of this
pass is **nothing**, and it says so rather than ranking three rows of noise.
:data:`MIN_COVERAGE_SESSIONS` is the whole-pass floor that enforces it.

**Nothing here edits memory** (contract C5), and nothing here acts on the table
beyond reporting it. P3 produces estimates; this reports them; a human decides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional

from lib import utilisation as util
from lib.section_loader import extract_section

logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "DeadWeight",
    "MIN_COVERAGE_SESSIONS",
    "UNUSED_RATE_THRESHOLD",
    "EXPENSIVE_LIMIT",
    "looks_normative",
    "section_text",
    "find_dead_weight",
    "run_pass",
    "render_section",
]

#: Sessions of coverage below which the whole pass reports nothing. Ranking a
#: corpus off two sessions is not a pruning signal, it is a coin toss with a
#: table around it.
MIN_COVERAGE_SESSIONS = 10

#: Estimated use rate at or below which a section counts as unused. Must be met
#: on **both** estimators independently — a single estimator saying "unused" is
#: one model's opinion, and the two are never averaged.
UNUSED_RATE_THRESHOLD = 0.1

#: How many "expensive" rows to list. The tail is long and uninformative.
EXPENSIVE_LIMIT = 15


# --- rule detection (the protective gate) -----------------------------------

_NORMATIVE_RE = re.compile(
    r"(?:^|[\s(\[*_`>-])(?:"
    r"always|never|must(?:\s+not)?|should(?:\s+not)?|shall|do\s+not|don'?t|"
    r"avoid|prefer|require[sd]?|forbidden|mandatory|non-negotiable|"
    r"the\s+rule\s+is|rule\s+of\s+thumb|convention\s+is|policy|"
    r"stop\s+and\s+ask|by\s+default\s+use"
    r")\b",
    re.IGNORECASE,
)

#: A **bare imperative** opening a bullet or a line: "Commit frequently…",
#: "Bind to 0.0.0.0…", "Use `git -C <dir>` instead of cd." None of these carry
#: any of the keywords above, and all three are real rules from this corpus — the
#: characteristic failure of a keyword list is this direction, not the other one.
#: Matched only at the start of a line or bullet, because mid-sentence these
#: words are ordinary verbs ("we run the suite nightly" is a fact).
_IMPERATIVE_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+\.|>)?[ \t]*(?:\*\*)?(?:"
    r"use|run|bind|commit|keep|check|read|write|add|remove|delete|set|"
    r"pass|call|prefer|treat|route|stage|start|stop|leave|ask|verify|"
    r"ensure|make|put|name|declare|reach|hold|flag|report|state|cite"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)


def looks_normative(text: str) -> bool:
    """True when *text* reads as behavioural guidance rather than a fact.

    Deliberately generous, and generous in **two** ways: a keyword list *and* a
    bare-imperative opener. See rule 3 in the module docstring — a false positive
    costs one unproposed pruning candidate and is listed with its reason, while a
    false negative proposes deleting a behavioural rule because nobody noticed it
    was one, and the cost of that is a behaviour that quietly stops happening.

    The keyword half alone missed every bare imperative, which is the *usual*
    shape of a rule in this corpus: "Commit frequently throughout development.",
    "Bind to 0.0.0.0, not 127.0.0.1.", "Use ``git -C <dir>`` instead of cd."
    """
    if not text:
        return False
    return bool(_NORMATIVE_RE.search(text) or _IMPERATIVE_RE.search(text))


def section_text(memory_dir: Path, key: str) -> str:
    """The memory text behind a utilisation key, or ``""``.

    ``file.md#Section`` yields that H2's body; a bare ``file.md`` — which means
    *the whole file was injected*, not "no section" — yields the whole file.
    """
    filename, section = util.split_key(key)
    path = Path(memory_dir) / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return extract_section(text, section) if section else text


# --- findings ---------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One dead-weight finding, carrying the evidence that produced it."""

    key: str
    finding: str            # "never-retrieved" | "retrieved-unused" | "expensive"
    retrieved: int = 0
    bytes: int = 0
    self_report: Optional[dict] = None
    judge: Optional[dict] = None
    rank_basis: Optional[str] = None
    cost_per_use: Optional[float] = None
    zero_use: bool = False
    disagreement: bool = False
    protected: bool = False
    protected_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "finding": self.finding,
            "retrieved": self.retrieved,
            "bytes": self.bytes,
            "self_report": self.self_report,
            "judge": self.judge,
            "rank_basis": self.rank_basis,
            "cost_per_use": self.cost_per_use,
            "zero_use": self.zero_use,
            "disagreement": self.disagreement,
            "protected": self.protected,
            "protected_reason": self.protected_reason,
            "estimate": True,
        }


@dataclass
class DeadWeight:
    never_retrieved: list[Candidate] = field(default_factory=list)
    retrieved_unused: list[Candidate] = field(default_factory=list)
    expensive: list[Candidate] = field(default_factory=list)
    protected: list[Candidate] = field(default_factory=list)
    reported: bool = True
    reason: str = ""
    min_observations: int = util.MIN_OBSERVATIONS
    threshold: float = UNUSED_RATE_THRESHOLD
    coverage: dict = field(default_factory=dict)
    insufficient: int = 0

    def as_dict(self) -> dict:
        return {
            "never_retrieved": [c.as_dict() for c in self.never_retrieved],
            "retrieved_unused": [c.as_dict() for c in self.retrieved_unused],
            "expensive": [c.as_dict() for c in self.expensive],
            "protected": [c.as_dict() for c in self.protected],
            "reported": self.reported,
            "reason": self.reason,
            "min_observations": self.min_observations,
            "threshold": self.threshold,
            "coverage": self.coverage,
            "insufficient": self.insufficient,
        }

    @property
    def total(self) -> int:
        return (len(self.never_retrieved) + len(self.retrieved_unused)
                + len(self.expensive))


def _estimator_block(row: Mapping, name: str) -> dict:
    data = row.get(name) or {}
    return {
        "observed": int(data.get("observed") or 0),
        "used": int(data.get("used") or 0),
        "rate": data.get("rate"),
    }


def _is_unused(row: Mapping, *, min_observations: int, threshold: float) -> bool:
    """Both estimators independently observed enough and estimated near-zero use.

    A ``None`` rate fails this test — rule 2. It is never coerced to 0.0, and
    the two rates are never averaged into one.
    """
    for name in util.ESTIMATORS:
        block = _estimator_block(row, name)
        if block["observed"] < min_observations:
            return False
        rate = block["rate"]
        if not isinstance(rate, (int, float)):
            return False
        if rate > threshold:
            return False
    return True


def find_dead_weight(
    table: Mapping,
    *,
    memory_dir: Optional[Path] = None,
    min_coverage_sessions: int = MIN_COVERAGE_SESSIONS,
    threshold: float = UNUSED_RATE_THRESHOLD,
    expensive_limit: int = EXPENSIVE_LIMIT,
) -> DeadWeight:
    """Turn P3's table into dead-weight findings. Pure; no model, no writes.

    *memory_dir* is read (read-only) to look up each candidate's text for the
    rule-bearing check. Without it nothing can be protected, so the whole pass
    reports nothing rather than proposing deletions it cannot screen.
    """
    thresholds = table.get("thresholds") or {}
    min_observations = int(thresholds.get("min_observations")
                           or util.MIN_OBSERVATIONS)
    coverage = dict(table.get("coverage") or {})
    result = DeadWeight(
        min_observations=min_observations,
        threshold=threshold,
        coverage=coverage,
        insufficient=len(table.get("insufficient_data") or []),
    )

    sessions = int(coverage.get("sessions") or 0)
    if sessions < min_coverage_sessions:
        result.reported = False
        result.reason = (
            f"only {sessions} session(s) of utilisation coverage — below the "
            f"{min_coverage_sessions}-session floor for this pass. Nothing is "
            f"proposed. This is the expected output until the telemetry has "
            f"accumulated; it is not a finding that the corpus is healthy."
        )
        return result
    if memory_dir is None:
        result.reported = False
        result.reason = (
            "no memory directory was available to screen candidates for "
            "behavioural rules, so nothing is proposed — an unscreened pruning "
            "list is exactly what rule 3 exists to prevent."
        )
        return result

    def _protect(candidate: Candidate) -> tuple[bool, str]:
        """``(protect?, reason)`` for one candidate.

        **Fails closed.** ``section_text`` returns ``""`` when the file cannot be
        read (``OSError``) or is empty, and returning ``False`` there skipped the
        rule screen entirely — so a candidate nobody could screen was *proposed
        for pruning*. That is the wrong direction: rule 3 and contract C4 both
        say a failure must narrow what gets pruned, not widen it.

        Worth stating precisely, because it is narrower than it looks: a stale
        key whose *heading* was renamed does **not** reach this branch.
        ``extract_section`` falls back to the whole file when no H2 matches, so
        the rule is still found and screened — that fallback is doing real work
        here. The uncoverable cases are an unreadable file and an empty one.
        """
        text = section_text(memory_dir, candidate.key)
        if not text:
            return True, (
                "this section could not be read back from the corpus (renamed "
                "heading, moved content, or an unreadable file), so it could not "
                "be screened for behavioural rules — an unscreened candidate is "
                "withheld rather than proposed"
            )
        if looks_normative(text):
            return True, (
                "the section reads as behavioural guidance (normative "
                "language); utilisation is the wrong instrument for a rule"
            )
        return False, ""

    def _emit(candidate: Candidate, bucket: list[Candidate]) -> None:
        protect, reason = _protect(candidate)
        if protect:
            result.protected.append(replace(
                candidate,
                protected=True,
                protected_reason=reason,
            ))
            return
        bucket.append(candidate)

    # --- never retrieved ---
    for key in table.get("never_retrieved") or []:
        _emit(Candidate(key=str(key), finding="never-retrieved"),
              result.never_retrieved)

    # --- retrieved but unused, and expensive ---
    ranked = [r for r in (table.get("sections") or []) if r.get("sufficient")]
    for row in ranked:
        candidate = Candidate(
            key=str(row.get("key") or ""),
            finding="retrieved-unused",
            retrieved=int(row.get("retrieved") or 0),
            bytes=int(row.get("bytes") or 0),
            self_report=_estimator_block(row, "self_report"),
            judge=_estimator_block(row, "judge"),
            rank_basis=row.get("rank_basis"),
            cost_per_use=row.get("cost_per_use"),
            zero_use=bool((row.get("zero_estimated_use") or {}).get(
                row.get("rank_basis"))),
            disagreement=bool(row.get("disagreement")),
        )
        if not candidate.key:
            continue
        if _is_unused(row, min_observations=min_observations, threshold=threshold):
            _emit(candidate, result.retrieved_unused)

    unused_keys = {c.key for c in result.retrieved_unused}
    unused_keys |= {c.key for c in result.protected}
    expensive = [
        r for r in ranked
        if r.get("key") not in unused_keys
        and isinstance(r.get("cost_per_use"), (int, float))
    ]
    expensive.sort(key=lambda r: (-(r.get("cost_per_use") or 0.0), str(r.get("key"))))
    for row in expensive[:expensive_limit]:
        _emit(Candidate(
            key=str(row.get("key")),
            finding="expensive",
            retrieved=int(row.get("retrieved") or 0),
            bytes=int(row.get("bytes") or 0),
            self_report=_estimator_block(row, "self_report"),
            judge=_estimator_block(row, "judge"),
            rank_basis=row.get("rank_basis"),
            cost_per_use=row.get("cost_per_use"),
            disagreement=bool(row.get("disagreement")),
        ), result.expensive)

    return result


def run_pass(
    memory_dir: Path,
    data_dir: Path,
    *,
    catalog: Optional[dict] = None,
    min_coverage_sessions: int = MIN_COVERAGE_SESSIONS,
) -> dict:
    """Load the utilisation table and produce the findings. Read-only throughout."""
    table = util.load_table(Path(data_dir), catalog=catalog)
    result = find_dead_weight(
        table,
        memory_dir=Path(memory_dir),
        min_coverage_sessions=min_coverage_sessions,
    )
    payload = result.as_dict()
    payload["disclaimer"] = table.get("disclaimer", util.DISCLAIMER)
    payload["estimator_notes"] = table.get("estimator_notes", dict(util.ESTIMATOR_NOTES))
    return payload


# --- rendering --------------------------------------------------------------

def _fmt_rate(block: Optional[Mapping]) -> str:
    if not block or not block.get("observed"):
        return "not estimated (n=0)"
    rate = block.get("rate")
    if not isinstance(rate, (int, float)):
        return f"not estimated (n={block['observed']})"
    return f"{rate:.0%} estimated used ({block['used']}/{block['observed']})"


def _fmt_cost(value: Optional[float]) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if value >= 1000:
        return f"{value / 1000:.1f} KB per estimated use"
    return f"{value:.0f} B per estimated use"


def _render_candidate(number: str, item: Mapping) -> list[str]:
    out = [f"- **{number}** `{item['key']}`"]
    if item["finding"] == "never-retrieved":
        out.append("  - No injection record at all. There is **no evidence either "
                   "way** about its value — it has never reached a prompt.")
        return out
    out.append(f"  - retrieved {item['retrieved']}x, {item['bytes']:,} bytes injected")
    out.append(f"  - self-report: {_fmt_rate(item.get('self_report'))}")
    out.append(f"  - judge: {_fmt_rate(item.get('judge'))}")
    basis = item.get("rank_basis") or "—"
    if item.get("zero_use"):
        out.append(f"  - cost per estimated use: **unbounded** (zero estimated "
                   f"uses under `{basis}`)")
    else:
        out.append(f"  - cost per estimated use: {_fmt_cost(item.get('cost_per_use'))} "
                   f"(basis: `{basis}`)")
    if item.get("disagreement"):
        out.append("  - ⚠️ the two estimators disagree past the stated margin. They "
                   "are marked, never averaged — treat this row as unresolved.")
    return out


def render_section(result: Mapping, *, limit: int = 25) -> str:
    """The dead-weight section of the doctor report."""
    out: list[str] = ["## 3. Dead weight", ""]
    out.append(f"> {result.get('disclaimer', util.DISCLAIMER)}")
    out.append("")
    notes = result.get("estimator_notes") or {}
    for name in util.ESTIMATORS:
        if name in notes:
            out.append(f"- **{name}** — {notes[name]}")
    coverage = result.get("coverage") or {}
    out.append("")
    out.append(
        f"Coverage: {coverage.get('sessions', 0)} session(s) with injections · "
        f"{coverage.get('sessions_self_reported', 0)} self-reported · "
        f"{coverage.get('sessions_judged', 0)} judged."
    )
    out.append("")
    out.append(
        f"**Sample-size floor: {result.get('min_observations', util.MIN_OBSERVATIONS)} "
        f"estimator observations.** {result.get('insufficient', 0)} row(s) are below "
        f"it and were **not** considered — a section seen three times is not "
        f"evidence. A section is only called **unused** when *both* estimators "
        f"independently observed it enough times and each estimated its use at "
        f"or below {result.get('threshold', UNUSED_RATE_THRESHOLD):.0%}; a missing "
        f"estimate is never read as zero."
    )
    out.append("")
    out.append(
        "That both-estimators rule covers the *unused* finding only. **Expensive "
        "per estimated use** is ranked from whichever estimator has data — the "
        "basis is named on every row — so a telemetry outage cannot make live "
        "memory look unused, but it can still shape this list off one surviving "
        "estimator. Check the named basis before acting on a row here."
    )
    out.append("")

    if not result.get("reported", True):
        out.append(f"_Nothing proposed: {result.get('reason', '')}_")
        return "\n".join(out)

    sections = (
        ("Never retrieved", "never_retrieved",
         "No injection record at all. Not the same finding as \"retrieved and "
         "unused\" — there is no evidence either way here."),
        ("Retrieved, estimated unused", "retrieved_unused",
         "Injected enough times to count, and estimated used at or below the "
         "threshold on **both** estimators independently."),
        ("Expensive per estimated use", "expensive",
         "Ranked by bytes per estimated use under each row's own named basis. "
         "One estimator with data is enough to rank a row here — unlike "
         "\"retrieved, estimated unused\", which needs both."),
    )
    prefixes = {"never_retrieved": "N", "retrieved_unused": "U", "expensive": "E"}
    any_found = False
    for title, field_name, blurb in sections:
        items = list(result.get(field_name) or [])
        out.append(f"### {title} ({len(items)})")
        out.append("")
        out.append(blurb)
        out.append("")
        if not items:
            out.append("_None._")
            out.append("")
            continue
        any_found = True
        for number, item in enumerate(items[:limit], 1):
            out.extend(_render_candidate(f"{prefixes[field_name]}{number}.", item))
        if len(items) > limit:
            out.append(f"- … {len(items) - limit} more")
        out.append("")

    protected = list(result.get("protected") or [])
    out.append(f"### Withheld — behavioural rules ({len(protected)})")
    out.append("")
    out.append(
        "These matched a dead-weight rule but read as behavioural guidance, so "
        "they are **not** proposed for removal. Rules are retrieved rarely by "
        "construction and are exactly the content whose absence you would not "
        "notice. Utilisation is the wrong instrument for them."
    )
    out.append("")
    if not protected:
        out.append("_None._")
    else:
        for item in protected[:limit]:
            out.append(f"- `{item['key']}` — {item.get('protected_reason', '')}")
        if len(protected) > limit:
            out.append(f"- … {len(protected) - limit} more")

    if not any_found:
        out.append("")
        out.append("_No dead-weight findings above the floor._")
    return "\n".join(out)
