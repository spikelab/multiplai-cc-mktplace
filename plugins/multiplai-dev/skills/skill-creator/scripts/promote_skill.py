#!/usr/bin/env python3
"""Promotion gate for a draft skill.

`propose-skill` used to end by writing the draft into
`$CLAUDE_CONFIG_DIR/skills/` and telling the user it was ready. Nothing
executed the scripts it had just written, so the first time anyone found out a
bundled script had a syntax error, a missing import, or a `--help` that raised,
was when they invoked the skill for real — usually mid-task, days later, with
the authoring context long gone.

This is the deterministic half of that gate: it runs the checks a human would
run if they remembered to. It does NOT judge whether the skill is a good idea,
whether its instructions are clear, or whether it duplicates an existing skill
— those are judgment calls that stay in SKILL.md, with the model.

Checks, in order (a failure in one does not skip the rest — you want the whole
list, not the first complaint):

    frontmatter  quick_validate.py: required keys, known model/effort values
    scripts      every bundled script runs with --help and exits 0
    contract     CONTRACT.md assertions, when --contract is passed

Exit codes:
    0 — every check passed; safe to install
    1 — at least one check failed
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUICK_VALIDATE = SCRIPT_DIR / "quick_validate.py"

# How long any single bundled script gets. A skill script that hangs on --help
# is broken; waiting on it just moves the hang into the gate.
TIMEOUT_SECONDS = 30

RUNNABLE_SUFFIXES = {".py", ".sh"}

# Directories that are never the skill's own entry points. `.venv` is the big
# one: two skills vendor a virtualenv under scripts/, and walking into it turned
# up 70+ site-packages modules whose `--help` fails for reasons that have
# nothing to do with the skill (pygments looking for an Xcode SDK, jsonschema
# benchmarks wanting pyperf). That is the shape of a gate nobody keeps.
SKIP_DIRS = {".venv", "venv", "site-packages", "node_modules",
             "tests", "test", "__pycache__", "fixtures"}


PEP723_RE = re.compile(r"^#\s*///\s*script\s*$", re.MULTILINE)

# An import that died before the script's own argv handling ran.
MISSING_DEP_RE = re.compile(
    r"ModuleNotFoundError: No module named '([^']+)'")


def python_command(script: Path) -> list[str]:
    """How to invoke a Python script so its dependencies are actually present.

    A script that declares PEP 723 inline metadata is asking for an isolated
    environment; running it with a bare `python3` gives it the base interpreter
    instead, and it dies on `import` before doing any work. That failure reads
    like the *skill* is broken rather than the runner, so resolve it here.
    """
    text = script.read_text(encoding="utf-8", errors="replace")
    if PEP723_RE.search(text) and shutil.which("uv"):
        return ["uv", "run", "--script", str(script)]
    return [sys.executable, str(script)]


def _clean_env() -> dict[str, str]:
    """uv refuses to re-enter an active venv the way we want; drop the hint."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env


@dataclass
class Result:
    check: str
    ok: bool
    detail: str = ""


@dataclass
class GateReport:
    results: list[Result] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, check: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(check, ok, detail))

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def render(self) -> str:
        lines = []
        for r in self.results:
            mark = "PASS" if r.ok else "FAIL"
            lines.append(f"  [{mark}] {r.check}")
            if r.detail:
                for line in r.detail.strip().splitlines():
                    lines.append(f"         {line}")
        for w in self.warnings:
            lines.append(f"  [warn] {w}")
        verdict = "promote: all checks passed" if self.ok else "promote: BLOCKED"
        lines.append("")
        lines.append(verdict)
        return "\n".join(lines)


def check_frontmatter(skill_dir: Path, report: GateReport) -> None:
    proc = subprocess.run(
        python_command(QUICK_VALIDATE) + [str(skill_dir)],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env=_clean_env())
    report.add("frontmatter", proc.returncode == 0,
               (proc.stdout + proc.stderr).strip())


def _bundled_scripts(skill_dir: Path) -> list[Path]:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    out = []
    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file() or path.suffix not in RUNNABLE_SUFFIXES:
            continue
        if SKIP_DIRS.intersection(path.parts):
            continue
        if path.suffix == ".py":
            # Library modules are imported, not invoked; only entry points are
            # expected to answer --help.
            if "__main__" not in path.read_text(encoding="utf-8",
                                                errors="replace"):
                continue
            # A module inside a package is reached as `-m pkg.mod`, not by
            # path — running it directly raises "attempted relative import
            # with no known parent package", which says nothing about the
            # skill. buildme's build_pipeline/ is the case in point.
            if (path.parent / "__init__.py").exists():
                continue
        out.append(path)
    return out


def check_scripts(skill_dir: Path, report: GateReport) -> None:
    scripts = _bundled_scripts(skill_dir)
    if not scripts:
        report.add("scripts", True, "no bundled entry points to run")
        return

    failures = []
    for script in scripts:
        rel = script.relative_to(skill_dir)
        cmd = (python_command(script) + ["--help"] if script.suffix == ".py"
               else ["bash", str(script), "--help"])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS, env=_clean_env())
        except subprocess.TimeoutExpired:
            failures.append(f"{rel}: --help hung (>{TIMEOUT_SECONDS}s)")
            continue
        if proc.returncode == 0:
            continue

        output = proc.stderr or proc.stdout
        missing = MISSING_DEP_RE.search(output)
        if missing:
            # The script never got as far as its own argument parsing, so this
            # says nothing about its CLI shape — it says the gate environment
            # lacks a dependency the script didn't declare. Actionable, but a
            # warning: failing here would block every skill with a third-party
            # import on any machine that hadn't pre-installed it.
            report.warn(
                f"{rel}: could not import {missing.group(1)!r} — declare it in "
                f"PEP 723 inline metadata so the script runs anywhere")
            continue

        tail = output.strip().splitlines()
        hint = tail[-1] if tail else f"exit {proc.returncode}"
        failures.append(f"{rel}: --help exited {proc.returncode}: {hint}")

    report.add("scripts", not failures,
               "\n".join(failures) if failures
               else f"{len(scripts)} entry point(s) answered --help")


# --------------------------------------------------------------------------- #
# CONTRACT.md
# --------------------------------------------------------------------------- #
# A contract is a fenced block per assertion:
#
#     ### <name>
#     ```sh
#     <command to run>
#     ```
#     Expect: <substring that must appear in stdout>
#
# Deliberately substring-based rather than exact-match: these guard against a
# changed output *shape* across a model or dependency upgrade, and an exact
# diff would fail on every incidental whitespace change and get deleted.

#   ### <name>              — name is a single line; `[^\n]` not `.`, because
#   <optional prose>           DOTALL would otherwise let it swallow the whole
#   ```sh                      explanatory paragraph that follows.
#   <command>
#   ```
#   Expect: <substring>
CONTRACT_CASE_RE = re.compile(
    r"^###[ \t]+(?P<name>[^\n]+?)[ \t]*\n"
    r"(?P<prose>(?:(?!```)[^\n]*\n)*?)"
    r"```(?:sh|bash)?[ \t]*\n(?P<cmd>.*?)^```[ \t]*\n\s*"
    r"Expect:[ \t]*(?P<expect>[^\n]+?)[ \t]*$",
    re.MULTILINE | re.DOTALL)


def parse_contract(text: str) -> list[tuple[str, str, str]]:
    return [(m.group("name").strip(),
             m.group("cmd").strip(),
             m.group("expect").strip())
            for m in CONTRACT_CASE_RE.finditer(text)]


def check_contract(skill_dir: Path, report: GateReport) -> None:
    contract = skill_dir / "CONTRACT.md"
    if not contract.exists():
        report.add("contract", True, "no CONTRACT.md (optional)")
        return

    cases = parse_contract(contract.read_text(encoding="utf-8"))
    if not cases:
        report.add("contract", False,
                   "CONTRACT.md exists but no '### name / ```sh``` / Expect:' "
                   "cases parsed — check the format")
        return

    failures = []
    for name, cmd, expect in cases:
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, timeout=TIMEOUT_SECONDS,
                                  cwd=skill_dir)
        except subprocess.TimeoutExpired:
            failures.append(f"{name}: timed out")
            continue
        combined = proc.stdout + proc.stderr
        if expect not in combined:
            failures.append(f"{name}: expected {expect!r} in output, not found")

    report.add("contract", not failures,
               "\n".join(failures) if failures else f"{len(cases)} assertion(s) held")


def promote(skill_dir: Path, run_contract: bool) -> GateReport:
    report = GateReport()
    check_frontmatter(skill_dir, report)
    check_scripts(skill_dir, report)
    if run_contract:
        check_contract(skill_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the promotion gate against a draft skill directory.")
    ap.add_argument("skill_directory", type=Path,
                    help="the skill directory to check (must contain SKILL.md)")
    ap.add_argument("--contract", action="store_true",
                    help="also run CONTRACT.md assertions")
    args = ap.parse_args(argv)

    skill_dir = args.skill_directory.resolve()
    if not (skill_dir / "SKILL.md").exists():
        print(f"promote: no SKILL.md in {skill_dir}")
        return 1

    print(f"promote gate: {skill_dir.name}")
    report = promote(skill_dir, args.contract)
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
