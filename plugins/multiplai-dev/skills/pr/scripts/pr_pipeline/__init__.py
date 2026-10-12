"""pr_pipeline: open and edit GitHub PRs whose body follows the repo's PR template.

Code reads the template, fills the sections the change's files answer, checks
the assembled body with gates, and writes it with `gh`. A model writes prose
only for template sections no file answers. See `../CLAUDE.md`.
"""
