---
name: config-audit
description: "Subtractive review of the active Claude/multiplai configuration on a ~90-day cadence — classifies every standing rule as still-serving, obsolete, or model-constraining, and writes a removals-first proposal to .multiplai/dreams/ for user review. Does NOT apply changes."
---

# Multiplai Config Audit — Subtractive Rule/Config Review

You are the multiplai config-audit skill. Your job is to review every standing rule
and configuration knob in the active setup and propose what to **remove**. Config
accretes: every rule was added to close a gap, but nobody deletes rules when the
gap disappears. Old rules quietly tax every session (context tokens, hook latency)
and — worse — constrain newer models with workarounds written for older models'
weaknesses. This skill is the counterweight: a periodic, subtractive pass whose
default question is "why does this rule still exist?", not "what should we add?"

**This skill never edits configuration.** The only files it writes are the
proposal (step 4) and the state stamp (step 6). The user reviews the proposal
and makes any changes themselves.

## Why this exists — three motivating cases

1. **The single-file-refactor rule** (Anthropic large-codebases guidance): a rule
   constraining refactors to single-file changes genuinely helped older models,
   but *prevents* newer models from making beneficial cross-file edits. The rule
   outlived its reason and became a ceiling.
2. **The redundant Perforce hook** (same source): a Perforce edit hook became
   redundant once Claude Code shipped native Perforce mode — kept around long
   after the migration, it was pure overhead on every edit.
3. **Our own kit, 2026-07-08**: the runtime's tracked `settings.json` carried
   `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: 75` while its required companion
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` had been silently wiped by a `git pull`
   over the tracked file. The override does nothing without the window var, so
   the stale env-var sat inert and the entire compaction scheme was silently
   disabled — a regression nobody noticed until it was rediscovered by hand.
   A periodic audit that asks "is this setting still doing anything?" catches
   exactly this class of decay.

## Steps

1. **Enumerate the active config surface.** Read each of these (skip gracefully
   with a note if one is absent in this installation):
   - `$CLAUDE_CONFIG_DIR/CLAUDE.md` — the user's global instructions.
   - Workspace `CLAUDE.md`s — the workspace-root `CLAUDE.md` and any
     project-level `CLAUDE.md` relevant to the current workspace.
   - `dotfiles/settings.json` **env and permissions blocks** (plus the
     `settings.local.json` overlay if present) in the kit checkout / runtime.
   - **Hook registrations** — `hooks` blocks in settings files and each
     installed plugin's `hooks/hooks.json`.
   - **Memory-file standing rules** — `*.md` files under the memory directory
     that encode directives (always/never rules, standing constraints), as
     opposed to plain facts about the user. Facts are out of scope; rules are in.

2. **Inventory every rule.** For each standing rule or config knob, record:
   the rule text (or a faithful summary), its source file and location, and —
   where discoverable from git history, the diary, or the rule's own wording —
   the gap it was originally written to close. If the original reason cannot be
   determined, record "reason unknown"; that is itself a signal.

3. **Classify each rule** into exactly one of:
   - **still-serving** — the gap it closes still exists and the rule still closes it.
   - **obsolete** — the gap it closed no longer exists (tooling changed, the
     workflow moved on, a companion setting disappeared, the migration finished).
   - **model-constraining** — written to work around an older model's weaknesses
     (capability hedges, "keep changes small" style guardrails, hand-holding
     steps) that today's models no longer need and that now suppress better behavior.

   When the evidence is thin, classify as still-serving and note the uncertainty —
   but state explicitly what evidence would justify removal, so the next audit
   can settle it.

4. **Write the subtractive proposal** to
   `.multiplai/dreams/config-audit-YYYY-MM-DD.md` (today's date, same directory
   the dream proposals use). Structure it **removals first**:
   1. **Removals** — every *obsolete* and *model-constraining* rule, each with:
      rule text, source file/location, classification, the evidence, and the
      expected effect of removing it.
   2. **Edits** — rules that are partly stale (tighten wording, narrow scope)
      rather than fully removable.
   3. **Additions** — only when a removal requires one (e.g. removing a broad
      rule needs a narrower replacement). Never propose free-standing additions;
      that is what the rest of the toolchain is for.

   End the proposal with a short "kept as-is" count (not a full listing) so the
   user can see the audit's coverage.

5. **Report to the user, change nothing.** Print the proposal path, the counts
   per section (removals / edits / additions / kept), and the top 2-3 removal
   candidates in one line each. Do **not** apply any of the proposed changes —
   not even "obvious" ones. The proposal is for the user's review; the user
   decides what happens to it.

6. **Stamp the state file** (this is what closes the 90-day SessionStart nudge
   gate — never skip it, even when the audit found nothing to remove). Write
   `config_audit_state.yaml` into the plugin data directory, **next to
   `dream_state.yaml`** (default: `<workspace>/.multiplai/data/config_audit_state.yaml`,
   falling back to `~/.multiplai/data/` when no workspace is configured — locate
   the directory containing `dream_state.yaml` and write beside it):

   ```yaml
   last_run: "<UTC ISO-8601 timestamp>"
   proposal: ".multiplai/dreams/config-audit-YYYY-MM-DD.md"
   ```

   Get the timestamp with `date -u +%Y-%m-%dT%H:%M:%S+00:00`. If a previous
   state file exists, overwrite it — only the latest run matters.

## Constraints

- **Never apply changes.** This skill must NOT apply, edit, or delete any
  configuration, rule, hook, or memory file. Its entire write surface is the
  proposal file in `.multiplai/dreams/` and `config_audit_state.yaml`.
- Removals first, edits second, additions only when a removal requires one —
  the proposal's bias is subtractive by design.
- Be evidence-based: every removal candidate cites where the rule lives and why
  the gap it closed is gone. "Feels unnecessary" is not a classification.
- A rule whose reason is unknown is a *candidate* for removal, not an automatic
  one — flag it and let the user decide.
- Always stamp the state file at the end (step 6), even on a clean audit;
  otherwise the nudge fires every session.
