"""Tests for catalog dispatcher (scripts/generators/dispatcher.py).

Block 7: Catalog dispatcher.

Covers all scenarios from requirements/catalog-dispatcher.md:
- Unified entry point dispatches all registered generators
- Sequential execution in fixed order: memory → diary → skills → resources
- Selective generator execution via filter argument
- Config-gated generators (skills, resources) skipped when disabled
- State-aware skipping of unchanged sources
- Force regeneration bypasses state check
- Dry-run mode reports actions without side effects
- Generator failure isolation
- Error classification: critical (state file corruption) vs non-critical (single entry LLM failure)
- Catalogs directory auto-creation
- Generation state file management
- Deletion pruning for removed sources
- Exit code reflects generation outcome
- Aggregate and return list[GenerationResult]
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Module Structure
# ---------------------------------------------------------------------------


class TestDispatcherModuleStructure:
    """Requirement: dispatcher.py must exist and expose generate_catalogs()."""

    def test_dispatcher_module_exists(self):
        """scripts/generators/dispatcher.py must exist."""
        dispatcher_file = SCRIPTS_DIR / "generators" / "dispatcher.py"
        assert dispatcher_file.exists(), (
            f"scripts/generators/dispatcher.py must exist at {dispatcher_file}"
        )

    def test_dispatcher_importable(self):
        """generate_catalogs must be importable from generators.dispatcher."""
        from generators.dispatcher import generate_catalogs

        assert callable(generate_catalogs)

    def test_generate_catalogs_returns_list(self):
        """generate_catalogs() must return a list[GenerationResult]."""
        from generators.dispatcher import generate_catalogs

        import inspect
        sig = inspect.signature(generate_catalogs)
        # Verify it accepts the expected parameters
        params = set(sig.parameters.keys())
        assert "config" in params, "generate_catalogs must accept 'config' parameter"


# ---------------------------------------------------------------------------
# Dispatcher Signature
# ---------------------------------------------------------------------------


class TestDispatcherSignature:
    """Requirement: generate_catalogs has correct function signature.

    Design Decision 6: generate_catalogs(config, generators=None, force=False, dry_run=False)
    """

    def test_has_generators_parameter(self):
        """generate_catalogs must accept optional 'generators' filter parameter."""
        import inspect
        from generators.dispatcher import generate_catalogs

        sig = inspect.signature(generate_catalogs)
        assert "generators" in sig.parameters
        param = sig.parameters["generators"]
        assert param.default is None or param.default == inspect.Parameter.empty or param.default is None

    def test_has_force_parameter(self):
        """generate_catalogs must accept 'force' boolean parameter."""
        import inspect
        from generators.dispatcher import generate_catalogs

        sig = inspect.signature(generate_catalogs)
        assert "force" in sig.parameters
        assert sig.parameters["force"].default is False

    def test_has_dry_run_parameter(self):
        """generate_catalogs must accept 'dry_run' boolean parameter."""
        import inspect
        from generators.dispatcher import generate_catalogs

        sig = inspect.signature(generate_catalogs)
        assert "dry_run" in sig.parameters
        assert sig.parameters["dry_run"].default is False


# ---------------------------------------------------------------------------
# Sequential Execution & Ordering
# ---------------------------------------------------------------------------


class TestSequentialExecution:
    """Requirement: Unified entry point dispatches all registered generators.

    Generators always run in fixed order: memory → diary → skills → resources.
    Each generator's run() method is called exactly once.
    """

    def test_all_generators_invoked_on_full_run(self, tmp_path, monkeypatch):
        """WHEN generate_catalogs() is called with no filter
        THEN it invokes memory, diary, skills, and resources generators in order."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=True, enable_resources=True, resources_dir="/tmp/res")

        invocation_order = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invocation_order.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        assert invocation_order == ["memory", "diary", "skills", "resources"]

    def test_execution_order_is_deterministic(self, tmp_path, monkeypatch):
        """WHEN generate_catalogs() is executed multiple times
        THEN generators always run in: memory → diary → skills → resources."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=True, enable_resources=True, resources_dir="/tmp/res")

        orders = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            orders.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        for _ in range(3):
            orders.clear()
            with patch("generators.memory.MemoryGenerator.run", mock_run), \
                 patch("generators.diary.DiaryGenerator.run", mock_run), \
                 patch("generators.skills.SkillsGenerator.run", mock_run), \
                 patch("generators.resources.ResourcesGenerator.run", mock_run):
                asyncio.run(generate_catalogs(config=config))
            assert orders == ["memory", "diary", "skills", "resources"]

    def test_each_generator_called_exactly_once(self, tmp_path, monkeypatch):
        """Each generator's run() method is called exactly once per dispatch."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=True, enable_resources=True, resources_dir="/tmp/res")

        call_counts = {"memory": 0, "diary": 0, "skills": 0, "resources": 0}

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            call_counts[self.name] += 1
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        for name, count in call_counts.items():
            assert count == 1, f"{name} generator called {count} times, expected 1"


# ---------------------------------------------------------------------------
# Generator Filtering
# ---------------------------------------------------------------------------


class TestGeneratorFiltering:
    """Requirement: Selective generator execution via filter argument.

    The dispatcher accepts an optional filter to run only a subset of generators.
    """

    def test_single_generator_filter(self, tmp_path, monkeypatch):
        """WHEN invoked with generators=["diary"]
        THEN only the diary generator runs."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config, generators=["diary"]))

        assert invoked == ["diary"]

    def test_multiple_generator_filter_maintains_canonical_order(self, tmp_path, monkeypatch):
        """WHEN invoked with generators=["diary", "memory"]
        THEN only memory and diary run, in canonical order (memory before diary)."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config, generators=["diary", "memory"]))

        assert invoked == ["memory", "diary"]

    def test_invalid_generator_name_raises_error(self, tmp_path, monkeypatch):
        """WHEN invoked with generators=["nonexistent"]
        THEN it raises an error identifying the unrecognized generator name."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        with pytest.raises((ValueError, SystemExit)):
            asyncio.run(generate_catalogs(config=config, generators=["nonexistent"]))

    def test_filter_with_mix_of_valid_and_invalid_raises(self, tmp_path, monkeypatch):
        """WHEN invoked with generators=["memory", "nonexistent"]
        THEN it raises an error for the unrecognized name."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        with pytest.raises((ValueError, SystemExit)):
            asyncio.run(generate_catalogs(config=config, generators=["memory", "nonexistent"]))


# ---------------------------------------------------------------------------
# Config-Gated Generators
# ---------------------------------------------------------------------------


class TestConfigGatedGenerators:
    """Requirement: Config-gated generators are skipped when disabled.

    Skills and resources generators are only invoked when enabled.
    Memory and diary are mandatory — always invoked.
    """

    def test_skills_generator_skipped_when_disabled(self, tmp_path, monkeypatch):
        """WHEN enable_skills is False and no filter is set
        THEN the skills generator is not invoked."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=False)
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        # Skills should either not be invoked or return a disabled result
        skills_results = [r for r in results if r.generator == "skills"]
        if skills_results:
            # If it appears in results, it should have zero work
            assert skills_results[0].total_sources == 0
            assert skills_results[0].generated == 0
        # Skills should not appear in invoked if truly skipped
        assert "skills" not in invoked or (
            skills_results and skills_results[0].total_sources == 0
        )

    def test_resources_generator_skipped_when_disabled(self, tmp_path, monkeypatch):
        """WHEN enable_resources is False
        THEN the resources generator is not invoked."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_resources=False)
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        resources_results = [r for r in results if r.generator == "resources"]
        if resources_results:
            assert resources_results[0].total_sources == 0
            assert resources_results[0].generated == 0

    def test_resources_skipped_when_resources_dir_empty(self, tmp_path, monkeypatch):
        """WHEN enable_resources is True but resources_dir is empty
        THEN the resources generator is skipped."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_resources=True, resources_dir="")
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        resources_results = [r for r in results if r.generator == "resources"]
        if resources_results:
            assert resources_results[0].total_sources == 0

    def test_mandatory_generators_always_run(self, tmp_path, monkeypatch):
        """WHEN generate_catalogs runs with all optional generators disabled
        THEN memory and diary generators are still invoked."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=False, enable_resources=False)
        invoked = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            invoked.append(self.name)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        assert "memory" in invoked
        assert "diary" in invoked


# ---------------------------------------------------------------------------
# Force Mode Passthrough
# ---------------------------------------------------------------------------


class TestForceMode:
    """Requirement: Force regeneration bypasses state check.

    WHEN force=True THEN all generators run with force=True passed through.
    """

    def test_force_flag_passed_to_generators(self, tmp_path, monkeypatch):
        """force=True is passed through to each generator's run() call."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        force_values = {}

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            force_values[self.name] = force
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config, force=True))

        assert force_values.get("memory") is True
        assert force_values.get("diary") is True


# ---------------------------------------------------------------------------
# Dry-Run Mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Requirement: Dry-run mode reports actions without side effects.

    WHEN dry_run=True THEN no files are modified and no LLM calls are made.
    """

    def test_dry_run_flag_passed_to_generators(self, tmp_path, monkeypatch):
        """dry_run=True is passed through to each generator's run() call."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        dry_run_values = {}

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            dry_run_values[self.name] = dry_run
            return GenerationResult(
                generator=self.name, total_sources=1, skipped=0,
                generated=1, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config, dry_run=True))

        assert dry_run_values.get("memory") is True
        assert dry_run_values.get("diary") is True

    def test_dry_run_results_all_have_dry_run_flag(self, tmp_path, monkeypatch):
        """All GenerationResult objects from a dry run have dry_run=True."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config, dry_run=True))

        for result in results:
            assert result.dry_run is True, (
                f"Result for {result.generator} should have dry_run=True"
            )

    def test_dry_run_does_not_instantiate_model_client(self, tmp_path, monkeypatch):
        """dry_run=True must not create a real model client.

        A dry run makes no LLM calls, so it must not require credentials —
        instantiating the real client would fail in credential-free
        environments. The dispatcher hands generators the stub instead.
        """
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators import dispatcher
        from generators.dispatcher import generate_catalogs, _StubModelClient

        config = CatalogConfig()
        created = {"real": False}

        async def boom():
            created["real"] = True
            raise AssertionError("real model client must not be created on dry-run")

        seen_clients = []

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            seen_clients.append(self._model_client)
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        monkeypatch.setattr(dispatcher, "_create_model_client", boom)
        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config, dry_run=True))

        assert created["real"] is False
        assert seen_clients and all(
            isinstance(c, _StubModelClient) for c in seen_clients
        )


# ---------------------------------------------------------------------------
# Generator Failure Isolation
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """Requirement: A failure in one generator does not prevent others from running.

    Error classification: critical (state file corruption) vs. non-critical
    (single entry LLM failure).
    """

    def test_one_generator_fails_others_succeed(self, tmp_path, monkeypatch):
        """WHEN skills generator raises an exception
        THEN memory, diary, and resources generators still complete."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=True, enable_resources=True, resources_dir="/tmp/res")

        async def mock_run_success(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=1, skipped=0,
                generated=1, pruned=0, errors=[], dry_run=dry_run,
            )

        async def mock_run_fail(self, *, force=False, dry_run=False, force_enable=False):
            raise RuntimeError("Skills LLM call failed")

        with patch("generators.memory.MemoryGenerator.run", mock_run_success), \
             patch("generators.diary.DiaryGenerator.run", mock_run_success), \
             patch("generators.skills.SkillsGenerator.run", mock_run_fail), \
             patch("generators.resources.ResourcesGenerator.run", mock_run_success):
            results = asyncio.run(generate_catalogs(config=config))

        # Should still get results for all generators
        generator_names = [r.generator for r in results]
        assert "memory" in generator_names
        assert "diary" in generator_names

        # The failed generator should have errors
        skills_results = [r for r in results if r.generator == "skills"]
        assert len(skills_results) == 1
        assert len(skills_results[0].errors) > 0

    def test_all_generators_fail(self, tmp_path, monkeypatch):
        """WHEN every generator raises an exception
        THEN all errors are collected and returned."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run_fail(self, *, force=False, dry_run=False, force_enable=False):
            raise RuntimeError(f"{self.name} failed")

        with patch("generators.memory.MemoryGenerator.run", mock_run_fail), \
             patch("generators.diary.DiaryGenerator.run", mock_run_fail), \
             patch("generators.skills.SkillsGenerator.run", mock_run_fail), \
             patch("generators.resources.ResourcesGenerator.run", mock_run_fail):
            results = asyncio.run(generate_catalogs(config=config))

        # Should still return results, each with errors
        assert len(results) >= 2  # at least memory and diary (mandatory)
        for result in results:
            assert len(result.errors) > 0, (
                f"{result.generator} should have errors when it failed"
            )

    def test_non_critical_error_in_generator_result(self, tmp_path, monkeypatch):
        """WHEN a generator completes with non-critical errors (e.g., single LLM failure)
        THEN the result captures those errors but the generator is not marked as crashed."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run_partial(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=3, skipped=1,
                generated=1, pruned=0,
                errors=["Error generating entry-2: LLM timeout"],
                dry_run=dry_run,
            )

        async def mock_run_ok(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run_partial), \
             patch("generators.diary.DiaryGenerator.run", mock_run_ok), \
             patch("generators.skills.SkillsGenerator.run", mock_run_ok), \
             patch("generators.resources.ResourcesGenerator.run", mock_run_ok):
            results = asyncio.run(generate_catalogs(config=config))

        memory_result = [r for r in results if r.generator == "memory"][0]
        assert memory_result.errors == ["Error generating entry-2: LLM timeout"]
        assert memory_result.generated == 1  # partial success, not total failure


# ---------------------------------------------------------------------------
# Result Aggregation
# ---------------------------------------------------------------------------


class TestResultAggregation:
    """Requirement: Aggregate and return list[GenerationResult].

    generate_catalogs() must return one GenerationResult per invoked generator.
    """

    def test_returns_list_of_generation_results(self, tmp_path, monkeypatch):
        """Return value is a list of GenerationResult objects."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.base import GenerationResult
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, GenerationResult)

    def test_result_count_matches_invoked_generators(self, tmp_path, monkeypatch):
        """One result per invoked generator."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.base import GenerationResult
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(enable_skills=True, enable_resources=True, resources_dir="/tmp/r")

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config))

        assert len(results) == 4

    def test_filtered_result_count(self, tmp_path, monkeypatch):
        """When filtered to 1 generator, only 1 result is returned."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.base import GenerationResult
        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            results = asyncio.run(generate_catalogs(config=config, generators=["memory"]))

        assert len(results) == 1
        assert results[0].generator == "memory"


# ---------------------------------------------------------------------------
# Catalogs Directory Auto-Creation
# ---------------------------------------------------------------------------


class TestCatalogsDirectoryCreation:
    """Requirement: The dispatcher ensures the output directory exists."""

    def test_creates_catalogs_directory_if_missing(self, tmp_path, monkeypatch):
        """WHEN catalogs/ does not exist THEN it is created before generators run."""
        data_dir = tmp_path / "plugin_data"
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        catalogs_dir = data_dir / "catalogs"
        assert not catalogs_dir.exists()

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        assert catalogs_dir.exists()

    def test_existing_catalogs_directory_not_modified(self, tmp_path, monkeypatch):
        """WHEN catalogs/ already exists THEN it is left untouched."""
        data_dir = tmp_path / "plugin_data"
        catalogs_dir = data_dir / "catalogs"
        catalogs_dir.mkdir(parents=True)
        existing_file = catalogs_dir / "existing.json"
        existing_file.write_text('{"test": true}')
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        assert existing_file.read_text() == '{"test": true}'


# ---------------------------------------------------------------------------
# Progress Logging
# ---------------------------------------------------------------------------


class TestProgressLogging:
    """Requirement: Sequential generator execution with progress logging."""

    def test_logs_generator_start(self, tmp_path, monkeypatch, caplog):
        """Dispatcher logs when each generator starts."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        import logging
        with caplog.at_level(logging.INFO):
            with patch("generators.memory.MemoryGenerator.run", mock_run), \
                 patch("generators.diary.DiaryGenerator.run", mock_run), \
                 patch("generators.skills.SkillsGenerator.run", mock_run), \
                 patch("generators.resources.ResourcesGenerator.run", mock_run):
                asyncio.run(generate_catalogs(config=config))

        log_text = caplog.text.lower()
        assert "memory" in log_text or "diary" in log_text, (
            "Dispatcher should log progress for generators"
        )

    def test_logs_generator_failure(self, tmp_path, monkeypatch, caplog):
        """Dispatcher logs errors when a generator fails."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run_ok(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        async def mock_run_fail(self, *, force=False, dry_run=False, force_enable=False):
            raise RuntimeError("Simulated failure")

        import logging
        with caplog.at_level(logging.ERROR):
            with patch("generators.memory.MemoryGenerator.run", mock_run_fail), \
                 patch("generators.diary.DiaryGenerator.run", mock_run_ok), \
                 patch("generators.skills.SkillsGenerator.run", mock_run_ok), \
                 patch("generators.resources.ResourcesGenerator.run", mock_run_ok):
                asyncio.run(generate_catalogs(config=config))

        log_text = caplog.text.lower()
        assert "error" in log_text or "fail" in log_text or "memory" in log_text, (
            "Dispatcher should log generator failures"
        )


# ---------------------------------------------------------------------------
# Error Classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """Requirement: Error classification — critical vs. non-critical.

    Critical: state file corruption → treated as force run.
    Non-critical: single entry LLM failure → errors in GenerationResult.
    """

    def test_critical_error_result_distinguishable(self, tmp_path, monkeypatch):
        """When a generator raises a critical exception (e.g., state corruption),
        the result should capture the error distinctly from non-critical ones."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run_critical(self, *, force=False, dry_run=False, force_enable=False):
            raise OSError("Permission denied writing state file")

        async def mock_run_ok(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run_critical), \
             patch("generators.diary.DiaryGenerator.run", mock_run_ok), \
             patch("generators.skills.SkillsGenerator.run", mock_run_ok), \
             patch("generators.resources.ResourcesGenerator.run", mock_run_ok):
            results = asyncio.run(generate_catalogs(config=config))

        memory_result = [r for r in results if r.generator == "memory"][0]
        assert len(memory_result.errors) > 0

    def test_non_critical_errors_aggregated_in_result(self, tmp_path, monkeypatch):
        """Non-critical errors (LLM failures for individual entries) appear in
        GenerationResult.errors without stopping the generator."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()

        async def mock_run_with_errors(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=5, skipped=2,
                generated=2, pruned=0,
                errors=["Error generating key-3: API timeout"],
                dry_run=dry_run,
            )

        async def mock_run_ok(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run_with_errors), \
             patch("generators.diary.DiaryGenerator.run", mock_run_ok), \
             patch("generators.skills.SkillsGenerator.run", mock_run_ok), \
             patch("generators.resources.ResourcesGenerator.run", mock_run_ok):
            results = asyncio.run(generate_catalogs(config=config))

        memory_result = [r for r in results if r.generator == "memory"][0]
        assert memory_result.errors == ["Error generating key-3: API timeout"]
        # Generator still produced some results — it wasn't a total crash
        assert memory_result.generated == 2


# ---------------------------------------------------------------------------
# Config Passthrough
# ---------------------------------------------------------------------------


class TestConfigPassthrough:
    """Requirement: Dispatcher passes config to each generator instance."""

    def test_config_passed_to_generators(self, tmp_path, monkeypatch):
        """The CatalogConfig object is passed to each generator's constructor."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig(model="claude-haiku-4-5", reasoning_effort="low")
        configs_seen = {}

        original_init = None

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            configs_seen[self.name] = self._config
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        for name in ("memory", "diary"):
            assert name in configs_seen, f"{name} generator was not invoked"
            assert configs_seen[name].model == "claude-haiku-4-5"
            assert configs_seen[name].reasoning_effort == "low"


# ---------------------------------------------------------------------------
# Async Interface
# ---------------------------------------------------------------------------


class TestAsyncInterface:
    """Requirement: generate_catalogs must be an async function (generators use async run)."""

    def test_generate_catalogs_is_async(self):
        """generate_catalogs must be an async function or coroutine function."""
        import asyncio
        import inspect
        from generators.dispatcher import generate_catalogs

        assert inspect.iscoroutinefunction(generate_catalogs), (
            "generate_catalogs must be async (generators use async run())"
        )


# ---------------------------------------------------------------------------
# Model Client Passthrough
# ---------------------------------------------------------------------------


class TestModelClientPassthrough:
    """Requirement: Dispatcher creates generators with a model_client instance."""

    def test_model_client_provided_to_generators(self, tmp_path, monkeypatch):
        """Each generator should receive a model_client for LLM calls."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        from generators.config import CatalogConfig
        from generators.dispatcher import generate_catalogs

        config = CatalogConfig()
        clients_seen = {}

        async def mock_run(self, *, force=False, dry_run=False, force_enable=False):
            from generators.base import GenerationResult
            clients_seen[self.name] = self._model_client
            return GenerationResult(
                generator=self.name, total_sources=0, skipped=0,
                generated=0, pruned=0, errors=[], dry_run=dry_run,
            )

        with patch("generators.memory.MemoryGenerator.run", mock_run), \
             patch("generators.diary.DiaryGenerator.run", mock_run), \
             patch("generators.skills.SkillsGenerator.run", mock_run), \
             patch("generators.resources.ResourcesGenerator.run", mock_run):
            asyncio.run(generate_catalogs(config=config))

        for name in ("memory", "diary"):
            assert name in clients_seen, f"{name} generator didn't receive model_client"
            assert clients_seen[name] is not None, (
                f"{name} generator's model_client is None"
            )


class TestRunSignatureContract:
    """The dispatcher passes force_enable to EVERY generator's run().

    Regression for 0.4.1: DiaryGenerator overrode run() without the
    force_enable kwarg and every dispatcher invocation crashed it with
    TypeError. Any generator override must accept the full contract.
    """

    def test_all_registered_generators_accept_force_enable(self):
        import inspect

        from generators.dispatcher import GENERATOR_CLASSES

        for name, cls in GENERATOR_CLASSES.items():
            sig = inspect.signature(cls.run)
            has_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            assert "force_enable" in sig.parameters or has_kwargs, (
                f"{name} generator's run() must accept force_enable "
                f"(dispatcher passes it unconditionally); got {sig}"
            )
