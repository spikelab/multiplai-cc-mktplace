"""The one way this pipeline calls a model: `agent_call_structured`.

Copied from review_pipeline's `sdk.py` (itself a copy of buildme's pattern): a repo trust gate,
an explicit allow-list (run_agent denies its complement), a validated
pydantic answer, and exactly one re-ask when the answer does not parse.
Parsing the answer (`parse_answer`) and the reformat prompt
(`reformat_prompt`) are `multiplai_core.structured`; the call loop and its
retry policy stay here.

The re-ask for an answer that came back but does not parse is a reformat: a
no-tools, one-turn call that turns the text already paid for into the schema.
Re-running the whole prompt would repeat the reading (in the review pipeline a
re-ask took 68 turns and cost as much as the first attempt). A run that failed returned no
text, so its re-ask re-runs the prompt.

The explore, docs and trace stages get `Read`, `Grep` and `Glob` and nothing
else; the write stage gets no tools. The pipeline is read-only over the code
by construction, and no agent can reach the network: code does all fetching.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TypeVar

from multiplai_core.agent_runner import AgentRunError, AgentRunTimeout, run_agent
from multiplai_core.structured import parse_answer, reformat_prompt
from pydantic import BaseModel, ValidationError

from . import budget

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_CALL_TIMEOUT_S = 1200.0
DEFAULT_MAX_TURNS = 60
REFORMAT_MAX_TURNS = 1

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
EXPLORE_TOOLS = DOCS_TOOLS = TRACE_TOOLS = READ_ONLY_TOOLS
WRITE_TOOLS: list[str] = []

# Only allow-lists live here. `_run` passes no `disallowed_tools`, so
# run_agent denies the complement of the tools it opens (`multiplai_core.
# deny_list` over core's `TOOL_UNIVERSE`), including the Read it adds for an
# oversized prompt. Under bypassPermissions an allow-list alone removes nothing.


class AgentCallError(Exception):
    """An agent call failed, or its answer did not validate after the re-ask."""


class RepoTrustError(AgentCallError):
    """The user has not said the repositories read are trusted."""


def repo_is_trusted() -> bool:
    return os.environ.get("WALKTHROUGH_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


def require_trusted_repo() -> None:
    """Checked before every agent call.

    The agents read the repositories' own files, and those files become part of
    what the model acts on. A hostile repo can steer them. With read-only
    tools the damage is bounded, but the user still decides.
    """
    if not repo_is_trusted():
        raise RepoTrustError(
            "the walkthrough skill sends these repositories' contents to a model and lets it "
            "read their trees. Re-run with --trust-repo (or set WALKTHROUGH_TRUST_REPO=1) if you trust it."
        )


async def _run(prompt: str, *, allowed_tools: list[str], model: str | None, effort: str | None,
               max_turns: int, cwd: str | None, call_timeout: float, budget_label: str,
               run_label: str = "", read_roots: list[str] | None = None) -> str:
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
            label=run_label or budget_label or "walkthrough",
            component="walkthrough",
            # None installs no hook; a list makes run_agent deny reads outside those folders.
            read_roots=read_roots,
        )
    except AgentRunError as e:
        partial = getattr(e, "partial", None)
        if partial is not None:
            budget.record(partial.usage, label=budget_label)
        kind = "timeout" if isinstance(e, AgentRunTimeout) else e.reason
        raise AgentCallError(f"agent call failed ({kind})\n{e.stderr_tail}") from e
    budget.record(result.usage, label=budget_label)
    return result.text


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
    read_roots: list[str] | None = None,
) -> T:
    """Run an agent and parse its final message into *schema*.

    A failure gets one re-ask; a second failure raises `AgentCallError`. An
    answer that does not parse is re-asked as a reformat of that answer, with
    no tools. A failed run (timeout, CLI error) re-runs the prompt with the
    error appended.
    """
    name = budget_label or "agent"
    common = dict(model=model, effort=effort, cwd=cwd, call_timeout=call_timeout, budget_label=budget_label,
                  read_roots=read_roots)
    text: str | None = None
    try:
        text = await _run(prompt, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        return parse_answer(text, schema)
    except RepoTrustError:
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
    except RepoTrustError:
        raise
    except (ValidationError, ValueError, AgentCallError) as e:
        log.warning("%s: answer unusable (attempt 2/2): %s", name, str(e)[:500])
        raise AgentCallError(f"{name}: no valid answer after a re-ask: {e}") from e
