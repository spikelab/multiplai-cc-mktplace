"""Tripwire: plugin options must be read under the name the harness exports.

Claude Code delivers a plugin's ``userConfig`` values to hook processes as
``CLAUDE_PLUGIN_OPTION_<KEY>`` with ``<KEY>`` **uppercased**
(https://code.claude.com/docs/en/plugins-reference.md). For eight days every
read in this plugin used the lowercase key, so no option ever resolved — and
because every affected option has a falsy/no-op default, the failure mode was
a feature that silently never ran. No error, clean logs (#148).

A lowercase literal can therefore never match anything in production. This
guard fails the build if one reappears, in source or in a docstring — the
docstrings are the documentation, and the old ones taught the bug.

Reads go through :mod:`multiplai_core.plugin_options`, which takes the **bare**
option name and uppercases it once. There is deliberately no lowercase
fallback: accepting both cases would keep a dead name alive as though it meant
something, and would let this very guard pass while the bug persisted.
"""
from __future__ import annotations

import re

from conftest import SCRIPTS_DIR

LOWERCASE_OPTION = re.compile(r"CLAUDE_PLUGIN_OPTION_[a-z]")


def test_no_lowercase_option_name_under_scripts():
    offenders = [
        f"{path.relative_to(SCRIPTS_DIR)}:{n}: {line.strip()}"
        for path in sorted(SCRIPTS_DIR.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if LOWERCASE_OPTION.search(line)
    ]
    assert not offenders, (
        "Plugin options are exported UPPERCASED; a lowercase name can never "
        "match and fails silently in production. Read it via "
        "multiplai_core.plugin_options.option(<bare name>):\n"
        + "\n".join(offenders)
    )


def test_accessor_resolves_uppercase_only(monkeypatch):
    """The contract the guard protects, asserted directly at this layer."""
    from multiplai_core.plugin_options import option

    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_ENABLE_SKILLS", "true")
    assert option("enable_skills", "false") == "true"

    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_ENABLE_SKILLS")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_skills", "true")
    assert option("enable_skills", "false") == "false"


def test_catalog_config_reads_the_uppercase_variable(monkeypatch):
    """End-to-end at the loader the outage was actually reported against."""
    from generators.config import load_catalog_config

    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_ENABLE_SKILLS", "true")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_ENABLE_COSTS", "true")
    cfg = load_catalog_config()
    assert cfg.enable_skills is True
    assert cfg.enable_costs is True
