"""Prompt template for the end-of-build documentation update."""

DOCS_UPDATE_PROMPT = """\
The build is finished and its code is on disk. Your job is the last piece of the
same change: bring this project's **documentation** back in line with what the
code now does, so the docs land in the same pull request as the code they
describe.

You are working in `{project_dir}`. Read what is there before you write.

## Change: {change_name}

## What the build changed (full diff)

```diff
{diff}
```

## What the build's agents found surprising

{notes}

## How to do it

1. **Take inventory first.** Use Glob to find the documents this project
   actually keeps — `README*`, `CHANGELOG*`, and everything under `docs/**` —
   and Read the ones the diff could have made stale. The project's own
   conventions are the ones to follow; match the file's existing voice,
   heading style, and level of detail.
2. **Update every document the diff makes stale.** A document is stale when it
   describes behavior, an interface, a flag, a file layout, or a default that
   the diff changed, added, or removed. Fix exactly those parts.
3. **Refresh usage examples the diff invalidated.** A command, snippet, or
   sample output that would no longer work (or would now print something else)
   is wrong documentation — correct it to what the current code does.
4. **Add a changelog entry when the project keeps a changelog.** Write it in
   [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format — an
   `## [Unreleased]` section (or the project's existing equivalent) with
   `Added` / `Changed` / `Fixed` / `Removed` / `Deprecated` / `Security`
   subsections as they apply — from the **user's** point of view: what someone
   using this project can now do differently, not a commit dump.
   Create a `CHANGELOG.md` only when the project's own conventions show one is
   expected (a `CHANGELOG` referenced by the README, by contributing docs, or
   by a release process in the repo). When nothing suggests a changelog is
   kept, record that under DOCS_IMPACT and move on.
5. **Write only what the diff supports.** Every sentence you add must be
   traceable to a line in the diff or to code you read. If the diff shows no
   user-visible change, the honest answer is that no document needed updating.

## Report

Close your report with these REQUIRED slots, exactly these labels:

```
DOCS_IMPACT: <none, or a comma-separated list of the documentation files you changed, as paths relative to the project root>
STATUS: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>
```

`DOCS_IMPACT: none` is a real answer — use it when the diff changed nothing a
reader of the documentation would notice. Use `NEEDS_CONTEXT` or `BLOCKED` when
you could not tell from the diff and the code what a document should now say;
saying so is more useful than a guess.

**List every file you wrote to, without exception.** Only the files named here
get committed; anything you changed and did not list stays out of the pull
request entirely. If you edited something by accident, list it and say so —
that is recoverable, and a silent edit is not.
"""
