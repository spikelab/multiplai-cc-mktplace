"""Interview modes and the research decision.

`interview: self` runs two separate calls with no human: a *questioner* that
decides what an interviewer would ask, then a fresh *answerer* that answers
each question from the repo, the ticket and the context. An answer with no
`grounded_in` is recorded as an assumption, not a fact.

`interview: ticket` uses only the questioner; the questions are posted on the
ticket by the wrapper session and the replies come back on the next run.

The research decision is one toolless call that says whether outside research
is needed, and why.

Every function takes its call function as a keyword argument; None means the
real SDK adapter, looked up at call time so a patched module attribute is seen.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..prompts.interview import (
    ANSWERER_PROMPT,
    QUESTIONER_PROMPT,
    RESEARCH_DECISION_PROMPT,
    UNTRUSTED_NOTICE,
)
from ..sdk import agent_call_structured, llm_call_structured

log = logging.getLogger(__name__)

#: Read-only, like every reviewer: the interview reads the repo, never edits it.
INTERVIEW_TOOLS = ["Read", "Glob", "Grep"]
INTERVIEW_MAX_TURNS = 30
MAX_QUESTIONS = 10


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


class ResearchDecision(BaseModel):
    needed: bool
    reason: str = ""
    topics: list[str] = Field(default_factory=list)


async def ask_questions(config, inputs: str, *, call=None) -> list[str]:
    """The questioner: at most MAX_QUESTIONS questions, blanks dropped."""
    call = call or agent_call_structured
    prompt = QUESTIONER_PROMPT.format(
        notice=UNTRUSTED_NOTICE, inputs=inputs, max_questions=MAX_QUESTIONS,
    )
    result = await call(
        prompt,
        InterviewQuestions,
        allowed_tools=INTERVIEW_TOOLS,
        model=config.spec_model,
        effort=config.spec_effort,
        max_turns=INTERVIEW_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="interview",
    )
    questions = [q.strip() for q in result.questions if q and q.strip()]
    return questions[:MAX_QUESTIONS]


async def answer_questions(
    config, inputs: str, questions: list[str], *, call=None,
) -> list[InterviewAnswer]:
    """The answerer: a fresh call that sees the questions, the inputs and the repo."""
    call = call or agent_call_structured
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    prompt = ANSWERER_PROMPT.format(
        notice=UNTRUSTED_NOTICE, inputs=inputs, questions=numbered,
    )
    result = await call(
        prompt,
        InterviewAnswers,
        allowed_tools=INTERVIEW_TOOLS,
        model=config.spec_model,
        effort=config.spec_effort,
        max_turns=INTERVIEW_MAX_TURNS,
        cwd=str(config.project_dir),
        budget_label="interview",
    )
    return result.answers


async def run_self_interview(
    config, inputs: str, *, call=None,
) -> tuple[list[str], list[InterviewAnswer]]:
    """Questioner, then answerer — two calls, no human."""
    questions = await ask_questions(config, inputs, call=call)
    if not questions:
        log.info("Self-interview: the questioner found nothing left to ask")
        return [], []
    answers = await answer_questions(config, inputs, questions, call=call)
    log.info(
        "Self-interview: %d question(s), %d answer(s), %d assumption(s)",
        len(questions), len(answers), sum(a.is_assumption for a in answers),
    )
    return questions, answers


async def decide_research(
    config, summary: str, *, call=None,
) -> ResearchDecision:
    """One toolless call: is outside research needed, and why."""
    call = call or llm_call_structured
    prompt = RESEARCH_DECISION_PROMPT.format(
        notice=UNTRUSTED_NOTICE, summary=summary or "(no interview summary)",
    )
    return await call(
        prompt,
        ResearchDecision,
        model=config.spec_model,
        effort=config.spec_effort,
        budget_label="research",
    )
