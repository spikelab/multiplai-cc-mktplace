"""The group step: one call with no tools decides the capabilities (plan, item 14).

Input: the gated facts, rebuilt from the stored explore answers in unit order
(never read from `facts.json`), and the saved symbol list. Output:
`capabilities: [{name, summary, symbols[]}]`, asked under item 7's dispatch
rule with key `group` (`calls.py`), with the write model (Decided 18).

The **Python gate** is a pure function of the saved answer and the symbol list,
so every run computes the same capabilities from the same state:

- names are kebab-case, cut to 60 characters before any suffix this gate
  adds, and unique (`-2`, `-3` … in answer order);
- every public symbol is in exactly one capability: a symbol the answer does
  not place goes to `unassigned`, last; one placed twice stays in the first;
  a name that is not a public symbol of this target is dropped, and so is a
  capability left with no symbol;
- at most 40 symbols per capability (judgment): a larger one is split by
  module, `<name>-<module>`, and a module over 40 into `-1`, `-2` … chunks.

`capabilities.json` is written from that gate on every run that reaches the
write step (rows R20, R21, R24); the write step takes the list from the gate,
never from the file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from code_facts import UNTRUSTED_RULES, CallFn, Gated, schema_json
from multiplai_core.budget import SpendLedger
from pydantic import BaseModel, ConfigDict, Field

from . import calls, sdk
from . import report as report_mod
from . import state as state_mod
from .explore import label
from .scope import Symbol
from .state import State
from .target import SNAPSHOT_DIR

KEY = "group"
MAX_SYMBOLS = 40  # judgment (plan, Unverified)
NAME_MAX = 60
UNASSIGNED = "unassigned"
CAPABILITIES_FILE = "capabilities.json"
MAX_TURNS = 1  # no tools: the answer is the first turn
NOT_GROUPED = report_mod.Section(
    "Not grouped", ["- not grouped: group gave no answer in 2 attempts"]
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CapabilityOut(_Model):
    name: str
    summary: str = ""
    symbols: list[str] = []


class GroupOutput(_Model):
    # Required and non-empty: an answer without it does not parse, so the wrapper sends it
    # back to be reformatted and then fails the attempt, instead of storing it as an answer.
    capabilities: list[CapabilityOut] = Field(min_length=1)


class Capability(BaseModel):
    name: str
    summary: str
    symbols: list[str]  # symbol ids, `path::name`, in the scope's order


@dataclass
class Grouping:
    capabilities: list[Capability] = field(default_factory=list)
    unassigned: list[str] = field(
        default_factory=list
    )  # symbol ids the answer did not place
    dropped: list[str] = field(default_factory=list)


def one_line(text: str) -> str:
    """Model text on one line (render also escapes its `#`, so it cannot start a heading)."""
    return " ".join(str(text).split())


def md(text: str) -> str:
    """Model text that cannot be read as a heading or a requirement."""
    return text.replace("#", "\\#")


def symbol_id(s: Symbol) -> str:
    return f"{s.path}::{s.name}"


def symbol_line(s: Symbol) -> str:
    return f"- {symbol_id(s)} ({s.kind}, line {s.line})"


def kebab(name: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:NAME_MAX].strip("-")
        or "capability"
    )


def _module(path: str) -> str:
    p = PurePosixPath(path)
    return kebab(p.parent.name if p.stem == "__init__" and p.parent.name else p.stem)


def _split(cap: Capability, path_of: dict[str, str]) -> list[Capability]:
    if len(cap.symbols) <= MAX_SYMBOLS:
        return [cap]
    by_module: dict[str, list[str]] = {}
    for sid in cap.symbols:
        by_module.setdefault(path_of[sid], []).append(sid)
    out = []
    for path, ids in by_module.items():
        base = f"{cap.name}-{_module(path)}"
        if len(ids) <= MAX_SYMBOLS:
            out.append(Capability(name=base, summary=cap.summary, symbols=ids))
            continue
        for n, start in enumerate(range(0, len(ids), MAX_SYMBOLS), 1):
            out.append(
                Capability(
                    name=f"{base}-{n}",
                    summary=cap.summary,
                    symbols=ids[start : start + MAX_SYMBOLS],
                )
            )
    return out


def gate(answer: dict, symbols: list[Symbol]) -> Grouping:
    """The capabilities from a group answer: a pure function of the answer and the symbols."""
    order: dict[str, int] = {}  # a name defined twice is one symbol, at its first def
    for i, s in enumerate(symbols):
        order.setdefault(symbol_id(s), i)
    path_of = {symbol_id(s): s.path for s in symbols}
    out = GroupOutput.model_validate(answer)
    g = Grouping()
    placed: set[str] = set()
    raw: list[Capability] = []
    for cap in out.capabilities:
        mine = []
        for sid in (s.strip().strip("`") for s in cap.symbols):
            if sid not in order:
                g.dropped.append(
                    f"group named {one_line(sid)}, which is not a public symbol of this target"
                )
            elif sid not in placed:
                placed.add(sid)
                mine.append(sid)
        if not mine:
            g.dropped.append(f"capability {kebab(cap.name)}: no symbol, so no file")
            continue
        raw.append(
            Capability(
                name=kebab(cap.name),
                summary=one_line(cap.summary),
                symbols=sorted(mine, key=order.__getitem__),
            )
        )
    g.unassigned = [sid for sid in order if sid not in placed]
    if g.unassigned:
        raw.append(
            Capability(
                name=UNASSIGNED,
                summary="Public symbols the group answer did not place.",
                symbols=g.unassigned,
            )
        )
    seen: set[str] = set()
    for cap in (c for r in raw for c in _split(r, path_of)):
        name, n = cap.name, 1
        while name in seen:
            n += 1
            name = f"{cap.name}-{n}"
        seen.add(name)
        g.capabilities.append(cap.model_copy(update={"name": name}))
    return g


def prompt(code: str, symbols: list[Symbol], gated: Gated) -> str:
    facts = [
        f"- {f.id}: {one_line(f.claim)} ({c.path}:{c.line_start}-{c.line_end})"
        for f in gated.facts
        for c in f.citations[:1]
    ]
    return f"""\
Group the public Python symbols of {code} into capabilities. A capability is one area of
behaviour that callers or users rely on (for example "billing" or "user-sessions").

{UNTRUSTED_RULES}

Rules:
- `name`: kebab-case, short, naming the behaviour, not the module.
- `symbols`: copy each symbol exactly as listed below (`path::name`). Put every symbol in exactly
  one capability, and at most {MAX_SYMBOLS} symbols in one capability.
- `summary`: one sentence saying what the capability does.

Public symbols:
{chr(10).join(symbol_line(s) for s in symbols)}

Checked facts about this code (each cites lines a program re-read at the commit):
{chr(10).join(facts) or "- none"}

Answer with JSON only, matching this schema:
{schema_json(GroupOutput)}
"""


async def run(
    state: State,
    run_dir: Path,
    gated: Gated,
    *,
    call: CallFn,
    ledger: SpendLedger,
    model: str | None,
) -> None:
    """Ask the group call when the dispatch rule allows. No symbols, no call."""
    scope = state.scope
    assert scope is not None
    answers = {} if state.group_answer is None else {KEY: state.group_answer}
    attempts = {KEY: state.group_attempts} if state.group_attempts else {}
    if not scope.symbols or not calls.any_eligible([KEY], answers, attempts):
        return
    sdk.require_trusted_repo()

    def save() -> None:
        state.group_answer = answers.get(KEY)
        state.group_attempts = attempts.get(KEY, 0)
        state.budget = ledger.to_state()
        state_mod.save(state, run_dir)

    task = (KEY, prompt(label(state.target), scope.symbols, gated), KEY)
    await calls.dispatch(
        [task],
        GroupOutput,
        call,
        answers,
        attempts,
        ledger,
        save,
        call_kwargs=dict(
            allowed_tools=[],
            max_turns=MAX_TURNS,
            cwd=str(run_dir / SNAPSHOT_DIR),
            model=model,
        ),
    )


def finish(state: State, run_dir: Path) -> Grouping | None:
    """Write `capabilities.json` from the gate and add `group`; None when not grouped
    (no answer after 2 attempts: no capability file, exit 1)."""
    scope = state.scope
    assert scope is not None
    if not scope.symbols:
        grouping = Grouping()
    elif state.group_answer is None or "error" in state.group_answer:
        return None
    else:
        grouping = gate(state.group_answer, scope.symbols)
    doc = {"capabilities": [c.model_dump() for c in grouping.capabilities]}
    report_mod.write_output(
        run_dir, CAPABILITIES_FILE, json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    )
    if "group" not in state.steps_done:
        state.steps_done.append("group")
        state_mod.save(state, run_dir)
    return grouping
