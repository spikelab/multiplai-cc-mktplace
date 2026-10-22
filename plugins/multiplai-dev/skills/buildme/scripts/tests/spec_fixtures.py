"""Ready specs for tests: what pm-spec hands buildme.

buildme builds only from a change whose proposal, use cases and requirements
pm-spec has marked ready in `.change.yaml` (`multiplai_core.specs.is_ready`),
and, for a build that branches a worktree, only from the change's feature
branch on origin, where pm-spec pushes them. `ready_specs` writes a minimal
valid set where one is missing, gives a change with no ticket ref (and no
parent) pm-spec's `ENG-1`, marks it ready, and commits it when the project is
a git repo; `publish` pushes that commit to `origin/ENG-1-<change>`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from multiplai_core.specs import (
    expected_capabilities,
    mark_ready,
    read_change_meta,
    write_change_meta,
)

# The ticket id `ready_specs` gives a change with none, as pm-spec requires one.
TICKET = "ENG-1"

REQUIREMENT = (
    "## ADDED Requirements\n\n### Requirement: Behavior\nThe system SHALL return a value.\n\n"
    "#### Scenario: Works\n- **WHEN** invoked\n- **THEN** returns a value\n"
)


def write_specs(
    change_dir: Path, *, capabilities: tuple[str, ...] = ("fixture",)
) -> Path:
    """Write proposal.md, use-cases.md and one requirements file per capability
    the proposal names, each only where it is missing. Returns *change_dir*."""
    change_dir = Path(change_dir)
    (change_dir / "requirements").mkdir(parents=True, exist_ok=True)
    proposal = change_dir / "proposal.md"
    text = (
        proposal.read_text()
        if proposal.is_file()
        else "# Proposal\n\n## Why\nA fixture change.\n"
    )
    if not expected_capabilities(text):
        caps = "\n".join(f"- `{c}`" for c in capabilities)
        proposal.write_text(
            text.rstrip("\n") + f"\n\n## Capabilities\n\n### New Capabilities\n{caps}\n"
        )
    use_cases = change_dir / "use-cases.md"
    if not use_cases.is_file():
        use_cases.write_text(
            "# Use cases\n\n## Personas\n- Dev: builds things\n\n## Use cases\n"
            "- Dev wants a value so that they can see it\n"
        )
    for cap in expected_capabilities(proposal.read_text()):
        path = change_dir / "requirements" / f"{cap}.md"
        if not path.is_file():
            path.write_text(REQUIREMENT)
    return change_dir


def commit_specs(
    project_dir: Path, change_dir: Path, message: str = "specs: ready"
) -> bool:
    """Commit *change_dir* in *project_dir* when that is inside a git repo
    and the dir has changes. Returns whether a commit was made."""
    project_dir = Path(project_dir)
    top = (
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if project_dir.is_dir()
        else None
    )
    if top is None or top.returncode != 0:
        return False
    rel = Path(change_dir).resolve().relative_to(project_dir.resolve()).as_posix()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", rel],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        return False
    subprocess.run(
        ["git", "add", "-f", "--", rel],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message, "--", rel],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    return True


def ready_specs(
    change_dir: Path,
    project_dir: Path | None = None,
    *,
    commit: bool = True,
    capabilities: tuple[str, ...] = ("fixture",),
) -> str:
    """Write missing specs (`write_specs`), give the change `TICKET` when it
    has no ticket ref and no parent, mark them ready and, with *commit* and a
    git *project_dir*, commit the change dir. Returns the revision."""
    write_specs(change_dir, capabilities=capabilities)
    give_ticket(change_dir)
    revision = mark_ready(change_dir)
    if commit and project_dir is not None:
        commit_specs(project_dir, change_dir)
    return revision


def give_ticket(change_dir: Path, ticket: str = TICKET) -> None:
    """Record *ticket* as the change's own ticket ref (`ticket_refs.epic`)
    when it has none and no parent, as pm-spec's `--ticket-ref` does."""
    try:
        meta = read_change_meta(change_dir)
    except (OSError, ValueError):
        meta = {}
    refs = meta.get("ticket_refs")
    if meta.get("parent") or (isinstance(refs, dict) and refs.get("epic")):
        return
    write_change_meta(change_dir, {"ticket_refs": {"epic": ticket, "children": []}})


def never_marked(change_dir: Path) -> bool:
    """Whether the change dir has no `spec_status` at all: pm-spec never
    handed it off."""
    try:
        return "spec_status" not in read_change_meta(change_dir)
    except (OSError, ValueError):
        return True


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def publish(project_dir: Path, change: str, branch: str = "") -> str | None:
    """Play pm-spec's push: HEAD of *project_dir*'s repository goes to
    `<TICKET>-<change>` (or *branch*) on origin, when origin lacks that
    branch. A repository with no origin gets a bare one beside it,
    `<repo>.origin.git`, holding HEAD as its default branch, whose
    pre-receive hook takes a push to that branch, the first push of each
    branch this function names (listed in `<bare>/published`) and a merge of
    origin's default branch into one (`Merge origin/...`), and refuses every
    other: as a repository with no origin did, it refuses the build's own
    push, so a test that never stubbed a PR opens none. Returns the branch,
    or None when *project_dir* is in no repository with a commit."""
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return None
    top = _git(project_dir, "rev-parse", "--show-toplevel")
    if (
        top.returncode
        or _git(project_dir, "rev-parse", "--verify", "-q", "HEAD").returncode
    ):
        return None
    repo = Path(top.stdout.strip())
    branch = branch or f"{TICKET}-{change}"
    if "origin" not in _git(repo, "remote").stdout.split():
        bare = repo.parent / f"{repo.name}.origin.git"
        on = (
            _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
            or "main"
        )
        if not bare.exists():
            subprocess.run(
                ["git", "init", "-q", "--bare", "-b", on, str(bare)],
                check=True,
                capture_output=True,
            )
            hook = bare / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\n"
                "zero=0000000000000000000000000000000000000000\n"
                "while read old new ref; do\n"
                f'  [ "$ref" = refs/heads/{on} ] && continue\n'
                '  if grep -qxF "$ref" published 2>/dev/null; then\n'
                '    [ "$old" = "$zero" ] && continue\n'
                '    git log -1 --format=%s "$new" | grep -q "^Merge origin/" && continue\n'
                "  fi\n"
                '  echo "this test origin takes no push to $ref" >&2; exit 1\n'
                "done\n"
            )
            hook.chmod(0o755)
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{on}")
    _git(repo, "fetch", "-q", "origin")
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    listed = Path(url) / "published"
    if Path(url).is_dir() and (Path(url) / "hooks" / "pre-receive").is_file():
        with listed.open("a") as f:
            f.write(f"refs/heads/{branch}\n")
    if _git(
        repo, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"
    ).returncode:
        pushed = _git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        if pushed.returncode:
            return None
        _git(repo, "fetch", "-q", "origin")
    return branch
