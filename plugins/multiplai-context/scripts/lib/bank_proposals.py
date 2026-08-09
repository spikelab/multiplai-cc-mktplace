"""Turning shared-bound memory items into a pull request on a bank.

## The one-sentence rule

**Contributions to a shared bank leave as a pull request, in every write mode,
and no model is involved in producing them.**

## Why there is no model on this path

The plan this implements proposed a *separate proposal pass* for bank-bound
items — a second prompt containing the bank's files and the candidate
learnings, but never personal memory — because "a personal fact riding along
inside a 'team' learning is the dangerous failure mode". This module takes the
stronger option available: there is **no prompt at all**. The text that goes
into the pull request is the item's own quoted text, byte for byte, exactly as
it appeared in the dream proposal a human read. A leak channel that does not
exist cannot be prompted, rate-limited, or degraded, and it satisfies the
degradation contract for free: this works identically with no SDK client.

What that does *not* remove is the residual risk that the drafter — which does
see personal memory when it writes the proposal — put a personal fact into a
bank-bound item. Three things stand between that and a teammate's repo:

1. the item sits in the **review** pile, because the write floor refused it
   (``shared-bank-write``), so a human reads it in the proposal;
2. :mod:`lib.bank_policy` refuses it if it names a no-go domain the bank
   declared, or contains anything shaped like a credential;
3. opening the pull request is an **explicit human command**
   (``memory_bank.py contribute --apply``), never something the dream pipeline
   does on its own — so ``auto`` mode has no path to a shared bank even if
   every check above were removed.

## The append is deterministic

An item is appended to the end of the H2 section it names, or to the end of the
file when it names none. No rewriting, no reflowing, no model deciding where a
line belongs. The diff a reviewer sees on the bank repo is therefore exactly
the lines contributed and nothing else, which is what makes reviewing it cheap
enough to actually happen.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from lib.bank_git import (
    current_branch,
    head_sha,
    is_git_repo,
    open_pull_request,
    push_branch,
    run_git,
    stage_commit,
)
from lib.bank_policy import BankPolicy, check_item, load_policy
from lib.banks import MemoryBank, banks_by_name
from lib.memory_write_floor import is_safe_target, target_bank

logger = logging.getLogger(__name__)

__all__ = [
    "Contribution",
    "ContributionPlan",
    "append_under_section",
    "plan_contributions",
    "render_contribution_file",
    "submit",
]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclasses.dataclass(frozen=True)
class Contribution:
    """One item bound for one file in one bank."""

    bank: str
    filename: str
    section: str
    title: str
    text: str
    source: str
    provenance: str = ""
    kind: str = ""
    blocked: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.bank}/{self.filename}"

    @property
    def allowed(self) -> bool:
        return not self.blocked


@dataclasses.dataclass(frozen=True)
class ContributionPlan:
    """Everything that would happen, computed before anything happens."""

    bank: MemoryBank
    policy: BankPolicy
    contributions: tuple[Contribution, ...]
    blocked: tuple[Contribution, ...]
    errors: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted({c.filename for c in self.contributions}))

    @property
    def is_empty(self) -> bool:
        return not self.contributions


def _as_bullet(text: str) -> str:
    """The item's text as one markdown bullet, its own formatting preserved."""
    body = (text or "").strip()
    if not body:
        return ""
    if body.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+[.)]\s", body):
        return body
    return f"- {body}"


def append_under_section(text: str, section: str, block: str) -> str:
    """Append *block* at the end of the H2 named *section*, or at end of file.

    "End of the section" means immediately before the next H2, with trailing
    blank lines preserved on the far side — so repeated contributions stack in
    order instead of interleaving, and a reviewer reading the diff sees an
    append and not a restructure.
    """
    block = block.rstrip("\n")
    if not block:
        return text
    lines = (text or "").splitlines()
    if section:
        wanted = section.strip().lstrip("#").strip().lower()
        start = None
        for i, line in enumerate(lines):
            if line.startswith("## ") and line[3:].strip().lower() == wanted:
                start = i
                break
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].startswith("## "):
                    end = j
                    break
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            new = lines[:end] + ["", block] + lines[end:]
            return "\n".join(new).rstrip("\n") + "\n"
    body = "\n".join(lines).rstrip("\n")
    if section:
        return f"{body}\n\n## {section.strip()}\n\n{block}\n" if body else (
            f"## {section.strip()}\n\n{block}\n"
        )
    return f"{body}\n\n{block}\n" if body else f"{block}\n"


def plan_contributions(
    items: Iterable, banks: Optional[Sequence[MemoryBank]] = None
) -> list[ContributionPlan]:
    """Group shared-bound *items* by bank, applying each bank's own policy.

    *items* are the ones ``dream_triage.shared_bank_items`` returned. An item
    naming a bank that is not subscribed is reported as an error rather than
    silently dropped — it means the catalog and the config disagree, which the
    user needs to know about.
    """
    by_name = banks_by_name(banks)
    grouped: dict[str, list] = {}
    unknown: dict[str, list[str]] = {}
    unsafe: dict[str, list[str]] = {}
    for item in items:
        raw_target = getattr(item, "target", "") or ""
        bank_name, filename = target_bank(raw_target)
        if bank_name not in by_name or not by_name[bank_name].is_shared:
            unknown.setdefault(bank_name or "?", []).append(raw_target or "?")
            continue
        # The remainder after the bank name is about to become `bank.path /
        # filename`, twice, in `submit`. `target_bank("team/../../CLAUDE.md")`
        # returns ("team", "../../CLAUDE.md") — and the write floor refuses that
        # target as `shared-bank-write`, which is precisely the reason code that
        # routes it *here*. pathlib does not normalise `..` and gives an absolute
        # path total override, so the unvalidated remainder was an arbitrary file
        # write with model-authored markdown. `is_safe_target` was already in this
        # module's import graph and never called.
        if not is_safe_target(filename):
            unsafe.setdefault(bank_name, []).append(raw_target or "?")
            continue
        grouped.setdefault(bank_name, []).append((item, filename))

    plans: list[ContributionPlan] = []
    for bank_name in sorted(grouped):
        bank = by_name[bank_name]
        policy = load_policy(bank.path, bank=bank.name)
        allowed: list[Contribution] = []
        blocked: list[Contribution] = []
        errors: list[str] = []
        if not bank.accepts_contributions:
            errors.append(
                f"bank '{bank.name}' is mode `{bank.mode}` — it takes no "
                "contributions; nothing will be proposed"
            )
        for item, filename in grouped[bank_name]:
            reasons = tuple(check_item(item, policy))
            contribution = Contribution(
                bank=bank.name,
                filename=filename,
                section=getattr(item, "section", "") or "",
                title=getattr(item, "title", "") or "",
                text=getattr(item, "text", "") or "",
                source=getattr(item, "source", "") or "",
                provenance=getattr(item, "provenance", "") or "",
                kind=getattr(item, "kind", "") or "",
                blocked=reasons,
            )
            if reasons or not bank.accepts_contributions:
                blocked.append(
                    contribution
                    if reasons
                    else dataclasses.replace(
                        contribution, blocked=(f"bank mode is {bank.mode}",)
                    )
                )
            else:
                allowed.append(contribution)
        plans.append(
            ContributionPlan(
                bank=bank,
                policy=policy,
                contributions=tuple(allowed),
                blocked=tuple(blocked),
                errors=tuple(errors),
            )
        )

    for name, targets in sorted(unknown.items()):
        logger.warning(
            "%d item(s) target bank '%s', which is not a subscribed shared bank: %s",
            len(targets), name, ", ".join(sorted(set(targets))),
        )
    for name, targets in sorted(unsafe.items()):
        logger.error(
            "%d item(s) targeting bank '%s' name something that is not a bare "
            "memory filename and were dropped: %s",
            len(targets), name, ", ".join(sorted(set(targets))),
        )
    return plans


def render_contribution_file(plan: ContributionPlan) -> str:
    """The human-readable record of what a contribution would do.

    Written next to the dream receipt so the decision is auditable whether or
    not a pull request was ever opened.
    """
    lines = [
        f"# Bank contribution — `{plan.bank.name}`",
        "",
        f"**Bank:** `{plan.bank.name}` at `{plan.bank.path}`",
        f"**Remote:** {plan.bank.remote or 'not configured'}",
        f"**Policy:** {plan.policy.summary}",
        f"**Generated:** {_today()}",
        "",
        "These items were refused a local write because they belong to a shared "
        "bank. Nothing here has been written anywhere: a contribution leaves as "
        "a pull request, reviewed by the bank's owners, in every write mode.",
        "",
    ]
    if plan.errors:
        lines += ["## Cannot proceed", ""] + [f"- {e}" for e in plan.errors] + [""]
    if plan.contributions:
        lines += [f"## Proposed ({len(plan.contributions)})", ""]
        for c in plan.contributions:
            where = f"`{c.filename}`" + (f" § {c.section}" if c.section else "")
            pair = f" [{c.provenance}/{c.kind}]" if (c.provenance or c.kind) else ""
            lines.append(f"### {c.title or '(untitled)'} → {where}{pair}")
            lines.append("")
            lines.append(_as_bullet(c.text))
            if c.source:
                lines.append("")
                lines.append(f"_Source: {c.source}_")
            lines.append("")
    if plan.blocked:
        lines += [f"## Blocked ({len(plan.blocked)})", ""]
        for c in plan.blocked:
            # The title is NOT echoed. `check_item` scans the title, so a title is
            # one of the places a credential or a no-go term can be — and echoing
            # it here printed that value to stdout *and* wrote it into
            # dreams/banks/…-contribution.md, i.e. the item was blocked and
            # transcribed. `find_secrets` returns labels only and the reasons
            # carry no values, which is what makes the reason line safe to print;
            # the title had no such guarantee. The item is identified by where it
            # was going and why it was refused, which is what the reader needs.
            lines.append(
                f"- `{c.filename}`"
                + (f" § {c.section}" if c.section else "")
                + " — " + "; ".join(c.blocked)
            )
        lines += [
            "",
            "Blocked items are **not** rejected memory — they are refused "
            "*sharing*. They stay in your review pile and can still be applied "
            "to your personal memory by retargeting them.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def submit(
    plan: ContributionPlan, *, dry_run: bool = True, branch: Optional[str] = None
) -> dict:
    """Write the files, commit, push and open the pull request.

    ``dry_run`` is the **default** and is not a formality: this is the only
    function in the memory system that puts the user's writing into somebody
    else's repository, and the caller has to say so twice (an explicit
    ``--apply`` on the CLI, and this flag) before it does.

    Returns a report dict; never raises. A failure at any step stops the
    sequence and leaves the bank checkout on its original branch where
    possible — a half-pushed contribution is worse than none.

    Every filename is re-validated here even though :func:`plan_contributions`
    already did. This is the function that turns a string into a path and writes
    to it, and a plan can be constructed by hand or by a future caller; the check
    belongs where the write is, not only where the plan was built.
    """
    report: dict = {
        "bank": plan.bank.name,
        "dry_run": dry_run,
        "files": list(plan.files),
        "items": len(plan.contributions),
        "blocked": len(plan.blocked),
        "branch": "",
        "pr_url": "",
        "errors": list(plan.errors),
    }
    if plan.is_empty:
        report["errors"].append("nothing to contribute")
        return report
    if plan.errors:
        return report

    path = plan.bank.path
    if not is_git_repo(path):
        report["errors"].append(f"{path} is not a git repository")
        return report

    base = current_branch(path) or "main"
    branch = branch or f"memory-bank/{_today()}-{head_sha(path)[:7] or 'new'}"
    report["branch"] = branch
    report["base"] = base

    # Compute every new file body first, so a dry run reports exactly what an
    # apply would write and an apply cannot half-write.
    updates: dict[str, str] = {}
    for c in plan.contributions:
        if not is_safe_target(c.filename):
            report["errors"].append(
                f"refusing to write {c.filename!r}: not a bare memory filename"
            )
            return report
        target = path / c.filename
        current = updates.get(c.filename)
        if current is None:
            try:
                current = target.read_text(encoding="utf-8") if target.exists() else (
                    f"# {c.filename[:-3].replace('-', ' ').title()}\n"
                )
            except (OSError, UnicodeDecodeError) as e:
                report["errors"].append(f"cannot read {c.filename}: {e}")
                return report
        updates[c.filename] = append_under_section(current, c.section, _as_bullet(c.text))
    report["diff_preview"] = {k: len(v) for k, v in updates.items()}

    if dry_run:
        return report

    checkout = run_git(["checkout", "-b", branch], cwd=path)
    if not checkout.ok:
        report["errors"].append(f"could not create branch {branch}: {checkout.detail}")
        return report

    try:
        for filename, body in updates.items():
            (path / filename).write_text(body, encoding="utf-8")
    except OSError as e:
        report["errors"].append(f"could not write bank file: {e}")
        run_git(["checkout", base], cwd=path)
        return report

    committed = stage_commit(
        path,
        pathspec=sorted(updates),
        message=(
            f"memory: contribute {len(plan.contributions)} item(s) "
            f"to {', '.join(sorted(updates))}"
        ),
    )
    if not committed.ok:
        report["errors"].append(f"commit failed: {committed.detail}")
        return report

    pushed = push_branch(path, branch)
    if not pushed.ok:
        report["errors"].append(f"push failed: {pushed.detail}")
        return report

    pr = open_pull_request(
        path,
        title=f"memory: {len(plan.contributions)} contribution(s) from a teammate",
        body=render_contribution_file(plan),
        base=base,
        head=branch,
    )
    if pr.ok:
        report["pr_url"] = pr.output.strip().splitlines()[-1] if pr.output else ""
    else:
        report["errors"].append(pr.detail)
    run_git(["checkout", base], cwd=path)
    return report
