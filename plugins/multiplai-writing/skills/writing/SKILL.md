---
name: writing
description: Content creation toolkit with 6 modes — brief (extract structure from braindump), cmd-brief (resolve commands in briefs), draft (transform brief into full draft), editor (copy edit for style and AI-tells), linkedin (LinkedIn posts), imagen (image prompt generation).
user_invocable: true
model: opus
effort: medium
---

# Writing — Dispatcher

You are a thin dispatcher. Parse the mode from the user's invocation and load the matching sub-prompt file from this skill's directory.

## Arguments

| Arg | Description | Required |
|-----|-------------|----------|
| **mode** | One of: `brief`, `cmd-brief`, `draft`, `editor`, `linkedin`, `imagen` | Yes |
| **path** | File path (source material, brief, draft, or content depending on mode) | Mode-dependent |

## Flow

1. Parse the user's invocation for `mode` and optional `path`
2. If no mode is given, show usage (see below) and stop
3. Read the matching sub-prompt file from this skill's directory: `{mode}.md`
4. Follow the instructions in that sub-prompt file exactly

## Usage (shown when no mode provided)

```
/writing <mode> [path]

Modes:
  brief       Extract structure from raw braindump/transcript into a brief
  cmd-brief   Resolve <cmd> tags in a completed brief
  draft       Transform a brief into a full draft
  editor      Copy edit a draft for style guide adherence and AI-tell removal
  linkedin    Create a LinkedIn post
  imagen      Generate an image prompt for a piece of content

Examples:
  /writing brief posts/topic-transcript.md
  /writing draft posts/topic-brief.md
  /writing editor posts/topic-draft.md
  /writing linkedin
  /writing imagen posts/topic.md
```
