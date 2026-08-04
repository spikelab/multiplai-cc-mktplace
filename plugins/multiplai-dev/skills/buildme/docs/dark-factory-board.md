# The dark-factory board — what buildme drives, and what nobody drives

The long-term target for buildme is a kanban board with an **agent in every owner
seat** — no human picks up a card. This document exists to say, without
optimism, which columns a buildme run actually moves a card through today and
which ones are vocabulary only.

Every "covered" claim below carries `file:line` evidence. If a claim and the code
disagree, the code is right and this file is stale — fix it here.

Line numbers were re-derived on **2026-08-04** against the buildme subtree in
`plugins/multiplai-dev/skills/buildme/`. Paths are relative to
`scripts/build_pipeline/`.

---

## The board

buildme should eventually run this board end to end, covering **Shaping →
Testing** — not Deploying/Deployed (see [Deploy is out of scope](#deploy-is-out-of-scope)).

| # | Column | Owner | Card means | Leaves when |
|---|--------|-------|------------|-------------|
| 1 | Backlog | — | might work on it | product commits |
| 2 | Accepted | — | agreed, not started | product starts |
| 3 | Shaping | Product | actively spec'ing, consulting eng | spec ready |
| 4 | Planning | Eng | specs → impl plan, reviewed by another eng | plan approved |
| 5 | In Development | Author | plan being implemented; may bounce back to Shaping/Planning | branch pushed |
| 6 | In Review | Reviewer | branch reviewed (fetch + diff) before the prod PR | merged to staging |
| 7 | Testing | Product/QA | on staging; E2E/manual testing not possible in docker | prod PR opened |
| 8 | Ready for Prod | — | prod PR open, awaiting merge | prod PR merged |
| 9 | Deploying | Ops | picked up next deployment session | deployed |
| 10 | Deployed | — | terminal (done) | — |
| 11 | Cancelled | — | terminal (won't do) | — |

The eleven names are an enum — `models.py:66 BoardColumn`, values
`models.py:78-88` — so the same string appears in `.board.json`'s `column` field
and in the `BOARD:<change>:<Column>` stdout line, with no translation table.
**The enum is the board's full vocabulary, not a claim of coverage.**

---

## Coverage today

`board.py:116 column_for(phase, block_status)` is the only mapping, and it is a
pure function over the table at `board.py:91 _PHASE_COLUMNS` (exhaustive over
`BuildPhase`). Cards are written by `board.py:190 record` / `board.py:218 _record`.

| # | Column | Covered? | Evidence |
|---|--------|----------|----------|
| 1 | Backlog | **Never driven.** No phase maps to it; nothing in the pipeline writes it. | Absent from `board.py:91-112 _PHASE_COLUMNS`; stated in the module docstring `board.py:41-46` |
| 2 | Accepted | **Mapped, never written.** `BuildPhase.INIT` maps to Accepted, but no card is recorded that early — the change directory does not exist yet at INIT, so the first recorded column is always Shaping. | `board.py:92` (INIT → ACCEPTED); `board.py:30-34`; first actual write is `orchestrator.py:80` at BOOTSTRAP |
| 3 | Shaping | **Driven.** BOOTSTRAP, INTERVIEW_DONE, RESEARCH, CODEBASE_ANALYSIS and SPEC_GENERATION all map to Shaping and each records the card. Products: `codebase-analysis.md`, `proposal.md`, `requirements/*.md`. | `board.py:93-99`; recorded at `orchestrator.py:80`, `:108`, `:129`, `:142`, `:184` |
| 4 | Planning | **Driven.** DESIGN_AUDIT, PROTOTYPE and REVIEW map to Planning. The "reviewed by another eng" half of the column is stood in for by the adversarial design audit and the tasks-shape audit, plus the prototype's findings pass. | `board.py:100-102`; recorded at `orchestrator.py:201`, `:209`, `:229`; audits at `llm_steps/spec_steps.py:220 run_design_audit`, `spec_generator.py:495 _audit_tasks_shape`; prototype at `orchestrator.py:203-209` → `llm_steps/prototype_steps.py:36 run_prototype` |
| 5 | In Development | **Driven.** TDD_BUILD, DOCS_UPDATE and RESPEC all map to In Development, and the card is recorded **before** the engine runs, so a card is in development for the whole build rather than only once it succeeds. Every block-status change re-records the same column. Updating README/CHANGELOG/`docs/**` is part of producing the change, not a separate review stage, so `DOCS_UPDATE` deliberately shares the column rather than introducing a new one. | `board.py:103-107` (TDD_BUILD / DOCS_UPDATE / RESPEC → IN_DEVELOPMENT); recorded at `orchestrator.py:236` (pre-engine) and again at `:263` (DOCS_UPDATE) and `:294` (RESPEC); per-block at `tdd_engine.py:81` |
| 6 | In Review | **Driven, but only on a real push + PR.** `BuildPhase.PUBLISH` itself still maps to In Development; In Review is recorded from the publish step *after* `git push` succeeded **and** `gh pr create` returned. A run with `--no-push`, `--no-pr`, `--no-worktree`, or a failed push finishes in In Development. Its exit condition (merged to staging) is **not** implemented. | `board.py:108-110` (PUBLISH → IN_DEVELOPMENT); push at `orchestrator.py:1051`, PR at `:1068`, In Review recorded at `orchestrator.py:1093-1097`; publish skipped with no pipeline branch at `orchestrator.py:1023-1024` |
| 7 | Testing | **Never driven.** There is no staging deploy and no QA/E2E agent. The nearest thing is a post-TDD entry-point smoke check, which warns and does not move any card. | `board.py:41-46`; `tdd_engine.py:2237 _verify_entry_point`, called at `tdd_engine.py:1907`, records no board column |
| 8 | Ready for Prod | **Never driven.** No prod-PR automation exists. | `board.py:41-46`; no `BoardColumn.READY_FOR_PROD` outside `models.py:85` and `board.py:83` |
| 9 | Deploying | **Never driven.** No deploy machinery, by design. | `board.py:41-46` |
| 10 | Deployed | **Never driven.** Same. | `board.py:41-46` |
| 11 | Cancelled | **Driven, narrowly.** Recorded only when the run ended unrecoverably — defined as "no resumable checkpoint survives" (`.build-state.json` gone). An ordinary failed phase leaves the checkpoint, so the card stays in its last column and a resume continues from it. | `board.py:273 record_failure` (guard at `:285-292`); callers `orchestrator.py:152`, `:246`, `:377`; `board.py:112` (FAILED → CANCELLED) |

### `BlockStatus` moves no column

`column_for` accepts a `BlockStatus` and ignores it — every block state lives
inside In Development (`board.py:116-123`). In particular
**`BlockStatus.REVIEWING` is not In Review**: that review is in-process, against
the working tree, with no pushed branch for anyone to fetch
(`tdd_engine.py:1684 _run_quality_review`, `tdd_engine.py:2156 _run_final_review`).
The parameter exists because block state is the natural refinement point for a
future scheduler, and because "it does not currently move the card" needs to be
explicit rather than assumed.

### Summary

Agent-driven today: **Shaping → Planning → In Development → In Review**, plus
`Cancelled` at the edge. Everything else is vocabulary.

---

## The roadmap for the columns nobody drives

None of the following is implemented. They are listed so the gap is a plan
rather than an oversight.

### Column 6's exit — a reviewer agent and a staging merge

What makes In Review real today is that a branch is pushed and a PR exists
(`orchestrator.py:1051`, `:1068`) — a reviewer *can* `git fetch` and diff it.
Nothing does. The missing pieces:

- **A reviewer agent** that fetches the pushed branch, diffs it against the base,
  reads the change's `specs/` (proposal, requirements, design, `codebase-analysis.md`,
  `unknowns.md`, `respec.md`) and reviews the diff against them — a real out-of-process review,
  unlike `_run_final_review`, which sees only the working tree it just produced.
- **Staging-merge automation** to satisfy the column's stated exit condition
  ("merged to staging"). This is the first place the pipeline would ever merge
  anything, and today `git_ops` merges nothing at all: it never merges,
  force-pushes, or deletes a branch (`git_ops.py:1-20` module contract), and
  `git_ops.py:288 remove_worktree` is a documented helper deliberately never
  called by the pipeline.

### Column 7 — a QA/E2E agent for what docker cannot test

"On staging; E2E/manual testing not possible in docker" is the column's own
description of the problem. The pipeline's only end-of-build behavioral check is
`tdd_engine.py:2237 _verify_entry_point`, which asks whether the entry point
runs — it does not exercise a UI, a browser, or a deployed environment, and it
warns rather than failing. A QA agent for this column would need a real target to
test against (a staging deploy) and a driver outside the container; the
`multiplai-media` `host-browser` skill is the obvious candidate for the browser
half. Nothing is wired.

### Columns 8–9 — prod-PR open/merge automation: **explicitly deferred**

Opening the prod PR (column 7 → 8) and merging it (8 → 9) are deliberately out of
scope. The rule the current code follows is: **git automation stops at "PR
opened."** No merge to `main` or staging, no force-push, no branch deletion, no
worktree removal, no auto-approve. Deferring this is a safety choice, not a
backlog accident — a factory that can merge its own work to production has no
human checkpoint left.

---

## Deploy is out of scope

**Columns 9 (Deploying) and 10 (Deployed) get this paragraph and nothing else.**
buildme will not gain deploy machinery: no environment provisioning, no release
pipeline, no deploy trigger, no rollback. The columns exist in `BoardColumn`
(`models.py:86-87`) purely so a later scheduler has the vocabulary, and
`board.py`'s docstring records that the pipeline must not pretend to move a card
into them (`board.py:41-46`). Anything that deploys is a separate system with its
own plan and its own review.

---

## The board protocol

- **`.board.json`** — written to the change directory
  (`board.py:71 BOARD_FILENAME`, path resolved by `board.py:156 board_path`).
  Schema: `board.py:143 BoardCard` — `card_id`, `change_name`, `column`,
  `owner_agent`, `entered_at`, `branch`, `worktree_path`, `pr_url`, and a
  `history` of `{column, at, note}` events (`board.py:137 BoardEvent`).
- **`BOARD:<change>:<Column>`** on stdout — emitted only when the card actually
  moves columns (`board.py:259-263`), alongside the existing `PHASE:` protocol.
- **Owner seats** — `board.py:75 _OWNERS` names the seat per column (product,
  eng, author, reviewer, product-qa, ops). A dark factory puts an agent in each;
  today only the seats for columns 3–6 are ever occupied.
- **Card identity** — `board.py:131 card_id_for` returns `buildme-<normalized
  change name>`, stable across runs and resumes, which is what lets a future
  scheduler correlate them.
- **Never raises** — a board write failure logs and lets the build continue
  (`board.py:213-214`); a corrupt card is ignored (`board.py:179 read_card`).

### Honest caveats

- In `--auto` runs the archive move happens *before* PUBLISH
  (`orchestrator.py:297-314`), so the final In Review card is written into
  `specs/archive/<date>-<name>/.board.json` (`board.py:156-176`) and — because
  the archive commit already ran — that last write sits **uncommitted** in the
  worktree. The PR is open by then; the card file is bookkeeping, not code.
- `pr_url` is recorded from the **live** `BuildState`, never re-read from
  `.build-state.json`: after an `--auto` archive the state file is already gone
  (`board.py:203-206`, `orchestrator.py:326`).
- Reaching `BuildPhase.COMPLETE` is not by itself evidence that anyone can review
  anything — that is why In Review is recorded by the publish step and not by the
  phase mapping (`board.py:21-27`).
