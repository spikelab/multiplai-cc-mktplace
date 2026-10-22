"""Item 7's dispatch rule for the group and write calls.

`code_facts.ask` hard-codes the explore schema and budget label, so group and
write go through `ask_one` instead, under the same rules as explore:

- A key is asked when it has no answer, or an `{"error": ...}` answer, and
  fewer than 2 attempts. Round 1 asks every key the rule allows; round 2 asks
  those that returned an error.
- Before each call: `ledger.check()`, then delete the key's error answer,
  count the attempt, save. As the call returns: one save holding its answer
  (or its error) and the ledger.
- `sdk.CallFailed` is stored as an error answer. A budget stop
  (`BudgetExceededError`) takes back the attempt it counted, saves, and never
  escapes `code_facts.bounded`, so calls in flight finish and are saved; then
  the run exits 3. `sdk.BackendUnavailable` and `sdk.RepoTrustError` are taken
  back the same way, then raised (exit 1 or 4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from code_facts import CallFn, bounded
from multiplai_core.budget import BudgetExceededError, SpendLedger
from pydantic import BaseModel

from . import sdk
from .explore import CONCURRENCY, MAX_ATTEMPTS, BudgetStopped


def eligible(key: str, answers: dict[str, dict], attempts: dict[str, int]) -> bool:
    has_answer = key in answers and "error" not in answers[key]
    return not has_answer and attempts.get(key, 0) < MAX_ATTEMPTS


async def ask_one(
    key: str,
    prompt: str,
    schema: type[BaseModel],
    label: str,
    call: CallFn,
    answers: dict[str, dict],
    attempts: dict[str, int],
    ledger: SpendLedger,
    save: Callable[[], None],
    *,
    call_kwargs: dict[str, Any],
) -> Exception | None:
    """Ask *key* once. None when the call returned (its answer or error is saved);
    otherwise the exception that stopped it, with the attempt taken back and saved."""
    try:
        ledger.check(label)
    except BudgetExceededError as e:
        return e
    answers.pop(
        key, None
    )  # only an error answer can be here: an answered key is not eligible
    before = attempts.get(key)
    attempts[key] = (before or 0) + 1
    save()
    try:
        out = await call(prompt, schema, budget_label=label, **call_kwargs)
        answers[key] = out.model_dump()
    except sdk.CallFailed as e:
        answers[key] = {"error": (str(e).splitlines() or [type(e).__name__])[0][:200]}
    except (BudgetExceededError, sdk.BackendUnavailable, sdk.RepoTrustError) as e:
        if before is None:
            attempts.pop(key, None)
        else:
            attempts[key] = before
        save()
        return e
    save()
    return None


async def dispatch(
    tasks: list[tuple[str, str, str]],
    schema: type[BaseModel],
    call: CallFn,
    answers: dict[str, dict],
    attempts: dict[str, int],
    ledger: SpendLedger,
    save: Callable[[], None],
    *,
    call_kwargs: dict[str, Any],
) -> None:
    """Ask every `(key, prompt, label)` the rule allows, in at most two rounds.

    Raises the first `BackendUnavailable` or `RepoTrustError`, else `BudgetStopped`
    when a task was stopped by the budget, after the round's calls in flight finish.
    """

    async def one(task: tuple[str, str, str]) -> Exception | None:
        key, prompt, label = task
        return await ask_one(
            key,
            prompt,
            schema,
            label,
            call,
            answers,
            attempts,
            ledger,
            save,
            call_kwargs=call_kwargs,
        )

    for _ in range(MAX_ATTEMPTS):
        todo = [t for t in tasks if eligible(t[0], answers, attempts)]
        if not todo:
            return
        stops = [e for e in await bounded(todo, one, CONCURRENCY) if e is not None]
        cannot_run = [e for e in stops if not isinstance(e, BudgetExceededError)]
        if cannot_run:
            raise cannot_run[0]
        if stops:
            raise BudgetStopped(str(stops[0]))


def any_eligible(
    keys: list[str], answers: dict[str, dict], attempts: dict[str, int]
) -> bool:
    return any(eligible(k, answers, attempts) for k in keys)
