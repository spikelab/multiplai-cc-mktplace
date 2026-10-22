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

## [0.8.0] - 2026-10-22

### Changed
- pm-spec now puts a change's specs on a branch of their own and pushes it.
  The branch is `change/<name>`, since a change can be a fix as well as a
  feature; `--branch <name>` (on every command) names another, for example
  `--branch fix/login-timeout`. pm-spec creates the branch from origin's
  default branch when origin does not have it, writes in its own worktree
  under `$WORKSPACE/.worktrees/` (`pm-spec-<branch>`), commits there and runs
  `git push -u origin <branch>`, so your checkout and its branch are left as
  they were. It opens no pull request and talks to no ticket system. After
  `SPEC:READY` it prints `SPEC:BRANCH:<branch>`. An epic's children go on the
  parent's branch.
- A repository with no `origin` remote is refused: the specs reach buildme
  through the branch on origin.
- A second run reuses the worktree and fast-forwards it to origin's branch;
  one that has diverged from origin is refused, naming both commits.
- A push that fails keeps the commit, exits `1` and says so; `pm-spec resume
  <name>` then retries only the push.
- `pm-spec revise <name> --project-dir <build worktree>`, the line buildme
  prints, commits the revision on the build's branch and pushes nothing: it
  reaches the feature branch through that build's pull request.

### Migration
- Specs a pm-spec before 0.8.0 handed off (ready and committed on your
  branch, with no feature branch) are refused by buildme with
  `Run /multiplai-pm:pm-spec resume <name> first`. That resume copies the
  change dir (and an epic's children) onto `change/<name>`, commits it as
  `specs(<name>): moved to change/<name>` and pushes it; your copy stays
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
