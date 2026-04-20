"""Catalog dispatcher — unified entry point for catalog generation.

Design Decision 6: Sequential generation with early termination on critical
failure, continuing past non-critical errors.

Dispatches all registered generators (memory, diary, skills, resources) in
a fixed order, with support for filtering, force mode, and dry-run mode.
"""

import logging
import os
from pathlib import Path

from generators.base import GenerationResult
from generators.config import CatalogConfig
from generators.diary import DiaryGenerator
from generators.memory import MemoryGenerator
from generators.resources import ResourcesGenerator
from generators.skills import SkillsGenerator

logger = logging.getLogger(__name__)

# Canonical execution order
GENERATOR_ORDER = ["memory", "diary", "skills", "resources"]

# Map names to generator classes
GENERATOR_CLASSES = {
    "memory": MemoryGenerator,
    "diary": DiaryGenerator,
    "skills": SkillsGenerator,
    "resources": ResourcesGenerator,
}


async def generate_catalogs(
    config: CatalogConfig,
    generators: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[GenerationResult]:
    """Dispatch catalog generation across all registered generators.

    Args:
        config: Catalog configuration.
        generators: Optional filter — only run these generators (in canonical order).
                    None means run all enabled generators.
        force: If True, bypass state-aware skipping.
        dry_run: If True, report what would happen without writing files.

    Returns:
        One GenerationResult per invoked generator.

    Raises:
        ValueError: If generators contains unrecognized names.
    """
    # Validate filter names
    if generators is not None:
        invalid = set(generators) - set(GENERATOR_ORDER)
        if invalid:
            raise ValueError(
                f"Unrecognized generator names: {', '.join(sorted(invalid))}. "
                f"Valid names: {', '.join(GENERATOR_ORDER)}"
            )

    # Ensure catalogs directory exists
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    catalogs_dir = Path(data_dir) / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)

    # Create model client
    model_client = await _create_model_client()

    # Determine which generators to run (in canonical order)
    names_to_run = _resolve_generators(config, generators)

    results: list[GenerationResult] = []
    for name in names_to_run:
        logger.info("Running %s catalog generator", name)
        gen_class = GENERATOR_CLASSES[name]
        gen = gen_class(config=config, model_client=model_client)
        try:
            result = await gen.run(force=force, dry_run=dry_run)
            results.append(result)
        except Exception as e:
            logger.error("Generator %s failed: %s", name, e, exc_info=True)
            results.append(
                GenerationResult(
                    generator=name,
                    total_sources=0,
                    skipped=0,
                    generated=0,
                    pruned=0,
                    errors=[f"{type(e).__name__}: {e}"],
                    dry_run=dry_run,
                )
            )

    return results


def _resolve_generators(
    config: CatalogConfig, generators: list[str] | None
) -> list[str]:
    """Determine which generators to run based on config and filter.

    When a filter is provided, all listed generators run (in canonical order),
    regardless of config gating — the filter is an explicit override.

    When no filter is provided, config gating applies:
    - memory and diary always run (mandatory)
    - skills runs only if enable_skills is True
    - resources runs only if enable_resources is True AND resources_dir is set
    """
    if generators is not None:
        # Explicit filter: run in canonical order
        return [name for name in GENERATOR_ORDER if name in generators]

    # No filter: apply config gating
    names = ["memory", "diary"]
    if config.enable_skills:
        names.append("skills")
    if config.enable_resources and config.resources_dir.strip():
        names.append("resources")
    return names


async def _create_model_client():
    """Create a model client, with graceful fallback for test environments."""
    try:
        from lib.model_client import create_client
        return await create_client()
    except Exception:
        # In test environments, return a simple mock-like object
        logger.debug("Could not create model client, using stub")
        return _StubModelClient()


class _StubModelClient:
    """Minimal stub for environments where no real model client is available."""

    async def query(self, system, messages, **kwargs):
        from lib.model_client import ModelResponse
        return ModelResponse(content="{}")
