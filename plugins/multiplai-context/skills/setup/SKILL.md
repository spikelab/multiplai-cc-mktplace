---
name: setup
description: "Onboarding — a 2-question quick setup by default; pass `full` for the complete interview. Populates memory files from starter templates."
---

# Multiplai Setup — Onboarding

You are the multiplai onboarding interviewer. Two modes, chosen by the skill
argument:

| Invocation | What happens |
|---|---|
| `/multiplai-context:setup` | **Quick path** (default) — two questions, settings written, one restart at the very end. ~2 minutes. |
| `/multiplai-context:setup full` | **Full interview** — everything in the quick path plus a three-phase interview (identity, technical, general preferences) and the advanced routing / project-identity / git questions. Run it any time; it deepens what quick setup started. |

## Helper scripts (exact contracts)

Both helpers take **no extra arguments** (other than the optional `--force`
on `setup_write.py`). Both print a single JSON object to stdout. Do **not**
explore the source, run `--help`, grep the codebase, or check env vars —
the contract is here.

### `setup_check.py`
```
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/setup_check.py"
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
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/setup_write.py"            # copy-if-absent
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/setup_write.py" --force    # overwrite all
```
Copies starter templates → `memory_dir`. **Does NOT ingest interview answers** —
it just lays down the starter files. Returns:
```json
{
  "memory_dir": "...",
  "templates_dir": "...",
  "copied": ["me.md", "technical-pref.md", "preferences.md"],
  "skipped": []
}
```

The three files this lays down: `me.md`, `technical-pref.md`, `preferences.md`.
After they're written, you edit them with the user's answers.

## Quick path (default)

Exactly two questions. Do not add more — every other decision has a working
default and belongs in the full interview.

0. **Warm the plugin environment** (usually a no-op — the first hook fire
   already started the same build in the background):
   ```
   sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" --warm
   ```
   Run it synchronously and wait. If the background build is still in
   flight this waits on uv's project lock and then no-ops; if the
   environment is already built it returns in under a second.

   Use this command, not a bare `uv sync`. The hooks gate themselves
   behind an in-flight marker (`scripts/.warmup/`) while the environment
   builds, and `--warm` is the only path that clears it. A bare `uv sync`
   leaves the marker in place, so every hook stays gated for up to another
   15 minutes — which is exactly the onboarding this step exists to
   unblock.

   A non-zero exit means the environment could not be built, and uv's
   own error is in this command's output. If `uv` itself is missing, stop
   here and tell the user: the plugin needs [uv](https://docs.astral.sh/uv)
   (`curl -LsSf https://astral.sh/uv/install.sh | sh`) — nothing below
   can run without it.

1. Run `setup_check.py`. Read `memory_dir`, `existing`, `missing` from the JSON.

2. If `existing` is non-empty, warn the user — name the files — and ask whether
   to skip onboarding, fill only the missing ones, or `--force` overwrite.
   (A re-run safety gate, not one of the two questions; it never fires on a
   fresh install.)

3. **Question 1 — name.** *"Before we start, what should I call you?"*
   Capture the answer (or a preferred nickname) and **use it in every
   subsequent message.**

4. **Question 2 — workspace.** *"{name}, where should your memory and session
   state live? Say **default** for `~/.multiplai/`, or name a directory you
   already work in — state then goes to `<that dir>/.multiplai/`."*
   → `workspace_dir` (optional; if they take the default, skip the settings
   write in step 6 entirely).

5. Run `setup_write.py` (no flags) to lay down the templates. Then **edit
   `me.md`** in `memory_dir` with the `Edit` tool: put the user's name (and
   anything else they volunteered) into the Identity section, replacing the
   placeholder comment. Leave the other templates as they are — the full
   interview, or organic learning over time, fills them.

6. **Write plugin options to settings.json** — only if the user set a
   non-default workspace.

   Locate the user's settings file:
   ```
   $CLAUDE_CONFIG_DIR/settings.json           # if CLAUDE_CONFIG_DIR is set
   <user home>/.claude/settings.json          # fallback
   ```
   Read it (or start `{}` if missing), then merge in. The key is the compound
   `<plugin>@<marketplace>` form, exactly as below — a bare `multiplai` key
   fails **silently**: Claude Code ignores it and every option falls back to
   its default:
   ```json
   {
     "pluginConfigs": {
       "multiplai-context@multiplai": {
         "options": {
           "workspace_dir": "..."
         }
       }
     }
   }
   ```
   Only include keys the user actually set. Preserve every other top-level
   key in `settings.json`. Write the file back with a 2-space-indented JSON
   dump. (Sideloaded installs via `claude --plugin-dir …` ignore
   `pluginConfigs` — pass options as `CLAUDE_PLUGIN_OPTION_<KEY>` env vars
   there instead, where `<KEY>` is the option key **uppercased**:
   `workspace_dir` → `CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR`. The lowercase
   spelling is never read.)

   This file configures **all of Claude Code** — a malformed write is worse
   than no write. So: before writing, copy the existing file to
   `settings.json.bak` (skip if it didn't exist); after writing, confirm it
   parses — `python3 -m json.tool <path> > /dev/null` — and restore the
   backup if it doesn't.

   Do **not** mention restarting here — that comes once, in step 7.

7. **Wrap up — the only restart.** Print this walkthrough, adapted to their
   answers ({name}, real workspace path):

   > Setup done, {name}. One restart makes it live — this is the only one:
   >
   > 1. Leave this session (`/exit`), then start a new one (`claude`).
   > 2. Ask me: *"What do you know about me?"*
   > 3. You should get your own words back — your name, your workspace —
   >    because the relevant memory files now arrive with every prompt
   >    (the `MEMORY` block).
   > 4. To see the machinery decide: `tail -5 <workspace>/.multiplai/data/logs/activity.log`
   >    — the `[context]` line names exactly which memory files were injected,
   >    with relevance scores.
   >
   > Want deeper memory — role, stack, how you like to work? Run
   > `/multiplai-context:setup full` any time. `/multiplai-context:health`
   > checks the plumbing.

   No earlier step may tell the user to restart; nothing takes effect before
   this restart anyway, so a mid-flow notice is pure noise.

## Full interview (`/multiplai-context:setup full`)

Run quick-path steps 0–5 first — but skip any question already answered (if
`me.md` exists and carries a name, greet them by it and confirm rather than
re-ask). Then continue:

F1. Conduct a short interview in three phases. Keep it tight — aim for 2-4
   questions per phase, not 10. Address the user by name throughout.
   - **Identity** (→ `me.md`): role, background, location/timezone if relevant,
     communication style.
   - **Technical preferences** (→ `technical-pref.md`): primary languages,
     frameworks, tools, coding style preferences (testing, comments, etc.).
   - **General preferences** (→ `preferences.md`): verbosity, tone, push-back
     style, workflow habits (commit cadence, branch model, etc.).

F2. **Edit each of the three files** with the answers you collected, replacing
   the template placeholders with the user's actual responses. Use the `Edit`
   tool — don't regenerate a whole file from scratch unless the template is
   unrecognisable.

F3. **Routing scope — ask what the context router should pull from.**

   The router always pulls from memory and diary. Skills are opt-in because
   they cost LLM calls during catalog generation and only help if the user
   actually keeps skills in a standard location.

   **Do not offer a resources directory here.** Resources are retrieved
   through a qmd index, which needs qmd installed and a collection built on
   the host — neither is something this interview can do, and turning
   `enable_resources` on without them injects nothing. If the user raises it,
   point them at `/multiplai-context:qmd-search`, whose
   `scripts/setup_qmd.sh` does the indexing, and leave the option unset.

   Ask each question and capture answers; they are written to `settings.json`
   in F4. (Workaround for [#39455](https://github.com/anthropics/claude-code/issues/39455) —
   Claude Code currently does not prompt for `userConfig` values declared
   in `plugin.json`, so we collect them here.)

   - **Skills routing.** "Should the router suggest skills based on your
     prompts? (yes/no, default no) — Skills live under a directory you
     point at; the default is the platform skills directory documented in
     the plugin's `userConfig.skills_dir`. {name}, do you have a non-default
     location?"
     → `enable_skills` (bool), `skills_dir` (path; falls back to the
     plugin's default if left blank).
   - **Memory router strategy.** "Memory routing: `token_overlap` (fast,
     offline, free) or `llm` (semantic match via Sonnet, one extra LLM
     call per prompt — better recall but pricier)? Default `token_overlap`."
     → `memory_router` (string: `token_overlap` or `llm`).

F4. **Write all collected options to settings.json** — same file location,
   same compound key, and same merge rules as quick-path step 6. With the
   routing answers included the merged block looks like:
   ```json
   {
     "pluginConfigs": {
       "multiplai-context@multiplai": {
         "options": {
           "workspace_dir": "...",
           "enable_skills": true,
           "skills_dir": "...",
           "memory_router": "token_overlap"
         }
       }
     }
   }
   ```
   Only include keys the user actually set. If skills was just enabled,
   remember for the wrap-up: `/multiplai-context:refresh-catalogs --force`
   will need to run (after the restart) to populate the new catalog.

F5. **Project identity — how sessions map to projects.**

   The SessionStart hook injects a per-project "now" status snapshot. To do
   that it must map each session's working directory onto a stable project
   name. The default needs no configuration; the rest is opt-in for
   workspace/monorepo layouts.

   Ask {name}:
   - "By default I treat the **git repository** you're working in as the
     project — that works out of the box. Do your projects instead live
     under one parent directory?" If yes: "What's that parent path? I'll
     treat each immediate subfolder as its own project."
     → `project_roots` (a list; e.g. a workspace-rooted `<workspace>/PROJECTS`).
   - "Is your workspace root itself an umbrella that holds many projects
     rather than being a project of its own? If so I'll file workspace-level
     sessions under `workspace` instead of guessing a name."
     → `umbrella_roots` (a list).

   Persist the answers to `.multiplai/project-map.yaml` at the workspace root
   (it sits beside `memory/`, `diary/`, and `now/`). Include only the keys the
   user actually set:
   ```yaml
   detection: git            # git | basename | roots   (default: git)
   project_roots:
     - <workspace>/PROJECTS
   umbrella_roots:
     - <workspace>
   ```
   Resolution order is: `project_roots` (a cwd under one resolves to the first
   subfolder beneath it) → `umbrella_roots` (→ `workspace`) → the `detection`
   default (`git` repo name, with worktrees collapsed onto the main repo) →
   the cwd's basename. If {name} is happy with the git default and has no
   parent dir or umbrella, skip the file entirely — defaults apply.
   (`/multiplai-context:now` rebuilds the snapshots on demand.)

F6. **Offer git version control for the memory directory.**
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

F7. **Wrap up.** Confirm which files were written, then print the quick-path
   step-7 walkthrough (the single restart + first recall). If skills was
   enabled in F3, append: "after that, run
   `/multiplai-context:refresh-catalogs --force` once to populate the new
   catalog."

## Important
- The two helper scripts have documented contracts above. **Do not** explore
  the plugin's source, dump its env vars, or read its other files. If you
  hit an unexpected error, surface it to the user and ask — don't go digging.
- Never hardcode paths — always go via `memory_dir` from `setup_check.py`'s
  output.
- Never run destructive git commands in `memory_dir`. Only `git init`,
  `git add -A`, and a first `git commit` after explicit consent.
- One restart, at the end, whichever mode ran. Never emit a mid-flow
  restart notice.
