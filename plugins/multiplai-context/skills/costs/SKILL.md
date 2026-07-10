---
name: costs
description: "Report API-equivalent costs for Claude Code usage — per chat, skill, subagent, project, model, day, git branch, or PR. Collects fresh data from session transcripts, then reports from the cost ledger. Triggers on 'what did this chat cost', 'how much did X cost', 'costs this month', 'token spend', 'cost report', 'what did that PR/branch cost'."
---

# Costs — Usage Cost Accounting

Reports what multiplai usage *would have billed* at Claude API list prices,
from the append-only cost ledger in `<data_dir>/costs/`. Two scripts:

- `collect_costs.py` — incrementally scans `$CLAUDE_CONFIG_DIR/projects/**/*.jsonl`
  and appends priced records to the ledger. Idempotent; first run is a full
  backfill, later runs read only new bytes.
- `costs_report.py` — aggregates the ledger.

## Steps

1. **Collect first** so the report includes recent sessions:
   ```
   uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/collect_costs.py"
   ```
2. **Report** based on what the user asked:

   | User asks | Command |
   |---|---|
   | overall / this month | `costs_report.py` |
   | a specific month | `costs_report.py --month YYYY-MM` |
   | this chat / a session | `costs_report.py --session <id-prefix>` (current session id is in the transcript filename) |
   | per skill | `costs_report.py --all --by skill` |
   | per project / model / day / component | `costs_report.py --by project\|model\|day\|component` |
   | per branch | `costs_report.py --all --by branch` |
   | one branch | `costs_report.py --branch <name>` (reads all months; `--branch "(none)"` for unattributed) |
   | a PR | resolve the branch with `gh`, then `--branch` — see below |

   All via `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/costs_report.py" ...`.
   Add `--json` when you need to post-process numbers.

3. Present the relevant numbers concisely. Always mention once: costs are
   **API-equivalent** (list-price USD) — on a subscription nothing was actually
   billed per call.

## Cost of a PR

`costs_report.py` is deliberately `gh`-free; PR→branch resolution happens here:

1. Resolve the PR's branch:
   ```
   gh pr view <num> --repo <owner>/<repo> --json headRefName -q .headRefName
   ```
   (batch: `gh pr list --repo <owner>/<repo> --state all --json number,headRefName`)
2. Report on it:
   ```
   uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/costs_report.py" --branch <that-branch>
   ```
3. If several PRs were cut from one branch, they **share** the branch's cost —
   say so when presenting the number.

## Branch attribution caveats

- `branch` comes from the transcript's per-entry `gitBranch` field, which
  reflects the repo at the session's **cwd**. Sessions run at the workspace
  root record the workspace repo's branch (usually `main`) even when the work
  targeted a sub-project via `git -C`. Sessions run inside a worktree or
  sub-project (the default workflow for non-trivial changes) attribute
  correctly. A mid-session `git checkout` splits records across branches.
- Records collected before this feature show `(none)` until the one-time
  enrichment is run:
  ```
  uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/collect_costs.py" --backfill-branches
  ```
  Idempotent; safe to re-run. Non-git cwds and SDK-sourced records stay `(none)`.

## Notes

- Skill spans inside interactive chats are approximate by construction (a
  skill is prompt injection, not a separate API context). SDK pipeline
  components (buildme, deep-research, dream) are exact.
- Subagent traffic is `sidechain` records; `--session` shows the
  main/subagent split.
- Unknown models are priced at fallback rates and flagged
  `pricing_fallback: true` in the ledger.
