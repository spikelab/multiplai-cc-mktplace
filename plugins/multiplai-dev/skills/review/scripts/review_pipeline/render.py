"""Markdown: the per-target review, and severity rollups across targets.

Both render from `findings.json` v1 dicts, so a rollup never scrapes markdown.
The per-target review adds two things only the pipeline state has: the
severity a finding had before it was lowered, and the question to ask for each
external premise ("Assumption: … Ask: …").
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .export import to_findings_file
from .models import NO_VERIFIED_FIX, SEVERITIES, ReviewState
from .target import github_web_base

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable")


def code_link(web_base: str | None, head_sha: str, path: str, start: int, end: int) -> str:
    """A full-sha GitHub link for a GitHub remote, else `path:lines`."""
    lines = f"{start}" if start == end else f"{start}-{end}"
    if web_base:
        return f"[{path}:{lines}]({web_base}/blob/{head_sha}/{path}#L{start}-L{end})"
    return f"`{path}:{lines}`"


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def finding_section(fd: dict, *, web_base: str | None, head_sha: str,
                    original_severity: str | None = None,
                    questions: dict[str, str] | None = None) -> str:
    """One finding. *questions* maps an external premise's statement to what to ask."""
    questions = questions or {}
    lines_ = f"{fd['line_start']}" if fd["line_start"] == fd["line_end"] else f"{fd['line_start']}-{fd['line_end']}"
    out = [f"### {fd['severity']} — {fd['file']}:{lines_} — {_one_line(fd['claim'])}", ""]

    status = fd["status"]
    if original_severity and original_severity != fd["severity"]:
        status += f" (lowered from {original_severity})"
    out += [f"**Verdict:** {status}. {_one_line(fd.get('verdict_reason') or '')}".rstrip(), ""]

    out.append("**Evidence:**")
    out.append("")
    for c in fd["citations"]:
        out.append(f"- {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}")
        out.append("")
        out.append(_fence(c["quote"]))
        out.append("")
    out += [f"**Failure scenario:** {_one_line(fd['failure_scenario'])}", ""]

    fix = fd.get("fix")
    if fix:
        if fix["description"] == NO_VERIFIED_FIX:
            out += ["**Fix:** no verified fix.", ""]
        else:
            out += [f"**Fix:** {fix['description'].strip()}", ""]
            if fix.get("patch_sketch"):
                out += [_fence(fix["patch_sketch"]), ""]
        asked = set()
        if fix.get("premises"):
            out.append("Premises:")
            for i, p in enumerate(fix["premises"], 1):
                if p["kind"] == "external":
                    question = questions.get(p["statement"])
                    text = f"Assumption: {_one_line(p['statement']).rstrip('.')}."
                    if question:
                        text += f" Ask: {_one_line(question)}"
                        asked.add(question)
                    out.append(f"{i}. {text}")
                else:
                    c = p.get("citation")
                    where = f" — {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}" if c else ""
                    out.append(f"{i}. {_one_line(p['statement'])}{where}")
            out.append("")
        remaining = [q for q in fix.get("open_questions") or [] if q not in asked]
        if remaining:
            out.append("Open questions:")
            out += [f"- {_one_line(q)}" for q in remaining]
            out.append("")
    return "\n".join(out)


def _counts(findings: list[dict]) -> dict[str, int]:
    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    return {s: sum(1 for f in shown if f["severity"] == s) for s in SEVERITIES}


def render_review(state: ReviewState, *, deployed: str | None = None,
                  findings_file: dict | None = None) -> str:
    data = findings_file or to_findings_file(state)
    t = state.target
    web = github_web_base(t.remote_url)
    head = t.head_sha
    findings = data["findings"]
    counts = _counts(findings)

    commit_link = (lambda sha: f"[{sha[:10]}]({web}/commit/{sha})") if web else (lambda sha: f"`{sha[:10]}`")
    out = [f"# Review — {t.label or t.slug}", ""]
    out.append(f"- **Repo:** {Path(t.repo_path).name}" + (f" ({web})" if web else f" (`{t.repo_path}`)"))
    if t.tickets:
        out.append(f"- **Tickets:** {', '.join(t.tickets)}")
    out.append(f"- **Base commit:** {commit_link(t.base_sha)}")
    out.append(f"- **Head commit:** {commit_link(head)}")
    out.append(f"- **Commits:** {len(t.commits)}")
    out += [f"  - {commit_link(sha)} {_one_line(subject)}" for sha, subject in t.commits]
    out.append(f"- **Files changed:** {len(t.files)}")
    out += [f"  - `{f}`" for f in t.files]
    if t.deployed_in:
        out.append(f"- **Deployed in {t.deployed_in}:** {deployed or 'unknown'}")
    refuted = sum(1 for f in findings if f["status"] == "refuted")
    rejected = sum(1 for f in findings if f["status"] == "rejected")
    out.append(f"- **Findings:** {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW; "
               f"{refuted} refuted, {rejected} rejected by the gates (see the appendix)")
    if state.budget.get("cost_usd") is not None:
        out.append(f"- **Model cost:** ${float(state.budget['cost_usd']):.2f} over {state.budget.get('calls', 0)} agent calls")
    if state.errors:
        out.append("- **Agent failures:** " + "; ".join(_one_line(e) for e in state.errors))
    out += ["", "## Findings", ""]

    questions = {}
    for fix in state.fixes.values():
        for p in fix.premises:
            if p.kind == "external" and p.question:
                questions[p.statement] = p.question

    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    if not shown:
        out += ["No confirmed or unverifiable findings.", ""]
    for severity in SEVERITIES:
        group = [f for f in shown if f["severity"] == severity]
        if not group:
            continue
        out += [f"## {severity}", ""]
        for fd in group:
            out.append(finding_section(fd, web_base=web, head_sha=head,
                                       original_severity=state.original_severity.get(fd["id"]),
                                       questions=questions))

    out += ["## Appendix — rejected and refuted", ""]
    dropped = [f for f in findings if f["status"] in ("refuted", "rejected")]
    if not dropped:
        out += ["Nothing was rejected or refuted.", ""]
    for fd in dropped:
        lines_ = f"{fd['line_start']}-{fd['line_end']}"
        who = "the verifier" if fd["status"] == "refuted" else "a gate"
        out.append(f"- **{fd['status']}** ({fd['severity']}) `{fd['file']}:{lines_}` — {_one_line(fd['claim'])}  ")
        out.append(f"  Reason from {who}: {_one_line(fd.get('verdict_reason') or '')}")
    out.append("")
    return "\n".join(out)


def write_review(state: ReviewState, target_dir: Path, *, deployed: str | None = None) -> Path:
    path = target_dir / f"review-{state.target.slug}.md"
    path.write_text(render_review(state, deployed=deployed), encoding="utf-8")
    return path


# --- rollups -------------------------------------------------------------------


def load_findings_files(paths: list[Path]) -> list[dict]:
    loaded = []
    for p in paths:
        try:
            loaded.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.warning("Skipping unreadable %s: %s", p, e)
    return loaded


def render_rollup(severity: str, files: list[dict]) -> str:
    sections, total, targets = [], 0, 0
    for data in files:
        t = data["target"]
        group = [f for f in data["findings"] if f["severity"] == severity and f["status"] in SHOWN_STATUSES]
        if not group:
            continue
        targets += 1
        total += len(group)
        web = github_web_base(t.get("remote_url"))
        sections.append(f"## {t['label']} (`{t['slug']}`)\n")
        for fd in group:
            sections.append(finding_section(fd, web_base=web, head_sha=t["head_sha"]))
    head = [f"# {severity} findings — {total} across {targets} of {len(files)} targets", ""]
    if not sections:
        head.append(f"No confirmed or unverifiable {severity} findings.")
    return "\n".join(head + sections) + "\n"


def write_rollups(out_dir: Path, paths: list[Path] | None = None) -> list[Path]:
    """`<SEV>-only.md` for each severity, from *paths* (default: `<out>/*/findings.json`, sorted)."""
    if paths is None:
        paths = sorted(out_dir.glob("*/findings.json"))
    files = load_findings_files(paths)
    written = []
    for severity in SEVERITIES:
        path = out_dir / f"{severity}-only.md"
        path.write_text(render_rollup(severity, files), encoding="utf-8")
        written.append(path)
    return written
