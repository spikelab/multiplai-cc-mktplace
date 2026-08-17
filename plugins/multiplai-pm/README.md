# multiplai-pm

Product-management skill pack for Claude Code: **JTBD synthesis, persona
codification, PR/FAQ, strategy memos, job applications, landing pages, and
Plane ticket management**. Part of the [`multiplai`](../../README.md)
marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-pm@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `pm-jtbd-synthesis` | Synthesize Jobs-to-be-Done from customer interview transcripts — Forces of Progress with verbatim quote attribution, job clusters, job stories, Opportunity-Solution Tree stub. |
| `pm-persona-codifier` | Codify customer/user personas into canonical, source-attributed persona docs — one file per persona plus an INDEX. |
| `pm-pr-faq` | Draft Amazon-style Working Backwards documents — fictional press release + internal FAQ, with an adversarial FAQ generator and stress-test pass. |
| `pm-strategy-memo` | Leadership-grade strategy memos using Minto Pyramid, with a Working Backwards stress-test and a fresh-Claude reader-test. |
| `job-application` | Draft tailored resumes and cover letters, then generate PDF applications. |
| `landing-page` | Landing-page copy: create from scratch, audit an existing page (CRO review), or iterate copy variations for specific sections. |
| `plane` | Work with [Plane](https://plane.so) tickets — list your board, read/create/update issues, comment, search — restricted to an explicit project allowlist: writes outside it are refused, reads and search results are filtered to it (two workspace-scoped commands are disclosed in its SKILL.md). |

## Composition

The discovery skills form a chain, each output the next skill's strongest input:

```
transcribe (multiplai-media) → pm-jtbd-synthesis → pm-persona-codifier → pm-pr-faq
```

- `pm-strategy-memo` composes downstream of `interviewer` / `extract-insights`
  (multiplai-research) and of `pm-jtbd-synthesis` / `pm-persona-codifier` when
  the memo references discovery evidence; `pm-pr-faq` composes downstream of
  `pm-strategy-memo` when strategy implies a launch.
- `landing-page` uses `interviewer` (multiplai-research) for discovery when
  available.

## Configuration — `plane`

The `plane` skill is the pack's only skill that needs credentials. It talks to
the Plane API with a personal access token, and it refuses to run without an
explicit project allowlist — a Plane token reaches *every* project in the
workspace, so defaulting to "everything the token can reach" is exactly the
failure the skill exists to prevent. All configuration comes from the
environment:

| Variable | Required | Meaning |
|----------|:--------:|---------|
| `PLANE_API_TOKEN` | yes | Personal access token. Plane: *Profile settings → Personal access tokens*. |
| `PLANE_WORKSPACE` | yes | Workspace slug — the segment after the host in your Plane URL. |
| `PLANE_ALLOWED_PROJECTS` | yes | Comma-separated project UUIDs, each with an optional `:label` — `<uuid>[:label],...`. Quote the whole value; no commas inside a label; use the UUID from the project URL (`https://app.plane.so/<workspace>/projects/<uuid>/issues`), not the URL itself. |
| `PLANE_BASE_URL` | no | Defaults to `https://api.plane.so`. Set to your own host if self-hosted. |
| `PLANE_ENV_FILE` | no | Path to a `KEY=VALUE` file to read the above from when they are absent from the environment. Only `PLANE_*` keys are read, and the real environment always wins. |

**Verify before you trust it:**

```bash
python3 skills/plane/scripts/plane.py check
```

`check` prints the resolved config, lists every project the token can see
marked `ALLOWED` / `BLOCKED`, and self-tests the guardrail against both
negative and positive cases. Details and the full command set are in
[`skills/plane/SKILL.md`](skills/plane/SKILL.md).

## Compatibility

All skills run on vanilla Claude Code, any OS. Personal memory files are
optional — skills ask for source material when absent. See the
[compatibility matrix](../../README.md#compatibility-matrix).
