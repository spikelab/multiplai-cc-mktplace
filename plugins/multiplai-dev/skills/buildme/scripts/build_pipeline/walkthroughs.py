"""After the build: the walkthroughs in the repo whose cited lines the build moved.

Walkthroughs that should be kept current live at `docs/walkthroughs/*-walkthrough.md`
in the project. `pr_section` is one of the orchestrator's
`PRE_PUBLISH_CONTRIBUTORS`: it runs after the last block and the docs/respec
phases, before publish hands the change to the pr skill. For each walkthrough
it runs the codebase-walkthrough skill's model-free check,
`python -m walkthrough_pipeline check <file> --at-head`, which re-reads every
cited `path:line` at the repo's current HEAD; exit 1 means stale. Each repo in
the walkthrough's "Commits read" table is passed as `--repo key=path`: its path
is relative to the walkthrough in the main checkout, so it is resolved there,
and the project's own repo maps to the build's worktree. A walkthrough that
reads a repo which is not there is reported as not checked.

A stale walkthrough is then checked at the build's base commit
(`BuildState.base_sha`) as well: a copy of the file as it was at the base, with
the project's row pinned to the base and each other repo's row to its HEAD,
checked without `--at-head`. A citation that also fails there was stale before
the build. A walkthrough whose every failed citation also failed at the base is
reported as already stale (`WALKTHROUGH:ALREADY_STALE:<path>`), not blamed on
the build and never regenerated. The others are printed as
`WALKTHROUGH:STALE:<path>` with one `WALKTHROUGH_FAIL:` line per citation the
build broke. All of it is written as a PR-body section.

With `regenerate_walkthroughs: true` (off by default: one run may spend up to
`walkthrough_max_usd`), each walkthrough the build made stale is re-run with the module
directory, depth and mode its header records, written over the old files, and
committed to the build branch. The run reads the project's repo only
(`--repos`) and keeps the key the header gave it (`--repo-key`; the worktree's
own directory is `buildme-<change>`), with `--trust-repo` passed only when
buildme's own trust check passed; a walkthrough that read other repositories
is reported, not re-run. A run stopped by its spend limit (exit 2) leaves the
old file in place.

The skill runs as a subprocess (`uv run --directory`), like the pr skill, so
buildme does not depend on walkthrough-pipeline.
"""

from __future__ import annotations

import logging
import re
import tempfile
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
_FIELD = re.compile(r"^- (Module directory|Depth|Mode): `([^`]*)`\s*$", re.MULTILINE)
_ROW = re.compile(
    r"^\|\s*([^|\s]+)\s*\|\s*([^|]+?)\s*\|\s*`([0-9a-f]{40})`\s*\|\s*$", re.MULTILINE
)


@dataclass
class Walkthrough:
    path: Path
    rel: str
    status: str = "fresh"  # "fresh" | "stale" | "already_stale" | "unchecked"
    failures: list[str] = field(
        default_factory=list
    )  # for "stale": only those the build caused
    note: str = ""  # why it was not checked, or what regeneration did
    repos: dict[str, Path] = field(default_factory=dict)  # each header repo's location
    project_key: str | None = None  # the header key of the project's own repo
    earlier: int = 0  # failed citations that already failed at the build's base
    compared: bool = False  # whether it was checked at the build's base


def skill_dir() -> Path:
    """codebase-walkthrough's script directory: ``<skills>/codebase-walkthrough/scripts``,
    the same ``parents[3]`` as ``orchestrator._pr_skill_dir``."""
    return Path(__file__).resolve().parents[3] / "codebase-walkthrough" / "scripts"


def _argv(*args: str) -> list[str]:
    return [
        "uv",
        "run",
        "--directory",
        str(skill_dir()),
        "python",
        "-m",
        "walkthrough_pipeline",
        *args,
    ]


def _last_line(res: git_ops.GitResult) -> str:
    # A run prints `SPEND:` last (git_ops counts it); it says nothing about a failure.
    lines = [
        line
        for line in (res.stderr or res.stdout).strip().splitlines()
        if not line.startswith("SPEND:")
    ]
    return lines[-1][:300] if lines else "no output"


def _failures(res: git_ops.GitResult) -> list[str]:
    return [
        line[len("FAIL ") :]
        for line in res.stdout.splitlines()
        if line.startswith("FAIL ")
    ]


def _locate(
    text: str, md_parent: Path, project_dir: Path, main_project_dir: Path
) -> tuple[dict[str, Path], str | None]:
    """Where each repo in *text*'s "Commits read" table lives, and the key of the project's own repo.

    A row's path is relative to the walkthrough's directory in the main
    checkout (*main_project_dir*), where it was written, not in the build's
    worktree. A path in the main checkout's repo (and not in another repo
    nested inside it) is the project's own repo and maps to the same place in
    the worktree.
    """
    main_parent = main_project_dir / md_parent.relative_to(project_dir)
    main_root = (git_ops.repo_root(main_project_dir) or main_project_dir).resolve()
    root = (git_ops.repo_root(project_dir) or project_dir).resolve()
    repos: dict[str, Path] = {}
    project_key = None
    for key, rel, _ in _ROW.findall(text):
        where = (main_parent / rel).resolve()
        try:
            inside = where.relative_to(main_root)
        except ValueError:
            inside = None
        owner = git_ops.repo_root(where) if where.is_dir() else None
        if inside is None or (owner is not None and owner.resolve() != main_root):
            repos[key] = where
            continue
        repos[key] = root / inside
        project_key = project_key or key
    return repos, project_key


def _repo_args(repos: dict[str, Path]) -> list[str]:
    return [arg for key, where in repos.items() for arg in ("--repo", f"{key}={where}")]


def _base_failures(
    w: Walkthrough, project_dir: Path, main_project_dir: Path, base: str
) -> list[str] | None:
    """The citations of *w* that fail at the build's base commit: [] when it was fresh
    there or did not exist yet, None when that could not be checked."""
    text = git_ops.text_at(
        project_dir, base, w.path.relative_to(project_dir).as_posix()
    )
    if text is None:
        return []  # the walkthrough is new in this build
    repos, project_key = _locate(text, w.path.parent, project_dir, main_project_dir)
    pins: dict[str, str] = {}
    for key, where in repos.items():
        sha = base if key == project_key else git_ops.rev_parse_head(where)
        if sha is None:
            return None
        pins[key] = sha
    text = _ROW.sub(lambda m: m.group(0).replace(m.group(3), pins[m.group(1)]), text)
    with tempfile.TemporaryDirectory(prefix="buildme-walkthrough-base-") as tmp:
        copy = Path(tmp) / w.path.name
        copy.write_text(text, encoding="utf-8")
        res = git_ops._run_walkthrough_skill(
            _argv("check", str(copy), *_repo_args(repos)), cwd=project_dir
        )
    if res.returncode == 0:
        return []
    if res.returncode == 1 and _failures(res):
        return _failures(res)
    log.warning(
        "walkthrough check at the base %s of %s did not run (exit %s): %s",
        base,
        w.rel,
        res.returncode,
        _last_line(res),
    )
    return None


def check_all(
    project_dir: Path, main_project_dir: Path | None = None, base: str | None = None
) -> list[Walkthrough] | None:
    """Check every walkthrough under docs/walkthroughs/ at HEAD, and each stale one at *base*.

    *main_project_dir* is the project in the main checkout (default: *project_dir*).
    None when there is no such directory.
    """
    main_project_dir = main_project_dir or project_dir
    directory = project_dir / WALKTHROUGH_DIR
    if not directory.is_dir():
        return None
    found = []
    for md in sorted(directory.glob(f"*{SUFFIX}")):
        w = Walkthrough(path=md, rel=md.relative_to(project_dir).as_posix())
        found.append(w)
        text = md.read_text(encoding="utf-8", errors="replace")
        w.repos, w.project_key = _locate(text, md.parent, project_dir, main_project_dir)
        missing = [
            (k, p)
            for k, p in w.repos.items()
            if not (p.is_dir() and git_ops.has_commits(p))
        ]
        if missing:
            key, where = missing[0]
            w.status, w.note = (
                "unchecked",
                f"the repo `{key}` it reads is not a git repository with a commit at {where}",
            )
            continue
        res = git_ops._run_walkthrough_skill(
            _argv("check", str(md), "--at-head", *_repo_args(w.repos)), cwd=project_dir
        )
        w.failures = _failures(res)
        if res.returncode == 0:
            w.status = "fresh"
        elif res.returncode == 1 and w.failures:
            w.status = "stale"
        else:
            w.status, w.note = (
                "unchecked",
                f"the check did not run (exit {res.returncode}): {_last_line(res)}",
            )
        if w.status != "stale" or base is None:
            continue
        before = _base_failures(w, project_dir, main_project_dir, base)
        if before is None:
            continue
        w.compared = True
        already = set(before)
        caused = [f for f in w.failures if f not in already]
        w.earlier = len(w.failures) - len(caused)
        if caused:
            w.failures = caused
        else:
            w.status = "already_stale"
    return found


def build_base(project_dir: Path, state) -> str | None:
    """The commit the build started from: `state.base_sha`, or, for a build begun
    before that was recorded, the merge base of HEAD with the default branch."""
    if state is None:
        return None
    if getattr(state, "base_sha", None):
        return state.base_sha
    if getattr(state, "worktree_path", None):
        return git_ops.merge_base(
            project_dir, "HEAD", git_ops.default_branch(project_dir)
        )
    return None


def main_checkout_project_dir(project_dir: Path, state) -> Path:
    """The project's directory in the main checkout the build's worktree was made from."""
    source, worktree = (
        getattr(state, "source_repo", None),
        getattr(state, "worktree_path", None),
    )
    if not source or not worktree:
        return project_dir
    try:
        return Path(source) / project_dir.resolve().relative_to(
            Path(worktree).resolve()
        )
    except ValueError:
        return project_dir


def regenerate(config, w: Walkthrough) -> str:
    """Re-run one stale walkthrough as its header says it was made. Returns what happened."""
    if not sdk._repo_is_trusted():
        return "not regenerated: buildme's repo trust check did not pass (--trust-repo)"
    text = w.path.read_text(encoding="utf-8", errors="replace")
    fields = dict(_FIELD.findall(text))
    if not {"Module directory", "Depth", "Mode"} <= fields.keys():
        return "not regenerated: its header does not record the module directory, depth and mode"
    rows = _ROW.findall(text)
    if len(rows) != 1 or rows[0][0] != w.project_key:
        others = (
            ", ".join(key for key, _, _ in rows if key != w.project_key)
            or "none listed"
        )
        return f"not regenerated: it reads other repositories ({others}); re-run it by hand"
    module = (w.path.parent / fields["Module directory"]).resolve()
    repo = w.repos[rows[0][0]]
    name = w.path.name[: -len(SUFFIX)]
    usd = config.walkthrough_max_usd
    res = git_ops._run_walkthrough_skill(
        _argv(
            "run",
            str(module),
            "--depth",
            fields["Depth"],
            "--mode",
            fields["Mode"],
            "--name",
            name,
            "--output",
            str(w.path.parent),
            "--max-usd",
            f"{usd:g}",
            "--trust-repo",
            "--repos",
            str(repo),
            f"--repo-key={w.project_key}",
        ),
        cwd=config.project_dir,
    )
    if res.returncode == 2:
        return f"not regenerated: the run stopped at its spend limit (${usd:g}); the old file is left in place"
    if not res.ok:
        return f"not regenerated: the run failed (exit {res.returncode}): {_last_line(res)}"
    paths = [w.rel] + [
        p.relative_to(config.project_dir).as_posix()
        for p in [w.path.with_suffix(".json")]
        if p.exists()
    ]
    sha = git_ops.commit_stage(
        config, f"docs(walkthroughs): regenerate {w.path.name}", paths
    )
    return (
        "regenerated and committed"
        if sha
        else "regenerated (not committed: the build owns no branch)"
    )


def _report_lines(w: Walkthrough) -> list[str]:
    lines = [
        f"WALKTHROUGH:{w.status.upper()}:{w.rel}" + (f" — {w.note}" if w.note else "")
    ]
    lines += [f"WALKTHROUGH_FAIL:{w.rel}: {f}" for f in w.failures[:MAX_FAILURES_SHOWN]]
    if len(w.failures) > MAX_FAILURES_SHOWN:
        lines.append(
            f"WALKTHROUGH_FAIL:{w.rel}: … and {len(w.failures) - MAX_FAILURES_SHOWN} more"
        )
    return lines


def _section(found: list[Walkthrough], regenerating: bool, base: str | None) -> str:
    out = ["## Stale walkthroughs", ""]
    stale = [w for w in found if w.status == "stale"]
    if stale:
        out += [
            "This build moved lines these walkthroughs cite "
            "(`python -m walkthrough_pipeline check <file> --at-head` fails on them"
            + (
                f"; the citations listed passed at the build's base commit `{base[:12]}`, "
                "unless an entry says it could not be checked there"
                if base
                else "; the build's base commit is not known, so some may have been stale before it"
            )
            + ").",
            "",
        ]
    for w in stale:
        what = (
            w.note
            if regenerating
            else "not regenerated (`regenerate_walkthroughs` is off)"
        )
        earlier = f" ({w.earlier} more already failed at the base)" if w.earlier else ""
        unknown = (
            ""
            if w.compared or not base
            else "; it could not be checked at the base, so it may have been stale before"
        )
        out.append(
            f"- `{w.rel}`: {len(w.failures)} citation(s) failed{earlier}{unknown}; {what}"
        )
        out += [f"  - `{f}`" for f in w.failures[:MAX_FAILURES_SHOWN]]
        if len(w.failures) > MAX_FAILURES_SHOWN:
            out.append(f"  - … and {len(w.failures) - MAX_FAILURES_SHOWN} more")
    for w in found:
        if w.status == "already_stale":
            out.append(
                f"- `{w.rel}`: already stale at the build's base commit `{(base or '')[:12]}`; "
                f"this build did not cause it, so it is not regenerated ({len(w.failures)} citation(s) fail)"
            )
        elif w.status == "unchecked":
            out.append(f"- `{w.rel}`: not checked; {w.note}")
    return "\n".join(out) + "\n"


def pr_section(config, state, docs_dir: Path) -> Path | None:
    """The PRE_PUBLISH contributor: report stale walkthroughs, regenerate them on request,
    and return the PR-body section file, or None when there is nothing to report."""
    base = build_base(config.project_dir, state)
    found = check_all(
        config.project_dir, main_checkout_project_dir(config.project_dir, state), base
    )
    if found is None or not found:
        print(
            f"WALKTHROUGHS:NONE:no {WALKTHROUGH_DIR.as_posix()}/*{SUFFIX} in {config.project_dir}",
            flush=True,
        )
        return None
    stale = [w for w in found if w.status == "stale"]
    if config.regenerate_walkthroughs:
        for w in stale:
            w.note = regenerate(config, w)
    for w in found:
        for line in _report_lines(w):
            print(line, flush=True)
    already = sum(w.status == "already_stale" for w in found)
    print(
        f"WALKTHROUGHS:{len(found)} checked, {len(stale)} stale"
        + (f", {already} already stale at the build's base" if already else ""),
        flush=True,
    )
    if not stale and not already and not any(w.status == "unchecked" for w in found):
        return None
    from .orchestrator import _pr_sections_dir

    directory = _pr_sections_dir(docs_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SECTION_FILE
    path.write_text(
        _section(found, config.regenerate_walkthroughs, base), encoding="utf-8"
    )
    return path
