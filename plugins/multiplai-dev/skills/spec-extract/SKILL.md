---
name: spec-extract
description: Writes BDD capability specs, in the format of buildme's specs/registry/, from the Python code of a commit, a directory or a PR, with every scenario citing lines a gate re-read at that commit. This version resolves the target, chooses and locks its run folder, lists the Python files and their public symbols, and asks one read-only agent per slice of code what it does, keeping only facts whose cited lines are at the commit; the group, write and apply stages come in later versions.
when_to_use: 'Triggers: spec-extract, reverse-engineer specs, write specs from code, /multiplai-dev:spec-extract'
model: sonnet
effort: medium
---

# spec-extract

Reads the Python code of one target and writes one capability file per
capability into a run folder. **This version stops after the explore step**:

1. **Resolve.** Resolves the target, chooses and locks the run folder, writes
   `state.json`, extracts the commit into `snapshot/`, and prints
   `SPEC_EXTRACT:RESOLVED <run>`.
2. **Scope.** Lists the `.py` files of the target (for `--pr`, those the PR
   changed that exist at its head, read whole), leaving out binary files, files
   over 200,000 characters, files marked `linguist-generated` or
   `linguist-vendored`, submodules and symbolic links. `ast` lists each
   module's public functions, classes and methods; a file it cannot parse is
   "not analysed". Test files are read as evidence, never specified. The files
   are split into explore units (at most 6,000 lines each); more than
   `--max-units` units is refused. Writes `plan.txt`.
3. **Explore.** One agent per unit, four at a time, with `Read`, `Glob` and
   `Grep` on `snapshot/` only. A unit whose agent run fails is asked once
   more; one with no answer after two attempts is listed as not analysed. An
   answer that does not parse is sent back once to be reformatted (one turn,
   no tools); when the budget or the backend stops that reformat, the attempt
   still counts, since its first run was paid for. A model setting no call can run with (an unknown provider,
   `claude-agent-sdk` missing, or for a `pi:` model the `pi` binary missing or
   failing to start) stops the run with exit 1 and counts no attempt, so the
   same command works once the setting is fixed. Every fact must cite lines
   a program re-reads at the commit with `git show`; the rest are dropped.
   Writes `facts.json` and `report.md`, and prints
   `SPEC_EXTRACT:EXPLORED <answered>/<units>`.

**It sends the target's code to a model.** The repository's own files become
part of what the model reads, so the run needs the user to say they trust it:
`--trust-repo`, or `SPEC_EXTRACT_TRUST_REPO=1`. Without it the run stops
before its first model call with exit 4. Never pass `--trust-repo` on your
own judgment: if the run exits 4, ask the user whether they trust the
repository, and re-run with `--trust-repo` only on a yes. `--plan-only` makes
no model call and needs no trust.

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
| `--trust-repo` | Say the repository is trusted: its code is sent to a model (same as `SPEC_EXTRACT_TRUST_REPO=1`). |
| `--max-usd <usd>` | Spend cap for this run folder, default 80. It counts every run that resumes the folder, so re-running never gets a fresh cap. |
| `--max-units <n>` | Refuse a target that splits into more than n explore units (default 20); narrow it with `--path`. |
| `--plan-only` | Resolve and scope, print the units and how many calls a run would make, write `plan.txt`, and stop before any model call. |

Running the same command again resumes the run in the same folder: it asks
only the units with no answer, and pays for no call whose answer was saved.

Models: `multiplai.conf` key `spec_extract_explore_model`; unset means the
session's model.

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
| 1 | Failed, including a model setting no call can run with (nothing is counted), a `state.json` that cannot be read, or a `--dir` run whose `<run>/source/` no longer holds its import commit (the message names `--restart`). |
| 2 | Refused: a bad target, a commit not in the clone, a `--dir` run folder that would be inside its source (for example `--out` the source's parent), every candidate folder holds another target's run (pass `--out`), no Python file to read (`SPEC_EXTRACT:NO_PYTHON`), or more explore units than `--max-units`. |
| 3 | The run folder's spend reached `--max-usd`. Everything paid for is saved; re-run with a higher `--max-usd` to continue. |
| 4 | The repository was not marked trusted. Ask the user whether they trust it, and re-run with `--trust-repo` only on a yes. |
| 5 | Another process is running this target now. The message names its pid, host and start time. |

## What it writes in the run folder

`state.json` (the target, the scope, every answer and the spend so far),
`snapshot/`, `plan.txt`, `facts.json` (the checked facts, in review's
`facts.v1` format) and `report.md` (the target and commit, the files left out
and why, and the files not analysed). Only `state.json` is read back; the
other files are rewritten from it, so editing them changes nothing.

## What it writes outside its output

Nothing. `--tree` and `--pr` read git objects with `git ls-tree`,
`git cat-file` and `git show`; `--fetch` adds refs to the clone. `--dir` reads
the directory only.

`snapshot/` holds each file's bytes as git stores them, whatever the commit's
`.gitattributes` export rules say. A link that is absolute or leads out of the
tree is left out of `snapshot/`, and the run names it on stderr.
