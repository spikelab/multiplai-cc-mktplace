"""Epic mode: buildme splits an epic or PRD into several child plans and,
once approved, builds them (plan-buildme-epic-mode-2026-10-12.md).

- `split.py` — the EPIC_SPLIT model call, its inputs and the checks on its answer.
- `store.py` — the epic directory under the data directory, and its files.
- `lock.py` — the one lock on an epic directory, held once per process.
- `phase.py` — the EPIC_SPLIT step and the approval.
"""

# Set to "1" in the environment of every child build the coordinator starts, so
# the child does not run spec-extract (`orchestrator._run_spec_extract_step`):
# the parent's CODEBASE_ANALYSIS ran it, and a child with no dependency in the
# epic's own repository branches from the parent's commit that holds the
# baseline (`coordinator.registry_commit`). Siblings at one commit would also
# race for spec-extract's lock. A child in another repository builds without a
# baseline.
CHILD_ENV = "BUILDME_EPIC_CHILD"
