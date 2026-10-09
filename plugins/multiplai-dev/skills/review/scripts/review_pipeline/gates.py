"""Pure-code gates. None of them calls a model.

Each gate takes the `TargetInfo` and the object it judges and returns a
`GateResult`. Everything they check is re-read from the target's head commit
with `git show`, never taken from what an agent said it read.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import functools
import logging
import re
import subprocess

from .models import SEVERITIES, Citation, Finding, GateResult, Need, TargetInfo, Verdict
from .target import _GIT, _env

log = logging.getLogger(__name__)


# The gate reasons that may reach activity.jsonl and checks.json. A raw reason
# can quote a citation; the log and the record name only which rule fired.
_REASON_KINDS = (
    "quote not at cited lines", "path not at head", "empty quote", "is not a changed file",
    "unknown severity", "no citations", "first citation is a web source", "confirmed without citing",
    "none of its citations reproduce",
)


def reason_kind(reason: str) -> str:
    for kind in _REASON_KINDS:
        if kind in reason:
            return kind
    return "other"


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
    """Every repo citation reproduces, the file is in the diff (unless pre-existing), severity is known.

    A web citation (an http(s) URL as `path`) is not checked: the gates only
    read git. It may follow a repo citation, never stand first or alone, so a
    finding is always anchored to the reviewed commit.
    """
    if finding.severity not in SEVERITIES:
        return GateResult(passed=False, reason=f"unknown severity {finding.severity!r}", action="reject")
    if finding.dimension != "pre-existing" and finding.file not in target.files:
        return GateResult(passed=False, reason=f"{finding.file} is not a changed file", action="reject")
    if not finding.citations:
        return GateResult(passed=False, reason="no citations", action="reject")
    if finding.citations[0].is_web:
        return GateResult(passed=False, reason="first citation is a web source, not a file at head", action="reject")
    for i, citation in enumerate(finding.citations):
        if citation.is_web:
            continue
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
        if citation.is_web:
            reasons.append(f"{citation.path}: a web source does not confirm a finding")
            continue
        result = citation_gate(target, citation)
        if result.passed:
            return GateResult(passed=True)
        reasons.append(f"{citation.path}:{citation.line_start}: {result.reason}")
    return GateResult(passed=False, action="downgrade",
                      reason="verifier confirmed, but none of its citations reproduce (" + "; ".join(reasons) + ")")


# --- needs --------------------------------------------------------------------

NEED_COMMAND_MAX_CHARS = 300
# Shell syntax that chains, pipes, redirects or substitutes. `&` covers `&&`
# and a command sent to the background; `|` covers `||`.
_NEED_FORBIDDEN = (";", "&", "|", ">", "<", "`", "$(")
# Each CLI a need's command may start with, and the verbs that only read. A
# command passes only when its verb is listed here; anything else could change
# state, and a person would be shown it as a read-only command to copy.
NEED_READ_VERBS: dict[str, frozenset[str]] = {
    "gh": frozenset({"api", "view", "list", "status", "diff", "checks"}),
    "gcloud": frozenset({"describe", "list", "read", "get-iam-policy", "get-value"}),
    "bq": frozenset({"show", "ls", "head", "query"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "version", "api-resources"}),
    "aws": frozenset({"ls"}),  # plus every describe-*, list-* and get-* operation
    "az": frozenset({"show", "list"}),
    "terraform": frozenset({"show", "output", "providers", "version", "list", "pull"}),
    "curl": frozenset(),  # checked by its flags instead
    "pip": frozenset({"show", "list", "freeze", "index", "download"}),
    "npm": frozenset({"view", "info", "show", "ls", "list", "outdated", "explain", "why", "search"}),
    "uv": frozenset({"tree", "show", "list", "freeze"}),  # `uv version X` would set the version
    "git": frozenset({"log", "show", "diff", "status", "ls-remote", "ls-files", "ls-tree", "rev-parse",
                      "blame", "cat-file", "describe", "shortlog", "grep", "rev-list", "merge-base"}),
    "psql": frozenset(),  # checked by its SQL instead
    "mysql": frozenset(),
}
NEED_CLIS = frozenset(NEED_READ_VERBS)
# Flags that take a value, so the word after them is not a verb.
_VALUE_FLAGS = frozenset({"-C", "-c", "-R", "--repo", "--project", "--region", "--profile", "-n",
                          "--namespace", "--context", "-g", "--resource-group", "--subscription",
                          "-o", "--output", "--format"})
_SQL_WRITES = re.compile(r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|copy|call)\b",
                         re.IGNORECASE)
_CURL_WRITES = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F",
                          "--form", "-T", "--upload-file", "-o", "--output", "-O", "--remote-name"})
_GH_API_WRITES = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})


def _positionals(words: list[str]) -> list[str]:
    """The words after the CLI that are neither flags nor the values of `_VALUE_FLAGS`."""
    out, skip = [], False
    for word in words[1:]:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in _VALUE_FLAGS
        else:
            out.append(word)
    return out


def _method(words: list[str], short: str, long: str) -> str | None:
    """The value of a request-method flag (`-X POST`, `-XPOST`, `--method=POST`), upper-cased, or None."""
    for i, word in enumerate(words):
        if word in (short, long):
            return words[i + 1].upper() if i + 1 < len(words) else ""
        if word.startswith(f"{long}="):
            return word.split("=", 1)[1].upper()
        if word.startswith(short) and len(word) > len(short):
            return word[len(short):].upper()
    return None


def _flag(words: list[str], flags: frozenset[str]) -> str:
    return next((w.split("=", 1)[0] for w in words if w.split("=", 1)[0] in flags), "")


def _not_read_only(words: list[str]) -> str:
    """Why the command *words* could change state, or "" when it only reads."""
    cli, rest = words[0], _positionals(words)
    if cli == "curl":
        method = _method(words, "-X", "--request")
        if method not in (None, "GET", "HEAD"):
            return f"curl with method {method or '(none)'} can change state"
        if hit := _flag(words, _CURL_WRITES):
            return f"curl with {hit} sends or writes data"
        return ""
    if cli in ("psql", "mysql", "bq") and (m := _SQL_WRITES.search(" ".join(words[1:]))):
        return f"{cli} with {m.group(1).upper()} can change state"
    if cli in ("psql", "mysql"):
        return ""
    if cli == "gh" and rest[:1] == ["auth"]:
        return "gh auth can print a token"
    if cli == "gh" and rest[:1] == ["api"]:
        method = _method(words, "-X", "--method")
        if method not in (None, "GET"):
            return f"gh api with method {method or '(none)'} can change state"
        if hit := _flag(words, _GH_API_WRITES):
            return f"gh api with {hit} sends a request body"
        return ""
    verbs = NEED_READ_VERBS[cli]
    if cli == "aws":
        op = rest[1] if len(rest) > 1 else ""
        if op in verbs or op.startswith(("describe-", "list-", "get-")):
            return ""
        return f"aws {op or '(no operation)'} is not a describe, list or get operation"
    if cli in ("gcloud", "az"):
        # The verb comes after the resource groups: `gcloud run services describe api`.
        return "" if any(w in verbs for w in rest) else f"{cli} without one of {', '.join(sorted(verbs))}"
    if cli == "gh":
        rest = rest[1:]  # the group: `gh pr view`, `gh run list`
    if (cli, rest[:1]) in (("uv", ["pip"]), ("terraform", ["state"])):
        rest = rest[1:]
    verb = rest[0] if rest else ""
    return "" if verb in verbs else f"{cli} {verb or '(no verb)'} is not a read-only form"


def need_gate(need: Need) -> GateResult:
    """The need's command is one short line, chains nothing, and is a read-only form of a known CLI.

    Not a security boundary: the session never runs these commands itself. It
    keeps each ask readable and safe to copy. On failure the caller blanks the
    command and keeps the need.
    """
    command = need.command.strip()
    if not command:
        return GateResult(passed=True)
    if "\n" in command or "\r" in command:
        return GateResult(passed=False, reason="command is more than one line", action="blank")
    if len(command) >= NEED_COMMAND_MAX_CHARS:
        return GateResult(passed=False, reason=f"command is {NEED_COMMAND_MAX_CHARS} characters or more",
                          action="blank")
    for token in _NEED_FORBIDDEN:
        if token in command:
            return GateResult(passed=False, reason=f"command contains {token!r}", action="blank")
    words = command.split()
    if words[0] not in NEED_CLIS:
        return GateResult(passed=False, reason=f"{words[0]!r} is not a known read-only CLI", action="blank")
    if why := _not_read_only(words):
        return GateResult(passed=False, reason=why, action="blank")
    return GateResult(passed=True)


def gated_need(need: Need) -> Need:
    """*need* with its command stripped, or blanked when `need_gate` fails. Never dropped."""
    need = need.model_copy(update={"command": need.command.strip(), "what": need.what.strip()})
    result = need_gate(need)
    if not result.passed:
        log.info("need_gate blanked a command: %s", result.reason)
        return need.model_copy(update={"command": ""})
    return need
