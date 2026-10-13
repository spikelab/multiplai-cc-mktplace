"""The one way spec-extract calls a model, its retry policy, its trust gate and its budget.

Copied in form from review's `sdk.py`, not imported (plan, Decided 9 and 16):
skills do not import each other in an installed plugin, and the 2026-10-02
decision keeps one model-call wrapper per pipeline, with no shared one in
multiplai-core.

- **Trust gate.** The agents read the target's own files, which become part of
  what the model acts on. The user says the repository is trusted with
  `--trust-repo` or `SPEC_EXTRACT_TRUST_REPO=1`; the run checks it before its
  first model call (exit 4), and `_run` checks it again before every call.
- **Tools.** Explore agents get `Read`, `Glob` and `Grep` only, on the
  snapshot. No agent can run a command or write (plan, Constraints).
- **Retry policy.** A call whose answer does not parse is re-asked once as a
  reformat of the text already paid for (no tools, one turn); a call that
  failed outright is re-run once with the error appended. A second failure
  raises `CallFailed`, which the dispatch rule stores as an error answer.
- **Budget.** Every call is checked against the run folder's ledger first and
  recorded into it as it returns (`multiplai_core.budget`, a ContextVar).
  `BudgetExceededError` is not a `CallFailed`: it stops the run (exit 3).
- **Models** (plan, Decided 18): `multiplai.conf` keys
  `spec_extract_explore_model` (and, from the write step, `spec_extract_write_model`),
  resolved through `multiplai_core.resolve_model_spec`. Unset means the
  session's model.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TypeVar

from multiplai_core import budget as core_budget
from multiplai_core import resolve_model_spec
from multiplai_core.agent_runner import AgentRunError, AgentRunTimeout, run_agent
from multiplai_core.budget import BudgetExceededError, SpendLedger
from multiplai_core.env import load_multiplai_conf
from multiplai_core.stage_config import conf_value
from multiplai_core.structured import parse_answer, reformat_prompt
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TRUST_ENV = "SPEC_EXTRACT_TRUST_REPO"
DEFAULT_MAX_USD = 80.0  # per run folder, across every invocation that resumes it (plan, Decided 7)
DEFAULT_CALL_TIMEOUT_S = 1200.0
EXPLORE_MAX_TURNS = 30  # buildme's codebase analysis uses the same (`code_analysis.py`, MAX_TURNS)
REFORMAT_MAX_TURNS = 1
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]

TRUST_MESSAGE = (
    "Not run: spec-extract sends this repository's contents to a model and lets it read the "
    f"tree. If you trust the repository, re-run with --trust-repo (or set {TRUST_ENV}=1)."
)

__all__ = ["BudgetExceededError", "CallFailed", "DEFAULT_MAX_USD", "RepoTrustError", "SpecExtractBudget",
           "TRUST_MESSAGE", "agent_call_structured", "model_for", "repo_is_trusted", "require_trusted_repo",
           "start_budget"]


class CallFailed(Exception):
    """A model call failed, or its answer did not validate after the re-ask."""


class RepoTrustError(Exception):
    """The user has not said the repository is trusted. Not a `CallFailed`: it stops the run."""


@dataclass
class SpecExtractBudget(SpendLedger):
    max_usd: float | None = DEFAULT_MAX_USD
    run_name = "spec-extract"


def start_budget(max_usd: float | None, saved: dict | None) -> SpendLedger:
    """The run folder's ledger in the current context, holding the spend *saved* in state.

    The ceiling is this invocation's `--max-usd`; the spend is every earlier
    invocation's, so re-running never gets a fresh cap (plan, Decided 19).
    """
    return core_budget.start(max_usd, saved, ledger_cls=SpecExtractBudget)


def repo_is_trusted() -> bool:
    return os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes")


def require_trusted_repo() -> None:
    if not repo_is_trusted():
        raise RepoTrustError(TRUST_MESSAGE)


def model_for(step: str) -> str | None:
    """`spec_extract_<step>_model` from multiplai.conf, resolved; None means the session's model."""
    spec = resolve_model_spec([conf_value(load_multiplai_conf(), f"spec_extract_{step}_model")])
    if spec is None:
        return None
    return spec.model if spec.is_anthropic else spec.qualified


async def _run(prompt: str, *, allowed_tools: list[str], model: str | None, effort: str | None,
               max_turns: int, cwd: str | None, call_timeout: float, budget_label: str,
               run_label: str = "") -> str:
    require_trusted_repo()
    core_budget.check(budget_label)
    try:
        result = await run_agent(
            prompt,
            allowed_tools=list(allowed_tools),
            max_turns=max_turns,
            model=model,
            effort=effort,
            cwd=cwd,
            timeout_s=call_timeout,
            label=run_label or budget_label or "spec-extract",
            component="spec-extract",
        )
    except AgentRunError as e:
        partial = getattr(e, "partial", None)
        if partial is not None:
            core_budget.record(partial.usage, label=budget_label)
        kind = "timeout" if isinstance(e, AgentRunTimeout) else e.reason
        raise CallFailed(f"agent call failed ({kind})\n{e.stderr_tail}") from e
    core_budget.record(result.usage, label=budget_label)
    return result.text


async def agent_call_structured(
    prompt: str,
    schema: type[T],
    *,
    allowed_tools: list[str],
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = EXPLORE_MAX_TURNS,
    cwd: str | None = None,
    budget_label: str = "",
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
) -> T:
    """Run an agent and parse its final message into *schema* (the `CallFn` code_facts calls).

    One re-ask on failure, then `CallFailed`. `RepoTrustError` and
    `BudgetExceededError` are never caught here.
    """
    name = budget_label or "agent"
    common = dict(model=model, effort=effort, cwd=cwd, call_timeout=call_timeout, budget_label=budget_label)
    text: str | None = None
    try:
        text = await _run(prompt, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        return parse_answer(text, schema)
    except (ValidationError, ValueError, CallFailed) as e:
        first_error = e
        log.warning("%s: answer unusable (attempt 1/2): %s", name, str(e)[:500])

    try:
        if text is None:
            retry = (
                f"{prompt}\n\n---\nYour previous answer was rejected by a program: {str(first_error)[:2000]}\n"
                f"Return ONLY one JSON object matching this schema:\n"
                f"{json.dumps(schema.model_json_schema(), indent=1)}\n"
            )
            text = await _run(retry, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        else:
            text = await _run(reformat_prompt(text, schema, first_error), allowed_tools=[],
                              max_turns=REFORMAT_MAX_TURNS, run_label=f"{name}:reformat", **common)
        return parse_answer(text, schema)
    except (ValidationError, ValueError, CallFailed) as e:
        log.warning("%s: answer unusable (attempt 2/2): %s", name, str(e)[:500])
        raise CallFailed(f"{name}: no valid answer after a re-ask: {e}") from e
