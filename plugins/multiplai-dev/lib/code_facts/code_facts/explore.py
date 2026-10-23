"""The explore loop: ask one agent per unit, then keep only facts whose citations pass.

The model call is a function the caller passes in, with the signature of the
pipelines' `agent_call_structured(prompt, schema, *, allowed_tools, model,
effort, max_turns, cwd, budget_label)`. This package never retries a call
itself: whatever retry policy the caller's function has is the one that runs.

Paths are resolved through a function the caller passes in:
`resolve(path, side) -> (repo_path, sha, rel_path) | None`. The citation check
is `multiplai_core.citations.check_quote`, which re-reads the lines from the
commit with `git show`, never from what the agent says it read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from multiplai_core.citations import check_quote, normalise_ws
from pydantic import BaseModel

from .models import Citation, Dropped, ExploreOutput, Fact, Side, Term, Unit

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

Resolve = Callable[[str, Side], "tuple[str, str, str] | None"]
CallFn = Callable[..., Awaitable[Any]]

# The reason a citation fails when `resolve` cannot place its path.
UNRESOLVED = "path names no known repo"
# The key `ask` stores a recoverable call failure under: `{CALL_ERROR: <first line>}`.
CALL_ERROR = "error"


async def bounded(
    items: Iterable[T], fn: Callable[[T], Awaitable[R]], limit: int
) -> list[R]:
    """Run fn over items, at most *limit* at a time, results in input order.

    A TaskGroup, so the first exception that escapes `fn` (trust, budget)
    cancels the rest instead of leaving them spending in the background.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def guarded(item: T) -> R:
        async with sem:
            return await fn(item)

    items = list(items)
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(guarded(item)) for item in items]
    except* Exception as group:
        # Re-raise the first underlying error, not the group, so callers can
        # catch BudgetExceededError / RepoTrustError by type.
        raise group.exceptions[0] from None
    return [t.result() for t in tasks]


def ws_path(path: str, root: Path) -> str:
    """The path relative to the agents' working directory, for whatever form the agent wrote."""
    p = path.strip().strip("`")
    for r in (str(root.resolve()), str(root)):
        r = r.rstrip("/") + "/"
        if p.startswith(r):
            p = p[len(r) :]
            break
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def check_citation(citation: Citation, resolve: Resolve) -> str | None:
    """None when the quote is inside the cited lines at its commit, else the reason it fails."""
    found = resolve(citation.path, citation.side)
    if found is None:
        # An empty quote is named first, whatever the path.
        return "empty quote" if not normalise_ws(citation.quote) else UNRESOLVED
    repo, sha, rel = found
    return check_quote(
        repo, sha, rel, citation.line_start, citation.line_end, citation.quote
    )


def citation_keeper(
    resolve: Resolve, *, root: Path | None = None, reasons: list[str] | None = None
) -> Callable[[Citation | None], Citation | None]:
    """A function that returns the citation (path made relative to *root*) when it passes, else None.

    Each failure's reason is appended to *reasons*.
    """

    def keep(c: Citation | None) -> Citation | None:
        if c is None:
            return None
        if root is not None:
            c = c.model_copy(update={"path": ws_path(c.path, root)})
        reason = check_citation(c, resolve)
        if reason is None:
            return c
        if reasons is not None:
            reasons.append(reason)
        return None

    return keep


def unit_key(unit: Unit) -> str:
    return f"unit:{unit.id}"


def explore_budget_label(key: str) -> str:
    """The `budget_label` of an explore task: `explore:` and the key's kind (`unit`, `boundary`, ...)."""
    return f"explore:{key.split(':')[0]}"


def canonical_digest(value: Any) -> str:
    """The sha256 hex of `value` as canonical JSON: the same for equal values, whatever their key order."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ask(
    key: str,
    prompt: str,
    call: CallFn,
    answers: dict[str, dict],
    *,
    call_kwargs: Mapping[str, Any],
    errors: list[str],
    recoverable: tuple[type[BaseException], ...] = (Exception,),
    fatal: tuple[type[BaseException], ...] = (),
    progress: Callable[[str], None] | None = None,
    schema: type[BaseModel] = ExploreOutput,
    budget_label: Callable[[str], str] = explore_budget_label,
    encode: Callable[[Any], dict] = BaseModel.model_dump,
) -> None:
    """Ask one task and store its answer in *answers* under *key*, as it returns.

    The call gets *schema* and `budget_label(key)`, which replaces any
    `budget_label` in *call_kwargs*; *encode* turns what it returns into the
    stored answer. An exception from the call or from *encode* is handled
    alike: one in *fatal* propagates, one in *recoverable* is logged, stored as
    `{"error": ...}` and appended to *errors*, and anything else propagates.
    """
    label = budget_label(key)
    try:
        answer = encode(
            await call(prompt, schema, **{**call_kwargs, "budget_label": label})
        )
    except fatal:
        raise
    except recoverable as e:
        log.error("%s (%s) failed", label, key, exc_info=True)
        first = (str(e).splitlines() or [type(e).__name__])[0][:200]
        answers[key] = {CALL_ERROR: first}
        errors.append(f"explore {key}: {first}")
    else:
        answers[key] = answer
    if progress:
        progress(f"  explore {key}: {len(answers[key].get('facts', []))} facts")


async def run_tasks(
    tasks: list[tuple[str, str]],
    call: CallFn,
    answers: dict[str, dict],
    *,
    concurrency: int,
    on_answer: Callable[[], None] | None = None,
    **ask_kwargs: Any,
) -> None:
    """Ask every `(key, prompt)` task whose answer is not already in *answers*.

    Skipping answered keys is what lets a resumed run repeat no paid work.
    *on_answer* runs after each answer (or failure) is stored, so a caller can
    save its checkpoint then.
    """

    async def one(task: tuple[str, str]) -> None:
        await ask(task[0], task[1], call, answers, **ask_kwargs)
        if on_answer is not None:
            on_answer()

    await bounded([t for t in tasks if t[0] not in answers], one, concurrency)


def reask_units(
    units: list[Unit],
    uncovered: set[str],
    answers: dict[str, dict],
    prompt_for: Callable[[Unit, list[str]], str],
) -> list[tuple[str, str]]:
    """One re-ask per unit that left files uncovered, naming them. Never a second one."""
    retry = []
    for u in units:
        miss = [f for f in u.files if f in uncovered]
        key = f"{unit_key(u)}:reask"
        if miss and key not in answers:
            retry.append((key, prompt_for(u, miss)))
    return retry


@dataclass
class Gated:
    facts: list[Fact] = field(
        default_factory=list
    )  # with ids f1.. and citation ids c1..
    purposes: dict[str, str] = field(default_factory=dict)  # path -> purpose
    interfaces: list[str] = field(default_factory=list)  # fact ids
    gotchas: list[str] = field(default_factory=list)  # fact ids
    conventions: list[str] = field(default_factory=list)  # fact ids
    terms: list[Term] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)


def gate_answers(
    answers: dict[str, dict],
    keep: Callable[[Citation | None], Citation | None],
    *,
    conventions: bool = False,
) -> Gated:
    """Rebuild the gated facts from every stored answer, in task order. Deterministic.

    A fact with no passing citation is dropped and recorded. Conventions are
    kept only when *conventions* is true, so a caller that never asks for them
    gets the same facts whatever the agent returns.
    """
    facts: list[Fact] = []
    purposes: dict[str, str] = {}
    picked: dict[str, list[int]] = {"interface": [], "gotcha": [], "convention": []}
    terms: dict[str, Term] = {}
    dropped: list[Dropped] = []

    for key, raw in answers.items():
        if CALL_ERROR in raw:
            continue
        out = ExploreOutput.model_validate(raw)
        buckets = [
            ("fact", out.facts),
            ("interface", out.interfaces),
            ("gotcha", out.gotchas),
        ]
        if conventions:
            buckets.append(("convention", out.conventions))
        for bucket, items in buckets:
            for f in items:
                good = [c for c in (keep(c) for c in f.citations) if c is not None]
                if not good:
                    dropped.append(
                        Dropped(
                            stage="explore",
                            what=f.claim[:300],
                            reason="no citation passed",
                        )
                    )
                    continue
                facts.append(
                    Fact(claim=f.claim, citations=good, source=f"{key}/{bucket}")
                )
                if bucket in picked:
                    picked[bucket].append(len(facts) - 1)
        for fp in out.files:
            c = keep(fp.citation)
            if c is not None:
                purposes.setdefault(c.path, fp.purpose)
                facts.append(
                    Fact(
                        claim=f"{c.path.split('/')[-1]}: {fp.purpose}",
                        citations=[c],
                        source=f"{key}/file",
                    )
                )
        for t in out.terms:
            c = keep(t.citation)
            if c is None:
                dropped.append(
                    Dropped(
                        stage="explore",
                        what=f"term {t.term}",
                        reason="no citation passed",
                    )
                )
                continue
            terms.setdefault(
                t.term.lower(), Term(term=t.term, meaning=t.meaning, citation=c)
            )

    n = 0
    for i, f in enumerate(facts):
        f.id = f"f{i + 1}"
        for c in f.citations:
            n += 1
            c.id = f"c{n}"
    return Gated(
        facts=facts,
        purposes=purposes,
        interfaces=[facts[i].id for i in picked["interface"]],
        gotchas=[facts[i].id for i in picked["gotcha"]],
        conventions=[facts[i].id for i in picked["convention"]],
        terms=list(terms.values()),
        dropped=dropped,
    )
