"""The epic's merge agent: resolves the conflicts a merge of the default branch
into a child's branch left, in the few files it may resolve (item 40.3)."""

MERGE_PROMPT = """\
You finish a merge of `origin/{default}` into the branch `{branch}`, in the git
worktree you are in. The merge stopped with conflicts in these files, and only
these:

{files}

Resolve each file's conflict markers, by these rules only:
- A version field (`"version": ...`, `version = ...`): the branch raised the
  version and `origin/{default}` raised it too. Write the next version after
  the one `origin/{default}` has, raising the same part the branch raised.
  Where the same version appears in another listed file, write the same value.
- A CHANGELOG: keep both entries. The branch's entry goes above the one from
  `origin/{default}`, under the version you wrote.
- A lockfile (`uv.lock`, `package-lock.json`, ...): regenerate it with the
  project's own tool (`uv lock`, `npm install --package-lock-only`, ...)
  instead of editing it by hand.

Change no other file. Do not stage, commit, abort the merge, reset, check
out or push: the caller checks the files and commits the merge itself. End
your answer with one line per file: `RESOLVED: <file>`.
"""
