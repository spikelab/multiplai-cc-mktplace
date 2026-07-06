---
name: landing-page
description: |
  Landing page copy creation and optimization. Three modes: create (full creation from scratch),
  audit (CRO review of existing page), iterate (generate copy variations for specific sections).
  Uses the interviewer skill (multiplai-research) for discovery when available, applies evidence-based conversion frameworks.
  Triggers on "landing page", "create a landing page", "landing page copy",
  "review this landing page", "optimize this landing page", "landing page audit".
model: opus
effort: high
---

# Landing Page — Dispatcher

You are a thin dispatcher. Parse the mode from the user's invocation and load the matching sub-prompt file from this skill's directory.

## Arguments

| Arg | Description | Required |
|-----|-------------|----------|
| **mode** | One of: `create`, `audit`, `iterate` | Yes |
| **path** | File path to existing landing page (HTML or markdown) | Required for `audit` and `iterate` |

## Flow

1. Parse the user's invocation for `mode` and optional `path`
2. If no mode is given, show usage (see below) and stop
3. Read the matching sub-prompt file from this skill's directory: `{mode}.md`
4. Follow the instructions in that sub-prompt file exactly

## Usage (shown when no mode provided)

```
/landing-page <mode> [path]

Modes:
  create      Create landing page copy from scratch (discovery → copy → output)
  audit       CRO audit of an existing landing page
  iterate     Generate copy variations for specific sections

Examples:
  /landing-page create
  /landing-page audit ./landing-page.html
  /landing-page iterate ./landing-page.html
```
