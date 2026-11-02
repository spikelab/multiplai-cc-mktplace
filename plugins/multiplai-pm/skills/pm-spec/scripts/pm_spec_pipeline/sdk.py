"""The one way pm-spec calls a model: `multiplai_core.run_agent`, its trust gate and its budget.

Restated from buildme's `sdk.py` and spec-extract's, not imported: a plugin
cannot import another plugin's code once installed.

- **Every call goes through `run`**, which calls `run_agent` (looked up on this
  module at call time, so tests replace it with a stub) and records the spend
  into the run's ledger (`multiplai_core.budget`).
- **Trust gate.** A call that holds tools reads the repository, whose files
  become part of what the model acts on. It runs only when the user said the
  repository is trusted: `--trust-repo` or `PM_SPEC_TRUST_REPO=1`
  (`RepoTrustError` otherwise, exit 2). A call with no tools needs no trust.
- **Read-only.** The interview agents and the consistency check get `Read`,
  `Glob` and `Grep` only (`READ_ONLY_TOOLS`); only the prototype agent gets
  `Write`, and its writes are checked against its folder in code.
- **Structured answers.** An answer that does not parse is sent back once to
  be reformatted (no tools, one turn); a second failure is `CallFailed`.
- **Budget.** Each call is checked against the ledger first
  (`BudgetExceededError`), and its usage recorded as it returns, failed calls
  included.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

from multiplai_core import budget as core_budget
from multiplai_core.agent_runner import AgentRunError, AgentRunTimeout, run_agent
from multiplai_core.budget import BudgetExceededError, SpendLedger
from multiplai_core.structured import parse_answer, reformat_prompt
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TRUST_ENV = "PM_SPEC_TRUST_REPO"
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
DEFAULT_MAX_USD = 20.0
DEFAULT_TIMEOUT_S = 900.0
AGENT_MAX_TURNS = 30

TEXT_SYSTEM_PROMPT = (
    "You are a product specification writer. Your ONLY job is to write the "
    "requested document from the context given in the prompt. Output the "
    "document directly; do not use tools or ask for more information. Output "
    "ONLY the requested content."
)

TRUST_MESSAGE = (
    "Not run: this step lets a model read the repository, so its files become part of what "
    f"the model acts on. If you trust the repository, re-run with --trust-repo (or set {TRUST_ENV}=1)."
)

__all__ = [
    "READ_ONLY_TOOLS",
    "BudgetExceededError",
    "CallFailed",
    "RepoTrustError",
    "agent_structured",
    "agent_text",
    "repo_is_trusted",
    "spend_marker",
    "start_budget",
    "text_call",
    "text_structured",
    "track_spend",
]


class CallFailed(Exception):
    """A model call failed, or its answer did not parse after the reformat."""


class RepoTrustError(Exception):
    """A tool-holding call on a repository the user has not marked trusted."""


@dataclass
class PmSpecBudget(SpendLedger):
    max_usd: float | None = DEFAULT_MAX_USD
    run_name = "pm-spec"


@dataclass
class _ProcessSpend:
    cost_usd: float = 0.0
    tokens: int = 0


_process = _ProcessSpend()

# The calls of one asyncio task, when it asked (`track_spend`): the component
# check runs its agents in parallel and reports each one's cost.
_task_spend: ContextVar[_ProcessSpend | None] = ContextVar(
    "pm_spec_task_spend", default=None
)


def track_spend() -> _ProcessSpend:
    """From now on, in this task (and tasks it starts), add each call's cost
    to the returned record as well."""
    spend = _ProcessSpend()
    _task_spend.set(spend)
    return spend


def start_budget(max_usd: float | None, saved: dict | None) -> SpendLedger:
    """The run's ledger in the current context, holding the spend *saved* in state."""
    return core_budget.start(max_usd, saved, ledger_cls=PmSpecBudget)


def ledger() -> SpendLedger:
    return core_budget.get(ledger_cls=PmSpecBudget)


def spend_marker() -> str:
    """`SPEND:<usd>:<tokens>`: what this process's calls cost."""
    return f"SPEND:{_process.cost_usd:.6f}:{_process.tokens}"


def _record(usage, label: str) -> None:
    ledger().record(usage, label=label)
    try:
        cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
        tokens = sum(
            int(getattr(usage, f, 0) or 0)
            for f in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        )
        for spend in (_process, _task_spend.get()):
            if spend is not None:
                spend.cost_usd += cost
                spend.tokens += tokens
    except Exception as e:  # accounting never breaks a run
        log.warning("Process spend accounting failed (ignored): %s", e)


def repo_is_trusted() -> bool:
    return os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes")


@dataclass
class Answer:
    text: str
    files_changed: list[str]


async def run(
    prompt: str,
    *,
    label: str,
    tools: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
    max_turns: int = 1,
    system_prompt: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Answer:
    """One model call. Tools need a trusted repository; spend is recorded either way."""
    if tools and not repo_is_trusted():
        raise RepoTrustError(TRUST_MESSAGE)
    ledger().check(label)
    log.info("START call=%s tools=%s model=%s", label, tools or [], model or "default")
    try:
        result = await run_agent(
            prompt,
            system_prompt=system_prompt,
            allowed_tools=list(tools or []) or None,
            max_turns=max_turns,
            model=model,
            effort=effort,
            cwd=cwd,
            timeout_s=timeout_s,
            label=label,
            component="pm-spec",
        )
    except AgentRunError as e:
        partial = getattr(e, "partial", None)
        if partial is not None:
            _record(partial.usage, label)
        kind = "timeout" if isinstance(e, AgentRunTimeout) else e.reason
        raise CallFailed(f"{label}: model call failed ({kind})") from e
    _record(result.usage, label)
    log.info("DONE call=%s chars=%d", label, len(result.text or ""))
    return Answer(result.text or "", list(result.files_changed or []))


async def _structured(prompt: str, schema: type[T], **kwargs) -> T:
    label = kwargs["label"]
    answer = await run(prompt, **kwargs)
    try:
        return parse_answer(answer.text, schema)
    except (ValidationError, ValueError) as e:
        first = e
        log.warning(
            "%s: answer unusable, asking for a reformat: %s", label, str(e)[:300]
        )
    try:
        again = await run(
            reformat_prompt(answer.text, schema, first),
            label=f"{label}:reformat",
            model=kwargs.get("model"),
            effort=kwargs.get("effort"),
        )
        return parse_answer(again.text, schema)
    except (ValidationError, ValueError) as e:
        raise CallFailed(f"{label}: no valid answer after a reformat: {e}") from e


async def text_call(prompt: str, *, config, step: str, label: str) -> str:
    """A call with no tools that returns a document's text."""
    answer = await run(
        prompt,
        label=label,
        model=config.model_for(step),
        effort=config.effort_for(step),
        system_prompt=TEXT_SYSTEM_PROMPT,
    )
    return answer.text


async def text_structured(
    prompt: str, schema: type[T], *, config, step: str, label: str
) -> T:
    """A call with no tools whose answer is JSON of *schema*."""
    return await _structured(
        prompt,
        schema,
        label=label,
        model=config.model_for(step),
        effort=config.effort_for(step),
    )


async def agent_structured(
    prompt: str, schema: type[T], *, config, step: str, label: str, cwd: str
) -> T:
    """A read-only agent (`READ_ONLY_TOOLS`) whose answer is JSON of *schema*."""
    return await _structured(
        prompt,
        schema,
        label=label,
        model=config.model_for(step),
        effort=config.effort_for(step),
        tools=list(READ_ONLY_TOOLS),
        cwd=cwd,
        max_turns=AGENT_MAX_TURNS,
    )


async def agent_text(
    prompt: str, *, config, step: str, label: str, tools: list[str], cwd: str
) -> Answer:
    """An agent holding *tools*; returns its text and the files it reports writing."""
    return await run(
        prompt,
        label=label,
        model=config.model_for(step),
        effort=config.effort_for(step),
        tools=tools,
        cwd=cwd,
        max_turns=AGENT_MAX_TURNS,
    )
