# Changelog

All notable changes to the **multiplai-pm** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-pm@<version>`.

Recorded history starts at **0.1.0**. This file was written on 2026-07-26 by
reading the tag and `git log`, so the `0.1.0` entries are summaries rather than
notes taken at the time.

`multiplai-pm@0.1.0` was tagged on 2026-07-09 at the commit that created the
plugin (2026-07-05). Everything now recorded under **0.2.0** landed after that
commit while the marketplace version stayed at `0.1.0`, so for a while it was
shipping to installs under the previous version number — exactly the ambiguity
the [changelog gate](../../CLAUDE.md#release-convention) exists to prevent.
Cutting `0.2.0` on 2026-08-16 closed it; only `0.1.0` carries a git tag so far.

## [Unreleased]

## [0.13.0] - 2026-10-27

### Changed

- **pm-spec: the consistency check after the specs review no longer loops.**
  It used to check every spec from scratch on each `resume` and block on any
  major finding, so fixing one round's findings could turn up new ones with
  no end. A check after a block now rechecks the findings it blocked on: each
  must be resolved, and a new finding blocks only when critical. The block
  line counts the findings still open from earlier rounds apart from new ones.

### Added

- **pm-spec: `resume --accept <id> --accept-reason "<why>"`** leaves a
  blocking consistency finding as it is. It is recorded in
  `accepted-findings.md`, committed with the specs, and later checks do not
  raise it again. Accepting every blocking finding on unchanged files passes
  with no model call.
- **pm-spec: a round cap.** After 5 blocked checks (`consistency:
  {max_rounds: N}` in `specs/config.yaml`), `resume` stops checking until the
  open findings are accepted or `--check-again` asks for one more round.
- **pm-spec: a loud warning on unfixed findings.** When the specs pass with
  a critical or major finding not fixed (a new major one in a later round, or
  an accepted one), pm-spec prints them under a banner and
  `SPEC:WARN:<change_dir>` before `SPEC:READY`.

## [0.12.1] - 2026-10-27

### Fixed

- **`plane relations` no longer crashes** with `AttributeError: 'str' object
  has no attribute 'get'` on a Plane server that lists each relation as a bare
  issue id instead of `{project_id, issue_id}`. It now looks each id up in the
  allowed projects, the ticket's own project first, and still withholds and
  counts relations to issues it cannot find there.

## [0.12.0] - 2026-10-26

### Added
- `plane state-edit` renames, recolours, regroups, re-describes, reorders a
  workflow state or makes it the project's default, and `plane state-delete`
  deletes an empty one. Both show what will change and how many issues sit in
  the state, and change nothing until you run them again with `--yes`.
  `state-delete` refuses the default state and a state that still holds
  issues, and says what to do first.
- `plane types` lists a project's work item types, and `--type` sets one on
  `create` and `update`. `get` shows an issue's type. Types are a Plane Pro
  feature turned on per project; without them `types` says so.
- `plane create --upload FILE` (repeatable) creates the ticket and attaches the
  files in one command. Every file is checked before the ticket is created; if
  an upload fails, the error names the new ref and gives the `attachments`
  command that attaches the rest.
- `plane create` and `plane update` take `--parent <ref>` to make a ticket a
  sub-issue of another in the same project (`--parent none` clears it), and
  `get` shows the parent.
- `plane relate SPK-12 blocked_by SPK-3` adds a relation between tickets, using
  Plane's types (`blocked_by`, `blocking`, `relates_to`, `duplicate`, and the
  start/finish before/after pairs). `plane relations` lists a ticket's
  relations and `plane unrelate` removes one. Tickets in projects outside
  `PLANE_ALLOWED_PROJECTS` are refused or withheld.

### Changed
- `plane label-edit` now shows what it will change and how many issues carry
  the label, and applies only with `--yes`.
- A ticket ref like `DB-2291` is now fetched with one request instead of a
  scan of the whole project, so `get`, `update`, `comment` and the other
  commands that take a ref no longer slow down on large projects.

## [0.11.0] - 2026-10-26

### Added
- `plane state-create` adds a workflow state, which is a column on the
  project's board: give it a name, a hex colour and a group (`backlog`,
  `unstarted`, `started`, `completed` or `cancelled`), and optionally a
  description and a position. It refuses a name the project already has, and
  like every write it only reaches projects in `PLANE_ALLOWED_PROJECTS`.

## [0.10.0] - 2026-10-24

### Changed
- Skills set `effort:` in their frontmatter again (still no `model:`), so
  each runs at an effort that fits its work rather than whatever the
  session happens to be set to. The level applies only while the skill
  runs; `CLAUDE_CODE_EFFORT_LEVEL` still overrides it.
  - `low`: plane
  - `medium`: job-application, landing-page, pm-jtbd-synthesis, pm-persona-codifier, pm-pr-faq, pm-spec, pm-strategy-memo

## [0.9.2] - 2026-10-23

### Fixed
- pm-spec's first `specs.md` of each component no longer keeps the model's
  commentary. The answer now holds the file between the same two delimiter
  lines a rewrite uses, and only the text between them is written. An
  answer without them, or with text above the first `# Capability:` line,
  writes nothing and fails the run, so `resume` asks again.
- `resume` of a change handed off before pm-spec pushed copies only
  pm-spec's own files (the ones it commits) and its run folder into its
  worktree. It copied the whole change dir, and every other file (buildme's
  design, tasks and records, a person's notes) was left there uncommitted.

## [0.9.1] - 2026-10-23

### Fixed
- A change name with a `/` is refused with a message naming the change:
  `revise word-freq/word-frequencies` now says to run `revise word-freq`
  (revise reads each component's `spec-issues.md` through its change). It
  used to run the two names together and report that there is no change
  `word-freqword-frequencies`.
- A rewrite (by `revise`, or by the consistency check) no longer writes the
  model's commentary into a specs file. Each answer is now either the line
  `UNCHANGED` or the whole file between two delimiter lines; only the text
  between them is written, and a file answered `UNCHANGED` stays byte for
  byte as it was. An answer of any other shape, or one that adds text above
  the first `# Capability:` line, writes nothing and blocks the step (exit 4).
- pm-spec commits only its own files in a change dir. A `revise` in a build's
  worktree committed buildme's `.build-state.json` and `.board.json` with the
  specs; buildme's files (and anything else pm-spec does not write) are now
  left out of every pm-spec commit.

## [0.9.0] - 2026-10-23

### Changed
- pm-spec now works in three stages and stops for review after each one,
  pushing the feature branch each time. Stage 1 writes the interview and the
  proposal and stops with `SPEC:REVIEW:proposal` (exit 3). `resume` means
  "approved": stage 2 writes the use cases and the components and stops with
  `SPEC:REVIEW:components`. The next `resume` runs stage 3: the prototype,
  `tickets.json`, then the specs, and stops with `SPEC:REVIEW:specs`. The
  `resume` after that runs the consistency check and the handoff on the
  files as reviewed. `--auto` runs it all with no stop.
- An epic is no longer split into sibling changes with runs of their own. A
  change holds its components: `components.md` (component, depends on,
  ticket) and `components/<id>/` with a `plan.md`, a `.change.yaml` and a
  `specs.md`, all written by one run. A change with one component has no
  `components/` folder: its `plan.md` and `specs.md` sit in the change dir.
  `SPEC:SPLIT` and `SPEC:CHILD` are gone.
- The specs are one `specs.md` per component, with a `# Capability: <name>`
  heading over each capability's ADDED/MODIFIED/REMOVED requirements,
  instead of one `requirements/<capability>.md` per capability. The proposal
  no longer lists capabilities; with a PRD, its `## Sources` lists the PRD
  sections it draws on.
- The prototype runs for every change unless `--no-prototype` (or
  `prototype: {enabled: false}`) turns it off, after the components are
  approved. It makes whatever helps people picture the change (screens,
  endpoint signatures, sample output), with no behaviour. Files already in
  `prototype/` are kept and no agent runs. After a failed prototype run, the
  next `resume` checks what is in `prototype/` again rather than taking it
  as the PM's: it goes on once the check passes, fails again on `STATUS:
  BLOCKED`, and otherwise runs the agent again. It no longer rewrites the
  proposal.
- `tickets.json` now holds one task per component under the change's epic
  (title, objective and `## Done means` from the component's plan), or, for
  one component, the change's own ticket as a task to rewrite.
- Every run pulls the feature branch first, so edits reviewers push between
  stops are kept and read by the later stages. A file that exists is never
  written again. A branch that cannot fast-forward is refused with the
  `git pull --rebase` to run.
- The ready record is written on each component's `.change.yaml` as well as
  the change's.
- `--plan-file` is read by the interview and kept in `interview.md`, no
  longer saved as `plan.md`.

### Added
- The ticket wait: after the prototype, pm-spec pushes `tickets.json` and
  stops with `SPEC:TICKETS:<path>` (exit 3). `resume --ticket
  <component>=<REF>` (once per component) records each component's ticket in
  its `.change.yaml`, `components.md` and `tickets.json`;
  `resume --tickets-later` goes on with them pending. Any other resume is
  refused (exit 2) with the flags it expects. A `--ticket` id git would
  refuse in a branch name (a space, `..`, `~`, a leading `-`) is refused
  (exit 2) with the reason, before anything is written.
- The skill tells Claude what to share at each review stop, and to show the
  tickets and wait for a yes before creating them.
- A `specs.md` that breaks the format (a capability name that is not
  kebab-case or is used twice) is a critical consistency finding.

### Fixed
- pm-spec no longer writes `## MODIFIED Requirements` for a capability with
  no `specs/registry/` file. The specs prompt allows `MODIFIED` and `REMOVED`
  only in a capability that has one, and names them; with an empty registry
  it asks for `ADDED` requirements only. The consistency check finds a
  `MODIFIED` or `REMOVED` section in a capability with no registry file in
  code, as a critical finding.
- Every markdown file pm-spec writes ends in exactly one newline; model text
  with none, or with several, is no longer written as it came.
- The prototype's `NOTES.md` no longer ends with the agent's `STATUS:` line
  or a remark that the inputs held no embedded instructions: the line is
  read by the check and then removed, and the agent mentions embedded
  instructions only when it found some.
- A list nested under a `## Done means` criterion stays in that criterion's
  `tickets.json` acceptance entry, one sub-item per line with its
  indentation, instead of being run together with " - ".
- `interview.md` no longer has an `## Interview` heading under its
  `# Interview` title: the interview summary sits right under the title.
- `pm-spec new --help` says a `--plan-file` is kept in `interview.md`, not
  saved as `plan.md`.
- After the specs review stop, the consistency check no longer rewrites the
  reviewed `specs.md` files. It reports its findings; a critical or major one
  stops the run with `SPEC:BLOCKED` (exit 4) and the change is not marked
  ready. Someone edits the files on the branch and pushes, and `resume`
  pulls and checks them again. Minor findings are reported and the handoff
  goes on. Runs with no review stop (`--auto`, an adopted change, a run
  from before the staged flow) keep the check's one rewrite, whose prompt
  now allows `MODIFIED` and `REMOVED` only in a capability with a
  `specs/registry/` file.

### Migration
- A change written in the old layout (`requirements/<capability>.md`) is
  still read: `resume` adopts it, and `revise` fixes it in place.
- A change pm-spec 0.8.0 or earlier started (its run state copied with it)
  does not go through the three stages on `resume`: they are recorded
  skipped, and the resume only checks it again when it changed since its
  handoff, then hands it off.
- `resume` on the parent of an epic pm-spec 0.7.2 or earlier split into
  sibling child changes changes nothing: it says the specs live in the
  children and prints a `resume <child>` command for each, then exits 0.

## [0.8.0] - 2026-10-22

### Changed
- pm-spec now puts a change's specs on a branch of their own and pushes it.
  The branch is named after the change's ticket, `<TICKET>-<change>` (for
  example `ENG-42-login-timeout`): the ticket is the change's own ticket ref,
  else its nearest parent's, so an epic's children use the epic's ticket
  until they have their own. New flag `--branch <name>` (on every command)
  names another branch, which must still start with `<TICKET>-`. A ticket id
  git refuses at the start of a branch name (one holding `[` or a space, for
  example) is refused with the reason.
- pm-spec creates the branch from origin's default branch when origin does
  not have it, writes in its own worktree under `$WORKSPACE/.worktrees/`
  (`pm-spec-<branch>`, detached, so a build can check the branch out),
  commits there and runs `git push origin HEAD:refs/heads/<branch>`, never
  forced. Your checkout and its branch are left as they were, unless your
  checkout has that branch checked out: pm-spec then writes, commits and
  pushes there. pm-spec opens
  no pull request and talks to no ticket system. After `SPEC:READY` it
  prints `SPEC:BRANCH:<branch>`. An epic's children go on the parent's
  branch.
- A repository with no `origin` remote is refused: the specs reach buildme
  through the branch on origin.
- A second run reuses the worktree and fast-forwards it to origin's branch;
  one that has diverged from origin is refused, naming both commits.
- A push that fails keeps the commit, exits `1` and says so; `pm-spec resume
  <name>` then retries only the push.
- `pm-spec revise <name> --project-dir <build worktree>`, the line buildme
  prints, commits the revision in the build's worktree and pushes nothing:
  it reaches the feature branch with that build's push, or through its pull
  request.

### Added
- `--ticket-ref <ticket id>` on `new`, `resume` and `revise` records the
  change's ticket ref. On `resume` of specs already ready and pushed (an
  epic's child handed off under its parent's ticket), the `.change.yaml`
  recording it is committed and pushed to the feature branch.
- No ticket, no handoff: a change with no ticket ref, of its own or a
  parent's, is refused before the commit and the push (and, for `new`,
  before any model call), with advice naming `--ticket-ref`. Nothing is
  committed or pushed.

### Migration
- Specs a pm-spec before 0.8.0 handed off (ready and committed on your
  branch, with no feature branch) are refused by buildme with
  `Run /multiplai-pm:pm-spec resume <name> first`. That resume (add
  `--ticket-ref <ticket id>` when the change records none) copies the change
  dir (and an epic's children) onto `<TICKET>-<name>`, commits it as
  `specs(<name>): moved to <TICKET>-<name>` and pushes it; your copy stays
  where it is, to delete when you choose.

## [0.7.2] - 2026-10-22

### Fixed
- `pm-spec revise <name>` now finds a change that an `--auto` build archived.
  When `specs/changes/<name>/` is gone, it works on the newest
  `specs/archive/<date>-<name>/`, where buildme-findings writes the
  requirements issues a review raises after the archive. Before, it stopped
  with "has no pm-spec run".

## [0.7.1] - 2026-10-22

### Added
- `pm-spec`: when it splits a change into children, each child's
  `.change.yaml` now says which other children it depends on, as
  `depends_on: [<child change name>, ...]` (`[]` when it depends on none),
  and the parent's `.change.yaml` lists its children, as
  `children: [<child change name>, ...]` in dependency order.
  `/multiplai-dev:buildme` will read these to build an epic's children in
  order.
- When the parent's inputs change and the split runs again, both records
  follow the new split. A child that already exists keeps its files. A child
  the new split drops keeps its dir but is no longer in `children`.

### Changed
- A split now commits the parent's change dir (its inputs and `children`),
  as `specs(<parent>): children updated`, so it needs a git repository with
  at least one commit, as the handoff already did. A re-split that changes
  `depends_on` of a child already handed off commits that child's
  `.change.yaml` alone, as `specs(<child>): dependencies updated`. A split
  whose records are unchanged writes and commits nothing, and a commit that
  failed is made by the next run.

## [0.7.0] - 2026-10-21

### Added
- `pm-spec`: writes a change's specs in `specs/changes/<name>/` before it is
  built — an interview (asked by you, by the model itself, or posted on the
  ticket and resumed with the replies), a proposal, use cases, and the
  requirements one capability at a time, plus a prototype for a UI or
  output-format change. A read-only agent then checks that the proposal, the
  use cases and the requirements agree, reporting each contradiction, gap or
  duplicate with both places; serious findings get one rewrite and a recheck,
  and anything left unresolved stops the run with the findings printed. When
  the check passes it marks the specs ready, writes `tickets.json` (one epic
  and one task per capability, in a neutral format described by
  `references/tickets.schema.json`), commits only the change dir, and prints
  `SPEC:READY:<change_dir>:<revision>` for `/multiplai-dev:buildme`. The ready
  record covers only the files the check read: an edit made while it ran is
  checked before the change is marked ready.
- `pm-spec --prd FILE` makes a PRD the primary source: the interview asks only
  what it leaves open, and the proposal names the PRD section behind each
  capability.
- `pm-spec` takes a ticket's text with `--ticket-ref` and one `--ticket-child`
  per child item, records the refs in `.change.yaml`, gives each existing
  child its own change when it splits the work, and carries the recorded
  refs into `tickets.json`.
- `pm-spec revise` fixes the requirements issues listed in `spec-issues.md`
  (or `respec.md` with `--from-respec`), rechecks them, clears `spec_issues`
  and hands off again. While issues are open, `pm-spec resume` refuses and
  never reports the change ready.
- Every stop can be resumed with `pm-spec resume <name>`, which pays for no
  call whose result was saved. On a change whose specs were written without
  pm-spec, it runs only the consistency check and the handoff.

## [0.6.0] - 2026-10-16

### Changed
- Skills no longer set `model:` or `effort:` in their frontmatter: each runs
  on the model and effort of the Claude Code session you start it from.


## [0.5.0] - 2026-08-20

### Added
- **`plane label-create`, `label-edit` and `label-delete` — manage a project's
  labels, not just attach them.** Until now the skill could read labels and put
  an existing one on an issue; creating one was a side effect of
  `--label X --create-labels` and there was no way to fix a typo in a name or
  change a colour. Now:

  ```bash
  plane label-create -p SPK --name triage --color '#ff8800' --description "Needs a look"
  plane label-edit  -p SPK triage --name "Needs triage"
  plane label-delete -p SPK "Needs triage" --yes
  ```

  A label is named by its name, its full UUID, or at least the first 8
  characters of one — the same way in every command that takes one. An
  ambiguous reference is refused and the candidates listed, rather than
  resolved by taking the first match, and an id prefix shorter than 8
  characters is reported as too short rather than as a label that does not
  exist.

  All three honour `--json`: `label-create` returns the new id, and
  `label-delete` returns the usage count as data on the run that refuses —
  which is the number you are being asked to act on.

- **`labels` now lists each label's description**, which is the field
  `label-create --description` and `label-edit --description` set. It is the
  last column, on one line, and truncated: descriptions are free text, and one
  long one would otherwise push the `id` column off the row. `--json` carries
  the full text.

### Changed
- **`label-delete` is the first command in this skill that destroys something,
  and it is gated twice.** It prints how many issues currently carry the label,
  then refuses unless `--yes` is passed. Deleting a label strips it from every
  one of those issues and Plane has no undo; a rename (`label-edit --name`) is
  the non-destructive move when that was the real intent. Everything else is
  unchanged — issues still cannot be deleted, and project objects are still
  untouchable.

  Three things about that count and that gate:

  - The count covers the issues the API lists, so **archived issues are not
    counted** — the command says so on the same line, because "no issues" would
    otherwise read as an absolute right before an irreversible write.
  - `--dry-run` prints the DELETE **without** `--yes`. Requiring both would mean
    the only way to preview the request is to type the confirmation flag, so the
    safe command and the destructive one would differ by one flag earlier in the
    line.
  - A **label group's parent is refused** rather than counted. Plane deletes a
    group's children along with it, and the issue count does not show that: a
    parent usually carries no issues at all. This tool does not manage groups,
    so it does not delete one either.

- **`DELETE` is now pinned to label paths in the guardrail.** The guard refuses
  the verb on anything but `/projects/<project>/labels/<label>/`. "Issues are
  never deleted" was previously true only because no call site wrote one; now a
  call site that tries fails closed, and `check` proves it on issue, comment and
  cycle paths.
- A colour that is not hex is refused locally with a message naming the field,
  instead of reaching Plane and coming back as a bare `400`. `##ff8800` counts
  as not hex, and the check now happens before any network call rather than
  after two listings.
- Creating a label whose name already exists now says so and points at
  `label-edit`, instead of failing on the server's uniqueness constraint.
- Repeating `--label` on `create`/`update` lists the project's labels **once**
  per command instead of once per flag, and the `label-delete` usage scan asks
  for `id,labels` rather than whole issues — it cannot stop at the first page,
  so the payload is its entire cost.

## [0.4.0] - 2026-08-18

### Added
- **`plane comment-edit` — fix a comment instead of adding a correction under
  it.** `comments` now prints each comment's id, and `comment-edit SPK-12 <id>
  "new text"` replaces that comment's body. The first 8 characters of an id are
  usually enough; if a prefix matches more than one comment the command refuses
  and lists the candidates, because overwriting the wrong comment destroys text
  nobody kept a copy of. Like `update --body`, it replaces the whole body — it
  is not an append.
- **`plane attachments --upload FILE` — put a file on a ticket.** Repeat the
  flag for several files. Each upload prints the filename, its size, and the new
  attachment id. `--upload` and `--download` cannot be combined.

  Where the bytes go is worth knowing: Plane hands out a presigned form for its
  object store (`*.amazonaws.com` on Plane Cloud) and the file is POSTed
  straight there over HTTPS, so it does not travel through the Plane API host.
  That request is anonymous by design — your Plane token is never attached to
  it, redirects are refused rather than followed, and no host besides the API
  host or `*.amazonaws.com` is accepted. Files over 32 MB are refused, and
  `--dry-run` prints what would be requested without asking Plane for an upload
  form at all.

  If the file fails to upload after Plane has created the attachment record,
  the error says exactly that and names the attachment id, so you can delete
  the half-uploaded record before retrying rather than wondering why an empty
  attachment appeared.

### Changed
- `plane comments` prints `[<comment-id>]` on each header line. Plane's own UI
  does not show comment ids, so this is the only place to get one.

### Fixed
- **`plane attachments --download` now fetches attachment records instead of
  skipping them.** It printed `skipped '<name>': no asset id to fetch` for every
  real attachment on Plane Cloud and downloaded nothing: it was reading the
  record's `asset` field, which holds a storage key like
  `<workspace-uuid>/<hash>-<filename>` rather than the asset id the download
  endpoint answers to. The record's own id is that asset id, and is now used
  when `asset` is not itself a UUID. Only images pasted into a description ever
  came back before; files attached to the ticket did not.

  One attachment it cannot fetch no longer ends the download. A half-uploaded
  attachment has a record but no file behind it, and it used to abort the whole
  command — so everything listed after it, including images pasted into the
  description, was silently left out of the directory. Such a row is now
  reported on stderr and the rest still download.

## [0.3.0] - 2026-08-17

### Added
- **`plane` — Plane ticket management, the pack's first skill with a script.**
  Talk to Claude about tickets ("what's on my board", "move SPK-12 to done",
  "open a ticket for the login bug") and it drives a stdlib-only CLI against
  the Plane API (Cloud or self-hosted). Every request passes a **project
  allowlist** (`PLANE_ALLOWED_PROJECTS`, required, no default): writes to any
  project you did not explicitly list are refused, and issue reads, listings
  and search results are filtered to the allowlist. Two commands are
  workspace-scoped by nature and disclosed as such in SKILL.md: `members`
  returns the whole workspace roster, and `search` executes server-side across
  the workspace before its results are filtered back down. Issues, comments,
  states, labels, members, cycles, estimates, attachments (download), search;
  no deletes and no project mutation, by design. Run
  `python3 skills/plane/scripts/plane.py check` first — it prints the resolved
  config and self-tests the guardrail. Needs `PLANE_API_TOKEN`,
  `PLANE_WORKSPACE` and `PLANE_ALLOWED_PROJECTS` in the environment; see the
  README's configuration section.

  Ported from the `dolce-plane` plugin
  (DolceTech/DolceClaudeMarketplace, commit `7154f85`), then hardened in a
  pre-release review (the fixes below); its test suite (allowlist parsing,
  adversarial guardrail coverage, markdown→HTML escaping, search filtering)
  ships with it — 207 tests as of this release.

### Fixed
All found by review of the ported code, before its first release here:

- **Guardrail hardening.** A trailing double slash (`/projects/<uuid>//`) no
  longer slips a PATCH/DELETE past the "project object is read-only" rule —
  consecutive slashes are collapsed before judging, the way a fronting proxy
  would merge them. A path still percent-encoded after three decode passes is
  refused instead of waved through. And every query parameter carrying a UUID
  is checked against the allowlist whatever its key is called, so a
  cross-project filter no longer has to spell "project" to be caught
  (`search` free text alone is exempt; its results are re-filtered
  client-side).
- **`check` no longer prints a token prefix.** It printed the first 10
  characters of `PLANE_API_TOKEN` into every transcript and CI log that
  followed the documented "run `check` first" step; now it prints the length
  only.
- **Errors no longer masquerade as "not found".** A 401/5xx/network failure
  while resolving an issue by UUID surfaces instead of being reported as
  "issue not found in any allowed project" — an expired token now says so.
- **Sturdier edges.** A non-numeric `X-RateLimit-Reset` header (some proxies
  send an HTTP-date) falls back to exponential backoff instead of crashing
  the 429 retry, and a delta-seconds value no longer degrades to a 1-second
  spin. Date-only cycle bounds (legacy self-hosted) no longer crash `cycles`
  and `--cycle active`. `attachments --download` suffixes colliding filenames
  instead of silently overwriting. A create/update answered with an empty
  2xx body is confirmed as real instead of being mistaken for a dry-run.
  `PLANE_ENV_FILE` accepts `export KEY=VALUE` lines. Issue refs whose project
  identifier contains digits (`WEB3-12`) parse.

## [0.2.0] - 2026-08-16

Everything below had already landed while the marketplace version stayed at
`0.1.0` — so it was shipping to installs under the previous version number.
This release gives it a number of its own.

### Fixed
- **`pm-jtbd-synthesis` no longer names a directory that does not exist.** Its
  output guidance described a curated workspace as having `RESOURCES/` and
  `PLANS/`; `PLANS/` was retired on 2026-08-11 and the convention is now
  `ARTIFACTS/`. Text only — the instruction's actual write target was already
  `INBOX/`, so nothing behaved differently.

### Added
- **A plugin README** (`plugins/multiplai-pm/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

### Changed
- **The four Opus-tier skills** (`landing-page`, `pm-jtbd-synthesis`,
  `pm-pr-faq`, `pm-strategy-memo`) now declare `effort: medium` instead of a
  per-skill spread of low/medium/high. Frontmatter is the only lever Claude
  Code offers for model tier, so it is now the single source of truth for it.
- **The skills work on a vanilla install.** Personal memory files are loaded
  only if present: `job-application` asks you for your career history instead
  of failing on a missing voice overlay, and `landing-page` treats its voice
  files as optional. Output paths soften from a hard `INBOX/` write to
  `./INBOX` if it exists, else the current directory.
- **Cross-plugin prerequisites are declared** rather than assumed —
  `pm-jtbd-synthesis` names `transcribe` (multiplai-media),
  `landing-page`/`pm-*` name `interviewer` and `extract-insights`
  (multiplai-research). Skills that were only ever planned (`pm-prd`,
  `pm-roadmap`, `pm-opportunity-tree`) are marked as not shipped instead of
  being referenced as if they existed.
- **`job-application` documents its PDF dependency** (weasyprint plus
  Pango/Cairo) and degrades with a message naming it rather than failing
  mid-run.

### Removed
- **The author's personal data is out of the shipped templates.**
  `job-application`'s `ats-checks.md` carried a real career narrative and
  `assets/application-template.html` carried real education rows as
  "examples" — both would have been copied verbatim into a stranger's resume.
  They are now structural placeholders that teach the technique without the
  PII. `landing-page`'s `voice-calibration.md` lost its owner-specific
  spelling and default-project guidance in favour of brand-neutral wording.

### Fixed
- Dropped `user_invocable: true` from all six SKILL.md files. The underscore
  spelling is not a recognized frontmatter key, so the line never did
  anything; the behaviour it was meant to request is the default anyway.

## [0.1.0] - 2026-07-09

### Added
- **First release of the product-management pack**, six skills moved out of
  `multiplai-kit` and adapted to plugin-relative paths:
  - `pm-jtbd-synthesis` — Jobs-to-be-Done from interview transcripts, with
    Forces of Progress and quote-level attribution.
  - `pm-persona-codifier` — raw archetype notes into source-attributed
    persona docs.
  - `pm-pr-faq` — Amazon-style Working Backwards press release plus an
    adversarial internal FAQ.
  - `pm-strategy-memo` — Minto-pyramid leadership memos with a
    fresh-reader test.
  - `job-application` — tailored resume and cover letter, rendered to PDF.
  - `landing-page` — landing-page copy in create, audit and iterate modes.
