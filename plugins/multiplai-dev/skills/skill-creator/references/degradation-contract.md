# Degradation contract

Every skill in this marketplace must work on **vanilla Claude Code** — no
multiplai kit, no container, no host bridge — or degrade with a clear, honest
message. This document canonizes the patterns that make that true. New skills
are reviewed against it (skill-creator references this file); changes to
existing skills must not regress it.

## The two rules

**1. The error-message rule.** When a capability is missing, name the *actual*
missing capability and the *vanilla* fix. Never mention the kit, the container,
or the SSH bridge to a user who doesn't have one — "set SSH_BUILD_USER in your
kit root .env" is gibberish on a vanilla Mac. Bridge instructions are only
appropriate after the environment has been positively identified as the
multiplai container (rule 2).

Good (plain Linux, transcribe): `this skill transcribes with mlx-whisper,
which requires Apple Silicon macOS. On Linux, consider whisper.cpp or
faster-whisper instead.`
Bad: `Error: no SSH user for the container→host bridge.`

**2. The container-detection rule.** "Am I in the multiplai container?" is
answered by the explicit env flag, never inferred from `uname`:

```sh
IS_CONTAINER=false
case "${MULTIPLAI_CONTAINER:-}" in
  1) IS_CONTAINER=true ;;                        # multiplai container image sets this
  0) ;;                                          # explicit override: NOT a container
  *) [ -f /.dockerenv ] && IS_CONTAINER=true ;;  # generic-Docker fallback (older images)
esac
```

The multiplai container image exports `MULTIPLAI_CONTAINER=1` (see
multiplai-container's Dockerfile). `uname != Darwin` means "not a Mac" —
it does NOT mean "the container", and treating it that way sends plain-Linux
users chasing an SSH bridge that doesn't exist.

## The four canonical degradation patterns

These are proven in the codebase — copy them, don't reinvent.

### 1. Free path first → opt-in expensive fallback → clear exit codes
*(exemplar: `multiplai-media:youtube-transcript`)*

Try the zero-cost/zero-dependency path first (subtitle download). Fall back to
the expensive path (audio download + transcription) only behind an explicit
opt-in flag (`--audio-fallback`), and tell the user what the fallback requires.
Distinguish outcomes with exit codes so the orchestrating Claude can react
(e.g. `2` = "no subtitles; re-run with --audio-fallback").

### 2. Zero-config default → optional API keys
*(exemplar: `multiplai-research:deep-research`)*

Default to what every Claude Code install already has (the Agent SDK / `claude`
CLI); treat API keys as optional upgrades, not prerequisites. A fresh install
must produce useful output with no configuration at all.

### 3. Memory files: load if present, silently skip, never block
*(exemplars: `multiplai-writing:writing`, `multiplai-pm:landing-page`,
`multiplai-pm:job-application`)*

Personal memory files (voice guides, career history) will not exist on vanilla
installs. Load them only if present, skip silently otherwise, and never make
them a hard requirement. When the missing file is *source material* the task
cannot proceed without (career history, brand voice), ask the user for it
directly — and still never fabricate beyond what they provide.

### 4. Cross-plugin dependency: sibling absent → ask the user
*(exemplar: the `multiplai-pm` skills' use of `interviewer`/`extract-insights`)*

When a skill composes with a sibling plugin's skill, check whether it's
installed; if not, do its job the direct way (usually: ask the user the
questions yourself). Never hard-fail on a missing sibling, and never silently
skip the step.

## Output-location rule

Never write to `INBOX/`, `PLANS/`, or any workspace directory *relative to the
session cwd*, and never create those directories on machines that don't have
them. The pattern: **workspace `INBOX/` if it exists (resolve the workspace
root from `$CLAUDE_CONFIG_DIR/.workspace`), else the current directory — and
always tell the user where the file landed.**

## Review checklist for new skills

- [ ] Works (or degrades with a clear message) with no kit, no container, no
      bridge, no `$WORKSPACE`, no memory files, no API keys.
- [ ] Every error names the missing capability + the vanilla fix (rule 1).
- [ ] Container detection uses `MULTIPLAI_CONTAINER` / `/.dockerenv` (rule 2),
      never `uname`.
- [ ] Hard platform requirements (e.g. Apple-Silicon-only tooling) are
      disclosed in SKILL.md up front.
- [ ] Output paths follow the output-location rule.
- [ ] Where the plugin has a `tests/` directory, behavioral degradation paths
      have tripwire tests (run the real script with a masked PATH / scratch
      HOME; see `plugins/multiplai-media/tests/test_platform_detection.py`).

---

*This is a vendored copy of `docs/degradation-contract.md` from the
marketplace repo (https://github.com/spikelab/multiplai-cc-mktplace) so it
resolves for installed plugins. The repo copy is canonical — sync this file
when it changes.*
