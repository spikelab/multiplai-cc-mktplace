"""The interview, in three modes, written to `interview.md`.

- `self` — two separate read-only agent calls with no human: a questioner, then
  a fresh answerer whose ungrounded answers become assumptions. With a PRD the
  answerer has no tools and answers from the PRD alone. Best effort: a failed
  self-interview leaves `interview.md` with the inputs and no Q&A.
- `user` — the wrapper ran the interview (SKILL.md) and passes its summary as
  `--interview-summary`.
- `ticket` — the questioner alone; its questions go to `interview-questions.md`
  and the run stops with `INTERVIEW:WAITING:<path>`, exit 3, until a run gets
  the replies as `--interview-replies` (row S1). Needs a ticket ref; without
  one it falls back to `self` under `--auto` and `user` otherwise.

Copied from buildme's `intake.run_interview_phase` and
`llm_steps/interview_steps.py`, then adapted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from . import intake, sdk
from .prompts import UNTRUSTED_NOTICE
from .prompts.interview import ANSWERER_PRD_PROMPT, ANSWERER_PROMPT, QUESTIONER_PRD_RULE, QUESTIONER_PROMPT
from .run import EXIT_WAITING, MARKER_INTERVIEW_WAITING, Run, Stop

log = logging.getLogger(__name__)

MAX_QUESTIONS = 10
QUESTIONS_FILE = "interview-questions.md"


class InterviewQuestions(BaseModel):
    questions: list[str] = Field(default_factory=list)


class InterviewAnswer(BaseModel):
    question: str
    answer: str
    grounded_in: str = ""

    @property
    def is_assumption(self) -> bool:
        return not self.grounded_in.strip()


class InterviewAnswers(BaseModel):
    answers: list[InterviewAnswer] = Field(default_factory=list)


async def ask_questions(run: Run, inputs: str, *, has_prd: bool) -> list[str]:
    prompt = QUESTIONER_PROMPT.format(notice=UNTRUSTED_NOTICE, inputs=inputs, max_questions=MAX_QUESTIONS,
                                      prd_rule=QUESTIONER_PRD_RULE if has_prd else "")
    result = await sdk.agent_structured(prompt, InterviewQuestions, config=run.config, step="interview",
                                        label="interview:questions", cwd=str(run.config.project_dir))
    return [q.strip() for q in result.questions if q and q.strip()][:MAX_QUESTIONS]


async def answer_questions(run: Run, inputs: str, questions: list[str], *, has_prd: bool) -> list[InterviewAnswer]:
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    if has_prd:
        prompt = ANSWERER_PRD_PROMPT.format(notice=UNTRUSTED_NOTICE, inputs=inputs, questions=numbered)
        result = await sdk.text_structured(prompt, InterviewAnswers, config=run.config, step="interview",
                                           label="interview:answers")
    else:
        prompt = ANSWERER_PROMPT.format(notice=UNTRUSTED_NOTICE, inputs=inputs, questions=numbered)
        result = await sdk.agent_structured(prompt, InterviewAnswers, config=run.config, step="interview",
                                            label="interview:answers", cwd=str(run.config.project_dir))
    return result.answers


def effective_mode(run: Run) -> str:
    mode = run.state.options.interview_mode
    if mode == "ticket" and not run.state.options.ticket_ref:
        fallback = "self" if run.state.options.auto else "user"
        log.info("interview: ticket has no ticket to ask on — using %s", fallback)
        return fallback
    return mode


def _write(run: Run, text: str) -> None:
    (run.config.change_dir / "interview.md").write_text(text)
    run.state.interview_waiting = False
    run.state.interview_done = True
    run.save()
    run.progress("INTERVIEW", f"Interview saved to {run.config.change_dir / 'interview.md'}")
    print("PHASE:INTERVIEW:COMPLETE", flush=True)


async def run_interview(run: Run, *, replies_file: str = "") -> None:
    """Write `interview.md`, or stop with exit 3 while ticket replies are awaited."""
    if run.state.interview_done and (run.config.change_dir / "interview.md").is_file():
        return
    change_dir = run.config.change_dir
    summary = run.state.summary
    contexts = [(c.name, c.content) for c in run.state.contexts]
    mode = effective_mode(run)
    has_prd = bool(intake.prd_text(change_dir))
    log.info("START phase=INTERVIEW mode=%s", mode)

    if mode == "ticket":
        await _ticket_interview(run, contexts, summary, replies_file, has_prd)
        return

    if mode == "self":
        inputs = intake.interview_inputs(change_dir, summary, contexts)
        try:
            questions = await ask_questions(run, inputs, has_prd=has_prd)
            answers = await answer_questions(run, inputs, questions, has_prd=has_prd) if questions else []
        except sdk.BudgetExceededError:
            raise
        except Exception as e:
            # Best effort, as in buildme: the spec is still written from the
            # inputs — only the Q&A is missing.
            log.warning("FAILED phase=INTERVIEW reason=%s — continuing without the self-interview", e)
            run.progress("INTERVIEW", f"Self-interview failed (non-fatal): {e}")
            questions, answers = [], []
        _write(run, intake.render_interview(change_dir, summary=summary, contexts=contexts,
                                            questions=questions, answers=answers))
        return

    # user: the wrapper ran the interview and passes its summary.
    _write(run, intake.render_interview(change_dir, summary=summary, contexts=contexts))


async def _ticket_interview(run: Run, contexts, summary: str, replies_file: str, has_prd: bool) -> None:
    change_dir = run.config.change_dir
    questions_path = change_dir / QUESTIONS_FILE
    replies = ""
    if replies_file:
        path = Path(replies_file)
        replies = path.read_text().strip() if path.is_file() else ""
        if not path.is_file():
            log.warning("Interview replies file not found: %s", replies_file)

    if replies and questions_path.is_file():
        _write(run, intake.render_interview(
            change_dir, summary=summary, contexts=contexts, questions=intake.read_questions(questions_path),
            replies=replies, replies_source=run.state.options.ticket_url or run.state.options.ticket_ref
            or "ticket replies"))
        return

    if questions_path.is_file():
        # Row S1, no answers: the same marker again, exit 3, nothing written.
        print(f"{MARKER_INTERVIEW_WAITING}:{questions_path}", flush=True)
        raise Stop(EXIT_WAITING, "waiting for the ticket replies")

    questions = await ask_questions(run, intake.interview_inputs(change_dir, summary, contexts), has_prd=has_prd)
    if not questions:
        log.info("phase=INTERVIEW mode=ticket questions=0 — continuing without waiting")
        _write(run, intake.render_interview(change_dir, summary=summary, contexts=contexts))
        return
    intake.write_questions(questions_path, questions, run.state.options.ticket_ref)
    run.state.interview_waiting = True
    run.save()
    run.progress("INTERVIEW", f"Waiting for replies on {run.state.options.ticket_ref}; questions in {questions_path}")
    print(f"{MARKER_INTERVIEW_WAITING}:{questions_path}", flush=True)
    raise Stop(EXIT_WAITING, "waiting for the ticket replies")
