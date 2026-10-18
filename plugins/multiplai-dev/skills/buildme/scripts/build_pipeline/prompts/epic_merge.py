"""The epic's merge agent: resolves the conflicts a merge of the default branch
into a child's branch left, in the few files it may resolve (item 40.3). The
rules here are the ones `epic/merge.py`'s `not_resolutions` checks."""

MERGE_PROMPT = """\
You finish a merge of `origin/{default}` (`MERGE_HEAD`) into the branch
`{branch}`, in the git worktree you are in. Resolve these files, and only
these:

{files}

Some hold conflict markers. A version file without markers is one where the
branch and `origin/{default}` raised the version to the same value: it still
needs the next one. Resolve by these rules only:
- The package's own version field (`"version": ...`, `version = ...`) that
  both the branch and `origin/{default}` raised: write the next version
  after the one `origin/{default}` has, raising the same part the branch
  raised (from 0.64.0, a branch at 0.65.0 and `origin/{default}` at 0.65.0
  give 0.66.0; `origin/{default}` at 0.64.1 gives 0.65.0). The package's
  own field is the top-level `version`: in TOML or INI, outside any table
  or under `[project]`, `[package]`, `[tool.poetry]`, `[workspace.package]`
  or `[metadata]`; in JSON, at the top level, or a marketplace manifest's
  `metadata.version` or `plugins[].version`; in pubspec.yaml, Chart.yaml,
  package.yaml, galaxy.yml or galaxy.yaml, and in a file that is not TOML,
  INI, JSON or YAML, unindented. Write it only in the version fields both sides raised
  that are the package's own.
  Leave every other line as the merge left it, even where it holds the old
  version or both sides raised it alike: a dependency's or a tool's version
  (`[dependencies.<name>]`, `[tool.commitizen]`, a devcontainer feature's),
  another package's version field that only one side or neither side
  changed, a description, a line of `origin/{default}`'s CHANGELOG. Change
  nothing else in the file.
- A CHANGELOG: keep both entries in full. Keep every line of
  `origin/{default}`'s copy (`git show MERGE_HEAD:<file>`), in its order, and
  put every line the branch added above its entry, in their order, under a
  heading for the version you wrote, even where both entries had the same
  heading or share lines. In the branch's lines, write the version you wrote
  wherever the branch's own old version appears, and change nothing else.
- A lockfile (`uv.lock`, `package-lock.json`, ...): regenerate it. Replace it
  with `origin/{default}`'s copy (`git show MERGE_HEAD:<file> > <file>`), then
  run the project's own tool on it (`uv lock`, `npm install
  --package-lock-only`, ...), never one that upgrades other packages. Never
  edit it by hand.

Change no other file. Do not stage, commit, abort the merge, reset, check
out or push: the caller checks the files and commits the merge itself. End
your answer with one line per file: `RESOLVED: <file>`.
"""
