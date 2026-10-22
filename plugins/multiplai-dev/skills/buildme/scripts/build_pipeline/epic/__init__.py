"""Epic mode: buildme builds the children pm-spec split a change into
(plan-buildme-epic-mode-2026-10-12.md; plan-pm-spec-split-2026-10-21.md,
Decision 11). The parent's `.change.yaml` lists `children` and each child's
lists `depends_on`; buildme reads those records and never splits a change.

- `phase.py` — the entry: `is_epic`, and `run_epic_from_specs`, which checks
  the children's records, readiness and commit, then runs the coordinator.
- `store.py` — the epic directory under the data directory, and `epic.json`.
- `lock.py` — the one lock on an epic directory, held once per process.
- `coordinator.py` — builds, reviews, fixes and merges the children.
"""

# Set to "1" in the environment of every child build the coordinator starts, so
# the child does not run spec-extract (`orchestrator._run_spec_extract_step`):
# siblings building at one commit would race for spec-extract's lock, and each
# would commit a registry baseline of its own. Nobody runs spec-extract for an
# epic: the parent's run builds nothing itself. The child's argv also carries
# `--epic-child`, which a hand re-run of its saved command keeps;
# `orchestrator._epic_child` reads either (#347).
CHILD_ENV = "BUILDME_EPIC_CHILD"
