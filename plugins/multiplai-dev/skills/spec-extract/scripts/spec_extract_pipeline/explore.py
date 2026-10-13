"""The explore step: one `code_facts` agent per unit, then the gated facts in `facts.json`.

**The dispatch rule** (plan, item 7), the same whether or not the process
stopped before:

- A unit is asked when it has no answer, or an `{"error": ...}` answer, and
  fewer than 2 attempts. Before each call the run checks the budget, deletes
  that unit's error answer, adds 1 to `explore_attempts[key]` and saves state.
  As the call returns, one save holds both its answer (or its error) and the
  ledger.
- Round 1 asks every unit the rule allows; round 2 asks those that returned
  an error. A unit with no answer after 2 attempts is final: its files are
  "not analysed" in `report.md`, and any error text stays in `state.json`.
- A failed call (`sdk.CallFailed`) is stored as an error answer. A budget
  stop (`BudgetExceededError`, from the check before counting or from inside
  the call) takes back the attempt it counted, saves, and marks the task
  "stopped by budget"; it never escapes `code_facts.bounded`, so calls in
  flight finish and are saved. Then the run exits 3.
- A call that cannot run at all (`sdk.BackendUnavailable`, or
  `sdk.RepoTrustError` from the wrapper's own check) is handled the same way,
  then raised: exit 1 or 4, and a re-run after the fix asks the unit as if it
  had never been asked.

**Order.** `explore_answers` fills in return order, so nothing reads that
order. `ordered_answers` orders them by `scope.units` (`partition`'s order),
and `gate_answers` numbers facts over that, so `facts.json` is the same bytes
in a run that stopped and one that did not.

The per-task function goes to `code_facts.bounded` and calls `code_facts.ask`,
not `run_tasks`, which skips error answers and has no hook to count an attempt.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from code_facts import (CallFn, ExploreOutput, Unit, ask, bounded, citation_keeper, explore_unit,
                        gate_answers, unit_key)
from multiplai_core.budget import BudgetExceededError, SpendLedger

from . import report as report_mod
from . import sdk
from . import state as state_mod
from .scope import TargetScope
from .state import State
from .target import SNAPSHOT_DIR, Target

MAX_ATTEMPTS = 2
CONCURRENCY = 4
FACTS_FILE = "facts.json"
# git's empty tree: the base of a whole-tree `facts.json` (review --tree, buildme's analysis).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class BudgetStopped(Exception):
    """The run folder's spend reached `--max-usd` (exit 3)."""


def ordered_answers(units: list[Unit], answers: dict[str, dict]) -> dict[str, dict]:
    """The stored answers in unit order: the only order any reader uses."""
    return {unit_key(u): answers[unit_key(u)] for u in units if unit_key(u) in answers}


def answered(units: list[Unit], answers: dict[str, dict]) -> list[Unit]:
    return [u for u in units if unit_key(u) in answers and "error" not in answers[unit_key(u)]]


def eligible(unit: Unit, answers: dict[str, dict], attempts: dict[str, int]) -> bool:
    key = unit_key(unit)
    has_answer = key in answers and "error" not in answers[key]
    return not has_answer and attempts.get(key, 0) < MAX_ATTEMPTS


def not_answered_files(units: list[Unit], answers: dict[str, dict]) -> list[str]:
    """Files of the units explore gave no answer for, in unit order then file order."""
    done = {unit_key(u) for u in answered(units, answers)}
    return [f for u in units if unit_key(u) not in done for f in u.files]


def label(target: Target) -> str:
    """The name the explore prompt gives the code."""
    base = Path(target.source).name if target.kind == "dir" and target.source else Path(target.repo).name
    if target.kind == "pr":
        return f"{base} (PR #{target.pr})"
    return f"{base}/{target.path}" if target.path else base


def unit_prompt(target: Target, unit: Unit) -> str:
    # code_facts' prompt carries UNTRUSTED_RULES: the code's comments and strings never instruct the agent.
    return explore_unit(label(target), unit.spans, ExploreOutput, "standard")


async def run(state: State, run_dir: Path, *, call: CallFn, ledger: SpendLedger, model: str | None) -> None:
    """Ask every unit the dispatch rule allows, in at most two rounds.

    Raises `sdk.RepoTrustError` before the first call when the repository is not
    trusted and some unit is to be asked; `BudgetStopped` when a task was
    stopped by the budget.
    """
    scope = state.scope
    assert scope is not None
    answers, attempts = state.explore_answers, state.explore_attempts
    if not any(eligible(u, answers, attempts) for u in scope.units):
        return
    sdk.require_trusted_repo()
    snapshot = run_dir / SNAPSHOT_DIR
    call_kwargs = dict(allowed_tools=list(sdk.READ_ONLY_TOOLS), max_turns=sdk.EXPLORE_MAX_TURNS,
                       cwd=str(snapshot), model=model)
    stopped: list[str] = []
    cannot_run: list[Exception] = []

    def save() -> None:
        state.budget = ledger.to_state()
        state_mod.save(state, run_dir)

    async def one(unit: Unit) -> None:
        key = unit_key(unit)
        try:
            ledger.check("explore")
        except BudgetExceededError as e:
            stopped.append(str(e))
            return
        answers.pop(key, None)  # only an error answer can be here: an answered unit is not eligible
        before = attempts.get(key)
        attempts[key] = (before or 0) + 1
        save()
        try:
            await ask(key, unit_prompt(state.target, unit), call, answers, call_kwargs=call_kwargs, errors=[],
                      recoverable=(sdk.CallFailed,), fatal=(BudgetExceededError,))
        except (BudgetExceededError, sdk.BackendUnavailable, sdk.RepoTrustError) as e:
            if before is None:
                attempts.pop(key, None)
            else:
                attempts[key] = before
            save()
            if isinstance(e, BudgetExceededError):
                stopped.append(str(e))
            else:
                cannot_run.append(e)
            return
        save()

    for _ in range(MAX_ATTEMPTS):
        todo = [u for u in scope.units if eligible(u, answers, attempts)]
        if not todo:
            break
        await bounded(todo, one, CONCURRENCY)
        if cannot_run:
            raise cannot_run[0]
        if stopped:
            raise BudgetStopped(stopped[0])


def resolver(target: Target) -> Callable[[str, str], tuple[str, str, str] | None]:
    """A cited path is read at the target's head commit, in its repository (`<run>/source` for --dir)."""
    def resolve(path: str, side: str):
        if side != "head" or not path:
            return None
        return target.repo, str(target.head_sha), path
    return resolve


def facts_document(target: Target, scope: TargetScope, answers: dict[str, dict], run_dir: Path,
                   generated_at: datetime) -> dict:
    """`facts.json` in review's format (`review/schema/facts.v1.schema.json`), kind `tree`."""
    keep = citation_keeper(resolver(target), root=run_dir / SNAPSHOT_DIR)
    g = gate_answers(ordered_answers(scope.units, answers), keep)
    by_id = {f.id: f for f in g.facts}
    files = []
    for path, purpose in g.purposes.items():
        cite = next((f.citations[0] for f in g.facts if f.source.endswith("/file") and f.citations[0].path == path),
                    None)
        files.append({"path": path, "purpose": purpose, "citation": cite.model_dump() if cite else None})
    cited = {c.path for f in g.facts for c in f.citations}
    uncovered = [p for u in answered(scope.units, answers) for p in u.files if p not in cited]
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "base_sha": EMPTY_TREE,
        "head_sha": target.head_sha,
        "kind": "tree",
        "facts": [f.model_dump() for f in g.facts],
        "files": files,
        "interfaces": [by_id[i].model_dump() for i in g.interfaces],
        "gotchas": [by_id[i].model_dump() for i in g.gotchas],
        "conventions": [],
        "terms": [t.model_dump() for t in g.terms],
        "uncovered": uncovered,
        "dropped": [d.model_dump() for d in g.dropped],
    }


def finish(state: State, run_dir: Path, now: datetime, git_target: Target) -> tuple[int, int]:
    """Write `facts.json` and `report.md` from state, then add `explore`. (answered, units).

    Runs on every run that gets past explore, so a resume rewrites both files
    from state with the same bytes (rows R6, R24). `generated_at` is the time
    the first run got here, saved in state before `facts.json` is first
    written (row R28). *git_target* is the saved target read from this run's
    checkout, where the citations are checked.
    """
    scope = state.scope
    assert scope is not None
    answers = state.explore_answers
    if state.facts_generated_at is None:
        state.facts_generated_at = now
        state_mod.save(state, run_dir)
    doc = facts_document(git_target, scope, answers, run_dir, state.facts_generated_at)
    report_mod.write_output(run_dir, FACTS_FILE, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    report_mod.write_report(run_dir, state.target,
                            report_mod.scope_sections(scope, not_answered_files(scope.units, answers)))
    if "explore" not in state.steps_done:
        state.steps_done.append("explore")
        state_mod.save(state, run_dir)
    return len(answered(scope.units, answers)), len(scope.units)
