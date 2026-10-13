"""The write step: one agent per capability writes its requirements and scenarios (plan, item 15).

Each agent gets `Read`, `Glob` and `Grep` on `snapshot/`, the capability's
symbols, the checked facts that cite the files those symbols are in, and the
target's test files to cite as evidence. Asked through `calls.dispatch` under
item 7's rule, key = capability name, label `write:<capability>`, with the
write model. A capability with no answer after 2 attempts is "not written".
"""

from __future__ import annotations

from pathlib import Path

from code_facts import UNTRUSTED_RULES, CallFn, Citation, Fact, Gated, citation_rules, schema_json
from multiplai_core.budget import SpendLedger
from pydantic import BaseModel, ConfigDict

from . import calls, sdk
from . import state as state_mod
from .explore import label
from .group import Capability, one_line, symbol_id, symbol_line
from .scope import Symbol, is_test_file
from .state import State
from .target import SNAPSHOT_DIR

PATH_RULE = "- `path` is the file's path relative to your working directory, as listed above."


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ScenarioOut(_Model):
    name: str
    when: str
    then: str
    citations: list[Citation] = []


class RequirementOut(_Model):
    name: str
    text: str = ""
    scenarios: list[ScenarioOut] = []


class WriteOutput(_Model):
    requirements: list[RequirementOut] = []


def capability_facts(cap: Capability, by_id: dict[str, Symbol], gated: Gated) -> list[Fact]:
    paths = {by_id[sid].path for sid in cap.symbols}
    return [f for f in gated.facts if any(c.path in paths for c in f.citations)]


def prompt(code: str, cap: Capability, by_id: dict[str, Symbol], gated: Gated, tests: list[str]) -> str:
    facts = [f"- {f.id}: {one_line(f.claim)} ({c.path}:{c.line_start}-{c.line_end})"
             for f in capability_facts(cap, by_id, gated) for c in f.citations[:1]]
    return f"""\
Write the requirements of the capability "{cap.name}" of {code}, as the code behaves now.
What it does: {cap.summary or "see the symbols below"}

{UNTRUSTED_RULES}

Read the code of these public symbols (paths are relative to your working directory):
{chr(10).join(symbol_line(by_id[sid]) for sid in cap.symbols)}

Test files you may cite as evidence that a behaviour is pinned:
{chr(10).join(f"- {t}" for t in tests) or "- none"}

Checked facts about these files:
{chr(10).join(facts) or "- none"}

Rules:
- One requirement per behaviour a caller can rely on. `name` is short; `text` states the behaviour
  with SHALL. Describe what the code does now, not what it should do.
- Each scenario: `when` is the condition, `then` the observable outcome.
- Every scenario cites the lines that implement it, and the test lines that check it when a test does.
- Cover every symbol listed above that has behaviour a caller can observe.
{citation_rules(PATH_RULE)}
Answer with JSON only, matching this schema:
{schema_json(WriteOutput)}
"""


async def run(state: State, run_dir: Path, caps: list[Capability], gated: Gated, *, call: CallFn,
              ledger: SpendLedger, model: str | None) -> None:
    """Ask each capability the dispatch rule allows, four at a time."""
    scope = state.scope
    assert scope is not None
    answers, attempts = state.write_answers, state.write_attempts
    if not calls.any_eligible([c.name for c in caps], answers, attempts):
        return
    sdk.require_trusted_repo()
    by_id = {symbol_id(s): s for s in scope.symbols}
    tests = [f for f in scope.files if is_test_file(f)]
    code = label(state.target)

    def save() -> None:
        state.budget = ledger.to_state()
        state_mod.save(state, run_dir)

    tasks = [(c.name, prompt(code, c, by_id, gated, tests), f"write:{c.name}") for c in caps]
    await calls.dispatch(tasks, WriteOutput, call, answers, attempts, ledger, save,
                         call_kwargs=dict(allowed_tools=list(sdk.READ_ONLY_TOOLS), max_turns=sdk.EXPLORE_MAX_TURNS,
                                          cwd=str(run_dir / SNAPSHOT_DIR), model=model))
