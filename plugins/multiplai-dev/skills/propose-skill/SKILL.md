---
name: propose-skill
description: Analyze session patterns and propose new skills. Use when Claude notices repeating workflows, when the skill-creation nudge fires, or when the user says "formalize this", "make this a skill", or "propose a skill". Reads session context to identify recurring tool sequences, command patterns, or file editing flows that could be automated.
model: opus
effort: high
disable-model-invocation: true
---

# Propose Skill

> **Best with the `multiplai-context` plugin.** Steps below read that plugin's runtime artifacts (`.multiplai/diary/` and `.multiplai/learnings/`). Without it, rely on the current conversation context alone.

Detect repeating patterns in the current session and draft a skill proposal.

---

## Step 1: Gather Session Context

Read these sources to understand what's been happening:

1. Today's `.multiplai/diary/` entry — session summaries and key actions
2. Recent `.multiplai/learnings/` files — patterns and observations from recent sessions
3. The current conversation context (already available)

Focus on: Which tool sequences recur? Which bash commands repeat? Which file types get edited in the same order? What workflows span multiple steps?

---

## Step 2: Identify the Pattern

Look for:
- **Tool chains**: Same sequence of Read → Edit → Bash happening repeatedly
- **File templates**: Same structure being created for different inputs
- **Multi-step workflows**: Procedures with 3+ steps that follow a consistent order
- **Domain knowledge**: Information that gets re-explained or re-discovered

Skip patterns that:
- Are already covered by an existing skill (check `$CLAUDE_CONFIG_DIR/skills/`)
- Are too simple to warrant a skill (single command, one-step action)
- Are one-off procedures unlikely to recur

---

## Step 3: Draft the SKILL.md

Follow the format from `skill-creator/SKILL.md`:

```yaml
---
name: skill-name
description: Clear description including when to use and trigger phrases.
---
```

Body should include:
- Step-by-step workflow (imperative form)
- Any scripts that should be bundled (propose them, don't write yet)
- Reference files needed
- Expected inputs and outputs

**Keep it under 200 lines.** Concise > comprehensive.

---

## Step 4: Present for Approval

Show the draft to the user with:

```
I noticed a repeating pattern: [describe the pattern in 1-2 sentences]

Here's a skill proposal:

[draft SKILL.md content]

Target path: $CLAUDE_CONFIG_DIR/skills/{name}/SKILL.md

Should I create this skill? (yes / modify / no)
```

**NEVER write the skill file without explicit "yes" from the user.**

---

## Step 5: Write if Approved

If approved:
1. Create `$CLAUDE_CONFIG_DIR/skills/{name}/SKILL.md` with the approved content
2. If scripts were proposed, create them in `$CLAUDE_CONFIG_DIR/skills/{name}/scripts/`
3. Add a trigger line to CLAUDE.md under "Skill Triggers" if the pattern needs explicit routing

---

## Guidelines

- One skill per pattern. Don't bundle unrelated workflows.
- Match existing skill conventions — look at 2-3 existing skills for style.
- Prefer high-freedom instructions over rigid scripts unless the workflow is fragile.
- The description field is the primary trigger — make it comprehensive with exact phrases.
