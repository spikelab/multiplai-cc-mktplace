## Why

On PR #295, which has 29 known bugs from 25 Codex review runs, `/multiplai-dev:review` found a median of **1**. Prototypes run as workflows on the same PR show which approaches find more:

| Prototype on PR #295, 3 runs | Median found, of 29 |
|---|---|
| Current review | 1 |
| 9 agents, each reviewing the whole diff | 9 |
| 9 agents, one per piece of the diff, each allowed to read the whole checkout ("A") | 13 |
| A plus one agent per code path, in the same run | 16 |

Three gaps in the current review explain most of the misses:

- **Finders are too narrow.**
  - Today's chunk finders (`--extra-finders`) are off by default and run only in the first round.
  - They report only HIGH and MEDIUM findings.
  - They are told "Never read a whole file".
  - In prototype A, finders that owned one piece but could read everything found more bugs at lower find cost ($14 against $23 per run). They wrote a free-form report that a second call converted to findings. Verification of A's findings removed no real bug.
- **Bugs that span files along one command are missed.**
  - Per-path finders found R-009, R-017, R-042 and R-056, which no piece finder found. Each spans 2–3 files along one call path.
  - PR 1 (#374) already provides `code_facts.map_paths` to map those paths.
- **The rules finder never sees the binding rules.**
  - Seven of the 29 bugs break rules written in `docs/untrusted-content.md` and `docs/degradation-contract.md`.
  - `CLAUDE.md` only links to these docs.
  - The conventions finder reads only the text of `CLAUDE.md` and `coding-standards.md`, so no Opus setup found these 7 bugs.
  - Codex `code-review` quotes those docs and found all 7.

This is PR 3 of 4. It stacks on PR 2, and PR 4 depends on measuring this change.

## What Changes

- **New paths stage.**
  - It runs before find on every change review, and on tree reviews only with `--paths`.
  - It calls `map_paths` with the changed lines at head and `cap=21`. A tree review passes every line of the files under review as its changed lines.
  - It writes `paths.json` beside `findings.json`.
  - It caches its answers in the run state so `resume` asks only for what is missing.
  - Its spend counts toward `--max-cost-usd`.
  - Dropped entries, broken traces and a failed or empty mapping are recorded in coverage and `state.errors`. The review continues without paths. Only `RepoTrustError` and budget stops propagate.
- **Per-piece finders become the default for a change review.**
  - The diff is split by file, never inside a file. Each test file is paired with its module. Files are grouped by their nearest package or skill directory.
  - A piece holds about 50,000 characters of diff at most. A larger single file gets its own piece. There is no limit on the number of pieces.
  - Release and repo metadata share one "meta" piece. Lockfiles stay out of the diff text.
  - Finders may read the whole snapshot, report all severities, and run every round.
  - `--extra-finders` is still accepted but does nothing, and prints a one-line notice.
- **One finder per mapped path.** Each follows its path end to end and reports defects on the path or in how the path uses changed code.
- **Report-then-convert for piece and path finders.**
  - The finder writes a plain-text report. A second call converts it into findings with exact quotes, keeping every item and judging none.
  - Each report is stored in state as soon as it returns, so a resume repeats only the convert call. It is also written as a top-level file, such as `report-piece-3.md` or `report-path-p07.md`, so kept rounds keep it.
  - Reports and path data are fenced and defanged as untrusted content through `multiplai_core.untrusted`.
  - Advice maps onto the existing `Finding` with LOW severity when no severity is stated. The `kind` of each item is recorded only in the find entries of `checks.json`.
- **Binding docs go to the conventions finder.**
  - It also gets every repository-local Markdown file linked from a `CLAUDE.md` or `coding-standards.md` it already reads.
  - Links are taken at head, plus links that exist only at base.
  - Links on lines saying `bind`, `must` or `required` come first.
  - All of it stays within `CONVENTIONS_MAX_CHARS`, and docs that do not fit are recorded as skipped.
  - A changed line that breaks a rule in one of these docs is a finding.
- **Path ids on findings.**
  - After merge, each finding gets an optional `paths` list.
  - A finding lies on a path when one of its citations is in the same file as a hop or the entry, and the citation's line range contains that line.
  - Citations in `.review-deps/` never count.
- **Report "Paths" section.** For each path the report shows its entry, who invokes it, how many findings lie on it, and its e2e tests. Tests are listed as counted, mocked (not counted), or "no e2e test". Nothing runs tests.
- **Existing dimension finders stay unchanged.** These are `diff-bugs`, `callers`, `history`, `conventions`, `tests` and `trim`.
- **Cost guidance.**
  - `--plan-only` and `SKILL.md` state the expected cost: about $14 for pieces plus $24 for paths on a 227 KB diff.
  - They warn that the 50 USD default will stop a large change review partway through.
  - The review warns about cost but never refuses to start.

**Goals**
- Raise the bugs found per review run toward the prototype's median of 16 of 29 on PR #295.
- Catch bugs that break the repository's linked binding rules.
- Show, for each changed command or entry point, which findings lie on it and whether an e2e test covers it.
- Keep every stage resumable and bounded by `--max-cost-usd`, with no paid result lost when a run stops.

**Non-Goals**
- Removing or changing any existing dimension finder.
- A spend-priority scheme between finder kinds. The ledger stays a circuit breaker.
- Changing citation gates, verification or merge, or adding `kind` to `Finding` or `findings.json`.
- Letting agents run code or shell commands. They stay on Read, Grep and Glob.
- Changing `review-viewer` or `code_facts`.
- Making a benchmark target a merge gate. The PR #295 rerun happens after merge, as input to PR 4.
- Fixing the existing review `prompts.fence()` gap beyond the new prompts.

## Capabilities

### New Capabilities
- `review-paths-stage`
- `piece-splitting`
- `piece-finders`
- `path-finders`
- `report-then-convert`
- `binding-rules-docs`
- `finding-path-ids`
- `report-paths-section`
- `find-cost-guidance`

### Modified Capabilities

## Impact

- **Code.** All under `plugins/multiplai-dev/skills/review/scripts/review_pipeline/`:
  - `stages/`: a new paths stage is added. The stage order changes so that paths runs before `find`.
  - `stages/find.py`: finder keying changes. Piece and path keys are added beside the dimension keys, and the `extra@<i>` path becomes a no-op.
  - `chunks.py`: piece splitting replaces `finder_chunks`, including its cap of 6.
  - `prompts/find.py`: `build_extra` is removed, and "Never read a whole file" goes with it.
  - `prompts/`: new piece, path and convert prompts are added.
  - `conventions_blocks` gains link following.
  - The run state model gains stored path answers and stored finder reports.
  - Merge gains a post-step that assigns path ids.
  - The Markdown report renderer gains the Paths section.
  - `--plan-only` output gains the cost estimate.
  - `rounds.py` already copies top-level files in the target directory, so per-finder reports survive kept rounds.
- **Schema and output.**
  - `schema/findings.v1.schema.json`, or the version the review writes, gains an optional `paths` array per finding. Existing readers keep working.
  - A new `paths.json` follows `code_facts`' `paths.v1.schema.json`.
  - `checks.json` find entries gain each item's converted `kind`.
- **CLI.**
  - `--extra-finders` and `extra_finders` become no-ops that print a notice.
  - `--paths` enables the paths stage for `--tree` and `--dir` reviews.
- **Dependencies.**
  - `code_facts` (from PR 1): `map_paths`, `PathsResult` and `write_paths_file`. Its answer cache lives in the run's state folder.
  - `multiplai-core` (`multiplai_core.untrusted`): fences and defangs untrusted text.
  - The `exp/review-general-prompt` commit `658206d2` is the starting point for report-then-convert.
- **Cost and runtime.**
  - Per-run model spend and the number of agents per change review go up a lot, by about $38 on a 227 KB diff before the dimension finders.
  - Under the 50 USD default, large reviews will stop and need `resume` with a higher `--max-cost-usd`.
- **Docs and release.**
  - `plugins/multiplai-dev/skills/review/SKILL.md` covers the paths stage, the three finder kinds, binding docs, path ids, the Paths section and expected cost.
  - The multiplai-dev version is bumped, with a `CHANGELOG.md` entry checked by `scripts/check_changelog.py`.
- **Tests.**
  - The review suite gains tests for:
    - the paths stage: write, cap and resume
    - piece splitting
    - report-then-convert for pieces and paths
    - binding-doc order, budget and skips
    - path ids on findings
    - the Paths section
  - The full repository CI must pass.
- **Unaffected.**
  - `review-viewer`.
  - The `code_facts` capabilities already in the registry: `entry-point-discovery`, `path-tracing`, `path-scope-and-cap`, `e2e-test-listing`, `paths-output-contract`, `trace-gate` and `entry-point-model-fallback`. This change only consumes them.