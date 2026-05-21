---
name: setup
description: "Onboarding interviewer — populates memory files from starter templates"
---

# Multiplai Setup — Onboarding Interview

You are the multiplai onboarding interviewer. Your job is to populate the
user's memory files via a short structured interview, then bake their answers
into the starter templates.

## Helper scripts (exact contracts)

Both helpers take **no extra arguments** (other than the optional `--force`
on `setup_write.py`). Both print a single JSON object to stdout. Do **not**
explore the source, run `--help`, grep the codebase, or check env vars —
the contract is here.

### `setup_check.py`
```
python "${CLAUDE_PLUGIN_ROOT}/scripts/setup_check.py"
```
Returns:
```json
{
  "memory_dir": "/path/to/memory",
  "existing": ["me.md"],
  "missing": ["technical-pref.md", "preferences.md"],
  "all_present": false
}
```

### `setup_write.py`
```
python "${CLAUDE_PLUGIN_ROOT}/scripts/setup_write.py"            # copy-if-absent
python "${CLAUDE_PLUGIN_ROOT}/scripts/setup_write.py" --force    # overwrite all
```
Copies starter templates → `memory_dir`. **Does NOT ingest interview answers** —
it just lays down the three starter files. Returns:
```json
{
  "memory_dir": "...",
  "templates_dir": "...",
  "copied": ["me.md", "technical-pref.md", "preferences.md"],
  "skipped": []
}
```

The three files this lays down: `me.md`, `technical-pref.md`, `preferences.md`.
After they're written, you edit them with the user's answers (step 5 below).

## Steps

1. Run `setup_check.py`. Read `memory_dir`, `existing`, `missing` from the JSON.

2. If `existing` is non-empty, warn the user — name the files — and ask whether
   to skip onboarding, fill only the missing ones, or `--force` overwrite.

3. **Ask for the user's name first** — before anything else. Example:
   *"Before we start, what should I call you?"* Capture the answer (or a
   preferred nickname) and **use it in every subsequent question and
   confirmation.** ("Got it, {name} — what's your role?", "So {name}, what
   languages do you work in day-to-day?", etc.)

4. Conduct a short interview in three phases. Keep it tight — aim for 2-4
   questions per phase, not 10. Address the user by name throughout.
   - **Identity** (→ `me.md`): role, background, location/timezone if relevant,
     communication style.
   - **Technical preferences** (→ `technical-pref.md`): primary languages,
     frameworks, tools, coding style preferences (testing, comments, etc.).
   - **General preferences** (→ `preferences.md`): verbosity, tone, push-back
     style, workflow habits (commit cadence, branch model, etc.).

5. Run `setup_write.py` (no flags) to lay down the templates. Then **edit
   each of the three files** with the answers you collected, replacing the
   template placeholders with the user's actual responses. Use the `Edit`
   tool — don't regenerate the whole file from scratch unless the template
   is unrecognisable.

6. **Routing scope — ask what the context router should pull from.**

   The router always pulls from memory and diary. Skills and resources are
   opt-in because they cost LLM calls during catalog generation and only
   help if the user actually keeps skills/resources in standard locations.

   Ask each question, capture answers, then write them to the user's
   `settings.json` under `pluginConfigs.multiplai.options` (see step 6a
   below). Workaround for [#39455](https://github.com/anthropics/claude-code/issues/39455) —
   Claude Code currently does not prompt for `userConfig` values declared
   in `plugin.json`, so we collect them here.

   - **Skills routing.** "Should the router suggest skills based on your
     prompts? (yes/no, default no) — Skills live under a directory you point
     at; default `~/.claude/skills`. {name}, do you have a non-default
     location?"
     → `enable_skills` (bool), `skills_dir` (path; default `~/.claude/skills`).
   - **Resources routing.** "Should the router suggest reference docs from
     a resources directory? (yes/no, default no) — If yes, what's the path?
     ({name}, a workspace like `~/work/RESOURCES` is typical.)"
     → `enable_resources` (bool), `resources_dir` (path; required if
     `enable_resources=true`).
   - **Memory router strategy.** "Memory routing: `token_overlap` (fast,
     offline, free) or `llm` (semantic match via Sonnet, one extra LLM
     call per prompt — better recall but pricier)? Default `token_overlap`."
     → `memory_router` (string: `token_overlap` or `llm`).
   - **Workspace dir.** "Where's your workspace root? This is where
     `.multiplai/{memory,diary,now,learnings}` will live by default. Press
     enter for `~/.multiplai/`."
     → `workspace_dir` (path; optional).

6a. **Write plugin options to settings.json.**

   Locate the user's settings file:
   ```
   $CLAUDE_CONFIG_DIR/settings.json   # if CLAUDE_CONFIG_DIR is set
   ~/.claude/settings.json            # fallback
   ```
   Read it (or start `{}` if missing), then merge in:
   ```json
   {
     "pluginConfigs": {
       "multiplai": {
         "options": {
           "workspace_dir": "...",
           "enable_skills": true,
           "skills_dir": "...",
           "enable_resources": true,
           "resources_dir": "...",
           "memory_router": "token_overlap"
         }
       }
     }
   }
   ```
   Only include keys the user actually set. Preserve every other top-level
   key in `settings.json`. Write the file back with a 2-space-indented JSON
   dump.

   **Tell the user** the values will take effect on the next Claude Code
   restart, and that the next `/multiplai:refresh-catalogs` run will need
   to populate skills/resources catalogs if they were just enabled.

7. **Offer git version control for the memory directory.**
   Check whether `memory_dir` is already inside a git repository:
   ```
   git -C <memory_dir> rev-parse --is-inside-work-tree
   ```
   If it exits non-zero (or prints anything other than `true`), ask:
   > "Memory lives at `<memory_dir>` but isn't tracked by git. Memory changes
   > accumulate over time — without version control there's no way to recover
   > from accidental corruption or track how your preferences evolve. Should
   > I `git init` here and commit the starter files, {name}?"

   - **If yes:**
     1. `git -C <memory_dir> init`
     2. Write a minimal `.gitignore` inside `<memory_dir>`:
        ```
        *.lock
        *.tmp
        .DS_Store
        ```
     3. `git -C <memory_dir> add -A`
     4. `git -C <memory_dir> commit -m "initial multiplai memory"`
   - **If no:** Warn plainly: "Memory will not be version-controlled. You
     can always run `git init` in `<memory_dir>` later." Do not force the
     issue.

8. Confirm which files were written and suggest running `/multiplai:health`
   to verify. If `enable_skills` or `enable_resources` were turned on in
   step 6, also suggest restarting Claude Code and then running
   `/multiplai:refresh-catalogs --force` to populate the new catalogs.

## Important
- The two helper scripts have documented contracts above. **Do not** explore
  the plugin's source, dump its env vars, or read its other files. If you
  hit an unexpected error, surface it to the user and ask — don't go digging.
- Never hardcode paths — always go via `memory_dir` from `setup_check.py`'s
  output.
- Never run destructive git commands in `memory_dir`. Only `git init`,
  `git add -A`, and a first `git commit` after explicit consent.
