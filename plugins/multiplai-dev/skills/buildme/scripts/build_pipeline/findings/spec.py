"""Record the requirements changes review findings ask for; pm-spec makes them.

buildme never edits requirements. For every `spec_change` verdict, and every
`code_fix` whose fix contradicts the current spec (`Triage.spec_conflict`),
the spec step appends one `## ` section to `spec-issues.md` in the change
directory (the archived change's own directory when the change is archived),
sets `spec_issues` in that directory's `.change.yaml`
(`multiplai_core.specs.set_spec_issues`), and commits both as
`spec(findings-r<N>): <ids>`, with or without `--auto`. `pm-spec revise
<change>` reads the file, one issue per `## ` section, and edits the
requirements.

Each section is written from the triage verdict alone, with no model call:
its heading names the finding, and its body names the requirements file
(`specs.md#<cap>` or `requirements/<cap>.md`, only when triage named a
capability the change has), the requirement as triage named it, the reviewer's comment link
and the change triage asks for. A section already in the file under the same
heading (an earlier round's, or a resumed step's) is not written again, and
nothing already in the file is rewritten.

A `code_fix` with `spec_conflict` depends on that change, so it is not fixed
this round (`code.code_fixes` leaves it out) and its reply says why. Other
code fixes go ahead in the same round.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from multiplai_core.specs import (
    CHANGE_META_FILE,
    normalize_change_name,
    set_spec_issues,
)
from multiplai_core.untrusted import defang

from .. import git_ops
from ..change_manager import requirement_sources
from ..config import BuildConfig
from ..readiness import pm_spec_change, write_issues
from .models import SpecIssue, TriageRecord, Verdict

log = logging.getLogger(__name__)

ISSUES_FILE = "spec-issues.md"


def issues_path(change_dir: Path) -> Path:
    return Path(change_dir) / ISSUES_FILE


def revise_command(config: BuildConfig) -> str:
    return f"/multiplai-pm:pm-spec revise {normalize_change_name(pm_spec_change(config.change_name))}"


def short_id(record: TriageRecord) -> str:
    c = record.comment
    return c.finding_id or f"c{c.id}"


def defers_fix(record: TriageRecord) -> bool:
    """A `code_fix` whose fix contradicts the current spec: it waits on the
    requirements change."""
    t = record.triage
    return bool(t and t.verdict == Verdict.CODE_FIX and t.spec_conflict)


def needs_spec(records: list[TriageRecord]) -> list[TriageRecord]:
    """The records that ask for a requirements change."""
    return [
        r
        for r in records
        if r.triage and (r.triage.verdict == Verdict.SPEC_CHANGE or defers_fix(r))
    ]


def _line(text: str, limit: int) -> str:
    """Model- or GitHub-written text as one defanged line, so it cannot start
    a `## ` section of its own."""
    return " ".join(defang(text or "", limit).split())


def heading(record: TriageRecord) -> str:
    c = record.comment
    if c.finding_id:
        return f"Finding {_line(c.finding_id, 80)}"
    return c.label[:1].upper() + c.label[1:]


def requirements_file(change_dir: Path, requirement: str) -> str:
    """Where triage's `<cap>: <name>` is specified: `specs.md#<cap>` when the
    change's `specs.md` has that capability, else `requirements/<cap>.md`
    when the change has that file (`requirement_sources`); "" otherwise: the
    name came from a model."""
    cap = requirement.partition(":")[0].strip()
    if not cap or cap != Path(cap).name:
        return ""
    sources = requirement_sources(Path(change_dir))
    for label in (f"specs.md#{cap}", f"requirements/{cap}.md"):
        if label in sources:
            return label
    return ""


def entry(record: TriageRecord, req_file: str) -> str:
    """The body of the record's `## ` section of `spec-issues.md`, under
    `heading(record)`."""
    t = record.triage
    c = record.comment
    assert t is not None
    link = _line(c.url, 300) or "(no link)"
    lines = []
    if req_file:
        lines.append(f"- Requirements file: `{req_file}`")
    lines.append(
        f"- Requirement: {_line(t.requirement, 300) or '(not named by the triage)'}"
    )
    lines.append(
        f"- Raised in: {link} (by @{_line(c.author, 80)}, "
        f"buildme findings round {record.round})"
    )
    if defers_fix(record):
        lines.append(
            "- A code fix waits on this change: the fix the comment asks for "
            "contradicts this requirement."
        )
    label = "Why the fix needs it" if defers_fix(record) else "Requested change"
    lines += ["", f"{label}: {_line(t.reason, 2000)}"]
    if defers_fix(record) and t.failing_test:
        lines.append(
            f"Once changed, the requirement must allow this: {_line(t.failing_test, 1000)}"
        )
    return "\n".join(lines) + "\n"


def _has_section(text: str, head: str) -> bool:
    return re.search(rf"^## {re.escape(head)}[ \t]*$", text, re.MULTILINE) is not None


def append_issues(
    change_dir: Path, records: list[TriageRecord], *, change: str | None = None
) -> list[str]:
    """Append each record's section to `spec-issues.md` through
    `readiness.write_issues`, the file's one writer, skipping one whose
    heading the file already holds; return the headings appended. *change*
    names the change in a new file's header (the change dir's name when not
    given)."""
    path = issues_path(change_dir)
    text = path.read_text() if path.exists() else ""
    added: list[str] = []
    issues: list[tuple[str, str]] = []
    for r in records:
        head = heading(r)
        if _has_section(text, head) or head in added:
            continue
        t = r.triage
        assert t is not None
        issues.append((head, entry(r, requirements_file(change_dir, t.requirement))))
        added.append(head)
    if issues:
        write_issues(Path(change_dir), issues, change=change or Path(change_dir).name)
    return added


def commit_spec(
    project_dir: Path, round_n: int, records: list[TriageRecord], paths: list[Path]
) -> str | None:
    """`spec(findings-r<N>): <ids>` over exactly *paths*."""
    ids = ", ".join(short_id(r) for r in records)
    return git_ops.commit_paths(
        Path(project_dir),
        f"spec(findings-r{round_n}): {ids}",
        [str(p) for p in paths] + git_ops.BOOKKEEPING_EXCLUDES,
    )


def _relative(path: Path, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def run_spec_step(
    config: BuildConfig,
    change_dir: Path,
    round_n: int,
    records: list[TriageRecord],
    *,
    extra_paths: list[Path] | None = None,
) -> tuple[list[SpecIssue], str | None]:
    """Record the round's requirements changes in `spec-issues.md`, set
    `spec_issues` in `.change.yaml` and commit both. Returns the issues and
    the commit sha (None when no finding asked for a requirements change).
    *extra_paths* (e.g. `findings-r<N>.md`) ride along in the same commit."""
    wanted = needs_spec(records)
    if not wanted:
        return [], None
    change_dir = Path(change_dir)
    added = set(append_issues(change_dir, wanted, change=config.change_name))
    if not added:
        log.info(
            "findings round %d: spec-issues.md already held every requirements issue",
            round_n,
        )
    path = issues_path(change_dir)
    set_spec_issues(change_dir, path)
    issues_file, command = _relative(path, config.project_dir), revise_command(config)
    issues = []
    for r in wanted:
        t = r.triage
        assert t is not None
        head = heading(r)
        issues.append(
            SpecIssue(
                finding_key=r.comment.key,
                heading=head,
                issues_file=issues_file,
                command=command,
                requirement=t.requirement,
                requirements_file=requirements_file(change_dir, t.requirement),
                defers_fix=defers_fix(r),
                appended=head in added,
            )
        )
        added.discard(head)  # a second record under the same heading added nothing
    sha = commit_spec(
        config.project_dir,
        round_n,
        wanted,
        [path, change_dir / CHANGE_META_FILE, *(extra_paths or [])],
    )
    return issues, sha
