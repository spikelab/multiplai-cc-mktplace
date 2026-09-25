"""Fix-checker prompt: one fresh agent per fix that passed the gates."""

from __future__ import annotations

import json

from ..models import Finding, Fix, TargetInfo
from . import JSON_ONLY, workspace_block

SCHEMA = """\
{"status": "confirmed" | "refuted",
 "reason": "what breaks, citing path:line, or why nothing does"}"""


def build(target: TargetInfo, finding: Finding, fix: Fix, consumers: dict[str, list[str]]) -> str:
    premises = json.dumps([p.model_dump(exclude={"question"}) for p in fix.premises], indent=1)
    uses = "\n".join(f"- {symbol}: {', '.join(hits) or 'no uses found'}" for symbol, hits in consumers.items())
    return "\n\n".join([
        "You are checking one proposed fix from a code review. Assume it is wrong until the code "
        "shows otherwise.",
        workspace_block(target),
        f"Finding in `{finding.file}` lines {finding.line_start}-{finding.line_end}: {finding.claim}",
        f"Proposed fix: {fix.description}\n\nPatch sketch:\n{fix.patch_sketch or '(none)'}",
        f"The fix's premises:\n{premises}",
        f"Lines that use the symbols the premises name (from git grep at this commit):\n{uses or '(none)'}",
        "One question: does any consumer of the cited symbols, or any caller of the changed lines, "
        "break if the fix is applied as described? Read those lines before answering. Answer "
        "`refuted` if something breaks or a premise is false, citing path:line in the reason. "
        "Answer `confirmed` only if you read the consumers and callers and nothing breaks.",
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ])
