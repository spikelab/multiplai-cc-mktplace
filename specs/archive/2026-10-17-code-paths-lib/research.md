# Splitting the review by piece: prototype A and its control on PR #295

Written 2026-10-16, updated 2026-10-17 with the control. Pipeline facts were checked
against `PROJECTS/multiplai-cc-mktplace` at `438e5e1a`. Earlier benchmark numbers come from
`INBOX/review-lessons-pr295-2026-10-16.md` and
`INBOX/review-benchmark-pr295-2026-10-16/summary.md`. Lines marked *judgment* have no
evidence behind them.

## Latest: following call paths, and hints from fix history (2026-10-17)

Two more arms ran 3 times each on PR #295. They were converted and scored like the
control, without verification. The script is `paths-hints-inputs/workflow.js`; outputs are
in `paths-r{1,2,3}/` and `hints-r{1,2,3}/`.
- **Paths.** One agent listed 11–12 entry points that reach changed code (CLI commands,
  functions other programs call), each with its call path. Then one finder per path
  followed it end to end with the plain brief.
- **Hints.** One agent read the fix commits reachable from the PR base and wrote 15 kinds
  of bug this repo tends to produce (`paths-hints-inputs/bug-kinds.md`). A's 9 piece
  finders then ran with that list added to the plain brief.

| Arm | Per run, of 29 | 3 runs combined | Of the 6 code bugs A missed |
|---|---|---|---|
| A | 13 / 12 / 15 | 16 | none |
| Paths | 15 / 13 / 12 | 17 | R-009, R-017, R-042, R-056 |
| Hints | 14 / 14 / 14 | 18 | R-009, R-017, R-042, R-056 |
| A + paths, same run | 17 / 15 / 16 | 20 | the same 4 |
| A + hints, same run | 16 / 16 / 19 | 20 | the same 4 |
| A + paths + hints, same run | 18 / 19 / 19 | 20 | the same 4 |

- **Each arm found 4 of the 6 bugs A missed.** No arm found R-038 or R-043. Codex found
  R-038 in 3 of 25 runs and R-043 in 1.
- **All Opus arms together found 20 of the 22 code bugs.** Paths found all 4
  high-severity bugs in every run, and hints in 2 of 3 (run 3 missed R-002). None found a rule bug; the rules finder
  with the binding docs has not been built yet.
- **What each arm drops when run alone.** Paths alone missed R-026, R-035 and R-065
  (template parsing), which A found. Hints alone missed R-020 and R-035.
- **Noise.** 70–87 findings per run, and the scorers counted 1 false positive across the
  6 runs. The findings were not verified.
- **Cost:** paths $72.60 for 3 runs, about $24 a run. Hints $56.61, about $19 a run,
  including $0.88 to mine the history once.
- **Checked.**
  - The history agent ran 13 commands, every `git log` on the base commit, and none
    touched HEAD, the PR range or the inputs.
  - No finder read outside the scratch checkout. Two finders and the history agent read
    Claude Code's own saved copies of their large tool outputs.
  - No finder ran code. The `python` hits are text inside grep patterns; only the scorers
    ran Python, to edit their own score files.
  - The scorers' matches were not spot-checked this time.
- **Same caveats as before:** one PR, one model scorer per run, and an answer key built
  from Codex findings.

## Result

**Giving each agent one piece of the diff found more bugs than giving 9 agents the whole
diff, and the find stage cost less.**
- Prototype A had 9 agents, one per piece. It found a median of **13** of the 29 known
  bugs.
- The control had 9 agents that each reviewed the whole diff. It found **9** in every
  run.
- Everything else was the same: the brief, high effort, no running of code, and the
  conversion step.

| Arm, PR #295, 3 runs each | Bugs found per run, of 29 | Median | High-severity, of 4 | Cost per run |
|---|---|---|---|---|
| `/multiplai-dev:review` on Opus 5.5 | 0 / 3 / 1 | 1 | 0–1 | $8–14 |
| One plain Opus 5.5 agent, config stripped | 4 / 3 / 4 | 4 | 1–2 | $1.53–1.62 |
| One high-effort agent, from the control's 27 | 2–7 | 3 | not counted | ≈ $2.30 |
| Codex, plain prompt | 12 / 5 / 8 | 8 | 3–4 | not recorded |
| **Control: 9 whole-diff agents, combined** | **9 / 9 / 9** | **9** | **4 / 3 / 3** | **$23** find + convert |
| **Prototype A: 9 per-piece agents, combined** | **13 / 12 / 15** | **13** | **4 / 4 / 4** | **$14** find + convert, $32 with verify |

What each step added:
- **High effort did not help a single agent.** The control's 27 reviewers each found 2–7
  bugs, a median of 3. The plain agent, effort unrecorded, found a median of 4. The
  per-reviewer counts come from the scorers' `roots_by_reviewer` lists, not from separate
  scoring (`control-inputs/control-usage.json`).
- **Combining 9 whole-diff agents took 3 to 9.**
- **Splitting the diff took 9 to 13**, and the find and convert stages cost $14 a run
  against $23.

Whole-diff agents keep finding the same bugs:
- R-024 was found by 8 of 9 reviewers in every control run. R-029 was found by 7 to 8.
- 5 bugs were found by A and never by the control: R-021, R-026, R-028, R-035, R-065.
  A found R-021 and R-028 in all 3 runs.
- All 5 are in the internals of the `pr` skill: template parsing in `template.py`, base
  measurement in `github.py`, and the output stream in `commands.py` (ledger head
  locations).
- 2 bugs were found by the control and never by A: R-009 and R-042.

Combined over 3 runs: A found 16 different bugs, the control 13, and Codex with the plain
prompt 12.

## Compared with each Codex skill

The 29 bugs are the combined findings of 15 Codex skills over 25 runs. No single Codex
skill found more than 17 of them, even over its 3 runs combined. Codex counts are from
`review-skills-pr295-comparison-2026-10-15/results/aggregation/defect-by-run.csv`.

| Arm, 3 runs on PR #295 | Per run, of 29 | Median | 3 runs combined | Rule bugs combined, of 7 | High-severity combined, of 4 |
|---|---|---|---|---|---|
| Codex `ce-code-review` | 8 / 9 / 15 | 9 | 17 | 2 | 3 |
| Codex, plain prompt | 12 / 5 / 8 | 8 | 12 | 0 | 4 |
| Codex `find-bugs` | 6 / 8 / 7 | 7 | 9 | 0 | 4 |
| Codex `code-review` | 7 / 8 / 7 | 7 | 11 | 7 | 0 |
| Codex `sentry-code-review` | 6 / 4 / 5 | 5 | 7 | 0 | 3 |
| Opus `/multiplai-dev:review` | 0 / 3 / 1 | 1 | 3 | 1 | 1 |
| Opus, plain prompt | 4 / 3 / 4 | 4 | 5 | 0 | 2 |
| Opus control, 9 whole-diff agents | 9 / 9 / 9 | 9 | 13 | 0 | 4 |
| Opus prototype A | 13 / 12 / 15 | 13 | 16 | 0 | 4 |

The other 10 Codex skills ran once each and found 0–3. The Opus rows' combined counts
come from each run's `score.md`. Codex cost is not recorded, and comparison.md reports
that some Codex skills launched more than one agent.

## Where it is still short

- **The 7 bugs that break the repo's written rules.** Codex `code-review` found all 7
  across its 3 runs (4–5 per run). Opus found 2 in 18 runs on this PR: R-049 in review
  run 2, and R-007 in the first plain Opus subagent run (`no-skill-opus/score.md`). Codex
  with the plain prompt found none. So the difference is what the reviewer is told to
  check, not the model. *Judgment:* a finder told to check the change against the repo's
  written rules is the thing to try.
- **6 of the 22 code bugs** were not found by A in any run: R-009, R-017, R-038, R-042,
  R-043 and R-056. Codex with the plain prompt found 4 of them, and the control found 2.
- **The answer key is everything 25 Codex runs found together.** The best single Codex
  run, `ce-code-review-r3`, found 15. A's best run also found 15.

## What was run

The scripts are `INBOX/review-benchmark-pr295-2026-10-16/proto-a-inputs/workflow.js` and
`control-inputs/workflow.js`.

**Prototype A**
1. **Find.** The diff was split into 9 pieces: each `pr` module with its tests, the `pr`
   docs and lock files, buildme's code, buildme's tests, and the repository-level files
   (`proto-a-inputs/pieces.tsv`). Each of the 33 changed files is in exactly one piece.
   Each piece got a fresh agent with the plain review brief (correctness, regressions,
   security, tests, requirements). It could read anything in the checkout and in
   `multiplai-core`'s source at the pinned commit. It reported defects with at least one
   location in its piece, as a free-form report.
2. **Convert.** One agent per report turned it into findings, each with a quote copied
   from the cited lines.
3. **Verify.** One fresh agent per finding answered confirmed, refuted or unverifiable,
   using the pipeline verifier's wording.
4. **Score.** One agent per run scored the kept findings against
   `review-skills-pr295-comparison-2026-10-15/results/aggregation/ledger.md`. It used the
   rules of the earlier score files.

**Control.** The same as A with three differences:
- each of the 9 agents reviewed the whole diff;
- no verification ran, because in A it removed 1–4 findings per run and no real bug;
- the scorer scored every converted finding, counting a bug once however many reviewers
  reported it.

Finders and scorers ran at high effort, and converters at low. A's verifiers ran at
medium. Every agent ran on `claude-opus-5-5`: 1,663 model calls for A and 1,051 for the
control (agent transcripts).

## How far to trust it

**Checked:**
- **The agents could not see the answer or the fixes.** Both arms worked in a clone
  outside the workspace with `origin` removed. Every ref except one branch at the PR head
  was deleted and garbage-collected, leaving 819 commits, all ancestors of `c714fd61`.
  Scans of every find, convert and verify agent's tool calls found no access to INBOX,
  `PROJECTS` or the workspace. In the control, the one hit on `PROJECTS` is the text of a
  finding that quotes the PR body.
- **No agent ran code.** In both arms, every hit on `python`, `pytest`, `uv run` or
  `uv build` was text inside a grep or sed command. `git status` in the checkout is empty.
  Two of A's finders broke the rule against creating files: each saved a copy of the base
  commit's `orchestrator.py` to the scratch folder.
- **I spot-checked matches against the key:**
  - A, run 1: R-001, R-002, R-024, R-026 and R-045.
  - Control: R-042 in run 2 and R-009 in run 3.

  Each states the key's cause and consequence. The run 1 scorer for A calls its R-065
  match "the weakest match here".
- **The harness flagged "bypass-permissions"** in many agent outputs in both arms. It is
  the text of R-024, which is about `bypassPermissions` in `agent_runner.py`, not an
  instruction.

**Not controlled:**
- **One PR.** Every number here comes from PR #295.
- **Scoring.** Each run had one model scorer, as the earlier Opus arms did. The Codex arm
  had two blind judges and a reconciliation. The scorer prompts described each experiment,
  which could make a scorer lenient. The prompts for the two arms were worded the same way.
- **Verification differs between the arms.** The control's output was not verified. It
  has 1 false positive across its 3 runs, and A has 0.
- **The answer key comes from the Codex runs.** A bug that only Opus finds cannot count.

## Cost

Priced from each agent's token usage at the `claude-opus-5-5` rates in
`.multiplai/data/costs/pricing.json`: $4 in, $20 out, $0.20 cache read and $5 cache write
per million tokens. The usage totals are in `proto-a-inputs/proto-a-usage.json` and
`control-inputs/control-usage.json`.

| Stage, 3 runs | Prototype A | Control |
|---|---|---|
| Find (27 agents) | $36.15 | $62.23 |
| Convert (27 agents) | $6.39 | $6.99 |
| Verify | $53.67 (210 agents) | not run |
| Score (3 agents) | $9.67 | $7.99 |
| **Total** | **$105.87** | **$77.21** |

- **Whole-diff agents cost more per agent:** $2.30 each against A's $1.34.
- **Verification is A's largest cost and lost no real bug.** $38 of its $54 was writing
  each verifier's context to the cache.
- **Wall time:** A's 3 runs took 36 minutes together and the control's 36 minutes. They
  ran in parallel, and per-run times were not recorded.

## What to build next

1. **Build per-piece finders into the pipeline through `/multiplai-dev:buildme`.** Base
   them on the existing `--extra-finders` code (`prompts/find.py:288-323`), which differs
   from A in four ways:
   - its finders see only their chunk and are told "Never read a whole file";
   - they report only HIGH and MEDIUM;
   - they run only in the first round;
   - they are off by default.

   A's finders may read the whole snapshot, report every severity as a free-form report
   that a second call converts, and are the default find stage. The citation gates stay in
   Python. Then run the real pipeline on PR #295 3 times. A had no gates and no merge,
   which the pipeline adds.
2. **Add a finder for the repo's written rules**, aimed at the 7 rule bugs. Opus has found
   2 of them in 18 runs; Codex `code-review` found all 7 over its 3 runs.
3. **Make verification cheaper.** It cost $18 a run. Options: verify only HIGH and MEDIUM,
   verify at low effort, or have one verifier check all of a piece's findings.
4. **Test on a second PR.** One PR with 29 bugs is a small answer key, and building one
   is expensive.
5. **Prototype B**, which adds a step that tries to reproduce each finding, has to beat
   A's 13.

## Why the workflow is a prototype and Python is the product

A Workflow script has no filesystem access, so it cannot re-read cited lines with
`git show` in code. Its resume also works only inside the same session. The pipeline does
both. Python can run this shape too: it only fans out agents and passes results between
them.

What Python cannot do as built is let agents run code. Its agents are read-only by
construction (`sdk.py:15-19`, `read_roots=[cwd]`), because running a repository's tests
means running code the review treats as untrusted. That matters for prototype B, not A.

## Sources

- Video: "Claude found 15 bugs. Then it built a team and found 66.", The Hype Kevin,
  2026-10-09. Transcript: `INBOX/yt/claude-found-15-bugs-team-66.txt`.
- Anthropic docs, [Workflow runs](https://platform.claude.com/docs/en/managed-agents/workflow-runs),
  fetched 2026-10-16.
- The 70-bug figures come from the video and from secondary coverage
  ([xenospectrum](https://xenospectrum.com/en/claude-managed-agents-dynamic-workflows/)).
  I did not find Anthropic's own post.
- Prototype A outputs: `INBOX/review-benchmark-pr295-2026-10-16/proto-a-r{1,2,3}/`
  (`score.md`, `reports.md`, `findings.json`) and `proto-a-inputs/`. Workflow run
  `wf_d0b807a1-135`.
- Control outputs: `control-r{1,2,3}/` (the same three files) and `control-inputs/`.
  Workflow run `wf_db113a7c-e1c`.
- Codex per-run counts: `review-skills-pr295-comparison-2026-10-15/results/aggregation/defect-by-run.csv`.

## Appendix: what was cut from earlier versions of this file

- The first version said no planted-bug benchmark existed for the review. That was wrong:
  the PR #295 benchmark was written earlier the same day.
- With the benchmark's data, three recommendations were dropped:
  - a verifier told to refute;
  - rounds repeated until one finds nothing new;
  - finder questions written by a planner.

  A's results agree with dropping the first: verification lost no real bug.
- The second version estimated $30–45 for A's 3 runs. They cost $96, or $106 with scoring.
- The third version's first next step was the control run. It has run, and its results
  are above.
