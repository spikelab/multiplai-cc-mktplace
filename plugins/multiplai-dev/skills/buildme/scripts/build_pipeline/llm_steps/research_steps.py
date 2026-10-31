"""The research decision: one toolless call that says whether outside
research is needed before the design, and why.

It takes its call function as a keyword argument; None means the real SDK
adapter, looked up at call time so a patched module attribute is seen.
"""

from __future__ import annotations

from pathlib import Path

from multiplai_core.specs import change_root
from pydantic import BaseModel, Field

from ..change_manager import requirement_sources
from ..prompts.research import RESEARCH_DECISION_PROMPT, UNTRUSTED_NOTICE
from ..sdk import llm_call_structured


class ResearchDecision(BaseModel):
    needed: bool
    reason: str = ""
    topics: list[str] = Field(default_factory=list)


def specs_summary(change_dir: Path) -> str:
    """What the decision reads: the change's proposal, use cases (a
    component's change's) and requirements."""
    change_dir = Path(change_dir)
    root = change_root(change_dir)
    parts = []
    for path in [root / "proposal.md", root / "use-cases.md"]:
        if path.is_file():
            parts.append(f"### {path.name}\n{path.read_text()}")
    parts += [
        f"### {label}\n{text}"
        for label, text in requirement_sources(change_dir).items()
    ]
    return "\n\n".join(parts)


async def decide_research(config, summary: str, *, call=None) -> ResearchDecision:
    """One toolless call: is outside research needed, and why."""
    from multiplai_core.untrusted import fence

    call = call or llm_call_structured
    prompt = RESEARCH_DECISION_PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        summary="\n".join(fence(summary, source="change specs")) or "(no specs)",
    )
    return await call(
        prompt,
        ResearchDecision,
        model=config.model_for("research"),
        effort=config.effort_for("research"),
        budget_label="research",
    )
