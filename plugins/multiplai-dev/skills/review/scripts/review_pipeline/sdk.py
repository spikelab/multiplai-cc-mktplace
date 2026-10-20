"""The ways this pipeline calls a model: `agent_call_structured`, and `agent_call_text` for a plain report.

Copies the pattern of buildme's `sdk.py`, not the module: a repo trust gate,
an explicit allow-list (run_agent denies its complement), a validated
pydantic answer, and exactly one re-ask when the answer does not parse.
Parsing the answer (`parse_answer`) and the reformat prompt
(`reformat_prompt`) are `multiplai_core.structured`; the call loop and its
retry policy stay here.

The re-ask for an answer that came back but does not parse is a reformat: a
no-tools, one-turn call that turns the text already paid for into the schema.
Re-running the whole prompt would repeat the review (a finder re-ask took 68
turns and cost as much as the first attempt). A run that failed returned no
text, so its re-ask re-runs the prompt.

`agent_call_text` is the piece and path finders' report call: the same trust
gate, budget check and usage recording, and the answer returned as text, with
no parsing and no re-ask (its convert call is an `agent_call_structured`).

Every stage gets `Read`, `Grep` and `Glob`; finders and verifiers also get
`WebFetch` and `WebSearch`, to look up how a dependency behaves at the version
the repository pins. No stage can run a command or write, so the pipeline is
read-only over the reviewed code by construction. `read_roots=[cwd]` makes
multiplai-core deny any `Read`, `Grep` or `Glob` outside the stage's snapshot.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Iterator, TypeVar

from pydantic import BaseModel, ValidationError

from multiplai_core.agent_runner import AgentRunError, AgentRunTimeout, is_usage_limit, run_agent
from multiplai_core.structured import parse_answer, reformat_prompt

from . import budget

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_CALL_TIMEOUT_S = 1200.0
DEFAULT_MAX_TURNS = 60
REFORMAT_MAX_TURNS = 1

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
WEB_TOOLS = ["WebFetch", "WebSearch"]
FINDER_TOOLS = VERIFIER_TOOLS = READ_ONLY_TOOLS + WEB_TOOLS
MERGER_TOOLS = READ_ONLY_TOOLS  # deciding whether two findings are one needs no web

# Only allow-lists live here. `_run` passes no `disallowed_tools`, so
# run_agent denies the complement of the tools it opens (`multiplai_core.
# deny_list` over core's `TOOL_UNIVERSE`), including the Read it adds for an
# oversized prompt. Under bypassPermissions an allow-list alone removes nothing.


@dataclass
class CallRecord:
    """What the agent runs inside one `recording()` block did: every attempt's
    tool calls in order (`multiplai_core.ToolCall`), turns and cost."""
    tool_calls: list = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0


_recorder: contextvars.ContextVar[CallRecord | None] = contextvars.ContextVar("review_call_record", default=None)


@contextlib.contextmanager
def recording() -> Iterator[CallRecord]:
    """Collect what every `_run` in this block does, the re-ask included.

    A ContextVar, so concurrent stage tasks each fill their own record. The
    stages open one around each `agent_call_structured`, which keeps that
    function's signature (and the tests that monkeypatch it) unchanged.
    """
    record = CallRecord()
    token = _recorder.set(record)
    try:
        yield record
    finally:
        _recorder.reset(token)


def _note(result) -> None:
    record = _recorder.get()
    if record is None or result is None:
        return
    record.tool_calls.extend(getattr(result, "tool_calls", ()) or ())
    record.turns += int(getattr(result, "turns", 0) or 0)
    record.cost_usd += float(getattr(getattr(result, "usage", None), "cost_usd", 0.0) or 0.0)


class AgentCallError(Exception):
    """An agent call failed, or its answer did not validate after the re-ask."""


class RepoTrustError(AgentCallError):
    """The user has not said the reviewed repository is trusted."""


class UsageLimitError(AgentCallError):
    """The Claude usage limit (session, weekly, ...) was hit, so no call can succeed until it resets.

    Never retried or re-asked. Like `RepoTrustError`, every stage lets it
    escape; the orchestrator then saves the checkpoint, as for a budget stop,
    so `resume` after the reset asks only for what is missing. `limit` and
    `resets` are what multiplai-core's `UsageLimitError` read from the CLI's
    message (`"session"`, `"9:40am (UTC)"`), or `""`.
    """

    def __init__(self, message: str, *, limit: str = "", resets: str = "") -> None:
        super().__init__(message)
        self.limit = limit
        self.resets = resets


def _usage_limit(e: AgentRunError) -> UsageLimitError:
    limit, resets = getattr(e, "limit", "") or "", getattr(e, "resets", "") or ""
    message = f"the Claude {limit + ' ' if limit else ''}usage limit was hit"
    return UsageLimitError(message + (f"; it resets {resets}" if resets else ""), limit=limit, resets=resets)


def repo_is_trusted() -> bool:
    return os.environ.get("REVIEW_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


def require_trusted_repo() -> None:
    """Checked before every agent call.

    The agents read the repository's own files, and those files become part of
    what the model acts on. A hostile repo can steer them. With read-only
    tools the damage is bounded, but the user still decides.
    """
    if not repo_is_trusted():
        raise RepoTrustError(
            "the review skill sends this repository's contents to a model and lets it read "
            "the tree. Re-run with --trust-repo (or set REVIEW_TRUST_REPO=1) if you trust it."
        )


async def _run(prompt: str, *, allowed_tools: list[str], model: str | None, effort: str | None,
               max_turns: int, cwd: str | None, call_timeout: float, budget_label: str,
               run_label: str = "") -> str:
    require_trusted_repo()
    budget.check(stage=budget_label)
    try:
        result = await run_agent(
            prompt,
            allowed_tools=list(allowed_tools),
            max_turns=max_turns,
            model=model,
            effort=effort,
            cwd=cwd,
            timeout_s=call_timeout,
            label=run_label or budget_label or "review",
            component="review",
            # Reads stop at the snapshot; with no cwd there is nothing to read.
            read_roots=[cwd] if cwd is not None else [],
        )
    except AgentRunError as e:
        partial = getattr(e, "partial", None)
        if partial is not None:
            budget.record(partial.usage, label=budget_label)
            _note(partial)
        if is_usage_limit(e):
            raise _usage_limit(e) from e
        kind = "timeout" if isinstance(e, AgentRunTimeout) else e.reason
        raise AgentCallError(f"agent call failed ({kind})\n{e.stderr_tail}") from e
    budget.record(result.usage, label=budget_label)
    _note(result)
    return result.text


async def agent_call_text(
    prompt: str,
    *,
    allowed_tools: list[str],
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    cwd: str | os.PathLike | None = None,
    budget_label: str = "",
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
) -> str:
    """Run an agent and return its final message as it is. A failed run raises `AgentCallError`."""
    return await _run(prompt, allowed_tools=allowed_tools, model=model, effort=effort, max_turns=max_turns,
                      cwd=None if cwd is None else str(cwd), call_timeout=call_timeout, budget_label=budget_label)


async def agent_call_structured(
    prompt: str,
    schema: type[T],
    *,
    allowed_tools: list[str],
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    cwd: str | None = None,
    budget_label: str = "",
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
) -> T:
    """Run an agent and parse its final message into *schema*.

    A failure gets one re-ask; a second failure raises `AgentCallError`. A
    hit usage limit (`UsageLimitError`) and a trust stop are never re-asked. An
    answer that does not parse is re-asked as a reformat of that answer, with
    no tools. A failed run (timeout, CLI error) re-runs the prompt with the
    error appended.
    """
    name = budget_label or "agent"
    common = dict(model=model, effort=effort, cwd=cwd, call_timeout=call_timeout, budget_label=budget_label)
    text: str | None = None
    try:
        text = await _run(prompt, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        return parse_answer(text, schema)
    except (RepoTrustError, UsageLimitError):
        raise
    except (ValidationError, ValueError, AgentCallError) as e:
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
    except (RepoTrustError, UsageLimitError):
        raise
    except (ValidationError, ValueError, AgentCallError) as e:
        log.warning("%s: answer unusable (attempt 2/2): %s", name, str(e)[:500])
        raise AgentCallError(f"{name}: no valid answer after a re-ask: {e}") from e
