"""`setup` and `config-get`: write or read specs/config.yaml without a human.

`/buildme setup` asks the questions in the wrapper session and calls
``python -m build_pipeline setup`` with one flag per answer. This module edits
``specs/config.yaml`` with ruamel.yaml in round-trip mode, so the comments and
key order a person wrote survive: a flag that is passed sets its key (adding
it, or changing an existing value), a flag that is not passed leaves its key
alone, and no key is ever removed.

ruamel is used here only. ``BuildConfig._load_specs_config`` and the bootstrap
stub writer keep PyYAML.

stdout markers:
  ADDED: <key> = <value>          one per key the run added
  CHANGED: <key>: <old> → <new>   one per key whose value changed
  SETUP:WROTE:<path>
  SETUP:COMMIT_REQUIRED:<path>    the file is not committed; a build's worktree
                                  branches from HEAD and would not see it
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _setup_values(args: argparse.Namespace) -> list[tuple[tuple[str, ...], object]]:
    """``(key path, value)`` for every flag that was passed, in a stable order."""
    values: list[tuple[tuple[str, ...], object]] = []
    if args.architecture is not None:
        values.append((("architecture",), args.architecture))
    if args.pr_template is not None:
        values.append((("pr", "template"), args.pr_template))
    if args.findings_authors is not None:
        raw = args.findings_authors.strip()
        if raw == "collaborators":
            authors: object = "collaborators"
        else:
            authors = [a.strip() for a in raw.split(",") if a.strip()]
        values.append((("findings", "authors"), authors))
    return values


def _plain(value):
    """A ruamel node as plain Python, for comparing and printing."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def apply_setup(text: str, values: list[tuple[tuple[str, ...], object]]):
    """Apply *values* to the YAML *text*; returns ``(new_text, added, changed)``.

    ``added`` is ``[(dotted_key, value)]``; ``changed`` is
    ``[(dotted_key, old, new)]``. A value equal to what is there already is
    neither. Pure apart from parsing; the caller does the I/O.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    data = yaml.load(text) if text.strip() else None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise ValueError("specs/config.yaml does not hold a mapping at the top level")

    added: list[tuple[str, object]] = []
    changed: list[tuple[str, object, object]] = []
    for path, value in values:
        node = data
        for depth, part in enumerate(path[:-1], 1):
            child = node.get(part)
            if not isinstance(child, dict):
                if child is not None:  # a scalar where a mapping belongs
                    changed.append((".".join(path[:depth]), _plain(child), {}))
                child = CommentedMap()
                node[part] = child
            node = child
        leaf = path[-1]
        dotted = ".".join(path)
        if leaf not in node:
            node[leaf] = value
            added.append((dotted, value))
        elif _plain(node[leaf]) != value:
            changed.append((dotted, _plain(node[leaf]), value))
            node[leaf] = value

    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue(), added, changed


def run_setup(args: argparse.Namespace) -> int:
    from .intake import config_commit_state

    project = Path(args.project_dir)
    path = project / "specs" / "config.yaml"
    existing = path.read_text() if path.exists() else ""
    values = _setup_values(args)
    if not existing and not values:
        values = [(("schema",), "spec-driven")]
    try:
        text, added, changed = apply_setup(existing, values)
    except Exception as e:  # unparseable YAML: never overwrite a file we cannot read
        print(f"ERROR: cannot update {path}: {e}", file=sys.stderr)
        return 1

    if text != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for key, value in added:
        print(f"ADDED: {key} = {value}")
    for key, old, new in changed:
        print(f"CHANGED: {key}: {old} → {new}")
    if not added and not changed:
        print("UNCHANGED: every passed value was already set")
    print(f"SETUP:WROTE:{path}", flush=True)
    if config_commit_state(project) != "committed":
        print(f"SETUP:COMMIT_REQUIRED:{path}", flush=True)
    return 0


def config_get(args: argparse.Namespace) -> int:
    """Print one *resolved* value: what the pipeline would use, defaults applied."""
    from .config import BuildConfig

    project = Path(args.project_dir)
    config = BuildConfig(project_dir=project)
    config.specs_dir = project / "specs"
    config._load_specs_config()
    resolved = {
        "architecture": config.architecture,
        "pr.template": config.pr.template,
        "pr.max_change_lines": config.pr.max_change_lines,
        "findings.authors": (config.findings.authors
                             if isinstance(config.findings.authors, str)
                             else ",".join(config.findings.authors)),
        "findings.max_rounds": config.findings.max_rounds,
        "mutation.enabled": str(config.mutation.enabled).lower(),
        "mutation.min_score": config.mutation.min_score,
        "git.worktree": str(config.git.worktree).lower(),
        "git.push": str(config.git.push).lower(),
        "git.pr": config.git.pr,
    }
    if args.key not in resolved:
        print(f"ERROR: unknown key {args.key!r}; known: {', '.join(sorted(resolved))}",
              file=sys.stderr)
        return 2
    print(resolved[args.key])
    return 0
