"""Shared library for multiplai plugin scripts.

Modules:
    paths         — Centralized path resolution with env-var cascade
    model_client  — Abstract LLM client (Agent SDK / Anthropic API)
    log_utils     — Logging setup with file + stderr handlers
    config        — JSON/YAML config file loader
    venv_guard    — Venv re-exec guard for hook scripts
"""
