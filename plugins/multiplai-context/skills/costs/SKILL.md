---
name: costs
description: "Report API-equivalent costs for Claude Code usage — per chat, skill, subagent, project, model, or day. Collects fresh data from session transcripts, then reports from the cost ledger. Triggers on 'what did this chat cost', 'how much did X cost', 'costs this month', 'token spend', 'cost report'."
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

   All via `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/costs_report.py" ...`.
   Add `--json` when you need to post-process numbers.

3. Present the relevant numbers concisely. Always mention once: costs are
   **API-equivalent** (list-price USD) — on a subscription nothing was actually
   billed per call.

## Notes

- Skill spans inside interactive chats are approximate by construction (a
  skill is prompt injection, not a separate API context). SDK pipeline
  components (buildme, deep-research, dream) are exact.
- Subagent traffic is `sidechain` records; `--session` shows the
  main/subagent split.
- Unknown models are priced at fallback rates and flagged
  `pricing_fallback: true` in the ledger.
