"""The gate on the write answers (plan, item 16), a pure function of state and the commit.

- Every scenario citation is re-read at the target's commit
  (`code_facts.check_citation`, which uses `check_quote`). A scenario with no
  passing citation is dropped and listed with the reason; a requirement left
  with no scenario is dropped and listed, and so is a capability left with no
  requirement (no file). A requirement whose name repeats one already kept in
  the same capability is dropped too: buildme merges deltas by requirement
  name. So is one whose name as written (`md`) is a prefix of a kept name, or
  has a kept name as a prefix: buildme's `ChangeManager` finds a REMOVED or
  MODIFIED name as a prefix, with no end-of-line anchor.
- A scenario is dropped too when its requirement, written with only that
  scenario as render writes it, fails `multiplai_core.specs.valid_requirement_text`
  (buildme's check of a requirements file): a scenario name, a WHEN or a THEN
  with no text. So every registry file render writes passes that check.
- A scenario is "pinned by test" when a passing citation is in a test file,
  else "read from code only".
- A public symbol is specified when a kept citation in its file overlaps its
  lines (`def` or `class` to its last line, from `ast` at the commit). A name
  defined more than once (a property's getter and setter, `@overload` stubs, a
  redefined function) is one symbol, specified by a citation of any of its
  defs. The others are "Not specified", once each, with the reason.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from code_facts import Citation, check_citation, ws_path
from multiplai_core.citations import file_at
from multiplai_core.specs import valid_requirement_text

from .explore import resolver
from .group import Capability, md, one_line, symbol_id
from .scope import Symbol, is_test_file
from .target import Target
from .write import WriteOutput


@dataclass
class Scenario:
    name: str
    when: str
    then: str
    cites: list[Citation]
    pinned: bool


@dataclass
class Requirement:
    name: str
    text: str
    scenarios: list[Scenario]


@dataclass
class Spec:
    name: str
    summary: str
    requirements: list[Requirement]


@dataclass
class Checked:
    specs: list[Spec] = field(default_factory=list)  # capabilities with a file, in group order
    dropped: list[str] = field(default_factory=list)
    not_specified: list[tuple[str, str]] = field(default_factory=list)  # (symbol id, reason), scope order
    not_written: list[str] = field(default_factory=list)


def symbol_spans(target: Target, symbols: list[Symbol]) -> dict[str, list[tuple[int, int]]]:
    """{symbol id: (first line, last line) of each of its defs} at the commit, once per id in scope
    order; just the `def` lines when `ast` cannot say."""
    def_lines: dict[str, list[tuple[int, int]]] = {}
    for s in symbols:
        def_lines.setdefault(symbol_id(s), []).append((s.line, s.line))
    spans: dict[str, list[tuple[int, int]]] = {}
    for path in dict.fromkeys(s.path for s in symbols):
        text = file_at(target.repo, str(target.head_sha), path)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text or "")
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                spans.setdefault(f"{path}::{node.name}", []).append((node.lineno, node.end_lineno or node.lineno))
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        spans.setdefault(f"{path}::{node.name}.{item.name}", []).append(
                            (item.lineno, item.end_lineno or item.lineno))
    return {sid: spans.get(sid) or lines for sid, lines in def_lines.items()}


def _as_written(cap: Capability, name: str, scenario: Scenario) -> str:
    """Requirement *name* with only *scenario*, as render writes it into a registry file."""
    from .render import capability_md  # here, not at the top: render imports this module
    return capability_md("", "", Spec(cap.name, cap.summary, [Requirement(name, "", [scenario])]))


def check(caps: list[Capability], answers: dict[str, dict], symbols: list[Symbol], target: Target,
          snapshot: Path) -> Checked:
    """Gate every write answer. *target* is read through this run's checkout."""
    resolve = resolver(target)
    spans = symbol_spans(target, symbols)
    path_of = {symbol_id(s): s.path for s in symbols}
    out = Checked()
    cited: set[str] = set()
    unwritten: set[str] = set()
    for cap in caps:
        raw = answers.get(cap.name)
        if raw is None or "error" in raw:
            out.not_written.append(cap.name)
            unwritten.update(cap.symbols)
            continue
        reqs: list[Requirement] = []
        for r in WriteOutput.model_validate(raw).requirements:
            name = one_line(r.name)
            if not name or name in {q.name for q in reqs}:
                out.dropped.append(f"{cap.name} / {name or '(no name)'}: a requirement needs a name "
                                   "no other requirement in its capability has")
                continue
            # As written to the file, both ways round: the first kept name stays.
            clash = next((q.name for q in reqs if md(q.name).startswith(md(name))
                          or md(name).startswith(md(q.name))), None)
            if clash is not None:
                out.dropped.append(f"{cap.name} / {name}: the kept requirement `{clash}` is a prefix of its "
                                   "name as written, or the other way round; buildme matches requirement "
                                   "names by prefix")
                continue
            kept = []
            for sc in r.scenarios:
                good, reasons = [], []
                for c in sc.citations:
                    c = c.model_copy(update={"path": ws_path(c.path, snapshot)})
                    reason = check_citation(c, resolve)
                    if reason is None:
                        good.append(c)
                    elif reason not in reasons:
                        reasons.append(reason)
                if not good:
                    out.dropped.append(f"{cap.name} / {name} / {one_line(sc.name)}: "
                                       f"{'; '.join(reasons) or 'no citation'}")
                    continue
                scenario = Scenario(one_line(sc.name), one_line(sc.when), one_line(sc.then), good,
                                    pinned=any(is_test_file(c.path) for c in good))
                if not valid_requirement_text(_as_written(cap, name, scenario)):
                    out.dropped.append(f"{cap.name} / {name} / {scenario.name or '(no name)'}: a scenario "
                                       "needs a name, a WHEN and a THEN, each with text")
                    continue
                kept.append(scenario)
                for c in good:
                    cited.update(sid for sid, ranges in spans.items() if path_of[sid] == c.path
                                 and any(c.line_start <= hi and c.line_end >= lo for lo, hi in ranges))
            if not kept:
                out.dropped.append(f"{cap.name} / {name}: no scenario left")
                continue
            reqs.append(Requirement(name, one_line(r.text), kept))
        if reqs:
            out.specs.append(Spec(cap.name, cap.summary, reqs))
        else:
            out.dropped.append(f"{cap.name}: no requirement left, so no file")
    for sid in spans:
        if sid not in cited:
            reason = "its capability was not written" if sid in unwritten else "no kept scenario cites it"
            out.not_specified.append((sid, reason))
    return out
