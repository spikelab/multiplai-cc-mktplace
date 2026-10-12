"""Intake, interview and research: what a build starts from, saved in the change dir.

Everything here writes a file into ``specs/changes/<name>/`` so the inputs
travel with the change and are committed on the build branch by the existing
spec-stage commit (none of these names is in ``git_ops.BOOKKEEPING_EXCLUDES``):

- ``ticket.md``             — the ticket the wrapper fetched (--ticket-file)
- ``plan.md``               — a plan-skill plan passed as --plan-file
- ``interview.md``          — the interview, whichever mode produced it
- ``interview-questions.md``— the questions `interview: ticket` is waiting on
- ``research.md``           — research content, or why none was needed
- ``research-request.md``   — the topics the wrapper should research
- ``split-proposal.md``     — written by the size gate (orchestrator)

Exit code 3 (WAITING_EXIT) means "waiting on a human or the wrapper"; each
case prints one stdout marker the wrapper acts on before re-running the same
``build --change <name>`` command.

Ticket text, ticket replies and research files are external content. They are
wrapped in an ``<untrusted-content>`` fence (``multiplai_core.untrusted.fence``)
wherever they are written or inlined into a prompt.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from multiplai_core.untrusted import fence

from .models import BuildPhase

log = logging.getLogger(__name__)

#: "Waiting on a human or the wrapper" — distinct from 0 (done), 1 (failure),
#: 2 (usage) and 4 (agent timeout, ``tdd_engine.EXIT_AGENT_TIMEOUT``).
WAITING_EXIT = 3

MARKER_INTERVIEW_WAITING = "INTERVIEW:WAITING"
MARKER_RESEARCH_NEEDED = "RESEARCH:NEEDED"
MARKER_SPLIT_REQUIRED = "SPLIT:REQUIRED"

CONFIG_NOT_COMMITTED = (
    "specs/config.yaml is not committed; the build's worktree would not see it. "
    "Commit it first."
)


class ConfigNotCommittedError(RuntimeError):
    """specs/config.yaml is untracked or modified, and the build would branch
    from HEAD without it."""


# --- Input file paths ---------------------------------------------------------

#: The `build` flags that name one input file the pipeline reads: the
#: attribute, the flag, the phase that reads it, and the copy that phase saves
#: in the change dir. Once the phase is behind the checkpoint, or its copy
#: exists, the build no longer needs the original.
INPUT_FILE_FLAGS = (
    ("ticket_file", "--ticket-file", BuildPhase.BOOTSTRAP, "ticket.md"),
    ("research_path", "--research-path", BuildPhase.RESEARCH, "research.md"),
    ("plan_file", "--plan-file", BuildPhase.BOOTSTRAP, "plan.md"),
    ("interview_replies", "--interview-replies", BuildPhase.INTERVIEW_DONE, "interview.md"),
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
    contexts = getattr(args, "context_files", None)
    if contexts:
        args.context_files = [str(absolute(raw)) for raw in contexts]


def check_input_files(args, state, change_dirs) -> str | None:
    """The error for a named input file the build still needs and cannot read.

    A missing file is an error only while the phase that reads it is still
    ahead of the checkpoint and its copy is not in the change dir: a mistyped
    path never turns into a build without its ticket or research (the caller
    exits 2). A resume does not need the original once it has been copied —
    the wrapper's ticket file is a scratch file that may be gone days later —
    so the flag is dropped and the copy is used. ``--context-files`` keeps its
    warn-and-skip behaviour for a missing file.
    """
    base = getattr(args, "project_dir", ".") or "."
    for attr, flag, phase, copy in INPUT_FILE_FLAGS:
        raw = getattr(args, attr, "") or ""
        if not raw or Path(raw).is_file():
            continue
        copies = [Path(d) / copy for d in change_dirs if (Path(d) / copy).is_file()]
        if state.is_phase_complete(phase) or copies:
            log.info("%s %s is gone; the build already has what it read (%s)",
                     flag, raw, copies[0] if copies else f"phase {phase.value} done")
            setattr(args, attr, "")
            continue
        return (f"{flag} {raw}: no such file. Pass an absolute path, "
                f"or one relative to --project-dir {base}.")
    return None


def carry_over_inputs(previous: Path, change_dir: Path) -> list[Path]:
    """Copy the saved inputs of *previous* into a new *change_dir*.

    A FAILED build re-runs bootstrap, which sets up a new worktree, so the
    change dir moves. ``check_input_files`` accepted a gone input file because
    its copy was in the old change dir; that copy has to come along, or the
    retry builds without the ticket, plan, research or interview it had.
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
            cwd=str(project_dir), capture_output=True, text=True, timeout=30,
        )
        if tracked.returncode not in (0, 1):
            return "no-repo"
        if tracked.returncode == 1:
            return "uncommitted"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            cwd=str(project_dir), capture_output=True, text=True, timeout=30,
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


def fenced(text: str, source: str) -> str:
    """*text* inside an untrusted-content fence, as one string."""
    return "\n".join(fence(text, source))


def save_ticket(change_dir: Path, ticket_file: str, *, ref: str = "", url: str = "") -> Path | None:
    """Copy the wrapper's ticket file to ``ticket.md``, fenced as untrusted."""
    src = Path(ticket_file)
    if not src.is_file():
        log.warning("Ticket file not found: %s", ticket_file)
        return None
    source = url or ref or src.name
    header = [f"# Ticket {ref}".rstrip(), ""]
    if url:
        header += [f"Source: {url}", ""]
    dest = change_dir / "ticket.md"
    dest.write_text("\n".join(header) + fenced(src.read_text(), source) + "\n")
    log.info("Saved ticket %s to %s", ref or src.name, dest)
    return dest


def save_plan(change_dir: Path, plan_file: str) -> Path | None:
    """Copy a plan-skill plan into ``plan.md``. Recorded only: skipping plan
    authoring for it is a later change."""
    src = Path(plan_file)
    if not src.is_file():
        log.warning("Plan file not found: %s", plan_file)
        return None
    dest = change_dir / "plan.md"
    dest.write_text(src.read_text())
    log.info("Saved plan %s to %s", src, dest)
    return dest


# --- Interview ----------------------------------------------------------------

def read_context_files(paths: list[str]) -> list[tuple[str, str]]:
    """``(name, content)`` for each context file that exists."""
    out = []
    for raw in paths or []:
        path = Path(raw)
        if path.is_file():
            out.append((path.name, path.read_text()))
        else:
            log.warning("Context file not found: %s", raw)
    return out


def interview_inputs(change_dir: Path, summary: str, contexts: list[tuple[str, str]]) -> str:
    """What the interview is about: ticket (already fenced), plan, context, summary."""
    parts = []
    ticket = change_dir / "ticket.md"
    if ticket.is_file():
        parts.append("## Ticket\n" + ticket.read_text())
    plan = change_dir / "plan.md"
    if plan.is_file():
        parts.append("## Plan\n" + plan.read_text())
    for name, content in contexts:
        parts.append(f"## Context: {name}\n{content}")
    if summary:
        parts.append(f"## Interview\n{summary}")
    return "\n\n".join(parts)


def render_interview(
    change_dir: Path,
    *,
    summary: str = "",
    contexts: list[tuple[str, str]] | None = None,
    questions: list[str] | None = None,
    answers: list | None = None,
    replies: str = "",
    replies_source: str = "ticket replies",
) -> str:
    """The text of ``interview.md``.

    Sections, each only when it has content: the ticket (already fenced),
    ``## Plan`` for ``plan.md``, one ``## Context: <name>`` per context file, ``## Interview`` for a summary,
    then ``## Questions`` / ``## Answers`` / ``## Assumptions`` (self mode) or
    ``## Questions`` / ``## Replies`` (ticket mode). An answer with empty
    ``grounded_in`` goes under Assumptions, never Answers.
    """
    out = ["# Interview", ""]
    ticket = change_dir / "ticket.md"
    if ticket.is_file():
        out += ["## Ticket", "", ticket.read_text().rstrip(), ""]
    plan = change_dir / "plan.md"
    if plan.is_file():
        out += ["## Plan", "", plan.read_text().rstrip(), ""]
    for name, content in contexts or []:
        out += [f"## Context: {name}", "", content.rstrip(), ""]
    if summary:
        out += ["## Interview", "", summary.rstrip(), ""]
    if questions:
        out += ["## Questions", ""]
        out += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        out.append("")
    if answers:
        grounded = [a for a in answers if not a.is_assumption]
        assumed = [a for a in answers if a.is_assumption]
        if grounded:
            out += ["## Answers", ""]
            for a in grounded:
                out += [f"**Q: {a.question}**", "", a.answer.rstrip(), "",
                        f"_Grounded in: {a.grounded_in.strip()}_", ""]
        if assumed:
            out += ["## Assumptions", "",
                    "Nothing in the repo, the ticket or the context decides these; "
                    "each answer is an assumption to confirm.", ""]
            for a in assumed:
                out += [f"**Q: {a.question}**", "", a.answer.rstrip(), ""]
    if replies:
        out += ["## Replies", "", fenced(replies, replies_source), ""]
    return "\n".join(out).rstrip() + "\n"


def write_interview(change_dir: Path, text: str) -> Path:
    path = change_dir / "interview.md"
    path.write_text(text)
    log.info("Wrote %s", path)
    return path


def read_questions(path: Path) -> list[str]:
    """Questions back out of ``interview-questions.md`` (numbered lines)."""
    import re

    if not path.is_file():
        return []
    return [m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", path.read_text(), re.M)]


def write_questions(path: Path, questions: list[str], ref: str = "") -> None:
    lines = [f"# Questions for {ref}".rstrip() if ref else "# Questions", "",
             "buildme needs these answered before it writes the spec. "
             "Reply on the ticket; the build continues on its next run.", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    path.write_text("\n".join(lines) + "\n")


def _read_replies(raw: str) -> str:
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_file():
        log.warning("Interview replies file not found: %s", raw)
        return ""
    return path.read_text().strip()


def effective_interview_mode(config, state) -> str:
    """`ticket` needs a ticket to ask on; a prompt or a file falls back to
    `self` under --auto and `user` otherwise (logged)."""
    mode = config.interview_mode
    if mode == "ticket" and not state.ticket_ref:
        fallback = "self" if config.auto else "user"
        log.info("interview: ticket has no ticket to ask on — using %s", fallback)
        return fallback
    return mode


async def run_interview_phase(config, state, args, state_path: Path, progress) -> int | None:
    """The INTERVIEW phase. Returns an exit code to stop with, or None to go on.

    `user` keeps today's flow (the wrapper's interviewer summary arrives as
    --interview-summary) and now also writes ``interview.md``. `self` runs the
    questioner and the answerer. `ticket` asks on the ticket and returns
    WAITING_EXIT until replies arrive via --interview-replies.
    """
    from . import board
    from .orchestrator import _progress_note

    change_dir = config.change_dir
    summary = getattr(args, "interview_summary", "") or ""
    contexts = read_context_files(getattr(args, "context_files", []) or [])
    mode = effective_interview_mode(config, state)
    log.info("START phase=INTERVIEW mode=%s", mode)

    if mode == "ticket":
        return await _ticket_interview(config, state, args, state_path, progress, contexts, summary)

    if mode == "self":
        inputs = interview_inputs(change_dir, summary, contexts)
        if not inputs:
            # Nothing to interview on: the pre-intake behaviour exactly.
            return _no_interview(config, state, state_path)
        from .llm_steps.interview_steps import run_self_interview
        try:
            questions, answers = await run_self_interview(config, inputs)
        except Exception as e:
            # Best-effort, like codebase analysis: the spec is still written
            # from the ticket, context and summary — only the Q&A is missing.
            log.warning("FAILED phase=INTERVIEW reason=%s — continuing without the "
                        "self-interview", e)
            _progress_note(progress, "INTERVIEW",
                           f"Self-interview failed (non-fatal, needs --trust-repo): {e}")
            questions, answers = [], []
        text = render_interview(change_dir, summary=summary, contexts=contexts,
                                questions=questions, answers=answers)
        write_interview(change_dir, text)
        state.interview_summary = text
    else:  # user — the wrapper ran the interviewer skill
        # A --plan-file used to arrive as a context file; it still reaches the
        # spec the same way, as the first context entry.
        plan = change_dir / "plan.md"
        plan_context = [(plan.name, plan.read_text())] if plan.is_file() else []
        state_summary = _legacy_summary(summary, plan_context + contexts)
        if not state_summary and not (change_dir / "ticket.md").is_file():
            return _no_interview(config, state, state_path)
        write_interview(change_dir, render_interview(change_dir, summary=summary, contexts=contexts))
        ticket = change_dir / "ticket.md"
        if ticket.is_file():
            state_summary = "\n\n".join(p for p in (ticket.read_text(), state_summary) if p)
        state.interview_summary = state_summary

    state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)
    log.info("DONE phase=INTERVIEW mode=%s", mode)
    _progress_note(progress, "INTERVIEW", f"Interview ({mode}) saved to {change_dir / 'interview.md'}")
    print("PHASE:INTERVIEW:COMPLETE", flush=True)
    board.record(config, state, BuildPhase.INTERVIEW_DONE, progress=progress)
    return None


def _legacy_summary(summary: str, contexts: list[tuple[str, str]]) -> str:
    """`state.interview_summary` exactly as it was built before intake existed."""
    if not contexts:
        return summary
    loaded = "\n\n".join(f"--- Context: {name} ---\n{content}" for name, content in contexts)
    return f"{loaded}\n\n--- Interview Summary ---\n{summary}" if summary else loaded


def _no_interview(config, state, state_path: Path) -> None:
    """No summary, no context, no ticket: skip, as before intake existed."""
    if not config.auto:
        log.warning("SKIP phase=INTERVIEW reason=no-summary-provided")
        state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)
    else:
        log.info("SKIP phase=INTERVIEW reason=nothing-to-interview-on")
    return None


async def _ticket_interview(config, state, args, state_path, progress, contexts, summary) -> int | None:
    from . import board
    from .orchestrator import _progress_note

    change_dir = config.change_dir
    questions_path = change_dir / "interview-questions.md"
    replies = _read_replies(getattr(args, "interview_replies", "") or "")

    def finish(text: str | None, note: str) -> None:
        if text is not None:
            write_interview(change_dir, text)
            state.interview_summary = text
        state.interview_waiting = False
        state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)
        log.info("DONE phase=INTERVIEW mode=ticket")
        _progress_note(progress, "INTERVIEW", note)
        print("PHASE:INTERVIEW:COMPLETE", flush=True)
        board.record(config, state, BuildPhase.INTERVIEW_DONE, progress=progress)

    interview_path = change_dir / "interview.md"
    if not state.interview_waiting and state.interview_summary and interview_path.is_file():
        # A retry of a build that already finished its interview (a FAILED
        # checkpoint re-runs every phase): reuse it. Asking again would print
        # INTERVIEW:WAITING and the wrapper would post the questions twice.
        # This holds when the retry repeats --interview-replies too: the retry
        # sets up a new worktree without interview-questions.md, so the
        # replies have no questions to match and the questioner would run.
        log.info("phase=INTERVIEW mode=ticket — reusing %s", interview_path)
        finish(None, f"Interview already saved in {interview_path}; reused")
        return None

    if replies and questions_path.is_file():
        questions = read_questions(questions_path)
        finish(render_interview(
            change_dir, summary=summary, contexts=contexts, questions=questions,
            replies=replies, replies_source=state.ticket_url or state.ticket_ref or "ticket replies",
        ), "Ticket replies received; interview saved")
        return None

    if questions_path.is_file():
        questions = read_questions(questions_path)
    else:
        from .llm_steps.interview_steps import ask_questions
        inputs = interview_inputs(change_dir, summary, contexts)
        try:
            questions = await ask_questions(config, inputs)
        except Exception as e:
            log.error("FAILED phase=INTERVIEW reason=%s", e)
            _progress_note(progress, "INTERVIEW", f"Questioner failed: {e}")
            print(f"ERROR: interview questioner failed: {e}", flush=True)
            state.advance_to(BuildPhase.FAILED, state_path)
            board.record_failure(config, state, f"interview questioner failed: {e}",
                                 progress=progress)
            return 1

    if not questions:
        # Nothing to ask, so nothing to wait for — same as self mode with an
        # empty list. A questions file with no questions (written by an
        # earlier run) is removed so it is not committed as a blank ask.
        questions_path.unlink(missing_ok=True)
        log.info("phase=INTERVIEW mode=ticket questions=0 — continuing without waiting")
        finish(render_interview(change_dir, summary=summary, contexts=contexts),
               "The questioner had no questions for the ticket; interview saved")
        return None

    if not questions_path.is_file():
        write_questions(questions_path, questions, state.ticket_ref or "")

    state.interview_waiting = True
    # Pointer AT INTERVIEW_DONE, which is_phase_complete does not yet count as
    # complete (it compares strictly): the interview re-runs on resume, while
    # BOOTSTRAP counts as done and does not try to set up the worktree again.
    state.advance_to(BuildPhase.INTERVIEW_DONE, state_path)
    log.info("DONE phase=INTERVIEW mode=ticket waiting=true questions=%s", questions_path)
    _progress_note(progress, "INTERVIEW",
                   f"Waiting for replies on {state.ticket_ref}; questions in {questions_path}")
    print(f"{MARKER_INTERVIEW_WAITING}:{questions_path}", flush=True)
    return WAITING_EXIT


# --- Research -----------------------------------------------------------------

def import_research(src: str, dest: Path) -> bool:
    """Copy --research-path's content into the change dir's ``research.md``."""
    path = Path(src)
    if not path.is_file():
        log.warning("Research file not found: %s — the proposal gets no research", src)
        return False
    if path.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(path.read_text())
    log.info("Research copied from %s to %s", src, dest)
    return True


async def run_research_phase(config, state, args, state_path: Path, progress) -> int | None:
    """The RESEARCH phase for scratch/brief builds.

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

    from .llm_steps.interview_steps import decide_research
    try:
        decision = await decide_research(config, state.interview_summary or "")
    except Exception as e:
        log.warning("FAILED phase=RESEARCH reason=%s — continuing without research", e)
        dest.write_text(
            "# Research\n\nNot run: the research decision call failed "
            f"({e}). The spec is written without outside research.\n")
        state.research_path = str(dest)
        _progress_note(progress, "RESEARCH", f"Research decision failed (non-fatal): {e}")
        return None

    if not decision.needed:
        dest.write_text(f"# Research\n\nNot needed. {decision.reason.strip()}\n")
        state.research_path = str(dest)
        log.info("DONE phase=RESEARCH needed=false")
        _progress_note(progress, "RESEARCH", f"Research not needed: {decision.reason}")
        return None

    request = config.change_dir / "research-request.md"
    lines = ["# Research request", "", f"Why: {decision.reason.strip()}", "", "## Topics", ""]
    lines += [f"- {t}" for t in decision.topics] or ["- (none named)"]
    request.write_text("\n".join(lines) + "\n")
    # Pointer AT RESEARCH: this phase re-runs on resume, the interview before
    # it does not (a self-interview would otherwise pay for its calls again).
    state.advance_to(BuildPhase.RESEARCH, state_path)
    log.info("DONE phase=RESEARCH needed=true request=%s", request)
    _progress_note(progress, "RESEARCH", f"Research needed: {decision.reason}")
    print(f"{MARKER_RESEARCH_NEEDED}:{request}", flush=True)
    return WAITING_EXIT
