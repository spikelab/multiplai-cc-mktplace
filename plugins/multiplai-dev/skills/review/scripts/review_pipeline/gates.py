"""Pure-code gates. None of them calls a model.

Each gate takes the `TargetInfo` and the object it judges and returns a
`GateResult`. Everything they check is re-read from the target's head commit
with `git show`, never taken from what an agent said it read.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import logging
import re

from multiplai_core.citations import check_quote, file_at, lines_at

from .models import (
    DEPS_DIR,
    SEVERITIES,
    Citation,
    EarlierStatus,
    Finding,
    GateResult,
    Need,
    TargetInfo,
    Verdict,
)

log = logging.getLogger(__name__)


# The gate reasons that may reach activity.jsonl and checks.json. A raw reason
# can quote a citation; the log and the record name only which rule fired.
_REASON_KINDS = (
    "quote not at cited lines",
    "path not at head",
    "empty quote",
    "is not a changed file",
    "unknown severity",
    "no citations",
    "first citation is a web source",
    "confirmed without citing",
    "none of its citations reproduce",
    "dependency not in manifest",
    "first citation is a dependency",
    "path not at commit",
    "status without citing",
    "not a path:line location",
    "line past the end of the file",
)


def reason_kind(reason: str) -> str:
    for kind in _REASON_KINDS:
        if kind in reason:
            return kind
    return "other"


def file_at_head(target: TargetInfo, path: str) -> str | None:
    """File text at head, or None when the path is not in that commit."""
    return file_at(target.repo_path, target.head_sha, path)


def lines_at_head(
    target: TargetInfo, path: str, start: int, end: int
) -> list[str] | None:
    """Lines start..end (1-based, inclusive) at head, split on \\n like git numbers them."""
    return lines_at(target.repo_path, target.head_sha, path, start, end)


# --- citations ----------------------------------------------------------------


def _dependency_citation_gate(target: TargetInfo, citation: Citation) -> GateResult:
    """A citation under `.review-deps/<name>@<commit12>/`, re-read from that dependency's clone at its commit."""
    folder, _, inner = citation.path[len(DEPS_DIR) + 1 :].partition("/")
    entry = next((e for e in target.deps if e.folder == folder), None)
    if entry is None or not inner:
        return GateResult(
            passed=False, reason="dependency not in manifest", action="reject"
        )
    reason = check_quote(
        entry.cache,
        entry.commit,
        inner,
        citation.line_start,
        citation.line_end,
        citation.quote,
    )
    return (
        GateResult(passed=True)
        if reason is None
        else GateResult(passed=False, reason=reason, action="reject")
    )


def citation_gate(target: TargetInfo, citation: Citation) -> GateResult:
    """The quote, whitespace-normalised, is inside lines line_start..line_end at head.

    A dependency citation is checked against that dependency's pinned commit instead.
    """
    if citation.is_dependency:
        return _dependency_citation_gate(target, citation)
    reason = check_quote(
        target.repo_path,
        target.head_sha,
        citation.path,
        citation.line_start,
        citation.line_end,
        citation.quote,
    )
    if reason is None:
        return GateResult(passed=True)
    # Core says "path not at commit"; review's reason kinds and checks.json name it "path not at head".
    if reason == "path not at commit":
        reason = "path not at head"
    return GateResult(passed=False, reason=reason, action="reject")


def finding_gate(target: TargetInfo, finding: Finding) -> GateResult:
    """Every repo citation reproduces, the file is in the diff (unless pre-existing), severity is known.

    A web citation (an http(s) URL as `path`) is not checked: the gates only
    read git. It may follow a repo citation, never stand first or alone, so a
    finding is always anchored to the reviewed commit. A dependency citation
    is checked, and follows the same rule.
    """
    if finding.severity not in SEVERITIES:
        return GateResult(
            passed=False,
            reason=f"unknown severity {finding.severity!r}",
            action="reject",
        )
    if not finding.pre_existing and finding.file not in target.files:
        return GateResult(
            passed=False,
            reason=f"{finding.file} is not a changed file",
            action="reject",
        )
    if not finding.citations:
        return GateResult(passed=False, reason="no citations", action="reject")
    if finding.citations[0].is_web:
        return GateResult(
            passed=False,
            reason="first citation is a web source, not a file at head",
            action="reject",
        )
    if finding.citations[0].is_dependency:
        return GateResult(
            passed=False, reason="first citation is a dependency", action="reject"
        )
    for i, citation in enumerate(finding.citations):
        if citation.is_web:
            continue
        result = citation_gate(target, citation)
        if not result.passed:
            return GateResult(
                passed=False,
                action="reject",
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
        return GateResult(
            passed=False,
            action="downgrade",
            reason="verifier confirmed without citing what it read",
        )
    reasons = []
    for citation in verdict.citations:
        if citation.is_web:
            reasons.append(f"{citation.path}: a web source does not confirm a finding")
            continue
        result = citation_gate(target, citation)
        if result.passed:
            return GateResult(passed=True)
        reasons.append(f"{citation.path}:{citation.line_start}: {result.reason}")
    return GateResult(
        passed=False,
        action="downgrade",
        reason="verifier confirmed, but none of its citations reproduce ("
        + "; ".join(reasons)
        + ")",
    )


def earlier_status_gate(target: TargetInfo, status: EarlierStatus) -> GateResult:
    """Any status but `cannot_verify` must show at least one repository citation that reproduces at head.

    Web and dependency citations do not count. On failure the caller records `cannot_verify`.
    """
    if status.status == "cannot_verify":
        return GateResult(passed=True)
    repo = [c for c in status.citations if not c.is_web and not c.is_dependency]
    if not repo:
        return GateResult(
            passed=False,
            action="downgrade",
            reason=f"{status.status}: status without citing a line of the repository at head",
        )
    reasons = []
    for citation in repo:
        result = citation_gate(target, citation)
        if result.passed:
            return GateResult(passed=True)
        reasons.append(f"{citation.path}:{citation.line_start}: {result.reason}")
    return GateResult(
        passed=False,
        action="downgrade",
        reason=f"{status.status}: none of its citations reproduce ("
        + "; ".join(reasons)
        + ")",
    )


# --- other locations ----------------------------------------------------------

# `path:line` or `path:start-end`; the path is everything before the last colon-number part.
_LOCATION_RE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def locations_gate(target: TargetInfo, location: str) -> GateResult:
    """One of a finding's `other_locations` names a file at head and lines inside it.

    A web URL, a dependency path or a location without a line is not at head,
    so it fails too. On failure the caller drops the location and keeps the finding.
    """
    m = _LOCATION_RE.match(location.strip())
    if m is None:
        return GateResult(
            passed=False,
            reason=f"{location!r} is not a path:line location",
            action="drop",
        )
    path, start = m.group("path"), int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1 or end < start:
        return GateResult(
            passed=False,
            reason=f"{location!r} is not a path:line location",
            action="drop",
        )
    text = file_at_head(target, path)
    if text is None:
        return GateResult(
            passed=False, reason=f"{location!r}: path not at head", action="drop"
        )
    # Split on \n as git numbers lines; the empty string after a final newline is not a line.
    count = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    if end > count:
        return GateResult(
            passed=False,
            action="drop",
            reason=f"{location!r}: line past the end of the file ({count} lines)",
        )
    return GateResult(passed=True)


def gated_locations(target: TargetInfo, finding: Finding) -> tuple[Finding, list[str]]:
    """*finding* with each location `locations_gate` fails dropped, and one reason per dropped location."""
    kept, dropped = [], []
    for location in finding.other_locations:
        result = locations_gate(target, location)
        if result.passed:
            kept.append(location)
        else:
            dropped.append(result.reason)
    if not dropped:
        return finding, []
    return finding.model_copy(update={"other_locations": kept}), dropped


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
    "kubectl": frozenset(
        {"get", "describe", "logs", "top", "explain", "version", "api-resources"}
    ),
    "aws": frozenset({"ls"}),  # plus every describe-*, list-* and get-* operation
    "az": frozenset({"show", "list"}),
    "terraform": frozenset({"show", "output", "providers", "version", "list", "pull"}),
    "curl": frozenset(),  # checked by its flags instead
    "pip": frozenset({"show", "list", "freeze", "index", "download"}),
    "npm": frozenset(
        {"view", "info", "show", "ls", "list", "outdated", "explain", "why", "search"}
    ),
    "uv": frozenset(
        {"tree", "show", "list", "freeze"}
    ),  # `uv version X` would set the version
    "git": frozenset(
        {
            "log",
            "show",
            "diff",
            "status",
            "ls-remote",
            "ls-files",
            "ls-tree",
            "rev-parse",
            "blame",
            "cat-file",
            "describe",
            "shortlog",
            "grep",
            "rev-list",
            "merge-base",
        }
    ),
    "psql": frozenset(),  # checked by its SQL instead
    "mysql": frozenset(),
}
NEED_CLIS = frozenset(NEED_READ_VERBS)
# Flags that take a value, so the word after them is not a verb.
_VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "-R",
        "--repo",
        "--project",
        "--region",
        "--profile",
        "-n",
        "--namespace",
        "--context",
        "-g",
        "--resource-group",
        "--subscription",
        "-o",
        "--output",
        "--format",
    }
)
_SQL_WRITES = re.compile(
    r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|copy|call)\b",
    re.IGNORECASE,
)
_CURL_WRITES = frozenset(
    {
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--json",
        "-F",
        "--form",
        "-T",
        "--upload-file",
        "-o",
        "--output",
        "-O",
        "--remote-name",
    }
)
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
            return word[len(short) :].upper()
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
    if cli in ("psql", "mysql", "bq") and (
        m := _SQL_WRITES.search(" ".join(words[1:]))
    ):
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
        return (
            ""
            if any(w in verbs for w in rest)
            else f"{cli} without one of {', '.join(sorted(verbs))}"
        )
    if cli == "gh":
        rest = rest[1:]  # the group: `gh pr view`, `gh run list`
    if (cli, rest[:1]) in (("uv", ["pip"]), ("terraform", ["state"])):
        rest = rest[1:]
    verb = rest[0] if rest else ""
    return (
        "" if verb in verbs else f"{cli} {verb or '(no verb)'} is not a read-only form"
    )


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
        return GateResult(
            passed=False, reason="command is more than one line", action="blank"
        )
    if len(command) >= NEED_COMMAND_MAX_CHARS:
        return GateResult(
            passed=False,
            reason=f"command is {NEED_COMMAND_MAX_CHARS} characters or more",
            action="blank",
        )
    for token in _NEED_FORBIDDEN:
        if token in command:
            return GateResult(
                passed=False, reason=f"command contains {token!r}", action="blank"
            )
    words = command.split()
    if words[0] not in NEED_CLIS:
        return GateResult(
            passed=False,
            reason=f"{words[0]!r} is not a known read-only CLI",
            action="blank",
        )
    if why := _not_read_only(words):
        return GateResult(passed=False, reason=why, action="blank")
    return GateResult(passed=True)


def gated_need(need: Need) -> Need:
    """*need* with its command stripped, or blanked when `need_gate` fails. Never dropped."""
    need = need.model_copy(
        update={
            "command": need.command.strip(),
            "what": need.what.strip(),
            "where": need.where.strip(),
        }
    )
    result = need_gate(need)
    if not result.passed:
        log.info("need_gate blanked a command: %s", result.reason)
        return need.model_copy(update={"command": ""})
    return need


# --- the write stage's plain-language text --------------------------------------

WRITE_TITLE_MAX_WORDS = 12
WRITE_SENTENCE_MAX_WORDS = 50
# Phrases that announce a point instead of making it (Clear Writing rule 7).
WRITE_BANNED = (
    "the key insight",
    "crucially",
    "worth noting",
    "importantly",
    "the mechanism behind",
)
# `path:line`: a name with a slash or a file extension, then a colon and a number.
_PATH_LINE_RE = re.compile(r"[\w./-]*(?:/|\.[A-Za-z][A-Za-z0-9]*)[\w./-]*:\d+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_BACKTICKED_RE = re.compile(r"`([^`\n]+)`")


def write_gate(title: str, what_happens: str, material: list[str]) -> list[str]:
    """Why a write answer fails, one reason per broken rule; [] when it passes.

    *material* is the text the writer was given about the finding (claim, failure scenario,
    expected behaviour, the verifier's reason and every cited quote): a backticked identifier
    must occur in it.
    """
    reasons = []
    words = len(title.split())
    if not title.strip() or not what_happens.strip():
        reasons.append("empty title or what_happens")
    if words > WRITE_TITLE_MAX_WORDS:
        reasons.append(f"title has {words} words, over {WRITE_TITLE_MAX_WORDS}")
    both = f"{title}\n{what_happens}"
    if _PATH_LINE_RE.search(both):
        reasons.append("path:line in the text")
    long = [
        s
        for s in _SENTENCE_END_RE.split(what_happens.strip())
        if len(s.split()) > WRITE_SENTENCE_MAX_WORDS
    ]
    if long:
        reasons.append(f"a sentence over {WRITE_SENTENCE_MAX_WORDS} words")
    lowered = both.lower()
    banned = [p for p in WRITE_BANNED if p in lowered]
    if banned:
        reasons.append(f"announces instead of saying: {', '.join(banned)}")
    corpus = "\n".join(material)
    unknown = sorted({b for b in _BACKTICKED_RE.findall(both) if b not in corpus})
    if unknown:
        reasons.append(f"backticked names not in the material: {', '.join(unknown)}")
    return reasons
