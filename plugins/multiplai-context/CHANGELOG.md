# Changelog

All notable changes to the **multiplai-context** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-context@<version>`.

Recorded history starts at **0.1.0**; anything earlier is in `git log` only.

Of the 31 versions recorded here, `0.6.4`, `0.6.5` and `0.8.0` carry a git tag
— the tagging convention started partway through. Dates on untagged versions
are the release dates recorded at the time, not derived from a tag.

## [Unreleased]

Nothing yet.

## [0.15.1] - 2026-08-03

### Fixed
- **The fleet reading counted dead sessions.** A status line saying
  `36 fronts · 5 need you · oldest 19d · 9 collisions` over a fleet of one
  running session is not a reading, it is noise you learn to ignore — and
  that is what the real registry produced: 117 entries, 34 of them counted
  as fronts, every one of the nine collisions between pairs of sessions that
  had been dead for three to eighteen days.

  Three separate causes, all fixed:

  - **A container that dies without a `SessionEnd` left an immortal entry.**
    Hooks report from inside a session and cannot report their own container
    being killed — reboot, `docker kill`, OOM, or just closing the tab, all
    routine with `--rm`. The entry kept its last event (often a Notification)
    forever, and the fleet view read it as a live agent waiting on you.

    The launcher can see what no hook can, so it now leaves a `<sid>.exited`
    marker beside the entry the moment `docker run` returns; this plugin
    reads it as `ended` and clears it on the session's next event. **This
    half needs multiplai-kit at the commit that adds the marker** — without
    it the other two fixes still apply, they just have less to work with.

  - **Idle sessions counted as fronts.** `AGENTS.md` still lists every tab
    that is on the board, idle ones included — that is where you go looking
    for the one you forgot about. But `fleet.txt` has room for one number,
    and it now counts only what has a claim on you: **Needs you**, **Working**
    and **Parked**. The full read's header follows suit (`N front(s) … · N
    idle`), so the two cannot disagree.

  - **Collisions were reported between long-dead sessions.** A collision is
    the claim that two agents might *now* write the same file. Both holders
    must therefore be fronts and have been heard from within 24 hours; a file
    two sessions both touched last week is shared history.

- **Registry GC kept observed-dead entries for 30 days.** The long window
  exists for sessions that might still be alive, and it was being granted to
  exactly the entries that most needed collecting. An entry with an exit
  marker now takes the same 7-day cutoff as one that ended cleanly. On the
  real registry this reclaimed 16 entries the old rule would have held for
  another three weeks.

### Changed
- **Session state is now documented in one place** — README → *Session
  accounting*. It had accreted across two half-sections and a lot of code
  comments, which is how you end up with three overlapping notions of "is
  this session alive" and no single page saying which is which. The new
  section covers what is on disk and who may write it, the three independent
  fields (`status` / `disposition` / group), how each of the four ways a
  session can stop is detected, which groups are listed versus counted, and
  the GC cutoffs. The kit README and the suite `ARCHITECTURE.md` link here
  rather than restating it.

- **There is now a single walkthrough of what a session actually does** —
  README → *The life of a session*. Checkpointing, extraction and the
  registry were each documented well and separately, which left no page
  answering the question people actually ask: what happens, in what order,
  and where does it fork. Ten stages from `docker run` to the deletion of
  the last registry entry, each fork stated as a branch — the four values
  of `SessionStart.source` and which two re-inject a checkpoint, the three
  outcomes of the Stop-hook checkpoint decision, the four ways out of a
  full context window, the four ways a session can stop, the two drains and
  the atomic rename that keeps them from colliding, and the retry ladder
  down to `failed_extractions/`. It closes with every branch point in one
  table, including the two that turn features off entirely (`uv` missing,
  no model client).

  Also corrects *Session accounting* → *When it refreshes*, which credited
  the fleet-view regeneration to `synthesize_agents.py` running alongside
  the drain. `SessionStart` writes it in-process; the host drain writes it
  again after a container exits; the script is the on-demand entry point.

## [0.15.0] - 2026-08-01

### Added
- **A tab you left open for days now has a checkpoint saying where it stopped.**
  Checkpoints used to be triggered by context size alone — bands at 100K and
  200K tokens — so a session that sat at 40K for three days had none at all.
  That is backwards: the session whose state you have most thoroughly lost track
  of was the one the fleet view had least to say about, because `AGENTS.md`
  reads its intent, next action and files-in-hand from the checkpoint.

  A third trigger now fires on age instead of size. It writes when a session is
  at least **30 minutes** old and either has never checkpointed or last did so
  over **3 hours** ago.

  **What it costs:** a four-hour session writes twice instead of once or twice.
  Checkpoint writes distill only the transcript since the last one and overwrite
  a single file, so writing more often makes each write smaller rather than
  making the total larger. There is a test asserting the two-writes-in-four-hours
  figure, so the cadence cannot drift silently.

  Both numbers are configurable:
  `CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours` and
  `CLAUDE_PLUGIN_OPTION_checkpoint_min_session_minutes`. Setting `stale_hours`
  to `0` turns the age trigger off and restores the previous size-only
  behaviour exactly. These are **new** settings —
  `checkpoint_ttl_hours` still means what it always meant (how long a `/clear`
  handoff marker stays valid) and is unaffected.

  A session idle for three days fires no hooks at all, so nothing runs during
  the quiet stretch. That is fine: the last completed turn *is* the moment the
  work stopped, so the checkpoint it leaves behind is at most `stale_hours`
  behind where the session actually ended up.

## [0.14.0] - 2026-08-01

### Changed
- **Checkpoints are now cleaned up once the diary has been written.** A
  checkpoint is *live state* — roughly where a session is right now, so the next
  context window can pick it up. The diary is the permanent record of what the
  session actually did. Those are two artifacts with two lifetimes, but only the
  diary ever had an ending: every session that crossed a token band left a
  directory in `.multiplai/data/checkpoints/` forever. There were **182 of them**
  on one machine, one per session ever run, none ever collected.

  Extraction now deletes a session's checkpoint directory once that session's
  diary entry exists — and only then. Six things keep one alive:

  - **The extraction was not fully successful.** A partial LLM failure can
    still write a diary entry from the chunks that survived, but that entry
    covers only part of the session — a diary that does not fully supersede
    the checkpoint never deletes it.
  - **The session is still running.** An extraction deferred by context
    compaction runs against a live session; its checkpoint is working state,
    not a leftover, and is kept untouched.
  - **The session is `parked`.** `AGENTS.md` renders a parked session's intent,
    next action and files-in-hand from its checkpoint, and a parked session is
    precisely the one still listed weeks later. Deleting it would leave an entry
    you deliberately kept with nothing to say.
  - **The diary write failed** — nothing has superseded anything.
  - **A checkpoint writer is still running** for that session.
  - **An unconsumed `/clear` rebuild marker still points at it** — you crossed
    the handoff threshold and closed the tab instead of clearing, so that
    checkpoint is what tomorrow's session rebuilds from.

  Nothing you can see changes: no checkpoint is removed while it is still the
  best available answer to "where was this?", and if the cleanup itself fails,
  the diary entry is already written and unaffected.

## [0.13.0] - 2026-08-01

### Added
- **Say "park it for now" and the session stays on the list.** There is no new
  command to remember — which is the point, because the moment you want to park
  something is the moment you are overloaded and walking away, and that is
  exactly when you will not remember a `/park`.

  The extraction pass that already reads your whole transcript for the diary now
  also reads how you *left*, from the closing exchange alone, and labels the
  session one of three ways:

  - **`done`** — you said so. "we're done", "ship it", "merged, thanks".
  - **`parked`** — you said you were stopping without finishing. "park it for
    now", "let's pick this up tomorrow", "shelve this".
  - **`active`** — everything else, including a session that just stops
    mid-work. This is the default, and anything ambiguous lands here.

  Three things follow. A parked session shows in `AGENTS.md` under its own
  **Parked** heading — between **Working** and **Idle**, since it is not urgent
  but it is the pile you *chose* to return to — quoting your own closing words
  as the reason. It stays there after its container exits, which is the whole
  point of parking it. And a `done` session drops off the list even if its
  container is still running.

  Most importantly, **a parked session is never garbage-collected.** Registry
  entries normally age out in 7 to 30 days, while the transcripts behind them
  survive a year — so a parked idea used to stay `--resume`-able while becoming
  invisible, which is the failure this fixes. Nothing is copied anywhere; the
  session record simply stops expiring.

  It costs no extra model call — the label rides along on the response the
  diary extraction was already making — and it is written to its own registry
  key, so a session's liveness (`working`/`idle`/`ended`) is untouched: a
  session can be both `ended` and `parked`. If the model says nothing useful,
  or extraction fails outright, the session is `active` and everything else
  behaves exactly as before.

  The label's lifecycle holds up at the edges, too. Resuming a session clears
  its old departure label — a resumed parked session groups by what it is
  *doing* again (including "Needs you"), and a resumed done session comes back
  onto the list. The label survives a long absence: registry cleanup skips any
  session whose deferred extraction is still queued, so coming back on day 8
  to a session parked on day 0 still finds it labelled. And on a long
  transcript, a mid-session extraction hiccup no longer costs the label — only
  the closing exchange's own chunk decides it.

## [0.12.0] - 2026-08-01

### Added
- **A fleet view: one file that says what every agent is doing.** If you run
  several Claude Code sessions at once, the state of each one has until now
  lived in your head — the plugin knew *where* each session was (the session
  registry) and *what* it was doing (the per-session checkpoint), but nothing
  put the two together where you could read it.

  `.multiplai/data/AGENTS.md` now does. Each session appears under **Needs
  you** / **Working** / **Idle** / **Ended**, carrying its project, container,
  branch (worktrees say which one), how long it has been quiet, what it is
  doing, what it will do next, and which files it has in hand. Below that, a
  **Collisions** section lists every file two live sessions are both holding —
  the overlapping-work question answered without any agent talking to another.

  A second file, `.multiplai/data/fleet.txt`, carries the same reading as one
  short line — `6 fronts · 2 need you · oldest 3d · 1 collision` — cheap enough
  for a terminal status line to `cat` on every prompt.

  It refreshes by itself: at every session start, and on the host right after
  your last tab closes. To render it by hand:

  ```
  uv run --no-project <plugin>/scripts/synthesize_agents.py --stdout
  ```

  **No model call and no network** — it is pure aggregation of files that
  already exist, so it costs a few file reads and is safe to run at any time.
  Both files are a **cache**: delete them and the next run rebuilds them
  identically. Nothing writes into `AGENTS.md` as state, and anything you add
  to it by hand is overwritten on the next refresh.

  Sessions that never wrote a checkpoint (most of them — checkpoints only
  trigger past a context-size band) still appear, with the fields the registry
  does have.

## [0.11.0] - 2026-08-01

### Added
- **The diary can now be written the evening you stop working, instead of the
  next time you open a session.** When a session ends, the plugin leaves a
  marker and something else has to pick it up and run the (multi-minute)
  extraction. Until now the only thing that ever picked one up was the *next*
  `SessionStart` — so closing your last tab on a Friday produced Friday's diary
  entry on Monday, and a fleet of long-lived tabs could sit on unwritten
  history for days.

  There is now a standalone `scripts/drain_extractions.py` that drains the same
  queue from outside a session:

  ```
  uv run --no-project drain_extractions.py --data-dir <workspace>/.multiplai/data
  ```

  It takes an optional `--data-dir` (defaulting to the usual path cascade),
  `--wait` to block until each extraction finishes with its errors visible, and
  `--verbose` for a one-line summary. Container launchers can call it right
  after the container exits; you can also just run it by hand when you suspect
  a session never got written up.

  Session start drains through exactly the same code, so the two paths cannot
  drift apart. Two drains racing is handled end to end: the dequeue is an
  atomic rename that also refreshes the marker's mtime (staleness is measured
  from *launch*, so a marker written Friday and drained Monday is not
  instantly "stale" and re-launched by a concurrent session start), and
  staleness recovery claims a marker atomically before rewriting it, so two
  recoverers cannot double-count its retry attempts.

  `--wait` exits nonzero when any extraction child fails, so
  `drain … --wait && echo ok` actually proves extraction worked. Errors (a
  missing `extract_learnings.py`, failed children) always print to stderr;
  `--verbose` gates only the success summary.

  The script's header documents which environment it needs: notably
  `CLAUDE_PLUGIN_OPTION_workspace_dir` (or `WORKSPACE`), without which the
  diary silently lands in `~/.multiplai/` instead of your workspace, and
  `CLAUDE_CONFIG_DIR` so the Agent SDK finds your existing credentials.

  The startup line reports **both** queues — `(N pending, M in flight)`. A
  session whose container died mid-extraction leaves its marker in
  `processing_extractions/`, where the drain can still rescue it but the
  pending count cannot see it; reporting pending alone made such a run
  announce `0 marker(s) pending` and then say it had drained one, which reads
  as a malfunction rather than a recovery.
## [0.10.2] - 2026-07-31

### Fixed
- **A big memory file no longer runs out of time while being updated.** Writing
  approved changes back into a memory file was given the same fixed time limit
  no matter how much there was to write. Your largest files are exactly the ones
  that collect the most updates — a recent backlog had 41 KB of pending changes
  for `claude-code-tools.md` and 38 KB for `multiplai.md` — and on a slow call
  those could run out of time and be skipped, silently leaving the file
  untouched while smaller files updated fine. The time limit now scales with how
  much is actually being written. A file that still runs out is left exactly as
  it was, never half-written, and the log now names the file and its size.

### Changed
- **`/dream-remember` now applies a review per target file instead of per
  item.** A large proposal used to cost one script cold start and a fresh
  read of the same memory file for every single decision — a 70-item review
  across 14 files ran out of context part-way through and had to hand off,
  having applied five. The skill now reads each memory file once, applies all
  of its approved edits, updates `Last Updated` once, and records every
  decision for that file — approved *and* rejected — in one call. Reviews that
  previously needed a compaction handoff now finish in one sitting. Nothing
  about *consent* changed: `[RULE-PROPOSAL]` items are still presented and
  answered one at a time, and items you neither approved nor rejected stay
  pending.

### Added
- **`dream.py --mark-processed --decisions -`** takes a JSON array of
  decisions on stdin — `{"kind","file","index","status","target"}` per item —
  and marks them all in one read and one write of the proposal, printing
  `marked N processed, M unchanged`. The write is atomic, so an interrupted
  or failed call leaves the proposal exactly as it was rather than
  half-decided. The existing single-item flags are unchanged and still
  supported.
- **`dream.py --gc-learnings`** replaces the skill's judgement call about when
  it is safe to delete consolidated learnings files. Pure code, no model call:
  a file is removed only when every `## Session Learnings` record in it has
  been consolidated **and** no proposal citing it is still pending, so a
  review you left half-finished keeps the sources its `**Source:**` citations
  point at. It prints what it removed and why it kept the rest. Step 5 of
  `/dream-remember` now runs this instead of deciding by hand.

## [0.10.1] - 2026-07-31

### Changed

- **`/dream` now runs 8 consolidation calls at once instead of 4.** On a large
  backlog four was simply too few to finish in a reasonable time: a measured
  283 KB backlog is about 98 minutes of model work, which four workers cannot
  get through in under ~24 minutes no matter how it is scheduled — the actual
  run took 38. Eight roughly halves that. The trade is that `/dream` now uses
  more of your machine while it runs; if you would rather it stayed out of the
  way, `MULTIPLAI_DREAM_CONCURRENCY=4` (or any number) still wins.

### Fixed

- **`/dream` no longer cites the wrong file for some of its sources.** Every
  entry in a proposal ends with a `**Source:**` line naming the learnings file
  and line it came from, so you can check an entry before applying it. When a
  session ran past midnight its notes are saved into the *next* day's file, and
  `/dream` sometimes cited it under the earlier date — sending you to a file
  where that line doesn't exist. On a large backlog about 2% of citations were
  affected. They are now corrected automatically wherever the right file can be
  identified beyond doubt, and a `## Citation Repairs` section at the end of the
  proposal lists every correction made. Citations that can't be verified are
  listed there too, and left exactly as written rather than guessed at.

- **`/dream` no longer loses a chunk of learnings on its first run.** The very
  first run on a machine had to guess how fast drafting goes, and it guessed
  nearly twice too fast — so it built chunks bigger than it could finish inside
  the deadline. On a 283 KB backlog one chunk of twelve ran out of time, retried,
  ran out again, and was skipped. Nothing was lost permanently (a skipped chunk's
  learnings stay pending and come back next run), but the run took 30 minutes
  longer than it should have and quietly consolidated less than it reported. The
  starting guess is now the measured rate. From the second run onward `/dream`
  has always calibrated itself from your own machine, and still does.

- **The second-pass reviewer now actually runs on large backlogs.** `/dream`
  drafts a proposal and then re-reads it to merge duplicates, drop
  point-in-time noise, and re-route items filed under the wrong memory file. On
  a big backlog that review was handed the entire proposal in one piece — far
  more than it could read in the time allowed — so it timed out and was skipped,
  every time, while the log said only "keeping the merged draft". The review is
  now done in batches, so it runs on backlogs of any size. Its edits are applied
  to the whole proposal together, exactly as before. If one batch fails, the rest
  of the review still lands instead of the whole pass being lost.

- **`/dream` no longer reports a partial run as a complete one.** When some of a
  run's work failed — a rate limit, a slow call, a network blip — it still
  printed the number of learnings it *set out* to process, not the number it
  actually did, and said nothing about the second-pass review having been
  skipped. A run that consolidated 122 of 231 learnings announced "231 new
  learning block(s)", which reads as "your backlog is done" when half of it is
  still queued. It now says `122 of 231 ... consolidated`, names how many chunks
  did not finish, and states that the deferred learnings come back on the next
  run. Nothing about the recovery behaviour changed — those learnings were always
  safe and always came back; only the reporting was wrong.

- **`/dream --check` now tells you its estimate is a minimum, and says so
  honestly.** The old estimate assumed chunks run in synchronised batches,
  waiting for the slowest of each group before starting the next. They don't —
  a chunk starts as soon as a slot frees up, so the old answer changed depending
  on nothing more than the order the chunks happened to be listed in (a 16%
  swing on a measured 283 KB run). The new number can't do that. It is a floor
  rather than a guess, so it now prints as `est. ≥24m` and "at least 24 min":
  the one run measured against it came in 13% above its own prediction, and a
  bare figure would have promised a precision it doesn't have. The plan line
  also drops the "wave" count, which never described how the work was
  scheduled.

## [0.10.0] - 2026-07-31

### Changed
- **`/dream` now handles a backlog of any size, in one run, without moving
  anything.** It used to read every learnings file and consolidate them in a
  single model call. That call emits proposal text at a flat rate regardless of
  how much you feed it, so the run time was essentially your backlog size
  divided by a constant — and past roughly 150 KB it hit the internal ceiling
  and failed every time. The only workaround was to move learnings files out of
  the directory and back a few at a time, which produced one proposal per slice
  and, on at least one killed run, left files stranded in a hidden backup
  directory where no later run could see them.

  Dream now splits the backlog into chunks sized from the timeout, drafts them
  concurrently (4 at a time by default), and merges the results into one
  document. **No learnings file is moved or deleted by this path anymore.**

- **`/dream` reports what it is about to do before spending anything.** A
  planning line names how much is new, how many chunks it will take, and the
  estimated wall clock. `dream.py --check` prints the same plan without
  starting a run. The estimate self-calibrates from your own observed
  throughput as runs complete.

- **Re-running after a crash resumes instead of starting over.** Dream keeps a
  ledger of which learnings it has already consolidated, keyed by content, so a
  killed run costs only its in-flight chunks. Whitespace-only edits to a
  learnings file do not orphan its ledger entries.

- **A second `/dream` while one is running is now a no-op**, not a competing
  writer. It names when the running one started and exits cleanly.

- **You end up with exactly one proposal to review.** An earlier proposal you
  have not decided on is folded into the new one and moved to
  `.multiplai/dreams/superseded/`.

  Two kinds of proposal are never touched: one you have already decided items in
  (it has a `## Processed` section), and **one you have curated by hand**.
  Curation is detected by content, not modification time — so a `git checkout`
  or `git stash` in your workspace cannot make dream mistake an untouched
  proposal for an edited one, and editing a proposal cannot be undone by dream.

- **`## Filtered Out` is now one line per dropped item** instead of a
  multi-line block, so the section stays skimmable. It is still per-item on
  purpose: a filtered learning is marked consolidated and will not resurface,
  so each drop has to stay visible.

### Requires
- `multiplai-core` **v0.12.0** (up from v0.10.0), for the per-call timeout that
  lets one oversized chunk get a longer ceiling without affecting the others,
  and for the `alive` heartbeat that shows a long call is still producing. Set
  `MULTIPLAI_AGENT_HEARTBEAT_S=0` to silence the heartbeat.

### Tuning
- `MULTIPLAI_DREAM_CONCURRENCY` (default `4`) — chunks drafted at once. Each is
  a CLI subprocess; raise it only if your machine has headroom.
- `MULTIPLAI_DREAM_THROUGHPUT` — override the bytes-per-second estimate.

## [0.9.0] - 2026-07-27

### Fixed
- **`/setup` now writes plugin options to the key Claude Code actually
  reads.** It previously wrote `pluginConfigs.multiplai.options`; the
  correct key is the compound form
  `pluginConfigs["multiplai-context@multiplai"].options`. The wrong key
  fails silently — every option quietly falls back to its default — so if
  you ran setup before this release and set a custom workspace, check your
  `settings.json` and move the `options` block under the compound key.
- The plugin README no longer claims Claude Code prompts for `userConfig`
  values at enable time (it doesn't —
  [anthropics/claude-code#39455](https://github.com/anthropics/claude-code/issues/39455)).
  Options are collected by `/setup` or set manually; the README now shows
  the correct settings key.

### Changed
- **`/setup` is now a 2-question quick path by default** — your name and
  your workspace directory, then one restart at the end (the two mid-flow
  restart notices are gone). It finishes by walking you to your "first
  recall": ask a fresh session what it knows about you and see the memory
  arrive. The complete interview (identity/technical/general preferences,
  routing scope, project identity, git for memory) is unchanged and now
  runs via `/multiplai-context:setup full`.
- The README quickstart now ends at the first recall — what to do, what
  you should see, and how to read the `[context]` routing line that proves
  it — instead of ending at configuration tables.
- `/dream-remember`'s docs no longer name an unreleased GUI; the
  `## Processed` cross-tool contract is unchanged.

### Added
- **Troubleshooting** section in the README: `uv` missing, slow first
  start, hooks apparently doing nothing, memory not injected, settings
  changes not taking effect, where the logs live.
- **Uninstall** section in the README: the two commands, what stays on
  disk, and how to remove everything.
- **What it costs** section in the README: real ledger-derived figures for
  the unattended passes (dream proposal ≈ $1, catalog refresh ≈ $0.05,
  background extraction/rebuild calls ≈ $0.16 — medians, July 2026), whose
  quota unattended calls draw on (the Agent SDK, i.e. your own Claude Code
  auth; an `anthropic_api_key` only as explicit fallback), and how to
  switch the background work off.

## [0.8.2] - 2026-07-27

### Changed
- **`/log-doctor`'s untrusted-text handling now comes from `multiplai-core`**
  instead of a copy kept in `log_doctor.py`. Digest output is unchanged, with
  two improvements inherited from the shared version: a log line saying "ignore
  **the** previous instructions" is now marked `⟪INJECTION?⟫` (the local copy
  only matched "ignore all/any previous instructions", so that phrasing slipped
  through unmarked), and a `source` label containing a `"` can no longer close
  the fence's tag attribute. Core pin moves `v0.9.0` → `v0.10.0` across all 23
  scripts and `requirements-dev.txt` together.

## [0.8.1] - 2026-07-26

Post-merge review fixes for memory evolution (findings 7.1–7.4 of
`INBOX/pr-review-batch-2026-07-26.md`, against #62). No new features.

### Fixed
- **The condition sweep could silently skip a whole cycle**
  (`scripts/lib/prospective.py`). The sweep fired on
  `(today - captured).days % 30 == 0` — only on exact 30-day multiples from
  capture. Miss that single day (no session, a run either side of the UTC
  midnight rollover, a closed laptop) and the intention waited another 30 days.
  For the one memory channel whose stated failure mode is *being silent*, a
  scheduler that can skip is the wrong shape. Now each intention carries a
  `last_surfaced` stamp in `prospective_sweep.json` (plugin data dir — derived
  state, deliberately NOT in the human-edited `prospective.md`) and the test is
  elapsed-time: `today - last_surfaced >= 30`. Because that condition stays
  true until something records the surface, an un-stamped sweep now re-fires
  next session — noise, never silence.
  - Keyed on the intention's condition + text, not its line number, so
    reordering or reflowing `prospective.md` doesn't reset the clock.
  - An intention with neither stamp nor capture date now surfaces once and is
    stamped, instead of being ignored forever.
- **The maintainer was spawned on every session start, gate or no gate**
  (`scripts/session_start.py`). `_launch_maintainer` unconditionally `Popen`'d
  `uv run … memory_maintainer.py`; the child then checked its own 24h gate and
  exited. The gate check is a timestamp read, so it now happens **in-process
  before the spawn** — the child stays authoritative, but ~95% of sessions no
  longer pay a `uv run` startup (and, on a cold uv cache, a network fetch for
  the PEP 723 git dependency) to accomplish nothing. A test pins the restated
  gate constants to the maintainer's own.
- **Unattended dream proposals accumulated one file per day**
  (`scripts/memory_maintainer.py`). Generating a proposal correctly does not
  stamp the dream gate (nothing was applied), so a backlog left unconsolidated
  for a week produced seven dated proposals — seven proposal + critique model
  calls — and handed `/dream-remember` a pile to choose between. The pass is now
  skipped while any un-archived `processed-learnings-*.md` is still waiting.
- **An ancient `as of` with no `review by` was permanently clean**
  (`scripts/lib/memory_lint.py`). `(as of 2019-01)` matched the annotation
  regex and was accepted as complete, so nothing could ever expire it — while
  the linter's whole premise is that facts rot on their own schedule. Reported
  after 12 months as a **third kind**, `undated`, not folded into `expired`:
  nothing has passed, and blurring a missed review date with a missing one would
  misdescribe both. `--expired-only` still means only genuinely-passed dates.
- **A trailing `<!-- comment -->` rode into the surfaced intention text**
  (`scripts/lib/prospective.py`). Closed comments are now stripped from a line
  before it is matched.

### Changed
- **Core pin moved `v0.8.1` → `v0.9.0`** across all 23 PEP 723 script headers
  and `requirements-dev.txt`. The maintainer resolving one core version while
  the hook that launches it imports another is two versions of the same library
  in one workflow.
- README documents the three linter kinds, the end-of-month meaning of a
  `YYYY-MM` stamp (previously only in a docstring), the elapsed-time sweep, and
  the pre-spawn gate. `templates/prospective.md` states the sweep cadence and
  the comment-stripping rule.

## [0.8.0] - 2026-07-26

Memory evolution (#62), untrusted-log handling (#60), behavioural contracts
(#61), and outcome-based cost reporting (#59).

### Added
- **Prospective memory — intentions that fire later** (`scripts/lib/prospective.py`,
  `templates/prospective.md`, seeded by `/setup`). Every other memory file
  answers "what is true?"; `memory/prospective.md` answers "what did I say I'd
  come back to?". One intention per line, two trigger kinds:
  - `- [due: 2026-09-01] Re-check X (captured 2026-07-26)` — a date. Surfaced
    as a `SessionStart` nudge from a week before, and for as long as it stays
    overdue.
  - `- [on: the runtime moves past v0.5] Re-run the audit (captured 2026-07-26)`
    — a condition in prose. **Never machine-evaluated.** No code decides
    whether "the runtime updated" has happened, because a wrong guess fires the
    reminder at the wrong time; these surface through normal memory routing and
    on a periodic sweep instead.
  - Nothing expires automatically and nothing writes to the file behind your
    back — capture goes through extraction → dream → `/dream-remember` like
    every other learning. Remove acted-on intentions yourself.
- **Proactive memory maintainer** (`scripts/memory_maintainer.py`), launched
  **detached** from `SessionStart` and gated to **at most once per 24h**. Four
  passes: staleness lint → dream proposal (only when the dream gate is open and
  a backlog exists) → catalog refresh (only when memory is newer than the
  catalog) → `now/` status rebuild for the active project (on a Haiku tier —
  unattended work shouldn't spend the session's model budget). Silent, never
  blocks session start, and a failed run costs one duplicate pass next session.
  - **It never writes to `.multiplai/memory/`.** That is the whole safety
    story: passes 1–2 write proposals to `.multiplai/dreams/` and the health
    log; 3–4 write derived files that are rebuilt from source.
    `/dream-remember` remains the only path that edits memory.
  - Run by hand with `uv run --no-project scripts/memory_maintainer.py
    [--force] [--dry-run]`. State: `<data_dir>/maintainer_state.yaml`.
- **Per-fact validity windows** (`scripts/lib/memory_lint.py`). Memory files
  carry one file-level `**Last Updated:**` stamp, which says when the file was
  touched, not whether a given fact is still true. Facts may now carry
  `(as of 2026-07)` and `(as of 2026-07, review by 2026-10)`; the linter reports
  `expired` (a passed `review by`) and `unmarked` (a volatile-class fact with no
  annotation). **Warn-only and non-rewriting on purpose** — the volatile-class
  patterns are heuristics, so a false positive must cost one noisy report line,
  never a silently rewritten fact. Surfaced by the maintainer and in
  `/health` (`memory_validity`), and the dream prompt now asks for the
  annotation on newly proposed volatile lines.
- **Conflict-triggered supersede edits** (`scripts/lib/conflict_edits.py`). For
  each CORRECTION / `trust: verified` learning, a deterministic pass finds the
  existing memory line that learning is about and prepends a
  `## Conflict Resolutions` section **above** the model's own proposal output,
  so review sees corrections before new information. Precision over recall by
  design (`MIN_OVERLAP`): it emits nothing when unsure, and it says plainly that
  text overlap cannot distinguish "contradicts" from "restates" — both want the
  same handling (edit in place, don't append a near-duplicate), and which one it
  is stays the reviewer's call. Fail-open: a crash here never loses a proposal.
- **Behavioural contracts for skills** (`skills/costs/CONTRACT.md`,
  `skills/log-doctor/CONTRACT.md`) — the two pilots for the `--contract` mode of
  `multiplai-dev`'s `promote_skill.py`. Each case is a shell command plus a
  substring that must appear in its output; they pin the *shape* of the
  interface (the `--by` dimensions, `--json` being parseable JSON), never the
  numbers, since a cost ledger changes hourly.
- **Cost reporting per outcome, not per token** (`scripts/costs_report.py`):
  - `--group task --pr-join` — a task is a branch whose PR state resolves via
    `gh` (merged / closed / open / no-pr); the summary divides total cost by
    merged-PR count with per-task median/p90. A repo `gh` cannot reach degrades
    to `no-pr` instead of failing the report.
  - `--group build` — cost per DONE **and** per FAILED buildme block, joined
    from `specs/changes/*/.build-state.json`. The failed-block figure is the
    retry-and-loop spend.
  - `--report cache [--cache-threshold R]` — per project/skill/component cache
    hit ratio `cr / (in + cr)` and write share, flagging rows under the
    threshold. Computed from fields already in every ledger record, so it works
    on historical months. Baseline measured 2026-07-25: **99.7% overall hit
    ratio**, every SDK component at 100.0% — "already optimal", not a fix.
  - **Framing rule:** cross-model comparisons are per-outcome only. Different
    models tokenize differently, so a cheaper per-token model that loops twice
    is more expensive.

### Changed
- **Log text is now treated as untrusted input** (`scripts/log_doctor.py`).
  Anything that reaches a log — an echoed HTTP response, a filename, a
  traceback carrying a remote payload — can be authored by someone who wants the
  agent *reading the digest* to act on it. Two defences applied in the script
  rather than trusted to the reader: (1) log text can no longer break out of its
  container (C0/C1 controls, ANSI escapes, zero-width and bidi characters
  stripped; code fences and `untrusted-content` tags defanged), and (2)
  instruction-shaped spans are marked in place as `⟪INJECTION?⟫` — the analyst
  still sees the original words, which is the forensic signal, but they arrive
  labelled. Every digest carries an `UNTRUSTED_NOTICE` stating that fenced
  content is data, never instructions, and that imperative text inside a fence
  is a finding to report, not an order to follow.
- **Config-audit nudge cadence tightened 90 → 60 days** (`session_start.py`,
  `config_audit.py`, `skills/config-audit/SKILL.md`). The cadence is now pinned
  to how often *models* ship, not to how fast configuration rots: what the audit
  removes is scaffolding a newer model no longer needs, and rules written for a
  model two releases ago still cost tokens and still constrain a model that
  outgrew them. The skill gained a "delete scaffolding aggressively" section —
  the test is "would a current model do this correctly without being told?", and
  a ruleset that *shrinks* after a model upgrade is the expected outcome.
- `/health` now reports per-fact `memory_validity` findings alongside the
  file-level mtime freshness. Non-fatal: a lint failure must not take down the
  report people run when something else is already broken.

## [0.7.0] - 2026-07-23

### Added
- **In-file decision record for dream proposals (`## Processed`).** When
  `/dream-remember` applies, edits, or rejects an item, the item's block is now
  **moved into a `## Processed` section** at the end of the proposal `.md`
  instead of the proposal being reviewed all-or-nothing. Items under
  `## Processed` are no longer pending, so a proposal can be reviewed partly in
  the multiplai-gui GUI and finished in the CLI (or vice versa) with no
  double-applies. The `## Processed` heading is the entire cross-tool contract —
  no sidecar file, no key scheme. Mirrors the multiplai-gui hub.
  - New `scripts/lib/dream_processed.py`: `move_to_processed` / `mark_processed`
    (idempotent, group-aware block relocation via write-then-rename) and
    `has_pending_items`.
  - New `dream.py --mark-processed --proposal … --kind {update|action} --index N
    [--file …] --status {applied|edited|rejected} [--target …]` verb, called by
    the skill per item so decisions are recorded mechanically, never hand-edited.
  - `dream.py --archive` now **refuses** a proposal that still has pending items
    (exits non-zero), so a partially-reviewed proposal is left pending instead of
    silently discarding the remainder.

### Changed
- `dream-remember` SKILL: Step 3 presents only pending items (skips
  `## Processed`); Step 4/4b record each decision via `--mark-processed`; Step 5
  cleans up learnings only when the proposal is fully decided; Step 6 archives
  only when nothing is left pending (undecided items stay for a later run/GUI).

## [0.6.17] - 2026-07-17

### Fixed
- **Single-token false positives in `token_overlap` routing.** Smoothed IDF
  over a small catalog values any df=1 domain term at `log((N+1)/2)+1` —
  ≈3.74 on a 30-entry catalog, above `MIN_SIGNAL=2.0` — so ONE incidental
  generic token ("search", "browser", "skill", "data") injected an unrelated
  file (live-reproduced: a travel prompt pulled in three files, each on a
  single token; "data" alone injected five). Two levers, both scoring-side
  (`keep_ratio` / relative-cutoff logic untouched):
  - **Match-breadth eligibility gate** (`MIN_DOMAIN_MATCHES = 2`): an entry
    can clear the NONE floor only if it matched ≥ 2 distinct `intent_domains`
    tokens or a multi-word domain phrase appears verbatim in the prompt.
    Ineligible entries still rank in diagnostics but are never picked
    (`select_multi` only; `select()` keeps its pure rank+cap contract).
    Hardcoded, not a plugin option — 2 is the smallest breadth that kills
    one-token noise, and no catalog geometry favors another value.
  - **Per-term IDF cap** (`MAX_TERM_IDF = 2.5`) bounds the small-catalog
    inflation so no single locally-rare term can dominate a score (the
    historical `audiovideo.md`=23.5 spike mechanism).
  Seed golden set: NONE-accuracy 66.7% → 100% with recall and precision held
  at 100%; production replay unchanged (28.4% cap-hit at `keep_ratio=0.30`).
- **`ROUTING_SCORES` accuracy under the gate.** Picks are no longer a
  contiguous prefix of the ranking, so the log line now emits the router's
  actual injected set (`picked_scored`) and computes `floor_excluded` as the
  best non-injected score.
- **The other contiguous-prefix consumers of the ranking.** A shared
  `_picked_ranking` helper now feeds everything that reports or reasons
  about "what was actually injected":
  - The post-cooldown re-floor anchors on the top *pick* instead of the raw
    ranking's top — which, under the gate, can be an ineligible entry that
    never injected and thus never appears in `suppressed`, silently
    disabling the re-floor exactly when weak cooldown survivors most need
    re-checking. Intended side effect (matches the documented contract):
    bundle/co_retrieve expansion picks are no longer bar-checked via their
    incidental raw-pool scores.
  - The activity-line score hint reads top/floor from the picked set, and
    abstention distinguishes "best N lacks match breadth" (raw best may
    clear MIN_SIGNAL — the single-token trap) from a genuine
    "best eligible N < floor" (via a new `top_eligible` diag field).
- **`replay_router_logs.py`** imports `MIN_SIGNAL` from `lib.memory_router`
  instead of mirroring the constant (drift-proof, and satisfies the sys.path
  wiring test).

## [0.6.16] - 2026-07-17

### Fixed
- **Routing cap-saturation / filler injection.** The `token_overlap` router
  was hitting the 10-file memory cap on ~45% of routes (measured by replaying
  439 real routing calls, 2026-07-10→16). Root cause: `domain_score` is an
  unnormalized sum of matched IDF weights, so a long/rich prompt inflates the
  whole ranking and the shallow `0.20×top` relative cutoff admits a fat filler
  tail — median 16 candidates cleared it and the cap chopped to 10, so the
  *cap*, not relevance, did the filtering. The relative-cutoff ratio is raised
  `0.20 → 0.30` (halves cap-saturation to ~28%; production-replay is exact for
  a tighter policy since it can only trim). Top matches are always retained;
  only the sub-30%-of-top tail is dropped. On the seed golden set, `0.30`
  raised precision 96.8%→100% and false-positives 3.6%→0% with recall held at
  100% (multi-domain matches preserved).

### Added
- **`keep_ratio` plugin option** (default `0.30`) exposes the cutoff as a
  live-tunable knob, so it can be dialed in production without a code release
  while a full golden eval set is rebuilt. Clamped to `(0, 1]`.
- **`scripts/replay_router_logs.py`** — label-free, real-traffic eval that
  sweeps `keep_ratio` against your own `ROUTING_SCORES` logs.
- **`eval_router.py --keep-ratio`** for golden-case sweeps at a given ratio,
  plus a **synthetic** eval fixture (`evals/synthetic-fixture-catalog.json` +
  `evals/synthetic-cases.jsonl`, a fictional persona — no real user data) so
  the harness runs out of the box for CI / reviewers (the previously-cited
  50-case golden set was absent from the repo). Real, user-supplied golden
  sets live privately under `<workspace>/.multiplai/data/evals/`, which
  `eval_router.py` now discovers by default.

## [0.6.15] - 2026-07-16

Fixes from the 2026-07-12→16 PR audit (mktplace PRs #24–#39).

### Fixed
- **PreCompact freshness gate (data-loss guard).** The summarizer-stub
  directive is now emitted only when the checkpoint on disk is *fresh* —
  the synchronous pre-compaction checkpoint succeeded this invocation, or
  the checkpoint's recorded token watermark / file mtime is close to the
  live context size. Previously a *stale-but-valid* checkpoint (writer
  timed out, script missing, tokens unreadable) still stubbed the native
  summary, silently losing the tail of the session. `_sync_checkpoint`
  now returns explicit success/failure (all four silent-fail paths return
  False and are covered by tests), a writer exiting non-zero counts as
  failure, and a context size of 0 always keeps the native summary.
- **Dream: last invisible failure path.** `create_client()` in
  `dream_report()` now sits inside the logged try/except, so an
  SDK-unavailable `RuntimeError` lands in `dream.log`/`hook-errors.log`
  like every other dream failure instead of dying only on task stdout.
- **Session-registry GC races.** GC now takes the same per-entry flock
  the writers use and re-checks staleness under it before unlinking, so
  an entry can no longer be deleted out from under a hook/hub mid
  read-merge-write. `.lock` files are removed only while their flock is
  held (lock-then-unlink); when the lock is unavailable they are left
  for a new orphan sweep that collects aged `.adopt`/`.lock` files whose
  entry is gone (non-blocking flock probe, re-check under the lock).
- **uv-guard marker fallback.** When `$CLAUDE_CONFIG_DIR` is unwritable,
  the missing-uv warning marker now degrades to `$TMPDIR` instead of
  re-warning on every hook event.

### Added
- **CLI-version canary for the summarizer-steering channel.** The
  PreCompact hook reads the CLI version from `AI_AGENT` and logs a
  warning when the major is newer than the last verified one (2.x /
  2.1.207) — the directive is still emitted (worst case is the native
  summary). Documented in README with the residual risk.
- **qmd http exposure documented where it's configured.** The
  `qmd_http_url` option description and README now state plainly that a
  `0.0.0.0`-bound daemon is an unauthenticated read-only HTTP endpoint
  over the whole indexed corpus, reachable from the LAN unless scoped,
  with a concrete macOS pf rule scoping `:8181` to the OrbStack subnet.
  The `0.0.0.0` bind instruction itself is unchanged (required for
  container→host reach under OrbStack).

### Changed
- `qmd_candidate_limit` is now capped at 50 so the HTTP timeout it
  scales (`http_timeout()`) is bounded (~38s worst case).
- `qmd_refresh.py` shell-quotes the workspace path in the remote SSH
  maintenance command.
- Test hygiene: the 90-day config-audit boundary test pins `>=` at
  exactly 90 days under a frozen clock; the config-audit skill's
  "never applies" test uses an explicit sentence whitelist instead of an
  80-char negation-proximity heuristic.

## [0.6.14] - 2026-07-16

### Added
- **Charter-based extraction targets.** The Stop-hook extractor (and
  backfill) no longer routes learnings against a bare filename list: each
  valid target now renders as `file — purpose. NOT: …`, with `purpose`
  taken from the first sentence of the file's memory-catalog summary and
  the `NOT:` note from its `anti_domains`. New shared loader
  `lib/extraction.load_target_charters()` (replaces the two drifting
  `_list_valid_targets` copies); degrades gracefully to bare names when
  the catalog is absent/unreadable. The prompt's "use the closest match"
  instruction is gone — learnings that fit no file's domain are now tagged
  `target: unknown` for downstream consolidation to reroute or filter,
  instead of being forced into the closest broadly-named file.
- **Deterministic routing-validation gate on dream proposals**
  (`lib/routing_validation.py`, pure code, no LLM). Every generated
  proposal now ends with a `## Routing Warnings` section — `(none)` when
  clean — produced by two checks: a section-registry check (H2 section
  names are unique across memory files, so an entry whose `Section:`
  lives in a different file than its target is a misroute, and a "new
  section" colliding with another file's section breaks the invariant)
  and a cross-file duplicate check (normalized 8-gram token overlap of
  each proposed insert against *all* memory files, ≥50% flags). The gate
  only warns — it never rewrites the proposal — and is wired fail-open +
  loud: a gate crash logs an exception and the proposal is written
  without the section.
- **dream-remember consults the warnings.** The skill now surfaces
  Routing Warnings at presentation time and gates application: flagged
  items are never silently applied — misrouted sections propose a reroute
  to the section's owner file, new-section collisions ask for a
  rename/reroute, and cross-file duplicates are read at the flagged
  location and skipped/merged unless the user confirms an intentional
  update.

### Changed
- **Dream routing prose is data-driven, not hardcoded.** `dream.py`'s
  proposal prompt now renders `NOT HERE:` per candidate file from the
  catalog's `anti_domains`, and the routing principles in
  `_PROPOSAL_SYSTEM` / the critic's mis-routing check (`_CRITIC_SYSTEM`)
  are genericized to work from the per-file PURPOSE/OWNS DOMAINS/NOT HERE
  blocks instead of naming specific memory files that drift. The critic
  now receives the same memory-domain blocks as the proposal pass.

## [0.6.13] - 2026-07-15

### Fixed
- **Bumped every `multiplai-core` pin to `@v0.8.1`** (across all PEP 723
  scripts and `requirements-dev.txt`). `v0.8.1` floors `claude-agent-sdk`
  to `>=0.2.116,<0.3`; the previously-pinned `v0.6.0` capped the SDK at
  `<0.2`, forcing the 0.1.x line. Against a modern Claude CLI (`>=2.x`)
  that SDK misparses the terminal result message and raises the
  deterministic `Claude Code returned an error result: success` *after* a
  full generation — the failure that silently broke `dream` (and which any
  other `[sdk]` consumer — `extract_learnings`, `backfill`,
  `context_manager`, `generate_catalog`, `checkpoint_writer`,
  `synthesize_now` — would have hit next). Restores pin consistency (the
  `test_core_pin_consistency` drift-guard was red: `dream` had moved to
  `v0.8.0` while every other script lagged at `v0.6.0`).

### Added
- **`dream` SDK-call failures are now diagnosable from persistent logs**
  (folds in the unreleased 0.6.12 work): `multiplai-core` `v0.8.1`'s
  `setup_logging(propagate_loggers=…)` attaches the dream file handler to
  the `multiplai_core` package loggers, and `dream.py` wraps the
  `_generate_proposal` call so a failed report logs an exception and exits
  non-zero instead of vanishing silently.

## [0.6.11] - 2026-07-14

### Added
- **qmd `http` execution mode.** A third `qmd_mode` (`http`) POSTs an
  authored, typed query to a resident `qmd mcp --http` daemon on the host
  instead of shelling out per prompt. The daemon keeps the embedding and
  rerank models warm in VRAM (no ~12s cold start per prompt) and does the
  fusion + rerank itself; the request is a JSON `searches` array, so the
  ssh bridge's shell-quoting and newline limits no longer apply. New
  options: `qmd_http_url` (default `http://host.docker.internal:8181`),
  `qmd_candidate_limit` (rerank latency dial, default 10), and
  `qmd_min_score` (weak-match cutoff, default 0.30, now applies to all
  modes). `qmd_strategy` is ignored in `http` mode.

### Changed
- **Authored typed queries replace raw-prompt pasting (http mode).**
  Rather than sending the user's whole sentence — which qmd's lexical arm
  ANDs (stopwords and all) at 2× RRF weight, electing junk to rank 1 — the
  lexical arm now carries only the IDF-rarest content words (document
  frequency read from the project-local `.qmd/index.sqlite`), with the
  full prompt on the vector arm and passed as `intent`. Degrades to
  stopword-filtered word order when the index is unreadable.
- **Stopword list extended** with intent/quantifier fillers (`learn`,
  `more`, `understand`, `explain` and morphological variants) that were
  leaking into the lexical keyword arm.

## [0.6.10] - 2026-07-14

### Added
- **`/multiplai-context:config-audit` — subtractive config/rules review**
  (gap B1 of the AI-coding-insights analysis). The skill enumerates the
  active config surface (`$CLAUDE_CONFIG_DIR/CLAUDE.md`, workspace
  `CLAUDE.md`s, `settings.json` env/permissions blocks, hook
  registrations, memory-file standing rules), classifies every standing
  rule as *still-serving*, *obsolete*, or *model-constraining*, and
  writes a removals-first proposal to
  `.multiplai/dreams/config-audit-YYYY-MM-DD.md` for user review. It
  never applies changes — same propose-then-review UX as dream. A new
  90-day SessionStart gate (`_config_audit_gate_open`, state file
  `config_audit_state.yaml` beside the dream state) nudges
  `/multiplai-context:config-audit` when the review falls out of
  cadence. The stamp is deterministic: the skill's final step runs
  `scripts/config_audit.py --stamp` (mirroring `dream.py --stamp`),
  which resolves the data dir via the same `get_paths()` cascade the
  gate uses — never hand-written YAML. Gate semantics: a **missing**
  state file (fresh install) is seeded with `last_run: now` and does
  NOT nudge — the 90-day clock starts at install; a stale (>=90d) or
  existing-but-corrupt state opens the gate (fail-open recovery, like
  the dream gate).

## [0.6.9] - 2026-07-14

### Added
- **Near-instant compaction via summarizer steering.** The PreCompact hook
  now prints a directive to stdout when a valid checkpoint exists — Claude
  Code appends PreCompact stdout to the compaction summarization prompt as
  custom instructions (verified in the CLI 2.1.207 binary; the background
  precompute path honors them too), so the native summarizer emits a
  one-sentence stub instead of a multi-KB summary. The checkpoint rebuild
  (SessionStart source=compact) carries the real state. Safety gates: only
  when checkpointing is enabled, the session is not a child, and
  `checkpoint.md` validates; the pending rebuild marker is written first so
  even a manual /compact below the handoff threshold gets its checkpoint
  re-injected. Any doubt → native summary (silent stdout).

## [0.6.8] - 2026-07-12

### Added
- **Hub session registry.** The lifecycle hooks now maintain a per-session
  JSON entry at `<data_dir>/sessions/<session_id>.json` implementing the
  "hub input contract" of the multiplai hub (spikelab/multiplai-gui,
  `docs/api-contract.md`): identity fields (`session_id`, `hostname`,
  `cwd`, `project`, `workspace`, `started_at`) plus a `last_event`
  stamp — `start` (SessionStart), `stop` (turn finished, session idle
  and adoptable), `notification` (waiting for user input — the hub's
  push trigger; a new Notification hook, the plugin's sixth event), and
  `end` (SessionEnd). Writes are atomic (tmp+rename), read-merge-write
  so hub-written keys survive, and best-effort throughout — the hooks
  never raise. SessionStart GCs entries whose session ended more than
  7 days ago (removing orphaned `.adopt` markers with them) and the
  data-dir `*` gitignore keeps registry files untracked by mechanism.
  Degradation: works identically with or without docker or the kit;
  with no hub installed the files are simply never read.

## [0.6.6] - 2026-07-10

### Added
- **Memory-vs-session conflict surfacing.** Every injected `=== MEMORY ===`
  block now opens with a directive requiring the model to cross-check the
  retrieved memory against the rest of the session's context (documents,
  pasted files, other injected context, user statements) and, on any
  disagreement, explicitly surface it to the user — naming the memory
  file, presenting both versions, and stating which source it follows
  (newer/in-session wins by default). Each memory file is additionally
  stamped with its last-updated date so the model has a concrete recency
  signal to judge staleness: the in-content `**Last Updated:**` header
  (maintained by the dream tooling) is preferred, falling back to
  filesystem mtime — mtime alone lies after a re-clone/checkout, which
  would stamp stale facts as fresh. Applies to both injection paths
  (router picks and the recency fallback), fused in a single renderer so
  the directive and the stamps can't ship separately. Opt out via the
  new `memory_conflict_preamble` option (default `true`; ~90 tokens per
  memory-carrying turn). Detection lives in the model rather than the
  hook because only the model sees the full session context; covered by
  unit tests plus an opt-in live-LLM E2E test (`MULTIPLAI_E2E_LLM=1`)
  that demonstrates a stale memory fact being flagged against a
  contradicting in-session document.

### Internal
- Consolidated the context_manager E2E test harness (sandbox layout,
  catalog writer, subprocess hook runner) into `tests/conftest.py` —
  three test suites carried drifting near-copies.

## [0.6.5] - 2026-07-10

### Added
- **Reviewed dream proposals are archived out of the dreams root.**
  `dream.py --stamp` takes `--archive <proposal-path>` (with
  `--archive-as applied|rejected`, default `applied`): after stamping
  dream state it moves the reviewed proposal into `dreams/applied/` or
  `dreams/rejected/`, collision-safe (`-2`/`-3` suffix instead of
  overwriting a previously archived same-name file) and via plain rename
  (`git mv` would fail on the typically-untracked fresh proposal).
  `--auto` runs self-archive their audit-trail proposal after a fully
  successful apply. Previously applied proposals sat in
  `.multiplai/dreams/` indistinguishable from pending ones.

### Changed
- **dream-remember Step 6 runs on every review, including `none`.** The
  skill now stamps with zero counts and archives the proposal as
  `rejected` when the user declines everything — a fully rejected
  proposal was previously left looking pending forever (its source
  learnings already deleted), so the next run re-presented it. Step 1
  explicitly scopes proposal discovery to the dreams root (never
  `applied/`/`rejected/`) and pins the exact proposal path for Step 6 so
  a concurrent session's newer proposal can't be archived by mistake;
  the Step 8 summary no longer claims an archive on runs where none
  happened.

## [0.6.4] - 2026-07-09

### Added
- **Config knobs now do what the README promises.** `catalog_model_diary`
  is wired: the diary catalog generator runs on its own model
  (`effective_diary_model`) instead of inheriting the generic
  `catalog_model`. `catalog_ttl_hours` is wired on the read path:
  `context_manager` emits a once-per-session advisory staleness warning
  when a loaded catalog's `generated_at` exceeds the TTL (a cheap
  timestamp compare — it never regenerates inline; run
  `/refresh-catalogs` for that).
- Drift-guard tests: the `multiplai-core` pin is now asserted consistent
  across every script's PEP 723 metadata and `requirements-dev.txt`, and
  every shipped skill is asserted to have a README command-table row.

### Changed
- **`session_state.json` writes are atomic and key-preserving.**
  `session_start` and `session_stop` now go through the atomic
  temp+rename helper and read-merge rather than overwrite, so a session
  start/stop no longer drops a concurrent session's `turn_index` /
  `recently_injected` cooldown state.
- README command table completed with the `now`, `log-doctor`, and
  `costs` skills; `marketplace.json` version realigned with `plugin.json`
  (had silently lagged at 0.6.1).

### Fixed
- **qmd SSH remote string fully sanitized.** `build_argv` now vets and
  single-quotes the workspace path (from the hook cwd) and the collection
  name (from config), not just the query — an unsafe value now yields no
  command (fail-open) instead of risking shell injection over the bridge.

### Removed
- **`catalog_reasoning_effort`** config option (and its README/schema
  entries). It was validated and documented but never consumable: the
  `multiplai-core` `ModelClient.query()` interface has no reasoning/thinking
  parameter, so honoring it would require an out-of-scope cross-repo
  change. Removed rather than left as a false promise.

### Internal
- Added `pytest-timeout` to the dev toolchain (`--timeout` is assumed by
  docstrings/reviewer commands). Defused ~12 dormant
  `pytest.skip("...does not exist yet")` guards for files that now exist —
  a renamed/removed file now fails loudly. Tidied dispatcher
  signature-introspection tests (behavioral return-value check; dropped a
  tautological assertion) and removed a misleading unused `create_client`
  import from `context_manager`.

## [0.6.3] - 2026-07-09

### Changed
- **qmd retrieval entries now carry the matching chunk's line number.**
  qmd matches chunks, not whole documents; injected resource entries
  render as `(score 0.72, line 5) Title` so the model (and the reader)
  can jump straight to the matching chunk instead of skimming the whole
  file. The `qmd-search` skill documents the chunk semantics (`line`,
  `@@` snippet context headers, best-chunk-per-file dedup, `qmd get`).

## [0.6.2] - 2026-07-09

### Fixed
- **log-doctor injection traces use the embedded prompt.** Decision traces
  now print the `prompt` key that 0.5.3 ROUTING_SCORES payloads carry, and
  the footer note claiming "prompts are not logged by context_manager" —
  stale as of 0.5.3 — only appears when the scanned lines actually predate
  the embedded prompt (and now says so).

## [0.6.1] - 2026-07-09

### Added
- **Cost ledger + `/costs` skill.** A new collector
  (`scripts/collect_costs.py` over `scripts/lib/costing_collector.py`)
  incrementally scans Claude Code session transcripts, prices every API
  call (per-model rates + cache tiers, unknown model → fallback), and
  appends to a monthly append-only JSONL ledger under `<data_dir>/costs/`.
  Span attribution follows Skill / Agent / Workflow invocations (sidechain
  subagent traffic flagged and attributed to the innermost agent span;
  nested/ambiguous spans marked `approx`). Offsets are checkpointed and
  records dedup by `msg_id`, so re-runs append nothing new.
- **`costs` skill + `scripts/costs_report.py`** — reports month-to-date
  totals and breakdowns `--by session|skill|project|model|day|component`,
  with `--session` for a per-chat itemized bill and `--json` output.
- **SDK cost tap.** SDK-driven pipelines tag their runs with a `component`
  (buildme, deep-research, dream) so their spend lands in the same ledger.
- **Automatic collection at session start.** With the new `enable_costs`
  option on, `session_start.py` fires the collector detached (like the qmd
  refresh) so the ledger stays current with no manual step. The collector
  self-guards with an flock, so racing session starts can't double-append.
  Opt-in (default off); local-only, nothing leaves the machine.

### Changed
- Pinned `multiplai-core` to **v0.6.0** plugin-wide (adds the `costing`
  module and the `component` cost-ledger tap in `run_agent`). The
  `buildme` and `deep-research` pipeline `pyproject.toml` pins moved to
  v0.6.0 to match their new `component=` call sites.

## [0.6.0] - 2026-07-08

### Added
- **qmd resources retrieval backend (`resources_retrieval=qmd`).** When
  `enable_resources` is on, resources can now be retrieved through a
  [qmd](https://github.com/tobi/qmd) hybrid index instead of the
  catalog+router path: BM25 keyword ladder + vector search fused by
  reciprocal rank (`scripts/qmd_retrieval.py`), per prompt, no LLM in the
  loop. Results render as path + excerpt entries in the existing
  `=== RESOURCES ===` section and respect the re-recommendation cooldown.
  Fail-open throughout: any qmd error/timeout/missing binary means "no
  resources this turn", never a blocked prompt. `catalog` remains the
  default — nothing changes for existing users.
- **New plugin options** — `resources_retrieval` (`catalog`|`qmd`),
  `qmd_mode` (`local`|`ssh` for container→host bridge execution),
  `qmd_ssh_host`, `qmd_collection`, `qmd_strategy`
  (`fused`|`hybrid`|`fts`).
- **Incremental index refresh at session start.** When the qmd backend is
  active, `session_start.py` fires a detached, per-workspace flock-guarded
  child (`scripts/qmd_refresh.py`): `qmd update` + `qmd embed` retry
  passes (embedding can die mid-run but is incremental).
- **`qmd-search` skill** — manual/deep searches against the same index
  (semantic + keyword + hybrid rerank), config-aware for both `local` and
  `ssh` modes. Ships `scripts/setup_qmd.sh`, the one-shot host setup
  (bun + qmd install, `qmd init`, collection add, index + embed, smoke
  query). Container setups additionally need the qmd allowlist in the
  multiplai-container SSH-bridge gateway.
## [0.5.3] - 2026-07-07

### Fixed
- **Post-cooldown relevance re-floor — weak co-picks no longer injected once
  their anchor is suppressed (injection forensics, session 351388d2).** The
  router admits everything within `KEEP_RATIO` (20%) of a corpus's top score,
  so near-floor files ride in as companions of a strong match; when the
  cooldown then suppressed that top scorer, the weak survivors were injected
  alone (e.g. life.md at 3.335 after its 10.8/9.9 anchors were suppressed —
  perceived as "it injected stuff that makes no sense"). Now, when the
  top-ranked pick is itself cooldown-suppressed, survivors must re-clear
  `POST_COOLDOWN_KEEP_RATIO` (0.5) × the suppressed top score or nothing is
  injected; drops are logged as `COOLDOWN_REFLOOR`. Design: chosen over the
  alternative of running cooldown *before* the floor/cap pick because the
  surviving weak tail then forms a fresh ranking whose top can still clear
  the absolute `MIN_SIGNAL` floor — exactly the observed failure. Behavior is
  unchanged when the top pick survives cooldown, when scores are unavailable
  (LLM router), and for unscored bundle/co_retrieve expansion picks.

### Added
- **`ROUTING_SCORES` lines carry the prompt.** Each line's JSON payload now
  includes a whitespace-collapsed, 80-char-truncated `"prompt"` key so
  score→prompt attribution no longer requires digging through session
  transcripts. Embedded in the JSON (not a trailing `key=value`) so existing
  `memory={...}$`-anchored parsers (/health, log tooling) keep working. The
  prompt is already session context per the logging standard's PII rule.
- **`ROUTING_SCORES` emitted for skills and resources corpora.** Previously
  only `memory=` was logged, leaving skill/resource injections with no score
  trail. Memory still logs unconditionally (the /health contract); skills and
  resources log whenever their corpus is enabled.

## [0.5.2] - 2026-07-07

### Added
- **`/log-doctor` skill + `scripts/log_doctor.py`.** Scans the runtime logs
  directory (`paths.logs_dir()`), clusters ERROR/WARNING/INFO entries by
  normalized signature (with traceback tails, first/last seen, counts), runs
  cross-cutting health checks (oversized append-only logs, format drift,
  missing session ids), and supports per-subsystem focus (`--subsystem`),
  recency windows (`--days`), and JSON output. The skill guides root-cause
  verification against source code before writing a fix-recommendation
  report to `INBOX/`. Read-only; the scanner has no LLM dependency.
- **log-doctor probe mode.** Exercise a functionality and assert its expected
  log entries appeared: `--probe-start` snapshots per-file byte offsets,
  `--probe-check --scenario <name>` evaluates only content appended since the
  baseline. Ships grounded scenarios (session-start/end/stop, routing,
  extract-learnings, generate-catalog, synthesize-now, backfill, dream,
  deep-research) plus ad-hoc `--expect SUBSYSTEM:LEVEL:REGEX` expectations;
  unexpected ERRORs from the involved subsystems fail the probe (exit 1).
  Entries now carry their parsed `[component]`, so errors that only surface
  in `hook-errors.log` are attributed to the right subsystem.
- **log-doctor injection forensics (`--injections`).** Reconstructs each
  context-routing decision by joining `context_manager` ROUTING_SCORES /
  COOLDOWN lines with `activity.jsonl` inject events: per-file
  picked/injected/suppressed counts with score stats, cap-hit and abstain
  rates, and `--trace N` full decision traces (`--file X` to focus on one
  file). Explains "why did it inject that" cases — e.g. cooldown suppressing
  the top scorers so near-floor files fill the slots.

## [0.5.1] - 2026-07-07

### Fixed
- **Learning extraction no longer depends on resolution luck (log-doctor F1).**
  All scripts that create a model client now declare `multiplai-core[sdk]`
  in their PEP 723 headers — uv script envs get no host-injected
  `claude-agent-sdk`, so extraction silently lost every session whenever a
  re-resolved env happened to omit it. Core pin bumped to v0.5.1 across all
  scripts (brings in the pytest log-dir guard and hook-errors.log oversize
  truncation from multiplai-core 0.5.1).
- **Diary catalog now actually regenerates during backfill (log-doctor F2).**
  The post-pass called `generate_catalog.main()`, whose `asyncio.run()`
  always raised `RuntimeError` inside backfill's running event loop — the
  "Regenerated diary catalog" branch had never executed (failure was logged
  as a non-fatal warning). Backfill now awaits `generate_catalogs()`
  directly; regression test added.
- **Tests can no longer write into real workspace logs (log-doctor F3).**
  Scripts configure logging at module import, which pytest runs during
  collection — before any fixture. conftest now pins `WORKSPACE` to a
  throwaway temp dir at import time and also scrubs
  `CLAUDE_CODE_AUTO_COMPACT_*` / `CLAUDE_AUTOCOMPACT_*` (ambient
  autocompact steering flipped checkpoint hooks into silent auto mode,
  failing 5 checkpoint tests on steered hosts).
- **Standard-format log lines carry real session ids (log-doctor F5).**
  Hook entry points re-bind `setup_logging` with the payload's session id
  instead of leaving `[session:--------]` on every WARNING+ line.

## [0.5.0] - 2026-07-07

### Added
- **Context checkpointing & rebuild (MiMo-style long-horizon support).** One
  logical chat can now span many physical context windows. The Stop hook
  measures real context footprint from the transcript tail and spawns a
  detached `checkpoint_writer.py` at token bands (default 100K/200K, tuned
  for 1M-window models) producing an incremental 11-field `checkpoint.md`;
  above the handoff threshold (200K) the checkpoint auto-refreshes every
  25K tokens, the user is advised to `/clear` via `systemMessage`, and a new
  `checkpoint_nudge.py` UserPromptSubmit hook tells Claude to wind down at a
  natural boundary. SessionStart consumes a TTL-gated pending marker and
  re-seeds the fresh session from the checkpoint. Goal-safe by construction:
  no `decision` output ever (cannot block /goal loops), child/subagent
  sessions fully excluded, writer failure never blocks the session. New
  `lib/checkpoint.py` core + config via `checkpoint_*` options; docs in
  README ("Context checkpointing"). Verified by a simulated >700K-token
  multi-rebuild E2E suite plus a live hook-subprocess smoke run.
- **Fully-automatic rebuild via steered auto-compaction.** Setting
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`/`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (README
  "Activation") makes native auto-compaction fire near the handoff threshold;
  `SessionStart(source="compact")` then injects the checkpoint into the
  compacted window — same session id, same terminal, `/goal` loops survive,
  zero user action. Same-session marker consumption is permitted only on the
  compact path; band counters reset after every rebuild so each new physical
  window re-checkpoints. In auto mode the `/clear` nudges are suppressed
  (they return only if compaction is overdue/misconfigured).
## [0.4.3] - 2026-07-06

### Changed
- **Bumped `multiplai-core` pin to `@v0.4.0`.** Picks up the library's security
  fix (the no-tools SDK client now also blocks Read/WebFetch/etc. under
  `bypassPermissions`, closing a prompt-injection exfiltration path) plus
  correctness fixes (malformed-timeout env var no longer crashes at import,
  atomic state writes, robust JSON extraction). All entry-point scripts and
  `requirements*.txt` updated.

## [0.4.2] - 2026-07-05

### Fixed
- **Every dispatcher run crashed the diary generator** — the 0.3.x
  `--only`-override feature made the dispatcher pass `force_enable` to every
  generator, but `DiaryGenerator.run()` still had the old signature
  (`TypeError: unexpected keyword argument`). Signature updated; a new
  contract test asserts every registered generator accepts the dispatcher's
  full run() contract, so future overrides can't regress this.

## [0.4.1] - 2026-07-05

### Fixed
- **refresh-catalogs skill doc contradicted the uv migration** — an Operational
  Note still told the session to invoke `generate_catalog.py` with bare
  `python` via the removed managed-venv self-routing, causing
  `ModuleNotFoundError: multiplai_core`. All skill docs now consistently
  mandate `uv run --no-project`. Also scrubbed stale `venv_dir`/`python
  dream.py` references from the health and dream skill docs.

## [0.4.0] - 2026-07-05

### Changed
- **uv + PEP 723 runtime.** The managed plugin venv (`venv_bootstrap`/`venv_guard`)
  is gone. Every entry-point script carries inline dependency metadata and runs
  via `uv run --no-project`. Shared modules (`paths`, `config`, `log_utils`,
  `model_client`) moved to the `multiplai-core` package, consumed as
  `git+https://github.com/spikelab/multiplai-core@v0.2`.

### Added
- **Installed-plugin skill discovery.** The skills catalog now also indexes
  skills shipped by installed Claude Code plugins (the themed Multiplai packs:
  pm, writing, research, dev, media) via `installed_plugins.json`, in addition
  to `skills_dir`. New `plugins_dir` option (empty = `$CLAUDE_CONFIG_DIR/plugins`).


### Changed
- **`refresh-catalogs --only <gen>` now honors its override contract.** An
  explicit `--only` filter has always been documented as running a generator
  regardless of config gating, but each generator's `run()` still re-checked
  its own `enable_*` flag and silently no-op'd — so `--only resources` did
  nothing when `enable_resources=false`. The dispatcher now threads a
  `force_enable` signal into the gated generators, so an explicitly-named
  generator runs even with its flag off (the `resources_dir` requirement is
  still enforced). This lets you keep a catalog fresh without turning on
  injection. Documented in the `refresh-catalogs` skill and README, with
  operational notes (managed-venv self-routing, `exit 1` = partial errors,
  don't `pkill -f generate_catalog`).
- **Resources catalog indexes Markdown only.** `ResourcesGenerator.discover_sources()`
  now allowlists `.md`/`.markdown` and skips dotfiles and binaries (PDFs,
  images, archives, scripts, raw `.txt`), so the routing surface is no longer
  diluted by placeholder "binary file" entries.

### Added
- **Action Items — a third dream disposition.** A learning that asks the
  toolchain to change its own code/config/file-structure ("split these files",
  "delete this orphan", "use install.sh in the Dockerfile") is no longer
  mis-filed as memory. The dream proposal now has a `## Action Items` section
  (`A{N}` numbering, What/Why/Source). On approval, `/multiplai-context:dream-remember`
  writes them to `PLANS/dream-actions-{date}.md` as unchecked tasks, so they
  survive the learnings cleanup and become durable work. A learning that
  carries a durable general *principle* alongside the change keeps **both** —
  the principle as memory and the change as an action item.
- **Bounded critic second pass over the dream proposal.** After the draft, a
  cheap second LLM pass (over the proposal only, not the raw backlog)
  surgically strips point-in-time residue (commit SHAs, `Decision (date):`
  framing, finished-task imperatives, one-off paths), demotes past-event
  records to Filtered Out, and reroutes mis-filed action items. Falls back to
  the raw draft on failure.
- **`filename:line` provenance.** Each proposal entry ends with a `**Source:**`
  line citing the learnings file and line number it was distilled from
  (pending learnings are now fed to the model with line-number prefixes so the
  citation is accurate, not guessed) — so a report is traceable on re-processing.

### Changed
- **Dream now distills generalized, reusable knowledge — not a session log.**
  The proposal prompt is built around an explicit DIARY-vs-MEMORY distinction:
  the diary already records what happened; memory holds guidance that changes a
  *future, different* task. Entries are generalized ("when X, do Y"), with the
  point-in-time scaffolding stripped. Report noise removed: no per-file
  learning counts, `seen Nx` notes, or trust labels (weak items get a
  `[warning low confidence]` marker instead).
- **`/multiplai-context:dream --auto` uses the same generalization pass as
  report mode.** Auto mode previously ran a thin per-file prompt with none of
  the above discipline. It now generates the same proposal (same prompt +
  critic), writes it to `.multiplai/dreams/` for audit, then mechanically
  applies each file's slice concurrently.
- **`router_model` option for the `llm` router** (default `claude-haiku-4-5`).
  The LLM router now forwards a model to the client; Haiku keeps the
  per-prompt classification cheap. The `UserPromptSubmit` hook timeout is
  raised to 15s (router timeout 12s) so an inline `llm` call can complete.
  See the README "Router latency" note — `llm` runs ~7-10s/prompt via the
  Agent SDK (CLI cold-start per call) and is best treated as a routing-
  quality experiment until routing moves off the blocking hook.
- **Re-recommendation cooldown.** After a file is injected, it is
  suppressed from re-injection for `recommend_cooldown_turns` turns
  (default `4`; `0` disables) — it's already in the conversation, so
  re-injecting wastes context. A turn counter and a `recently_injected`
  map persist in `data/session_state.json`; the `PreCompact` hook clears
  the map so post-compaction every file is eligible again. An
  all-suppressed turn logs `on cooldown, nothing injected` and is
  distinct from router abstention (no recency fallback). New
  `recommend_cooldown_turns` userConfig option.

### Changed
- **Activity-log `[context] inject` lines now group injected files by
  corpus** — `→ memory: … · skills: … · resources: …` instead of one
  flat comma-separated list, so you can tell which files came from which
  corpus. The JSONL mirror gains a `files_by_corpus` field.

### Fixed
- **Skills were routed but never injected.** The loader called
  `read_text()` on the skill *directory* instead of `<name>/SKILL.md`
  (the real Claude Code layout), so the skills count was always 0.
  Skills are now surfaced as lightweight recommendations (catalog
  summary + `/<name>` invocation hint) rather than full SKILL.md bodies.
- **`anti_domains` hard-excluded relevant entries across all corpora.**
  Anti phrases that reuse the entry's own positive vocabulary (e.g.
  "…unrelated to memory routing") nuked the entry on the very tokens
  that made it relevant. Anti tokens that are also the entry's
  `intent_domains` vocabulary are now dropped before the exclusion check.

## [0.3.0] - 2026-05-21

Diary layout aligned with learnings — one file per UTC day.

### Changed (breaking on-disk layout)
- **Diary now uses per-day files**, matching the learnings layout. Each
  ``YYYY-MM-DD.md`` file under your diary dir holds one ``# Diary``
  header plus one ``## Session: <id> — <ts> — <cwd>`` block per session
  that ran on that day. The previous ``YYYY-MM-DD/<sessionId>.md``
  per-session layout is gone.
- Why: easier to browse (`ls diary/` shows ~365 entries/year instead of
  thousands), consistent with learnings, append-only with `fcntl.flock`
  for concurrent SessionStart subprocesses.
- Idempotent on session_id: re-extracting the same session is a no-op.
- The diary catalog generator now iterates ``*.md`` files at the top of
  the diary directory; one catalog entry per day, same schema as before.
- Health check renamed ``diary.entry_count`` → ``diary.day_count`` to
  reflect what it actually measures.

### Migration
- No public users existed on the pre-0.3.0 layout, so no user-facing
  migration tool ships. The internal migration was a one-shot script
  applied to existing on-disk diaries during development and discarded.

## [0.2.1] - 2026-05-21

First public-marketplace-ready release. Focused on safety, transparency,
and onboarding rather than new features.

### Fixed
- **`UserPromptSubmit` hook can no longer crash your session.** The
  context-routing hook now wraps all work in a top-level guard: any
  unhandled error logs to `hook-errors.log` and emits a safe empty
  context. Previously a single failing file read could surface a
  traceback mid-prompt.

### Added
- One-time warning at session start when neither the Agent SDK nor an
  Anthropic API key is configured — so LLM-backed features (extraction,
  dreams, catalogs) silently no-op'ing is no longer a mystery.
- README: **How it works** (4-step lifecycle), **Where your data lives**
  (what gets written where + `.gitignore` snippet), **Quick start: the
  only options you probably need** (cuts the 18-option config wall down
  to 3-4 that matter).
- Platform support note in README — macOS / Linux / WSL on Windows;
  native Windows isn't supported.

### Changed
- `anthropic` dependency pinned to an exact version for reproducible
  installs.

## [0.2.0] - 2026-05-17

Internal release. Highlights for users:

- **Runtime state moved next to your memory.** Logs, catalogs, the
  plugin venv, and dream state now live at `<workspace>/.multiplai/data`
  instead of Claude Code's managed plugin dir. Side effect: routing
  catalogs are now actually loaded by default (they were silently
  unreachable before). New `data_dir` config option to override.
- **Tail-friendly logs.** Every log file rotates daily into
  `<name>-YYYY-MM-DD.log`, with `<name>.log` always pointing at today.
  Retention controlled by `MULTIPLAI_LOG_RETENTION_DAYS` (default 7).
- **`activity.log` curated stream.** One plain-language line per
  meaningful action — context injected, dream nudge, session boundary,
  diary write, learnings capture, catalog rebuild. Tail with
  `tail -f <data>/logs/activity.log`.
- Test suite hardened so it never inherits the host workspace's
  `CLAUDE_PLUGIN_*` / `WORKSPACE` env.

### Observability

- `log_utils.py` rewritten to the project logging standard: UTC ISO-8601
  lines with `[component] [session:xxxxxxxx] LEVEL:` shape, env-driven
  level (`MULTIPLAI_DEBUG=1` or `MULTIPLAI_LOG_LEVEL`), and a shared
  `hook-errors.log` for ERROR+ across all components.
- **Uniform date-rotation across every log.** `<name>.log` is always the
  *current* file; on the first write of a new UTC day it rotates to
  `<name>-YYYY-MM-DD.log` (date infix *before* the extension, per the
  standard). The stdlib `TimedRotatingFileHandler` was emitting the
  rejected `<name>.log.YYYY-MM-DD` form; legacy files in that shape are
  auto-migrated. Retention is `MULTIPLAI_LOG_RETENTION_DAYS` (default 7,
  `0` = keep forever), applied uniformly to every rotated file.
- New `log_event()` curated activity stream — one plain-language line per
  meaningful action in `activity.log` (current), mirrored to
  `activity.jsonl` for tooling, both rotating to `activity-YYYY-MM-DD.*`
  the same way. Previously these were always date-stamped with *today*,
  so there was never a stable current file to `tail -f`. Written
  regardless of log level; never raises into a hook.
- Lifecycle scripts instrumented: context inject/skip/fallback (with the
  exact files loaded), dream nudge, session start/end/pre-compact, diary
  write, learnings capture, catalog rebuild (+entry count and timing),
  deferred-extraction launch.
- README "Observability" section: live-watch command, debug toggle, log
  layout, retention.

### Fixed

- **`data_dir` is now workspace-anchored.** Previously `paths.py`
  resolved `data_dir` from `CLAUDE_PLUGIN_DATA`, so logs/catalogs/venv/
  dream-state landed in Claude Code's per-install managed dir —
  split away from `<workspace>/.multiplai/` where memory/diary/learnings
  live (and contradicting the in-code comment). Now: explicit `data_dir`
  option → `<workspace>/.multiplai/data` → `CLAUDE_PLUGIN_DATA` (only
  when no workspace) → `~/.multiplai/data`. New `data_dir` userConfig
  option. As a side effect this also resolves the router always falling
  back (the managed dir had no `catalogs/`).
- Test suite hardened: an autouse fixture scrubs ambient
  `CLAUDE_PLUGIN_*`/`WORKSPACE` so tests never inherit the host
  workspace.

## [0.1.0] - 2026-05-16

Initial public release of the **multiplai** context-manager plugin,
distributed via the `multiplai` Claude Code marketplace.

### Plugin

- `.claude-plugin/plugin.json` with `userConfig` (workspace/memory/diary/
  now/learnings dirs, sensitive `anthropic_api_key`, catalog & router
  options) and an explicit `hooks` declaration.
- Official Claude Code hooks schema (`hooks/hooks.json`): `SessionStart`
  (venv bootstrap + session init), `UserPromptSubmit` (context routing),
  `Stop` (lightweight checkpoint), `SessionEnd` and `PreCompact`
  (deferred-extraction markers).

### Core

- Path resolver with plugin-env → workspace → standalone cascade; all
  runtime state resolves through it (catalog generators included).
- Model client abstraction: Agent SDK (zero-config) with Anthropic
  API-key fallback; empty-content responses handled gracefully.
- First-run virtualenv bootstrap (`uv` preferred, `pip` fallback).
- Routed, per-prompt memory injection (`token_overlap` or `llm`
  strategy). `SessionStart` no longer dumps the full memory corpus.
- Diary-first learning extraction (brace-safe prompt construction);
  per-session diary, per-day learnings.
- Catalog generation (memory, diary, optional skills/resources) with
  content-hash incremental regeneration.
- Dream consolidation: report mode by default
  (`/multiplai-context:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai-context:setup`, `/multiplai-context:dream`, `/multiplai-context:dream-remember`,
`/multiplai-context:health`, `/multiplai-context:memory-health-audit`,
`/multiplai-context:refresh-catalogs`, `/multiplai-context:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
