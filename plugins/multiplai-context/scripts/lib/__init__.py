"""Context-specific library for multiplai plugin scripts.

Generic core modules (paths, config, log_utils, model_client) now live in
the standalone `multiplai_core` package. This package holds the plugin's
context-specific modules:

    extraction          — Learning/diary extraction
    memory_router       — Memory routing logic
    routing_logic       — Prompt routing
    router_prompt       — Router prompt construction
    section_loader      — Memory section loading
    project_identity    — Project identity resolution
    transcript_distiller — Transcript distillation
    transcript_helper   — Transcript helpers
"""
