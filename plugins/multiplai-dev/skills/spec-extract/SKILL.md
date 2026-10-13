---
name: spec-extract
description: Writes BDD capability specs, in the format of buildme's specs/registry/, from the Python code of a commit, a directory or a PR, with every scenario citing lines a gate re-read at that commit. This version resolves the target, chooses and locks its run folder, saves the run's state and extracts the commit for the agents; the scope, explore, write and apply stages come in later versions.
when_to_use: 'Triggers: spec-extract, reverse-engineer specs, write specs from code, /multiplai-dev:spec-extract'
model: sonnet
effort: medium
---

# spec-extract

Reads the Python code of one target and writes one capability file per
capability into a run folder. **This version stops after the first step**: it
resolves the target, chooses and locks the run folder, writes `state.json`,
extracts the commit into `snapshot/`, and prints
`SPEC_EXTRACT:RESOLVED <run>`. It makes no model call.

## Command

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT}/skills/spec-extract/scripts" python -m spec_extract_pipeline run \
  --repo /abs/path/to/repo --tree HEAD [options]
```

**Pass absolute paths, and always pass `--repo` for `--tree` and `--pr`.**
`uv run --directory` runs the command from the skill's `scripts/` folder, so a
relative `--repo`, `--dir` or `--out`, or a missing `--repo`, would name that
folder, not the user's checkout.

| Target | Reads |
|---|---|
| `--tree <commit> [--path <dir>]` | the commit, or one directory of it, in `--repo` |
| `--pr <n>` | the PR's head commit, in `--repo`; needs `gh`. The PR is read from `origin`'s repository, which is also where `--fetch` fetches from |
| `--dir <dir>` | a directory that is not under git: copied into `<run>/source/` and committed there, including files its `.gitignore` names; never written to |

| Option | Does |
|---|---|
| `--out <root>` | Where run folders go. Default `<data_dir>/spec-extract` (the multiplai data directory). |
| `--repo <dir>` | The checkout for `--tree` and `--pr`, as an absolute path. Without it the run reads the current directory, which under `uv run --directory` is the skill's `scripts/` folder. |
| `--fetch` | For `--pr`, fetch `origin` and the PR's head first. Without it, a commit missing from the clone is refused with the `git fetch` command to run. |
| `--restart` | Renames the run folder to `<object>.old-<YYYYmmddHHMMSS>` (never deletes) and starts it again. |

Running the same command again resumes the run in the same folder.

## Where results land

`<root>/<repo>/<object>/`:

- `<repo>` is the repository name from `origin`, without the owner (the
  directory name when there is no `origin`). A repository named `legacy` or
  `.locks` becomes `<name>@<owner>`.
- `<object>` is `tree@<sha12>`, `tree-<path>@<sha12>`, `pr-<n>@<head12>`, or for
  `--dir`, `dir-<basename>-<6 hex>`.
- A folder another remote's run wrote is left alone: the run uses
  `<repo>@<owner>/<object>/`. A folder another `--path` wrote (two paths that
  clean to one name) gives way to `tree-<path>-<6 hex>@<sha12>`.

Only one run may write a folder at a time: a run holds
`<root>/<repo>/.locks/<object>.lock` until it ends.

## Exit codes

| Code | Means |
|---|---|
| 0 | Done. |
| 1 | Failed, including a `state.json` that cannot be read, or a `--dir` run whose `<run>/source/` no longer holds its import commit (the message names `--restart`). |
| 2 | Refused: a bad target, a commit not in the clone, a `--dir` run folder that would be inside its source (for example `--out` the source's parent), or every candidate folder holds another target's run (pass `--out`). |
| 5 | Another process is running this target now. The message names its pid, host and start time. |

## What it writes outside its output

Nothing. `--tree` and `--pr` read git objects with `git ls-tree`,
`git cat-file` and `git show`; `--fetch` adds refs to the clone. `--dir` reads
the directory only.

`snapshot/` holds each file's bytes as git stores them, whatever the commit's
`.gitattributes` export rules say. A link that is absolute or leads out of the
tree is left out of `snapshot/`, and the run names it on stderr.
