"""Which plugin ships which skill, and therefore how to invoke it.

A plugin-shipped skill is invoked as ``/plugin:skill``. The bare directory
name is not a valid identifier for it — ``Skill(skill="extract-insights")``
fails with ``Unknown skill``, and ``/extract-insights`` does nothing.

This mattered enough to be worth a module: a 2026-08-07 audit of 111,780 real
tool calls found the ``Skill`` tool failing **23.2%** of the time, and every
one of those failures was an unqualified name (``extract-insights`` ×24,
``youtube-transcript`` ×19, ``deep-research`` ×13). The routing hook was
suggesting ``Invoke with /<name>`` using the catalog's source key, which is the
skill *directory* name, so the hook was the one teaching the wrong form.

Two callers share this so they cannot drift: ``generators/skills.py`` (which
discovers the SKILL.md files) and ``context_manager.py`` (which renders the
invocation hint).
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.fsio import claude_config_dir


def default_plugins_dir(configured: str | None = None) -> Path:
    """Resolve the plugins directory, honouring an explicit config value."""
    if configured:
        return Path(configured).expanduser()
    return claude_config_dir() / "plugins"


def plugin_skills(plugins_dir: str | Path | None = None) -> dict[str, tuple[Path, str]]:
    """Map skill directory name -> (SKILL.md path, owning plugin name).

    Reads ``installed_plugins.json`` (v2 layout:
    ``{"plugins": {"<plugin>@<marketplace>": [{"installPath": ...}, ...]}}``)
    and globs ``<installPath>/skills/*/SKILL.md`` per install record.

    A missing or malformed manifest yields ``{}`` — skill discovery and
    context assembly must never break on it. First writer wins.
    """
    if isinstance(plugins_dir, (str, type(None))):
        root = default_plugins_dir(plugins_dir)
    else:
        root = Path(plugins_dir)

    manifest = root / "installed_plugins.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return {}

    found: dict[str, tuple[Path, str]] = {}
    for qualified_key, records in plugins.items():
        if not isinstance(records, list) or not isinstance(qualified_key, str):
            continue
        # "multiplai-research@multiplai" -> "multiplai-research"
        plugin_name = qualified_key.split("@", 1)[0].strip()
        if not plugin_name:
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            install = rec.get("installPath")
            if not install:
                continue
            for path in sorted(Path(install).glob("skills/*/SKILL.md")):
                if path.is_file():
                    found.setdefault(path.parent.name, (path, plugin_name))
    return found


def plugin_skill_owners(plugins_dir: str | Path | None = None) -> dict[str, str]:
    """Map skill directory name -> owning plugin name."""
    return {name: plugin for name, (_, plugin) in plugin_skills(plugins_dir).items()}


def qualify(
    skill_name: str,
    owners: dict[str, str] | None,
    skills_dir: str | Path | None = None,
) -> str:
    """Return the invocable identifier for ``skill_name``.

    ``plugin:skill`` when the skill is plugin-shipped, otherwise the bare name
    (a user-local skill in ``skills_dir`` really is invoked as ``/<name>``).
    A skill already carrying a ``:`` is returned untouched.

    ``skills_dir`` resolves the collision the catalog generator already
    resolves in the other direction: when a local skill and a plugin skill
    share a directory name, the local one wins and is invoked bare. Without
    this check a local ``writing`` would be advertised under the name of some
    plugin's ``writing``, which is the same class of bug in a new costume.
    """
    if not skill_name or ":" in skill_name:
        return skill_name
    if skills_dir:
        local = Path(skills_dir).expanduser() / skill_name / "SKILL.md"
        if local.is_file():
            return skill_name
    plugin = (owners or {}).get(skill_name)
    return f"{plugin}:{skill_name}" if plugin else skill_name
