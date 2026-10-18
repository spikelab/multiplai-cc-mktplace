# Use cases — who this change is for

## Personas

### change-author
- **Who they are:** A developer in this repository who has just finished a large, multi-file change, such as the 227 KB, 29-bug PR #295. They run `/multiplai-dev:review` on it before asking for merge.
- **The job they are doing:** Finding the real defects in their change before anyone else does. This includes bugs that only show when a command is followed across 2–3 files.
- **The constraint they are under:** They have the 50 USD `--max-cost-usd` default and limited patience. The review's agents can only Read, Grep and Glob, and never run the code. A stopped run must not throw away work they already paid for.

### rules-keeper
- **Who they are:** The maintainer who wrote the repository's binding rules in `docs/untrusted-content.md` and `docs/degradation-contract.md`. `CLAUDE.md` only links to these docs and does not repeat them.
- **The job they are doing:** Making sure every change is checked against those written rules, the way Codex's `code-review` skill checks them line by line.
- **The constraint they are under:** They cannot copy every binding doc into `CLAUDE.md`. Conventions text has a fixed size budget. A PR can drop or weaken a rule or the link to it.

### benchmark-runner
- **Who they are:** The person who reruns the PR #295 benchmark after this PR merges. They use the results to decide what PR 4 does, including whether the dimension finders still add anything.
- **The job they are doing:** Counting which of the 29 known bugs a review run finds. They attribute each find to a piece finder, a path finder or a dimension finder, and inspect what each finder actually wrote.
- **The constraint they are under:** The benchmark is not a merge gate and has no target median. It only works if each run leaves readable, comparable outputs on disk, including in kept rounds.

### tree-auditor
- **Who they are:** A developer who reviews an existing directory or set of files with `--tree` or `--dir` rather than a change.
- **The job they are doing:** Seeing which entry points reach the files under review and what defects lie along those paths.
- **The constraint they are under:** There is no diff, so there are no changed lines. Mapping paths costs money, so they want it only when they ask for it with `--paths`.

### existing-user
- **Who they are:** Someone whose scripts, config or habits already use `--extra-finders` or `extra_finders`, or who reads `findings.json` with existing tools such as `review-viewer`.
- **The job they are doing:** Continuing to run reviews and read their output without having to rewrite anything on the day this ships.
- **The constraint they are under:** They will not read the changelog before their next run. Their tools expect the current `findings.json` shape.

## Use cases

- change-author wants every change review to split the diff into pieces of related files, each owned by a finder that may read the whole checkout, so that a review of PR #295 reports noticeably more of its known bugs than the current median of 1.
- change-author wants findings of every severity from every round, not only HIGH and MEDIUM from the first round, so that the report lists LOW items and advice alongside serious defects on round 2 and later.
- change-author wants one finder to follow each changed command from its entry point to its last effect, so that bugs spanning 2–3 files along one command appear in the findings, as R-009, R-017, R-042 and R-056 did in the prototype.
- change-author wants a Paths section in the Markdown report, so that for each path they can read its entry, who invokes it, how many findings lie on it, and which e2e tests cover it, which are mocked and not counted, or "no e2e test".
- change-author wants each finding in `findings.json` to list the ids of the paths it lies on, so that they can see which commands a defect affects without tracing it themselves.
- change-author wants `--plan-only` and `SKILL.md` to state the expected cost, about $14 for pieces plus $24 for paths on a 227 KB diff, and to warn that the 50 USD default will stop a large review partway, so that they can choose a `--max-cost-usd` before anything is spent.
- change-author wants a run that hits `--max-cost-usd` to keep every finder result already paid for, so that `resume` with a higher ceiling spends money only on the finders and path answers still missing.
- change-author wants a finder whose report was written but whose conversion failed to be retried from the saved report, so that the resumed run shows a charge only for the convert call and not a second full review.
- change-author wants the review to finish with the piece and dimension finders when path mapping fails or finds nothing, so that they still get a report, and its coverage names the actual missing capability, for example "no entry points found for this language".
- change-author wants dropped entries and broken traces listed in the review's coverage, so that they can see which parts of their change no path finder followed.
- rules-keeper wants the conventions finder to receive the text of every repository-local Markdown doc linked from a `CLAUDE.md` or `coding-standards.md`, so that a changed line breaking a rule in `docs/untrusted-content.md` or `docs/degradation-contract.md` shows up as a finding.
- rules-keeper wants docs linked on lines that say `bind`, `must` or `required` to be included first, and any doc that does not fit the size budget to be recorded as skipped, so that they can see in the review's output which binding rules were checked and which were not.
- rules-keeper wants links that exist only at base to be followed too, so that a PR removing a binding doc or its link is still reviewed against the rule it removes.
- rules-keeper wants links that are URLs, absolute paths, escape the repo with `../`, point at symlinks or point at non-Markdown files to be ignored, so that the rules finder only ever quotes docs that live in the repository.
- rules-keeper wants finder reports and path data fenced and defanged as untrusted content in the new prompts, so that the new prompts meet `docs/untrusted-content.md` when they review a change against that doc.
- benchmark-runner wants each piece and path finder's free-form report saved as a top-level file such as `report-piece-3.md` or `report-path-p07.md`, so that they can open what each finder wrote, including in kept rounds.
- benchmark-runner wants `paths.json` written beside `findings.json`, so that they can see which paths each run mapped and compare them across runs.
- benchmark-runner wants the existing dimension finders to keep running unchanged next to the new finders, so that the post-merge rerun can show whether `diff-bugs`, `callers`, `history`, `conventions`, `tests` and `trim` still find bugs the new finders miss.
- benchmark-runner wants the kind of each converted item (defect, advice or hypothesis) recorded in the find entries of `checks.json`, so that they can count how many converted findings were advice or unverified hypotheses.
- tree-auditor wants `--tree` and `--dir` reviews to map paths only when `--paths` is given, so that a plain tree review shows no paths stage spend and no `paths.json`.
- tree-auditor wants a `--paths` tree review to trace the entry points that reach the files under review, capped at 21 paths, so that the report's Paths section and the path ids on findings cover the directory they asked about.
- existing-user wants `--extra-finders` and `extra_finders` still accepted, printing a one-line notice that they no longer do anything, so that their existing commands and config run without error and they learn the flag is obsolete.
- existing-user wants the new `paths` field on findings to be optional, so that their existing readers of `findings.json`, `review-viewer` included, keep opening new reviews without errors.