# pr scripts — orientation

Opens and edits GitHub PRs whose body follows the repo's PR template. Code
reads the template, fills the sections the change's files answer, and checks
the assembled body with gates; one no-tools model call writes the prose for
any other section. The user-facing contract (commands, markers, exit codes)
is `../SKILL.md`.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/pr/scripts python -m pr_pipeline --help
uv run --directory plugins/multiplai-dev/skills/pr/scripts python -m pytest tests/ -q
# CI form
cd plugins/multiplai-dev/skills/pr/scripts && \
  uv run --project ../../../../.. --package pr-pipeline --extra dev python -m pytest tests/ -q
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `open`, `edit`, `render`. Calls `setup_logging` once, then `commands.run`. |
| `commands.py` | `render_body` (template → sources → model when needed → assemble → gates, with one re-ask), `cmd_open`, `cmd_edit`, `cmd_render`. `run` maps `GateFailed` to exit 1 and `Refused` to exit 2, prints `PR_DIAGNOSIS:`, and logs one start and one done/failed line with `log_event` (no body text). `_resolve_repo` reads `pr:` from `<--repo>/specs/config.yaml` (a subproject's, for a monorepo) and returns the git root, which discovery, git and `gh` use. `_adopt` handles a hand-opened PR; `_can_edit` is the rights check. |
| `template.py` | `discover` (config `pr.template`, which warns and discovers when it names no file or one outside the repo; then `.github/`, root, `docs/`, names in any case; the standard template when there is none or several), `load_config` (a malformed value warns and takes its default, as buildme's `config.py` does), and `parse` into `Section`s (own blocks: `comment`, `checkbox`, `blank`, `text`; `subtree_end` for nesting). Headings in fenced code or HTML comments are not headings. |
| `fill.py` | `read_sources` (proposal Why, blocks from `tasks.md` + `.build-state.json` statuses, `docs_impact`, artifact links, ticket, diff size, `--section` files), `plan_sections` (code / model / none per section), `build_prompt` (ticket text fenced with `multiplai_core.untrusted.fence`; link, size and extras never in it), `_run_model` (the seam tests stub: `run_agent`, no tools, one turn, GitHub tokens blanked), `verified_ticks`, `assemble` (appends Ticket, Size, and Blocks/Documentation/Companion artifacts when the template lacks them and the source is not empty: `CODE_TAILS`), `code_keys` (the keys `edit` holds back when the ticket link or `.build-state.json` is missing). |
| `gates.py` | `MAX_WORDS` (2,500, Spike's rule of 2026-10-12, cited at the constant) and `MODEL_TARGET_WORDS` (1,500); heading, checkbox, filled-section and word gates. `open` counts the whole body less comments; `edit` counts only text inside markers. |
| `markers.py` | `<!-- pr:managed v1 -->`, the `written` list, `spans` (refuses nested, unpaired or repeated markers), `merge` (replace owned spans, append never-written tails, never restore a deleted pair). |
| `github.py` | Every `git`/`gh` call. `_run_gh` is the seam tests stub. Push never forces. |

`templates/pull_request_template.md` (one level up from here) is the standard
template. A change to it is a plugin change: version bump and CHANGELOG entry.

## Tests

No test calls `gh` or a model: `tests/conftest.py` makes both seams fail
unless a test stubs them (`gh_stub`, `model_stub`). Git runs for real on
temporary repos (`make_repo`, `make_bare_origin`).
