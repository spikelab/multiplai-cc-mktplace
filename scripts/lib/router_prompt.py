"""Generic LLM-router prompt template.

The prompt is intentionally **catalog-agnostic**: it never names
specific files, people, or workspace conventions. All routing
specificity comes from per-entry catalog metadata (``intent_domains``,
``anti_domains``, ``bundle``, ``co_retrieve_for``, ``section_anchors``)
that each plugin user authors for their own files.

Few-shot examples illustrate the *patterns* (intent matching, bundle
expansion, anti-domain rejection, continuation rule) without
referencing any specific corpus, so the same prompt works for any
user's memory/skills/resources.
"""

from __future__ import annotations

from collections.abc import Iterable


SYSTEM_PROMPT = """\
You are a CONTEXT ROUTER for a Claude Code session. Given the user's prompt, \
optionally the most recent assistant response, and catalogs of memory files, \
skills, and resources, decide which items would help Claude produce a better \
response on the next turn.

OUTPUT (strict):
Return ONE JSON object with three keys: "memory", "skills", "resources" — each \
mapping to an array of names from the corresponding catalog. Use empty arrays \
for corpora with no relevant matches. If nothing is relevant in any corpus, \
return {"memory": [], "skills": [], "resources": []}. \
For memory entries with section_anchors, you MAY emit "filename#Section Name" \
to load only that section. \
No prose, no markdown fences, no commentary — JSON only.

ROUTING RULES (apply in order):

1. CONTINUATION CHECK. If the user prompt is a short go-ahead, approval, or \
continuation ("yes", "go", "sounds good", "do it", "next", "continue"), \
return all-empty arrays. The conversation already has the needed context.

2. INTENT MATCHING. Match the user's TASK INTENT against each entry's \
intent_domains field. The intent is what the user is trying to accomplish, \
not literal keyword overlap. A prompt about "voice agents" matches \
intent_domain "researching voice AI frameworks" by topic, not by token.

3. ANTI-DOMAINS. If the user's intent matches an entry's anti_domains, \
EXCLUDE that entry even if its intent_domains also match. anti_domains \
are explicit "do not retrieve for X" hints written by the user.

4. BUNDLES. If you select an entry with a "bundle" field, the routing \
layer will automatically include its bundle siblings — you do NOT need \
to enumerate bundle members yourself. Just pick the most representative \
member.

5. CO_RETRIEVE_FOR. Same: companions listed in co_retrieve_for are added \
automatically. Pick the primary entry; companions follow.

6. CONVERSATIONAL CONTEXT. If a LAST ASSISTANT RESPONSE is provided, \
use it to disambiguate the prompt. Words like "costs", "budget", or \
"timeline" mean different things in technical vs. personal contexts; \
let the recent response narrow the meaning.

7. UTILITY TEST. For each candidate, ask: "Would having this entry's \
content materially help answer the prompt?" If no, drop it. False \
negatives are cheaper than false positives — when in doubt, leave out.

8. CORPUS DISCRIMINATION:
   - memory     = user's current state, identity, personal context
   - skills     = slash-command capabilities Claude can invoke
   - resources  = past research and reference material
   Prefer memory for personal queries; resources for "what does the \
research say" or known-topic deep-dives; skills only when a clear \
slash-command match exists.

9. SLASH-COMMAND ECHO: do NOT return a skill the user already typed as \
a slash command (e.g., user wrote "/code-review" → don't echo it back).

10. SECTION ANCHORS: when a memory entry has section_anchors and only \
ONE section is relevant, prefer "file#Section" over the whole file. \
When the whole file is relevant, return the bare filename.
"""


FEW_SHOT_EXAMPLES = """\
EXAMPLES (abstract patterns — your catalogs may differ):

User prompt: "fix this CSS bug"
(Pure technical, no personal/research signal.)
{"memory": [], "skills": [], "resources": []}

User prompt: "Sounds good. Let's do that."
(Continuation — return empty.)
{"memory": [], "skills": [], "resources": []}

User prompt: "I'm worried about cash flow next month"
(Personal-finance signal — pick memory entries whose intent_domains match \
"personal finance", "cash flow", "budgeting".)
{"memory": ["<finance-related-file>"], "skills": [], "resources": []}

User prompt: "should we use library X or library Y?"
(Tech-eval signal — pick memory whose intent_domains cover technical \
preferences, plus any resources covering X/Y.)
{"memory": ["<tech-prefs-file>"], "skills": [], "resources": ["<related-research>"]}

User prompt: "help me draft a blog post"
(Writing signal — if a writing-related entry has bundle="writing", \
picking one bundle member suffices; siblings are added automatically.)
{"memory": ["<voice-or-style-file>"], "skills": ["writing"], "resources": []}

User prompt: "review this codebase"
(Skill-trigger signal.)
{"memory": [], "skills": ["code-review"], "resources": []}
"""


def format_catalog_for_llm(corpus_label: str, entries: Iterable[dict]) -> str:
    """Render one corpus's entries as a labeled block for the LLM input.

    Each entry shows its name, summary, intent_domains, and anti_domains
    where present. ``corpus_label`` is the heading the LLM sees ("MEMORY",
    "SKILLS", "RESOURCES").
    """
    lines = [f"=== {corpus_label.upper()} CATALOG ==="]
    written = 0
    for entry in entries:
        filename = (
            entry.get("source")
            or entry.get("path")
            or entry.get("name")
            or entry.get("file", "")
        )
        if not filename:
            continue
        block = [f"FILE: {filename}"]
        summary = (entry.get("summary") or "").strip()
        if summary:
            block.append(f"  Purpose: {summary}")
        intent = entry.get("intent_domains") or []
        if isinstance(intent, list) and intent:
            block.append(f"  Relevant for: {', '.join(str(i) for i in intent)}")
        anti = entry.get("anti_domains") or []
        if isinstance(anti, list) and anti:
            block.append(f"  NOT relevant for: {', '.join(str(a) for a in anti)}")
        anchors = entry.get("section_anchors") or []
        if isinstance(anchors, list) and anchors:
            block.append(
                f"  Sections: {', '.join(str(a) for a in anchors)} "
                f"(emit '{filename}#<section>' for partial loads)"
            )
        bundle = entry.get("bundle")
        if isinstance(bundle, str) and bundle.strip():
            block.append(f"  Bundle: {bundle.strip()}")
        lines.append("\n".join(block))
        written += 1
    if written == 0:
        lines.append("(no entries)")
    return "\n\n".join(lines)


def build_user_message(
    prompt: str,
    last_response: str | None,
    corpora: dict[str, list[dict]],
) -> str:
    """Assemble the user-side message for the LLM router call.

    Order: catalogs first (so the LLM has the rule space before
    seeing the prompt), then optional last response, then the user
    prompt. Keeps the prompt's most relevant signal at the bottom
    where models tend to attend most.
    """
    parts = []
    for label in ("memory", "skills", "resources"):
        entries = corpora.get(label) or []
        if entries:
            parts.append(format_catalog_for_llm(label, entries))
    if last_response:
        snippet = last_response.strip()
        # Cap at ~2KB to avoid blowing the routing budget on a long turn.
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "…"
        parts.append(f"=== LAST ASSISTANT RESPONSE ===\n{snippet}")
    parts.append(f"=== USER PROMPT ===\n{prompt.strip()}")
    return "\n\n".join(parts)
