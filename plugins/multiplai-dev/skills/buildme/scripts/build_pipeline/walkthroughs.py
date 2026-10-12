"""After the build: the walkthroughs in the repo whose cited lines the build moved.

Walkthroughs that should be kept current live at `docs/walkthroughs/*-walkthrough.md`
in the project. `pr_section` is one of the orchestrator's
`PRE_PUBLISH_CONTRIBUTORS`: it runs after the last block and the docs/respec
phases, before publish hands the change to the pr skill. For each walkthrough
it runs the codebase-walkthrough skill's model-free check,
`python -m walkthrough_pipeline check <file> --at-head`, which re-reads every
cited `path:line` at the repo's current HEAD; exit 1 means stale. The stale
ones and their failed citations are printed (`WALKTHROUGH:STALE:<path>` and one
`WALKTHROUGH_FAIL:` line per citation) and written as a PR-body section.

With `regenerate_walkthroughs: true` (off by default: one run may spend up to
`walkthrough_max_usd`), each stale walkthrough is re-run with the module
directory, depth and mode its header records, written over the old files, and
committed to the build branch. The run reads the project's repo only
(`--repos`), with `--trust-repo` passed only when buildme's own trust check
passed; a walkthrough that read other repositories is reported, not re-run. A
run stopped by its spend limit (exit 2) leaves the old file in place.

The skill runs as a subprocess (`uv run --directory`), like the pr skill, so
buildme does not depend on walkthrough-pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops, sdk

log = logging.getLogger(__name__)

WALKTHROUGH_DIR = Path("docs") / "walkthroughs"
SUFFIX = "-walkthrough.md"
SECTION_FILE = "walkthroughs.md"
# Failed citations printed per walkthrough; the rest are counted.
MAX_FAILURES_SHOWN = 20

# The header lines codebase-walkthrough writes (render.render_markdown).
_FIELD = re.compile(r"^- (Module directory|Depth|Mode): `([^`]*)`\s*$", re.M)
_ROW = re.compile(r"^\|\s*([^|\s]+)\s*\|\s*([^|]+?)\s*\|\s*`([0-9a-f]{40})`\s*\|\s*$", re.M)


@dataclass
class Walkthrough:
    path: Path
    rel: str
    status: str = "fresh"       # "fresh" | "stale" | "unchecked"
    failures: list[str] = field(default_factory=list)
    note: str = ""              # why it was not checked, or what regeneration did


def skill_dir() -> Path:
    """codebase-walkthrough's script directory: ``<skills>/codebase-walkthrough/scripts``,
    the same ``parents[3]`` as ``orchestrator._pr_skill_dir``."""
    return Path(__file__).resolve().parents[3] / "codebase-walkthrough" / "scripts"


def _argv(*args: str) -> list[str]:
    return ["uv", "run", "--directory", str(skill_dir()), "python", "-m", "walkthrough_pipeline", *args]


def _last_line(res: git_ops.GitResult) -> str:
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[-1][:300] if lines else "no output"


def check_all(project_dir: Path) -> list[Walkthrough] | None:
    """Check every walkthrough under docs/walkthroughs/ at HEAD. None when there is no such directory."""
    directory = project_dir / WALKTHROUGH_DIR
    if not directory.is_dir():
        return None
    found = []
    for md in sorted(directory.glob(f"*{SUFFIX}")):
        w = Walkthrough(path=md, rel=md.relative_to(project_dir).as_posix())
        res = git_ops._run_walkthrough_skill(_argv("check", str(md), "--at-head"), cwd=project_dir)
        w.failures = [line[len("FAIL "):] for line in res.stdout.splitlines() if line.startswith("FAIL ")]
        if res.returncode == 0:
            w.status = "fresh"
        elif res.returncode == 1 and w.failures:
            w.status = "stale"
        else:
            w.status, w.note = "unchecked", f"the check did not run (exit {res.returncode}): {_last_line(res)}"
        found.append(w)
    return found


def regenerate(config, w: Walkthrough) -> str:
    """Re-run one stale walkthrough as its header says it was made. Returns what happened."""
    if not sdk._repo_is_trusted():
        return "not regenerated: buildme's repo trust check did not pass (--trust-repo)"
    text = w.path.read_text(encoding="utf-8", errors="replace")
    fields = dict(_FIELD.findall(text))
    if not {"Module directory", "Depth", "Mode"} <= fields.keys():
        return "not regenerated: its header does not record the module directory, depth and mode"
    rows = _ROW.findall(text)
    if len(rows) != 1:
        others = ", ".join(key for key, _, _ in rows[1:]) or "none listed"
        return f"not regenerated: it reads other repositories ({others}); re-run it by hand"
    module = (w.path.parent / fields["Module directory"]).resolve()
    repo = (w.path.parent / rows[0][1]).resolve()
    name = w.path.name[:-len(SUFFIX)]
    usd = config.walkthrough_max_usd
    res = git_ops._run_walkthrough_skill(_argv(
        "run", str(module), "--depth", fields["Depth"], "--mode", fields["Mode"], "--name", name,
        "--output", str(w.path.parent), "--max-usd", f"{usd:g}", "--trust-repo", "--repos", str(repo),
    ), cwd=config.project_dir)
    if res.returncode == 2:
        return f"not regenerated: the run stopped at its spend limit (${usd:g}); the old file is left in place"
    if not res.ok:
        return f"not regenerated: the run failed (exit {res.returncode}): {_last_line(res)}"
    paths = [w.rel] + [p.relative_to(config.project_dir).as_posix()
                       for p in [w.path.with_suffix(".json")] if p.exists()]
    sha = git_ops.commit_stage(config, f"docs(walkthroughs): regenerate {w.path.name}", paths)
    return "regenerated and committed" if sha else "regenerated (not committed: the build owns no branch)"


def _report_lines(w: Walkthrough) -> list[str]:
    lines = [f"WALKTHROUGH:{w.status.upper()}:{w.rel}" + (f" — {w.note}" if w.note else "")]
    lines += [f"WALKTHROUGH_FAIL:{w.rel}: {f}" for f in w.failures[:MAX_FAILURES_SHOWN]]
    if len(w.failures) > MAX_FAILURES_SHOWN:
        lines.append(f"WALKTHROUGH_FAIL:{w.rel}: … and {len(w.failures) - MAX_FAILURES_SHOWN} more")
    return lines


def _section(found: list[Walkthrough], regenerating: bool) -> str:
    out = ["## Stale walkthroughs", "",
           "This build moved lines these walkthroughs cite "
           "(`python -m walkthrough_pipeline check <file> --at-head` failed on them).", ""]
    for w in found:
        if w.status == "stale":
            what = w.note if regenerating else "not regenerated (`regenerate_walkthroughs` is off)"
            out.append(f"- `{w.rel}`: {len(w.failures)} citation(s) failed; {what}")
            out += [f"  - `{f}`" for f in w.failures[:MAX_FAILURES_SHOWN]]
            if len(w.failures) > MAX_FAILURES_SHOWN:
                out.append(f"  - … and {len(w.failures) - MAX_FAILURES_SHOWN} more")
        elif w.status == "unchecked":
            out.append(f"- `{w.rel}`: not checked; {w.note}")
    return "\n".join(out) + "\n"


def pr_section(config, state, docs_dir: Path) -> Path | None:
    """The PRE_PUBLISH contributor: report stale walkthroughs, regenerate them on request,
    and return the PR-body section file, or None when there is nothing to report."""
    found = check_all(config.project_dir)
    if found is None or not found:
        print(f"WALKTHROUGHS:NONE:no {WALKTHROUGH_DIR.as_posix()}/*{SUFFIX} in {config.project_dir}", flush=True)
        return None
    stale = [w for w in found if w.status == "stale"]
    if config.regenerate_walkthroughs:
        for w in stale:
            w.note = regenerate(config, w)
    for w in found:
        for line in _report_lines(w):
            print(line, flush=True)
    print(f"WALKTHROUGHS:{len(found)} checked, {len(stale)} stale", flush=True)
    if not stale and not any(w.status == "unchecked" for w in found):
        return None
    from .orchestrator import _pr_sections_dir

    directory = _pr_sections_dir(docs_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SECTION_FILE
    path.write_text(_section(found, config.regenerate_walkthroughs), encoding="utf-8")
    return path
