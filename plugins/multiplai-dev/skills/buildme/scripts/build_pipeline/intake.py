"""Research and the input checks of a build, saved in the change dir.

pm-spec writes what is built (proposal, use cases, requirements, and the
interview, ticket and plan it read). buildme starts from those and writes
only how it is built. The files here go into ``specs/changes/<name>/`` so
they travel with the change and are committed on the build branch by the
existing spec-stage commit (none of these names is in
``git_ops.BOOKKEEPING_EXCLUDES``):

- ``research.md``           — research content, or why none was needed
- ``research-request.md``   — the topics the wrapper should research
- ``split-proposal.md``     — written by the size gate (orchestrator)
- ``plan-approval.md``      — written by stop 4, the plan approval (orchestrator)

Exit code 3 (WAITING_EXIT) means "waiting on a human or the wrapper"; each
case prints one stdout marker the wrapper acts on before re-running the same
``build --change <name>`` command.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from build_pipeline.budget import BudgetExceededError

from .models import BuildPhase

log = logging.getLogger(__name__)

#: "Waiting on a human or the wrapper" — distinct from 0 (done), 1 (failure),
#: 2 (usage) and 4 (agent timeout, ``tdd_engine.EXIT_AGENT_TIMEOUT``).
WAITING_EXIT = 3

MARKER_RESEARCH_NEEDED = "RESEARCH:NEEDED"
MARKER_SPLIT_REQUIRED = "SPLIT:REQUIRED"
# Stop 4: the build waits for an engineer's approval of the implementation plan
# (`orchestrator._plan_approval_gate`).
MARKER_PLAN_WAITING = "PLAN:WAITING"

CONFIG_NOT_COMMITTED = (
    "specs/config.yaml is not committed; the build's worktree would not see it. "
    "Commit it first."
)

CONFIG_NOT_ON_BASE = (
    "specs/config.yaml at HEAD is not the version on the commit the build branches from "
    "(--base-ref, origin's default branch): the build would read one version and its "
    "worktree hold another. Push it, or pull origin's, first."
)


class ConfigNotCommittedError(RuntimeError):
    """specs/config.yaml is untracked or modified, and the build would branch
    from HEAD without it (`CONFIG_NOT_COMMITTED`); or, with `--base-ref`, the
    version committed at HEAD is not the one on that commit, so the build's
    settings and its worktree's file would differ (`CONFIG_NOT_ON_BASE`)."""


# --- Input file paths ---------------------------------------------------------

#: The `build` flags that name one input file the pipeline reads: the
#: attribute, the flag, the phase that reads it, and the copy that phase saves
#: in the change dir. Once the phase is behind the checkpoint, or its copy
#: exists, the build no longer needs the original.
INPUT_FILE_FLAGS = (
    ("research_path", "--research-path", BuildPhase.RESEARCH, "research.md"),
)


def resolve_input_paths(args) -> None:
    """Make every input file path in *args* absolute, in place.

    SKILL.md runs the pipeline under ``uv run --directory <scripts>``, so the
    process cwd is the scripts directory, never the caller's. A relative path
    is read from ``--project-dir`` instead. Whether a named file must exist is
    decided by ``check_input_files``, once the checkpoint is known.
    """
    base = Path(getattr(args, "project_dir", ".") or ".").expanduser().resolve()

    def absolute(raw: str) -> Path:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else base / path

    for attr, _flag, _phase, _copy in INPUT_FILE_FLAGS:
        raw = getattr(args, attr, "") or ""
        if raw:
            setattr(args, attr, str(absolute(raw)))


def check_input_files(args, state, change_dirs) -> str | None:
    """The error for a named input file the build still needs and cannot read.

    A missing file is an error only while the phase that reads it is still
    ahead of the checkpoint and its copy is not in the change dir: a mistyped
    path never turns into a build without its research (the caller exits 2).
    A resume does not need the original once it has been copied — the
    wrapper's research file is a scratch file that may be gone days later —
    so the flag is dropped and the copy is used.
    """
    base = getattr(args, "project_dir", ".") or "."
    for attr, flag, phase, copy in INPUT_FILE_FLAGS:
        raw = getattr(args, attr, "") or ""
        if not raw or Path(raw).is_file():
            continue
        copies = [Path(d) / copy for d in change_dirs if (Path(d) / copy).is_file()]
        if state.is_phase_complete(phase) or copies:
            log.info(
                "%s %s is gone; the build already has what it read (%s)",
                flag,
                raw,
                copies[0] if copies else f"phase {phase.value} done",
            )
            setattr(args, attr, "")
            continue
        return (
            f"{flag} {raw}: no such file. Pass an absolute path, "
            f"or one relative to --project-dir {base}."
        )
    return None


def carry_over_inputs(previous: Path, change_dir: Path) -> list[Path]:
    """Copy the saved inputs of *previous* into a new *change_dir*.

    A FAILED build re-runs bootstrap. In its own worktree the change dir
    stays put and this copies nothing; a build that failed with no worktree
    recorded and is re-run with one gets a new worktree, so the change dir
    moves. ``check_input_files`` accepted a gone input file because
    its copy was in the old change dir; that copy has to come along, or the
    retry builds without the research it had.
    A copy the new dir already holds is kept.
    """
    previous, change_dir = Path(previous), Path(change_dir)
    if previous.resolve() == change_dir.resolve() or not previous.is_dir():
        return []
    carried = []
    for _attr, _flag, _phase, copy in INPUT_FILE_FLAGS:
        src, dest = previous / copy, change_dir / copy
        if src.is_file() and not dest.exists():
            dest.write_text(src.read_text())
            carried.append(dest)
            log.info("Carried %s over from %s", copy, previous)
    return carried


# --- Bootstrap helpers --------------------------------------------------------


def config_commit_state(project_dir: Path) -> str:
    """``committed``, ``uncommitted`` or ``no-repo`` for ``specs/config.yaml``.

    Uncommitted means untracked (ignored included) or modified. A missing file
    counts as committed: there is nothing for the worktree to miss.
    """
    project_dir = Path(project_dir)
    if not (project_dir / "specs" / "config.yaml").exists():
        return "committed"
    rel = "specs/config.yaml"
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if tracked.returncode not in (0, 1):
            return "no-repo"
        if tracked.returncode == 1:
            return "uncommitted"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "no-repo"
    if status.returncode != 0:
        return "no-repo"
    return "uncommitted" if status.stdout.strip() else "committed"


def config_yaml_uncommitted(project_dir: Path) -> bool:
    """True when ``specs/config.yaml`` exists and is untracked or modified.

    `git worktree add` branches from HEAD, so a config that is not committed
    never reaches the build's worktree. Not a git repo (or git unavailable)
    reads as False: the worktree preflight owns that diagnosis.
    """
    return config_commit_state(project_dir) == "uncommitted"


def config_yaml_off_ref(project_dir: Path, ref: str) -> bool:
    """True when the ``specs/config.yaml`` committed at HEAD is not the version
    at *ref*: *ref* holds another version or none. The build reads the
    checkout's file and a worktree branched from *ref* holds *ref*'s, so the
    two would differ, whether HEAD's version was never pushed or origin has
    changed the file since. No file in the checkout while *ref* holds one
    reads as True too: the build would read no config (no `tdd.test_command`,
    so test discovery runs on the checkout) while the worktree holds *ref*'s.
    No file on either side reads as False. Untracked, or not a git repo,
    also reads as False (`config_yaml_uncommitted` and the worktree preflight
    own those)."""
    present = (Path(project_dir) / "specs" / "config.yaml").exists()
    blobs = []
    for rev in ("HEAD", ref):
        try:
            res = subprocess.run(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{rev}:./specs/config.yaml",
                ],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        blobs.append(res.stdout.strip() if res.returncode == 0 else None)
    if not present:
        return blobs[1] is not None
    return blobs[0] is not None and blobs[0] != blobs[1]


# --- Research -----------------------------------------------------------------


def import_research(src: str, dest: Path) -> bool:
    """Copy --research-path's content into the change dir's ``research.md``."""
    path = Path(src)
    if not path.is_file():
        log.warning("Research file not found: %s — the design gets no research", src)
        return False
    if path.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(path.read_text())
    log.info("Research copied from %s to %s", src, dest)
    return True


async def run_research_phase(
    config, state, args, state_path: Path, progress
) -> int | None:
    """The RESEARCH phase.

    --research-path wins: its content lands in ``research.md``. Otherwise one
    toolless call decides; `needed: false` writes the reason to
    ``research.md`` and continues, `needed: true` writes
    ``research-request.md`` and returns WAITING_EXIT for the wrapper to run
    the research and re-run with --research-path.
    """
    from .orchestrator import _progress_note

    dest = config.research_path
    src = getattr(args, "research_path", "") or ""
    if src:
        if import_research(src, dest):
            state.research_path = str(dest)
        log.info("DONE phase=RESEARCH source=%s", src)
        return None

    if dest.is_file():
        state.research_path = str(dest)
        log.info("DONE phase=RESEARCH source=existing")
        return None

    from .llm_steps.research_steps import decide_research, specs_summary

    try:
        decision = await decide_research(config, specs_summary(config.change_dir))
    except BudgetExceededError:
        raise
    except Exception as e:
        log.warning("FAILED phase=RESEARCH reason=%s — continuing without research", e)
        dest.write_text(
            "# Research\n\nNot run: the research decision call failed "
            f"({e}). The design is written without outside research.\n"
        )
        state.research_path = str(dest)
        _progress_note(
            progress, "RESEARCH", f"Research decision failed (non-fatal): {e}"
        )
        return None

    if not decision.needed:
        dest.write_text(f"# Research\n\nNot needed. {decision.reason.strip()}\n")
        state.research_path = str(dest)
        log.info("DONE phase=RESEARCH needed=false")
        _progress_note(progress, "RESEARCH", f"Research not needed: {decision.reason}")
        return None

    request = config.change_dir / "research-request.md"
    lines = [
        "# Research request",
        "",
        f"Why: {decision.reason.strip()}",
        "",
        "## Topics",
        "",
    ]
    lines += [f"- {t}" for t in decision.topics] or ["- (none named)"]
    request.write_text("\n".join(lines) + "\n")
    # Pointer AT RESEARCH: this phase re-runs on resume.
    state.advance_to(BuildPhase.RESEARCH, state_path)
    log.info("DONE phase=RESEARCH needed=true request=%s", request)
    _progress_note(progress, "RESEARCH", f"Research needed: {decision.reason}")
    print(f"{MARKER_RESEARCH_NEEDED}:{request}", flush=True)
    return WAITING_EXIT
