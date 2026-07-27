# Contributing

Thanks for helping. [`CLAUDE.md`](CLAUDE.md) is the orientation for anyone —
human or agent — changing this repo: where things live, the consequence that
shapes everything (installed plugins run on strangers' machines with their
credentials), and the conventions below in contributor-facing detail.

Questions and ideas go to [GitHub Discussions on the umbrella
repo](https://github.com/spikelab/multiplai/discussions); bugs to
[issues here](https://github.com/spikelab/multiplai-cc-mktplace/issues)
(templates provided).

## Pre-publish checks

Installing a plugin copies these files onto someone else's machine, where they
run with that person's credentials — and nobody reads every line first. Two
repo-level checks stand in for that reading; both are deterministic and offline:

```bash
uv run --no-project scripts/lint_skills.py     # structure: frontmatter, script refs, absolute paths
uv run --no-project scripts/scan_skills.py     # security: what a skill does vs what its SKILL.md says
```

- `lint_skills.py` catches malformed frontmatter, unknown `model`/`effort`
  values (silently ignored at runtime, so the skill quietly runs on the wrong
  tier), SKILL.md references to renamed scripts, and machine-specific absolute
  paths baked into shipped files. Exit 1 on any error.
- `scan_skills.py` reports declared-vs-actual behaviour. **FAIL** (blocks CI) for
  patterns with no legitimate use in a shipped skill — `curl | bash`,
  base64-decode-and-execute; **WARN** for behaviour that is fine when declared
  and suspicious when not — network calls or credential reads absent from the
  SKILL.md. It is a static scan, not a sandbox: it raises the cost of hiding
  behaviour, it does not make hiding impossible.

Individual skills can additionally ship a `CONTRACT.md` (assertions on interface
*shape*, not on values) run by
`plugins/multiplai-dev/skills/skill-creator/scripts/promote_skill.py --contract`.

To report something you find, or to understand what the scanner does *not*
cover, see [`SECURITY.md`](SECURITY.md).

## Changelog gate & versioning

Every version bump in `.claude-plugin/marketplace.json` gets a
matching annotated git tag `<plugin>@<version>` (e.g. `multiplai-context@0.6.4`)
pointing at the commit where that version lands on `main`, **and** an entry in
`plugins/<plugin>/CHANGELOG.md`. The `changelog-gate` CI job enforces the bump
and the entry on every pull request
([`scripts/check_changelog.py`](scripts/check_changelog.py), runnable locally
as `uv run --no-project scripts/check_changelog.py --base origin/main`);
the tag is cut by hand at release. Full detail, including the gate's escape
hatch for genuinely docs-only changes, is in
[`CLAUDE.md`](CLAUDE.md#release-convention).

## Tests

CI runs the gate scripts' own self-tests plus three per-plugin suites — the
commands are listed in [`CLAUDE.md`](CLAUDE.md#gates-before-publishing).

## Counting skills

Docs say "seven plugin packs, 40+ skills" on purpose — the number grows.
When an exact count is needed, derive it, never quote from memory:

```bash
ls -d plugins/*/skills/*/ | wc -l
```
