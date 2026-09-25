"""Pure-code gates. None of them calls a model.

Each gate takes the `TargetInfo` and the object it judges and returns a
`GateResult`. Everything they check is re-read from the target's head commit
with `git show` / `git grep`, never taken from what an agent said it read.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import functools
import logging
import re
import subprocess

from .models import SEVERITIES, Citation, Finding, Fix, GateResult, Premise, TargetInfo, Verdict
from .target import _GIT, _env

log = logging.getLogger(__name__)

# How a premise's statement names a settings key, constant or env var when the
# model left `symbol` blank.
SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
MAX_CONSUMERS_IN_REASON = 5


def _normalise(text: str) -> str:
    return " ".join(text.split())


@functools.lru_cache(maxsize=512)
def _show(repo: str, sha: str, path: str) -> str | None:
    """File text at *sha*, or None when the path is not in that commit."""
    proc = subprocess.run(
        [*_GIT, "-C", repo, "show", f"{sha}:{path}"],
        capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def file_at_head(target: TargetInfo, path: str) -> str | None:
    return _show(target.repo_path, target.head_sha, path)


def lines_at_head(target: TargetInfo, path: str, start: int, end: int) -> list[str] | None:
    """Lines start..end (1-based, inclusive) at head, split on \\n like git numbers them."""
    text = file_at_head(target, path)
    if text is None:
        return None
    return text.split("\n")[start - 1:end]


# --- citations ----------------------------------------------------------------


def citation_gate(target: TargetInfo, citation: Citation) -> GateResult:
    """The quote, whitespace-normalised, is inside lines line_start..line_end at head."""
    quote = _normalise(citation.quote)
    if not quote:
        return GateResult(passed=False, reason="empty quote", action="reject")
    text = file_at_head(target, citation.path)
    if text is None:
        return GateResult(passed=False, reason="path not at head", action="reject")
    window = _normalise("\n".join(text.split("\n")[citation.line_start - 1:citation.line_end]))
    if quote not in window:
        return GateResult(passed=False, reason="quote not at cited lines", action="reject")
    return GateResult(passed=True)


def finding_gate(target: TargetInfo, finding: Finding) -> GateResult:
    """Every citation reproduces, the file is in the diff (unless pre-existing), severity is known."""
    if finding.severity not in SEVERITIES:
        return GateResult(passed=False, reason=f"unknown severity {finding.severity!r}", action="reject")
    if finding.dimension != "pre-existing" and finding.file not in target.files:
        return GateResult(passed=False, reason=f"{finding.file} is not a changed file", action="reject")
    if not finding.citations:
        return GateResult(passed=False, reason="no citations", action="reject")
    for i, citation in enumerate(finding.citations):
        result = citation_gate(target, citation)
        if not result.passed:
            return GateResult(
                passed=False, action="reject",
                reason=f"citation {i} ({citation.path}:{citation.line_start}-{citation.line_end}): {result.reason}",
            )
    return GateResult(passed=True)


def verdict_gate(target: TargetInfo, verdict: Verdict) -> GateResult:
    """A `confirmed` verdict must show at least one citation that reproduces.

    On failure the caller downgrades the verdict to `unverifiable`.
    """
    if verdict.status != "confirmed":
        return GateResult(passed=True)
    if not verdict.citations:
        return GateResult(passed=False, action="downgrade",
                          reason="verifier confirmed without citing what it read")
    reasons = []
    for citation in verdict.citations:
        result = citation_gate(target, citation)
        if result.passed:
            return GateResult(passed=True)
        reasons.append(f"{citation.path}:{citation.line_start}: {result.reason}")
    return GateResult(passed=False, action="downgrade",
                      reason="verifier confirmed, but none of its citations reproduce (" + "; ".join(reasons) + ")")


# --- premises -----------------------------------------------------------------


def premise_gate(target: TargetInfo, premise: Premise) -> GateResult:
    """`in_repo` needs a citation that reproduces; `external` must carry none."""
    if premise.kind == "external":
        if premise.citation is not None:
            return GateResult(passed=False, action="reask",
                              reason="an external premise must not carry a citation")
        return GateResult(passed=True)
    if premise.citation is None:
        return GateResult(passed=False, action="reask",
                          reason="an in-repo premise must cite the lines that establish it")
    result = citation_gate(target, premise.citation)
    if not result.passed:
        return GateResult(passed=False, action="reask", reason=f"premise citation: {result.reason}")
    return GateResult(passed=True)


@functools.lru_cache(maxsize=256)
def _grep(repo: str, sha: str, symbol: str) -> tuple[tuple[str, int, str], ...]:
    """(path, line, text) for every whole-word hit of *symbol* at *sha*."""
    proc = subprocess.run(
        [*_GIT, "-C", repo, "grep", "-n", "-w", "--null", "-I", "-F", "-e", symbol, sha],
        capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    hits = []
    prefix = f"{sha}:"
    for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = raw.split("\0", 2)
        if len(parts) != 3:
            continue
        path, lineno, text = parts
        if path.startswith(prefix):
            path = path[len(prefix):]
        try:
            hits.append((path, int(lineno), text))
        except ValueError:
            continue
    return tuple(hits)


def is_definition(symbol: str, line: str) -> bool:
    """A line that defines *symbol* rather than using it."""
    s = re.escape(symbol)
    if re.match(rf"^\s*{s}\s*[:=]", line):
        return True
    return bool(re.search(rf"""(?:config|getenv)\(\s*['"]{s}['"]""", line))


def symbol_hits(target: TargetInfo, symbol: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(definitions, uses) of *symbol* at head, each as (path, line)."""
    definitions, uses = [], []
    for path, lineno, text in _grep(target.repo_path, target.head_sha, symbol):
        (definitions if is_definition(symbol, text) else uses).append((path, lineno))
    return definitions, uses


def premise_symbols(target: TargetInfo, premise: Premise) -> list[str]:
    """The symbols the consumer gate applies to: settings keys, constants, env vars.

    Those are UPPER_CASE (`SYMBOL_RE`). An explicit `premise.symbol` in any
    other shape — a function, a variable, a field — is not one: a premise
    about what a function does rightly cites its definition, and in the
    2026-09-25 acceptance run every one of 16 consumer-gate rejections was
    such a name. Symbols pulled out of the statement apply only when the repo
    defines them: an all-caps word like `JSON` that nothing defines is not a
    settings key either.
    """
    explicit = (premise.symbol or "").strip()
    if explicit:
        return [explicit] if SYMBOL_RE.fullmatch(explicit) else []
    found = []
    for symbol in dict.fromkeys(SYMBOL_RE.findall(premise.statement)):
        definitions, _ = symbol_hits(target, symbol)
        if definitions:
            found.append(symbol)
    return found


def symbol_consumer_gate(target: TargetInfo, premise: Premise) -> GateResult:
    """A premise naming a settings key / constant / env var must cite a line that *uses* it.

    Checks provenance, not truth: citing a consumer shows the premise was
    grounded in how the value is used. Whether the premise is right is the
    check_fix stage's question.
    """
    if premise.kind == "external":
        return GateResult(passed=True, reason="external premise")
    symbols = premise_symbols(target, premise)
    if not symbols:
        return GateResult(passed=True, reason="no symbol")
    citation = premise.citation
    for symbol in symbols:
        definitions, uses = symbol_hits(target, symbol)
        consumers = ", ".join(f"{p}:{n}" for p, n in uses[:MAX_CONSUMERS_IN_REASON]) or "none found"
        if citation is None:
            return GateResult(passed=False, action="reask",
                              reason=f"premise names {symbol} but cites nothing; consumers: {consumers}")
        in_range = lambda hit: hit[0] == citation.path and citation.line_start <= hit[1] <= citation.line_end  # noqa: E731
        if any(in_range(u) for u in uses):
            continue
        if any(in_range(d) for d in definitions):
            reason = f"premise cites the definition of {symbol}, not a consumer; consumers: {consumers}"
        else:
            reason = f"premise names {symbol}, but its citation is not a line that uses it; consumers: {consumers}"
        return GateResult(passed=False, action="reask", reason=reason)
    return GateResult(passed=True)


def fix_gate(target: TargetInfo, fix: Fix) -> GateResult:
    """Every premise passes `premise_gate` and `symbol_consumer_gate`.

    A fix with no premises depends on nothing it has shown, so it fails too.
    The reason names the premise index so the re-ask can quote it back.
    """
    if not fix.premises:
        return GateResult(passed=False, action="reask",
                          reason="the fix lists no premises; every fact it depends on is a premise")
    for i, premise in enumerate(fix.premises):
        for gate in (premise_gate, symbol_consumer_gate):
            result = gate(target, premise)
            if not result.passed:
                return GateResult(passed=False, action="reask",
                                  reason=f"premise {i} ({premise.statement[:120]!r}): {result.reason}")
    return GateResult(passed=True)
