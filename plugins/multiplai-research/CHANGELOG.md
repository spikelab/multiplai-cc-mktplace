# Changelog

All notable changes to the **multiplai-research** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-research@<version>`.

Recorded history starts at **0.2.0**; anything earlier is in `git log` only.

Of the 4 versions recorded here, `0.5.0` carries a git tag — the tagging
convention started partway through. Dates on untagged versions are the release
dates recorded at the time, not derived from a tag.

`multiplai-research@0.1.1` predates this file and has no section here.

## [Unreleased]

Nothing yet.

## [0.7.0] - 2026-08-15

### Changed

- **Deep research's mechanical nodes no longer pay extended-thinking latency.**
  The Agent SDK enables extended thinking by default on every call, which costs
  ~15s per call (18.4s → 2.9s measured 2026-08-09 on a cold no-tools call) and
  buys nothing on formatting/parsing work. The `search`, `triage_relevance`,
  `extract` and `verify` nodes — the high-volume per-source work — now pass
  `thinking={"type": "disabled"}`; the reasoning nodes (`plan`, `diverge`,
  `challenge`, `reassess`, `synthesize`, `adversarial`, `quality_check`) keep
  the SDK default. Per node or skill-wide opt-back from `multiplai.conf`,
  beside the existing `MODEL=`/`EFFORT=` keys: `[deep-research.search]
  THINKING=on` or `[deep-research] THINKING=on` (see the skill's "Tuning"
  section); `--thinking on|off` on the CLI overrides every node for one run,
  the way `--effort` does. An unrecognized `THINKING=` value is ignored with a
  warning rather than guessed at, so a typo cannot quietly strip thinking from
  the reasoning nodes. On a `multiplai-core` or `claude-agent-sdk` too old to
  carry the setting, it is dropped with a single warning naming the fix —
  behavior degrades to today's (thinking on), never to an error.

  Worth knowing if you rely on this skill's handling of hostile pages: the four
  nodes that lose extended thinking are also the four that read
  externally-authored text. What guards those calls is unchanged — the
  fail-closed tool deny-list on every SDK call, plus the `<untrusted-content>`
  fencing of page text — and no node's allow-list or prompt changed here. Set
  `[deep-research.extract] THINKING=on` to buy the reasoning budget back on any
  one of them.

## [0.6.5] - 2026-08-06

A review of 0.6.4 found each of its three protections applied to one code path
when two existed. Nothing here is a new idea — it is the same three guards,
extended to the route that was missed, and pinned with tests so the next one
cannot be missed silently.

### Security

- **Deep research no longer fetches internal addresses on any path.** 0.6.4
  blocked URLs resolving to loopback, private, link-local, or cloud-metadata
  addresses — but only on the direct HTTP path. Research runs by default
  through the Claude Agent fetcher instead, which reaches the network through
  WebFetch and was never covered. A search result or a link scraped off a page
  pointing at `http://169.254.169.254/` — the standard way to steal cloud
  credentials — was therefore still fetched on the path you actually use. Both
  paths now apply the same check, before the URL reaches any tool.

- **Read stays open on very large prompts; nothing that can send data does.**
  Prompts above the operating-system argument limit are handed to the agent as
  a file, so the Read tool has to remain available for them. That exception is
  now bounded and visible: every tool that could carry text off the machine —
  shell, network, publishing, messaging, scheduled work — stays blocked, a
  warning is logged whenever the exception applies, and a test fails if a
  future change widens it.

- **The blocked-tool list can no longer fall behind.** It is intentionally a
  copy of the shared library's list rather than an import, so that it still
  holds on an older install. A test now fails when the library learns about a
  tool this copy does not — the drift that previously left several tools
  available.

### Fixed

- **A crash no longer destroys the run in progress or its report.** Only the
  resume checkpoint was written crash-safely. The report itself, the challenge
  review, and the API-quota file were written in place, so an interruption
  could leave a truncated file — losing a finished report, or silently
  resetting quota accounting and re-granting a month of paid API calls. All
  four now write to a temporary file and swap it in atomically: an interrupted
  write leaves the previous version, never a half-written one.

## [0.6.4] - 2026-08-06

Review of 0.6.3 found the deny-list it introduced was stale, and that the one
field with a tool-enabled consequence was the one field it did not cover.

### Security

- **Links harvested from a page are now rejected, not escaped.** A page's
  `links` are model output derived from that page's HTML, and deep-research
  follows them by interpolating each into the fetch prompt — the only prompt in
  the pipeline that runs with a tool enabled. Escaping cannot help there,
  because the URL *is* the argument: text smuggled into it lands inside the
  instruction, after the "fetch this and nothing else" line. Anything that is
  not a plain `http(s)` URL — another scheme, embedded whitespace or newlines,
  control characters, absurd length — is now dropped before anything acts on
  it, with a warning in the log, and the fetcher re-checks independently of its
  caller. The URL fields themselves are also defanged, so an unfetchable link
  is still safe to show you in the bibliography.

- **The deny-list covers the tools that actually exist.** It was written from
  memory and named 18 tools; the CLI ships 44. Missing were `Artifact` (which
  publishes a page to the web) and `SendMessage` (which hands text to another
  agent) — both exfiltration channels as direct as `WebFetch` — plus `REPL`
  next to a carefully denied `Bash`, and the `Task*`/`Cron*`/`Workflow` family,
  which can queue work that gets tools later. It is now derived from the CLI's
  own generated schema list, and the command to re-derive it sits in the source.

- **Defang now holds on assignment, not only on construction.** Field
  validators fire when a model is built; `source.title = raw_page_text` walked
  straight past them. It no longer does. `error` and `extracted_content` are
  covered too — the first quotes failed model output back into a prompt.

### Fixed

- **Checkpoint durability now matches what 0.6.3 claimed.** The rename was
  atomic with respect to *ordering* only: without an `fsync` the data could
  still be in the page cache when the rename committed, so a machine crash
  could leave `--resume` reading a zero-length file — the exact failure the
  change was made to prevent. The temp file and its directory entry are now
  both flushed. The state file also keeps its original permissions instead of
  silently becoming owner-only (`mkstemp` creates `0600`, and the rename
  carried that across).

## [0.6.3] - 2026-08-06

### Security

- **deep-research's LLM calls now deny every tool they don't need.** The
  pipeline runs its calls under `bypassPermissions`, where naming a tool in the
  allow-list *adds* it but never removes the rest — and it passed no deny-list
  at all. Since every one of those prompts carries text fetched from a web
  page, an instruction injected into a page reached a `Bash`, `Read`, `Write`
  and `WebFetch` that were not only available but pre-approved. Each call now
  denies the complement of what it opens: the fetcher gets `WebFetch` and
  nothing else, the search provider `WebSearch` and nothing else, and every
  reasoning call gets no tools at all. Nothing to change in your config — this
  is how these calls were always documented to behave.

- **Page-derived text is defanged where it is stored, not where it is
  printed.** Findings from the default fetch path were extracted inside the
  fetch call, straight off the page, and then interpolated *unfenced* into the
  reassess, synthesize, triage and adversarial-review prompts — so a page could
  close the `<untrusted-content>` fence and promote itself from data to
  instruction. Search-result titles and snippets took the same route from six
  provider parsers. Defang now happens on the models themselves, which covers
  both fetch paths, all six providers, and any prompt added later. Wording is
  untouched: an injection attempt still reaches the extractor intact, so it can
  be reported.

- **The fetch-extract prompt states the data-not-instructions rule and pins the
  URL.** That fetch happens inside the SDK call, so there is no page text in
  hand to wrap in a literal fence; the instruction plus the storage-boundary
  defang above are the mitigation.

### Fixed

- **A crash mid-checkpoint no longer destroys a research run.** State was
  written in place after *every source*, so a crash during the write left
  truncated JSON and `--resume` raised on it — the checkpoint destroying the
  run it exists to protect. Writes now go to a temp file in the same directory
  and are renamed into place atomically.

## [0.6.2] - 2026-08-05

### Security

- **deep-research no longer installs `cryptography` 49.0.0 (CVE-2026-69247,
  high).** If you have deep-research installed, updating to 0.6.2 is the fix —
  the next run resolves `cryptography` 50.0.0. There is nothing to change in
  your own config.

  What went wrong: `skills/deep-research/scripts/` shipped its own `uv.lock`,
  left behind when this repo consolidated onto a single workspace lock. After
  that consolidation nothing could regenerate it — `uv lock` run from inside
  the skill resolves the whole workspace and rewrites the *root* lock, never
  the nested one — so it stayed frozen on the day's versions while the root
  lock moved on. In this repo that was invisible, because in-repo runs use the
  root lock. On *your* machine there is no workspace above the plugin, so
  `uv run --project` found the frozen lock and used it. Dependabot filed the
  advisory correctly and opened patches; those patches edited the nested lock,
  which is the one file that could not be updated.

  The nested lock is now deleted, so an installed deep-research resolves
  against the declared dependency ranges and picks up patched versions on its
  own. A new repo gate (`lint_workspace.py` → nested lockfiles) fails CI if a
  lockfile ever reappears outside the workspace root.

## [0.6.1] - 2026-08-04

### Fixed

- **`deep-research`'s pipeline resolves on an installed copy of the plugin.**
  Its `scripts/pyproject.toml` now declares its own git source for
  `multiplai-core`, so `uv run --project <skill>/scripts` works standalone —
  an install is a copy of the plugin subtree only, with no marketplace
  workspace above it to resolve through.

### Added
- **A plugin README** (`plugins/multiplai-research/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

## [0.6.0] - 2026-08-04

### Changed
- **`deep-research` takes its dependencies from the repo-root uv workspace.**
  Its own `scripts/uv.lock` is gone, replaced by one lock covering every plugin
  in the marketplace. Nothing changes about what the pipeline does; if you
  invoke it by hand, use `uv run --project <repo-root>` rather than running
  from inside `scripts/`, which no longer resolves a project of its own.

  This also removes a whole class of the problem that caused 0.5.2: one plugin
  can no longer sit on a stale lock while its siblings are patched, because
  there is only one lock to keep current.

## [0.5.2] - 2026-07-30

### Fixed

- **deep-research was resolving a `claude-agent-sdk` old enough to fail every
  run against a current Claude CLI.** If you have seen the pipeline die with
  `Claude Code returned an error result: success` *after* a full generation —
  the work done, then thrown away — this is why. The 0.1.x SDK line misparses
  the terminal result message emitted by Claude CLI 2.x, and it is
  deterministic, so the retry wrapper could never rescue it.

  The pipeline reaches the SDK only through `multiplai_core.run_agent()`, and
  `multiplai-core` has long pinned `claude-agent-sdk>=0.2.116,<0.3` in its
  `[sdk]` extra. deep-research never asked for that extra — it listed
  `claude-agent-sdk>=0.1.0` beside core as a dependency of its own, which
  silently undercut core's floor and let the resolver settle on **0.1.56**.

  The dependency is now `multiplai-core[sdk]`, so the floor is stated once, in
  the package that owns it, and cannot drift again. Resolves to 0.2.128. No
  configuration change and no API change on your side.

  This is the only package that moved; the pin on `multiplai-core` itself
  stays at `v0.10.0` deliberately, per the repo's rule that pins are bumped
  per-consumer and only when tested.

## [0.5.1] - 2026-07-27

### Changed
- **`deep-research`'s page-text defanging now comes from `multiplai-core`.**
  `research_pipeline/untrusted.py` stays as the pipeline's seam — `nodes/read.py`
  still imports `defang_untrusted` from it — but the regexes behind it are the
  shared ones. Behaviour is unchanged: fetched pages keep `markdown_fences=False`
  and no injection marking, so a page about shell scripting is not corrupted and
  the extractor still sees an injection attempt in the page's own words, which is
  what it is asked to report. Core pin moves `v0.7.0` → `v0.10.0`
  (`pyproject.toml` + `uv.lock`).

## [0.5.0] - 2026-07-26

### Security
- **Fetched page text is now treated as untrusted input**
  (`research_pipeline/untrusted.py`). Every page this pipeline reads was written
  by someone who is not the user, and it goes into a prompt inside an
  `<untrusted-content>` fence — which is only a boundary as long as the content
  cannot close it. `defang_untrusted()` strips control/ANSI/zero-width/bidi
  characters and HTML-escapes the fence markers, so a page embedding
  `</untrusted-content>` (or a code fence, or a chat-role prefix) cannot promote
  itself from data to instruction. Wording is otherwise untouched — the extractor
  has to see what the page actually said, including an injection attempt it is
  asked to report. Applied in `nodes/read.py`; the instruction half lives in
  `prompts/extract.py` and the `deep-research` / `extract-insights` SKILL.md
  files. Full convention:
  [`docs/untrusted-content.md`](../../docs/untrusted-content.md).

### Added
- **Model × effort as two config axes** (`research_pipeline/config.py`). Per-node
  model tier *and* reasoning effort are both settable from `multiplai.conf` with
  no code edit — `[deep-research]` for the whole pipeline, `[deep-research.<node>]`
  (`parse`, `extract`, …) to override one node. The `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT` ceilings still cap the result, so a budget run forces
  everything down and a conf override cannot escape it.

## [0.4.0] - 2026-07-19

Released without a CHANGELOG entry at the time; see
[#51](https://github.com/spikelab/multiplai-cc-mktplace/pull/51) —
deep-research hardening: verification loop, adversarial review, cost and cache
visibility.

## [0.3.0] - 2026-07-19

extract-insights v2 — coherent argument chain, nuance harvest, readable middle.

### Changed
- **Argument Chain redesigned around a handoff contract.** Every link ends with
  `→ therefore:` naming its conclusion, and the next link must open from it.
  Non-linear arguments (the common case for podcasts) are organized as named
  threads (`**Thread A — <label>:**`, 2–4 links each) closed by a mandatory
  `**Convergence:**` block; forcing parallel material into a fake-linear
  numbered list is named a hard failure. Links are 1–2 full prose sentences
  with the speaker attributed in the sentence — the old `→ enables:` annotation
  format is gone.
- **New Pass 2: Nuance Harvest.** For sources > 500 lines, a windowed
  (~300-line) sweep collects hedged reversals, self-undercutting admissions,
  vivid metaphors/examples, content-bearing asides, and host contributions
  *before* any output section is written; the harvest feeds Tensions & Nuances
  and Emergent Insights and is exempt from the length-budget squeeze. Former
  Pass 2/3 renumbered to 3/4.
- **Two new fidelity checks.** Check 9 (chain linkage: every link's successor
  opens from its `→ therefore:` conclusion, or the link closes a labeled
  thread) and Check 10 (quartile coverage: every quarter of the source by line
  number must contribute at least one anchored item).
- **Readability pass on the middle sections.** Key Claims text is a full
  sentence with the speaker named in-sentence; strength tags stay but leading
  bracket-tag pileups are gone. TL;DR and Most Memorable Line are unchanged.

## [0.2.0] - 2026-07-10

Semantic model tiers for the deep-research pipeline (requires multiplai-core ≥ v0.7.0).

### Changed
- **Per-node model tiers instead of a single dated literal.** Reasoning nodes
  run opus via `pick_model("opus", task="deep-research")`; the high-volume
  per-source parse nodes (`triage_relevance`, `extract`) now run sonnet via
  `pick_model("sonnet", task="deep-research.parse")` — cheaper bulk work without
  touching the reasoning quality. The model family lives in
  `multiplai_core.env.CURRENT_MODEL` (no dated literal to go stale), both tiers
  are capped by the `MULTIPLAI_MODEL` ceiling, and each is retunable per task via
  a `[deep-research]` / `[deep-research.parse]` section in `multiplai.conf`.
  `--model` still overrides every node.
