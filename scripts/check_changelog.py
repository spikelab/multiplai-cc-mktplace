#!/usr/bin/env python3
"""Release-notes gate: a changed plugin must carry a version bump and notes.

The versioning convention has been documented since the marketplace opened —
bump the plugin's version in `.claude-plugin/marketplace.json`, tag
`<plugin>@<version>` — but nothing checked it. CI checked structure, security
and tests; none of those notice that a plugin's behaviour changed under an
unchanged version number. When that happens the consequence lands on the user:
Claude Code's `/plugin` menu offers an update by version number, so a silent
change either ships invisibly or does not ship at all, and either way there is
nothing for the user to read.

So, for each plugin whose files a pull request touches, this asks for two
things:

    1. its version in `.claude-plugin/marketplace.json` differs from the base
    2. `plugins/<name>/CHANGELOG.md` is modified in the same diff

Two exemptions, because a gate that fires on changes it cannot possibly be
about is a gate people learn to bypass:

  * **Docs-only.** If everything changed under `plugins/<name>/` is a
    `README.md` or the `CHANGELOG.md` itself, there is no behaviour to
    describe and no version to bump.
  * **An explicit opt-out**, for the genuine exception: a `no-changelog` label
    on the PR, or a `[skip changelog]` line in its body. Deliberate, visible
    in review, and recorded on the PR.

Like the sibling gates this is stdlib-only and offline: `git` plus JSON. It
runs on `pull_request` only — gating pushes to `main` would make an in-flight
branch unpushable.

Exit codes:
    0 — clean (or exempt)
    1 — one or more plugins changed without a bump and notes
    2 — the gate itself could not run (bad ref, unreadable manifest)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

MANIFEST = ".claude-plugin/marketplace.json"

# Files under a plugin that describe it rather than do anything. Changing only
# these cannot change what the plugin does on an installing user's machine.
DOCS_ONLY_NAMES = {"README.md", "CHANGELOG.md"}

SKIP_LABEL = "no-changelog"
SKIP_MARKER = "[skip changelog]"


class GateError(RuntimeError):
    """The gate could not evaluate — distinct from the gate failing."""


@dataclass
class Findings:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def versions(manifest_text: str) -> dict[str, str]:
    """Plugin name -> version, from a marketplace.json body."""
    try:
        data = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{MANIFEST} is not valid JSON: {exc}") from exc
    out = {}
    for entry in data.get("plugins", []):
        name = entry.get("name")
        if name:
            out[name] = entry.get("version")
    return out


def plugin_of(path: str) -> tuple[str, str] | None:
    """`plugins/<name>/<rest>` -> (name, rest). Anything else -> None."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "plugins":
        return parts[1], "/".join(parts[2:])
    return None


def check(
    changed_files: list[str],
    base_versions: dict[str, str],
    head_versions: dict[str, str],
    *,
    labels: list[str] | None = None,
    pr_body: str = "",
) -> Findings:
    """The whole gate, as a pure function over an already-computed diff."""
    f = Findings()

    labels = labels or []
    if SKIP_LABEL in labels:
        f.notes.append(f"exempt: the `{SKIP_LABEL}` label is on this PR")
        return f
    if SKIP_MARKER in pr_body.lower():
        f.notes.append(f"exempt: `{SKIP_MARKER}` in the PR body")
        return f

    # Bucket the diff by plugin, keeping the paths so the message can name them.
    touched: dict[str, list[str]] = {}
    for path in changed_files:
        hit = plugin_of(path)
        if hit:
            touched.setdefault(hit[0], []).append(hit[1])

    if not touched:
        f.notes.append("no plugin files changed")
        return f

    for name in sorted(touched):
        rel = touched[name]
        if all(Path(r).name in DOCS_ONLY_NAMES for r in rel):
            f.notes.append(f"{name}: docs-only change, not gated")
            continue

        # A plugin deleted wholesale has nothing left to document.
        if name not in head_versions:
            f.notes.append(f"{name}: no longer in {MANIFEST}, not gated")
            continue

        old, new = base_versions.get(name), head_versions.get(name)
        bumped = old != new
        noted = f"plugins/{name}/CHANGELOG.md" in changed_files

        if bumped and noted:
            f.notes.append(f"{name}: {old or '(new)'} -> {new}, notes updated")
            continue

        behaviour = [r for r in rel if Path(r).name not in DOCS_ONLY_NAMES]
        f.failures.append(_message(name, old, new, bumped, noted, behaviour))

    return f


def _message(
    name: str,
    old: str | None,
    new: str | None,
    bumped: bool,
    noted: bool,
    behaviour: list[str],
) -> str:
    sample = ", ".join(behaviour[:3])
    if len(behaviour) > 3:
        sample += f" (+{len(behaviour) - 3} more)"
    lines = [
        f"{name}: changed ({sample}) but not released.",
    ]
    if not bumped:
        lines.append(
            f"    - bump \"version\" for \"{name}\" in {MANIFEST} "
            f"(currently \"{new}\", same as the base branch)")
    if not noted:
        lines.append(
            f"    - add an entry to plugins/{name}/CHANGELOG.md under a new "
            f"`## [<version>] - <YYYY-MM-DD>` heading")
    if bumped and not noted:
        lines.append(
            f"      (the version moved {old} -> {new}; the notes have to move "
            f"with it)")
    lines.append(
        f"    - or, if this genuinely needs no notes: add the `{SKIP_LABEL}` "
        f"label, or `{SKIP_MARKER}` in the PR body")
    return "\n".join(lines)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def collect(repo: Path, base: str, head: str = "HEAD") -> tuple[
        list[str], dict[str, str], dict[str, str]]:
    """Diff and both manifests, read out of git rather than the worktree.

    `base...head` (three dots) is deliberate: it diffs against the merge base,
    so commits that landed on the base branch after this one forked do not show
    up as changes this PR made.
    """
    changed = [ln for ln in
               _git(repo, "diff", "--name-only", f"{base}...{head}").splitlines()
               if ln]
    base_manifest = _git(repo, "show", f"{base}:{MANIFEST}")
    head_manifest = _git(repo, "show", f"{head}:{MANIFEST}")
    return changed, versions(base_manifest), versions(head_manifest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--repo", type=Path, default=REPO_ROOT,
                    help="marketplace repo root (default: this script's repo)")
    ap.add_argument("--base", default="origin/main",
                    help="base ref to compare against (default: origin/main)")
    ap.add_argument("--head", default="HEAD", help="head ref (default: HEAD)")
    ap.add_argument("--labels", default=os.environ.get("PR_LABELS", ""),
                    help="comma-separated PR labels (default: $PR_LABELS)")
    ap.add_argument("--pr-body", default=os.environ.get("PR_BODY", ""),
                    help="PR body text (default: $PR_BODY)")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args(argv)

    try:
        changed, base_v, head_v = collect(
            args.repo.resolve(), args.base, args.head)
        f = check(changed, base_v, head_v,
                  labels=[s.strip() for s in args.labels.split(",") if s.strip()],
                  pr_body=args.pr_body)
    except GateError as exc:
        print(f"check_changelog: cannot run: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        for n in f.notes:
            print(f"ok:    {n}")
    for msg in f.failures:
        print(f"ERROR: {msg}")

    if f.ok:
        if not args.quiet:
            print("check_changelog: clean")
        return 0
    print(f"\ncheck_changelog: {len(f.failures)} plugin(s) changed without "
          f"release notes")
    return 1


if __name__ == "__main__":
    sys.exit(main())
