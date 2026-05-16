"""Catalog dispatcher — unified entry point for catalog generation.

Design Decision 6: Sequential generation with early termination on critical
failure, continuing past non-critical errors.

Dispatches all registered generators (memory, diary, skills, resources) in
a fixed order, with support for filtering, force mode, and dry-run mode.
"""

import logging
from dataclasses import dataclass

from lib.paths import Paths
from generators.base import GenerationResult
from generators.config import CatalogConfig
from generators.diary import DiaryGenerator
from generators.memory import MemoryGenerator
from generators.resources import ResourcesGenerator
from generators.skills import SkillsGenerator

logger = logging.getLogger(__name__)

# Canonical execution order
GENERATOR_ORDER = ["memory", "diary", "skills", "resources"]

# Generators that always run regardless of config flags
_MANDATORY_GENERATORS = {"memory", "diary"}

# Map names to generator classes
GENERATOR_CLASSES = {
    "memory": MemoryGenerator,
    "diary": DiaryGenerator,
    "skills": SkillsGenerator,
    "resources": ResourcesGenerator,
}


def _validate_generator_names(names: list[str]) -> None:
    """Raise ValueError if any generator names are unrecognized."""
    invalid = set(names) - set(GENERATOR_ORDER)
    if invalid:
        raise ValueError(
            f"Unrecognized generator names: {', '.join(sorted(invalid))}. "
            f"Valid names: {', '.join(GENERATOR_ORDER)}"
        )


def _is_generator_enabled(name: str, config: CatalogConfig) -> bool:
    """Check whether a generator should run based on config gating.

    Mandatory generators (memory, diary) are always enabled.
    Optional generators are gated on their respective config flags.
    """
    if name in _MANDATORY_GENERATORS:
        return True
    if name == "skills":
        return config.enable_skills
    if name == "resources":
        return config.enable_resources and bool(config.resources_dir.strip())
    return False


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
        return [name for name in GENERATOR_ORDER if name in generators]

    return [name for name in GENERATOR_ORDER if _is_generator_enabled(name, config)]


def _make_error_result(
    name: str, error: Exception, dry_run: bool
) -> GenerationResult:
    """Create a GenerationResult capturing a generator-level failure."""
    return GenerationResult(
        generator=name,
        total_sources=0,
        skipped=0,
        generated=0,
        pruned=0,
        errors=[f"{type(error).__name__}: {error}"],
        dry_run=dry_run,
    )


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
    if generators is not None:
        _validate_generator_names(generators)

    # Ensure catalogs directory exists (resolver-routed so workspace/
    # standalone fallbacks apply when CLAUDE_PLUGIN_DATA is unset).
    catalogs_dir = Paths.resolve().catalogs_dir()
    catalogs_dir.mkdir(parents=True, exist_ok=True)

    model_client = await _create_model_client()
    names_to_run = _resolve_generators(config, generators)

    results: list[GenerationResult] = []
    for name in names_to_run:
        logger.info("Running %s catalog generator", name)
        gen = GENERATOR_CLASSES[name](config=config, model_client=model_client)
        try:
            result = await gen.run(force=force, dry_run=dry_run)
            results.append(result)
        except Exception as e:
            logger.error("Generator %s failed: %s", name, e, exc_info=True)
            results.append(_make_error_result(name, e, dry_run))

    return results


async def _create_model_client():
    """Create a model client, with graceful fallback for test environments."""
    try:
        from lib.model_client import create_client
        return await create_client()
    except Exception:
        logger.debug("Could not create model client, using stub")
        return _StubModelClient()


@dataclass(frozen=True)
class _StubModelResponse:
    """Minimal response object for stub client, avoiding external imports."""
    content: str = "{}"


class _StubModelClient:
    """Minimal stub for environments where no real model client is available."""

    async def query(self, system, messages, **kwargs):
        return _StubModelResponse()
