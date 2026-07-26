#!/usr/bin/env python3
"""Structural lint for the marketplace tree.

This repo is *published* — installing a plugin copies these files onto someone
else's machine. Until now nothing checked the tree before that happened, so a
malformed frontmatter, a SKILL.md pointing at a script that was renamed, or an
absolute `/Users/spike/...` path baked into a shipped file would all ship
silently and fail on the installing side, where nobody can debug it.

Everything here is deterministic and offline: parse, resolve, compare. No LLM,
no network. The security-flavoured checks live in `scan_skills.py`.

Exit codes:
    0 — clean
    1 — one or more errors
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Frontmatter values Claude Code actually accepts. A typo here ("sonnet-4",
# "high-effort") is silently ignored at runtime, so the skill quietly runs on
# the wrong tier — exactly the failure a lint should catch.
KNOWN_MODELS = {"opus", "sonnet", "haiku", "inherit", "fable"}
KNOWN_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# An absolute path under someone's macOS home directory. Shipping one means the
# installing user gets a path that doesn't resolve, or one that resolves to
# something of theirs.
#
# Deliberately NOT flagged: `/home/agent/...`. That is the container's own home
# — every multiplai container runs as the `agent` user — so it is correct and
# portable, and the host-bridge skills use it as a legitimate fallback after
# checking $SSH_BUILD_KEY and $HOME. Flagging it produced 15 false positives on
# the first run and would have trained everyone to ignore this check.
HOST_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+/")

# Docs describe the author's own layout by necessity, and tests use host paths
# as synthetic fixture strings — neither is a runtime file that an installing
# user executes.
HOST_PATH_EXEMPT_NAMES = {"README.md", "CHANGELOG.md"}
HOST_PATH_EXEMPT_DIRS = {"tests", "test", "references", "docs"}

# `${CLAUDE_PLUGIN_ROOT}/...` references inside a SKILL.md. Trailing
# punctuation and closing backticks/quotes are stripped by the caller.
PLUGIN_ROOT_REF_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[^\s`\"'\)\],;]*)")


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, where: Path | str, msg: str) -> None:
        self.errors.append(f"{_rel(where)}: {msg}")

    def warn(self, where: Path | str, msg: str) -> None:
        self.warnings.append(f"{_rel(where)}: {msg}")

    @property
    def ok(self) -> bool:
        return not self.errors


def _rel(p: Path | str) -> str:
    try:
        return str(Path(p).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return str(p)


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #

def parse_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """Return (fields, error). A minimal YAML-subset parser.

    Deliberately not PyYAML: frontmatter here is flat `key: value` pairs, and
    depending on a third-party parser in a CI gate that must run anywhere is a
    worse trade than handling the subset we actually use. Values may be quoted;
    everything is returned as a string.
    """
    if not text.startswith("---"):
        return None, "no frontmatter block (file must start with '---')"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "frontmatter block is never closed"

    fields: dict[str, str] = {}
    last_key: str | None = None
    for raw in text[3:end].splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # A continuation of a multi-line value (indented, no `key:` of its own).
        if line[:1].isspace() and last_key:
            fields[last_key] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return None, f"unparseable frontmatter line: {line!r}"
        key = key.strip()
        value = value.strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        if quoted:
            value = value[1:-1]
        elif ": " in value:
            # Claude Code parses frontmatter as real YAML, where an unquoted
            # value containing ": " starts a nested mapping and the whole block
            # fails to load. This parser is lenient enough not to notice, so
            # the hazard is called out explicitly rather than papered over.
            # Found in the wild: backfill's "Default window: last 7 days".
            return None, (
                f"value for '{key}' contains ': ' but is unquoted — YAML reads "
                f"this as a nested mapping and the frontmatter fails to parse; "
                f"wrap the value in double quotes"
            )
        fields[key] = value
        last_key = key
    return fields, ""


def check_frontmatter(skill_md: Path, f: Findings) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fields, err = parse_frontmatter(text)
    if fields is None:
        f.error(skill_md, err)
        return {}

    for required in ("name", "description"):
        if not fields.get(required):
            f.error(skill_md, f"frontmatter missing required field '{required}'")

    # The directory name is how the skill is invoked; a mismatched `name:`
    # means /slash-command and the catalog disagree.
    dir_name = skill_md.parent.name
    if fields.get("name") and fields["name"] != dir_name:
        f.error(skill_md,
                f"frontmatter name '{fields['name']}' != directory '{dir_name}'")

    model = fields.get("model")
    if model is not None and model not in KNOWN_MODELS:
        f.error(skill_md,
                f"unknown model '{model}' (known: {', '.join(sorted(KNOWN_MODELS))})")

    effort = fields.get("effort")
    if effort is not None and effort not in KNOWN_EFFORTS:
        f.error(skill_md,
                f"unknown effort '{effort}' (known: {', '.join(sorted(KNOWN_EFFORTS))})")

    return fields


# --------------------------------------------------------------------------- #
# Script references
# --------------------------------------------------------------------------- #

def check_script_refs(skill_md: Path, plugin_dir: Path, f: Findings) -> None:
    """Every `${CLAUDE_PLUGIN_ROOT}/...` path in a SKILL.md must resolve.

    `${CLAUDE_PLUGIN_ROOT}` expands to the plugin directory at runtime, so the
    reference is checkable statically. A stale one is invisible until someone
    invokes the skill and the command dies with "No such file or directory".
    """
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    for match in PLUGIN_ROOT_REF_RE.finditer(text):
        ref = match.group(1).rstrip("/.,:;")
        if not ref or any(c in ref for c in "*<>"):
            continue  # a glob or a placeholder like /skills/<name>/
        target = plugin_dir / ref.lstrip("/")
        if not target.exists():
            f.error(skill_md, f"broken script reference: ${{CLAUDE_PLUGIN_ROOT}}{ref}")


# --------------------------------------------------------------------------- #
# Host paths
# --------------------------------------------------------------------------- #

def check_host_paths(path: Path, f: Findings) -> None:
    if path.name in HOST_PATH_EXEMPT_NAMES:
        return
    if HOST_PATH_EXEMPT_DIRS.intersection(path.parts):
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable — not this check's business
    for lineno, line in enumerate(text.splitlines(), 1):
        if HOST_PATH_RE.search(line):
            f.error(path, f"line {lineno}: absolute host path in a shipped file")


# --------------------------------------------------------------------------- #
# marketplace.json
# --------------------------------------------------------------------------- #

def check_marketplace(repo: Path, f: Findings) -> None:
    manifest = repo / ".claude-plugin" / "marketplace.json"
    if not manifest.exists():
        f.error(manifest, "marketplace manifest is missing")
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        f.error(manifest, f"invalid JSON: {exc}")
        return

    listed = {str(p.get("name") or "") for p in data.get("plugins", [])
              if isinstance(p, dict)} - {""}
    present = {d.name for d in (repo / "plugins").iterdir()
               if d.is_dir() and not d.name.startswith(".")}

    for missing in sorted(present - listed):
        f.error(manifest, f"plugin directory '{missing}' exists but is not listed")
    for phantom in sorted(listed - present):
        f.error(manifest, f"lists plugin '{phantom}' with no directory under plugins/")

    # A `source` that doesn't resolve installs nothing, with no error until the
    # user tries to use the plugin.
    for entry in data.get("plugins", []):
        if not isinstance(entry, dict):
            continue
        source = entry.get("source", "")
        if source.startswith("./") and not (repo / source[2:]).is_dir():
            f.error(manifest, f"plugin '{entry.get('name')}' source does not resolve: {source}")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def lint(repo: Path) -> Findings:
    f = Findings()
    plugins_dir = repo / "plugins"
    if not plugins_dir.is_dir():
        f.error(repo, "no plugins/ directory")
        return f

    check_marketplace(repo, f)

    for skill_md in sorted(plugins_dir.glob("*/skills/*/SKILL.md")):
        plugin_dir = skill_md.parent.parent.parent
        check_frontmatter(skill_md, f)
        check_script_refs(skill_md, plugin_dir, f)

    for path in sorted(plugins_dir.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".py", ".sh", ".json"}:
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            check_host_paths(path, f)

    return f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--repo", type=Path, default=REPO_ROOT,
                    help="marketplace repo root (default: this script's repo)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only failures")
    args = ap.parse_args(argv)

    f = lint(args.repo.resolve())

    for w in f.warnings:
        print(f"warn:  {w}")
    for e in f.errors:
        print(f"ERROR: {e}")

    if f.ok:
        if not args.quiet:
            print("lint_skills: clean")
        return 0
    print(f"\nlint_skills: {len(f.errors)} error(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
