# Components

How the tokens are meant to be used. Optional in a design system; pages may
ignore it.

- **Page:** `background: var(--bg)`, text `var(--fg)` in `var(--font-body)` at
  `var(--step-0)`, lines no wider than `var(--measure)`.
- **Card or panel:** `background: var(--surface)`, `1px solid var(--line)`,
  `border-radius: var(--radius)`, padding `var(--space-4)`.
- **Table header, inset well:** `background: var(--sunk)`.
- **Secondary text, captions, citations:** `var(--muted)` at `var(--step--1)`.
- **Links and the current item:** `var(--accent)`; a highlighted row or a
  quote uses `var(--accent-soft)` behind `var(--fg)`.
- **Status badge:** `var(--ok|warn|bad)` text on its `-soft` background.
- **Code:** `var(--code-fg)` on `var(--code-bg)` in `var(--font-mono)`.
- **Headings:** `var(--font-display)`, sizes `var(--step-1)` to `var(--step-4)`.
- **Spacing:** `var(--space-1)` (4px) to `var(--space-6)` (32px).
