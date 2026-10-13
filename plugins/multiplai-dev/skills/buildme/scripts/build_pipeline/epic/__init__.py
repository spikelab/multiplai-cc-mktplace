"""Epic mode: buildme splits an epic or PRD into several child plans and,
once approved, builds them (plan-buildme-epic-mode-2026-10-12.md).

- `split.py` — the EPIC_SPLIT model call, its inputs and the checks on its answer.
- `store.py` — the epic directory under the data directory, and its files.
- `lock.py` — the one lock on an epic directory, held once per process.
- `phase.py` — the EPIC_SPLIT step and the approval.
"""
