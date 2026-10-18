# Implementation notes: code-paths-review

Where the build departs from `design.md` and `tasks.md`, or decides something
they leave open. One section per block, in build order.

## Block 1: paths stage

- **A trust stop is never a dropped entry.** `map_paths` takes one
  `recoverable` tuple and records every exception in it as a failed call (a
  `trace` drop or a failed fallback). `sdk.RepoTrustError` subclasses
  `sdk.AgentCallError`, so passing `AgentCallError` would have turned a trust
  stop into a drop and let the review continue. The stage's call function
  re-raises `RepoTrustError` and turns every other `AgentCallError` into a
  private `_CallFailed`, which is the only recoverable type. A budget stop is
  not an `AgentCallError` and propagates as it is.
- **`paths-cache/` is in `rounds.NOT_KEPT`.** The design says `keep_round`
  skips it because it is a directory, which is true, but
  `test_every_top_level_directory_a_review_writes_is_in_not_kept` requires
  every directory the pipeline writes to be listed there.
- **Duplicate path ids cannot reach the stage.** `PathsResult` refuses ids that
  are not `p1..pN` in order, and `write_paths_file` checks the rule again. The
  stage still records `duplicate-id` if one ever appears, but no test can feed
  it one through a valid result; the error line is tested through
  `add_coverage` directly.
- **`--paths` on a change review exits 2**, like `--trim`: a change review
  always maps its paths, so the flag would mean nothing there.
- **Subjects.** A dropped entry's subject is `<name> at <file>:<line>`, so the
  `state.errors` line reads `paths trace review_cmd at a.py:2: <detail>`. A
  failed or empty mapping uses subject `mapping`, a failed fallback `fallback`.
- **`paths_failure` on an empty mapping** says why there is no path ("no entry
  point reaches the lines under review", or how many entries were dropped), so
  the report's notice (block 6) has a reason to print.
- **The model fallback's own drops** (`model_fallback_citation`,
  `model_fallback_duplicate`) are not in the design's table; they are recorded
  as `info` / `out-of-scope`, like the other scoping drops.
- **Tree line counts** are read from the head snapshot and counted as git
  numbers lines (split on `\n` only), not with `str.splitlines`, which also
  splits on form feeds.
- **Existing tests.** Every change review now calls `map_paths`, which the
  fakes in the tests written before this stage did not answer. `tests/paths_stub.py`
  is installed for every test by `conftest.py` (and by the two subprocess
  drivers in `test_data_dir.py`); `test_paths_stage.py` replaces it with its
  own fakes. One assertion in `test_orchestrator.py` (`saved.stage == "target"`
  after a stop during find) now reads `not saved.past("find")`.
- **review-viewer.** `ChecksFile.coverage` is an optional list and the checks
  schema is regenerated; nothing in the page reads it yet.

## Block 2: pieces

- **`finder_chunks` is deleted in block 3, not here.** The extra finders still
  use it until block 3 removes the `extra@<i>` path, so the "no stage calls
  `finder_chunks`" test moves to block 3 with the deletion.
- **`Pipfile.lock` joins `target.LOCKFILES`**, the one lockfile list (it also
  makes a tree review skip it, as it skips the others). `chunks.LOCKFILES` is
  that set.
- **Plan line wording.** The root group prints as `[.]` (an empty `[]` reads as
  a formatting slip), and a piece of one file says `1 file`. A lockfile-only
  meta piece prints `piece <n>: 0 files, 0 characters [meta] omitted: uv.lock`.
  A tree review with `--paths` prints `path finder calls: up to 21 paths (...)`.
- **Omitted lockfiles** are listed on the first meta piece when the metadata
  splits into several.
- **The model fallback's drops** and binary files: a binary file is in its
  piece's `files` and `binary`, and its (header-only) section is in
  `piece_diff_text`, but it counts 0 characters.
- **`diff_files` splits on `\n` only** (it used `str.splitlines`, which also
  splits on form feeds and other separators), and reads each file's base and
  head paths from `---`/`+++`, `rename from`/`rename to` and the mode lines,
  undoing git's C-style path quoting.
- **Existing test.** `test_extra_finders.py::test_plan_only_counts_the_extra_finders`
  read the last line containing `finder calls:`, which is now the new piece
  line; it reads the `groups:` line instead.

## Block 3: piece finders through report-then-convert

- **review-viewer.** The plan says review-viewer is not changed, and the
  build was told to change it only for what `checks.json` needs. Its models
  forbid unknown keys, so `checks.json`'s new `items` on a convert entry
  needs `AgentEntry.items` (optional, `{kind, severity, finding_id}`); the
  checks schema is regenerated. The design's "dimension values `piece` and
  `path`" needs no change: the viewer's `Finding` has no dimension.
- **Budget labels.** One label per kind and call, not per key: `find:piece`
  for the report and `find:piece-convert` for the convert (the design's
  `budget_label=key` would give the run record one row per piece). The
  per-key detail is in `checks.json`.
- **No citation block in the piece prompt.** `CITATION_RULES` describes JSON
  citations, which a plain-text report has none of; the piece prompt asks for
  `path:line` and exact quotes instead, and the convert prompt carries the
  quote rules.
- **`ConvertItem.failure_scenario`.** `Finding` requires a failure scenario,
  which the tasks' `ConvertItem` lacks; the convert call is asked for the
  report's trigger and consequence there, default empty.
- **The piece prompt's shared blocks.** `build_piece(piece, diff, workspace=)`
  is given the workspace, dependency, commit, description and branch-rule
  blocks joined (`stages/find.shared_blocks`), so a piece finder sees the
  change's stated requirements, as the prototype's did.
- **A piece with no file** (a meta piece holding only omitted lockfiles)
  launches no finder, which is what makes a lockfile-only diff run none.
- **Closed gaps are removed.** When `resume` re-asks a failed report or
  convert and it succeeds, `checks.drop_coverage` removes that key's
  `finder-failed` / `convert-failed` record and its `state.errors` line, so
  the review does not report a gap it no longer has.
- **`--extra-finders` takes an optional value** (`nargs="?"`), so
  `--extra-finders 4` parses, and it no longer refuses `--tree` / `--dir`: a
  no-op has nothing to refuse. The notice also prints on `resume` when
  `review.yaml` sets `extra_finders`. `ReviewState.extra_finders` stays, unread,
  so older checkpoints load.
- **Tests.** `test_extra_finders.py` is removed with the feature; its
  `diff_files` test moved to `test_pieces.py`. `tests/paths_stub.py` now also
  answers `sdk.agent_call_text` with `NO FINDINGS` (after the trust check)
  for every test that is not about the piece finders. The fakes in
  `test_reported_finders.py` check and spend the real ledger, because a fake
  that never awaits cannot be cancelled by the task group, so a one-off stop
  let the queued pieces run.

## Block 4: path finders

- **Duplicate ids** are dropped by `paths_io.views` (the first kept), but a
  valid `paths.json` cannot hold one (block 1), so the stage records no
  coverage for it here; `views` is tested on a result built without
  validation.
- **No fence parser.** `multiplai_core.untrusted` has no function that parses
  a fence, so the hostile-path test counts the opening and closing tags (one
  each) and checks the hostile text lies between them.
- **Hop descriptions** are `` calls `<symbol>`: <quote> `` with the hop's note
  in brackets; `paths.v1` hops have no description field of their own.
- **The path finder is not given the diff.** Its prompt has the changed lines
  on the path and the shared blocks (commits, description, branch rules), as
  the tasks list; a 227 KB diff in each of 21 prompts would cost more than
  the paths themselves. Its citations are marked against the change's hunks.
- **Existing tests.** conftest's stub mapping gives every change review one
  path, so every review now also runs `path@p1` (answered `NO FINDINGS`). The
  block 3 tests count only piece keys, and the block 1 kept-round test checks
  that `report-path-p1.md` is this round's, since the path finder now writes
  one.

## Block 5: path ids on findings

- **Where it runs.** `assign_paths` runs at the end of `run_merge`, on
  `state.findings`. Findings rejected at find are exported too, but they
  never reach merge, so they carry no `paths`.
- **Web citations** are skipped along with `.review-deps/` ones: a URL has no
  line of the repository.
- **The viewer's `Finding.paths`** is the one other review-viewer change, for
  the same reason as `AgentEntry.items`: its models forbid unknown keys. It
  is optional, its schema is the array the design gives (never null), and
  the model refuses repeated or empty ids too.

## Block 6: the report's Paths section

- **`render_review` takes `paths=`.** The section needs `paths.json`, which
  `render_review` (state only) cannot read; `write_review` loads it with
  `paths_io.load_paths(target_dir, state)` and passes it in. The summary and
  the checks file have no Paths section.
- **Findings counted** are every `findings.json` row whose `paths` lists the
  id, as the requirement says, so a refuted finding on a path counts too.
- **No path to show while the status is `ok`** (a `paths.json` that no longer
  parses) prints the heading and `No paths were mapped: the paths file could
  not be read.` rather than an empty section.
- **`inline_code`** also turns Unicode line and paragraph separators into
  spaces, and shows an empty value as a padded empty span.
- **Invoker lines** are the entry's `invoked_by` followed by its
  `also_invoked_by` entries as `paths_io.views` names them; an empty
  `invoked_by` is skipped, so an entry with no invoker reads `unknown invoker`.

## Block 7: binding docs for the conventions finder

- **Its own module.** `linked_docs`, `LinkSource` and `LinkedDocs` are in
  `review_pipeline/linked_docs.py`; `conventions_blocks` calls it after the
  existing blocks, as the design says, and takes a new keyword `coverage=`
  (a list it appends the skip records to). `run_find` adds each record once
  through `add_coverage`, however many tree groups or resumed runs produce it.
- **Bytes, not text.** The existing `target.git` decodes output as text,
  which fails on a non-UTF-8 doc before it can be called `unreadable`, so the
  module runs `git ls-tree` / `git show` through the same `GIT_ARGV` and
  `git_env` with bytes output. No shell, as before.
- **A skipped doc** is a `(path, False, "")` entry in `conventions_blocks`,
  so `checks.json` labels it `rules skipped: <path>` like a skipped
  `CLAUDE.md`; the two joins that build the prompt drop empty blocks, so it
  adds nothing to the prompt.
- **The instruction** goes before the first included doc's block, with the
  included paths as a list (each defanged and on one line). It is not
  counted against `CONVENTIONS_MAX_CHARS`; the docs' fenced blocks are, so
  the rules content stays within the cap, but the prompt can pass it by the
  instruction's length.
- **A symlink inside the repository** is followed once (its target read
  instead); a symlink to a symlink, or to an absolute path, is `unreadable`
  or `outside-repo`. A target that is not a regular file (a directory, a
  submodule) is `unreadable`.
- **First-seen order of a reference link** is its definition line.

## Block 8: cost guidance

- **`max_cost_source` is `default` or `cli`.** No config file sets the budget
  today (`review.yaml` and `multiplai.conf` have no budget key), so nothing
  produces `config`; it stays in the type for when one does. To tell a given
  `--max-cost-usd 50` from the default, the flag now defaults to `None` and
  `__main__._budget_of` turns that into the 50 USD default.
- **On resume** only the WARNING is printed (no cost line, no NOTICE), and
  only while find has not finished; the budget left is the new ceiling
  minus `state.budget["cost_usd"]`.
- **A tree review with `--paths`** gets the paths-only cost line and the
  WARNING, never the NOTICE.
- **The STOPPED line** is attached to the budget error by `run_state`
  (`e.kept`) and printed by `__main__` after the resume command.
- **Path keys before mapping** are counted as `path@p1..p21`, so on a resume
  that stopped before the paths stage the remaining estimate is the cap's.
- **The SKILL.md test** joins the Cost section's lines before matching, so
  rewrapping the prose does not break it.
