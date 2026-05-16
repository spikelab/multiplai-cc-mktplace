"""Generator base class and state management.

Provides GeneratorBase (template method pattern), GenerationResult,
and GenerationState for catalog generation lifecycle orchestration.

Design Decision 1: Shared base class with template method pattern.
Design Decision 2: Content hashing at source-unit granularity.
Design Decision 3: Separate .generation-state.json sidecar file.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.paths import Paths

logger = logging.getLogger(__name__)

# Retryable HTTP status codes
RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.1  # seconds

STATE_SCHEMA_VERSION = 1
# 1.2.0 adds:
#   - section_anchors to memory entries (hand-authored, preserved)
#   - intent_domains/anti_domains to resources entries (LLM-generated)
#   - renames skills "triggers" -> "intent_domains" (consistency)
# 1.1.0 added intent_domains/anti_domains to memory entries.
# Catalogs pinned to older versions are invalidated automatically by
# the catalog-read path; regeneration produces the new shape.
CATALOG_SCHEMA_VERSION = "1.2.0"

# Keys used to identify the source key in catalog entries
_ENTRY_KEY_FIELDS = ("source", "path", "file")


def _atomic_write_json(target: Path, data: dict) -> None:
    """Write JSON data to a file atomically (write-to-temp-then-rename).

    Creates parent directories if needed. On failure, cleans up the
    temp file and re-raises the exception.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _entry_key(entry: dict) -> str:
    """Extract the source key from a catalog entry.

    Tries 'source', 'path', and 'file' fields in order.
    """
    for field_name in _ENTRY_KEY_FIELDS:
        value = entry.get(field_name, "")
        if value:
            return value
    return ""


def _is_retryable(error: Exception) -> bool:
    """Check whether an exception represents a retryable LLM error."""
    status_code = getattr(error, "status_code", None)
    return status_code is not None and status_code in RETRYABLE_STATUS_CODES


@dataclass
class GenerationResult:
    """Result of a generation run.

    Reports what happened: counts of skipped/generated/pruned entries,
    any errors encountered, and whether this was a dry run.
    """

    generator: str
    total_sources: int
    skipped: int
    generated: int
    pruned: int
    errors: list[str]
    dry_run: bool


@dataclass
class _ProcessingBatch:
    """Accumulated results from processing all sources in a run.

    Collects entries, hashes, and counters as sources are processed
    one by one, then handed back to the run() orchestrator.
    """

    entries: dict[str, dict] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    skipped: int = 0
    generated: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class GenerationState:
    """Tracks per-generator source hashes and generation metadata.

    State file schema:
    {
      "schema_version": 1,
      "generators": {
        "<name>": {
          "last_run": "ISO-8601",
          "source_hashes": { "key": "hash..." },
          "entry_count": N
        }
      }
    }
    """

    schema_version: int = STATE_SCHEMA_VERSION
    generators: dict[str, dict[str, Any]] = field(default_factory=dict)


class GeneratorBase:
    """Base class for catalog generators using template method pattern.

    Subclasses must define:
      - name (str): generator name for state namespacing
      - catalog_filename (str): output catalog filename
      - discover_sources() -> dict[str, Path]: find raw files to catalog
      - build_prompt(source: Path) -> str: LLM prompt for one entry
      - parse_response(raw: str) -> dict: parse LLM output

    Optional overrides:
      - merge_entry(existing, new) -> dict: preserve hand-authored fields
      - hash_source(path: Path) -> str: custom content hashing
    """

    name: str = ""
    catalog_filename: str = ""

    def __init__(self, *, config, model_client):
        self._config = config
        self._model_client = model_client

    # ---- Properties (overridable by subclass) ----

    @property
    def _catalogs_dir(self) -> Path:
        """Directory where catalogs and state are stored.

        Routed through the path resolver so the workspace/standalone
        fallbacks apply when CLAUDE_PLUGIN_DATA is unset (manual/CLI
        runs). Reading the env var directly would resolve to a relative
        ``catalogs/`` under cwd and scatter state across projects.
        Resolved fresh (not the cached singleton) so it tracks env
        changes within the process.
        """
        return Paths.resolve().catalogs_dir()

    @property
    def _state_file(self) -> Path:
        """Path to the shared generation state file."""
        return self._catalogs_dir / ".generation-state.json"

    # ---- Subclass hooks ----

    def discover_sources(self) -> dict[str, Any]:
        """Find raw source files to catalog. Returns {key: source_object}."""
        raise NotImplementedError

    def hash_source(self, path: Path) -> str:
        """Compute a deterministic content hash of a source file."""
        content = path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def build_prompt(self, source) -> str:
        """Build an LLM prompt for one source entry."""
        raise NotImplementedError

    def parse_response(self, raw: str) -> dict:
        """Parse LLM response text into a catalog entry dict."""
        raise NotImplementedError

    def _disabled_result(self, dry_run: bool = False) -> GenerationResult:
        """Create a zero-work result for when the generator is disabled or skipped.

        Used by config-gated generators (skills, resources) to return early
        without touching any catalog or state files.
        """
        return GenerationResult(
            generator=self.name,
            total_sources=0,
            skipped=0,
            generated=0,
            pruned=0,
            errors=[],
            dry_run=dry_run,
        )

    def merge_entry(self, existing: dict | None, new: dict) -> dict:
        """Merge new LLM-generated entry with existing catalog entry.

        Default: return new entry as-is. Override to preserve hand-authored fields.
        """
        return new

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """Parse JSON from an LLM response, stripping markdown code fences.

        Handles responses wrapped in ```json ... ``` or plain ``` ... ```.
        Raises json.JSONDecodeError if the content is not valid JSON.
        """
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        return json.loads(text)

    # ---- Template method ----

    async def run(self, *, force: bool = False, dry_run: bool = False) -> GenerationResult:
        """Orchestrate the full generation lifecycle.

        1. Ensure catalogs directory exists
        2. Load state and existing catalog
        3. Discover sources and compare hashes
        4. Generate entries for changed/new sources (or report in dry_run)
        5. Prune deleted sources
        6. Write catalog and state (unless dry_run)
        """
        self._catalogs_dir.mkdir(parents=True, exist_ok=True)

        state = self._load_state()
        existing_by_key = self._build_entry_index(self._read_catalog())
        sources = self.discover_sources()
        stored_hashes = state.generators.get(self.name, {}).get("source_hashes", {})

        # Process each source: skip, dry-run report, or generate via LLM
        batch = await self._process_sources(
            sources, stored_hashes, existing_by_key, force=force, dry_run=dry_run
        )

        # Prune entries for deleted sources (hashes are implicitly pruned
        # because batch.hashes only contains keys from current sources)
        pruned = self._prune_deleted(sources, stored_hashes, batch.entries)

        # No real model client: do NOT persist. Writing empty stub output
        # and recording its source hashes would make every later run
        # "skip unchanged" and lock the catalog permanently empty.
        # `is True` (not just truthy): a MagicMock test client would
        # auto-create a truthy .is_stub attribute and wrongly skip writes.
        stub = getattr(self._model_client, "is_stub", False) is True
        errors = batch.errors
        if stub:
            errors = errors + [
                "model client unavailable — catalog not generated or persisted"
            ]
        elif not dry_run:
            self._write_results(state, batch.entries, batch.hashes)

        return GenerationResult(
            generator=self.name,
            total_sources=len(sources),
            skipped=batch.skipped,
            generated=batch.generated,
            pruned=pruned,
            errors=errors,
            dry_run=dry_run,
        )

    # ---- Run helpers ----

    @staticmethod
    def _build_entry_index(catalog: dict) -> dict[str, dict]:
        """Build a lookup of existing catalog entries keyed by source key."""
        index = {}
        for entry in catalog.get("entries", []):
            key = _entry_key(entry)
            if key:
                index[key] = entry
        return index

    async def _process_sources(
        self,
        sources: dict[str, Any],
        stored_hashes: dict[str, str],
        existing_by_key: dict[str, dict],
        *,
        force: bool,
        dry_run: bool,
    ) -> _ProcessingBatch:
        """Classify and process each source: skip, dry-run, or generate."""
        batch = _ProcessingBatch()

        for key, source in sources.items():
            current_hash = self.hash_source(source)
            batch.hashes[key] = current_hash

            if self._should_skip(key, current_hash, stored_hashes, force):
                batch.skipped += 1
                if key in existing_by_key:
                    batch.entries[key] = existing_by_key[key]
                continue

            if dry_run:
                batch.generated += 1
                if key in existing_by_key:
                    batch.entries[key] = existing_by_key[key]
                continue

            await self._generate_or_preserve(
                key, source, existing_by_key, stored_hashes, batch
            )

        return batch

    async def _generate_or_preserve(
        self,
        key: str,
        source: Any,
        existing_by_key: dict[str, dict],
        stored_hashes: dict[str, str],
        batch: _ProcessingBatch,
    ) -> None:
        """Generate a single entry via LLM, falling back to the existing entry on error.

        On success, adds the merged entry to the batch. On failure, preserves
        the prior entry (if any) and restores the old hash so the source is
        retried on the next run.
        """
        entry, error = await self._generate_entry(key, source, existing_by_key)
        if error:
            batch.errors.append(error)
            if key in existing_by_key:
                batch.entries[key] = existing_by_key[key]
            # Restore previous hash so the entry is retried next run
            if key in stored_hashes:
                batch.hashes[key] = stored_hashes[key]
            else:
                del batch.hashes[key]
        else:
            batch.entries[key] = entry
            batch.generated += 1

    @staticmethod
    def _should_skip(
        key: str, current_hash: str, stored_hashes: dict[str, str], force: bool
    ) -> bool:
        """Determine whether a source should be skipped (unchanged hash)."""
        if force:
            return False
        stored_hash = stored_hashes.get(key)
        return stored_hash is not None and stored_hash == current_hash

    async def _generate_entry(
        self, key: str, source: Any, existing_by_key: dict[str, dict]
    ) -> tuple[dict | None, str | None]:
        """Generate a single catalog entry via LLM.

        Returns (entry_dict, None) on success or (None, error_message) on failure.
        """
        try:
            raw_response = await self._call_llm(self.build_prompt(source))
            parsed = self.parse_response(raw_response)
            parsed["source"] = key

            existing = existing_by_key.get(key)
            merged = self.merge_entry(existing, parsed)
            merged["source"] = key

            return merged, None
        except Exception as e:
            error_msg = f"Error generating {key}: {e}"
            logger.error(error_msg)
            return None, error_msg

    @staticmethod
    def _prune_deleted(
        sources: dict[str, Any],
        stored_hashes: dict[str, str],
        new_entries: dict[str, dict],
    ) -> int:
        """Remove entries for deleted sources. Returns count of pruned entries."""
        deleted_keys = set(stored_hashes.keys()) - set(sources.keys())
        for key in deleted_keys:
            new_entries.pop(key, None)
        return len(deleted_keys)

    def _write_results(
        self,
        state: GenerationState,
        new_entries: dict[str, dict],
        new_hashes: dict[str, str],
    ) -> None:
        """Write catalog and update generation state."""
        catalog = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entries": list(new_entries.values()),
        }
        self._write_catalog(catalog)

        state.generators[self.name] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "source_hashes": new_hashes,
            "entry_count": len(new_entries),
        }
        self._save_state(state)

    # ---- State I/O ----

    def _load_state(self) -> GenerationState:
        """Load generation state from .generation-state.json.

        Returns empty state if file doesn't exist or is corrupt.
        """
        if not self._state_file.exists():
            return GenerationState()

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("State file is not a dict, starting fresh")
                return GenerationState()
            return GenerationState(
                schema_version=data.get("schema_version", STATE_SCHEMA_VERSION),
                generators=data.get("generators", {}),
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Corrupt state file, starting fresh: %s", e)
            return GenerationState()

    def _save_state(self, state: GenerationState) -> None:
        """Save generation state atomically (write-to-temp-then-rename)."""
        data = {
            "schema_version": state.schema_version,
            "generators": state.generators,
        }
        _atomic_write_json(self._state_file, data)

    # ---- Catalog I/O ----

    def _read_catalog(self) -> dict:
        """Read existing catalog JSON file.

        Returns empty catalog structure if file doesn't exist or is corrupt.
        """
        catalog_file = self._catalogs_dir / self.catalog_filename
        empty = {"schema_version": CATALOG_SCHEMA_VERSION, "entries": []}

        if not catalog_file.exists():
            return empty

        try:
            data = json.loads(catalog_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return empty
            return data
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt catalog file %s, starting fresh", catalog_file)
            return empty

    def _write_catalog(self, catalog: dict) -> None:
        """Write catalog JSON atomically (write-to-temp-then-rename)."""
        catalog_file = self._catalogs_dir / self.catalog_filename
        _atomic_write_json(catalog_file, catalog)

    # ---- LLM Client ----

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM via model_client with retry logic for transient failures.

        Retries on status codes 429, 500, 502, 503 with exponential backoff.
        Non-retryable errors (400, 401, 403, 404) are raised immediately.
        """
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._model_client.query(
                    system="You are a catalog generator. Produce structured JSON output.",
                    messages=[{"role": "user", "content": prompt}],
                    model=self._config.model,
                )
                return response.content
            except Exception as e:
                is_last_attempt = attempt >= MAX_RETRIES
                if _is_retryable(e) and not is_last_attempt:
                    delay = RETRY_BACKOFF_BASE * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                raise
