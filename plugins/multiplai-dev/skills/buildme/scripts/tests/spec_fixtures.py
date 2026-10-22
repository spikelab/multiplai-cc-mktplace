"""Ready specs for tests: what pm-spec hands buildme.

buildme builds only from a change whose proposal, use cases and requirements
pm-spec has marked ready in `.change.yaml` (`multiplai_core.specs.is_ready`),
and, for a build that branches a worktree, only once they are committed.
`ready_specs` writes a minimal valid set where one is missing, marks it
ready, and commits it when the project is a git repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from multiplai_core.specs import expected_capabilities, mark_ready, read_change_meta

REQUIREMENT = ("## ADDED Requirements\n\n### Requirement: Behavior\nThe system SHALL return a value.\n\n"
               "#### Scenario: Works\n- **WHEN** invoked\n- **THEN** returns a value\n")


def write_specs(change_dir: Path, *, capabilities: tuple[str, ...] = ("fixture",)) -> Path:
    """Write proposal.md, use-cases.md and one requirements file per capability
    the proposal names, each only where it is missing. Returns *change_dir*."""
    change_dir = Path(change_dir)
    (change_dir / "requirements").mkdir(parents=True, exist_ok=True)
    proposal = change_dir / "proposal.md"
    text = proposal.read_text() if proposal.is_file() else "# Proposal\n\n## Why\nA fixture change.\n"
    if not expected_capabilities(text):
        caps = "\n".join(f"- `{c}`" for c in capabilities)
        proposal.write_text(text.rstrip("\n") + f"\n\n## Capabilities\n\n### New Capabilities\n{caps}\n")
    use_cases = change_dir / "use-cases.md"
    if not use_cases.is_file():
        use_cases.write_text("# Use cases\n\n## Personas\n- Dev: builds things\n\n## Use cases\n"
                             "- Dev wants a value so that they can see it\n")
    for cap in expected_capabilities(proposal.read_text()):
        path = change_dir / "requirements" / f"{cap}.md"
        if not path.is_file():
            path.write_text(REQUIREMENT)
    return change_dir


def commit_specs(project_dir: Path, change_dir: Path, message: str = "specs: ready") -> bool:
    """Commit *change_dir* in *project_dir* when that is inside a git repo
    and the dir has changes. Returns whether a commit was made."""
    project_dir = Path(project_dir)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project_dir,
                         capture_output=True, text=True) if project_dir.is_dir() else None
    if top is None or top.returncode != 0:
        return False
    rel = Path(change_dir).resolve().relative_to(project_dir.resolve()).as_posix()
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", rel],
                            cwd=project_dir, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        return False
    subprocess.run(["git", "add", "-f", "--", rel], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message, "--", rel],
                   cwd=project_dir, check=True, capture_output=True)
    return True


def ready_specs(change_dir: Path, project_dir: Path | None = None, *, commit: bool = True,
                capabilities: tuple[str, ...] = ("fixture",)) -> str:
    """Write missing specs (`write_specs`), mark them ready and, with *commit*
    and a git *project_dir*, commit the change dir. Returns the revision."""
    write_specs(change_dir, capabilities=capabilities)
    revision = mark_ready(change_dir)
    if commit and project_dir is not None:
        commit_specs(project_dir, change_dir)
    return revision


def never_marked(change_dir: Path) -> bool:
    """Whether the change dir has no `spec_status` at all: pm-spec never
    handed it off."""
    try:
        return "spec_status" not in read_change_meta(change_dir)
    except (OSError, ValueError):
        return True
