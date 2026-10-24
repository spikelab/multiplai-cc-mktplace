---
name: design-system
description: Supplies the design tokens (colours, fonts, type scale, spacing, radius) an HTML page is styled with, from a folder of design systems the user keeps, or a neutral theme shipped here when none is configured. Lists, shows and checks a design system (every required token in light and both dark blocks, WCAG AA contrast for each text/background pair), and creates a new one from the neutral theme.
when_to_use: 'Triggers: before building an HTML page or report, use my design system, which design system, list design systems, check my design system, create a design system, /multiplai-dev:design-system'
effort: medium
---

# Design system

A design system here is a folder of tokens, not a component library. Pages
inline its `tokens.css` and style themselves with `var(--…)`. artifact-design
already says to apply an existing design system first; this skill tells you
which one exists and where it is.

Run every command through the skill's environment:

```bash
DS() { uv run --directory "${CLAUDE_PLUGIN_ROOT}/skills/design-system/scripts" python -m design_system "$@"; }
```

## What this skill does on the machine

- Reads two settings, from the environment first and then from
  `multiplai.conf` when that file exists:
  - `MULTIPLAI_DESIGN_SYSTEMS`: the absolute path (`~` allowed) of the folder
    that holds one subfolder per design system;
  - `MULTIPLAI_DESIGN_SYSTEM`: the default subfolder's name.
- With neither set, it uses the neutral theme in `assets/neutral/`.
- Reads the files of the design system it is pointed at.
- Writes only on `create`, which copies `assets/neutral/` to a new folder. It
  never overwrites one that exists.
- No network access, no credentials, no model calls.

## Folder layout

```
<MULTIPLAI_DESIGN_SYSTEMS>/<name>/
  tokens.css      required: the tokens (below)
  README.md       required: what the system is for and where it came from
  fonts.css       optional: @import or @font-face rules for web fonts
  components.md   optional: how the tokens are meant to be used
  assets/         optional: logos, images
```

`tokens.css` puts every token on bare `:root`, and then redefines the colour
tokens twice, each with `color-scheme: dark`:
- under `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }`;
- under `:root[data-theme="dark"]`.

Required tokens:
- **Colours** (in all three blocks): `--bg`, `--surface`, `--sunk`, `--fg`,
  `--muted`, `--line`, `--accent`, `--accent-soft`, `--ok`, `--warn`, `--bad`
  (each with a `-soft` pair), `--code-bg`, `--code-fg`.
- **Type and shape** (in the light block only): `--font-display`,
  `--font-body`, `--font-mono`, `--step--1` … `--step-4`, `--space-1` …
  `--space-6`, `--radius`, `--measure`.

## Jobs

### Use it (before building an HTML page)

1. `DS path` prints the folder of the default design system (neutral if none is
   set). `DS path <name>` prints the folder of a named one.
2. Read its `tokens.css`, plus `fonts.css` and `components.md` when they exist.
3. Build the page on those tokens. Inline `fonts.css`, then `tokens.css`, in
   `<style>` elements in `<head>`. Then use only `var(--…)` for colours, fonts,
   sizes and spacing; no hex values of your own.
4. A skill with its own renderer takes the folder as an option instead. For
   example, `codebase-walkthrough` takes `--design-system-dir "$(DS path)"`.

If `DS path` exits 1, show the user its message. It names the setting to fix,
or lists the design systems that exist. Do not fall back to neutral on your own.

### List, show, check

- `DS list` prints the folder and every design system in it, with `*` marking
  the default.
- `DS show [name]` prints the README and every token value.
- `DS check [name|path]` prints the contrast ratio of each text/background
  pair in each scheme. Then it prints `FAIL <problem>` for:
  - a missing file;
  - a required token missing from a block;
  - a dark block without `color-scheme: dark`;
  - a pair below 4.5:1;
  - a colour it cannot read (it reads `#hex` and `rgb()`, following `var()`);
  - `</style` in `tokens.css` or `fonts.css`.
  It ends with `check: ok` (exit 0) or `check: N problem(s)` (exit 1).

### Create one

1. Ask the user for a name, unless they gave one.
2. If `MULTIPLAI_DESIGN_SYSTEMS` is unset (`DS list` says "not set"), ask where
   the folder of design systems should live. It must be an absolute path.
3. Run `DS create <name>`, or `DS create <name> --dir <folder>`. It copies the
   neutral theme. It exits 1 without writing anything if `<folder>/<name>`
   already exists; then ask the user to remove it or pick another name.
4. When you used `--dir`, give the user the two lines to set, in their shell
   profile or in `multiplai.conf`:
   ```
   MULTIPLAI_DESIGN_SYSTEMS="<folder>"
   MULTIPLAI_DESIGN_SYSTEM="<name>"
   ```
5. Edit the new `tokens.css` and `README.md` with the user, then run
   `DS check <name>` until it prints `check: ok`.

For example, `DS create acme --dir ~/design-systems` gives
`~/design-systems/acme/`. Once the two settings point at it, `DS check acme`
checks it.
