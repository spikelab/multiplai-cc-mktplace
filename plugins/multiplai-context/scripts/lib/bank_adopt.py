"""Adoption — the migration that makes "a fact lives in exactly one bank" real.

Subscribing to a bank does not merge two corpora; it makes the bank
**authoritative** for the domains it declares. So joining one means moving your
overlapping content into it and **deleting your local copy**. That deletion is
the awkward step, it is the step a user left to their own devices will skip,
and skipping it is exactly the "I inject twice the stuff" problem banks exist
to solve. Hence a first-class command rather than a paragraph of instructions.

## Two phases, and why it is not one

``propose`` opens a pull request moving the content into the bank.
``finalize`` deletes the local copy.

They are separate because between opening a PR and having it merged, the
content is *not yet* in the bank — a one-shot "move" would blind the user's
routing to that file for however long review takes. So ``finalize`` re-reads
the bank's working tree and enforces one invariant, which is the safety
property of this whole module:

    **Nothing is deleted from personal memory that is not already present,
    line for line, in the bank's working tree.**

Not "a PR was opened". Not "the model thinks it is equivalent". The lines are
there or the file is not deleted. :func:`content_present` is that check, and
:func:`finalize` cannot be made to skip it — there is no flag.

## The other three guards

* **Per-file confirmation.** ``finalize`` acts only on files named explicitly.
  There is deliberately no "adopt everything" — the plan this implements says
  a migration touching more than a handful of files "is a conversation, not a
  command", and the way to make that true is to give the command no way to
  express it.
* **A receipt, and a revert line.** Memory is a git repository. The deletion is
  one commit, the receipt names it, and the receipt carries the exact
  ``git revert`` that undoes it.
* **Dry run by default.** Both phases report and change nothing unless asked
  twice.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from lib.bank_collisions import find_collisions
from lib.bank_git import dirty_paths, head_sha, is_git_repo, run_git, stage_commit
from lib.banks import MemoryBank, configured_banks, split_ref
from lib.memory_write_floor import is_safe_target

logger = logging.getLogger(__name__)

__all__ = [
    "AdoptionCandidate",
    "AdoptionPlan",
    "CONVERSATION_THRESHOLD",
    "content_present",
    "finalize",
    "plan_adoption",
    "render_receipt",
]

#: Above this many candidate files, the command says so loudly. It still
#: refuses to act on anything not named explicitly — this only changes how hard
#: it shouts.
CONVERSATION_THRESHOLD = 5

#: Lines too short or too structural to carry meaning on their own. A file's
#: presence in a bank is judged on its *substance*, not on whether both copies
#: happen to have a blank line in the same place.
_TRIVIAL_RE = re.compile(r"^(?:[-*_=#>\s`|]*|\d+[.)])$")

#: Per-file bookkeeping the memory tooling maintains, not content. A bank will
#: never carry the personal file's freshness header, and reporting it as
#: "missing from the bank" would make every adoption look unsafe for a reason
#: that has nothing to do with whether the content moved.
_METADATA_RE = re.compile(r"^\*\*(?:last updated|source|status)\b", re.IGNORECASE)

#: Leading list/quote markup, stripped before comparing. The same fact can be a
#: bullet in one file and a bare line in another, and that is formatting rather
#: than content — this check ignores formatting and does not attempt semantics.
#: It matters more now that comparison is line **membership** rather than a
#: substring search: with a substring test a leading "- " happened not to matter,
#: because the personal line was found inside the bank's bulleted one.
_LIST_MARKER_RE = re.compile(r"^(?:[-*+>]|\d+[.)])\s+")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _significant_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or _TRIVIAL_RE.match(line) or len(line) < 12:
            continue
        if _METADATA_RE.match(line):
            continue
        out.append(_LIST_MARKER_RE.sub("", line).lower())
    return out


def content_present(personal_text: str, bank_texts: Sequence[str]) -> tuple[bool, list[str]]:
    """Is every substantive line of *personal_text* already in the bank?

    Returns ``(ok, missing_lines)``. The comparison normalises whitespace and
    case and ignores lines too short or too structural to carry meaning; it
    does **not** attempt any semantic equivalence. A paraphrase is a miss, and
    that is the intended strictness: this check is the only thing standing
    between "the PR is open" and "your copy is gone".

    Two things this gets right only because they are stated as rules:

    * **Line membership in a set, not a substring search.** ``line in haystack``
      against the concatenated bank text passed whenever a personal line appeared
      *anywhere inside* a bank line — so a bank could embed the line in a
      sentence that negates it ("It is NOT true that the staging cluster lives in
      eu-west-1 any more.") and the invariant would hold while the bank asserted
      the opposite. The user's own ``contribute`` PR shows the bank owner the
      exact lines to embed. Set membership makes the check mean what the
      docstring says.
    * **A file with nothing substantive to compare is never "present".** Every
      line under 12 characters is skipped, so a file of short bullets has zero
      significant lines and ``missing == []`` — which read as "the bank has all of
      it" against an **empty** bank. Requiring at least one matched line turns
      that from silent deletion into a skip.
    """
    haystack = {
        line for text in bank_texts for line in _significant_lines(text)
    }
    personal_lines = _significant_lines(personal_text)
    if not personal_lines:
        return False, []
    missing = [line for line in personal_lines if line not in haystack]
    return (not missing), missing


@dataclasses.dataclass(frozen=True)
class AdoptionCandidate:
    """One personal file the bank overlaps, and why."""

    filename: str
    bank_refs: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"`{self.filename}` ↔ " + ", ".join(f"`{r}`" for r in self.bank_refs)


@dataclasses.dataclass(frozen=True)
class AdoptionPlan:
    bank: MemoryBank
    candidates: tuple[AdoptionCandidate, ...]
    errors: tuple[str, ...] = ()

    @property
    def is_conversation(self) -> bool:
        """Is this too large to be a command?"""
        return len(self.candidates) > CONVERSATION_THRESHOLD


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def plan_adoption(
    bank: MemoryBank,
    *,
    memory_dir: Path,
    personal_entries: Sequence[dict],
    bank_entries: Sequence[dict],
) -> AdoptionPlan:
    """Which personal files this bank's declared domains overlap.

    Reuses the catalog collision detector rather than inventing a second notion
    of "overlap": a collision *is* the thing adoption resolves, and having two
    definitions of it would let the catalog report an overlap that adopt could
    not act on, or the reverse.
    """
    texts: dict[str, str] = {}
    for entry in list(personal_entries) + list(bank_entries):
        ref = str(entry.get("source") or "")
        if not ref:
            continue
        bank_name, filename, _ = split_ref(ref)
        base = memory_dir if bank_name == "personal" else bank.path
        body = _read(base / filename)
        if body is not None:
            texts[ref] = body

    collisions = find_collisions(
        list(personal_entries) + list(bank_entries), texts=texts
    )
    by_file: dict[str, dict] = {}
    for collision in collisions:
        personal_refs = [r for r in collision.refs if "/" not in r]
        bank_refs = [r for r in collision.refs if r.startswith(f"{bank.name}/")]
        if not personal_refs or not bank_refs:
            continue
        for ref in personal_refs:
            slot = by_file.setdefault(ref, {"refs": set(), "reasons": []})
            slot["refs"].update(bank_refs)
            slot["reasons"].append(f"{collision.kind}: {collision.detail}")

    candidates = tuple(
        AdoptionCandidate(
            filename=name,
            bank_refs=tuple(sorted(slot["refs"])),
            reasons=tuple(slot["reasons"]),
        )
        for name, slot in sorted(by_file.items())
    )
    errors: list[str] = []
    if not bank.path.exists():
        errors.append(f"bank '{bank.name}' is not present at {bank.path} — sync it first")
    return AdoptionPlan(bank=bank, candidates=candidates, errors=tuple(errors))


def finalize(
    bank: MemoryBank,
    filenames: Sequence[str],
    *,
    memory_dir: Path,
    dry_run: bool = True,
) -> dict:
    """Delete the named personal files, but only once the bank truly has them.

    Every file is checked independently and reported independently: a file the
    bank has not merged yet is skipped with its missing lines named, and the
    others still proceed. Partial adoption is the normal state during review
    and must not be an error.

    This function **deletes the user's memory**, so it refuses rather than
    guesses on every input it cannot verify:

    * **The bank must be a real git checkout.** It read ``bank.path.glob("*.md")``
      and never checked the directory existed — only ``plan_adoption`` did, as a
      printed warning ``cmd_adopt`` then ignored. Against a bank that had not been
      cloned yet, or had been moved, ``bank_texts`` was ``[]``, and the receipt
      still said "verified line-for-line against the bank's working tree".
    * **Each filename must be a bare memory filename.** ``memory_dir / filename``
      does not normalise ``..`` and pathlib gives an absolute path total override,
      so ``--file ../victim.md`` and ``--file /etc/hosts`` both resolved outside
      the memory dir and were then ``unlink()``ed.
    * **The bank's working tree must be clean.** Reading working-tree files means
      content that exists only as an uncommitted local edit counted as "the bank
      has it" — so the personal copy was deleted against something no other
      subscriber can see. (The opposite case is already safe: content missing from
      a stale checkout produces ``missing`` and the file is skipped.)
    """
    report: dict = {
        "bank": bank.name,
        "dry_run": dry_run,
        "deleted": [],
        "skipped": [],
        "errors": [],
        "commit": "",
        "revert": "",
    }
    if not filenames:
        report["errors"].append(
            "name the files to adopt explicitly — there is no adopt-everything"
        )
        return report

    # Refusals that apply to the whole call, checked before anything is read.
    # Each of these was a path to deleting memory against nothing.
    if not is_git_repo(bank.path):
        report["errors"].append(
            f"bank '{bank.name}' at {bank.path} is not a git checkout — sync it "
            "first. Nothing was deleted: an absent bank reads as 'the bank has "
            "none of your lines', which is indistinguishable from 'the bank has "
            "all of them' for a file with nothing substantive to compare."
        )
        return report
    dirty = dirty_paths(bank.path)
    if dirty:
        report["errors"].append(
            f"bank '{bank.name}' has uncommitted changes ({', '.join(dirty[:5])}) "
            "— content that exists only as a local edit is not content the bank "
            "has. Commit or stash them first. Nothing was deleted."
        )
        return report

    to_delete: list[str] = []
    for filename in filenames:
        if not is_safe_target(filename):
            report["skipped"].append(
                {"file": filename,
                 "why": "not a bare memory filename (no paths, no '..', must end .md)"}
            )
            continue
        personal = memory_dir / filename
        text = _read(personal)
        if text is None:
            report["skipped"].append(
                {"file": filename, "why": "no such personal memory file"}
            )
            continue
        bank_texts = [
            t for t in (_read(p) for p in sorted(bank.path.glob("*.md"))) if t is not None
        ]
        ok, missing = content_present(text, bank_texts)
        if not ok:
            report["skipped"].append(
                {
                    "file": filename,
                    "why": (
                        f"{len(missing)} line(s) are not in the bank yet — has the "
                        "contribution PR been merged and pulled?"
                    ),
                    "missing": missing[:5],
                }
            )
            continue
        to_delete.append(filename)

    report["deleted"] = list(to_delete)
    if dry_run or not to_delete:
        return report

    for filename in to_delete:
        try:
            (memory_dir / filename).unlink()
        except OSError as e:
            report["errors"].append(f"could not delete {filename}: {e}")

    if is_git_repo(memory_dir):
        committed = stage_commit(
            memory_dir,
            pathspec=to_delete,
            message=(
                f"memory: adopt {len(to_delete)} file(s) into shared bank "
                f"'{bank.name}'"
            ),
        )
        if committed.ok:
            report["commit"] = head_sha(memory_dir)
            report["revert"] = f"git -C {memory_dir} revert {report['commit']}"
        else:
            report["errors"].append(f"commit failed: {committed.detail}")
    else:
        report["errors"].append(
            f"{memory_dir} is not a git repository — the deletion is NOT "
            "revertable; restore from your own backup if this was wrong"
        )
    return report


def render_receipt(bank: MemoryBank, report: dict, plan: Optional[AdoptionPlan] = None) -> str:
    """The audit record of an adoption. Written whether or not anything moved."""
    lines = [
        f"# Bank adoption receipt — `{bank.name}`",
        "",
        f"**Bank:** `{bank.name}` at `{bank.path}`",
        f"**Mode:** {'dry-run' if report.get('dry_run') else 'applied'}",
        f"**Generated:** {_today()}",
        "",
    ]
    deleted = report.get("deleted") or []
    if deleted:
        lines += [
            f"## Deleted from personal memory ({len(deleted)})",
            "",
            "Each file below was verified line-for-line against the bank's "
            "working tree before deletion. The bank now owns this content.",
            "",
        ]
        lines += [f"- `{name}` → bank `{bank.name}`" for name in deleted]
        lines.append("")
    skipped = report.get("skipped") or []
    if skipped:
        lines += [f"## Not adopted ({len(skipped)})", ""]
        for entry in skipped:
            lines.append(f"- `{entry.get('file')}` — {entry.get('why')}")
            for missing in entry.get("missing") or []:
                lines.append(f"  - not in bank: `{missing[:120]}`")
        lines.append("")
    if report.get("errors"):
        lines += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]
    if report.get("revert"):
        lines += [
            "## Undo",
            "",
            f"**Revert this adoption:** `{report['revert']}`",
            "",
        ]
    elif not report.get("dry_run") and deleted:
        lines += [
            "## Undo",
            "",
            "The memory directory is not a git repository, so this deletion "
            "cannot be reverted from here.",
            "",
        ]
    if plan is not None and plan.is_conversation:
        lines += [
            "## Note",
            "",
            f"This bank overlaps {len(plan.candidates)} personal files. That is "
            "more than a handful — worth talking through before adopting the "
            "rest.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def resolve_bank(name: str) -> Optional[MemoryBank]:
    """The subscribed shared bank called *name*, or ``None``."""
    for bank in configured_banks():
        if bank.name == name and bank.is_shared:
            return bank
    return None
