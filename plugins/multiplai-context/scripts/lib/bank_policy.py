"""``BANK.md`` — what a bank declares about itself, and the checks it buys.

Every bank carries a ``BANK.md`` at its root declaring who owns it, how
contributions are reviewed, and — the part that is machine-enforced — which
**domains must never be written into it**. Names, compensation, health,
finances: the categories a household or a team bank is most likely to leak by
accident, because the leak looks like an ordinary useful fact.

Two checks run on anything about to leave this machine for a bank:

* **No-go domains** (:func:`check_item`) — declared by the bank itself, so the
  bank's owners set the boundary rather than each contributor's client.
* **Secrets** (:func:`find_secrets`) — high-signal credential shapes only, and
  it is a *blocker*, never a redactor. Redaction-by-regex fails open: a filter
  that rewrites what it recognises quietly passes what it does not, and the
  contributor believes they are protected. Refusing to open the PR at all puts
  a human on the decision.

Neither check is the safety story on its own. The safety story is three layers:
the author reads the proposal, this file refuses the obvious leaks, and a
reviewer on the bank repo reads the pull request. What this file adds is that
the *cheapest* mistakes stop before a human's attention is spent on them.

The domain check is a **keyword** check and it is stated as one: it cannot
understand a sentence, and a bank owner who writes ``no-go: everything
sensitive`` has written a comment, not a rule. That limitation is the reason
the PR gate exists and is not softened by pretending this is smarter than it is.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

__all__ = [
    "BankPolicy",
    "DEFAULT_NO_GO",
    "check_item",
    "check_text",
    "find_secrets",
    "load_policy",
    "parse_policy",
]

#: Applied when a bank ships no ``BANK.md``, or ships one that declares no
#: no-go list. Fail-closed: the categories most likely to be leaked by accident
#: are refused by default, and a bank that genuinely wants one of them has to
#: say so in a file its own reviewers can see.
DEFAULT_NO_GO: tuple[str, ...] = (
    "salary",
    "compensation",
    "comp band",
    "equity grant",
    "bonus",
    "medical",
    "health condition",
    "diagnosis",
    "prescription",
    "therapy",
    "bank account",
    "iban",
    "credit card",
    "tax id",
    "social security",
    "passport",
    "home address",
    "date of birth",
    "password",
    "api key",
    "access token",
)

_NO_GO_HEADINGS = ("no-go", "no go", "never share", "do not share", "off-limits")

_OWNER_HEADINGS = ("owner", "owners", "maintainer", "maintainers")

_REVIEW_HEADINGS = ("review", "reviews", "review rules", "contributing")

# Credential shapes with essentially no false-positive rate. Deliberately a
# short list of *specific* prefixes rather than an entropy heuristic: a
# high-entropy string in a memory file is usually a commit sha or a UUID, and a
# check that cried wolf on those would be turned off within a week.
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("github personal access token", r"gh[pousr]_[A-Za-z0-9]{16,}"),
    ("github fine-grained token", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("anthropic api key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai api key", r"sk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("aws access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google api key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("slack token", r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("url with inline credentials", r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
)

_COMPILED_SECRETS = tuple((label, re.compile(p)) for label, p in _SECRET_PATTERNS)


@dataclasses.dataclass(frozen=True)
class BankPolicy:
    """What a bank's ``BANK.md`` declares."""

    bank: str
    owners: tuple[str, ...] = ()
    review: str = ""
    no_go: tuple[str, ...] = DEFAULT_NO_GO
    declared: bool = False

    @property
    def summary(self) -> str:
        owners = ", ".join(self.owners) if self.owners else "not declared"
        source = "BANK.md" if self.declared else "defaults (no BANK.md found)"
        return (
            f"bank `{self.bank}` — owners: {owners}; "
            f"{len(self.no_go)} no-go term(s), from {source}"
        )


def _bullets(lines: Sequence[str]) -> list[str]:
    """Bullet payloads from a block of markdown lines."""
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ", "+ ")):
            out.append(line[2:].strip())
        elif re.match(r"^\d+[.)]\s+", line):
            out.append(re.sub(r"^\d+[.)]\s+", "", line).strip())
    return [x for x in (b.strip("`*_ ").strip() for b in out) if x]


def _sections(text: str) -> dict[str, list[str]]:
    """Heading (lowercased) → its lines, for every ATX heading in *text*."""
    out: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", raw)
        if m:
            current = m.group(1).strip().lower()
            out.setdefault(current, [])
            continue
        if current is not None:
            out[current].append(raw)
    return out


def _match_section(sections: dict[str, list[str]], needles: Iterable[str]) -> list[str]:
    for heading, lines in sections.items():
        if any(n in heading for n in needles):
            return lines
    return []


def parse_policy(text: str, *, bank: str) -> BankPolicy:
    """Parse a ``BANK.md`` body.

    Tolerant on purpose: a bank whose ``BANK.md`` has an unexpected shape gets
    the **defaults**, not an exception and not an empty no-go list. The failure
    mode of a policy file is "the boundary is wider than intended", so an
    unparsed section must never be read as "no restrictions".
    """
    sections = _sections(text or "")
    owners = tuple(_bullets(_match_section(sections, _OWNER_HEADINGS)))
    review_lines = _match_section(sections, _REVIEW_HEADINGS)
    review = " ".join(x.strip() for x in review_lines if x.strip())
    no_go_terms = tuple(
        t.lower() for t in _bullets(_match_section(sections, _NO_GO_HEADINGS))
    )
    return BankPolicy(
        bank=bank,
        owners=owners,
        review=review.strip(),
        no_go=no_go_terms or DEFAULT_NO_GO,
        declared=bool(text and text.strip()),
    )


def load_policy(bank_path: Path, *, bank: str) -> BankPolicy:
    """The bank's declared policy, or the defaults when it declares none."""
    candidate = Path(bank_path) / "BANK.md"
    try:
        return parse_policy(candidate.read_text(encoding="utf-8"), bank=bank)
    except (OSError, UnicodeDecodeError):
        return BankPolicy(bank=bank)


def find_secrets(text: str) -> list[str]:
    """Names of the credential shapes found in *text*.

    Returns the **labels only** — never the matched value. A blocker that
    printed the secret it found would put it in the transcript, which is the
    thing the block exists to prevent.
    """
    found: list[str] = []
    for label, pattern in _COMPILED_SECRETS:
        if pattern.search(text or ""):
            found.append(label)
    return found


def check_text(text: str, policy: BankPolicy) -> list[str]:
    """Blocking reasons for putting *text* into *policy*'s bank. Empty = clear."""
    reasons: list[str] = []
    lowered = (text or "").lower()
    hits = sorted({term for term in policy.no_go if term and term in lowered})
    if hits:
        reasons.append(
            "names a no-go domain declared by the bank: " + ", ".join(hits)
        )
    for label in find_secrets(text):
        reasons.append(f"contains what looks like a {label}")
    return reasons


def check_item(item, policy: BankPolicy) -> list[str]:
    """Blocking reasons for contributing *item* to *policy*'s bank.

    Checks the item's text, title, section **and target filename** — every part
    of it that ends up in the pull request, and therefore every part that is
    exactly as public as the body once the PR is open. The filename was the gap:
    ``team/salary-2026.md`` with a body of "Spike: 180000 EUR base" cleared every
    no-go term, because nothing scanned the name of the file being written.
    """
    parts = [
        getattr(item, "text", "") or "",
        getattr(item, "title", "") or "",
        getattr(item, "section", "") or "",
        getattr(item, "target", "") or "",
    ]
    return check_text("\n".join(parts), policy)
